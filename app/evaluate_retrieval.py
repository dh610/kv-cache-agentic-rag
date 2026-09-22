from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from rag.evaluation import EvalCase, evaluate_dense, passed, select_model, validate_dataset
from runtime.settings import load_settings


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="30-pair retrieval benchmark with pre-registered thresholds"
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument(
        "--run",
        action="store_true",
        help="Download models and execute; default is dataset validation only",
    )
    parser.add_argument(
        "--remediate",
        action="store_true",
        help="If baseline fails, run chunking -> bilingual -> learned sparse -> reranker",
    )
    parser.add_argument(
        "--trust-remote-code",
        action="store_true",
        help="Only after independently reviewing a candidate model implementation",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    settings = load_settings()
    cases = [EvalCase.model_validate(x) for x in json.loads(args.dataset.read_text())]
    validate_dataset(cases, settings)
    if not args.run:
        print(
            "Dataset shape/verification flags valid. Source quotes are checked against PDF chunks when running."
        )
        return 0
    output = args.output or Path("outputs/local") / (
        "retrieval-eval-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json"
    )
    if output.exists():
        raise ValueError("Refusing to overwrite an existing evaluation artifact")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        "policy": settings.evaluation.model_dump(),
        "experiments": [],
        "errors": [],
        "accepted": False,
    }

    def save():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    def measure(stage, model, config, **kwargs):
        try:
            result = evaluate_dense(
                config, cases, model, trust_remote_code=args.trust_remote_code, **kwargs
            )
            result["stage"] = stage
            report["experiments"].append(result)
            save()
            return result
        except Exception as exc:
            report["errors"].append(
                {"stage": stage, "model": model, "error_type": type(exc).__name__}
            )
            save()
            return None

    baseline = [
        r for model in settings.evaluation.candidates if (r := measure("baseline", model, settings))
    ]
    if len(baseline) != len(settings.evaluation.candidates):
        print(f"Incomplete candidate comparison; no final selection. Inspect {output}")
        return 2
    winner = select_model(baseline, settings.evaluation)
    report["selection"] = winner["model"]
    current = winner
    if args.remediate and not passed(current["metrics"], settings.evaluation):
        configs = []
        for size, overlap in [(800, 160), (1600, 240)]:
            config = settings.model_copy(deep=True)
            config.retrieval.chunk_size = size
            config.retrieval.chunk_overlap = overlap
            if r := measure("chunking", winner["model"], config):
                configs.append(r)
        if configs:
            current = max(
                [current, *configs], key=lambda r: (r["metrics"]["mrr"], r["metrics"]["hit@5"])
            )
        config = settings.model_validate(current["settings"])
        if not passed(current["metrics"], settings.evaluation):
            current = measure("bilingual", winner["model"], config, bilingual=True) or current
        if not passed(current["metrics"], settings.evaluation):
            sparse = measure("dense_sparse", "BAAI/bge-m3", config, bilingual=True, sparse=True)
            if sparse and sparse["metrics"]["mrr"] > current["metrics"]["mrr"]:
                current = sparse
        if not passed(current["metrics"], settings.evaluation):
            current = (
                measure(
                    "rerank",
                    current["model"],
                    config,
                    bilingual=True,
                    sparse=current["sparse"],
                    rerank=True,
                )
                or current
            )
    report["final"] = current
    report["accepted"] = passed(current["metrics"], settings.evaluation)
    report["note"] = (
        "Development-set selection only; thresholds do not prove held-out generalization. No config was auto-rewritten."
    )
    save()
    print(f"Results: {output}; accepted={report['accepted']}")
    return 0 if report["accepted"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, FileNotFoundError, ImportError) as exc:
        print(f"Evaluation setup error: {exc}")
        raise SystemExit(1) from None

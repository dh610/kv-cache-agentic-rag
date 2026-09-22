"""Score a saved synthesis fixture run against reviewer-labelled synthesis cases."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from graph.node_graph import contract_errors
from runtime.settings import ROOT
from runtime.synthesis_check import UNKNOWN
from schemas.contracts import JudgeResult, NodeInput, NodeRun

DATASET = ROOT / "tests/fixtures/synthesis/eval"
RECOMMENDATION = re.compile(r"추천|권장|우월|우수하|더 낫|더 유리|선택해야|채택해야")
DISCLAIMER = "공개 정보 기반 추정"


def load_case(case_id: str, dataset: Path = DATASET) -> tuple[dict, NodeInput]:
    labels = json.loads((dataset / "labels.json").read_text(encoding="utf-8"))
    case = next((item for item in labels["cases"] if item["id"] == case_id), None)
    if case is None:
        raise ValueError(f"Unknown synthesis case: {case_id}")
    data = NodeInput.model_validate_json((dataset / case["input"]).read_text(encoding="utf-8"))
    return case, data


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def score_case(case: dict, data: NodeInput, run: NodeRun) -> dict:
    """Check judgments, TRL bounds, citations and preservation; leave meaning to a reader."""
    problems, items, review_flags = [], [], []
    if run.node != "synthesis" or run.mode != "fixture" or run.model.startswith("mock"):
        problems.append("Use a real Generator/Judge synthesis fixture run, not mock output")
    if {e.id: e for e in run.evidence} != {e.id: e for e in data.evidence}:
        problems.append("Run evidence differs from the selected fixed case")
    if run.status == "failed":
        problems.append("Node execution failed")
    allowed = {"No evidence available"} if not data.evidence else set()
    if [e for e in run.validation_errors if e not in allowed]:
        problems.append("Contract validation errors remain in the saved run")
    problems.extend(
        contract_errors(run.node, data, run.result, run.evidence, JudgeResult(checks=run.checks))
    )
    if any(check.label != "supported" for check in run.checks):
        problems.append("Claim verification did not pass")

    for technology, expected in case["expectations"].items():
        for criterion in ("consistency", "implications"):
            found = [
                a
                for a in run.result.assessments
                if a.technology == technology and a.criterion == criterion
            ]
            if len(found) != 1:
                problems.append(f"{technology}/{criterion}: expected exactly one assessment")
                continue
            assessment = found[0]
            accepted = expected[criterion]
            if assessment.judgment == UNKNOWN:
                if not accepted:
                    decision = "pass"
                elif case["allow_unknown"]:
                    decision = "inconclusive"
                else:
                    decision = "fail"
                    problems.append(f"{technology}/{criterion}: unknown judgment is not allowed")
            elif assessment.judgment in accepted:
                decision = "pass"
            else:
                decision = "fail"
                problems.append(
                    f"{technology}/{criterion}: {assessment.judgment} is outside reviewer set "
                    f"{accepted}"
                )
            required = set(expected.get(f"required_{criterion}_evidence_ids", []))
            if assessment.judgment != UNKNOWN and not required.issubset(assessment.evidence_ids):
                problems.append(
                    f"{technology}/{criterion}: must cite both sides {sorted(required)}"
                )
            items.append(
                {
                    "item": f"{technology}/{criterion}",
                    "judgment": assessment.judgment,
                    "expected": accepted,
                    "decision": decision,
                }
            )

        estimates = [t for t in run.result.trl_estimates if t.technology == technology]
        if len(estimates) > 1:
            problems.append(f"TRL {technology}: duplicate estimates")
        estimate = estimates[0] if len(estimates) == 1 else None
        level = estimate.level if estimate else None
        bounds = expected["trl_range"]
        if bounds is None:
            decision = "pass"
            if level is not None:
                decision = "fail"
                problems.append(f"TRL {technology}: assigned {level} without any perspective")
        elif level is None:
            decision = "inconclusive" if case["allow_unknown"] else "fail"
            if decision == "fail":
                problems.append(f"TRL {technology}: level=null is not allowed")
        elif not bounds[0] <= level <= bounds[1]:
            decision = "fail"
            problems.append(f"TRL {technology}: {level} is outside reviewer range {bounds}")
        else:
            decision = "pass"
        if estimate and estimate.provisional:
            problems.append(f"TRL {technology}: synthesis estimate is still provisional")
        if estimate and DISCLAIMER not in estimate.rationale:
            review_flags.append(f"TRL {technology}: rationale에 '{DISCLAIMER}' 명시를 확인")
        items.append(
            {"item": f"TRL {technology}", "level": level, "expected": bounds, "decision": decision}
        )

    preserved = [_norm(u) for u in run.result.unverified]
    for role, prior in data.prior_results.items():
        for text in prior.unverified:
            if not any(_norm(text) in u for u in preserved):
                problems.append(f"{role}: upstream unverified item dropped: {text}")

    texts = [run.result.summary, *(a.rationale for a in run.result.assessments)]
    if any(RECOMMENDATION.search(t) for t in texts):
        review_flags.append("추천·우열 표현 가능성: summary와 rationale을 사람이 확인")
    if DISCLAIMER not in run.result.summary:
        review_flags.append(f"summary에 '{DISCLAIMER}' 명시를 확인")

    decision = (
        "fail"
        if problems
        else "inconclusive"
        if any(i["decision"] == "inconclusive" for i in items)
        else "pass"
    )
    return {
        "case_id": case["id"],
        "decision": decision,
        "items": items,
        "problems": problems,
        "review_flags": review_flags,
        "manual_review": case["manual_review"],
        "note": "Fixture inputs are curated paraphrases and constructed upstream results. A passing score checks judgment bounds, citations and preservation, not factual truth.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score one saved synthesis fixture run")
    parser.add_argument("--case", required=True, choices=("paper_only", "no_prior"))
    parser.add_argument(
        "--run", required=True, type=Path, help="Results directory from app.run_node"
    )
    args = parser.parse_args(argv)
    case, expected_input = load_case(args.case)
    actual_input = NodeInput.model_validate_json(
        (args.run / "input.json").read_text(encoding="utf-8")
    )
    if actual_input != expected_input:
        raise ValueError("Saved run input does not match the selected synthesis case")
    run = NodeRun.model_validate_json((args.run / "result.json").read_text(encoding="utf-8"))
    result = score_case(case, expected_input, run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["decision"] == "pass" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, FileNotFoundError) as exc:
        print(f"Setup error: {exc}")
        raise SystemExit(1) from None

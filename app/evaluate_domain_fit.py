"""Score a saved domain fixture run against evidence-bounded domain-fit review cases."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from runtime.domain_checks import domain_rule_errors
from runtime.settings import ROOT
from schemas.contracts import NodeInput, NodeResult, NodeRun

DATASET = ROOT / "tests/fixtures/domain/fit_eval"
CASE_IDS = ("kivi_paper", "itme_paper", "context_only", "no_evidence")


def load_case(case_id: str, dataset: Path = DATASET) -> tuple[dict, NodeInput]:
    labels = json.loads((dataset / "labels.json").read_text(encoding="utf-8"))
    case = next((item for item in labels["cases"] if item["id"] == case_id), None)
    if case is None:
        raise ValueError(f"Unknown domain-fit case: {case_id}")
    data = NodeInput.model_validate_json((dataset / case["input"]).read_text(encoding="utf-8"))
    return case, data


def score_case(case: dict, data: NodeInput, run: NodeRun, draft: NodeResult | None = None) -> dict:
    """Check judgments against reviewer ranges and citations; semantics stay with a reader.

    With ``draft`` the Generator's pre-verification output is scored instead of the
    delivered result, so judgment logic can be reviewed even when the runtime withheld
    every grade because one claim failed. Draft scores never replace the delivered run.
    """
    problems = []
    scored = run
    if draft is not None:
        scored = run.model_copy(update={"result": draft})
        if draft.node != "domain":
            problems.append("Draft belongs to a different node")
    if run.node != "domain" or run.mode != "fixture" or run.model.startswith("mock"):
        problems.append("Use a real Generator/Judge domain fixture run, not mock output")
    if {e.id: e for e in run.evidence} != {e.id: e for e in data.evidence}:
        problems.append("Run evidence differs from the selected fixed case")
    errors = [
        error
        for error in run.validation_errors
        if not (not case["evidence_expected"] and error == "No evidence available")
    ]
    if run.status == "failed" or errors:
        problems.append("Node execution or contract validation failed")
    problems.extend(domain_rule_errors(data, scored))

    technology = case["technology"]
    supplied = {e.id: e for e in data.evidence}
    supported = {check.claim_id for check in scored.checks if check.label == "supported"}
    items = {}
    for criterion, label in case["items"].items():
        found = [
            a
            for a in scored.result.assessments
            if a.technology == technology and a.criterion == criterion
        ]
        if len(found) != 1:
            items[criterion] = {"judgment": None, "decision": "fail"}
            problems.append(f"{criterion}: expected exactly one assessment")
            continue
        item = found[0]
        if item.judgment in label["accepted"]:
            decision = "pass"
        elif item.judgment in label["disputed"]:
            decision = "inconclusive"
        else:
            decision = "fail"
            problems.append(
                f"{criterion}: {item.judgment} is outside reviewer range "
                f"{label['accepted']} (disputed {label['disputed']})"
            )
        if item.judgment == "확인 불가":
            if not scored.result.unverified:
                problems.append(f"{criterion}: unknown judgment must leave an unverified item")
        else:
            cited = set(item.evidence_ids)
            if not cited or not cited.issubset(supplied):
                problems.append(f"{criterion}: assessment must cite supplied evidence IDs")
            if not set(label["required_evidence_ids"]).issubset(cited):
                problems.append(f"{criterion}: assessment omits required evidence")
            verified = {
                eid
                for claim in scored.result.claims
                if claim.id in supported
                and claim.technology == technology
                and claim.criterion == criterion
                for eid in claim.evidence_ids
            }
            if not cited.issubset(verified):
                problems.append(f"{criterion}: citations lack a supported claim for this item")
        items[criterion] = {"judgment": item.judgment, "decision": decision}

    decisions = {v["decision"] for v in items.values()}
    if problems or "fail" in decisions:
        decision = "fail"
    elif "inconclusive" in decisions:
        decision = "inconclusive"
    else:
        decision = "pass"
    return {
        "case_id": case["id"],
        "technology": technology,
        "scored": "draft" if draft is not None else "result",
        "items": items,
        "decision": decision,
        "problems": sorted(set(problems)),
        "manual_review": case["manual_review"],
        "note": "Fixture notes are reviewer paraphrases. A passing score is not a factual domain-fit certification.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score one saved domain-fit fixture run")
    parser.add_argument("--case", required=True, choices=CASE_IDS)
    parser.add_argument(
        "--run", required=True, type=Path, help="Results directory from app.run_node"
    )
    parser.add_argument(
        "--draft",
        action="store_true",
        help="Score draft.json (Generator output before verification) for prompt review",
    )
    args = parser.parse_args(argv)
    case, expected_input = load_case(args.case)
    actual_input = NodeInput.model_validate_json(
        (args.run / "input.json").read_text(encoding="utf-8")
    )
    if actual_input != expected_input:
        raise ValueError("Saved run input does not match the selected domain-fit case")
    run = NodeRun.model_validate_json((args.run / "result.json").read_text(encoding="utf-8"))
    draft = (
        NodeResult.model_validate_json((args.run / "draft.json").read_text(encoding="utf-8"))
        if args.draft
        else None
    )
    result = score_case(case, expected_input, run, draft)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["decision"] == "pass" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, FileNotFoundError) as exc:
        print(f"Setup error: {exc}")
        raise SystemExit(1) from None

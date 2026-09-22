"""Score a saved tech fixture run against evidence-bounded TRL review cases."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from runtime.settings import ROOT
from runtime.validation import tech_trl_errors, verified_evidence_ids
from schemas.contracts import NodeInput, NodeRun

DATASET = ROOT / "tests/fixtures/tech/trl_eval"


def load_case(case_id: str, dataset: Path = DATASET) -> tuple[dict, NodeInput]:
    labels = json.loads((dataset / "labels.json").read_text(encoding="utf-8"))
    case = next((item for item in labels["cases"] if item["id"] == case_id), None)
    if case is None:
        raise ValueError(f"Unknown TRL case: {case_id}")
    data = NodeInput.model_validate_json((dataset / case["input"]).read_text(encoding="utf-8"))
    return case, data


def score_case(case: dict, data: NodeInput, run: NodeRun) -> dict:
    """Check a TRL decision and citations; leave semantic review points to a reader."""
    problems = tech_trl_errors(run.result)
    if run.node != "tech" or run.mode != "fixture" or run.model.startswith("mock"):
        problems.append("Use a real Generator/Judge tech fixture run, not mock output")
    if {e.id: e for e in run.evidence} != {e.id: e for e in data.evidence}:
        problems.append("Run evidence differs from the selected fixed case")
    expected_empty = case["maximum_supported_stage"] is None
    errors = [
        error
        for error in run.validation_errors
        if not (expected_empty and error == "No evidence available")
    ]
    if run.status == "failed" or errors:
        problems.append("Node execution or contract validation failed")

    technology = case["technology"]
    maturity = [
        item
        for item in run.result.assessments
        if item.technology == technology and item.criterion == "maturity"
    ]
    if len(maturity) != 1:
        problems.append("Expected exactly one maturity assessment")
    judgment = maturity[0].judgment if len(maturity) == 1 else None
    stage_match = re.fullmatch(r"TRL ([1-9])", judgment or "")
    stage = int(stage_match.group(1)) if stage_match else None

    if judgment == "확인 불가":
        if not case["allow_unknown"]:
            problems.append("Unknown judgment is not allowed for this case")
        if not run.result.unverified:
            problems.append("Unknown judgment must leave an unverified item")
        decision = "pass" if case["maximum_supported_stage"] is None else "inconclusive"
    elif stage is None:
        problems.append("Maturity judgment must be TRL 1..9 or 확인 불가")
        decision = "fail"
    elif case["maximum_supported_stage"] is None:
        problems.append("Assigned a TRL stage without evidence")
        decision = "fail"
    elif not case["minimum_supported_stage"] <= stage <= case["maximum_supported_stage"]:
        problems.append(
            f"TRL {stage} is outside reviewer range "
            f"{case['minimum_supported_stage']}..{case['maximum_supported_stage']}"
        )
        decision = "fail"
    else:
        decision = "pass"

    if stage is not None and maturity:
        supplied = {e.id: e for e in data.evidence}
        cited = set(maturity[0].evidence_ids)
        if not cited or not cited.issubset(supplied):
            problems.append("TRL assessment must cite supplied evidence IDs")
        elif any(supplied[eid].technology != technology for eid in cited):
            problems.append("TRL assessment cites a different technology")
        if not set(case["required_maturity_evidence_ids"]).issubset(cited):
            problems.append("TRL assessment omits required experiment or prototype evidence")
        verified_ids = verified_evidence_ids(
            run.result, run.checks, data.evidence, technology, "maturity"
        )
        if not cited.issubset(verified_ids):
            problems.append("TRL citations lack a supported maturity claim")

    if problems:
        decision = "fail"
    return {
        "case_id": case["id"],
        "technology": technology,
        "judgment": judgment,
        "reviewer_range": [case["minimum_supported_stage"], case["maximum_supported_stage"]],
        "decision": decision,
        "problems": problems,
        "manual_review": case["manual_review"],
        "note": "Fixture notes are curated paraphrases. A passing score is not a factual TRL certification.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score one saved tech TRL fixture run")
    parser.add_argument(
        "--case", required=True, choices=("kivi_paper", "itme_paper", "no_evidence")
    )
    parser.add_argument(
        "--run", required=True, type=Path, help="Results directory from app.run_node"
    )
    args = parser.parse_args(argv)
    case, expected_input = load_case(args.case)
    actual_input = NodeInput.model_validate_json(
        (args.run / "input.json").read_text(encoding="utf-8")
    )
    if actual_input != expected_input:
        raise ValueError("Saved run input does not match the selected TRL case")
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

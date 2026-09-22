"""Score a saved stakeholder fixture run against reviewer-labeled reaction cases."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from runtime.settings import ROOT
from schemas.contracts import Claim, Evidence, NodeInput, NodeRun

DATASET = ROOT / "tests/fixtures/stakeholder/eval"
QUOTE = re.compile(r"「(.+?)」", re.S)
ELLIPSIS = re.compile(r"\s*(?:\.\.\.|…|\[\.\.\.\]|\(\.\.\.\))\s*")
MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _squash(text: str) -> str:
    """Compare quotes without markdown emphasis/links or whitespace differences."""
    text = MD_LINK.sub(r"\1", text)
    return " ".join(text.replace("*", "").replace("#", "").split())


def _fragments(quote: str) -> list[str]:
    """A quote may join non-adjacent passages with an ellipsis; each part must be verbatim."""
    return [_squash(part) for part in ELLIPSIS.split(quote) if part.strip()]


def grounding_errors(claim: Claim, evidence_by_id: dict[str, Evidence]) -> list[str]:
    """Deterministic checks the LLM Judge has been observed to miss (stakeholder harness only)."""
    errors = []
    for eid in claim.evidence_ids:
        item = evidence_by_id.get(eid)
        # "other" marks shared context (e.g. reference papers) usable by any technology.
        if item and item.technology not in (claim.technology, "other"):
            errors.append(f"{claim.id}: cites {item.technology} evidence for {claim.technology}")
    cited = [_squash(evidence_by_id[e].text) for e in claim.evidence_ids if e in evidence_by_id]
    for quote in QUOTE.findall(claim.text):
        for needle in _fragments(quote):
            if not any(needle in text for text in cited):
                errors.append(f"{claim.id}: quoted text is not verbatim in cited evidence")
                break
    return errors


def load_labels(dataset: Path = DATASET) -> dict:
    return json.loads((dataset / "labels.json").read_text(encoding="utf-8"))


def load_case(case_id: str, dataset: Path = DATASET) -> tuple[dict, NodeInput]:
    case = next((item for item in load_labels(dataset)["cases"] if item["id"] == case_id), None)
    if case is None:
        raise ValueError(f"Unknown stakeholder case: {case_id}")
    data = NodeInput.model_validate_json((dataset / case["input"]).read_text(encoding="utf-8"))
    return case, data


def score_case(case: dict, data: NodeInput, run: NodeRun) -> dict:
    """Check the reaction judgment and its citations; semantic review stays with a reader."""
    problems = []
    if run.node != "stakeholder" or run.mode != "fixture" or run.model.startswith("mock"):
        problems.append("Use a real Generator/Judge stakeholder fixture run, not mock output")
    if {e.id: e for e in run.evidence} != {e.id: e for e in data.evidence}:
        problems.append("Run evidence differs from the selected fixed case")
    expected_empty = not data.evidence
    errors = [
        error
        for error in run.validation_errors
        if not (expected_empty and error == "No evidence available")
    ]
    if run.status == "failed" or errors:
        problems.append("Node execution or contract validation failed")
    technology, criterion = case["technology"], case["criterion"]
    items = [
        item
        for item in run.result.assessments
        if item.technology == technology and item.criterion == criterion
    ]
    if len(items) != 1:
        problems.append(f"Expected exactly one {technology}/{criterion} assessment")
    judgment = items[0].judgment if len(items) == 1 else None
    if judgment == "확인 불가":
        if not run.result.unverified:
            problems.append("Unknown judgment must leave an unverified item")
        if case["expected_unknown"]:
            decision = "pass"
        elif case["allow_unknown"]:
            decision = "inconclusive"
        else:
            problems.append("Unknown judgment is not allowed for this case")
            decision = "fail"
    elif judgment is None:
        decision = "fail"
    elif case["expected_unknown"]:
        problems.append(f"Graded '{judgment}' although the evidence is not a stakeholder reaction")
        decision = "fail"
    elif judgment not in case["allowed_judgments"]:
        problems.append(f"'{judgment}' is outside reviewer range {case['allowed_judgments']}")
        decision = "fail"
    else:
        decision = "pass"
    if judgment not in (None, "확인 불가") and items:
        supplied = {e.id: e for e in data.evidence}
        cited = set(items[0].evidence_ids)
        if not cited or not cited.issubset(supplied):
            problems.append("Assessment must cite supplied evidence IDs")
        elif any(supplied[eid].technology not in (technology, "other") for eid in cited):
            problems.append("Assessment cites a different technology")
        if not set(case["required_evidence_ids"]).issubset(cited):
            problems.append("Assessment omits the reviewer-required evidence")
        supported = {check.claim_id for check in run.checks if check.label == "supported"}
        premises = [
            claim
            for claim in run.result.claims
            if claim.id in supported
            and claim.technology == technology
            and claim.criterion == criterion
        ]
        if not cited.issubset({eid for claim in premises for eid in claim.evidence_ids}):
            problems.append("Citations lack a supported claim for this item")
        for claim in premises:
            problems.extend(grounding_errors(claim, supplied))
        needle = case.get("required_claim_condition")
        if needle and not any(needle in cond for claim in premises for cond in claim.conditions):
            problems.append(f"No supported claim marks its evidence as '{needle}'")
    if problems:
        decision = "fail"
    return {
        "case_id": case["id"],
        "item": f"{technology}/{criterion}",
        "judgment": judgment,
        "allowed_judgments": case["allowed_judgments"],
        "expected_unknown": case["expected_unknown"],
        "decision": decision,
        "problems": problems,
        "manual_review": case["manual_review"],
        "note": (
            "Fixture notes are reviewer summaries of public pages, not verbatim excerpts. "
            "A passing score is not a factual stakeholder finding."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score one saved stakeholder fixture run")
    parser.add_argument(
        "--case", required=True, choices=[case["id"] for case in load_labels()["cases"]]
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
        raise ValueError("Saved run input does not match the selected stakeholder case")
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

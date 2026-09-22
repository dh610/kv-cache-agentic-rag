"""Offline checks of saved market fixture runs; never calls an LLM or retriever."""

import argparse
import json
from pathlib import Path

from graph.node_graph import contract_errors
from runtime.settings import ROOT
from schemas.contracts import JudgeResult, NodeInput, NodeRun

DATASET = ROOT / "tests/fixtures/market"


def load_case(case_id: str) -> tuple[dict, NodeInput]:
    catalog = json.loads((DATASET / "expectations.json").read_text(encoding="utf-8"))
    for case in catalog["cases"]:
        if Path(case["input"]).stem == case_id:
            return case, NodeInput.model_validate_json(
                (DATASET / case["input"]).read_text(encoding="utf-8")
            )
    raise ValueError(f"Unknown market evaluation case: {case_id}")


def score_case(case: dict, data: NodeInput, run: NodeRun) -> dict:
    """Compare existing labels and contracts, keeping semantic review explicit."""
    problems = []
    if run.node != "market" or run.result.node != "market":
        problems.append("result must belong to market")
    if run.mode != "fixture" or run.model.lower().startswith("mock"):
        problems.append("only non-mock fixture runs can be evaluated")
    if run.status == "failed":
        problems.append("node execution failed")
    if not run.model.strip() or not run.prompt_hash.strip():
        problems.append("missing execution metadata")
    if len({e.id for e in run.evidence}) != len(run.evidence) or {
        e.id: e.model_dump() for e in run.evidence
    } != {e.id: e.model_dump() for e in data.evidence}:
        problems.append("run evidence differs from the fixed input")
    problems.extend(
        error
        for error in run.validation_errors
        if not (not data.evidence and error == "No evidence available")
    )
    problems.extend(
        contract_errors("market", data, run.result, run.evidence, JudgeResult(checks=run.checks))
    )
    if any(check.label != "supported" for check in run.checks):
        problems.append("claim verification did not pass")
    questions = {q.id for q in data.questions}
    if {s.question_id for s in run.searches} != questions:
        problems.append("search history must cover every input question")
    if len(run.searches) != len(questions) or any(
        s.attempt != 1 or s.intent != "fixture" or s.error for s in run.searches
    ):
        problems.append("fixture searches must succeed once per question")
    known = {e.id: e for e in data.evidence}
    if any(not set(s.evidence_ids).issubset(known) for s in run.searches):
        problems.append("search history cites unknown evidence")
    if len(run.coverage) != len(questions) or {c.question_id for c in run.coverage} != questions:
        problems.append("v2 sufficiency coverage must include every question exactly once")
    if any(
        not set(c.evidence_ids).issubset(known) or (c.sufficient and not c.evidence_ids)
        for c in run.coverage
    ):
        problems.append("invalid sufficiency evidence")
    if run.fix_count not in (0, 1):
        problems.append("v2 answer fix count must be 0 or 1")
    if run.verdict == "표현 오류" or (run.status == "completed" and run.verdict != "통과"):
        problems.append("unresolved or inconsistent node verdict")
    if run.result.trl_estimates:
        problems.append("market must leave TRL estimates to tech/synthesis")
    contextual_citations = set()
    for item in [*run.result.claims, *run.result.assessments]:
        for eid in item.evidence_ids:
            evidence = known.get(eid)
            if evidence is None or evidence.technology == item.technology:
                continue
            # B.3 permits reference documents; C.2 permits field background.
            # Metadata alone cannot establish whether their use is appropriate.
            if evidence.document_role == "reference" or evidence.scope == "context":
                contextual_citations.add((item.technology, item.criterion, eid))
            else:
                problems.append(f"{item.technology}/{item.criterion}: cross-technology citation")
    unknown = [a for a in run.result.assessments if a.judgment == "확인 불가"]
    if unknown and not run.result.unverified:
        problems.append("unknown judgments must remain in unverified")
    if (unknown or run.result.unverified) and run.verdict == "통과":
        problems.append("unknown judgments and unverified findings require 추가 근거 필요 verdict")
    if (unknown or run.result.unverified or run.validation_errors) and run.status == "completed":
        problems.append("unresolved findings must retain needs_revision status")
    for item in run.result.assessments:
        if not item.rationale.strip():
            problems.append(f"{item.technology}/{item.criterion}: empty rationale")

    actual = {(a.technology, a.criterion): a for a in run.result.assessments}
    expected = {(a["technology"], a["criterion"]) for a in case["expected_assessments"]}
    if set(actual) != expected:
        problems.append("result must contain exactly the expected technology/criterion pairs")
    matched = 0
    compared = 0
    manual_items = []
    for label in case["expected_assessments"]:
        key = (label["technology"], label["criterion"])
        if label["judgment"] is None:
            manual_items.append(label)
            continue
        compared += 1
        item = actual.get(key)
        if item is not None and item.judgment == label["judgment"]:
            matched += 1
        else:
            problems.append(f"{key[0]}/{key[1]}: expected {label['judgment']}")
    return {
        "case": Path(case["input"]).stem,
        "automatic_decision": "fail" if problems else "inconclusive" if manual_items else "pass",
        "problems": sorted(set(problems)),
        "matched_items": matched,
        "compared_items": compared,
        "manual_items": manual_items,
        "contextual_citations_for_review": [
            {"technology": tech, "criterion": criterion, "evidence_id": eid}
            for tech, criterion, eid in sorted(contextual_citations)
        ],
        "review_checks": case["review_checks"],
        "human_review_required": True,
        "note": "합성 사례의 등급·계약 검사이며 실제 기술 평가나 검색 성능 측정이 아닙니다. "
        "인용 내용의 의미, 상충·미확인 보존, 이해관계 표시 등 review_checks는 사람이 검토해야 합니다. "
        "다른 기술로 분류된 배경·보조 자료는 관련성을 확인하고, 그 기술의 채택·지원을 "
        "선정 기술 자체의 사실로 옮기지 않았는지 검토해야 합니다.",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, help="Case name from market/expectations.json")
    parser.add_argument(
        "--run", type=Path, required=True, help="Saved input.json/result.json directory"
    )
    args = parser.parse_args(argv)
    case, data = load_case(args.case)
    saved_input = NodeInput.model_validate_json(
        (args.run / "input.json").read_text(encoding="utf-8")
    )
    if saved_input != data:
        raise ValueError("Saved input differs from the selected fixture (including prior_results)")
    run = NodeRun.model_validate_json((args.run / "result.json").read_text(encoding="utf-8"))
    report = score_case(case, data, run)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["automatic_decision"] == "pass" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(f"Setup error: {exc}")
        raise SystemExit(1) from None

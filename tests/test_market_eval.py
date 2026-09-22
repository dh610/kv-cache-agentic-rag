"""Test evaluator behavior using fabricated results, not model performance."""

import json

import pytest

from app.evaluate_market import DATASET, load_case, main, score_case
from graph.node_graph import build_node_graph
from rag.interface import FixedEvidence
from runtime.models import MockBackend
from runtime.prompts import load_rubric
from runtime.settings import load_settings
from schemas.contracts import (
    Assessment,
    Claim,
    ClaimCheck,
    Coverage,
    JudgeResult,
    NodeResult,
    NodeRun,
    SearchRecord,
)

CASES = [
    item["input"].removesuffix(".json")
    for item in json.loads((DATASET / "expectations.json").read_text())["cases"]
]


def sample_run(case_id="supported"):
    """Hand-built structural examples; citations are not semantic ground truth."""
    case, data = load_case(case_id)
    assessments, claims, checks, unverified = [], [], [], []
    for label in case["expected_assessments"]:
        technology, criterion = label["technology"], label["criterion"]
        judgment = label["judgment"] or "확인 불가"
        claim_id = f"{technology}/{criterion}"
        ids = [e.id for e in data.evidence if e.technology == technology]
        if judgment == "확인 불가":
            ids = []
            unverified.append(claim_id + ": 근거 미확인")
        else:
            claims.append(
                Claim(
                    id=claim_id,
                    technology=technology,
                    criterion=criterion,
                    text="검증기 테스트용 가상 주장",
                    kind="fact",
                    evidence_ids=ids,
                    conditions=["검증기 단위 테스트 전용"],
                )
            )
            checks.append(
                ClaimCheck(
                    claim_id=claim_id,
                    label="supported",
                    evidence_ids=ids,
                    reason="테스트용 검사",
                )
            )
        assessments.append(
            Assessment(
                technology=technology,
                criterion=criterion,
                judgment=judgment,
                rationale="검증기 테스트용 판정. 실제 기술 평가가 아님.",
                evidence_ids=ids,
            )
        )
    run = NodeRun(
        node="market",
        mode="fixture",
        status="needs_revision" if unverified else "completed",
        result=NodeResult(
            node="market",
            summary="테스트용 결과",
            claims=claims,
            assessments=assessments,
            unverified=unverified,
            limitations=[],
        ),
        evidence=data.evidence,
        checks=checks,
        validation_errors=[] if data.evidence else ["No evidence available"],
        searches=[
            SearchRecord(
                question_id=q.id,
                attempt=1,
                query=q.text,
                evidence_ids=[e.id for e in data.evidence if e.technology == q.technology],
            )
            for q in data.questions
        ],
        prompt_hash="unit-test",
        model="unit-test-backend",
        verdict="추가 근거 필요" if unverified else "통과",
        fix_count=0,
        coverage=[
            Coverage(
                question_id=q.id,
                sufficient=bool(data.evidence),
                evidence_ids=[e.id for e in data.evidence if e.technology == q.technology],
                reason="검증기 테스트용 충분성 응답; 실제 의미 평가 아님",
            )
            for q in data.questions
        ],
    )
    return case, data, run


@pytest.mark.parametrize("case_id", CASES)
def test_existing_labels_and_complete_results(case_id):
    case, data, run = sample_run(case_id)
    requested = {(q.technology, q.criterion) for q in data.questions}
    expected = {(a["technology"], a["criterion"]) for a in case["expected_assessments"]}
    assert len(requested) == len(data.questions) == len(case["expected_assessments"]) == 6
    assert requested == expected
    rubric = {c.id: c.judgments for c in load_rubric("market").criteria}
    for label in case["expected_assessments"]:
        if label["judgment"] is None:
            assert label["manual_review_reason"]
        else:
            assert label["judgment"] in rubric[label["criterion"]]
    report = score_case(case, data, run)
    assert report["problems"] == []
    assert report["automatic_decision"] == ("inconclusive" if case_id == "planned-only" else "pass")
    assert report["matched_items"] == report["compared_items"]
    assert report["human_review_required"]
    assert report["review_checks"] == case["review_checks"]


@pytest.mark.parametrize("case_id", CASES)
def test_wrong_fixed_grade_is_rejected(case_id):
    case, data, run = sample_run(case_id)
    index = next(i for i, label in enumerate(case["expected_assessments"]) if label["judgment"])
    item = run.result.assessments[index]
    item.judgment = "낮음" if item.judgment == "확인 불가" else "확인 불가"
    assert score_case(case, data, run)["automatic_decision"] == "fail"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "invalid_grade",
        "mock",
        "wrong_node",
        "failed",
        "changed_evidence",
        "duplicate_evidence",
        "no_judge",
        "rejected_claim",
        "unknown_citation",
        "cross_technology",
        "missing_search",
        "search_error",
        "empty_rationale",
        "validation_error",
    ],
)
def test_bad_run_is_rejected(mutation):
    case, data, original = sample_run()
    run = original.model_copy(deep=True)
    if mutation == "missing":
        run.result.assessments.pop()
    elif mutation == "duplicate":
        run.result.assessments.append(run.result.assessments[0])
    elif mutation == "invalid_grade":
        run.result.assessments[0].judgment = "적합"
    elif mutation == "mock":
        run.mode = "mock"
    elif mutation == "wrong_node":
        run.node = "tech"
    elif mutation == "failed":
        run.status = "failed"
    elif mutation == "changed_evidence":
        run.evidence[0].text = "변경된 근거"
    elif mutation == "duplicate_evidence":
        run.evidence.append(run.evidence[0])
    elif mutation == "no_judge":
        run.checks.clear()
    elif mutation == "rejected_claim":
        run.checks[0].label = "unsupported"
    elif mutation == "unknown_citation":
        run.result.assessments[0].evidence_ids = ["nonexistent"]
    elif mutation == "cross_technology":
        ids = [e.id for e in data.evidence if e.technology == "ITME"]
        run.result.assessments[0].evidence_ids = ids
        run.result.claims[0].evidence_ids = ids
        run.checks[0].evidence_ids = ids
    elif mutation == "missing_search":
        run.searches.pop()
    elif mutation == "search_error":
        run.searches[0].error = "Search failed"
    elif mutation == "empty_rationale":
        run.result.assessments[0].rationale = " "
    elif mutation == "validation_error":
        run.validation_errors.append("Judge failed")
    assert score_case(case, data, run)["automatic_decision"] == "fail"


@pytest.mark.parametrize("mutation", ["clear_unverified", "false_completed", "low"])
def test_no_evidence_cannot_be_cleared_or_downgraded(mutation):
    case, data, run = sample_run("missing-evidence")
    if mutation == "clear_unverified":
        run.result.unverified.clear()
    elif mutation == "false_completed":
        run.status = "completed"
    else:
        run.result.assessments[0].judgment = "낮음"
    assert score_case(case, data, run)["automatic_decision"] == "fail"


@pytest.mark.parametrize("case_id,expected_code", [("supported", 0), ("planned-only", 2)])
def test_saved_run_cli(case_id, expected_code, tmp_path, capsys):
    _, data, run = sample_run(case_id)
    (tmp_path / "input.json").write_text(data.model_dump_json(), encoding="utf-8")
    (tmp_path / "result.json").write_text(run.model_dump_json(), encoding="utf-8")
    assert main(["--case", case_id, "--run", str(tmp_path)]) == expected_code
    report = json.loads(capsys.readouterr().out)
    assert report["compared_items"] == (4 if case_id == "planned-only" else 6)
    if case_id == "planned-only":
        assert len(report["manual_items"]) == 2
    data.description += " changed"
    (tmp_path / "input.json").write_text(data.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="Saved input differs"):
        main(["--case", case_id, "--run", str(tmp_path)])


def test_unknown_case_is_rejected():
    with pytest.raises(ValueError, match="Unknown market evaluation case"):
        load_case("../basic")


@pytest.mark.parametrize("case_id", ["supported", "missing-evidence", "context-only"])
def test_v2_graph_output_is_compatible_with_market_scorer(case_id):
    case, data, sample = sample_run(case_id)

    class FixedResultBackend(MockBackend):
        name = "unit-test-backend"

        def generate(self, node, data, evidence, system, user):
            return sample.result.model_copy(deep=True)

        def judge(self, result, evidence):
            return JudgeResult(checks=sample.checks)

    graph = build_node_graph(
        "market", data, "fixture", load_settings(), FixedResultBackend(), FixedEvidence(data)
    )
    run = graph.invoke({})["output"]
    assert len(run.coverage) == 6
    assert {s.intent for s in run.searches} == {"fixture"}
    assert run.verdict == ("통과" if case_id == "supported" else "추가 근거 필요")
    report = score_case(case, data, run)
    assert report["problems"] == []
    assert report["automatic_decision"] == "pass"
    assert report["human_review_required"]


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_coverage",
        "duplicate_coverage",
        "unknown_coverage_evidence",
        "empty_sufficient_evidence",
        "too_many_fixes",
        "wrong_intent",
        "wrong_verdict",
    ],
)
def test_v2_metadata_is_required_and_consistent(mutation):
    case, data, run = sample_run()
    if mutation == "missing_coverage":
        run.coverage.clear()
    elif mutation == "duplicate_coverage":
        run.coverage[-1] = run.coverage[0]
    elif mutation == "unknown_coverage_evidence":
        run.coverage[0].evidence_ids = ["unknown-source"]
    elif mutation == "empty_sufficient_evidence":
        run.coverage[0].evidence_ids = []
    elif mutation == "too_many_fixes":
        run.fix_count = 2
    elif mutation == "wrong_intent":
        run.searches[0].intent = "critical"
    else:
        run.verdict = "표현 오류"
    assert score_case(case, data, run)["automatic_decision"] == "fail"


@pytest.mark.parametrize(
    "technology,role,scope",
    [
        ("other", "reference", "context"),
        ("ITME", "reference", "context"),
        ("other", "target", "context"),
        ("other", "reference", "target"),
    ],
)
def test_background_and_reference_citations_keep_semantic_review(technology, role, scope):
    case, data, run = sample_run("context-only")
    evidence = next(e for e in data.evidence if e.technology == "KIVI")
    evidence.technology = technology
    evidence.document_role = role
    evidence.scope = scope
    # Data and run deliberately describe the same fixed input for this unit case.
    run.evidence = data.evidence
    report = score_case(case, data, run)
    assert report["automatic_decision"] == "pass"
    assert report["problems"] == []
    assert report["contextual_citations_for_review"] == [
        {"technology": "KIVI", "criterion": "growth", "evidence_id": evidence.id}
    ]
    assert report["human_review_required"] is True


@pytest.mark.parametrize("misuse", ["adoption_transfer", "unverified_claim"])
def test_reference_exception_does_not_bypass_grade_or_judge_checks(misuse):
    case, data, run = sample_run("context-only")
    for evidence in data.evidence:
        evidence.technology = "other"
    run.evidence = data.evidence
    if misuse == "adoption_transfer":
        # Field adoption is not evidence of KIVI's own adoption (C.2).
        adoption = next(
            a
            for a in run.result.assessments
            if a.technology == "KIVI" and a.criterion == "adoption"
        )
        adoption.judgment = "높음"
        adoption.evidence_ids = [data.evidence[0].id]
        run.result.claims.append(
            Claim(
                id="transferred-adoption",
                technology="KIVI",
                criterion="adoption",
                text="분야 채택을 KIVI의 채택으로 잘못 옮긴 테스트 주장",
                kind="fact",
                evidence_ids=adoption.evidence_ids,
                conditions=[],
            )
        )
        run.checks.append(
            ClaimCheck(
                claim_id="transferred-adoption",
                label="supported",
                evidence_ids=adoption.evidence_ids,
                reason="잘못된 Judge 응답을 모사",
            )
        )
        expected_problem = "KIVI/adoption: expected 확인 불가"
    else:
        run.checks[0].label = "unsupported"
        expected_problem = "claim verification did not pass"
    report = score_case(case, data, run)
    assert report["automatic_decision"] == "fail"
    assert expected_problem in report["problems"]

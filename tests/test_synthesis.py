from collections import Counter

import pytest

from app.evaluate_synthesis import load_case, main, score_case
from graph.main_graph import build_main_graph
from graph.node_graph import build_node_graph
from rag.interface import FixedEvidence
from runtime.models import MockBackend
from runtime.prompts import load_rubric, render
from runtime.runner import load_input
from runtime.settings import load_settings
from runtime.synthesis_check import synthesis_errors
from schemas.contracts import (
    NODES,
    Assessment,
    Claim,
    ClaimCheck,
    NodeResult,
    NodeRun,
    SearchRecord,
    TRLEstimate,
)

KIVI_SIDES = ["kivi-review-p6-quality", "kivi-review-p1-abstract"]
ITME_SIDES = ["itme-review-p8-fpga", "itme-review-p9-workload"]


def mock_run(case):
    data = load_input("synthesis", case)
    graph = build_node_graph(
        "synthesis", data, "mock", load_settings(), MockBackend(), FixedEvidence(data)
    )
    return data, graph.invoke({})["output"]


def synthesis_result(
    data,
    *,
    kivi="상충",
    itme="일치",
    implications="조건부 시사점",
    kivi_trl=4,
    itme_trl=5,
    preserve=True,
):
    claims, checks = [], []

    def claim(cid, technology, criterion, ids, kind="fact"):
        claims.append(
            Claim(
                id=cid,
                technology=technology,
                criterion=criterion,
                text=f"{technology} 고정 근거에 기술된 조건 ({criterion})",
                kind=kind,
                evidence_ids=ids,
                conditions=["고정 근거 범위"],
            )
        )
        checks.append(
            ClaimCheck(claim_id=cid, label="supported", evidence_ids=ids, reason="unit test")
        )

    claim("syn-kivi-quality", "KIVI", "consistency", KIVI_SIDES)
    claim("syn-kivi-conditions", "KIVI", "implications", ["kivi-review-p8-efficiency"])
    claim("syn-itme-hardware", "ITME", "consistency", ITME_SIDES)
    claim("syn-itme-conditions", "ITME", "implications", ["itme-review-p8-cmm"])

    def assessment(technology, criterion, judgment, ids):
        return Assessment(
            technology=technology,
            criterion=criterion,
            judgment=judgment,
            rationale="관점별 판정 대조. 공개 정보 기반 추정.",
            evidence_ids=[] if judgment == "확인 불가" else ids,
        )

    unverified = [
        f"{role}: {item}" for role, prior in data.prior_results.items() for item in prior.unverified
    ]
    if not preserve:
        unverified = unverified[1:]
    return NodeResult(
        node="synthesis",
        summary="관점이 갈린 지점을 기술별로 정리한 결과. 공개 정보 기반 추정.",
        claims=claims,
        assessments=[
            assessment("KIVI", "consistency", kivi, KIVI_SIDES),
            assessment("KIVI", "implications", implications, ["kivi-review-p8-efficiency"]),
            assessment("ITME", "consistency", itme, ITME_SIDES),
            assessment("ITME", "implications", implications, ["itme-review-p8-cmm"]),
        ],
        unverified=unverified,
        limitations=["원 저자(first_party) 자료만 사용", "시장·이해관계자 자료 없음"],
        trl_estimates=[
            TRLEstimate(
                technology="KIVI",
                level=kivi_trl,
                rationale="잠정 TRL 대조. 공개 정보 기반 추정.",
                evidence_ids=["kivi-review-p8-efficiency"],
                provisional=False,
            ),
            TRLEstimate(
                technology="ITME",
                level=itme_trl,
                rationale="잠정 TRL 대조. 공개 정보 기반 추정.",
                evidence_ids=["itme-review-p8-cmm"],
                provisional=False,
            ),
        ],
    ), checks


def synthesis_run(data, result, checks, mode="fixture"):
    return NodeRun(
        node="synthesis",
        mode=mode,
        status="needs_revision" if result.unverified else "completed",
        result=result,
        evidence=data.evidence,
        checks=checks,
        validation_errors=[],
        searches=[
            SearchRecord(question_id=q.id, attempt=1, query=q.text, evidence_ids=[])
            for q in data.questions
        ],
        prompt_hash="unit-test-hash",
        model="unit-test-backend",
    )


def test_rubric_ids_and_fixture_integrity():
    assert [c.id for c in load_rubric("synthesis").criteria] == ["consistency", "implications"]
    for case in ("basic", "acceptance"):
        data = load_input("synthesis", case)
        known = {e.id for e in data.evidence}
        assert all("검토자 요약" in e.text for e in data.evidence)
        for role, prior in data.prior_results.items():
            assert prior.node == role
            for item in [*prior.claims, *prior.assessments, *prior.trl_estimates]:
                assert set(item.evidence_ids) <= known, (case, role, item)
    acceptance = load_input("synthesis", "acceptance")
    assert set(acceptance.prior_results) == {"tech", "market", "stakeholder", "domain"}
    assert len(acceptance.questions) == 4


def test_prompt_states_procedure_and_unapplied_design_rule():
    data = load_input("synthesis", "acceptance")
    system, user, _ = render("synthesis", data, data.evidence)
    for token in ("prior_results", "상충", "provisional=false", "공개 정보 기반 추정", "TRL 4~6"):
        assert token in system, token
    assert "애매하면 낮은 등급" in system and "적용하지 않는다" in system
    assert "<role>: <원문 그대로>" in user
    assert '"prior_results"' in user and "tech-kivi-limitations" in user


def test_mock_wiring_preserves_upstream_unverified_without_contract_errors():
    _, basic = mock_run("basic")
    assert basic.status == "completed" and not basic.validation_errors
    data, run = mock_run("acceptance")
    assert run.status == "needs_revision" and not run.validation_errors
    for role, prior in data.prior_results.items():
        for text in prior.unverified:
            assert f"{role}: {text}" in run.result.unverified
    assert len(run.result.assessments) == 4
    # Mock answers each provisional TRL without inventing a level.
    assert {t.technology: t.level for t in run.result.trl_estimates} == {"KIVI": None, "ITME": None}
    assert all(not t.provisional for t in run.result.trl_estimates)


def test_bounded_synthesis_passes_reviewer_case():
    case, data = load_case("paper_only")
    result, checks = synthesis_result(data)
    assert synthesis_errors(data, result, data.evidence) == []
    report = score_case(case, data, synthesis_run(data, result, checks))
    assert report["decision"] == "pass", report["problems"]


def test_trl_cannot_exceed_provisional_without_market_target_evidence():
    case, data = load_case("paper_only")
    result, checks = synthesis_result(data, kivi_trl=6)
    errors = synthesis_errors(data, result, data.evidence)
    assert any("exceeds provisional" in e for e in errors)
    assert score_case(case, data, synthesis_run(data, result, checks))["decision"] == "fail"


def test_trl_may_rise_when_market_cites_direct_target_evidence():
    _, data = load_case("paper_only")
    market = data.prior_results["market"]
    market.claims.append(
        Claim(
            id="market-kivi-adoption",
            technology="KIVI",
            criterion="adoption",
            text="고정 근거",
            kind="fact",
            evidence_ids=["kivi-review-p8-efficiency"],
            conditions=[],
        )
    )
    result, _ = synthesis_result(data, kivi_trl=6)
    errors = synthesis_errors(data, result, data.evidence)
    assert not any("exceeds provisional" in e for e in errors)


def test_trl_must_be_final_and_direct():
    _, data = load_case("paper_only")
    result, _ = synthesis_result(data)
    result.trl_estimates[0].provisional = True
    result.trl_estimates[1].evidence_ids = ["kivi-review-p8-efficiency"]
    errors = synthesis_errors(data, result, data.evidence)
    assert any("provisional=false" in e for e in errors)
    assert any("not direct target evidence" in e for e in errors)
    result.trl_estimates = []
    assert any("needs a final level" in e for e in synthesis_errors(data, result, data.evidence))


def test_consistency_needs_two_evaluated_perspectives_and_both_citations():
    _, data = load_case("paper_only")
    only_tech = data.model_copy(update={"prior_results": {"tech": data.prior_results["tech"]}})
    result, _ = synthesis_result(only_tech)
    errors = synthesis_errors(only_tech, result, only_tech.evidence)
    assert any("needs two evaluated perspectives" in e for e in errors)
    result, _ = synthesis_result(data)
    result.assessments[0].evidence_ids = ["kivi-review-p6-quality"]  # tech only cites p.6
    errors = synthesis_errors(data, result, data.evidence)
    assert not any("needs two evaluated perspectives" in e for e in errors)
    assert any("at least two perspectives" in e for e in errors)
    result.assessments[0].evidence_ids = ["kivi-review-p2-method"]  # tech and domain cite p.2
    assert not any("perspectives" in e for e in synthesis_errors(data, result, data.evidence))


def test_cross_technology_evidence_is_rejected():
    _, data = load_case("paper_only")
    result, _ = synthesis_result(data)
    result.claims[0].evidence_ids = ["itme-review-p8-fpga"]
    assert any("cannot support KIVI" in e for e in synthesis_errors(data, result, data.evidence))


def test_consistency_does_not_count_citations_from_unknown_assessments():
    _, data = load_case("paper_only")
    result, _ = synthesis_result(data)
    # Keep another evaluated domain assessment, but the cited quality assessment is unknown.
    for item in data.prior_results["domain"].assessments:
        if item.technology == "KIVI" and "kivi-review-p1-abstract" in item.evidence_ids:
            item.judgment = "확인 불가"
    errors = synthesis_errors(data, result, data.evidence)
    assert any("KIVI/consistency: cite evidence" in e for e in errors)


def test_every_upstream_unverified_item_must_survive_runtime_validation():
    _, data = load_case("paper_only")
    data.prior_results["tech"].unverified = ["first unresolved", "second unresolved"]
    result, _ = synthesis_result(data)
    result.unverified.remove("tech: second unresolved")
    errors = synthesis_errors(data, result, data.evidence)
    assert any("tech: upstream unverified item" in e and "second unresolved" in e for e in errors)


def test_dropped_upstream_item_fails_and_unknown_is_inconclusive():
    case, data = load_case("paper_only")
    result, checks = synthesis_result(data, preserve=False)
    assert score_case(case, data, synthesis_run(data, result, checks))["decision"] == "fail"
    result, checks = synthesis_result(data, kivi="확인 불가")
    report = score_case(case, data, synthesis_run(data, result, checks))
    assert report["decision"] == "inconclusive" and not report["problems"]


def test_wrong_judgment_or_mock_run_fails():
    case, data = load_case("paper_only")
    result, checks = synthesis_result(data, kivi="일치")
    assert score_case(case, data, synthesis_run(data, result, checks))["decision"] == "fail"
    result, checks = synthesis_result(data)
    assert score_case(case, data, synthesis_run(data, result, checks, "mock"))["decision"] == "fail"


def test_no_prior_case_only_accepts_unknown():
    case, data = load_case("no_prior")
    unknown = NodeResult(
        node="synthesis",
        summary="상위 결과가 없어 판정을 보류한다. 공개 정보 기반 추정.",
        claims=[],
        assessments=[
            Assessment(
                technology=q.technology,
                criterion=q.criterion,
                judgment="확인 불가",
                rationale="관점 결과 없음",
                evidence_ids=[],
            )
            for q in data.questions
        ],
        unverified=[f"{q.id}: 관점 결과 없음" for q in data.questions],
        limitations=["초록 요약만 존재"],
    )
    assert score_case(case, data, synthesis_run(data, unknown, []))["decision"] == "pass"
    unknown.assessments[0].judgment = "상충"
    unknown.assessments[0].evidence_ids = ["kivi-review-p1-abstract"]
    report = score_case(case, data, synthesis_run(data, unknown, []))
    assert report["decision"] == "fail"


def test_cli_scores_saved_run(tmp_path, capsys):
    case, data = load_case("paper_only")
    result, checks = synthesis_result(data)
    (tmp_path / "input.json").write_text(data.model_dump_json(), encoding="utf-8")
    (tmp_path / "result.json").write_text(
        synthesis_run(data, result, checks).model_dump_json(), encoding="utf-8"
    )
    assert main(["--case", "paper_only", "--run", str(tmp_path)]) == 0
    assert '"decision": "pass"' in capsys.readouterr().out


def test_pipeline_hands_synthesis_all_perspectives_and_code_gaps():
    class Recording(MockBackend):
        def __init__(self):
            self.inputs = {}
            self.calls = Counter()

        def generate(self, node, data, evidence, system, user):
            self.calls[node] += 1
            self.inputs[node] = data
            return super().generate(node, data, evidence, system, user)

    inputs = {node: load_input(node, "acceptance") for node in NODES}
    backend = Recording()
    final = build_main_graph(inputs, "mock", load_settings(), backend).invoke({})
    data = backend.inputs["synthesis"]
    assert set(data.prior_results) == {"tech", "market", "stakeholder", "domain"}
    assert "코드 규칙 gaps" in data.description
    assert backend.calls["synthesis"] == 1
    assert final["run_status"] == "completed"  # mock wiring only; not an evaluation


@pytest.mark.parametrize("case", ["basic", "missing-evidence", "acceptance"])
def test_fixture_cases_load_and_stay_within_question_limit(case):
    data = load_input("synthesis", case)
    assert 1 <= len(data.questions) <= load_settings().limits.questions
    criteria = {c.id for c in load_rubric("synthesis").criteria}
    assert all(q.criterion in criteria for q in data.questions)

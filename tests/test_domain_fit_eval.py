import json

import pytest

from app.evaluate_domain_fit import CASE_IDS, DATASET, load_case, main, score_case
from graph.node_graph import build_node_graph
from rag.interface import FixedEvidence
from runtime.domain_checks import domain_rule_errors
from runtime.handoff import check_handoff
from runtime.models import MockBackend
from runtime.prompts import load_rubric
from runtime.runner import load_input
from runtime.settings import load_settings
from schemas.contracts import (
    Assessment,
    Claim,
    ClaimCheck,
    Evidence,
    NodeResult,
    NodeRun,
    SearchRecord,
)

CRITERIA = ["cost", "performance", "quality", "operations", "scalability"]


def sample_run(case_id: str, judgments: dict[str, str]) -> tuple[dict, object, NodeRun]:
    """Build a synthetic fixture-mode run whose judgments and citations follow the labels."""
    case, data = load_case(case_id)
    tech = case["technology"]
    direct = [e.id for e in data.evidence if e.technology == tech and e.scope == "target"]
    claims, checks, assessments, unverified = [], [], [], []
    for criterion in CRITERIA:
        judgment = judgments.get(criterion, "확인 불가")
        label = case["items"][criterion]
        cited = list(dict.fromkeys(label["required_evidence_ids"] + direct[:1]))
        if judgment != "확인 불가" and cited:
            claims.append(
                Claim(
                    id=f"{criterion}-fact",
                    technology=tech,
                    criterion=criterion,
                    text="고정 근거에 기술된 조건",
                    kind="fact",
                    evidence_ids=cited,
                    conditions=["고정 근거 범위; 단일 GPU 실험"],
                )
            )
            checks.append(
                ClaimCheck(
                    claim_id=f"{criterion}-fact",
                    label="supported",
                    evidence_ids=cited,
                    reason="평가용 검사",
                )
            )
        else:
            cited = []
            unverified.append(f"{tech}/{criterion}: 근거 부족")
        assessments.append(
            Assessment(
                technology=tech,
                criterion=criterion,
                judgment=judgment,
                rationale="단일 GPU 실험과 데이터센터 규모 차이 있음. 공개 정보 기반 추정",
                evidence_ids=cited,
            )
        )
    run = NodeRun(
        node="domain",
        mode="fixture",
        status="needs_revision" if unverified else "completed",
        result=NodeResult(
            node="domain",
            summary="평가용 결과",
            claims=claims,
            assessments=assessments,
            unverified=unverified,
            limitations=[],
        ),
        evidence=data.evidence,
        checks=checks,
        validation_errors=[] if data.evidence else ["No evidence available"],
        searches=[
            SearchRecord(question_id=q.id, attempt=1, query=q.text, evidence_ids=[])
            for q in data.questions
        ],
        prompt_hash="fixture-test",
        model="unit-test-backend",
    )
    return case, data, run


KIVI_BOUNDED = {
    "cost": "적합",
    "performance": "조건부 적합",
    "quality": "조건부 적합",
    "operations": "확인 불가",
    "scalability": "조건부 적합",
}
ITME_BOUNDED = {
    "cost": "확인 불가",
    "performance": "조건부 적합",
    "quality": "확인 불가",
    "operations": "조건부 적합",
    "scalability": "조건부 적합",
}


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_labels_cover_every_question_with_rubric_judgments(case_id):
    case, data = load_case(case_id)
    rubric = {c.id: c for c in load_rubric("domain").criteria}
    assert {q.criterion for q in data.questions} == set(case["items"]) == set(rubric)
    assert len({q.id for q in data.questions}) == len(data.questions)
    for criterion, label in case["items"].items():
        allowed = set(rubric[criterion].judgments)
        assert set(label["accepted"]) <= allowed and set(label["disputed"]) <= allowed
        assert not set(label["accepted"]) & set(label["disputed"])
        assert set(label["required_evidence_ids"]) <= {e.id for e in data.evidence}
    assert all(e.source_type == "fixture" for e in data.evidence)
    assert all("원문 발췌 아님" in e.title or "fixture" in e.title for e in data.evidence)


def test_fit_cases_run_offline_with_the_shared_graph():
    for case_id in CASE_IDS:
        _, data = load_case(case_id)
        run = build_node_graph(
            "domain", data, "mock", load_settings(), MockBackend(), FixedEvidence(data)
        ).invoke({})["output"]
        assert len(run.result.assessments) + len(run.result.unverified) >= len(data.questions)


def test_bounded_judgments_pass():
    case, data, run = sample_run("kivi_paper", KIVI_BOUNDED)
    assert score_case(case, data, run)["decision"] == "pass"
    case, data, run = sample_run("itme_paper", ITME_BOUNDED)
    assert score_case(case, data, run)["decision"] == "pass"


def test_paper_only_performance_cannot_be_fully_fit():
    case, data, run = sample_run("kivi_paper", {**KIVI_BOUNDED, "performance": "적합"})
    report = score_case(case, data, run)
    assert report["decision"] == "fail"
    assert any("external case" in p for p in report["problems"])


def test_scorer_rejects_assessment_citations_the_judge_did_not_confirm():
    case, data, run = sample_run("kivi_paper", KIVI_BOUNDED)
    unconfirmed = data.evidence[0].model_copy(update={"id": "unconfirmed-extra-source"})
    data.evidence = [*data.evidence, unconfirmed]
    run.evidence = data.evidence
    next(c for c in run.result.claims if c.criterion == "cost").evidence_ids.append(unconfirmed.id)
    next(a for a in run.result.assessments if a.criterion == "cost").evidence_ids.append(
        unconfirmed.id
    )
    report = score_case(case, data, run)
    assert report["decision"] == "fail"
    assert any("verified claim premises" in p for p in report["problems"])


def test_missing_cost_information_is_not_unfit():
    case, data, run = sample_run("itme_paper", {**ITME_BOUNDED, "cost": "부적합"})
    assert score_case(case, data, run)["decision"] == "fail"


def test_quality_without_report_cannot_be_fit():
    case, data, run = sample_run("itme_paper", {**ITME_BOUNDED, "quality": "적합"})
    assert score_case(case, data, run)["decision"] == "fail"


def test_disputed_judgment_is_inconclusive_not_pass():
    case, data, run = sample_run("itme_paper", {**ITME_BOUNDED, "operations": "부적합"})
    assert score_case(case, data, run)["decision"] == "inconclusive"
    case, data, run = sample_run("kivi_paper", {**KIVI_BOUNDED, "cost": "확인 불가"})
    assert score_case(case, data, run)["decision"] == "inconclusive"


@pytest.mark.parametrize("case_id", ["context_only", "no_evidence"])
def test_background_or_empty_evidence_requires_unknown(case_id):
    case, data, run = sample_run(case_id, {})
    assert score_case(case, data, run)["decision"] == "pass"
    case, data, run = sample_run(case_id, {"performance": "조건부 적합"})
    report = score_case(case, data, run)
    assert report["decision"] == "fail"


def test_context_evidence_cannot_ground_a_judgment():
    case, data, run = sample_run("context_only", {})
    background = data.evidence[0].id
    run.result.claims = [
        Claim(
            id="performance-inference",
            technology="ITME",
            criterion="performance",
            text="배경 자료 기반 추론",
            kind="inference",
            evidence_ids=[background],
            conditions=["배경 자료"],
        )
    ]
    run.checks = [
        ClaimCheck(
            claim_id="performance-inference",
            label="supported",
            evidence_ids=[background],
            reason="test",
        )
    ]
    run.result.assessments[1] = Assessment(
        technology="ITME",
        criterion="performance",
        judgment="조건부 적합",
        rationale="공개 정보 기반 추정",
        evidence_ids=[background],
    )
    errors = domain_rule_errors(data, run)
    assert any("direct target evidence" in e for e in errors)
    assert score_case(case, data, run)["decision"] == "fail"


def test_cross_technology_citation_and_unfit_by_inference_are_rejected():
    case, data, run = sample_run("kivi_paper", KIVI_BOUNDED)
    foreign = Evidence(
        id="foreign-itme",
        text="다른 기술 근거",
        title="ITME fixture",
        url="https://example.invalid/itme",
        technology="ITME",
        source_type="fixture",
        scope="target",
    )
    run.evidence = [*run.evidence, foreign]
    run.result.claims[0].evidence_ids.append(foreign.id)
    assert any("cites ITME evidence" in e for e in domain_rule_errors(data, run))

    case, data, run = sample_run("kivi_paper", {**KIVI_BOUNDED, "scalability": "부적합"})
    claim = next(c for c in run.result.claims if c.criterion == "scalability")
    claim.kind = "inference"
    assert any("부적합 requires a supported fact claim" in e for e in domain_rule_errors(data, run))
    assert score_case(case, data, run)["decision"] == "fail"


def test_experimental_claims_need_conditions_and_rationale_needs_disclaimer():
    case, data, run = sample_run("kivi_paper", KIVI_BOUNDED)
    next(c for c in run.result.claims if c.criterion == "performance").conditions = [" "]
    run.result.assessments[0].rationale = "근거 있음"  # graded item (cost=적합)
    run.result.assessments[3].rationale = "런타임이 덮어쓴 문구"  # unknown item is exempt
    errors = domain_rule_errors(data, run)
    assert any("conditions" in e for e in errors)
    assert [e for e in errors if "공개 정보 기반 추정" in e] == [
        "KIVI/cost: rationale must state 공개 정보 기반 추정"
    ]


def test_empty_evidence_skips_the_sufficiency_judge_and_stays_needs_revision():
    class NoCallBackend(MockBackend):
        def sufficiency(self, data, evidence):
            raise AssertionError("sufficiency judge must not be called without evidence")

    _, data = load_case("no_evidence")
    run = build_node_graph(
        "domain", data, "fixture", load_settings(), NoCallBackend(), FixedEvidence(data)
    ).invoke({})["output"]
    assert run.status == "needs_revision"
    assert all(not c.sufficient for c in run.coverage) and len(run.coverage) == 5


def test_mock_or_changed_evidence_is_rejected():
    case, data, run = sample_run("kivi_paper", KIVI_BOUNDED)
    run.mode = "mock"
    assert score_case(case, data, run)["decision"] == "fail"
    run.mode = "fixture"
    run.evidence = run.evidence[:1]
    assert score_case(case, data, run)["decision"] == "fail"


def test_handoff_applies_domain_rules_only_to_domain():
    acceptance = load_input("domain", "acceptance")
    run = build_node_graph(
        "domain", acceptance, "mock", load_settings(), MockBackend(), FixedEvidence(acceptance)
    ).invoke({})["output"]
    run.mode, run.model = "fixture", "unit-test-backend"
    graded = run.result.assessments[0]
    graded.judgment, graded.rationale = "조건부 적합", "추정 문구 없음"
    report = check_handoff(acceptance, run)
    assert any("공개 정보 기반 추정" in p for p in report["problems"])
    market = load_input("market", "acceptance")
    market_run = build_node_graph(
        "market", market, "mock", load_settings(), MockBackend(), FixedEvidence(market)
    ).invoke({})["output"]
    market_run.mode, market_run.model = "fixture", "unit-test-backend"
    market_graded = market_run.result.assessments[0]
    market_graded.judgment, market_graded.rationale = "보통", "추정 문구 없음"
    assert not any(
        "공개 정보 기반 추정" in p for p in check_handoff(market, market_run)["problems"]
    )


def test_cli_scores_saved_run(tmp_path, capsys):
    _, data, run = sample_run("itme_paper", ITME_BOUNDED)
    (tmp_path / "input.json").write_text(data.model_dump_json(), encoding="utf-8")
    (tmp_path / "result.json").write_text(run.model_dump_json(), encoding="utf-8")
    assert main(["--case", "itme_paper", "--run", str(tmp_path)]) == 0
    assert '"decision": "pass"' in capsys.readouterr().out
    labels = json.loads((DATASET / "labels.json").read_text(encoding="utf-8"))
    assert {c["id"] for c in labels["cases"]} == set(CASE_IDS)


def test_draft_scoring_reviews_withheld_judgments_without_replacing_the_result(tmp_path):
    case, data, run = sample_run("kivi_paper", KIVI_BOUNDED)
    draft = run.result.model_copy(deep=True)
    # Runtime withheld every grade because one claim failed verification.
    run.validation_errors = ["kivi-cost-1: unsupported: test"]
    for a in run.result.assessments:
        a.judgment, a.rationale, a.evidence_ids = (
            "확인 불가",
            "근거 검증 실패로 판정을 보류합니다.",
            [],
        )
    assert score_case(case, data, run)["decision"] == "fail"
    report = score_case(case, data, run, draft)
    assert report["scored"] == "draft"
    assert report["items"]["performance"]["decision"] == "pass"
    assert report["decision"] == "fail"  # validation errors still count against the run
    (tmp_path / "input.json").write_text(data.model_dump_json(), encoding="utf-8")
    (tmp_path / "result.json").write_text(run.model_dump_json(), encoding="utf-8")
    (tmp_path / "draft.json").write_text(draft.model_dump_json(), encoding="utf-8")
    assert main(["--case", "kivi_paper", "--run", str(tmp_path), "--draft"]) == 2


def test_judge_is_not_called_when_the_draft_has_no_claims():
    class NoClaims(MockBackend):
        def generate(self, node, data, evidence, system, user):
            result = super().generate(node, data, evidence, system, user)
            result.claims = []
            for a in result.assessments:
                a.evidence_ids = []
            return result

        def judge(self, result, evidence):
            raise AssertionError("judge must not be called without claims")

    _, data = load_case("context_only")
    run = build_node_graph(
        "domain", data, "fixture", load_settings(), NoClaims(), FixedEvidence(data)
    ).invoke({})["output"]
    assert run.status == "needs_revision" and run.checks == []


def test_absence_claims_are_repaired_once_before_the_judge_runs():
    """A draft that reports missing information as a claim gets one fix round, no Judge call."""
    calls = {"generate": 0, "judge": 0}

    class AbsenceThenClean(MockBackend):
        def generate(self, node, data, evidence, system, user):
            calls["generate"] += 1
            result = super().generate(node, data, evidence, system, user)
            if calls["generate"] == 1:
                result.claims[0].text = "KIVI의 비용 절감 수치는 명시되지 않는다."
            return result

        def judge(self, result, evidence):
            calls["judge"] += 1
            assert all("명시되지 않" not in c.text for c in result.claims)
            return super().judge(result, evidence)

    _, data = load_case("kivi_paper")
    run = build_node_graph(
        "domain", data, "fixture", load_settings(), AbsenceThenClean(), FixedEvidence(data)
    ).invoke({})["output"]
    assert calls == {"generate": 2, "judge": 1}
    assert run.fix_count == 1
    assert not any("absence" in e for e in run.validation_errors)


def test_persisting_absence_claim_is_withheld_after_the_single_fix():
    class AlwaysAbsent(MockBackend):
        def generate(self, node, data, evidence, system, user):
            result = super().generate(node, data, evidence, system, user)
            result.claims[0].text = "KIVI의 비용 절감 수치는 명시되지 않는다."
            return result

    _, data = load_case("kivi_paper")
    run = build_node_graph(
        "domain", data, "fixture", load_settings(), AlwaysAbsent(), FixedEvidence(data)
    ).invoke({})["output"]
    assert run.fix_count == 1 and run.status == "needs_revision"
    assert any("absence" in e for e in run.validation_errors)
    assert all(a.judgment == "확인 불가" for a in run.result.assessments)

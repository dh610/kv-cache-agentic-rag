import pytest

from app.evaluate_tech_trl import load_case, main, score_case
from graph.node_graph import build_node_graph, contract_errors
from rag.interface import FixedEvidence
from runtime.models import MockBackend
from runtime.settings import load_settings
from schemas.contracts import (
    Assessment,
    Claim,
    ClaimCheck,
    JudgeResult,
    NodeResult,
    NodeRun,
    SearchRecord,
    TRLEstimate,
)


def sample_run(case_id: str, judgment: str) -> tuple[dict, object, NodeRun]:
    case, data = load_case(case_id)
    evidence_ids = case["required_maturity_evidence_ids"]
    claims = (
        [
            Claim(
                id="maturity-fact",
                technology=case["technology"],
                criterion="maturity",
                text="고정 근거에 기술된 검증 수준",
                kind="fact",
                evidence_ids=evidence_ids,
                conditions=["고정 근거 범위"],
            )
        ]
        if evidence_ids and judgment != "확인 불가"
        else []
    )
    run = NodeRun(
        node="tech",
        mode="fixture",
        status="needs_revision" if judgment == "확인 불가" else "completed",
        result=NodeResult(
            node="tech",
            summary="평가용 결과",
            claims=claims,
            assessments=[
                Assessment(
                    technology=case["technology"],
                    criterion="maturity",
                    judgment=judgment,
                    rationale="공개 정보 기반 잠정 추정",
                    evidence_ids=evidence_ids if claims else [],
                )
            ],
            unverified=["근거 부족"] if judgment == "확인 불가" else [],
            limitations=[],
        ),
        evidence=data.evidence,
        checks=(
            [
                ClaimCheck(
                    claim_id="maturity-fact",
                    label="supported",
                    evidence_ids=evidence_ids,
                    reason="평가용 검사",
                )
            ]
            if claims
            else []
        ),
        validation_errors=[],
        searches=[
            SearchRecord(question_id=q.id, attempt=1, query=q.text, evidence_ids=[])
            for q in data.questions
        ],
        prompt_hash="fixture-test",
        model="unit-test-backend",
    )
    return case, data, run


def test_paper_case_accepts_bounded_supported_stage():
    case, data, run = sample_run("kivi_paper", "TRL 5")
    assert score_case(case, data, run)["decision"] == "pass"


def test_paper_case_rejects_overclaimed_stage():
    case, data, run = sample_run("itme_paper", "TRL 8")
    assert score_case(case, data, run)["decision"] == "fail"


def test_paper_case_reports_abstention_as_inconclusive():
    case, data, run = sample_run("kivi_paper", "확인 불가")
    assert score_case(case, data, run)["decision"] == "inconclusive"


def test_empty_evidence_requires_unknown():
    case, data, run = sample_run("no_evidence", "확인 불가")
    run.validation_errors = ["No evidence available"]
    assert score_case(case, data, run)["decision"] == "pass"


def test_mock_or_unverified_citation_is_rejected():
    case, data, run = sample_run("kivi_paper", "TRL 5")
    run.mode = "mock"
    assert score_case(case, data, run)["decision"] == "fail"
    run.mode = "fixture"
    run.checks = []
    assert score_case(case, data, run)["decision"] == "fail"


def test_missing_experiment_evidence_is_rejected():
    case, data, run = sample_run("itme_paper", "TRL 6")
    run.result.assessments[0].evidence_ids = ["itme-curated-p8-fpga"]
    assert score_case(case, data, run)["decision"] == "fail"


def test_changed_run_evidence_is_rejected():
    case, data, run = sample_run("kivi_paper", "TRL 5")
    run.evidence = run.evidence[:1]
    assert score_case(case, data, run)["decision"] == "fail"


def test_cli_scores_saved_run(tmp_path, capsys):
    _, data, run = sample_run("kivi_paper", "TRL 5")
    (tmp_path / "input.json").write_text(data.model_dump_json(), encoding="utf-8")
    (tmp_path / "result.json").write_text(run.model_dump_json(), encoding="utf-8")
    assert main(["--case", "kivi_paper", "--run", str(tmp_path)]) == 0
    assert '"decision": "pass"' in capsys.readouterr().out


def runtime_errors(data, run):
    data = data.model_copy(
        update={"questions": [q for q in data.questions if q.criterion == "maturity"]}
    )
    return contract_errors("tech", data, run.result, run.evidence, JudgeResult(checks=run.checks))


@pytest.mark.parametrize("judgment,level", [("TRL 5", 9), ("TRL 5", None), ("확인 불가", 5)])
def test_both_validators_reject_conflicting_trl_fields(judgment, level):
    case, data, run = sample_run("kivi_paper", judgment)
    run.result.trl_estimates = [
        TRLEstimate(
            technology="KIVI",
            level=level,
            rationale="잠정 추정",
            evidence_ids=case["required_maturity_evidence_ids"],
        )
    ]
    assert any("disagrees" in e for e in runtime_errors(data, run))
    assert score_case(case, data, run)["decision"] == "fail"


@pytest.mark.parametrize(
    "case_id,judgment,level", [("kivi_paper", "TRL 5", 5), ("no_evidence", "확인 불가", None)]
)
def test_both_validators_accept_consistent_trl_fields(case_id, judgment, level):
    case, data, run = sample_run(case_id, judgment)
    run.result.trl_estimates = [
        TRLEstimate(
            technology=case["technology"],
            level=level,
            rationale="잠정 추정",
            evidence_ids=case["required_maturity_evidence_ids"],
        )
    ]
    assert runtime_errors(data, run) == []
    assert score_case(case, data, run)["decision"] == "pass"


def test_both_validators_reject_evidence_not_confirmed_by_judge():
    case, data, run = sample_run("itme_paper", "TRL 6")
    run.checks[0].evidence_ids = run.checks[0].evidence_ids[:1]
    assert any("verified claim premises" in e for e in runtime_errors(data, run))
    assert score_case(case, data, run)["decision"] == "fail"


def test_subset_judge_evidence_is_allowed_when_assessment_only_cites_that_subset():
    _, data, run = sample_run("itme_paper", "TRL 6")
    run.checks[0].evidence_ids = run.checks[0].evidence_ids[:1]
    run.result.assessments[0].evidence_ids = run.checks[0].evidence_ids
    assert runtime_errors(data, run) == []


def test_scorer_rejects_ambiguous_duplicate_checks():
    case, data, run = sample_run("itme_paper", "TRL 6")
    run.checks.append(run.checks[0].model_copy(update={"label": "unsupported"}))
    assert score_case(case, data, run)["decision"] == "fail"


@pytest.mark.parametrize("fault", ["conflicting_trl", "unchecked_citation"])
def test_graph_rejects_regressions_after_bounded_fix(fault):
    case, data, run = sample_run("itme_paper", "TRL 6")
    data.questions = [q for q in data.questions if q.criterion == "maturity"]
    if fault == "conflicting_trl":
        run.result.trl_estimates = [
            TRLEstimate(
                technology="ITME",
                level=9,
                rationale="과대 판정",
                evidence_ids=case["required_maturity_evidence_ids"],
            )
        ]
    else:
        run.checks[0].evidence_ids = run.checks[0].evidence_ids[:1]

    class FixedBackend(MockBackend):
        def generate(self, *args):
            return run.result.model_copy(deep=True)

        def judge(self, *args):
            return JudgeResult(checks=run.checks)

    state = build_node_graph(
        "tech", data, "fixture", load_settings(), FixedBackend(), FixedEvidence(data)
    ).invoke({})
    output = state["output"]
    assert output.fix_count == 1
    assert output.status == "needs_revision"
    assert output.validation_errors
    assert not output.result.claims
    assert all(a.judgment == "확인 불가" for a in output.result.assessments)
    assert all(t.level is None for t in output.result.trl_estimates)

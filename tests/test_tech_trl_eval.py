from app.evaluate_tech_trl import load_case, main, score_case
from schemas.contracts import Assessment, Claim, ClaimCheck, NodeResult, NodeRun, SearchRecord


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

import pytest

from app.evaluate_stakeholder import load_case, load_labels, main, score_case
from schemas.contracts import Assessment, Claim, ClaimCheck, NodeResult, NodeRun, SearchRecord


def sample_run(case_id: str, judgment: str, conditions=None) -> tuple[dict, object, NodeRun]:
    case, data = load_case(case_id)
    graded = judgment != "확인 불가" and data.evidence
    evidence_ids = [e.id for e in data.evidence] if graded else []
    claims = (
        [
            Claim(
                id="reaction-fact",
                technology=case["technology"],
                criterion=case["criterion"],
                # Quote the fixture text itself so the verbatim check is exercised.
                text=f"「{data.evidence[0].text}」 — 검토자 요약, 날짜 미확인",
                kind="fact",
                evidence_ids=evidence_ids,
                conditions=conditions if conditions is not None else ["고정 근거 범위"],
            )
        ]
        if graded
        else []
    )
    run = NodeRun(
        node="stakeholder",
        mode="fixture",
        status="needs_revision" if judgment == "확인 불가" else "completed",
        result=NodeResult(
            node="stakeholder",
            summary="평가용 결과",
            claims=claims,
            assessments=[
                Assessment(
                    technology=case["technology"],
                    criterion=case["criterion"],
                    judgment=judgment,
                    rationale="평가용 판정 사유",
                    evidence_ids=evidence_ids,
                )
            ],
            unverified=[f"{case['technology']}/{case['criterion']}: 근거 부족"]
            if judgment == "확인 불가"
            else [],
            limitations=[],
        ),
        evidence=data.evidence,
        checks=(
            [
                ClaimCheck(
                    claim_id="reaction-fact",
                    label="supported",
                    evidence_ids=evidence_ids,
                    reason="평가용 검사",
                )
            ]
            if claims
            else []
        ),
        validation_errors=[] if data.evidence else ["No evidence available"],
        searches=[
            SearchRecord(question_id=q.id, attempt=1, query=q.text, evidence_ids=evidence_ids)
            for q in data.questions
        ],
        prompt_hash="fixture-test",
        model="unit-test-backend",
    )
    return case, data, run


def test_every_case_input_matches_its_label_and_rubric():
    from runtime.prompts import load_rubric

    criteria = {c.id: c for c in load_rubric("stakeholder").criteria}
    for case in load_labels()["cases"]:
        _, data = load_case(case["id"])
        assert len(data.questions) == 1
        question = data.questions[0]
        assert (question.technology, question.criterion) == (case["technology"], case["criterion"])
        assert set(case["allowed_judgments"]) <= set(criteria[case["criterion"]].judgments)
        assert case["expected_unknown"] == (not case["allowed_judgments"])
        assert set(case["required_evidence_ids"]) <= {e.id for e in data.evidence}


@pytest.mark.parametrize("judgment", ["중립", "긍정"])
def test_third_party_adopter_source_accepts_reviewer_range(judgment):
    case, data, run = sample_run("adopter_third_party", judgment)
    assert score_case(case, data, run)["decision"] == "pass"


def test_third_party_adopter_source_rejects_judgment_outside_range():
    case, data, run = sample_run("adopter_third_party", "부정")
    assert score_case(case, data, run)["decision"] == "fail"


def test_abstention_on_gradable_case_is_inconclusive():
    case, data, run = sample_run("adopter_third_party", "확인 불가")
    assert score_case(case, data, run)["decision"] == "inconclusive"


def test_approach_level_evidence_requires_explicit_condition():
    case, data, run = sample_run(
        "industry_approach_level", "긍정", ["접근 전반 근거, ITME 직접 언급 없음"]
    )
    assert score_case(case, data, run)["decision"] == "pass"
    case, data, run = sample_run("industry_approach_level", "긍정", ["ITME 직접 언급"])
    report = score_case(case, data, run)
    assert report["decision"] == "fail"
    assert any("접근 전반" in p for p in report["problems"])


@pytest.mark.parametrize(
    "case_id,judgment",
    [
        ("industry_repost_trap", "긍정"),
        ("competitor_third_party_trap", "중립"),
        ("competitor_self_report_trap", "부정"),
    ],
)
def test_trap_cases_fail_when_graded(case_id, judgment):
    case, data, run = sample_run(case_id, judgment)
    report = score_case(case, data, run)
    assert report["decision"] == "fail"
    assert any("not a stakeholder reaction" in p for p in report["problems"])


@pytest.mark.parametrize(
    "case_id",
    [
        "industry_repost_trap",
        "competitor_third_party_trap",
        "competitor_self_report_trap",
        "no_evidence",
    ],
)
def test_trap_and_empty_cases_pass_only_with_unknown(case_id):
    case, data, run = sample_run(case_id, "확인 불가")
    assert score_case(case, data, run)["decision"] == "pass"


def test_unknown_without_unverified_entry_is_rejected():
    case, data, run = sample_run("no_evidence", "확인 불가")
    run.result.unverified = []
    assert score_case(case, data, run)["decision"] == "fail"


def test_mock_or_unverified_citation_is_rejected():
    case, data, run = sample_run("adopter_third_party", "중립")
    run.mode = "mock"
    assert score_case(case, data, run)["decision"] == "fail"
    run.mode = "fixture"
    run.checks = []
    assert score_case(case, data, run)["decision"] == "fail"


def test_fabricated_quote_is_rejected_even_when_judge_supported_it():
    case, data, run = sample_run("adopter_third_party", "중립")
    run.result.claims[
        0
    ].text = "「개발자들이 KIVI로 2.6배 메모리를 절감했다고 확인했다」 — 커뮤니티"
    report = score_case(case, data, run)
    assert report["decision"] == "fail"
    assert any("not verbatim" in p for p in report["problems"])


def test_changed_run_evidence_is_rejected():
    case, data, run = sample_run("adopter_third_party", "중립")
    run.evidence = []
    assert score_case(case, data, run)["decision"] == "fail"


def test_cli_scores_saved_run(tmp_path, capsys):
    _, data, run = sample_run("adopter_third_party", "중립")
    (tmp_path / "input.json").write_text(data.model_dump_json(), encoding="utf-8")
    (tmp_path / "result.json").write_text(run.model_dump_json(), encoding="utf-8")
    assert main(["--case", "adopter_third_party", "--run", str(tmp_path)]) == 0
    assert '"decision": "pass"' in capsys.readouterr().out

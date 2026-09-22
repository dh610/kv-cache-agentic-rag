import pytest

from graph.node_graph import build_node_graph
from rag.interface import FixedEvidence
from runtime.handoff import check_handoff, reference_issues
from runtime.models import MockBackend
from runtime.prompts import load_rubric
from runtime.runner import load_input
from runtime.settings import load_settings
from schemas.contracts import NODES, Assessment, NodeResult, NodeRun, SearchRecord


@pytest.mark.parametrize("node", NODES)
def test_acceptance_case_covers_both_technologies_and_all_rubric_items(node):
    data = load_input(node, "acceptance")
    assert len(data.questions) == 2 * len(load_rubric(node).criteria)
    graph = build_node_graph(
        node, data, "mock", load_settings(), MockBackend(), FixedEvidence(data)
    )
    run = graph.invoke({})["output"]
    assert len(run.result.assessments) == len(data.questions)
    assert not check_handoff(data, run)["ready"]  # Mock never counts as delivery.


def unknown_market_run():
    data = load_input("market", "acceptance")
    result = NodeResult(
        node="market",
        summary="제공된 자료로 확인되지 않은 항목을 보고합니다.",
        claims=[],
        assessments=[
            Assessment(
                technology=q.technology,
                criterion=q.criterion,
                judgment="확인 불가",
                rationale="검색 한도 내 직접 근거를 찾지 못했습니다.",
                evidence_ids=[],
            )
            for q in data.questions
        ],
        unverified=[q.id for q in data.questions],
        limitations=["공개 정보 제한"],
    )
    run = NodeRun(
        node="market",
        mode="web",
        model="unit-test-stub",
        status="needs_revision",
        result=result,
        evidence=[],
        checks=[],
        validation_errors=["No evidence available"],
        searches=[
            SearchRecord(question_id=q.id, attempt=1, query=q.text, evidence_ids=[])
            for q in data.questions
        ],
        prompt_hash="unit-test-hash",
    )
    return data, run


def test_complete_unknown_is_a_handoff_without_becoming_a_known_answer():
    data, run = unknown_market_run()
    report = check_handoff(data, run)
    assert report["ready"] and report["expected_items"] == 6
    assert len(report["unknown_items"]) == 6
    assert run.status == "needs_revision"  # Handoff checker never rewrites the run.


@pytest.mark.parametrize(
    "mutation", ["missing_item", "search_failure", "placeholder", "missing_history", "blank_reason"]
)
def test_incomplete_or_dummy_results_do_not_pass_handoff(mutation):
    data, run = unknown_market_run()
    if mutation == "missing_item":
        run.result.assessments.pop()
    elif mutation == "search_failure":
        run.searches[0].error = "Search failed: TimeoutError"
    elif mutation == "placeholder":
        run.result.summary = "[더미] 나중에 작성"
    elif mutation == "missing_history":
        run.searches = []
    elif mutation == "blank_reason":
        run.result.assessments[0].rationale = " "
    assert not check_handoff(data, run)["ready"]


def test_market_contract_uses_the_three_design_categories():
    assert {c.id for c in load_rubric("market").criteria} == {"growth", "adoption", "ecosystem"}


def test_required_bibliographic_fields_and_unknown_web_dates():
    paper = load_input("tech").evidence[0].model_copy(update={"source_type": "paper"})
    assert any("authors" in s for s in reference_issues(paper))
    paper.authors, paper.year, paper.venue = "Test author", 2024, "Test conference"
    assert any("citation_id" in s for s in reference_issues(paper))  # 권(호)·페이지
    paper.citation_id = "12(3), 45-67"
    assert reference_issues(paper) == []
    patent = paper.model_copy(update={"source_type": "patent", "citation_id": None})
    assert {"publisher", "published_at", "citation_id"} <= {
        s.split("missing ")[1] for s in reference_issues(patent)
    }
    patent.publisher, patent.published_at, patent.citation_id = "Test Corp", "2025-03", "US-1-A1"
    assert reference_issues(patent) == []
    web = paper.model_copy(
        update={
            "source_type": "web",
            "authors": None,
            "publisher": "Test organization",
            "site": "example.invalid",
            "retrieved_at": "2026-09-22T00:00:00Z",
        }
    )
    assert any("published_at" in s for s in reference_issues(web))
    web.published_at = "2026-09-21"
    assert reference_issues(web) == []

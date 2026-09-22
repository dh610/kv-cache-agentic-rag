"""충분성 판정기의 형식 실수는 근거 부족으로 정리하고 계속한다 (설계서 D.3). 실행 실패만 failed."""

import pytest

from graph.node_graph import build_node_graph, normalize_coverage
from rag.interface import FixedEvidence
from runtime.models import MockBackend
from runtime.runner import load_input
from runtime.settings import load_settings
from schemas.contracts import Coverage, SufficiencyResult


def run(backend):
    data = load_input("tech", "basic")
    graph = build_node_graph("tech", data, "mock", load_settings(), backend, FixedEvidence(data))
    return graph.invoke({}), data


class SloppyJudge(MockBackend):
    """실제 live 에서 관찰된 실수들을 한 번에: 빠진 질문, 모르는 id, 다른 기술 근거, 중복·미지 question_id."""

    def sufficiency(self, data, evidence):
        good = super().sufficiency(data, evidence).items
        first, *rest = good
        other_tech = next(e for e in evidence if e.technology != data.questions[0].technology)
        return SufficiencyResult(
            items=[
                Coverage(
                    question_id=first.question_id,
                    sufficient=True,
                    evidence_ids=[other_tech.id, "not-an-evidence-id"],
                    reason="다른 기술 근거와 모르는 id 를 인용",
                ),
                Coverage(
                    question_id=first.question_id, sufficient=True, evidence_ids=[], reason="중복"
                ),
                Coverage(
                    question_id="unknown-question", sufficient=True, evidence_ids=[], reason="미지"
                ),
                # rest(나머지 질문)는 아예 빠뜨린다.
            ]
        )


def test_sloppy_sufficiency_output_is_normalized_not_fatal():
    state, data = run(SloppyJudge())
    assert state["output"].status != "failed"
    assert not any("Sufficiency failed" in e for e in state["output"].validation_errors)
    coverage = state["coverage"]
    assert [c.question_id for c in coverage] == [
        q.id for q in data.questions
    ]  # 질문마다 정확히 하나
    first = coverage[0]
    assert first.sufficient is False and "not-an-evidence-id" not in first.evidence_ids
    assert all(c.sufficient is False for c in coverage[1:])  # 빠진 질문은 부족
    assert state["is_sufficient"] is False


class BrokenJudge(MockBackend):
    def sufficiency(self, data, evidence):
        raise RuntimeError("provider down")


def test_provider_failure_in_sufficiency_is_still_fatal():
    state, _ = run(BrokenJudge())
    assert state["output"].status == "failed"
    assert any("Sufficiency failed" in e for e in state["output"].validation_errors)


def test_normalize_keeps_well_formed_output_unchanged():
    data = load_input("tech", "basic")
    review = MockBackend().sufficiency(data, data.evidence)
    out = normalize_coverage(review, data.questions, data.evidence)
    assert [c.model_dump() for c in out] == [c.model_dump() for c in review.items]


class EmptyJudge(MockBackend):
    def sufficiency(self, data, evidence):
        return SufficiencyResult(items=[])


def test_review_with_no_usable_item_still_fails_closed():
    state, _ = run(EmptyJudge())
    assert state["output"].status == "failed"
    assert any("Sufficiency failed" in e for e in state["output"].validation_errors)


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_duplicate_judgments_never_become_sufficient_by_order(reverse):
    data = load_input("tech")
    original = MockBackend().sufficiency(data, data.evidence).items[0]
    conflicting = original.model_copy(update={"sufficient": False, "reason": "부족"})
    items = [conflicting, original] if reverse else [original, conflicting]
    out = normalize_coverage(SufficiencyResult(items=items), data.questions, data.evidence)
    assert not out[0].sufficient and "중복" in out[0].reason


@pytest.mark.parametrize("invalid", ["unknown-id", "other-technology"])
def test_partially_invalid_citations_leave_insufficient_with_reason(invalid):
    data = load_input("tech")
    original = MockBackend().sufficiency(data, data.evidence).items[0]
    extra = "unknown-id" if invalid == "unknown-id" else data.evidence[1].id
    original.evidence_ids.append(extra)
    out = normalize_coverage(SufficiencyResult(items=[original]), data.questions, data.evidence)
    assert not out[0].sufficient
    assert extra not in out[0].evidence_ids
    assert "근거 제거" in out[0].reason

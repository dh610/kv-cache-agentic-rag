from collections import Counter

import pytest

from graph.main_graph import RESULT_KEYS, build_main_graph
from graph.node_graph import build_node_graph
from rag.interface import FixedEvidence
from runtime.models import MockBackend
from runtime.prompts import render
from runtime.runner import load_input
from runtime.settings import load_settings
from schemas.contracts import NODES, JudgeResult, NodeRun


def run(node="tech", case="basic", backend=None, source=None, mode="mock"):
    data = load_input(node, case)
    return build_node_graph(
        node, data, mode, load_settings(), backend or MockBackend(), source or FixedEvidence(data)
    ).invoke({})["output"]


@pytest.mark.parametrize("node", NODES)
def test_every_node_runs_independently(node):
    result = run(node)
    assert isinstance(result, NodeRun)
    assert result.status == "completed"
    assert len(result.searches) == 2  # Every question, not only questions[0].
    assert {c.technology for c in result.result.claims} == {"KIVI", "ITME"}
    assert result.prompt_hash


@pytest.mark.parametrize("node", NODES)
def test_empty_evidence_is_not_success(node):
    result = run(node, "missing-evidence")
    assert result.status == "needs_revision"
    assert not result.result.claims
    assert result.result.unverified
    assert len(result.searches) == 2  # Fixtures are never re-searched.


def test_live_prompt_cannot_see_stale_fixture_evidence():
    data = load_input("tech")
    _, prompt, digest = render("tech", data, [])
    assert "kivi-abstract-fixture" not in prompt
    assert data.evidence[0].text not in prompt
    assert digest != render("tech", data, data.evidence)[2]


class UnknownCitation(MockBackend):
    def generate(self, *args):
        result = super().generate(*args)
        result.claims[0].evidence_ids = ["invented-source"]
        return result


class MissingJudgeCheck(MockBackend):
    def judge(self, result, evidence):
        return JudgeResult(checks=super().judge(result, evidence).checks[:1])


class Misstated(MockBackend):
    def judge(self, result, evidence):
        judgment = super().judge(result, evidence)
        judgment.checks[0].label = "misstated"
        return judgment


class MissingClaimPremise(MockBackend):
    def generate(self, *args):
        result = super().generate(*args)
        result.claims = []
        result.assessments[0].judgment = "원리 확인"
        return result


@pytest.mark.parametrize(
    "backend", [UnknownCitation(), MissingJudgeCheck(), Misstated(), MissingClaimPremise()]
)
def test_invalid_claims_cannot_be_final_accepted_output(backend):
    result = run(backend=backend)
    assert result.status == "needs_revision"
    assert result.validation_errors
    assert all(a.judgment == "확인 불가" for a in result.result.assessments)
    assert "검증을 통과하지" in result.result.summary


class BrokenBackend(MockBackend):
    def generate(self, *args):
        raise RuntimeError("provider error with confidential detail")


def test_provider_failure_is_failed_not_empty_success():
    result = run(backend=BrokenBackend())
    assert result.status == "failed"
    assert "confidential" not in result.model_dump_json()


class EmptySearch:
    retryable = True

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def search(self, question, attempt):
        self.calls.append((question.id, attempt))
        if self.fail:
            raise RuntimeError("private provider detail")
        return []


@pytest.mark.parametrize("fail", [False, True])
def test_search_budget_is_bounded_per_question(fail):
    source = EmptySearch(fail)
    result = run(source=source, mode="rag")
    assert result.status == "needs_revision"
    assert len(source.calls) == 6
    assert sorted(a for _, a in source.calls) == [1, 1, 2, 2, 3, 3]
    assert "private provider detail" not in result.model_dump_json()


def test_partial_evidence_retries_for_unanswered_question():
    class Partial(EmptySearch):
        def search(self, question, attempt):
            self.calls.append((question.id, attempt))
            return load_input("tech").evidence[:1] if question.technology == "KIVI" else []

    source = Partial()
    result = run(source=source, mode="rag")
    assert result.status == "needs_revision"
    # 그림 2의 '질문 수정'은 부족한 질문만 다시 찾는다: 답을 얻은 KIVI 질문은 1회, 못 얻은 ITME 질문은 3회.
    questions = load_input("tech").questions
    expected = {q.id: (1 if q.technology == "KIVI" else 3) for q in questions}
    assert dict(Counter(q for q, _ in source.calls)) == expected
    assert result.is_sufficient is False
    assert any("근거 부족" in u for u in result.result.unverified)


class RecordingBackend(MockBackend):
    def __init__(self):
        self.inputs = {}
        self.calls = Counter()

    def generate(self, node, data, evidence, system, user):
        self.calls[node] += 1
        self.inputs[node] = (data, evidence)
        return super().generate(node, data, evidence, system, user)


def test_parallel_join_runs_once_and_preserves_upstream_sources():
    inputs = {node: load_input(node) for node in NODES}
    # Give each branch its own ID to catch source-catalog overwrites at the join.
    for node, data in inputs.items():
        for e in data.evidence:
            e.id = f"{node}-{e.id}"
    backend = RecordingBackend()
    final = build_main_graph(inputs, "mock", load_settings(), backend).invoke({})
    assert final["run_status"] == "completed"
    assert backend.calls == Counter({node: 1 for node in NODES})
    synthesis_input, sources = backend.inputs["synthesis"]
    assert set(synthesis_input.prior_results) == {"tech", "market", "stakeholder", "domain"}
    assert len({e.id for e in sources}) == 10  # 4 upstream roles + synthesis fixture.
    assert set(backend.inputs["market"][0].prior_results) == {"tech"}


def test_parent_pipeline_preserves_failure_status():
    class FailTech(RecordingBackend):
        def generate(self, node, *args):
            if node == "tech":
                raise RuntimeError("failed")
            return super().generate(node, *args)

    inputs = {node: load_input(node) for node in NODES}
    final = build_main_graph(inputs, "mock", load_settings(), FailTech()).invoke({})
    assert final["run_status"] == "failed"
    assert final[RESULT_KEYS["tech"]].result.unverified


def test_live_pipeline_uses_injected_sources_and_drops_demo_evidence():
    class RealSource:
        retryable = False

        def search(self, question, attempt):
            evidence = load_input("tech").evidence
            for e in evidence:
                e.id = "injected-" + e.id
            return evidence

    inputs = {node: load_input(node) for node in NODES}
    sources = {role: RealSource() for role in NODES[:4]}
    backend = RecordingBackend()
    final = build_main_graph(inputs, "live", load_settings(), backend, sources).invoke({})
    # Mock assessments are unknown in live mode and must not report success.
    assert final["run_status"] == "needs_revision"
    assert len(final["report"].evidence) == 2
    assert all(e.id.startswith("injected-") for e in final["report"].evidence)


def test_verdict_follows_design_table_15():
    assert run().verdict == "통과"
    assert run().is_sufficient is True
    assert run(backend=UnknownCitation()).verdict == "추가 근거 필요"
    assert run(backend=Misstated()).verdict == "표현 오류"


class CountingFix(Misstated):
    def __init__(self):
        self.fixes = 0

    def fix(self, result, checks, evidence):
        self.fixes += 1
        return super().fix(result, checks, evidence)


def test_fix_loop_is_bounded_by_limits_fix():
    backend = CountingFix()
    result = run(backend=backend)
    assert backend.fixes == 1  # 표 13: 답변 수정 1회
    assert result.fix_count == 1
    assert result.status == "needs_revision"  # mock은 표현을 고치지 못하므로 재검증도 실패

    settings = load_settings()
    settings.limits.fix = 0
    data = load_input("tech")
    backend = CountingFix()
    result = build_node_graph("tech", data, "mock", settings, backend, FixedEvidence(data)).invoke(
        {}
    )["output"]
    assert backend.fixes == 0
    assert result.verdict == "표현 오류"


def test_rewrite_keeps_korean_question_and_adds_english_keywords():
    source = EmptySearch()
    result = run(source=source, mode="rag")
    by_question = {}
    for record in result.searches:
        by_question.setdefault(record.question_id, []).append(record)
    for question_id, records in by_question.items():
        first, second = records[0].query, records[1].query
        assert second != first
        assert first in second  # 한국어 원문 유지
        assert "evidence" in second  # 영어 핵심어 추가 (mock 자리 표시자)
    assert result.search_count == {q: 3 for q in by_question}


class InventedIds(MockBackend):
    def sufficiency(self, questions, evidence):
        verdict = super().sufficiency(questions, evidence)
        verdict.missing_question_ids = ["not-a-question"]
        verdict.sufficient = False
        return verdict


def test_sufficiency_judge_cannot_invent_question_ids():
    source = EmptySearch()
    result = run(backend=InventedIds(), source=source, mode="rag")
    # 알 수 없는 id는 버려지므로 재검색 대상이 없고, 첫 검색 뒤 바로 작성으로 넘어간다.
    assert len(source.calls) == 2
    assert result.status == "needs_revision"

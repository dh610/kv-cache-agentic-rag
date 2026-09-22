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
    data = load_input(node)
    assert Counter(s.question_id for s in result.searches) == Counter(q.id for q in data.questions)
    assert {c.technology for c in result.result.claims} == {"KIVI", "ITME"}
    assert result.prompt_hash


@pytest.mark.parametrize("node", NODES)
def test_empty_evidence_is_not_success(node):
    result = run(node, "missing-evidence")
    assert result.status == "needs_revision"
    assert not result.result.claims
    assert result.result.unverified
    data = load_input(node, "missing-evidence")
    assert Counter(s.question_id for s in result.searches) == Counter(q.id for q in data.questions)


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
    assert len(source.calls) == 6


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
    expected_market = {
        (tech, criterion)
        for tech in ("KIVI", "ITME")
        for criterion in ("growth", "adoption", "ecosystem")
    }
    market_items = synthesis_input.prior_results["market"].assessments
    assert len(market_items) == 6
    assert {(a.technology, a.criterion) for a in market_items} == expected_market


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

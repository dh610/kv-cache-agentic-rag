"""Count real source invocations with deterministic backends; no API calls."""

from collections import Counter

from graph.node_graph import build_node_graph
from runtime.models import MockBackend
from runtime.runner import load_input
from runtime.settings import load_settings
from schemas.contracts import QueryPlan


def input_data():
    data = load_input("tech")
    data.questions = [q for q in data.questions if q.criterion == "mechanism"]
    return data


class Backend(MockBackend):
    def __init__(self):
        self.planned = []
        self.reviewed = []
        self.generated = []

    def plan(self, data, feedback):
        self.planned.append([q.id for q in data.questions])
        return super().plan(data, feedback)

    def sufficiency(self, data, evidence):
        self.reviewed.append([q.id for q in data.questions])
        return super().sufficiency(data, evidence)

    def generate(self, node, data, evidence, system, user):
        self.generated.append([q.id for q in data.questions])
        result = super().generate(node, data, evidence, system, user)
        for assessment in result.assessments:
            assessment.judgment = "원리 확인"
        return result


class Source:
    retryable = True

    def __init__(self, missing=(), fail_critical=False):
        self.calls = []
        self.missing = set(missing)
        self.fail_critical = fail_critical

    def search(self, question, attempt):
        self.calls.append((question.id, attempt, question.text))
        if self.fail_critical and question.technology == "KIVI" and attempt == 2:
            raise RuntimeError("temporary search failure")
        return [
            e
            for e in input_data().evidence
            if e.technology == question.technology and e.technology not in self.missing
        ]


def execute(source, backend=None, previous=None, refresh=None):
    backend = backend or Backend()
    state = build_node_graph(
        "tech",
        input_data(),
        "rag",
        load_settings(),
        backend,
        source,
        previous=previous,
        refresh_technologies=refresh,
    ).invoke({}, config={"recursion_limit": 80})
    return state["output"], backend


def counts(source):
    return Counter(qid for qid, _, _ in source.calls)


def test_one_insufficient_question_does_not_research_sufficient_question():
    source = Source(missing={"ITME"})
    run, backend = execute(source)
    assert counts(source) == {"kivi-mechanism": 2, "itme-mechanism": 3}
    assert backend.planned[-1] == backend.reviewed[-1] == ["itme-mechanism"]
    assert all(qs == ["kivi-mechanism", "itme-mechanism"] for qs in backend.generated)
    assert next(c for c in run.coverage if c.question_id == "kivi-mechanism").sufficient
    assert run.status == "needs_revision"
    assert run.result.unverified  # No invented success for the exhausted question.


def test_sufficient_questions_still_need_both_search_intents():
    source = Source()
    run, _ = execute(source)
    assert counts(source) == {"kivi-mechanism": 2, "itme-mechanism": 2}
    for q in input_data().questions:
        assert [r.intent for r in run.searches if r.question_id == q.id] == ["positive", "critical"]
    assert run.status == "completed"


def test_failed_critical_search_retries_critical_only_and_keeps_error_history():
    source = Source(fail_critical=True)
    run, backend = execute(source)
    assert counts(source) == {"kivi-mechanism": 3, "itme-mechanism": 2}
    assert backend.planned[-1] == ["kivi-mechanism"]
    assert [r.intent for r in run.searches if r.question_id == "kivi-mechanism"] == [
        "positive",
        "critical",
        "critical",
    ]
    assert run.status == "needs_revision"
    assert any("Search failed" in e for e in run.validation_errors)


def test_judge_rejection_reopens_only_the_affected_question():
    class RejectITME(Backend):
        def judge(self, result, evidence):
            checks = super().judge(result, evidence)
            for check in checks.checks:
                if "itme-mechanism" in check.claim_id:
                    check.label = "unsupported"
            return checks

    source = Source()
    run, backend = execute(source, RejectITME())
    assert counts(source) == {"kivi-mechanism": 2, "itme-mechanism": 3}
    assert backend.planned[-1] == ["itme-mechanism"]
    assert run.status == "needs_revision" and run.fix_count <= 1


def test_unscoped_warning_is_preserved_without_researching_every_good_question():
    class Warning(Backend):
        def generate(self, *args):
            result = super().generate(*args)
            result.unverified.append("추가 검토가 필요한 일반적인 제약")
            return result

    source = Source()
    run, _ = execute(source, Warning())
    assert set(counts(source).values()) == {2}
    assert run.status == "needs_revision"
    assert "추가 검토가 필요한 일반적인 제약" in run.result.unverified


def test_rewrite_cannot_sneak_completed_questions_back_into_plan():
    class BadSubset(Backend):
        def plan(self, data, feedback):
            result = super().plan(data, feedback)
            if len(data.questions) == 1:
                return QueryPlan(queries=MockBackend().plan(input_data(), []).queries)
            return result

    source = Source(missing={"ITME"})
    run, _ = execute(source, BadSubset())
    assert counts(source) == {"kivi-mechanism": 2, "itme-mechanism": 2}
    assert run.status == "failed"
    assert any("Rewrite failed" in e for e in run.validation_errors)


def test_supplement_reuses_good_retrieval_and_adds_only_missing_question():
    first, _ = execute(Source(missing={"ITME"}))
    source = Source()
    second, backend = execute(source, previous=first)
    assert counts(source) == {"itme-mechanism": 1}
    assert all(qs == ["itme-mechanism"] for qs in backend.planned + backend.reviewed)
    assert [a for _, a, _ in source.calls] == [4]  # both intents were already attempted
    assert second.searches[: len(first.searches)] == first.searches
    assert {e.id for e in first.evidence}.issubset({e.id for e in second.evidence})
    assert second.status == "completed"
    assert len(second.result.assessments) == 2


def test_supplement_does_not_plan_or_search_already_sufficient_questions():
    first, _ = execute(Source())
    source = Source()
    second, backend = execute(source, previous=first)
    assert not source.calls and not backend.planned and not backend.reviewed
    assert backend.generated == [["kivi-mechanism", "itme-mechanism"]]
    assert second.searches == first.searches and second.status == "completed"


def test_changed_upstream_tech_rechecks_cached_coverage_before_searching():
    first, _ = execute(Source())
    source = Source()
    second, backend = execute(source, previous=first, refresh={"ITME"})
    assert backend.reviewed == [["itme-mechanism"]]
    assert not source.calls and second.status == "completed"


def test_supplement_has_only_one_new_search_budget_for_unresolved_question():
    first, _ = execute(Source(missing={"ITME"}))
    source = Source(missing={"ITME"})
    second, _ = execute(source, previous=first)
    assert counts(source) == {"itme-mechanism": 3}
    assert [a for _, a, _ in source.calls] == [4, 5, 6]
    assert second.status == "needs_revision"


def test_changed_upstream_can_reopen_only_its_invalidated_coverage():
    class Changed(Backend):
        def sufficiency(self, data, evidence):
            result = super().sufficiency(data, evidence)
            if len(self.reviewed) == 1:
                for item in result.items:
                    item.sufficient = False
            return result

    first, _ = execute(Source())
    source = Source()
    second, backend = execute(source, Changed(), previous=first, refresh={"ITME"})
    assert counts(source) == {"itme-mechanism": 1}
    assert backend.reviewed == [["itme-mechanism"], ["itme-mechanism"]]
    assert second.status == "completed"


def test_cached_sufficiency_cannot_reuse_missing_evidence_or_demo_input():
    first, _ = execute(Source())
    first.evidence = [e for e in first.evidence if e.technology != "KIVI"]
    source = Source()
    second, _ = execute(source, previous=first)
    assert counts(source) == {"kivi-mechanism": 1}
    assert second.status == "completed"

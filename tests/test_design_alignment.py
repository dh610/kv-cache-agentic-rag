from copy import deepcopy

import pytest

from graph.main_graph import RESULT_KEYS, MainState, build_main_graph
from graph.node_graph import RAGSubState, build_node_graph
from rag.evaluation import EvalCase, metrics, passed, resolve_gold, select_model, validate_dataset
from rag.interface import FixedEvidence
from runtime.models import MockBackend
from runtime.reporting import assemble_report, collect_gaps, validate_report
from runtime.runner import load_input
from runtime.settings import load_settings
from schemas.contracts import NODES, QueryPlan, SufficiencyResult


def invoke(backend=None, source=None, mode="mock", data=None):
    data = data or load_input("tech")
    graph = build_node_graph(
        "tech", data, mode, load_settings(), backend or MockBackend(), source or FixedEvidence(data)
    )
    return graph.invoke({}, config={"recursion_limit": 80})


def test_design_state_keys_and_eight_stage_graph():
    assert {
        "target_techs",
        "domain",
        "limits",
        "sources",
        "trl_result",
        "gaps",
        "supplement_round",
        "report_path",
    } <= MainState.__annotations__.keys()
    assert {
        "role",
        "questions",
        "current_query",
        "search_results",
        "is_sufficient",
        "draft",
        "verdict",
        "search_count",
        "fix_count",
        "output",
    } <= RAGSubState.__annotations__.keys()
    data = load_input("tech")
    graph = build_node_graph(
        "tech", data, "mock", load_settings(), MockBackend(), FixedEvidence(data)
    )
    assert len(set(graph.get_graph().nodes) - {"__start__", "__end__"}) == 8


def test_expression_fix_runs_once_and_is_reverified():
    class Repair(MockBackend):
        def __init__(self):
            self.judges = 0

        def judge(self, result, evidence):
            out = super().judge(result, evidence)
            self.judges += 1
            if self.judges == 1:
                out.checks[0].label = "misstated"
            return out

    backend = Repair()
    state = invoke(backend)
    assert state["fix_count"] == 1 and backend.judges == 2
    assert state["verdict"] == "통과" and state["output"].status == "completed"


def test_permanent_expression_error_does_not_loop_or_pass():
    class Bad(MockBackend):
        def judge(self, result, evidence):
            out = super().judge(result, evidence)
            out.checks[0].label = "misstated"
            return out

    state = invoke(Bad())
    assert state["fix_count"] == 1
    assert state["output"].status == "needs_revision"
    rejected = {check.claim_id for check in state["output"].checks if check.label != "supported"}
    assert rejected.isdisjoint({claim.id for claim in state["output"].result.claims})


@pytest.mark.parametrize("phase", ["plan", "sufficiency"])
def test_malformed_pre_generation_output_fails_closed(phase):
    class Bad(MockBackend):
        def plan(self, data, feedback):
            return QueryPlan(queries=[]) if phase == "plan" else super().plan(data, feedback)

        def sufficiency(self, data, evidence):
            return SufficiencyResult(items=[])

    state = invoke(Bad())
    assert state["rendered_system"] and state["rendered_user"]
    run = state["output"]
    assert run.status == "failed" and not run.result.claims


def test_live_search_records_both_intents_and_rewritten_queries():
    class Source:
        retryable = True

        def __init__(self):
            self.calls = []

        def search(self, q, attempt):
            self.calls.append((q.text, attempt))
            return [e for e in load_input("tech").evidence if e.technology == q.technology]

    source = Source()
    state = invoke(source=source, mode="rag")
    assert len(source.calls) == 3 * len(
        load_input("tech").questions
    )  # every question retains a bounded search budget
    for q in load_input("tech").questions:
        records = [r for r in state["searches"] if r.question_id == q.id]
        assert [r.intent for r in records] == ["positive", "critical", "followup"]
        assert "limitations" in records[1].query
    assert state["output"].status == "needs_revision"


def test_unsupported_verdict_researches_but_never_exceeds_budget():
    class Unsupported(MockBackend):
        def judge(self, result, evidence):
            out = super().judge(result, evidence)
            for c in out.checks:
                c.label = "unsupported"
            return out

    class Source:
        retryable = True

        def search(self, q, attempt):
            return load_input("tech").evidence

    state = invoke(Unsupported(), Source(), "rag")
    assert state["search_count"] == {q.id: 3 for q in load_input("tech").questions}
    assert state["verdict"] == "추가 근거 필요" and state["output"].status == "needs_revision"


def test_gaps_are_code_rules_and_report_gate_rejects_mock():
    inputs = {n: load_input(n, "acceptance") for n in NODES}
    state = build_main_graph(inputs, "mock", load_settings(), MockBackend()).invoke({})
    assert state["supplement_round"] == 0
    assert any(g.role == "domain" and "quality" in g.criterion for g in state["gaps"])
    assert not state["report_check"]["ready"]
    text = assemble_report(state, RESULT_KEYS, "mock")
    assert text.startswith("# SUMMARY") and "# REFERENCE" in text
    assert "| 항목 | KIVI | ITME |" in text
    assert all(t in state["trl_result"] for t in ("KIVI", "ITME"))
    state["sources"] = []
    assert any(
        "인용 출처 누락" in p
        for p in validate_report(state, RESULT_KEYS, text, "fixture")["problems"]
    )


def test_no_false_gap_just_because_opinions_differ():
    data = load_input("tech")
    run = invoke()["output"]
    for a in run.result.assessments:
        a.judgment = "원리 확인"
    for q in data.questions:
        for intent in ("positive", "critical"):
            record = deepcopy(run.searches[0])
            record.question_id = q.id
            record.intent = intent
            run.searches.append(record)
    run.result.summary = "관점 간 의견은 다를 수 있다"
    assert collect_gaps({"tech": run}, load_settings()) == []


def valid_cases():
    return [
        EvalCase(
            id=f"{tech}-{i}",
            technology=tech,
            question=f"{tech} KV 2bit 메모리 실험 {i} 결과는?",
            english_keywords="KV memory",
            document_id=tech.lower(),
            page=1,
            answer_quote="verified sentence",
            verified=True,
            acronym_or_number=True,
        )
        for tech in ("KIVI", "ITME")
        for i in range(15)
    ]


def test_eval_contract_rejects_unverified_or_non_korean():
    settings = load_settings()
    cases = valid_cases()
    validate_dataset(cases, settings)
    cases[0].verified = False
    with pytest.raises(ValueError, match="Human verification"):
        validate_dataset(cases, settings)
    cases[0].verified = True
    cases[0].question = "English only"
    with pytest.raises(ValueError, match="Korean"):
        validate_dataset(cases, settings)


def test_metrics_misses_duplicates_thresholds_and_tie_rule():
    score = metrics(
        {"a": ["x", "x", "a"], "b": ["z"], "c": ["c"]}, {"a": {"a"}, "b": {"b"}, "c": {"c"}}
    )
    assert score["hit@1"] == pytest.approx(1 / 3)
    assert score["hit@5"] == pytest.approx(2 / 3)
    assert score["mrr"] == pytest.approx(0.5)
    assert not passed(score, load_settings().evaluation)
    results = [
        {"model": "a", "parameters": 100, "metrics": {"mrr": 0.75, "hit@1": 0.5}},
        {"model": "b", "parameters": 90, "metrics": {"mrr": 0.71, "hit@1": 0.6}},
        {"model": "c", "parameters": 1, "metrics": {"mrr": 0.60, "hit@1": 1}},
    ]
    assert select_model(results, load_settings().evaluation)["model"] == "b"


def test_gold_is_resolved_from_quote_and_current_chunk_ids():
    case = valid_cases()[0]
    e = load_input("tech").evidence[0]
    e.text = "prefix verified sentence suffix"
    e.id = "new-chunk-id"
    assert resolve_gold([case], [e]) == {case.id: {"new-chunk-id"}}
    e.text = "not the evidence"
    with pytest.raises(ValueError, match="quote not found"):
        resolve_gold([case], [e])


def test_tech_trl_without_verified_claim_is_rejected():
    from schemas.contracts import TRLEstimate

    class Inflated(MockBackend):
        def generate(self, *args):
            result = super().generate(*args)
            result.trl_estimates = [
                TRLEstimate(
                    technology="KIVI", level=9, rationale="unsupported", evidence_ids=["invented"]
                )
            ]
            return result

    run = invoke(Inflated())["output"]
    assert run.status == "needs_revision"
    assert run.result.trl_estimates[0].level is None


def test_all_remediation_stages_execute_in_order_without_real_models(tmp_path, monkeypatch):
    import json

    import app.evaluate_retrieval as cli

    calls = []

    def fake(settings, cases, model_name, **kwargs):
        calls.append((settings.retrieval.chunk_size, kwargs.copy()))
        return {
            "model": model_name,
            "parameters": 100,
            "settings": settings.model_dump(),
            "sparse": kwargs.get("sparse", False),
            "metrics": {"mrr": 0.2, "hit@1": 0.1, "hit@5": 0.3},
        }

    monkeypatch.setattr(cli, "evaluate_dense", fake)
    path = tmp_path / "qa.json"
    path.write_text(json.dumps([c.model_dump() for c in valid_cases()]))
    output = tmp_path / "result.json"
    assert cli.main(["--dataset", str(path), "--run", "--remediate", "--output", str(output)]) == 2
    result = json.loads(output.read_text())
    assert [e["stage"] for e in result["experiments"]] == ["baseline"] * 3 + ["chunking"] * 2 + [
        "bilingual",
        "dense_sparse",
        "rerank",
    ]
    assert not result["accepted"] and len(calls) == 8


def test_partial_model_comparison_cannot_select_a_winner(tmp_path, monkeypatch):
    import json

    import app.evaluate_retrieval as cli

    def fake(settings, cases, model_name, **kwargs):
        if "gte" in model_name:
            raise ValueError("requires model review")
        return {
            "model": model_name,
            "parameters": 100,
            "metrics": {"mrr": 1, "hit@1": 1, "hit@5": 1},
        }

    monkeypatch.setattr(cli, "evaluate_dense", fake)
    path = tmp_path / "qa.json"
    path.write_text(json.dumps([c.model_dump() for c in valid_cases()]))
    output = tmp_path / "result.json"
    assert cli.main(["--dataset", str(path), "--run", "--output", str(output)]) == 2
    result = json.loads(output.read_text())
    assert not result["accepted"] and "selection" not in result


def test_reviewed_web_annotations_do_not_guess_unlisted_sources(tmp_path):
    from rag.annotations import annotate

    e = load_input("tech").evidence[0]
    path = tmp_path / "annotations.yaml"
    path.write_text(f"""KIVI:
  {e.url}:
    affiliation: first_party
    affiliation_reason: 원 저자 자료 확인
    stance: mixed
    reviewed_by: 테스트 검토자
""")
    assert annotate(e, path).affiliation == "first_party"
    assert annotate(e, path).stance == "mixed"
    assert annotate(e.model_copy(update={"technology": "ITME"}), path).affiliation == "unknown"


def test_saved_prompt_hash_matches_actual_generated_messages():
    import hashlib

    state = invoke()
    digest = hashlib.sha256(
        (state["rendered_system"] + "\n" + state["rendered_user"]).encode()
    ).hexdigest()
    assert digest == state["output"].prompt_hash
    assert "근거 충분성 검사" in state["rendered_user"]

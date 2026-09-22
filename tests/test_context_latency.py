import json

import pytest
from pydantic import ValidationError

from runtime.context import evidence_payload
from runtime.models import MockBackend, OpenAIBackend, result_schema
from runtime.runner import load_input
from schemas.contracts import JudgeResult, NodeResult
from tests.test_targeted_retries import Source, execute


def test_both_intents_share_one_plan_and_one_sufficiency_call():
    run, backend = execute(Source())
    assert len(backend.planned) == len(backend.reviewed) == 1
    assert len(run.searches) == 4
    assert run.status == "completed"


def test_criterion_schema_rejects_valid_grade_from_wrong_criterion():
    data = load_input("tech")
    draft = MockBackend().generate("tech", data, data.evidence, "", "").model_dump()
    schema = result_schema("tech")
    assert NodeResult.model_validate(schema.model_validate(draft).model_dump())
    item = next(a for a in draft["assessments"] if a["criterion"] == "mechanism")
    item["judgment"] = "제약 확인"
    with pytest.raises(ValidationError):
        schema.model_validate(draft)
    item["judgment"] = "원리 확인"
    assert schema.model_validate(draft)


@pytest.mark.parametrize("node", ["tech", "market", "stakeholder", "domain", "synthesis", "report"])
def test_role_schema_is_compatible_with_provider_tool_conversion(node):
    from langchain_core.utils.function_calling import convert_to_openai_function

    schema = convert_to_openai_function(result_schema(node), strict=True)
    assert schema["strict"]
    assert schema["parameters"]["properties"]["assessments"]["items"]["anyOf"]


def test_web_excerpts_retain_late_relevant_conditions_and_counterevidence():
    data = load_input("tech")
    question = data.questions[0].model_copy(update={"text": "bandwidth performance experiment"})
    original = data.evidence[0].model_copy(
        update={
            "source_type": "web",
            "text": "unrelated navigation " * 300
            + "bandwidth performance experiment: measured on GPU A with batch 8. " * 10
            + "unrelated navigation " * 100
            + "However, accuracy regressed under long-context workloads. " * 10,
        }
    )
    before = original.model_dump()
    row = evidence_payload([original], [question])[0]
    assert "GPU A with batch 8" in row["text"]
    assert "accuracy regressed" in row["text"]
    assert row["id"] == original.id
    assert len(row["text"]) < len(original.text)
    assert original.model_dump() == before
    assert all(original.text[start:end] in row["text"] for start, end in row["excerpt_ranges"])
    assert evidence_payload([original], full_text=True)[0]["text"] == original.text


def test_paper_chunks_are_never_excerpted():
    data = load_input("tech")
    paper = data.evidence[0].model_copy(update={"source_type": "paper", "text": "x" * 20000})
    assert evidence_payload([paper], data.questions)[0]["text"] == paper.text


def test_citation_judge_receives_all_cited_originals_without_unused_pages():
    data = load_input("tech")
    draft = MockBackend().generate("tech", data, data.evidence, "", "")
    original = data.evidence[0].model_copy(update={"source_type": "web", "text": "x" * 12000})
    extra = original.model_copy(update={"id": "unrelated-page"})
    captured = []

    class Evaluator:
        def invoke(self, messages):
            captured.append(json.loads(messages[1][1]))
            return JudgeResult(checks=[])

    backend = OpenAIBackend.__new__(OpenAIBackend)
    backend.evaluator = Evaluator()
    backend.judge(draft, [original, extra])
    assert [e["id"] for e in captured[0]["evidence"]] == [original.id]
    assert captured[0]["evidence"][0]["text"] == original.text


def test_isolated_claim_failure_preserves_other_technology():
    from tests.test_targeted_retries import Backend

    class RejectITME(Backend):
        def judge(self, result, evidence):
            checks = super().judge(result, evidence)
            for check in checks.checks:
                if "itme" in check.claim_id:
                    check.label = "unsupported"
            return checks

    run, _ = execute(Source(), RejectITME())
    assert run.status == "needs_revision"
    assert all(c.technology == "KIVI" for c in run.result.claims)
    assert run.result.claims
    assert next(a for a in run.result.assessments if a.technology == "KIVI").judgment == "원리 확인"
    assert next(a for a in run.result.assessments if a.technology == "ITME").judgment == "확인 불가"
    assert run.result.unverified


def test_cited_claim_schema_rejects_empty_evidence():
    data = load_input("tech")
    draft = MockBackend().generate("tech", data, data.evidence, "", "").model_dump()
    draft["claims"][0]["evidence_ids"] = []
    with pytest.raises(ValidationError):
        result_schema("tech").model_validate(draft)


def test_scoped_generation_merges_both_technologies_and_namespaces_claim_ids():
    from runtime.prompts import render

    data = load_input("tech")
    packets = []
    for tech in data.target_techs.values():
        scoped = data.model_copy(deep=True)
        scoped.questions = [q for q in data.questions if q.technology == tech]
        evidence = [e for e in data.evidence if e.technology == tech]
        system, user, _ = render("tech", scoped, evidence)
        packets.append((scoped, evidence, system, user))
    backend = OpenAIBackend.__new__(OpenAIBackend)
    backend.generate = MockBackend().generate
    result = backend.generate_scoped("tech", packets)
    assert len(result.assessments) == len(data.questions)
    assert {c.technology for c in result.claims} == {"KIVI", "ITME"}
    assert len({c.id for c in result.claims}) == len(result.claims)


@pytest.mark.parametrize("node", ["synthesis", "report"])
def test_upstream_only_nodes_do_not_replan_or_rejudge_retrieval(node):
    from graph.node_graph import build_node_graph
    from rag.interface import FixedEvidence
    from runtime.settings import load_settings

    class NoSearchModels(MockBackend):
        def plan(self, *args):
            pytest.fail("no new search plan needed")

        def sufficiency(self, *args):
            pytest.fail("upstream-only evidence does not need another retrieval judge")

    data = load_input(node)
    result = build_node_graph(
        node, data, "fixture", load_settings(), NoSearchModels(), FixedEvidence(data)
    ).invoke({})["output"]
    assert result.result.assessments


def test_unscoped_verification_failure_also_removes_foreign_claims():
    from tests.test_targeted_retries import Backend

    class BrokenJudge(Backend):
        def generate(self, *args):
            result = super().generate(*args)
            result.claims.append(
                result.claims[0].model_copy(update={"id": "foreign", "technology": "unknown-tech"})
            )
            return result

        def judge(self, *args):
            return JudgeResult(checks=[])

    run, _ = execute(Source(), BrokenJudge())
    assert run.status == "needs_revision"
    assert not run.result.claims
    assert run.result.unverified


def test_first_pass_pipeline_assembles_report_without_report_llm():
    from graph.main_graph import build_main_graph
    from runtime.settings import load_settings
    from schemas.contracts import NODES

    class NoReportGenerator(MockBackend):
        def generate(self, node, *args):
            assert node != "report"
            return super().generate(node, *args)

    settings = load_settings()
    settings.limits.search = 2
    settings.limits.fix = settings.limits.supplement = 0
    inputs = {node: load_input(node, "acceptance") for node in NODES}
    state = build_main_graph(inputs, "mock", settings, NoReportGenerator(), first_pass=True).invoke(
        {}, config={"recursion_limit": 80}
    )
    assert state["report"].model == "validated-result-assembler"
    assert len(state["report"].result.assessments) == len(inputs["report"].questions)
    assert state["report"].result.claims
    assert state["supplement_round"] == 0


def test_report_assembler_refuses_missing_research_nodes():
    from runtime.report_draft import assemble_report_node

    with pytest.raises(ValueError, match="All four"):
        assemble_report_node(load_input("report"), {}, "live")

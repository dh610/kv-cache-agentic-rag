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

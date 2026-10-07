import json

import pytest

from app.benchmark_fewshot import experimental_render, frozen_input, summarize
from graph.node_graph import build_node_graph
from rag.interface import FixedEvidence
from runtime.models import MockBackend
from runtime.runner import load_input
from runtime.settings import load_settings


def test_baseline_and_fewshot_preserve_same_user_payload():
    data = load_input("domain")
    baseline = experimental_render("baseline")("domain", data, data.evidence)
    fewshot = experimental_render("fewshot")("domain", data, data.evidence)
    assert baseline[1] == fewshot[1]
    assert fewshot[0].startswith(baseline[0])
    assert baseline[2] != fewshot[2]
    assert "가상" in fewshot[0]


def test_freezing_rejects_demo_evidence_and_preserves_questions():
    settings = load_settings()
    state = {}
    for role in ("tech", "domain"):
        data = load_input(role, "acceptance")
        run = build_node_graph(
            role, data, "mock", settings, MockBackend(), FixedEvidence(data)
        ).invoke({})["output"]
        state[f"{role}_result"] = run.model_dump()
    with pytest.raises(ValueError, match="real evidence"):
        frozen_input(state, "domain")
    for item in state["domain_result"]["evidence"]:
        item["source_type"] = "paper"
    before = json.dumps(state)
    data = frozen_input(state, "domain")
    assert len(data.questions) == 10
    assert len(data.evidence) == len(state["domain_result"]["evidence"])
    assert json.dumps(state) == before


def test_summary_does_not_count_abstention_as_known_answer():
    data = load_input("domain", "missing-evidence")
    final = build_node_graph(
        "domain", data, "mock", load_settings(), MockBackend(), FixedEvidence(data)
    ).invoke({})
    probe = type("Probe", (), {"events": []})()
    metrics = summarize(final, probe)
    assert metrics["known_assessments"] == 0
    assert metrics["status"] != "completed"

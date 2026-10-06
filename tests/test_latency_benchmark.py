from dataclasses import asdict
from threading import BoundedSemaphore
from types import SimpleNamespace
from uuid import uuid4

import pytest

from runtime.latency_benchmark import (
    VARIANTS,
    BenchmarkBackend,
    Recorder,
    configure,
)
from runtime.models import MockBackend, OpenAIBackend
from runtime.runner import load_input
from runtime.settings import load_settings
from schemas.contracts import JudgeResult


def test_each_contrast_changes_one_factor_without_mutating_settings():
    settings = load_settings()
    before = settings.model_dump()
    baseline = asdict(VARIANTS["baseline"])
    fields = {
        "A_serial_judge": "judge_workers",
        "B_followup_search": "search",
        "C_answer_fix": "fix",
        "D_supplement": "supplement",
        "E_llm_report": "assemble_report",
    }
    for name, field in fields.items():
        configured, variant = configure(settings, name)
        assert {k for k, v in asdict(variant).items() if v != baseline[k]} == {field}
        assert configured.limits.search == variant.search
        assert configured.limits.fix == variant.fix
        assert configured.limits.supplement == variant.supplement
    assert settings.model_dump() == before


def test_parallel_judge_preserves_order_labels_and_cited_evidence(monkeypatch):
    data = load_input("tech", "acceptance")
    result = MockBackend().generate("tech", data, data.evidence, "", "")
    expected = MockBackend().judge(result, data.evidence)
    calls = []

    def judge(self, selected, evidence):
        calls.extend(c.id for c in selected.claims)
        return MockBackend().judge(selected, evidence)

    monkeypatch.setattr(OpenAIBackend, "judge", judge)
    backend = object.__new__(BenchmarkBackend)
    backend.workers = 3
    backend.slots = BoundedSemaphore(3)
    actual = backend.judge(result, data.evidence)
    assert actual == expected
    assert sorted(calls) == sorted(c.id for c in result.claims)
    assert backend.judge(result.model_copy(update={"claims": []}), data.evidence) == JudgeResult(
        checks=[]
    )


def test_parallel_judge_failure_is_not_forged_as_supported(monkeypatch):
    data = load_input("tech")
    result = MockBackend().generate("tech", data, data.evidence, "", "")

    def fail(*args):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(OpenAIBackend, "judge", fail)
    backend = object.__new__(BenchmarkBackend)
    backend.workers = 2
    backend.slots = BoundedSemaphore(2)
    with pytest.raises(RuntimeError):
        backend.judge(result, data.evidence)


def test_recorder_stops_before_extra_llm_request_and_excludes_prompt(tmp_path):
    import json

    recorder = Recorder(tmp_path / "events.jsonl", max_llm_calls=1)
    run = uuid4()
    recorder.on_chat_model_start({}, [[SimpleNamespace(content="private prompt")]], run_id=run)
    recorder.on_llm_end(
        SimpleNamespace(llm_output={"token_usage": {"total_tokens": 8}}), run_id=run
    )
    with pytest.raises(RuntimeError, match="budget"):
        recorder.on_chat_model_start({}, [[SimpleNamespace(content="second")]], run_id=uuid4())
    text = recorder.path.read_text()
    assert "private prompt" not in text
    assert "second" not in text.replace("at_seconds", "").replace("seconds", "")
    rows = [json.loads(line) for line in text.splitlines()]
    assert sum(r.get("kind") == "llm" and r.get("phase") == "start" for r in rows) == 1
    assert rows[-1]["kind"] == "budget_exhausted"


def test_code_report_does_not_claim_refinement_was_disabled(tmp_path):
    from graph.main_graph import build_main_graph
    from schemas.contracts import NODES

    graph = build_main_graph(
        {n: load_input(n, "acceptance") for n in NODES},
        "mock",
        load_settings(),
        MockBackend(),
        code_report=True,
    )
    graph.invoke({}, config={"configurable": {"output_dir": str(tmp_path)}})
    report = (tmp_path / "report.md").read_text()
    assert "추가 재검색·전체 수정·종합 뒤 보완은 생략했습니다" not in report


def test_live_suite_never_starts_when_api_health_fails(monkeypatch, tmp_path):
    import app.benchmark_latency as cli

    monkeypatch.setattr(cli, "health", lambda *args: False)

    def forbidden(*args, **kwargs):
        raise AssertionError("No process may start with failed preflight")

    monkeypatch.setattr(cli.subprocess, "Popen", forbidden)
    assert cli.main(["suite", "--mode", "live", "--output", str(tmp_path)]) == 2
    assert not (tmp_path / "plan.json").exists()

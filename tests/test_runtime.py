import json
import subprocess

import pytest
from jinja2 import UndefinedError

import runtime.prompts as prompts
import runtime.runner as runner
from graph.node_graph import build_node_graph
from rag.interface import FixedEvidence, make_source
from runtime.models import MockBackend, OpenAIBackend
from runtime.settings import ROOT, load_settings


def test_mock_never_enables_tracing_even_when_user_enabled_it(monkeypatch, tmp_path):
    settings = load_settings()
    data = runner.load_input("tech")
    graph = build_node_graph("tech", data, "mock", settings, MockBackend(), FixedEvidence(data))
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    final, output = runner.execute(graph, "tech", "mock", {})
    assert final["output"].status == "completed"
    assert json.loads((output / "run.json").read_text())["trace_enabled"] is False


def test_trace_requires_individual_project_and_key(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    with pytest.raises(ValueError, match="LANGSMITH_API_KEY"):
        runner.execute(None, "tech", "fixture", {})
    monkeypatch.setenv("LANGSMITH_API_KEY", "unit-test-placeholder")
    monkeypatch.setenv("LANGSMITH_PROJECT", "kv-rag-yourname-dev")
    with pytest.raises(ValueError, match="LANGSMITH_PROJECT"):
        runner.execute(None, "tech", "fixture", {})


def test_real_backend_requires_llm_key():
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAIBackend(load_settings())


def test_provider_structured_output_can_be_constructed_without_network(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-placeholder")
    backend = OpenAIBackend(load_settings())
    assert backend.generator is not None and backend.evaluator is not None


def test_prompt_typo_fails_loudly(monkeypatch, tmp_path):
    import shutil

    shutil.copytree(ROOT / "rubrics", tmp_path / "rubrics")
    shutil.copytree(ROOT / "prompts", tmp_path / "prompts")
    (tmp_path / "prompts/tech/user.j2").write_text("{{ absent_variable }}")
    monkeypatch.setattr(prompts, "ROOT", tmp_path)
    with pytest.raises(UndefinedError):
        prompts.render("tech", runner.load_input("tech"), [])


@pytest.mark.parametrize(
    "node,mode", [("tech", "web"), ("stakeholder", "rag"), ("synthesis", "rag"), ("report", "web")]
)
def test_source_policy_is_enforced(node, mode):
    with pytest.raises(ValueError):
        make_source(mode, node, runner.load_input(node), load_settings())


def test_gitignore_hides_personal_assets_but_keeps_shared_inputs():
    ignored = [
        ".env",
        ".env.test",
        ".venv/test",
        "data/papers/kivi.pdf",
        ".cache/index.json",
        "outputs/local/run/result.json",
        "config.local.yaml",
        "private.key",
    ]
    shared = [
        ".env.example",
        "uv.lock",
        "tests/fixtures/tech/basic.json",
        "prompts/tech/system.j2",
        "data/documents.yaml",
        "outputs/.gitkeep",
        "data/papers/.gitkeep",
    ]
    for path in ignored + shared:
        result = subprocess.run(["git", "check-ignore", "--no-index", "-q", path], cwd=ROOT)
        assert result.returncode == (0 if path in ignored else 1), path


def test_public_exit_codes(tmp_path):
    (tmp_path / "run.json").write_text('{"mode": "mock"}')
    assert runner.show_run(tmp_path, "completed") == 0
    assert runner.show_run(tmp_path, "needs_revision") == 2
    assert runner.show_run(tmp_path, "failed") == 1


def test_langsmith_url_uses_run_object_as_required_by_sdk(monkeypatch, tmp_path):
    calls = {}

    class ClientStub:
        def read_run(self, run_id):
            calls["run_id"] = run_id
            return {"id": run_id}

        def get_run_url(self, *, run, project_name):
            assert run["id"] == calls["run_id"]
            assert project_name == "kv-rag-unit-test"
            return "https://example.invalid/trace"

    class GraphStub:
        def invoke(self, state, config):
            assert config["run_id"]
            return {"ok": True}

    import langchain_core.tracers.langchain as tracing

    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "Client", ClientStub)
    monkeypatch.setattr(tracing, "wait_for_all_tracers", lambda: None)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "unit-test-placeholder")
    monkeypatch.setenv("LANGSMITH_PROJECT", "kv-rag-unit-test")
    _, output = runner.execute(GraphStub(), "tech", "fixture", {})
    assert (
        json.loads((output / "run.json").read_text())["trace_url"]
        == "https://example.invalid/trace"
    )


def test_web_source_rejects_snippets_and_keeps_raw_content(monkeypatch):
    import httpx

    from rag.web import WebSource

    monkeypatch.setenv("TAVILY_API_KEY", "unit-test-placeholder")

    def post(url, *, json, timeout):
        assert json["include_raw_content"] is True
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "results": [
                    {"url": "https://example.invalid/snippet", "content": "Not a full page"},
                    {
                        "url": "https://example.invalid/raw",
                        "title": "Source",
                        "raw_content": "Full evidence text on KV cache quantization for LLM inference.",
                    },
                ]
            },
        )

    monkeypatch.setattr(httpx, "post", post)
    data = runner.load_input("market")
    source = WebSource(load_settings())
    kivi_question = next(q for q in data.questions if q.technology == "KIVI")
    itme_question = next(q for q in data.questions if q.technology == "ITME")
    first = source.search(kivi_question, 1)
    second = source.search(itme_question, 1)
    assert len(first) == len(second) == 1
    assert first[0].text == "Full evidence text on KV cache quantization for LLM inference."
    assert first[0].id != second[0].id  # Same page can contextualize different technologies.
    assert first[0].retrieved_at

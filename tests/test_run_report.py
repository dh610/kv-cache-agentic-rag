from contextlib import contextmanager
from io import BytesIO

import pytest
from pypdf import PdfWriter

from app import run_report
from runtime.settings import ROOT


def pdf_bytes(pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(100, 100)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def response(monkeypatch, content):
    class Reply:
        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield content

    @contextmanager
    def stream(*args, **kwargs):
        yield Reply()

    monkeypatch.setattr(run_report.httpx, "stream", stream)


def test_missing_keys_stop_before_resource_preparation(monkeypatch):
    monkeypatch.setattr(run_report, "load_settings", lambda: None)
    monkeypatch.setattr(run_report, "prepare_papers", lambda *a: pytest.fail("must stop first"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY.*TAVILY_API_KEY"):
        run_report.main([])


def test_opt_in_tracing_requires_personal_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unit-only")
    monkeypatch.setenv("TAVILY_API_KEY", "unit-only")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    with pytest.raises(ValueError, match="LANGSMITH_API_KEY"):
        run_report.check_keys()
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    run_report.check_keys()


@pytest.mark.parametrize("content", [b"<html>rate limit</html>", b"%PDF-1.4\nbroken download"])
def test_invalid_download_is_rejected_without_leaving_partial_pdf(tmp_path, monkeypatch, content):
    response(monkeypatch, content)
    with pytest.raises(ValueError, match="PDF"):
        run_report.download_pdf("https://example.invalid/paper", tmp_path / "paper.pdf", 1, 1)
    assert list(tmp_path.iterdir()) == []


def test_wrong_version_is_not_installed(tmp_path, monkeypatch):
    response(monkeypatch, pdf_bytes(2))
    with pytest.raises(ValueError, match="판본"):
        run_report.download_pdf("https://example.invalid/paper", tmp_path / "paper.pdf", 1, 1)
    assert list(tmp_path.iterdir()) == []


def test_valid_download_is_atomic_and_does_not_overwrite_existing_pdf(tmp_path, monkeypatch):
    content = pdf_bytes()
    response(monkeypatch, content)
    target = tmp_path / "paper.pdf"
    run_report.download_pdf("https://example.invalid/paper", target, 1, 1)
    assert target.read_bytes() == content
    original = content + b"\n% user copy\n"
    target.write_bytes(original)
    run_report.download_pdf("https://example.invalid/paper", target, 1, 1)
    assert target.read_bytes() == original
    assert list(tmp_path.iterdir()) == [target]


def test_mock_needs_neither_keys_nor_papers_and_preserves_exit_status(monkeypatch):
    import app.run_pipeline

    monkeypatch.setattr(run_report, "load_settings", lambda: None)
    monkeypatch.setattr(run_report, "check_keys", lambda: pytest.fail("mock needs no keys"))
    monkeypatch.setattr(
        run_report, "prepare_papers", lambda *a: pytest.fail("mock needs no papers")
    )

    def pipeline(argv):
        assert argv == ["--mode", "mock"]
        return 2

    monkeypatch.setattr(app.run_pipeline, "main", pipeline)
    assert run_report.main(["--mock"]) == 2


def test_current_index_prepare_only_skips_embedding_and_pipeline(monkeypatch):
    loaded = []
    monkeypatch.setattr(run_report, "load_settings", lambda: None)
    monkeypatch.setattr(run_report, "load_catalog", lambda: None)
    monkeypatch.setattr(run_report, "prepare_papers", lambda *a: None)
    monkeypatch.setattr(run_report, "check_keys", lambda: pytest.fail("no paid APIs"))
    monkeypatch.setattr(
        run_report, "read_chunks", lambda *a: ([], {"full_pdf_pages": 1, "indexed_body_pages": 1})
    )
    monkeypatch.setattr(run_report, "index_current", lambda *a: True)
    monkeypatch.setattr(run_report, "build_index", lambda *a: pytest.fail("reuse valid cache"))
    monkeypatch.setattr(run_report, "load_encoder", lambda *a: loaded.append(True))
    assert run_report.main(["--prepare-only"]) == 0
    assert loaded == [True]  # ensures model availability even if only an index was copied


def test_shell_mock_does_not_request_rag_dependencies(tmp_path, monkeypatch):
    import subprocess

    tool = tmp_path / "uv"
    log = tmp_path / "calls"
    tool.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$UV_TEST_LOG"\n')
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv("UV_TEST_LOG", str(log))
    subprocess.run(["sh", str(ROOT / "run-report.sh"), "--mock"], cwd=tmp_path, check=True)
    assert log.read_text().splitlines() == [
        "sync --frozen --python 3.11 --inexact",
        "run --no-sync python -m app.run_report --mock",
    ]


def test_invalid_shell_option_does_not_install_anything(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    result = subprocess.run(
        ["sh", str(ROOT / "run-report.sh"), "--invalid"], cwd=tmp_path, capture_output=True
    )
    assert result.returncode == 1 and b"Unknown option" in result.stderr

"""저장된 실행에서 보고서만 다시 조판한다 (모델 호출 없음)."""

import json
from pathlib import Path

from app.rerender_report import main
from app.run_pipeline import main as run_pipeline


def test_rerender_matches_the_original_without_calling_models(tmp_path, capsys):
    assert run_pipeline(["--mode", "mock"]) == 0
    import os

    runs = Path(os.environ["RUN_OUTPUT_DIR"])
    folder = max(runs.glob("*-pipeline-*"), key=lambda p: p.stat().st_mtime)
    original = (folder / "report.md").read_text(encoding="utf-8")
    capsys.readouterr()
    out = tmp_path / "again"
    assert main(["--from", str(folder), "--out", str(out)]) == 2  # mock 은 제출 보고서가 아니다
    assert (out / "report.md").read_text(encoding="utf-8") == original
    assert (out / "report.pdf").exists()
    printed = capsys.readouterr().out
    assert "모델 호출 없이 재조판" in printed
    assert json.loads((folder / "run.json").read_text())["mode"] == "mock"

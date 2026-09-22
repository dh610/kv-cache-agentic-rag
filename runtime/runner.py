from __future__ import annotations

import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from langsmith import Client, tracing_context

from runtime.settings import ROOT, require_key
from schemas.contracts import NODES, NodeInput


def load_input(node: str, case: str = "basic", path: Path | None = None) -> NodeInput:
    if node not in NODES or case not in ("basic", "missing-evidence", "acceptance"):
        raise ValueError("Unknown node/case; use --input for your own JSON fixture")
    target = path or ROOT / "tests/fixtures" / node / f"{case}.json"
    return NodeInput.model_validate_json(target.read_text(encoding="utf-8"))


def git_revision() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "uncommitted"


def git_dirty() -> bool:
    try:
        return bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
            ).strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return True


def execute(graph, label: str, mode: str, metadata: dict) -> tuple[dict, Path]:
    enabled = mode != "mock" and os.getenv("LANGSMITH_TRACING", "false").lower() == "true"
    project = os.getenv("LANGSMITH_PROJECT", "").strip()
    if enabled:
        require_key("LANGSMITH_API_KEY")
        if not project or "yourname" in project:
            raise ValueError("Set your own LANGSMITH_PROJECT, e.g. kv-rag-donghyeon-dev")
    run_id = uuid.uuid4()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / "outputs/local" / f"{stamp}-{label}-{run_id.hex[:8]}"
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "run_id": str(run_id),
        "mode": mode,
        "label": label,
        "git_revision": git_revision(),
        "git_dirty": git_dirty(),
        "trace_enabled": enabled,
        "trace_url": None,
        "metadata": metadata,
    }
    try:
        with tracing_context(enabled=enabled, project_name=project or None):
            final = graph.invoke(
                {},
                config={
                    "run_id": run_id,
                    "run_name": f"{label}-{mode}",
                    "recursion_limit": 80,
                    "configurable": {"output_dir": str(output)},
                    "tags": [label, mode],
                    "metadata": {**metadata, "git_revision": git_revision()},
                },
            )
        if enabled:
            from langchain_core.tracers.langchain import wait_for_all_tracers

            wait_for_all_tracers()
            try:
                client = Client()
                manifest["trace_url"] = client.get_run_url(
                    run=client.read_run(run_id), project_name=project
                )
            except Exception as exc:
                manifest["trace_warning"] = f"Trace URL unavailable: {type(exc).__name__}"
        return final, output
    finally:
        (output / "run.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def save_json(path: Path, value) -> None:
    def encode(obj):
        if hasattr(obj, "model_dump"):
            return obj.model_dump(mode="json")
        raise TypeError(f"Cannot serialize {type(obj).__name__}")

    path.write_text(
        json.dumps(value, default=encode, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def show_run(output: Path, status: str) -> int:
    meta = json.loads((output / "run.json").read_text())
    print(f"Status: {status}\nResults: {output}")
    if meta["mode"] == "mock":
        print("MOCK: offline wiring check only; no LLM quality evaluation or LangSmith upload.")
    if meta.get("trace_url"):
        print(f"LangSmith: {meta['trace_url']}")
    if meta.get("trace_warning"):
        print(meta["trace_warning"])
    return {"completed": 0, "needs_revision": 2, "failed": 1}[status]

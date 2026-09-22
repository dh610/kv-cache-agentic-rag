from __future__ import annotations

import argparse
from pathlib import Path

from graph.node_graph import build_node_graph
from rag.interface import make_source
from runtime.models import MockBackend, OpenAIBackend
from runtime.prompts import render
from runtime.runner import execute, load_input, save_json, show_run
from runtime.settings import load_settings
from schemas.contracts import NODES


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run one node without running its predecessors")
    parser.add_argument("--node", choices=NODES, required=True)
    parser.add_argument("--mode", choices=("mock", "fixture", "rag", "web"), default="mock")
    parser.add_argument(
        "--case", choices=("basic", "missing-evidence", "acceptance"), default="basic"
    )
    parser.add_argument(
        "--input", type=Path, help="Custom NodeInput JSON; overrides the built-in case"
    )
    args = parser.parse_args(argv)
    settings = load_settings()
    data = load_input(args.node, args.case, args.input)
    source = make_source(args.mode, args.node, data, settings)
    backend = MockBackend() if args.mode == "mock" else OpenAIBackend(settings)
    graph = build_node_graph(args.node, data, args.mode, settings, backend, source)
    _, _, prompt_hash = render(args.node, data, data.evidence)
    final, output = execute(
        graph,
        args.node,
        args.mode,
        {
            "case_id": data.case_id,
            "prompt_input_hash": prompt_hash,
            "generator": settings.models.generator,
            "judge": settings.models.judge,
            "settings": settings.model_dump(),
        },
    )
    run = final["output"]
    save_json(output / "input.json", data)
    save_json(output / "result.json", run)
    # The draft is the Generator's last output before the runtime withheld failed judgments.
    # It is for prompt review only and is never the delivered result.
    save_json(output / "draft.json", final["draft"])
    system, user = final["rendered_system"], final["rendered_user"]
    (output / "system.txt").write_text(system, encoding="utf-8")
    (output / "user.txt").write_text(user, encoding="utf-8")
    return show_run(output, run.status)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, FileNotFoundError, ImportError) as exc:
        print(f"Setup error: {exc}")
        raise SystemExit(1) from None

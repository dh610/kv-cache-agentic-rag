from __future__ import annotations

import argparse

from graph.main_graph import RESULT_KEYS, build_main_graph
from rag.interface import live_sources
from runtime.models import MockBackend, OpenAIBackend
from runtime.runner import execute, load_input, save_json, show_run
from runtime.settings import load_settings
from schemas.contracts import NODES


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the common graph with fixed evidence")
    parser.add_argument("--mode", choices=("mock", "fixture", "live"), default="mock")
    parser.add_argument(
        "--first-pass",
        action="store_true",
        help="Generate a reviewed first draft without follow-up search, fix, or supplement rounds",
    )
    args = parser.parse_args(argv)
    settings = load_settings()
    if args.first_pass:
        settings.limits.search = 2
        settings.limits.fix = 0
        settings.limits.supplement = 0
    inputs = {node: load_input(node, "acceptance") for node in NODES}
    backend = MockBackend() if args.mode == "mock" else OpenAIBackend(settings)
    sources = live_sources(settings) if args.mode == "live" else None
    graph = build_main_graph(
        inputs, args.mode, settings, backend, sources, first_pass=args.first_pass
    )
    final, output = execute(
        graph,
        "pipeline",
        args.mode,
        {"settings": settings.model_dump(), "first_pass": args.first_pass},
    )
    save_json(output / "state.json", final)
    body = [
        f"# Development preview ({args.mode})",
        "",
        f"Status: {final['run_status']}",
        "",
        "개발용 노드 결과입니다. 과제 제출용 최종 평가 보고서가 아닙니다.",
        "",
    ]
    for node, key in RESULT_KEYS.items():
        run = final[key]
        body.extend([f"## {node}", "", run.result.summary, ""])
        body.extend(f"- 확인 필요: {s}" for s in run.result.unverified + run.validation_errors)
        body.append("")
    (output / "preview.md").write_text("\n".join(body), encoding="utf-8")
    print(f"Report PDF: {output / 'report.pdf'}\nReport Markdown: {output / 'report.md'}")
    return show_run(output, final["run_status"])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, FileNotFoundError, ImportError) as exc:
        print(f"Setup error: {exc}")
        raise SystemExit(1) from None

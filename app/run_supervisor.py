"""Supervisor 패턴 실행 스크립트.

    uv run python -m app.run_supervisor --mode mock
    uv run --extra rag python -m app.run_supervisor --mode live

기존 고정 흐름 실행(``app.run_pipeline``)은 그대로 두고, 조정 계층만 바꾼 실행 경로를
따로 둔다. 같은 입력·같은 하위 에이전트로 두 패턴을 비교할 수 있다.
"""

from __future__ import annotations

import argparse
import uuid

from agents.report_view import RESULT_KEYS
from graph.supervisor_graph import build_supervisor_graph
from rag.interface import live_sources
from runtime.models import MockBackend, OpenAIBackend
from runtime.runner import execute, load_input, save_json, show_run
from runtime.settings import load_settings
from schemas.contracts import NODES


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the Supervisor multi-agent graph")
    parser.add_argument("--mode", choices=("mock", "fixture", "live"), default="mock")
    parser.add_argument(
        "--first-pass",
        action="store_true",
        help="Generate a reviewed first draft without follow-up search or fix rounds",
    )
    parser.add_argument(
        "--max-steps", type=int, default=None, help="Override the supervisor step ceiling"
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    if args.first_pass:
        settings.limits.search = 2
        settings.limits.fix = 0
    if args.max_steps:
        settings.supervisor.max_steps = args.max_steps

    inputs = {node: load_input(node, "acceptance") for node in NODES}
    backend = MockBackend() if args.mode == "mock" else OpenAIBackend(settings)
    sources = live_sources(settings) if args.mode == "live" else None
    trace_id = str(uuid.uuid4())
    graph = build_supervisor_graph(
        inputs,
        args.mode,
        settings,
        backend,
        sources,
        first_pass=args.first_pass,
        trace_id=trace_id,
    )
    # recursion_limit 은 LangGraph 의 안전망이고, 실제 종료는 supervisor 의 네 한도가 보장한다.
    final, output = execute(
        graph,
        "supervisor",
        args.mode,
        {
            "pattern": "supervisor",
            "trace_id": trace_id,
            "supervisor": settings.supervisor.model_dump(),
            "first_pass": args.first_pass,
        },
    )
    save_json(output / "state.json", final)

    print(f"Trace ID: {trace_id}")
    print(f"Supervisor steps: {final['step']} / {final['max_steps']}")
    print("Routing decisions:")
    for decision in final.get("decisions", []):
        targets = ", ".join(decision.targets) or "-"
        print(f"  [{decision.step:>2}] {decision.action:<10} → {targets:<28} {decision.reason}")
    verdict = final.get("quality")
    if verdict is not None:
        label = "통과" if verdict.passed else "미달"
        judge = "규칙+Judge" if verdict.judge_available else "규칙만"
        print(f"Report quality ({judge}): {label}")
        for check in verdict.checks:
            print(f"  - {check.criterion:<13} {'OK ' if check.passed else 'NG '} {check.reason}")
    print(f"Report Markdown: {output / 'report.md'}")
    print(f"Report PDF: {output / 'report.pdf'}")
    print(f"Decision log: {output / 'decisions.jsonl'}")
    skipped = [node for node in RESULT_KEYS if node not in final.get("results", {})]
    if skipped:
        print(f"실행되지 않은 역할: {', '.join(skipped)}")
    return show_run(output, final["run_status"])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, FileNotFoundError, ImportError) as exc:
        print(f"Setup error: {exc}")
        raise SystemExit(1) from None

"""One-factor-at-a-time latency experiments with explicit live API preflight.

Run health first; live runs stop on failed generation/search preflight.
All results stay under ignored outputs/. Production configuration is unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from langsmith import Client, tracing_context

from graph.main_graph import build_main_graph
from rag.interface import live_sources
from runtime.latency_benchmark import (
    VARIANTS,
    BenchmarkBackend,
    Recorder,
    TimedSource,
    configure,
    summarize,
)
from runtime.models import MockBackend
from runtime.runner import git_dirty, git_revision, load_input, save_json
from runtime.settings import ROOT, load_settings
from schemas.contracts import NODES


def health(folder, settings):
    import httpx
    from openai import OpenAI

    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for api in ("openai", "tavily", "langsmith"):
        start = time.perf_counter()
        row = {"api": api}
        try:
            if api == "openai":
                client = OpenAI(timeout=20, max_retries=0)
                # Exercise the structured-output endpoint that the project actually uses.
                for model in dict.fromkeys((settings.models.generator, settings.models.judge)):
                    response = client.chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": "Return ok=true."}],
                        max_tokens=30,
                        temperature=0,
                        response_format={
                            "type": "json_schema",
                            "json_schema": {
                                "name": "health",
                                "strict": True,
                                "schema": {
                                    "type": "object",
                                    "properties": {"ok": {"type": "boolean"}},
                                    "required": ["ok"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                    )
                    if json.loads(response.choices[0].message.content) != {"ok": True}:
                        raise ValueError("Unexpected structured health response")
                row["models"] = [settings.models.generator, settings.models.judge]
            elif api == "tavily":
                response = httpx.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": os.environ["TAVILY_API_KEY"],
                        "query": "KIVI KV cache quantization arxiv",
                        "max_results": 1,
                        "include_raw_content": True,
                    },
                    timeout=20,
                )
                response.raise_for_status()
                results = response.json().get("results", [])
                if not results or not any(r.get("raw_content") for r in results):
                    raise ValueError("No usable raw-content search result")
                row["results"] = len(results)
            else:
                client = Client(timeout_ms=20000)
                project = client.read_project(project_name=os.environ["LANGSMITH_PROJECT"])
                row["project"] = project.name
            row["ok"] = True
        except Exception as exc:
            row.update(
                ok=False,
                error_type=type(exc).__name__,
                status_code=getattr(exc, "status_code", None),
            )
            body = getattr(exc, "body", {})
            if isinstance(body, dict):
                row["error_code"] = body.get("code")
            # Do not log exception strings: provider errors can include request data.
        row["seconds"] = round(time.perf_counter() - start, 3)
        rows.append(row)
    save_json(
        folder / "api-health.json",
        {"checked_at": datetime.now(timezone.utc).isoformat(), "checks": rows},
    )
    print(json.dumps(rows, ensure_ascii=False, indent=2), flush=True)
    return all(r["ok"] for r in rows if r["api"] in ("openai", "tavily"))


def run_one(name, folder, mode, max_calls):
    folder.mkdir(parents=True, exist_ok=False)
    settings, variant = configure(load_settings(), name)
    inputs = {node: load_input(node, "acceptance") for node in NODES}
    manifest = {
        "name": name,
        "mode": mode,
        "git_revision": git_revision(),
        "git_dirty": git_dirty(),
        "settings": settings.model_dump(),
        "variant": variant.__dict__,
        "input_hashes": {
            n: hashlib.sha256(d.model_dump_json().encode()).hexdigest() for n, d in inputs.items()
        },
    }
    save_json(folder / "manifest.json", manifest)
    recorder = Recorder(folder / "events.jsonl", max_calls)
    recorder.event(kind="setup", phase="start")
    backend = MockBackend() if mode == "mock" else BenchmarkBackend(settings, variant.judge_workers)
    sources = (
        None
        if mode == "mock"
        else {n: TimedSource(s, recorder, n) for n, s in live_sources(settings).items()}
    )
    graph = build_main_graph(
        inputs, mode, settings, backend, sources, code_report=variant.assemble_report
    )
    recorder.event(kind="setup", phase="end")
    run_id = uuid.uuid4()
    project = os.getenv("LANGSMITH_PROJECT", "")
    tracing = mode != "mock" and os.getenv("LANGSMITH_TRACING", "false").lower() == "true"
    final = {}
    start = time.perf_counter()
    error = None
    try:
        with tracing_context(enabled=tracing, project_name=project or None):
            final = graph.invoke(
                {},
                config={
                    "run_id": run_id,
                    "run_name": f"latency-{name}",
                    "recursion_limit": 80,
                    "callbacks": [recorder],
                    "configurable": {"output_dir": str(folder)},
                    "tags": ["latency-ablation", name, mode],
                    "metadata": {"variant": name, "git_revision": git_revision()},
                },
            )
        save_json(folder / "state.json", final)
    except Exception as exc:
        error = type(exc).__name__
        recorder.event(kind="experiment_error", error=error)
    finally:
        elapsed = time.perf_counter() - start
        summary = summarize(folder / "events.jsonl", final, round(elapsed, 3), variant)
        summary.update(
            name=name,
            mode=mode,
            error=error,
            trace_id=str(run_id),
            trace_url=None,
            note="One-factor contrasts are conditional on baseline; interactions are not additive. Live search and LLM outputs can vary.",
        )
        save_json(folder / "summary.json", summary)
    if tracing:
        from langchain_core.tracers.langchain import wait_for_all_tracers

        wait_for_all_tracers()
        try:
            client = Client(timeout_ms=10000)
            summary["trace_url"] = client.get_run_url(
                run=client.read_run(run_id), project_name=project
            )
        except Exception as exc:
            summary["trace_warning"] = type(exc).__name__
        save_json(folder / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 1 if error or final.get("run_status") == "failed" else 0


def suite(args):
    settings = load_settings()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.mode == "live" and not health(args.output, settings):
        print("Live comparison blocked by API health check. No generation experiments started.")
        return 2
    names = [n for n in VARIANTS if n not in ("baseline", "full_control")]
    random.Random(args.seed).shuffle(names)
    order = ["baseline"] + names + ["baseline", "full_control"]
    save_json(
        args.output / "plan.json",
        {
            "order": order,
            "seed": args.seed,
            "mode": args.mode,
            "per_run_timeout_seconds": args.timeout,
            "max_llm_calls": args.max_calls,
            "quality": "Automatic checks are compared; human review remains required.",
            "limits": "No claim that this is all possible bottlenecks. Baseline keeps search, planner, sufficiency and citation verification.",
        },
    )
    rows = []
    for index, name in enumerate(order):
        folder = args.output / f"{index:02d}-{name}"
        if folder.exists():
            raise ValueError(f"Refusing to overwrite prior experiment: {folder}")
        command = [
            sys.executable,
            "-m",
            "app.benchmark_latency",
            "run",
            "--variant",
            name,
            "--mode",
            args.mode,
            "--output",
            str(folder),
            "--max-calls",
            str(args.max_calls),
        ]
        log = args.output / f"{index:02d}-{name}.log"
        started = time.perf_counter()
        print(f"Starting {name}; timeout {args.timeout}s", flush=True)
        with log.open("w") as out:
            process = subprocess.Popen(
                command, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, start_new_session=True
            )
            try:
                code = process.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                import signal

                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                code = 124
        row = {
            "name": name,
            "folder": str(folder),
            "exit_code": code,
            "process_wall_seconds": round(time.perf_counter() - started, 3),
        }
        if (folder / "summary.json").exists():
            row["summary"] = json.loads((folder / "summary.json").read_text())
        rows.append(row)
        save_json(args.output / "comparison.json", rows)
        print(f"Finished {name}: exit={code}, seconds={row['process_wall_seconds']}", flush=True)
        # Prevent eight expensive repeats when all research failed at the provider boundary.
        if code not in (0,):
            print(
                "Stopping suite after failed/timed-out run; inspect partial events before continuing."
            )
            return code
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("health", "run", "suite"))
    parser.add_argument("--variant", choices=list(VARIANTS), default="baseline")
    parser.add_argument("--mode", choices=("mock", "live"), default="mock")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--max-calls", type=int, default=300)
    parser.add_argument("--seed", type=int, default=20261006)
    args = parser.parse_args(argv)
    args.output = args.output.resolve()
    if args.timeout <= 0 or args.max_calls <= 0:
        parser.error("Limits must be positive")
    if args.command == "health":
        return 0 if health(args.output, load_settings()) else 2
    if args.command == "suite":
        return suite(args)
    if args.mode == "live":
        # Standalone runs also require a fresh probe before any costly generation.
        if not health(args.output.parent / "preflight", load_settings()):
            return 2
    return run_one(args.variant, args.output, args.mode, args.max_calls)


if __name__ == "__main__":
    raise SystemExit(main())

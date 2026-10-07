"""Opt-in paid, frozen-evidence prompt experiment; production prompts stay unchanged."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import subprocess
import sys
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from unittest.mock import patch

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tracers.langchain import wait_for_all_tracers
from langsmith import Client, tracing_context

from graph.node_graph import build_node_graph
from rag.interface import FixedEvidence
from runtime.models import OpenAIBackend
from runtime.progress import ProgressLog
from runtime.prompts import render
from runtime.runner import git_revision, load_input, save_json
from runtime.settings import ROOT, load_settings, require_key
from schemas.contracts import NodeRun

ROLES = ("domain", "stakeholder")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def frozen_input(state, role):
    """Retain every original question and all archived real evidence, never demo excerpts."""
    data = load_input(role, "acceptance").model_copy(deep=True)
    archived = NodeRun.model_validate(state[f"{role}_result"])
    data.evidence = archived.evidence
    data.prior_results = {"tech": NodeRun.model_validate(state["tech_result"]).result}
    data.case_id = f"frozen-real-evidence-{role}"
    data.description += (
        "\n10월 6일 실제 검색에서 보관한 원문을 고정 재생한다. 새 검색은 하지 않는다."
    )
    if not data.evidence or any(e.source_type == "fixture" for e in data.evidence):
        raise ValueError("Experiment requires archived real evidence, not demo fixture excerpts")
    return data


def experimental_render(variant):
    def wrapped(node, data, evidence):
        system, user, original_hash = render(node, data, evidence)
        if variant == "baseline":
            return system, user, original_hash
        example = (ROOT / "experiments/fewshot" / f"{node}.j2").read_text()
        system += "\n" + example
        return system, user, digest(system + "\n" + user)

    return wrapped


class Probe(BaseCallbackHandler):
    """Record model time and graph decisions without storing credentials or raw prompts."""

    def __init__(self, folder):
        self.path = folder / "events.jsonl"
        self.nodes = {}
        self.started = {}
        self.events = []

    def emit(self, event):
        self.events.append(event)
        with self.path.open("a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def ancestors(self, run_id):
        out = []
        while str(run_id) in self.nodes:
            name, run_id = self.nodes[str(run_id)]
            out.append(name)
        return out

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs):
        name = kwargs.get("name") or (serialized or {}).get("name", "chain")
        self.nodes[str(run_id)] = (name, str(parent_run_id))
        self.started[str(run_id)] = time.perf_counter()

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        key = str(run_id)
        name = self.nodes.get(key, ("unknown", None))[0]
        if name not in {"plan", "search", "check_sufficiency", "write_draft", "verify", "fix"}:
            return
        event = {"kind": "stage", "name": name, "seconds": time.perf_counter() - self.started[key]}
        if isinstance(outputs, dict):
            for field in ("verdict", "errors", "fix_count", "is_sufficient"):
                if field in outputs:
                    event[field] = outputs[field]
            if "draft" in outputs:
                d = outputs["draft"]
                event["draft"] = d.model_dump() if hasattr(d, "model_dump") else d
        self.emit(event)

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs):
        self.started[str(run_id)] = time.perf_counter()
        self.nodes[str(run_id)] = ("llm", str(parent_run_id))

    def on_llm_end(self, response, *, run_id, **kwargs):
        usage = (response.llm_output or {}).get("token_usage", {})
        self.emit(
            {
                "kind": "llm",
                "seconds": time.perf_counter() - self.started[str(run_id)],
                "ancestors": self.ancestors(run_id),
                "usage": usage,
            }
        )

    def on_llm_error(self, error, *, run_id, **kwargs):
        self.emit(
            {
                "kind": "llm_error",
                "error": type(error).__name__,
                "ancestors": self.ancestors(run_id),
            }
        )


def summarize(final, probe):
    run = final["output"]
    stages = defaultdict(lambda: {"visits": 0, "seconds": 0.0})
    llms = [e for e in probe.events if e["kind"] == "llm"]
    for e in probe.events:
        if e["kind"] == "stage":
            stages[e["name"]]["visits"] += 1
            stages[e["name"]]["seconds"] += e["seconds"]
    verifies = [e for e in probe.events if e.get("name") == "verify"]
    return {
        "status": run.status,
        "claims": len(run.result.claims),
        "assessments": len(run.result.assessments),
        "known_assessments": sum(a.judgment != "확인 불가" for a in run.result.assessments),
        "unverified": len(run.result.unverified),
        "validation_errors": run.validation_errors,
        "fix_count": run.fix_count,
        "judge_labels": dict(Counter(c.label for c in run.checks)),
        "first_verify": verifies[0] if verifies else None,
        "stages": dict(stages),
        "llm_calls": len(llms),
        "llm_errors": sum(e["kind"] == "llm_error" for e in probe.events),
        "tokens": sum(e["usage"].get("total_tokens", 0) for e in llms),
        "example_leakage": any(x in run.result.model_dump_json() for x in ("EXAMPLE-", "SAMPLE-")),
    }


def run_one(args, settings):
    folder = args.out / f"{args.role}-{args.variant}-{args.repeat}"
    folder.mkdir(parents=True, exist_ok=False)
    data = load_input(args.role, path=args.out / f"input-{args.role}.json")
    backend = OpenAIBackend(settings)
    probe = Probe(folder)
    run_id = uuid.uuid4()
    project = os.environ["LANGSMITH_PROJECT"]
    summary = {
        "role": args.role,
        "variant": args.variant,
        "repeat": args.repeat,
        "trace_id": str(run_id),
        "input_hash": digest(data.model_dump_json()),
        "generator": settings.models.generator,
        "judge": settings.models.judge,
        "git_revision": git_revision(),
        "settings": settings.model_dump(),
    }
    start = time.perf_counter()
    try:
        with patch("graph.node_graph.render", experimental_render(args.variant)):
            graph = build_node_graph(
                args.role,
                data,
                "fixture",
                settings,
                backend,
                FixedEvidence(data),
                log=ProgressLog(folder),
            )
            with tracing_context(enabled=True, project_name=project):
                final = graph.invoke(
                    {},
                    config={
                        "run_id": run_id,
                        "run_name": folder.name,
                        "recursion_limit": 80,
                        "callbacks": [probe],
                        "tags": ["fewshot-ab", args.role, args.variant],
                        "metadata": {
                            "input_hash": summary["input_hash"],
                            "experiment": "frozen-evidence-generator-only",
                            "git_revision": git_revision(),
                        },
                    },
                )
        summary["graph_seconds"] = time.perf_counter() - start
        summary.update(summarize(final, probe))
        save_json(folder / "result.json", final["output"])
        save_json(folder / "draft.json", final["draft"])
        (folder / "system.txt").write_text(final["rendered_system"])
        (folder / "user.txt").write_text(final["rendered_user"])
    except Exception as exc:
        summary.update(error=type(exc).__name__, graph_seconds=time.perf_counter() - start)
    finally:
        save_json(folder / "summary.json", summary)
    wait_for_all_tracers()
    try:
        client = Client(timeout_ms=15000)
        remote = client.read_run(run_id)
        summary["trace_url"] = client.get_run_url(run=remote, project_name=project)
        summary["trace_seconds"] = (
            (remote.end_time - remote.start_time).total_seconds() if remote.end_time else None
        )
        summary["trace_error"] = remote.error
    except Exception as exc:
        summary["trace_lookup_error"] = type(exc).__name__
    save_json(folder / "summary.json", summary)
    print(
        json.dumps(
            {
                k: v
                for k, v in summary.items()
                if k
                in {
                    "role",
                    "variant",
                    "repeat",
                    "graph_seconds",
                    "status",
                    "fix_count",
                    "llm_calls",
                    "error",
                }
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def render_html(out):
    summaries = [json.loads(f.read_text()) for f in sorted(out.glob("*-*-*/summary.json"))]
    parts = [
        '<!doctype html><meta charset="utf-8"><title>Few-shot 판단 실험</title>',
        "<style>body{font:16px/1.7 system-ui;max-width:1100px;margin:40px auto;padding:20px;color:#172b40}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd5df;padding:10px;text-align:left}pre{white-space:pre-wrap}section{margin:40px 0}a{color:#1467ad}</style>",
        "<h1>Few-shot 판단 지침 A/B 실험</h1><p>동일 모델·고정 실제 검색 근거. 생성 프롬프트만 변경. Judge와 재시도 한도는 동일. 새 검색 및 전체 보고서 파이프라인의 성능 실험이 아닙니다. 미확인·실패를 보존합니다.</p>",
        "<p>호출은 정상 응답을 받은 횟수입니다. 실패 호출은 summary.json의 llm_errors에 따로 기록합니다. '확인 불가 외 판정'은 모델이 판정을 내린 개수이며 정확성이 검증된 개수가 아닙니다.</p>",
        "<table><tr><th>역할/반복</th><th>프롬프트</th><th>초</th><th>완료 호출</th><th>수정</th><th>확인 불가 외 판정</th><th>상태</th></tr>",
    ]

    def esc(x):
        return html.escape(str(x))

    for s in summaries:
        parts.append(
            "<tr>"
            + "".join(
                f"<td>{esc(v)}</td>"
                for v in [
                    f"{s['role']}/{s['repeat']}",
                    s["variant"],
                    round(s["graph_seconds"], 2),
                    s.get("llm_calls"),
                    s.get("fix_count"),
                    f"{s.get('known_assessments')}/{s.get('assessments')}",
                    s.get("status", s.get("error")),
                ]
            )
            + "</tr>"
        )
    parts.append("</table>")
    for s in summaries:
        folder = out / f"{s['role']}-{s['variant']}-{s['repeat']}"
        if not (folder / "result.json").exists():
            continue
        r = json.loads((folder / "result.json").read_text())
        parts.append(f"<section><h2>{esc(folder.name)}</h2>")
        if s.get("trace_url"):
            parts.append(f'<a href="{esc(s["trace_url"])}">LangSmith trace</a>')
        parts.append(
            f"<p>{esc(r['result']['summary'])}</p><table><tr><th>기술/기준</th><th>판정</th><th>논거</th></tr>"
        )
        for a in r["result"]["assessments"]:
            parts.append(
                f"<tr><td>{esc(a['technology'] + '/' + a['criterion'])}</td><td>{esc(a['judgment'])}</td><td>{esc(a['rationale'])}</td></tr>"
            )
        parts.append("</table><h3>검증 후 남은 주장</h3>")
        evidence = {e["id"]: e for e in r["evidence"]}
        for claim in r["result"]["claims"]:
            parts.append(
                f"<p><b>{esc(claim['technology'] + '/' + claim['criterion'])}</b> {esc(claim['text'])}</p>"
            )
            for eid in claim["evidence_ids"]:
                e = evidence.get(eid)
                if e:
                    parts.append(
                        f"<details><summary>{esc(eid)} — {esc(e['title'])}</summary><p>{esc(e['url'])}</p><pre>{esc(e['text'])}</pre></details>"
                    )
        parts.append(
            "<h3>미확인 및 검증 오류</h3><pre>"
            + esc("\n".join(r["result"]["unverified"] + r["validation_errors"]))
            + "</pre></section>"
        )
    (out / "comparison.html").write_text("\n".join(parts))
    save_json(out / "comparison.json", summaries)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-state", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--role", choices=ROLES)
    parser.add_argument("--variant", choices=["baseline", "fewshot"])
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args(argv)
    settings = load_settings()
    if args.key_file:
        os.environ["OPENAI_API_KEY"] = args.key_file.expanduser().read_text().strip()
    settings.models.timeout_seconds = 60
    settings.models.max_retries = 0
    settings.models.generator = settings.models.judge = "gpt-4.1-mini"
    settings.models.temperature = 0
    require_key("LANGSMITH_API_KEY")
    if not os.environ.get("LANGSMITH_PROJECT"):
        raise ValueError("Set personal LANGSMITH_PROJECT")
    if args.role:
        run_one(args, settings)
        return
    if not args.source_state or args.repeats < 1:
        raise ValueError("Suite requires source-state and positive repeats")
    args.out.mkdir(parents=True, exist_ok=False)
    state = json.loads(args.source_state.read_text())
    for role in ROLES:
        save_json(args.out / f"input-{role}.json", frozen_input(state, role))
    from openai import OpenAI

    start = time.perf_counter()
    OpenAI(timeout=20, max_retries=0).chat.completions.create(
        model=settings.models.generator,
        messages=[{"role": "user", "content": "Reply OK."}],
        max_tokens=4,
    )
    Client(timeout_ms=15000).read_project(project_name=os.environ["LANGSMITH_PROJECT"])
    save_json(
        args.out / "protocol.json",
        {
            "source_state": str(args.source_state.resolve()),
            "git_revision": git_revision(),
            "settings": settings.model_dump(),
            "repeats": args.repeats,
            "preflight_seconds": time.perf_counter() - start,
            "judge_unchanged": True,
            "network_search": False,
            "notes": "Frozen real evidence; AB then BA per role; serial runs; no claim of full pipeline speedup.",
            "example_hashes": {
                r: digest((ROOT / "experiments/fewshot" / f"{r}.j2").read_text()) for r in ROLES
            },
        },
    )
    for repeat in range(1, args.repeats + 1):
        order = ["baseline", "fewshot"] if repeat % 2 else ["fewshot", "baseline"]
        for role in ROLES:
            for variant in order:
                command = [
                    sys.executable,
                    "-m",
                    "app.benchmark_fewshot",
                    "--out",
                    str(args.out),
                    "--role",
                    role,
                    "--variant",
                    variant,
                    "--repeat",
                    str(repeat),
                ]
                print(f"START {role}/{variant}/{repeat}", flush=True)
                try:
                    subprocess.run(command, cwd=ROOT, timeout=300, check=True)
                except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
                    save_json(
                        args.out / f"failure-{role}-{variant}-{repeat}.json",
                        {"error": type(exc).__name__},
                    )
                render_html(args.out)
    render_html(args.out)


if __name__ == "__main__":
    main()

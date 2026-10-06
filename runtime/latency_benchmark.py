"""Isolated latency experiments; production defaults and contracts stay unchanged."""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables.config import ContextThreadPoolExecutor

from runtime.models import OpenAIBackend
from schemas.contracts import JudgeResult


@dataclass(frozen=True)
class Variant:
    search: int = 2
    fix: int = 0
    supplement: int = 0
    judge_workers: int = 4
    assemble_report: bool = True


VARIANTS = {
    "baseline": Variant(),
    "A_serial_judge": Variant(judge_workers=1),
    "B_followup_search": Variant(search=3),
    "C_answer_fix": Variant(fix=1),
    "D_supplement": Variant(supplement=1),
    "E_llm_report": Variant(assemble_report=False),
    "full_control": Variant(search=3, fix=1, supplement=1, judge_workers=1, assemble_report=False),
}


def configure(settings, name):
    config = settings.model_copy(deep=True)
    variant = VARIANTS[name]
    config.limits.search = variant.search
    config.limits.fix = variant.fix
    config.limits.supplement = variant.supplement
    return config, variant


class Recorder(BaseCallbackHandler):
    """Append metadata as work finishes; preserve progress even after a hard timeout.

    Durations of overlapping spans are not wall time. No prompt text or API keys are logged.
    """

    run_inline = True
    raise_error = True

    def __init__(self, path: Path, max_llm_calls=300):
        self.path = path
        self.lock = threading.RLock()
        self.started = {}
        self.parents = {}
        self.names = {}
        self.llm_calls = 0
        self.max_llm_calls = max_llm_calls
        self.epoch = time.perf_counter()

    def event(self, **row):
        with self.lock:
            row["at_seconds"] = round(time.perf_counter() - self.epoch, 6)
            with self.path.open("a", encoding="utf-8") as out:
                out.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def _start(self, kind, run_id, parent_run_id, name, **extra):
        key = str(run_id)
        with self.lock:
            self.started[key] = time.perf_counter()
            self.parents[key] = str(parent_run_id) if parent_run_id else None
            self.names[key] = name
            if kind == "llm":
                if self.llm_calls >= self.max_llm_calls:
                    self.event(kind="budget_exhausted", limit=self.max_llm_calls)
                    raise RuntimeError("Experimental LLM call budget exhausted")
                self.llm_calls += 1
            self.event(
                kind=kind, phase="start", id=key, parent=self.parents[key], name=name, **extra
            )

    def _end(self, kind, run_id, **extra):
        key = str(run_id)
        with self.lock:
            start = self.started.pop(key, None)
            ancestors = []
            parent = self.parents.get(key)
            while parent:
                ancestors.append(self.names.get(parent, "unknown"))
                parent = self.parents.get(parent)
            self.event(
                kind=kind,
                phase="end",
                id=key,
                name=self.names.get(key),
                ancestors=ancestors,
                seconds=round(time.perf_counter() - start, 6) if start else None,
                **extra,
            )

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs):
        self._start(
            "chain",
            run_id,
            parent_run_id,
            kwargs.get("name") or (serialized or {}).get("name", "chain"),
        )

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        self._end("chain", run_id)

    def on_chain_error(self, error, *, run_id, **kwargs):
        self._end("chain", run_id, error=type(error).__name__)

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs):
        self._start(
            "llm",
            run_id,
            parent_run_id,
            (serialized or {}).get("name", "chat_model"),
            input_characters=sum(len(str(m.content)) for batch in messages for m in batch),
        )

    def on_llm_end(self, response, *, run_id, **kwargs):
        usage = (response.llm_output or {}).get("token_usage", {})
        self._end("llm", run_id, usage=usage)

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._end("llm", run_id, error=type(error).__name__)


class BenchmarkBackend(OpenAIBackend):
    """Same per-claim Judge and evidence inputs, with bounded concurrency only."""

    def __init__(self, settings, workers):
        super().__init__(settings)
        self.workers = workers
        self.slots = threading.BoundedSemaphore(workers)

    def judge(self, result, evidence):
        if self.workers == 1:
            return super().judge(result, evidence)

        def one(claim):
            with self.slots:
                single = result.model_copy(update={"claims": [claim]})
                return super(BenchmarkBackend, self).judge(single, evidence).checks

        with ContextThreadPoolExecutor(max_workers=self.workers) as pool:
            checks = list(pool.map(one, result.claims))
        return JudgeResult(checks=[item for group in checks for item in group])


class TimedSource:
    def __init__(self, source, recorder, role):
        self.source, self.recorder, self.role = source, recorder, role
        self.retryable = source.retryable

    def search(self, question, attempt, scope="target"):
        start = time.perf_counter()
        error = None
        try:
            return self.source.search(question, attempt, scope)
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            self.recorder.event(
                kind="search",
                phase="end",
                role=self.role,
                question_id=question.id,
                attempt=attempt,
                scope=scope,
                seconds=round(time.perf_counter() - start, 6),
                error=error,
            )


def summarize(events_path, final, wall_seconds, variant):
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    llms = [e for e in events if e["kind"] == "llm" and e["phase"] == "end"]
    starts = [e for e in events if e["kind"] == "llm" and e["phase"] == "start"]
    stages = {}
    for event in llms:
        stage = next(
            (
                n
                for n in event["ancestors"]
                if n
                in {"plan", "check_sufficiency", "rewrite_query", "write_draft", "verify", "fix"}
            ),
            "other",
        )
        group = stages.setdefault(stage, {"calls": 0, "sum_call_seconds": 0.0})
        group["calls"] += 1
        group["sum_call_seconds"] += event["seconds"] or 0
    roles = {}
    for role, key in {
        "tech": "tech_result",
        "market": "market_result",
        "stakeholder": "stakeholder_result",
        "domain": "domain_result",
        "synthesis": "synthesis",
        "report": "report",
    }.items():
        run = final.get(key)
        if run:
            roles[role] = {
                "status": run.status,
                "claims": len(run.result.claims),
                "assessments": len(run.result.assessments),
                "unknown": sum(a.judgment == "확인 불가" for a in run.result.assessments),
                "unverified": len(run.result.unverified),
                "errors": len(run.validation_errors),
                "search_records": len(run.searches),
                "fix_count": run.fix_count,
            }
    counts = Counter(
        e.get("name") for e in events if e["kind"] == "chain" and e["phase"] == "start"
    )
    usage = Counter()
    for event in llms:
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            usage[name] += event.get("usage", {}).get(name, 0)
    return {
        "variant": asdict(variant),
        "wall_seconds": wall_seconds,
        "llm_calls": len(starts),
        "llm_errors": sum(bool(e.get("error")) for e in llms),
        "usage": dict(usage),
        "stages": stages,
        "stage_visits": dict(counts),
        "roles": roles,
        "supplement_round": final.get("supplement_round"),
        "run_status": final.get("run_status"),
        "report_check": final.get("report_check"),
        "quality_note": "Counts and automatic checks only; not human factual acceptance.",
    }

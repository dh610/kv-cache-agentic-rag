"""Supervisor 패턴 메인 그래프.

    START → initialize → supervisor
                            │  add_conditional_edges (동적 · 순서 하드코딩 없음)
                            ├─→ tech ─────────┐
                            ├─→ market ───────┤
                            ├─→ stakeholder ──┤  (리스트 반환 시 병렬)
                            ├─→ domain ───────┤
                            ├─→ synthesis ────┼─→ supervisor
                            ├─→ report ───────┤
                            ├─→ quality ──────┘
                            └─→ finalize → END

간선은 모두 ``supervisor ↔ 하위 에이전트`` 뿐이다. 하위 에이전트끼리 잇는 간선은 없다
(가이드 B: 하위 에이전트 간 직접 통신 금지).
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from agents.observability import log_from_config
from agents.quality import make_quality_node
from agents.report_view import RESULT_KEYS, flat_state
from agents.state import SupervisorState
from agents.supervisor import ROUTES, initial_control, make_supervisor, route
from agents.workers import NEEDS_TECH, dump_state_digest, make_worker
from rag.interface import EvidenceSource
from runtime.models import ModelBackend
from runtime.reporting import assemble_report, validate_report, write_report
from runtime.settings import Settings
from schemas.contracts import NODES, NodeInput

WORKER_ROLES = ("tech", *NEEDS_TECH, "synthesis", "report")


def build_supervisor_graph(
    inputs: dict[str, NodeInput],
    mode: str,
    settings: Settings,
    backend: ModelBackend,
    sources: dict[str, EvidenceSource] | None = None,
    *,
    first_pass: bool = False,
    trace_id: str | None = None,
):
    if mode not in ("mock", "fixture", "live"):
        raise ValueError("Pipeline mode must be mock, fixture or live")
    if mode == "live" and (not sources or set(sources) != set(NODES[:4])):
        raise ValueError("live mode requires the four research-node sources")

    policy = settings.supervisor
    builder = StateGraph(SupervisorState)

    def initialize(state, config: RunnableConfig):
        """제어 메타를 세운다. trace_id 가 State·외부 로그·LangSmith 를 잇는다."""
        correlation = trace_id or str(uuid.uuid4())
        run_id = str((config or {}).get("run_id") or correlation)
        log = log_from_config({"trace_id": correlation, "run_id": run_id}, config)
        log.record(
            step=0,
            node="initialize",
            action="dispatch",
            targets=[],
            reason=f"실행 시작 — mode={mode}, 대상={list(settings.target_techs.values())}",
            extra={
                "max_steps": policy.max_steps,
                "max_attempts": policy.max_attempts,
                "max_seconds": policy.max_seconds,
            },
        )
        return {
            "target_techs": settings.target_techs,
            "domain": settings.domain,
            "results": {},
            "sources": [],
            "trl_result": {},
            "quality": None,
            "trace_id": correlation,
            "run_id": run_id,
            "step": 0,
            "max_steps": policy.max_steps,
            "control": initial_control(),
            "route": [],
            "assignment": None,
            "decisions": [],
            "revision_round": 0,
            "max_revisions": policy.max_revisions,
            "quality_round": 0,
            "max_quality_rounds": policy.max_quality_rounds,
            "last_error": None,
            "deadline_at": (time.time() + policy.max_seconds) if policy.max_seconds else None,
            "run_status": "needs_revision",
            "stop_reason": "",
        }

    def finalize(state, config: RunnableConfig):
        """보고서를 조판·검사하고 실행 상태를 확정한다. 새 판단은 하지 않는다."""
        view = flat_state(state, settings, mode)
        text = assemble_report(view, RESULT_KEYS, mode)
        if first_pass:
            text = text.replace(
                "# SUMMARY",
                "# SUMMARY\n1차 초안: 추가 재검색·전체 수정·종합 뒤 보완은 생략했습니다. "
                "미확인과 검증 실패는 그대로 표시합니다.",
                1,
            )
        validation = validate_report(view, RESULT_KEYS, text, mode)
        verdict = state.get("quality")
        if verdict is not None and not verdict.passed:
            validation["problems"] = sorted(
                {*validation["problems"], *(f"품질 미달: {c}" for c in verdict.failed_criteria())}
            )
            validation["ready"] = False

        folder = (config or {}).get("configurable", {}).get("output_dir")
        path = submission = ""
        if folder:
            # 전체본은 산출물로 남기고, 제출본은 과제 규칙의 10장 한도에 맞춰 따로 조판한다.
            # 같은 평가 결과의 다른 조판이며 판정·비교표·출처 연결은 양쪽이 같다.
            path = write_report(text, Path(folder), settings.report, mode, publish=False)
            compact_text = assemble_report(view, RESULT_KEYS, mode, compact=True)
            submission = write_report(
                compact_text,
                Path(folder) / "submission",
                settings.report,
                mode,
                compact=True,
            )
        if not path:
            validation["ready"] = False
            validation["problems"].append("No report output directory was configured")

        statuses = [run.status for run in state.get("results", {}).values()]
        quality_failed = verdict is not None and not verdict.passed
        status = (
            "failed"
            if "failed" in statuses or len(state.get("results", {})) < len(NODES)
            else "needs_revision"
            # 품질 평가를 통과하지 못한 보고서를 completed 로 보고하지 않는다.
            if "needs_revision" in statuses
            or quality_failed
            or (mode != "mock" and not validation["ready"])
            else "completed"
        )
        if folder:
            dump_state_digest({**state, "run_status": status}, Path(folder) / "supervisor.json")
        log_from_config(state, config).record(
            step=state.get("step", 0),
            node="finalize",
            action="finalize",
            targets=[],
            reason=f"실행 종료 — status={status}; {state.get('stop_reason', '')}",
            extra={"report_path": path, "ready": validation["ready"]},
        )
        return {
            "run_status": status,
            "report_path": path,
            "submission_path": submission,
            "report_text": text,
            "report_check": validation,
        }

    builder.add_node("initialize", initialize)
    builder.add_node("supervisor", make_supervisor(settings, inputs))
    for role in WORKER_ROLES:
        builder.add_node(
            role,
            make_worker(role, inputs, mode, settings, backend, sources, first_pass=first_pass),
        )
    builder.add_node("quality", make_quality_node(settings, backend, mode))
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "supervisor")
    # 동적 라우팅. 순서는 간선이 아니라 Supervisor 의 판단에 있다.
    builder.add_conditional_edges("supervisor", route, [*ROUTES, "supervisor"])
    # 모든 하위 에이전트는 Supervisor 에게만 보고한다.
    for role in (*WORKER_ROLES, "quality"):
        builder.add_edge(role, "supervisor")
    builder.add_edge("finalize", END)
    return builder.compile()

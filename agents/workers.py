"""하위 에이전트(Worker) 래퍼.

하위 에이전트는 **Supervisor 하고만 통신한다** (가이드 B). 그래서 이 모듈의 노드는
- 자기 역할의 입력을 State 에서 조립하고,
- 기존 공통 서브그래프(``graph.node_graph.build_node_graph``)를 그대로 호출하며,
- 결과를 Supervisor 가 읽는 State 키(``results`` · ``sources`` · ``control``)로만 돌려준다.

다른 하위 에이전트의 노드를 직접 호출하거나 그래프 간선을 잇는 코드는 여기에 없다.
관점 평가가 기술 조사 결과를 참고하는 것은 Supervisor 가 ``results`` 에 올려 둔 값을
입력으로 받는 것이지, 에이전트 간 직접 통신이 아니다.

지속성 비용(가이드 C): 노드가 끝나면 결과 **전문**을 ``<output_dir>/nodes/<role>.json`` 에
쓰고, State 의 ``sources`` 에는 실제 인용된 근거만 올린다.
"""

from __future__ import annotations

import json
from pathlib import Path

from langchain_core.runnables import RunnableConfig

from agents.observability import log_from_config
from agents.state import RoleControl
from agents.sufficiency import assess, rework_feedback
from graph.node_graph import build_node_graph, contract_errors
from rag.evidence import merge_evidence
from rag.interface import CombinedSource, EvidenceSource, FixedEvidence
from runtime.report_draft import assemble_report_node
from runtime.reporting import used_ids
from schemas.contracts import JudgeResult, NodeInput, NodeRun

RESEARCH_ROLES = ("tech", "market", "stakeholder", "domain")
# 관점 평가는 기술 조사 결과를 입력으로 받는다. 간선이 아니라 입력 의존성이다.
NEEDS_TECH = ("market", "stakeholder", "domain")


def _save_artifact(config: RunnableConfig, role: str, run: NodeRun) -> str | None:
    folder = (config or {}).get("configurable", {}).get("output_dir")
    if not folder:
        return None
    path = Path(folder) / "nodes"
    path.mkdir(parents=True, exist_ok=True)
    target = path / f"{role}.json"
    target.write_text(run.model_dump_json(indent=2), encoding="utf-8")
    return str(target)


def _node_input(role: str, state, inputs: dict[str, NodeInput], mode: str) -> NodeInput:
    """역할 입력 조립. Supervisor 가 올려 둔 상위 결과와 재작업 지시만 덧붙인다."""
    data = inputs[role].model_copy(deep=True)
    if mode == "live":
        data.evidence = []  # 데모 고정 발췌를 실제 검색 근거와 섞지 않는다.
    results = state.get("results", {})
    if role in NEEDS_TECH and "tech" in results:
        data.prior_results = {"tech": results["tech"].result}
    elif role in ("synthesis", "report"):
        data.prior_results = {name: run.result for name, run in results.items()}
    control = state.get("control", {}).get(role)
    if control is not None and control.attempts:
        data.description += rework_feedback(control)
    return data


def _inherited_evidence(role: str, state) -> list:
    """종합·보고서는 상위 노드의 근거 풀 전체가 아니라 '인용된 원문'만 물려받는다.

    풀 전체를 넘기면 입력이 수 MB 가 되어 생성이 제한 시간을 넘긴다 (기존 live 점검 기록).
    """
    runs = list(state.get("results", {}).values())
    if role in ("synthesis", "report"):
        return [[e for e in r.evidence if e.id in used_ids(r)] for r in runs]
    return [r.evidence for r in runs]


def make_worker(
    role: str,
    inputs: dict[str, NodeInput],
    mode: str,
    settings,
    backend,
    sources: dict[str, EvidenceSource] | None = None,
    *,
    first_pass: bool = False,
):
    """역할 하나를 도는 LangGraph 노드를 만든다."""

    def run_worker(state, config: RunnableConfig):
        log = log_from_config(state, config)
        step = state.get("step", 0)
        control = state.get("control", {}).get(role) or RoleControl(role=role)
        attempt = control.attempts + 1
        data = _node_input(role, state, inputs, mode)
        data.evidence = merge_evidence(data.evidence, *_inherited_evidence(role, state))

        previous = state.get("results", {}).get(role) if attempt > 1 else None
        try:
            if role == "report" and first_pass:
                out = assemble_report_node(data, dict(state.get("results", {})), mode)
                errors = contract_errors(
                    role, data, out.result, out.evidence, JudgeResult(checks=out.checks)
                )
                if errors:
                    out.validation_errors.extend(errors)
                    out.status = "failed"
            else:
                source = (
                    CombinedSource(FixedEvidence(data), sources[role])
                    if sources and role in sources
                    else FixedEvidence(data)
                )
                graph = build_node_graph(
                    role, data, mode, settings, backend, source, previous=previous
                )
                out = graph.invoke({}, config={"recursion_limit": 80})["output"]
        except Exception as exc:  # 하위 에이전트 실패는 실행 전체를 멈추지 않는다 (fallback).
            failure = f"{type(exc).__name__}: {exc}"
            log.record(
                step=step,
                node=role,
                action="dispatch",
                targets=[role],
                reason=f"하위 에이전트 실행 실패 — {failure}",
                extra={"attempt": attempt, "outcome": "error"},
            )
            return {
                "control": {
                    role: control.model_copy(
                        update={
                            "status": "failed",
                            "attempts": attempt,
                            "last_error": failure,
                            "sufficient": False,
                        }
                    )
                },
                "last_error": failure,
            }

        artifact = _save_artifact(config, role, out)
        cited = used_ids(out)
        # 수확 체감 측정: 재작업이 실제로 새 근거를 가져왔는가.
        # 근거가 한 건도 늘지 않았다면 "덜 찾은 것"이 아니라 "없는 것"에 가깝고,
        # 같은 질의로 한 번 더 도는 것은 시간과 호출만 쓴다.
        found = len(out.evidence)
        stalled = attempt > 1 and found <= control.evidence_count
        summary = assess(role, out, data, settings).model_copy(
            update={
                "attempts": attempt,
                "artifact": artifact,
                "evidence_count": found,
                "stalled": stalled,
            }
        )
        log.record(
            step=step,
            node=role,
            action="rework" if attempt > 1 else "dispatch",
            targets=[role],
            reason=(
                f"{role} 완료 — status={out.status}, 충분도={summary.sufficiency}, "
                f"근거 {found}건, 미해결 {len(summary.open_gaps)}건"
                + (" (재작업했으나 근거 증가 없음)" if stalled else "")
            ),
            extra={
                "attempt": attempt,
                "outcome": out.status,
                "open_gaps": summary.open_gaps[:12],
                "evidence_count": found,
                "stalled": stalled,
            },
        )
        delta = {
            "results": {role: out},
            "sources": [e for e in out.evidence if e.id in cited],
            "control": {role: summary},
        }
        if role == "synthesis":
            delta["trl_result"] = _trl_from_synthesis(out, settings)
        return delta

    run_worker.__name__ = f"worker_{role}"
    return run_worker


def _trl_from_synthesis(out: NodeRun, settings) -> dict:
    confirmed = {
        t.technology: t.model_dump() for t in out.result.trl_estimates if not t.provisional
    }
    return {
        tech: confirmed.get(
            tech,
            {
                "technology": tech,
                "level": None,
                "rationale": "확정 TRL 근거 부족",
                "evidence_ids": [],
                "disclaimer": "공개 정보 기반 추정",
            },
        )
        for tech in settings.target_techs.values()
    }


def dump_state_digest(state, path: Path) -> None:
    """재개용 최소 상태만 파일로 남긴다 (페이로드 본문은 nodes/*.json 에 있다)."""
    digest = {
        "trace_id": state.get("trace_id"),
        "run_id": state.get("run_id"),
        "step": state.get("step"),
        "revision_round": state.get("revision_round"),
        "quality_round": state.get("quality_round"),
        "run_status": state.get("run_status"),
        "stop_reason": state.get("stop_reason"),
        "control": {
            role: control.model_dump() for role, control in state.get("control", {}).items()
        },
        "decisions": [d.model_dump() for d in state.get("decisions", [])],
    }
    path.write_text(json.dumps(digest, ensure_ascii=False, indent=2), encoding="utf-8")

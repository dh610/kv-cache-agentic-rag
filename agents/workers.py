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
from agents.sufficiency import rework_feedback
from graph.node_graph import build_node_graph, contract_errors
from rag.evidence import merge_evidence
from rag.interface import CombinedSource, EvidenceSource, FixedEvidence
from runtime.report_draft import assemble_report_node
from runtime.reporting import used_ids
from schemas.contracts import JudgeResult, NodeInput, NodeRun

RESEARCH_ROLES = ("tech", "market", "stakeholder", "domain")
# 관점 평가는 기술 조사 결과를 입력으로 받는다. 간선이 아니라 입력 의존성이다.
NEEDS_TECH = ("market", "stakeholder", "domain")


def _scoped_to_assignment(run: NodeRun, item) -> NodeRun:
    """배정 범위 밖의 판정·주장은 버린다.

    기술별로 쪼갠 조각에게도 상위 결과는 전부 넘어가므로, 생성기가 맡지 않은 기술까지
    판정을 써내는 일이 있다 (live 실행에서 종합의 KIVI 조각이 ITME 판정을 "확인 불가"로
    써냈고, 도착 순서에 따라 ITME 조각의 제대로 된 판정을 덮을 수 있었다). 조각은
    자기가 맡은 것만 돌려줘야 병합이 (기술, 항목) 단위로 성립한다.
    """
    if item is None or not item.technologies:
        return run
    scope = set(item.technologies)
    result = run.result.model_copy(deep=True)
    kept_claims = [c for c in result.claims if c.technology in scope]
    dropped = {c.id for c in result.claims} - {c.id for c in kept_claims}
    result.claims = kept_claims
    result.assessments = [a for a in result.assessments if a.technology in scope]
    result.trl_estimates = [t for t in result.trl_estimates if t.technology in scope]
    return run.model_copy(
        update={
            "result": result,
            "checks": [c for c in run.checks if c.claim_id not in dropped],
        }
    )


def _save_artifact(config: RunnableConfig, role: str, run: NodeRun, item=None) -> str | None:
    """조각을 그대로 남긴다. 같은 역할의 조각이 서로 덮어쓰지 않게 배정을 파일명에 쓴다."""
    folder = (config or {}).get("configurable", {}).get("output_dir")
    if not folder:
        return None
    path = Path(folder) / "nodes"
    path.mkdir(parents=True, exist_ok=True)
    scope = "-".join(item.technologies) if item is not None and item.technologies else "all"
    attempt = item.attempt if item is not None else 1
    target = path / f"{role}.{scope}.{attempt}.json"
    target.write_text(run.model_dump_json(indent=2), encoding="utf-8")
    return str(target)


def _node_input(role: str, state, inputs: dict[str, NodeInput], mode: str, item) -> NodeInput:
    """배정 하나의 입력을 조립한다.

    배정에 기술·항목 범위가 적혀 있으면 질문을 그만큼으로 좁힌다. 좁힌 결과는 그 범위의
    조각이고, 역할 전체 결과는 ``agents/merge.py`` 가 (기술, 항목) 단위로 합쳐 만든다.
    """
    data = inputs[role].model_copy(deep=True)
    if mode == "live":
        data.evidence = []  # 데모 고정 발췌를 실제 검색 근거와 섞지 않는다.
    if item is not None and (item.technologies or item.criteria):
        scoped = [
            q
            for q in data.questions
            if (not item.technologies or q.technology in item.technologies)
            and (not item.criteria or q.criterion in item.criteria)
        ]
        # 범위에 맞는 질문이 하나도 없으면 좁히지 않는다. 노드는 질문 없이 돌 수 없다.
        data.questions = scoped or data.questions
    results = state.get("results", {})
    if role in NEEDS_TECH and "tech" in results:
        data.prior_results = {"tech": results["tech"].result}
    elif role in ("synthesis", "report"):
        data.prior_results = {name: run.result for name, run in results.items()}
    control = state.get("control", {}).get(role)
    if control is not None and control.attempts:
        data.description += rework_feedback(control, item)
    if item is not None and item.feedback:
        # 품질 평가가 적어 준 미달 사유. 어느 문장이 왜 걸렸는지가 여기 들어와야
        # 고쳐 쓸 수 있다. 사유 없이 "다시 하라"만 보내면 같은 글이 한 번 더 온다.
        data.description += (
            "\n보고서 품질 평가 미달 사유입니다. 아래를 고쳐 다시 작성하세요: "
            + " / ".join(item.feedback[:6])
        )
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
        item = state.get("assignment")
        attempt = item.attempt if item is not None else control.attempts + 1
        data = _node_input(role, state, inputs, mode, item)
        data.evidence = merge_evidence(data.evidence, *_inherited_evidence(role, state))

        previous = state.get("results", {}).get(role) if attempt > 1 else None
        # 재작업은 "처음부터 다시"가 아니라 "부족한 것만 더". 질문마다 첫 실행과 같은
        # 검색 예산을 다시 주면 라운드 하나가 첫 실행만큼 비싸진다 (live 실측 16분).
        budget = settings
        if previous is not None and settings.supervisor.rework_search:
            budget = settings.model_copy(deep=True)
            budget.limits.search = settings.supervisor.rework_search
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
                    role, data, mode, budget, backend, source, previous=previous
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

        out = _scoped_to_assignment(out, item)
        artifact = _save_artifact(config, role, out, item)
        cited = used_ids(out)
        # 충분성은 여기서 재지 않는다. 이 결과는 역할의 **조각**이라 혼자서는 그 역할이
        # 충분한지 판정할 수 없다. 조각들이 합쳐진 뒤 Supervisor 가 판정한다.
        log.record(
            step=step,
            node=role,
            action="rework" if attempt > 1 else "dispatch",
            targets=[item.label() if item is not None else role],
            reason=(
                f"{item.label() if item is not None else role} 완료 — status={out.status}, "
                f"질문 {len(data.questions)}건, 근거 {len(out.evidence)}건"
            ),
            extra={
                "attempt": attempt,
                "outcome": out.status,
                "questions": [q.id for q in data.questions],
                "evidence_count": len(out.evidence),
            },
        )
        delta = {
            "results": {role: out},
            "sources": [e for e in out.evidence if e.id in cited],
            "control": {
                role: control.model_copy(
                    update={"status": out.status, "attempts": attempt, "artifact": artifact}
                )
            },
        }
        # 확정 TRL 은 여기서 쓰지 않는다. 종합이 기술별로 쪼개져 동시에 돌아오므로 조각
        # 하나가 쓰면 다른 조각의 값을 덮는다. 합쳐진 결과에서 조판 직전에 계산한다
        # (agents/report_view.py).
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

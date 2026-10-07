"""Supervisor: 하위 에이전트를 동적으로 라우팅하는 조정 계층.

가이드 B 의 Supervisor 필수 항목이 여기 전부 들어 있다.

  * 하위 에이전트는 Supervisor 하고만 통신한다 — 모든 간선이 supervisor ↔ worker 다.
  * 순서를 하드코딩하지 않는다 — :func:`decide` 는 **현재 State(수집된 관점, 근거 충분도)**
    만 읽어 다음 분기를 고르고, ``add_conditional_edges`` 가 그 결과를 따른다. 같은 그래프가
    실행마다 다른 경로를 돈다.
  * 스텝 수를 고정하지 않는다 — 근거가 충분해졌다고 판정될 때까지 재작업을 돌고,
    충분해진 뒤에야 종합·보고서로 넘어간다.
  * 근거 부족이면 **그 하위 에이전트에게만** 재작업을 지시한다 (``rework``).

종료 보장(가이드 C): 아래 상한 중 하나라도 소진되면 ``finalize`` 로만 분기한다.
한도를 소진한 채 끝나는 것은 실패가 아니라 "미해결을 남긴 채 종료"이며, 그 사유를
State 의 ``stop_reason`` 과 decisions.jsonl 에 남긴다.

재작업을 **언제 하지 않는가**도 같은 비중으로 설계했다. 이 도메인(KV cache 최적화의
시장·이해관계자 근거)은 공개 정보 자체가 희소해서, 같은 질의로 한 번 더 도는 재작업은
같은 ``확인 불가`` 를 더 비싸게 받아 올 뿐이다. 그래서 두 개의 제동 장치를 둔다.

  수확 체감   재작업했는데 근거가 한 건도 늘지 않은 역할(``RoleControl.stalled``)은
              다시 재작업하지 않는다. "덜 찾은 것"과 "원래 없는 것"을 구분하는 장치다.
  시간 예산   ``deadline_at`` 을 넘기면 재작업·품질 재작성만 멈춘다. 보고서 생성과 품질
              평가는 계속 진행해 산출물은 반드시 나온다.
"""

from __future__ import annotations

import time

from langchain_core.runnables import RunnableConfig

from agents.observability import log_from_config
from agents.state import RoleControl
from agents.workers import NEEDS_TECH

# 이 그래프가 가질 수 있는 분기 대상 전체. add_conditional_edges 의 path_map 과 같다.
ROUTES = ("tech", "market", "stakeholder", "domain", "synthesis", "report", "quality", "finalize")
PIPELINE_ROLES = ("tech", *NEEDS_TECH, "synthesis", "report")


def initial_control() -> dict[str, RoleControl]:
    return {role: RoleControl(role=role) for role in PIPELINE_ROLES}


def _attempts_left(control: RoleControl, policy, bonus: int = 0) -> bool:
    """남은 실행 예산.

    ``bonus`` 는 품질 평가 미달로 추가 지급되는 실행 횟수다. 근거 충분성 재작업과
    품질 재작성이 같은 예산을 나눠 쓰면, 근거 보강이 예산을 먼저 소진해 품질 루프가
    한 번도 돌지 못한다 (mock 점검에서 실제로 그렇게 됐다). 두 예산을 분리하되,
    총량은 ``max_attempts + max_quality_rounds`` 로 여전히 유한하다.
    """
    return control.attempts < policy.max_attempts + bonus


def _invalidate(
    control: dict[str, RoleControl], roles, policy, bonus: int = 0
) -> dict[str, RoleControl]:
    """상위 결과가 바뀌면 그에 기대던 하위 단계를 다시 돌 수 있게 pending 으로 되돌린다.

    예산이 없는 역할은 되돌리지 않는다. 되돌려 놓고 못 돌리면 라우팅이 제자리를 돈다.
    """
    updates: dict[str, RoleControl] = {}
    for role in roles:
        current = control.get(role)
        if current is None or current.status == "pending":
            continue
        if role in ("synthesis", "report") or _attempts_left(current, policy, bonus):
            updates[role] = current.model_copy(update={"status": "pending", "sufficient": False})
    return updates


def _reworkable(control: RoleControl, policy, bonus: int) -> bool:
    """재작업할 가치가 있는가. 예산이 남았고, 직전 재작업이 헛돌지 않았어야 한다."""
    if policy.stop_on_no_new_evidence and control.stalled:
        return False
    return _attempts_left(control, policy, bonus)


def decide(state, policy, now: float | None = None) -> tuple[str, list[str], str]:
    """현재 State 만 보고 (action, targets, reason) 을 고른다. 순수 함수 — 테스트 대상.

    ``now`` 를 주면 시간 예산 판정이 결정적이 된다(테스트용). 기본값은 현재 시각.
    """
    step = state.get("step", 0) + 1
    if step > state["max_steps"]:
        return "finalize", ["finalize"], f"스텝 상한 {state['max_steps']} 도달 — 종료"

    control = state.get("control", {})
    revisions_left = state.get("revision_round", 0) < state["max_revisions"]
    # 품질 미달로 되돌아온 라운드만큼 하위 에이전트의 실행 예산을 늘려 준다.
    bonus = state.get("quality_round", 0)
    # 시간 예산 소진 — 재작업만 멈춘다. 처음 실행·종합·보고서·품질 평가는 계속한다.
    deadline = state.get("deadline_at")
    over_budget = bool(deadline) and (now if now is not None else time.time()) > deadline
    revisions_left = revisions_left and not over_budget

    # ── 1. 기술 조사. 세 관점 평가의 입력이므로 결과가 없으면 여기부터. ──────────
    tech = control["tech"]
    if tech.status == "pending":
        return "dispatch", ["tech"], "기술 조사 결과 없음 — 기술 조사 에이전트 배정"
    if not tech.sufficient and revisions_left and _reworkable(tech, policy, bonus):
        return (
            "rework",
            ["tech"],
            f"기술 조사 근거 부족 (충분도 {tech.sufficiency}, 미해결 {len(tech.open_gaps)}건) — 재작업",
        )

    # ── 2. 세 관점. 서로 독립이므로 한 스텝에 함께 보낸다 (동시 쓰기 → reducer). ──
    pending = [r for r in NEEDS_TECH if control[r].status == "pending"]
    runnable = [r for r in pending if _attempts_left(control[r], policy, bonus)]
    if runnable:
        return "dispatch", runnable, f"미수집 관점 {', '.join(runnable)} — 병렬 배정"
    if pending:
        # Fall-back: 예산을 소진한 관점은 제외하고 남은 근거로 진행한다.
        return (
            "skip",
            pending,
            f"관점 {', '.join(pending)} 시도 한도 소진 — 제외하고 진행",
        )

    weak = [
        r
        for r in NEEDS_TECH
        if not control[r].sufficient and _reworkable(control[r], policy, bonus)
    ]
    if weak and revisions_left:
        detail = ", ".join(f"{r}({control[r].sufficiency})" for r in weak)
        return "rework", weak, f"근거 부족 관점 재작업 — {detail}"

    # ── 3. 근거 충분성 판정이 끝난 뒤에야 종합·보고서로 넘어간다. ────────────────
    if control["synthesis"].status == "pending":
        short = [r for r in PIPELINE_ROLES[:4] if not control[r].sufficient]
        stalled = [r for r in PIPELINE_ROLES[:4] if control[r].stalled]
        note = f" (미해결 유지: {', '.join(short)})" if short else ""
        if stalled:
            note += f" / 재작업해도 근거 증가 없음: {', '.join(stalled)}"
        if over_budget:
            note += " / 시간 예산 소진으로 추가 재작업 중단"
        return "synthesize", ["synthesis"], f"네 관점 근거 충분성 판정 완료 — 종합{note}"
    if control["report"].status == "pending":
        return "report", ["report"], "종합 완료 — 보고서 작성"

    # ── 4. 보고서 생성 후 품질 평가, 미달이면 루프. ────────────────────────────
    verdict = state.get("quality")
    if verdict is None:
        return "quality", ["quality"], "보고서 초안 완성 — 품질 평가 수행"
    if not verdict.passed:
        if over_budget:
            return (
                "finalize",
                ["finalize"],
                "시간 예산 소진 — 품질 미달 항목을 보고서에 남기고 종료: "
                + ", ".join(verdict.failed_criteria()),
            )
        if state.get("quality_round", 0) >= state["max_quality_rounds"]:
            return (
                "finalize",
                ["finalize"],
                "품질 재작성 한도 소진 — 미달 항목을 보고서에 남기고 종료: "
                + ", ".join(verdict.failed_criteria()),
            )
        targets = [
            r
            for r in verdict.remediation_roles
            # 지금 쓰려는 라운드까지 더해 예산을 본다. 헛돈 역할은 다시 보내지 않는다.
            if r == "report" or _reworkable(control.get(r, RoleControl(role=r)), policy, bonus + 1)
        ]
        if not targets:
            return (
                "finalize",
                ["finalize"],
                "품질 미달이나 재작업 예산 없음 — 미달 항목을 남기고 종료: "
                + ", ".join(verdict.failed_criteria()),
            )
        return (
            "rework",
            targets,
            f"품질 미달 ({', '.join(verdict.failed_criteria())}) — {', '.join(targets)} 재작업",
        )

    return "finalize", ["finalize"], "품질 평가 통과 — 종료"


def make_supervisor(settings):
    """Supervisor 노드. 분기 결정과 그에 따른 제어 상태 갱신만 한다."""
    policy = settings.supervisor

    def supervise(state, config: RunnableConfig):
        log = log_from_config(state, config)
        step = state.get("step", 0) + 1
        action, targets, reason = decide(state, policy)
        control = state.get("control", {})
        delta: dict = {"step": step}

        if action == "rework":
            research = [r for r in targets if r in PIPELINE_ROLES[:4]]
            updates = {
                role: control[role].model_copy(update={"status": "pending", "sufficient": False})
                for role in targets
                if role in control
            }
            # 상위 결과가 다시 만들어지면 그에 기대던 하위 단계도 다시 돈다.
            downstream = list(NEEDS_TECH) if "tech" in targets else []
            downstream += ["synthesis", "report"]
            bonus = state.get("quality_round", 0) + (1 if state.get("quality") is not None else 0)
            updates.update(_invalidate({**control, **updates}, downstream, policy, bonus))
            delta["control"] = updates
            delta["quality"] = None
            if state.get("quality") is not None:
                delta["quality_round"] = state.get("quality_round", 0) + 1
            elif research:
                delta["revision_round"] = state.get("revision_round", 0) + 1
            delta["route"] = [r for r in targets if r in ROUTES]
        elif action == "skip":
            delta["control"] = {
                role: control[role].model_copy(update={"status": "skipped"}) for role in targets
            }
            delta["route"] = ["supervisor_again"]
        elif action == "finalize":
            delta["route"] = ["finalize"]
            delta["stop_reason"] = reason
        else:
            delta["route"] = targets

        delta["decisions"] = [
            log.record(step=step, action=_log_action(action), targets=targets, reason=reason)
        ]
        return delta

    return supervise


def _log_action(action: str) -> str:
    """내부 분기 이름을 결정 로그의 어휘로 맞춘다."""
    return {"skip": "dispatch", "synthesize": "synthesize"}.get(action, action)


def route(state) -> list[str]:
    """``add_conditional_edges`` 가 쓰는 라우터. 제어 필드 하나만 읽는다.

    리스트를 돌려주면 그 노드들이 **병렬로** 실행된다. 순서는 코드에 없다.
    """
    chosen = state.get("route") or ["finalize"]
    if chosen == ["supervisor_again"]:
        return ["supervisor"]
    return [r for r in chosen if r in ROUTES] or ["finalize"]

"""Supervisor: 하위 에이전트를 동적으로 라우팅하는 조정 계층.

가이드 B 의 Supervisor 필수 항목이 여기 전부 들어 있다.

  * 하위 에이전트는 Supervisor 하고만 통신한다 — 모든 간선이 supervisor ↔ worker 다.
  * 순서를 하드코딩하지 않는다 — :func:`decide` 는 **현재 State(수집된 관점, 근거 충분도)**
    만 읽어 다음 배정을 만들고, ``add_conditional_edges`` 가 그 결과를 따른다.
  * 스텝 수를 고정하지 않는다 — 근거가 충분해졌다고 판정될 때까지 재작업을 돌고,
    충분해진 뒤에야 종합·보고서로 넘어간다.
  * 근거 부족이면 **그 하위 에이전트에게만** 재작업을 지시한다.

배정의 단위는 역할이 아니라 **작업 항목**(:class:`WorkItem`)이다. 한 스텝에 나가는 배정은
``Send`` 로 각각 자기 범위를 들고 떠나며, **개수와 범위가 실행마다 달라진다** — 처음에는
역할 × 기술로 펼치고, 재작업에서는 근거가 부족하다고 판정된 (기술, 항목)만 담는다.
역할 전체를 다시 돌리지 않으므로 재작업이 싸지고, 돌아온 조각은 reducer 가 (기술, 항목)
단위로 합친다.

충분성 판정은 **합쳐진 결과**로만 할 수 있으므로 여기서 한다. 조각 하나를 받은 하위
에이전트는 자기 역할이 충분한지 알 수 없다 (가이드 C "제어 vs 페이로드 분리").

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
from langgraph.types import Send

from agents.observability import log_from_config
from agents.state import RoleControl, WorkItem
from agents.sufficiency import assess
from agents.workers import NEEDS_TECH

# 이 그래프가 가질 수 있는 분기 대상 전체. add_conditional_edges 의 path_map 과 같다.
ROUTES = ("tech", "market", "stakeholder", "domain", "synthesis", "report", "quality", "finalize")
RESEARCH_ROLES = ("tech", *NEEDS_TECH)
PIPELINE_ROLES = (*RESEARCH_ROLES, "synthesis", "report")
# 기술별로 쪼개지 않는 역할. 보고서는 두 기술을 나란히 조판하는 것이 일이고,
# 품질 평가와 마무리는 문서 하나를 본다.
#
# 종합은 쪼갠다. 판정 단위가 (기술, 항목)이고 질문도 그렇게 주어지는데, 한 번의 호출로
# 두 기술을 다 맡기면 한쪽이 통째로 빠진다 — live 실행에서 종합이 KIVI 주장만 2건 쓰고
# ITME 는 하나도 쓰지 않아, 근거와 무관하게 ITME 의 종합 판정이 보류됐다. 관점 간 대조에
# 필요한 상위 결과는 배정 범위와 무관하게 전부 넘어가므로 비교는 그대로 할 수 있다.
WHOLE_ROLES = ("report", "quality", "finalize")


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


def _reworkable(control: RoleControl, policy, bonus: int) -> bool:
    """재작업할 가치가 있는가. 예산이 남았고, 직전 재작업이 헛돌지 않았어야 한다."""
    if policy.stop_on_no_new_evidence and control.stalled:
        return False
    return _attempts_left(control, policy, bonus)


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


def plan_items(role: str, control: RoleControl, technologies, reason: str, *, first: bool):
    """배정 목록을 만든다. 개수는 여기서 **실행 중에** 정해진다.

    처음에는 기술마다 하나씩 펼치고, 재작업에서는 근거가 부족한 (기술, 항목)만 담는다.
    같은 역할의 KIVI 조각과 ITME 조각은 서로 독립이므로 동시에 나간다.
    """
    attempt = control.attempts + 1
    if role in WHOLE_ROLES:
        return [WorkItem(role=role, reason=reason, attempt=attempt)]
    if first or not control.open_items:
        return [
            WorkItem(role=role, technologies=[tech], reason=reason, attempt=attempt)
            for tech in technologies
        ]
    by_tech: dict[str, list[str]] = {}
    for item in control.open_items:
        by_tech.setdefault(item.technology, []).append(item.criterion)
    return [
        WorkItem(
            role=role,
            technologies=[tech],
            criteria=sorted(set(criteria)),
            reason=reason,
            attempt=attempt,
        )
        for tech, criteria in sorted(by_tech.items())
    ]


def decide(state, policy, now: float | None = None) -> tuple[str, list[WorkItem], str]:
    """현재 State 만 보고 (action, 배정 목록, 사유) 를 고른다. 순수 함수 — 테스트 대상.

    ``now`` 를 주면 시간 예산 판정이 결정적이 된다(테스트용). 기본값은 현재 시각.
    """
    step = state.get("step", 0) + 1
    technologies = list(state["target_techs"].values())

    def whole(role, reason):
        return [WorkItem(role=role, reason=reason, attempt=1)]

    if step > state["max_steps"]:
        return "finalize", whole("finalize", ""), f"스텝 상한 {state['max_steps']} 도달 — 종료"

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
        reason = "기술 조사 결과 없음 — 기술 조사 에이전트 배정"
        return "dispatch", plan_items("tech", tech, technologies, reason, first=True), reason
    if not tech.sufficient and revisions_left and _reworkable(tech, policy, bonus):
        reason = f"기술 조사 근거 부족 (충분도 {tech.sufficiency}, 미해결 {len(tech.open_gaps)}건) — 재작업"
        return "rework", plan_items("tech", tech, technologies, reason, first=False), reason

    # ── 2. 세 관점. 서로 독립이므로 한 스텝에 함께 보낸다 (동시 쓰기 → reducer). ──
    pending = [r for r in NEEDS_TECH if control[r].status == "pending"]
    runnable = [r for r in pending if _attempts_left(control[r], policy, bonus)]
    if runnable:
        reason = f"미수집 관점 {', '.join(runnable)} — 기술별로 병렬 배정"
        items = [
            item
            for role in runnable
            for item in plan_items(role, control[role], technologies, reason, first=True)
        ]
        return "dispatch", items, reason
    if pending:
        # Fall-back: 예산을 소진한 관점은 제외하고 남은 근거로 진행한다.
        return (
            "skip",
            [WorkItem(role=role) for role in pending],
            f"관점 {', '.join(pending)} 시도 한도 소진 — 제외하고 진행",
        )

    weak = [
        r
        for r in NEEDS_TECH
        if not control[r].sufficient and _reworkable(control[r], policy, bonus)
    ]
    if weak and revisions_left:
        detail = ", ".join(f"{r}({control[r].sufficiency})" for r in weak)
        reason = f"근거 부족 관점 재작업 — {detail}"
        items = [
            item
            for role in weak
            for item in plan_items(role, control[role], technologies, reason, first=False)
        ]
        return "rework", items, f"{reason} / 부족 항목 {len(items)}건으로 분할"

    # ── 3. 근거 충분성 판정이 끝난 뒤에야 종합·보고서로 넘어간다. ────────────────
    if control["synthesis"].status == "pending":
        short = [r for r in RESEARCH_ROLES if not control[r].sufficient]
        stalled = [r for r in RESEARCH_ROLES if control[r].stalled]
        note = f" (미해결 유지: {', '.join(short)})" if short else ""
        if stalled:
            note += f" / 재작업해도 근거 증가 없음: {', '.join(stalled)}"
        if over_budget:
            note += " / 시간 예산 소진으로 추가 재작업 중단"
        reason = f"네 관점 근거 충분성 판정 완료 — 종합{note}"
        items = plan_items("synthesis", control["synthesis"], technologies, reason, first=True)
        return "synthesize", items, reason
    if control["report"].status == "pending":
        reason = "종합 완료 — 보고서 작성"
        return "report", whole("report", reason), reason

    # ── 4. 보고서 생성 후 품질 평가, 미달이면 루프. ────────────────────────────
    verdict = state.get("quality")
    if verdict is None:
        reason = "보고서 초안 완성 — 품질 평가 수행"
        return "quality", whole("quality", reason), reason
    if not verdict.passed:
        failed = ", ".join(verdict.failed_criteria())
        if over_budget:
            return (
                "finalize",
                whole("finalize", ""),
                f"시간 예산 소진 — 품질 미달 항목을 보고서에 남기고 종료: {failed}",
            )
        if state.get("quality_round", 0) >= state["max_quality_rounds"]:
            return (
                "finalize",
                whole("finalize", ""),
                f"품질 재작성 한도 소진 — 미달 항목을 보고서에 남기고 종료: {failed}",
            )
        targets = [
            role
            for role in verdict.remediation_roles
            # 지금 쓰려는 라운드까지 더해 예산을 본다. 헛돈 역할은 다시 보내지 않는다.
            if role == "report"
            or _reworkable(control.get(role, RoleControl(role=role)), policy, bonus + 1)
        ]
        if not targets:
            return (
                "finalize",
                whole("finalize", ""),
                f"품질 미달이나 재작업 예산 없음 — 미달 항목을 남기고 종료: {failed}",
            )
        reason = f"품질 미달 ({failed}) — {', '.join(targets)} 재작업"
        items = [
            item
            for role in targets
            for item in plan_items(role, control[role], technologies, reason, first=False)
        ]
        return "rework", items, reason

    return "finalize", whole("finalize", ""), "품질 평가 통과 — 종료"


def refresh_sufficiency(state, inputs, settings) -> dict[str, RoleControl]:
    """합쳐진 결과로 역할별 근거 충분도를 다시 판정한다.

    조각을 받은 하위 에이전트는 자기 역할이 충분한지 알 수 없으므로 여기서 계산한다.
    재작업 대기(``pending``)로 되돌려 둔 역할은 건드리지 않는다 — 과거 결과로 판정하면
    방금 내린 재작업 결정이 되살아난 상태에 덮여 사라진다.
    """
    control = state.get("control", {})
    updates: dict[str, RoleControl] = {}
    for role, run in state.get("results", {}).items():
        prior = control.get(role)
        if role not in inputs or prior is None or prior.status == "pending":
            continue
        fresh = assess(role, run, inputs[role], settings)
        found = len(run.evidence)
        updates[role] = fresh.model_copy(
            update={
                "attempts": prior.attempts,
                "artifact": prior.artifact,
                "evidence_count": found,
                # 수확 체감: 재작업했는데 근거가 늘지 않았다.
                "stalled": prior.attempts > 1 and found <= prior.evidence_count,
                "last_error": fresh.last_error or prior.last_error,
            }
        )
    return updates


def make_supervisor(settings, inputs):
    """Supervisor 노드. 충분성 재판정과 분기 결정, 그에 따른 제어 상태 갱신만 한다."""
    policy = settings.supervisor

    def supervise(state, config: RunnableConfig):
        log = log_from_config(state, config)
        step = state.get("step", 0) + 1
        # 돌아온 조각들이 합쳐진 상태에서 먼저 충분성을 다시 판정한다.
        refreshed = refresh_sufficiency(state, inputs, settings)
        control = {**state.get("control", {}), **refreshed}
        action, items, reason = decide({**state, "control": control}, policy)
        delta: dict = {"step": step, "control": dict(refreshed)}

        if action in ("dispatch", "rework"):
            # 배정한 역할의 시도 횟수는 Supervisor 가 올린다. 같은 역할의 조각 여러 개가
            # 각자 올리면 한 라운드가 여러 번으로 세어진다.
            for item in items:
                prior = control[item.role]
                delta["control"][item.role] = prior.model_copy(
                    update={"status": "pending", "sufficient": False, "attempts": item.attempt}
                )
            if action == "rework":
                targets = {item.role for item in items}
                research = targets & set(RESEARCH_ROLES)
                downstream = list(NEEDS_TECH) if "tech" in targets else []
                # 다시 돌 **이유가 있는 것만** 되돌린다. 중립성 미달처럼 보고서의 서술만
                # 고치면 되는 경우까지 평가 종합을 다시 돌리면, 입력이 같으니 같은 결과를
                # 한 번 더 비싸게 만들 뿐이다.
                if research:
                    downstream += ["synthesis", "report"]
                bonus = state.get("quality_round", 0) + (
                    1 if state.get("quality") is not None else 0
                )
                delta["control"].update(
                    _invalidate({**control, **delta["control"]}, downstream, policy, bonus)
                )
                delta["quality"] = None
                if state.get("quality") is not None:
                    delta["quality_round"] = state.get("quality_round", 0) + 1
                elif research:
                    delta["revision_round"] = state.get("revision_round", 0) + 1
            delta["route"] = items
        elif action == "skip":
            for item in items:
                delta["control"][item.role] = control[item.role].model_copy(
                    update={"status": "skipped"}
                )
            delta["route"] = [WorkItem(role="supervisor")]
        elif action == "finalize":
            delta["route"] = items
            delta["stop_reason"] = reason
        else:
            delta["route"] = items

        delta["decisions"] = [
            log.record(
                step=step,
                action=_log_action(action),
                targets=[item.label() for item in items],
                reason=reason,
                extra={"fan_out": len(items)},
            )
        ]
        return delta

    return supervise


def _log_action(action: str) -> str:
    """내부 분기 이름을 결정 로그의 어휘로 맞춘다."""
    return {"skip": "dispatch"}.get(action, action)


def worker_payload(state, item: WorkItem) -> dict:
    """``Send`` 로 하위 에이전트에게 넘기는 입력.

    공유 State 를 통째로 복사해 보내지 않는다. 배정 하나가 실제로 읽는 것만 싣는다 —
    자기 배정, 상위 결과, 제어 메타, 상관 키. 배정이 여러 개면 payload 도 그만큼
    만들어지므로, 여기 담는 것이 곧 비용이다.

    누적 근거 풀(``sources``)과 확정 TRL 은 보고서를 다루는 역할에만 싣는다. 조사
    에이전트는 자기가 찾은 근거만 쓰고 풀 전체를 읽지 않지만, 품질 평가는 보고서를
    조판해 봐야 하므로 둘 다 필요하다. 이 역할들은 한 번에 하나씩만 배정되므로
    실어도 복사가 늘지 않는다.
    """
    payload = {
        "assignment": item,
        "results": state.get("results", {}),
        "control": state.get("control", {}),
        "target_techs": state["target_techs"],
        "domain": state["domain"],
        "step": state.get("step", 0),
        "trace_id": state.get("trace_id", ""),
        "run_id": state.get("run_id", ""),
    }
    if item.role in WHOLE_ROLES:
        payload["sources"] = state.get("sources", [])
        payload["trl_result"] = state.get("trl_result", {})
        payload["quality_round"] = state.get("quality_round", 0)
    return payload


def route(state):
    """``add_conditional_edges`` 가 쓰는 라우터. 제어 필드 하나만 읽는다.

    ``Send`` 를 배정마다 하나씩 돌려준다. 개수는 Supervisor 가 실행 중에 정했고 코드에
    고정된 값이 아니다 — 처음 배정은 역할 × 기술, 재작업은 부족한 항목 수만큼이다.
    """
    items = state.get("route") or []
    if not items:
        return ["finalize"]
    if items[0].role in ("finalize", "supervisor"):
        return [items[0].role]
    return [Send(item.role, worker_payload(state, item)) for item in items if item.role in ROUTES]

"""같은 역할에 대한 부분 결과를 하나로 합친다.

Supervisor 는 역할이 아니라 **작업 항목**(역할 × 기술)을 배정하므로, 한 역할의 결과가
여러 조각으로 나뉘어 동시에 도착한다. 이 모듈이 그 조각들을 합치는 규칙이고,
``agents.state.merge_results`` reducer 가 이것을 쓴다 (가이드 C "동시 처리").

합치는 규칙은 하나다 — **이번에 다시 만든 (기술, 항목)만 교체하고 나머지는 보존한다.**
재작업이 ``stakeholder × ITME`` 만 돌렸다면 KIVI 쪽 판정과 근거는 그대로 남아야 하고,
ITME 쪽은 낡은 것이 아니라 새 것이어야 한다. 단순히 이어 붙이면 같은 항목의 옛 판정과
새 판정이 함께 남아 보고서에 두 번 실리고, 통째로 덮으면 다른 기술의 조사 결과가 사라진다.

주장 ID 는 조각마다 따로 생성되므로 겹칠 수 있다. 겹치면 새로 들어온 쪽의 이름을 바꾸고
그 주장을 가리키던 판정(ClaimCheck)도 같이 고친다. ID 는 인용의 대상이므로 조용히
덮어쓰면 검증 연결이 끊긴다.
"""

from __future__ import annotations

from rag.evidence import merge_evidence
from schemas.contracts import NodeRun

# 나쁜 쪽이 이긴다. 한 조각이라도 실패했으면 역할 전체를 성공으로 보고하지 않는다.
_RANK = {"completed": 0, "needs_revision": 1, "failed": 2}


def _addressed(run: NodeRun) -> set[tuple[str, str]]:
    """이 조각이 다시 만든 (기술, 평가 항목). 교체 범위를 정한다."""
    return {(a.technology, a.criterion) for a in run.result.assessments}


def _rename_collisions(current: NodeRun, incoming: NodeRun) -> NodeRun:
    """새 조각의 주장 ID 가 기존과 겹치면 이름을 바꾸고 판정도 따라 고친다."""
    taken = {c.id for c in current.result.claims}
    clashes = {c.id for c in incoming.result.claims if c.id in taken}
    if not clashes:
        return incoming
    out = incoming.model_copy(deep=True)
    rename = {}
    for claim in out.result.claims:
        if claim.id in clashes:
            new = f"{claim.technology}-{claim.id}"
            while new in taken:
                new = f"x{new}"
            rename[claim.id] = new
            taken.add(new)
            claim.id = new
    for check in out.checks:
        if check.claim_id in rename:
            check.claim_id = rename[check.claim_id]
    out.validation_errors = [
        f"{rename[old]}{error[len(old) :]}" if (old := error.split(":")[0]) in rename else error
        for error in out.validation_errors
    ]
    return out


def _keep(items, addressed):
    return [item for item in items if (item.technology, item.criterion) not in addressed]


def merge_runs(current: NodeRun | None, incoming: NodeRun) -> NodeRun:
    """한 역할의 기존 결과에 새 조각을 얹는다. 교체는 (기술, 항목) 단위."""
    if current is None:
        return incoming
    if current.node != incoming.node:
        raise ValueError(f"Cannot merge {current.node} with {incoming.node}")

    incoming = _rename_collisions(current, incoming)
    addressed = _addressed(incoming)
    redone_techs = {tech for tech, _ in addressed}

    result = current.result.model_copy(deep=True)
    result.assessments = _keep(result.assessments, addressed) + incoming.result.assessments
    kept_claims = _keep(result.claims, addressed)
    dropped = {c.id for c in result.claims} - {c.id for c in kept_claims}
    result.claims = kept_claims + incoming.result.claims
    # 다시 추정한 기술의 TRL 은 새 값으로 바꾼다. 다른 기술 것은 건드리지 않는다.
    result.trl_estimates = [
        t for t in result.trl_estimates if t.technology not in redone_techs
    ] + incoming.result.trl_estimates
    # 다시 만든 항목을 가리키던 미확인 기록은 낡은 것이다. 그대로 두면 해결된 항목이
    # 보고서 6장에 계속 남는다. 그 항목을 가리키지 않는 기록은 보존한다.
    stale = tuple(f"{tech}/{criterion}" for tech, criterion in addressed)
    result.unverified = [
        note for note in result.unverified if not any(key in note for key in stale)
    ] + incoming.result.unverified
    result.limitations = list(
        dict.fromkeys(
            [note for note in result.limitations if not any(key in note for key in stale)]
            + incoming.result.limitations
        )
    )
    result.summary = "\n".join(
        part for part in (current.result.summary, incoming.result.summary) if part
    )

    redone_questions = {c.question_id for c in incoming.coverage}
    return NodeRun(
        node=current.node,
        mode=current.mode,
        status=max((current.status, incoming.status), key=lambda s: _RANK[s]),
        result=result,
        evidence=merge_evidence(current.evidence, incoming.evidence),
        # 사라진 주장을 가리키던 판정은 함께 버린다. 대상 없는 판정은 검증 기록이 아니다.
        checks=[c for c in current.checks if c.claim_id not in dropped] + incoming.checks,
        validation_errors=list(
            dict.fromkeys(
                [e for e in current.validation_errors if e.split(":")[0] not in dropped]
                + incoming.validation_errors
            )
        ),
        searches=current.searches + incoming.searches,
        prompt_hash=incoming.prompt_hash or current.prompt_hash,
        model=incoming.model or current.model,
        verdict=incoming.verdict,
        fix_count=current.fix_count + incoming.fix_count,
        coverage=[c for c in current.coverage if c.question_id not in redone_questions]
        + incoming.coverage,
    )

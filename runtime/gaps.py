"""근거 부족 판정 정책. 어느 역할의 무엇을 더 조사해야 하는지 센다.

보고서 조판과 함께 두면 "보고서를 만드는 코드"로 읽히지만, 실제로는 조정 계층이
재작업 대상을 고를 때와 보고서 6장이 한계를 적을 때 둘 다 쓰는 규칙이다.
"""

from __future__ import annotations

from schemas.contracts import Gap


def collect_gaps(runs, settings):
    """역할별 보완 대상. 같은 사유가 여러 번 쌓이면 보고서 6장이 같은 줄로 채워진다."""
    gaps = []
    for role, run in runs.items():
        items = run.result.assessments
        unknown = [a for a in items if a.judgment == "확인 불가"]
        if not items or len(unknown) / len(items) >= settings.gap_policy.unknown_fraction:
            gaps.append(
                Gap(
                    role=role,
                    criterion="coverage",
                    reason="확인 불가 항목 비율이 기준 이상이거나 평가 항목 없음",
                )
            )
        for a in unknown:
            if a.criterion in settings.gap_policy.critical_criteria.get(role, []):
                gaps.append(
                    Gap(
                        role=role,
                        criterion=f"{a.technology}/{a.criterion}",
                        reason="핵심 항목 확인 불가",
                    )
                )
        for reason in run.validation_errors + run.result.unverified:
            gaps.append(Gap(role=role, criterion="verification", reason=reason))
        questions = {r.question_id for r in run.searches}
        for question in sorted(questions):
            intents = {r.intent for r in run.searches if r.question_id == question and not r.error}
            if not {"positive", "critical"}.issubset(intents):
                gaps.append(Gap(role=role, criterion=question, reason="긍정·비판 양쪽 검색 미완료"))
        if not questions:
            gaps.append(Gap(role=role, criterion="search", reason="검색 이력 없음"))
    unique, seen = [], set()
    for gap in gaps:
        key = (gap.role, gap.criterion, gap.reason)
        if key in seen:
            continue
        seen.add(key)
        unique.append(gap)
    return unique

"""근거 충분성 평가: 하위 에이전트의 결과(페이로드)를 Supervisor 의 제어 메타로 요약한다.

가이드 B 의 "Supervisor 가 근거 충분성을 평가한 후에 최종 보고서 작성이 진행" 과
"근거 부족이면 해당 하위 에이전트에게 재작업 요청" 이 여기서 계산되는 값으로 결정된다.
라우팅 함수는 ``NodeRun`` 을 직접 읽지 않고 이 모듈이 만든 :class:`RoleControl` 만 본다
(가이드 C "제어 vs 페이로드 분리").

판정은 네 축의 **코드 규칙**이며 LLM 이 아니다. 라우팅이 비결정적이면 재현성이 깨지고,
"몇 번 재작업했는가"를 트레이스와 대조할 수 없기 때문이다. 내용의 타당성은 보고서 뒤
품질 평가(``agents/quality.py``)에서 Hybrid 로 따로 본다.

  1. 항목 충족     요청된 (기술, 기준) 쌍 중 '확인 불가'가 아닌 판정이 나온 비율
  2. 출처 다양성   기술마다 서로 다른 문서/사이트가 ``min_sources_per_tech`` 건 이상인가
  3. 양면 검색     질문마다 긍정·비판 검색이 모두 실제로 수행됐는가 (확증편향 통제)
  4. 검증 상태     노드가 스스로 남긴 ``validation_errors`` · ``unverified`` 가 있는가

1·2·3 을 평균한 값이 ``sufficiency`` 이고, 기준치 미만이거나 핵심 항목이 비면 부족이다.
"""

from __future__ import annotations

from agents.state import OpenItem, RoleControl
from schemas.contracts import NodeInput, NodeRun


def _expected_pairs(data: NodeInput) -> set[tuple[str, str]]:
    """그 역할에게 실제로 요청한 (기술, 기준) 쌍. 루브릭 전체가 아니라 입력이 기준이다."""
    return {(q.technology, q.criterion) for q in data.questions}


def _resolved_pairs(run: NodeRun) -> set[tuple[str, str]]:
    return {
        (a.technology, a.criterion) for a in run.result.assessments if a.judgment != "확인 불가"
    }


def _source_key(evidence) -> str:
    """같은 논문의 다른 청크, 같은 사이트의 다른 페이지는 하나의 출처로 센다."""
    return evidence.document_id or evidence.site or evidence.url or evidence.id


def _diversity(run: NodeRun, technologies: set[str], minimum: int) -> tuple[float, list[str]]:
    scores, gaps = [], []
    for tech in sorted(technologies):
        keys = {_source_key(e) for e in run.evidence if e.technology in (tech, "other")}
        independent = any(
            e.affiliation == "independent" for e in run.evidence if e.technology in (tech, "other")
        )
        scores.append(min(len(keys) / minimum, 1.0) if minimum else 1.0)
        if len(keys) < minimum:
            gaps.append(f"{tech}/sources")
        # 자사 자료만으로 채워진 관점은 확증편향 위험이 크다. 점수가 아니라 gap 으로 남긴다.
        if keys and not independent:
            gaps.append(f"{tech}/independent-source")
    return (sum(scores) / len(scores) if scores else 0.0), gaps


def _both_sided(run: NodeRun) -> tuple[float, list[str]]:
    questions = {r.question_id for r in run.searches}
    if not questions:
        return 0.0, ["search/none"]
    covered, gaps = 0, []
    for question in sorted(questions):
        intents = {r.intent for r in run.searches if r.question_id == question and not r.error}
        if {"positive", "critical"}.issubset(intents) or "fixture" in intents:
            covered += 1
        else:
            gaps.append(f"{question}/one-sided")
    return covered / len(questions), gaps


def assess(role: str, run: NodeRun, data: NodeInput, settings) -> RoleControl:
    """하위 에이전트 결과 하나를 제어 메타로 요약한다."""
    policy = settings.supervisor
    technologies = set(settings.target_techs.values())
    expected = _expected_pairs(data)
    resolved = _resolved_pairs(run)

    completeness = len(expected & resolved) / len(expected) if expected else 0.0
    diversity, diversity_gaps = _diversity(run, technologies, policy.min_sources_per_tech)
    balance, balance_gaps = _both_sided(run)

    missing = sorted(expected - resolved)
    gaps = [f"{tech}/{criterion}" for tech, criterion in missing]
    gaps += diversity_gaps + balance_gaps

    # 핵심 항목(config 의 gap_policy.critical_criteria)이 비면 점수와 무관하게 부족이다.
    critical = set(settings.gap_policy.critical_criteria.get(role, []))
    critical_open = sorted(
        f"{tech}/{criterion}" for tech, criterion in expected - resolved if criterion in critical
    )
    gaps += [f"critical:{item}" for item in critical_open]

    score = (completeness + diversity + balance) / 3
    sufficient = run.status != "failed" and score >= policy.min_sufficiency and not critical_open
    return RoleControl(
        role=role,
        status=run.status,
        sufficiency=round(score, 3),
        sufficient=sufficient,
        open_gaps=sorted(dict.fromkeys(gaps)),
        # 재작업 배정의 단위. 출처 다양성·양면 검색 부족은 특정 항목에 묶이지 않으므로
        # 그 기술의 모든 미해결 항목으로 대신한다 (없으면 그 기술 전체를 다시 본다).
        open_items=[OpenItem(technology=tech, criterion=criterion) for tech, criterion in missing],
        last_error=(run.validation_errors[0] if run.validation_errors else None),
    )


def rework_feedback(control: RoleControl, item=None) -> str:
    """재작업 지시문. 하위 에이전트 입력의 description 에 덧붙인다.

    배정(``item``)이 있으면 그 범위의 부족 항목만 적는다. 다른 기술·항목까지 적으면
    이번에 맡지도 않은 일을 보강하라는 지시가 된다.
    """
    gaps = control.open_gaps
    if item is not None and item.technologies:
        scope = set(item.technologies)
        gaps = [g for g in gaps if g.split("/")[0] in scope]
    if not gaps:
        return ""
    return (
        "\nSupervisor 재작업 지시: 아래 항목의 근거가 부족하다고 판단했습니다. "
        f"(충분도 {control.sufficiency}) 해당 항목의 근거를 우선 보강하고, "
        "근거를 찾지 못하면 추측하지 말고 확인 불가로 남기세요. 부족 항목: " + ", ".join(gaps[:12])
    )

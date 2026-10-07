"""보고서 품질 평가 노드 (가이드 D, 평가 방식 3안 = Hybrid).

보고서가 만들어진 **뒤에** 네 항목을 평가하고, 미달이면 Supervisor 가 루프를 돌린다.

    Groundedness   주장이 검색된 출처로 추적되는가
    중립성          특정 기술 추천/우열 판정이 없는가
    편향 통제        단일 출처·유리한 근거 편중이 없는가
    관점 커버리지     기술 성숙도·시장성·이해관계자·도메인 4개 관점을 포괄하는가

두 층으로 본다.

  1층 코드 규칙 (결정적)  인용 ID 가 실제 출처 목록에 있는가, 금지 표현이 있는가,
      기술마다 서로 다른 출처가 몇 건인가, 네 관점의 절이 실제로 채워졌는가.
      형식·연결은 LLM 없이 재현 가능하게 본다.
  2층 LLM Judge (내용)   1층이 통과시킨 보고서의 **문장**을 보고, 표현을 바꾼 우열 판정이나
      한쪽 근거에 기운 서술처럼 규칙으로 못 잡는 것을 본다.

한 항목이라도 어느 층에서 불합격하면 그 항목은 불합격이다 (AND 결합). 1층이 떨어뜨린
항목은 2층에 묻지 않는다 — 이미 결론이 정해졌고 호출만 낭비이기 때문이다.
mock 모드처럼 Judge 를 쓸 수 없으면 1층 결과만으로 판정하고 ``judge_available=False`` 로
남겨, "검사하지 않은 것"과 "통과한 것"을 구분한다.
"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig

from agents.observability import log_from_config
from agents.report_view import RESULT_KEYS, flat_state
from agents.state import CriterionVerdict, QualityVerdict
from runtime.reporting import RECOMMENDATION_PHRASES, assemble_report, report_sources

# 관점 커버리지의 4개 관점 ↔ 보고서 절 ↔ 하위 에이전트 역할.
PERSPECTIVES = {
    "기술 성숙도": ("tech", "overview"),
    "시장성": ("market", "market"),
    "이해관계자": ("stakeholder", "stakeholder"),
    "도메인 적용": ("domain", "domain"),
}
# 규칙으로 잡는 우열·추천 표현. 보고서 조립기의 금지 표현에 비교 표현을 더한다.
COMPARATIVE_PHRASES = RECOMMENDATION_PHRASES + (
    "우수하다",
    "유리하다고 판단",
    "최적의 선택",
    "앞선다",
    "뒤처진다",
    "승자",
)


def _source_key(evidence) -> str:
    return evidence.document_id or evidence.site or evidence.url or evidence.id


def check_groundedness(state, settings, sources) -> CriterionVerdict:
    """보고서가 인용한 ID 가 전부 실제 출처 목록으로 추적되는가."""
    known = {e.id for e in sources}
    report = state["results"]["report"]
    cited = [c for c in report.result.claims]
    if not cited:
        return CriterionVerdict(
            criterion="groundedness",
            passed=False,
            reason="보고서 주장이 하나도 없어 출처 추적이 불가능합니다.",
            source="rule",
        )
    traced = [c for c in cited if c.evidence_ids and set(c.evidence_ids) <= known]
    ratio = len(traced) / len(cited)
    # 등급(assessment)도 확인 불가가 아니면 근거를 끌고 있어야 한다.
    ungrounded = [
        f"{a.technology}/{a.criterion}"
        for a in report.result.assessments
        if a.judgment != "확인 불가" and not (set(a.evidence_ids) <= known and a.evidence_ids)
    ]
    threshold = settings.supervisor.quality.min_groundedness
    passed = ratio >= threshold and not ungrounded
    reason = f"주장 {len(traced)}/{len(cited)}건이 출처 ID 로 추적됨 (기준 {threshold})"
    if ungrounded:
        reason += f"; 근거 없는 등급: {', '.join(ungrounded[:6])}"
    return CriterionVerdict(criterion="groundedness", passed=passed, reason=reason, source="rule")


def check_neutrality(state, text) -> CriterionVerdict:
    """추천·우열 표현 검출. 보고서 본문과 LLM 이 쓴 SUMMARY·주장을 모두 본다."""
    report = state["results"]["report"]
    targets = [text, report.result.summary, *(c.text for c in report.result.claims)]
    hits = sorted({phrase for phrase in COMPARATIVE_PHRASES if any(phrase in t for t in targets)})
    return CriterionVerdict(
        criterion="neutrality",
        passed=not hits,
        reason="추천·우열 표현 없음" if not hits else f"검출된 표현: {', '.join(hits)}",
        source="rule",
    )


def check_bias_control(state, settings, sources) -> CriterionVerdict:
    """단일 출처 의존과 유리한 근거 편중을 본다."""
    policy = settings.supervisor
    problems = []
    for tech in settings.target_techs.values():
        pool = [e for e in sources if e.technology in (tech, "other")]
        keys = {_source_key(e) for e in pool}
        if len(keys) < policy.min_sources_per_tech:
            problems.append(
                f"{tech}: 서로 다른 출처 {len(keys)}건 (기준 {policy.min_sources_per_tech})"
            )
        if pool and not any(e.affiliation == "independent" for e in pool):
            problems.append(f"{tech}: 독립 출처 없음(자사·미분류 자료만)")
    stances = {e.stance for e in sources}
    if not ({"positive", "critical"} <= stances or "mixed" in stances):
        problems.append("긍정·비판 양쪽 입장의 원문이 확인되지 않음")
    return CriterionVerdict(
        criterion="bias_control",
        passed=not problems,
        reason="단일 출처·편중 없음" if not problems else "; ".join(problems[:6]),
        source="rule",
    )


def check_coverage(state, text) -> CriterionVerdict:
    """4개 관점이 보고서에 실제로 채워졌는가 (절 존재 + 그 관점의 등급 존재)."""
    results = state.get("results", {})
    report = results.get("report")
    missing = []
    for label, (role, criterion) in PERSPECTIVES.items():
        run = results.get(role)
        if run is None or run.status == "failed" or not run.result.assessments:
            missing.append(f"{label}(조사 결과 없음)")
            continue
        if report is None or not any(a.criterion == criterion for a in report.result.assessments):
            missing.append(f"{label}(보고서 절 미구성)")
    for marker in ("# 3. 기술 개요", "# 4. 관점별 평가"):
        if marker not in text:
            missing.append(f"{marker} 절 누락")
    return CriterionVerdict(
        criterion="coverage",
        passed=not missing,
        reason="4개 관점 모두 구성됨" if not missing else f"미구성: {', '.join(missing)}",
        source="rule",
    )


def _remediation(verdict: QualityVerdict) -> list[str]:
    """미달 항목을 '누구에게 되돌릴지'로 번역한다.

    근거 자체가 부족한 항목(groundedness·편향 통제)은 조사 에이전트에게,
    서술의 문제(중립성)는 보고서 에이전트에게 돌린다. 관점 커버리지는 비어 있는
    관점의 담당 에이전트에게 돌리고, 그 관점이 멀쩡하면 보고서 구성 문제다.
    """
    failed = set(verdict.failed_criteria())
    roles: list[str] = []
    if "bias_control" in failed or "groundedness" in failed:
        roles += ["market", "stakeholder", "domain"]
    if "coverage" in failed:
        roles += [role for role, _ in PERSPECTIVES.values()]
    if "neutrality" in failed or not roles:
        roles.append("report")
    return sorted(dict.fromkeys(roles))


def make_quality_node(settings, backend, mode: str):
    """보고서 품질 평가 노드를 만든다. 결과는 State 의 ``quality`` 에 들어간다."""

    def evaluate_quality(state, config: RunnableConfig):
        log = log_from_config(state, config)
        view = flat_state(state, settings, mode)
        text = assemble_report(view, RESULT_KEYS, mode)
        sources = report_sources(view, RESULT_KEYS)

        checks = [
            check_groundedness(state, settings, sources),
            check_neutrality(state, text),
            check_bias_control(state, settings, sources),
            check_coverage(state, text),
        ]

        # 2층: 1층을 통과한 항목만 내용 판정을 받는다.
        judge_available = settings.supervisor.quality.judge and hasattr(backend, "quality_review")
        if judge_available:
            askable = [c.criterion for c in checks if c.passed]
            review = backend.quality_review(text, askable, used_sources=sources)
            by_criterion = {item.criterion: item for item in review}
            checks = [
                c
                if not c.passed or c.criterion not in by_criterion
                else _combine(c, by_criterion[c.criterion])
                for c in checks
            ]

        verdict = QualityVerdict(
            passed=all(c.passed for c in checks),
            checks=checks,
            judge_available=bool(judge_available),
        )
        verdict.remediation_roles = [] if verdict.passed else _remediation(verdict)
        log.record(
            step=state.get("step", 0),
            node="quality",
            action="quality",
            targets=verdict.remediation_roles,
            reason=(
                "품질 평가 통과"
                if verdict.passed
                else "품질 미달: " + ", ".join(verdict.failed_criteria())
            ),
            extra={
                "round": state.get("quality_round", 0),
                "judge": bool(judge_available),
                "checks": [c.model_dump() for c in checks],
            },
        )
        return {"quality": verdict, "report_text": text}

    return evaluate_quality


def _combine(rule: CriterionVerdict, judge: CriterionVerdict) -> CriterionVerdict:
    """AND 결합. 규칙이 통과시켜도 Judge 가 떨어뜨리면 불합격이다."""
    if judge.passed:
        return rule.model_copy(update={"source": "both"})
    return CriterionVerdict(
        criterion=rule.criterion,
        passed=False,
        reason=f"규칙 통과 / Judge 불합격: {judge.reason}",
        source="judge",
    )

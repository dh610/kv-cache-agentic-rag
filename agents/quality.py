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
from runtime.report import RECOMMENDATION_PHRASES, assemble_report, report_sources

# 관점 커버리지의 4개 관점 ↔ 보고서 절 ↔ 하위 에이전트 역할.
PERSPECTIVES = {
    "기술 성숙도": ("tech", "overview"),
    "시장성": ("market", "market"),
    "이해관계자": ("stakeholder", "stakeholder"),
    "도메인 적용": ("domain", "domain"),
}
# 규칙으로 잡는 우열·추천 표현. 보고서 조립기의 금지 표현에 비교 표현을 더한다.
# 근거를 더 모아야 고쳐지는 미달의 책임 역할.
RESEARCH_OWNERS = ("market", "stakeholder", "domain")
# 2층 Judge 에게 묻지 않는 항목. 관점 커버리지는 "네 관점을 다뤘는가"라는 **구성**의
# 문제라 코드가 끝까지 판정할 수 있다. 내용까지 물으면 Judge 가 확인 불가가 많다는
# 이유로 미달을 내는데(live 실행에서 실제로 그랬다), 확인 불가는 이 설계에서 정상
# 산출이지 결함이 아니다. 내용 수준의 품질은 groundedness·편향 통제가 본다.
RULE_ONLY = ("coverage",)
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
    # 원인에 따라 고칠 사람이 다르다. 인용이 출처 목록에 없으면 근거를 더 모아야 하지만,
    # 근거 없이 등급을 쓴 것은 보고서 노드가 확인 불가로 내렸어야 할 일이다. 조사
    # 에이전트를 다시 돌려도 고쳐지지 않는다 (live 실행에서 그렇게 한 라운드를 버렸다).
    owners = ["report"] if ungrounded else list(RESEARCH_OWNERS)
    return CriterionVerdict(
        criterion="groundedness", passed=passed, reason=reason, source="rule", owners=owners
    )


def _worse(first: CriterionVerdict, second: CriterionVerdict) -> CriterionVerdict:
    """두 조판 중 나쁜 쪽을 택한다. 어느 본에서 걸렸는지는 사유에 남는다."""
    return first if not first.passed else second


def check_neutrality(state, text, where: str = "") -> CriterionVerdict:
    """추천·우열 표현 검출. 보고서 본문과 LLM 이 쓴 SUMMARY·주장을 모두 본다."""
    report = state["results"]["report"]
    targets = [text, report.result.summary, *(c.text for c in report.result.claims)]
    hits = sorted({phrase for phrase in COMPARATIVE_PHRASES if any(phrase in t for t in targets)})
    return CriterionVerdict(
        criterion="neutrality",
        passed=not hits,
        reason=(
            "추천·우열 표현 없음"
            if not hits
            else f"{where + ' ' if where else ''}검출된 표현: {', '.join(hits)}"
        ),
        source="rule",
        owners=["report"],
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
    # 유리한 근거만 모았는지는 "무엇을 찾으러 갔는가"로 잰다. 질문마다 긍정·비판 질의를
    # 쌍으로 내는데, 한쪽만 실제로 수행됐다면 그 질문의 근거는 한쪽으로 기운 것이다.
    #
    # 자료가 실제로 비판적인가(Evidence.stance)로 재지 않는 이유: 그 값은 사람이
    # source_annotations.yaml 에 적어야 채워진다. live 실행에서 수집한 367건이 전부
    # unknown 이었고, 그대로 두면 이 항목은 사람이 손대기 전까지 영원히 미달이다.
    # 항상 미달인 검사는 엄격한 것이 아니라 아무것도 알려주지 않는다. 내용 수준의 편중
    # ("비판 자료가 있는데도 긍정만 인용했는가")은 2층 Judge 가 본문을 읽고 판정한다.
    one_sided = _one_sided_questions(state)
    if one_sided:
        problems.append(f"한쪽 질의만 수행된 질문 {len(one_sided)}건: {', '.join(one_sided[:4])}")
    unclassified = sum(1 for e in sources if e.stance == "unknown")
    note = (
        f" / 출처 {unclassified}건은 입장 미분류 — 검색 수행과 자료의 실제 입장은 다르다"
        if unclassified
        else ""
    )
    return CriterionVerdict(
        criterion="bias_control",
        passed=not problems,
        reason=("단일 출처·편중 없음" if not problems else "; ".join(problems[:6])) + note,
        source="rule",
        owners=list(RESEARCH_OWNERS),
    )


def _one_sided_questions(state) -> list[str]:
    """긍정·비판 중 한쪽 검색만 성공한 질문. 고정 근거(fixture)는 양면 대상이 아니다."""
    out = []
    for role, _ in PERSPECTIVES.values():
        run = state.get("results", {}).get(role)
        if run is None:
            continue
        for question in sorted({r.question_id for r in run.searches}):
            intents = {r.intent for r in run.searches if r.question_id == question and not r.error}
            if "fixture" in intents or {"positive", "critical"} <= intents:
                continue
            out.append(question)
    return out


def check_coverage(state, text, where: str = "") -> CriterionVerdict:
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
        reason=(
            "4개 관점 모두 구성됨"
            if not missing
            else f"{where + ' ' if where else ''}미구성: {', '.join(missing)}"
        ),
        source="rule",
        # 비어 있는 관점의 담당자. 전부 차 있는데도 미달이면 보고서 구성 문제다.
        owners=sorted({role for label, (role, _) in PERSPECTIVES.items() if label in str(missing)})
        or ["report"],
    )


def _remediation(verdict: QualityVerdict) -> list[str]:
    """미달 항목을 '누구에게 되돌릴지'로 번역한다.

    판정한 쪽이 ``owners`` 로 지목한 역할을 그대로 쓴다. 같은 항목이라도 원인에 따라
    고칠 사람이 다르기 때문이다 — 인용이 출처에 없으면 근거를 더 모아야 하지만,
    근거 없이 등급을 쓴 것은 보고서 노드가 확인 불가로 내렸어야 할 일이다.
    """
    roles = [owner for check in verdict.checks if not check.passed for owner in check.owners]
    # 지목이 없으면 보고서 작성자에게 돌린다. 책임을 못 정한 미달을 조사 에이전트에게
    # 보내면 고칠 수 없는 일을 시키는 셈이다.
    return sorted(dict.fromkeys(roles)) or ["report"]


def make_quality_node(settings, backend, mode: str):
    """보고서 품질 평가 노드를 만든다. 결과는 State 의 ``quality`` 에 들어간다."""

    def evaluate_quality(state, config: RunnableConfig):
        log = log_from_config(state, config)
        view = flat_state(state, settings, mode)
        text = assemble_report(view, RESULT_KEYS, mode)
        # 사람이 받는 것은 제출본이다. 전체본만 검사하면 압축 조판이 떨어뜨린 문장은
        # 아무도 보지 않은 채 나간다. 본문을 읽는 검사는 둘 다 보고, 한쪽이라도
        # 걸리면 불합격으로 본다.
        submission = assemble_report(view, RESULT_KEYS, mode, compact=True)
        sources = report_sources(view, RESULT_KEYS)

        checks = [
            check_groundedness(state, settings, sources),
            _worse(check_neutrality(state, text), check_neutrality(state, submission, "제출본")),
            check_bias_control(state, settings, sources),
            _worse(check_coverage(state, text), check_coverage(state, submission, "제출본")),
        ]

        # 2층: 1층을 통과한 항목만 내용 판정을 받는다.
        judge_available = settings.supervisor.quality.judge and hasattr(backend, "quality_review")
        unanswered: list[str] = []
        if judge_available:
            askable = [c.criterion for c in checks if c.passed and c.criterion not in RULE_ONLY]
            review = backend.quality_review(text, askable, used_sources=sources)
            by_criterion = {item.criterion: item for item in review}
            # 물었는데 답이 오지 않은 항목은 "통과"가 아니라 "검사 미완료"다. 규칙 판정을
            # 그대로 두면 2층을 돌린 적 없는 결과가 돌린 것처럼 보고된다.
            unanswered = [criterion for criterion in askable if criterion not in by_criterion]
            checks = [
                _unjudged(c)
                if c.criterion in unanswered
                else c
                if not c.passed or c.criterion not in by_criterion
                else _combine(c, by_criterion[c.criterion])
                for c in checks
            ]

        verdict = QualityVerdict(
            passed=all(c.passed for c in checks),
            checks=checks,
            # 일부라도 답이 오지 않았으면 2층이 끝까지 돌지 않은 것이다.
            judge_available=bool(judge_available) and not unanswered,
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
                "unjudged": unanswered,
                "checks": [c.model_dump() for c in checks],
            },
        )
        return {"quality": verdict, "report_text": text}

    return evaluate_quality


def _unjudged(rule: CriterionVerdict) -> CriterionVerdict:
    """물었지만 답이 오지 않은 항목. 규칙 판정은 남기되 내용 판정은 안 했다고 적는다."""
    return rule.model_copy(
        update={"reason": f"{rule.reason} / Judge 응답 없음 — 내용 판정 미수행", "source": "rule"}
    )


def _combine(rule: CriterionVerdict, judge: CriterionVerdict) -> CriterionVerdict:
    """AND 결합. 규칙이 통과시켜도 Judge 가 떨어뜨리면 불합격이다.

    책임 소재는 규칙 쪽 판정을 따른다. Judge 는 내용을 읽고 불합격을 낼 수는 있어도
    조직 안에서 누가 그것을 고치는지는 모른다.
    """
    if judge.passed:
        return rule.model_copy(update={"source": "both"})
    return CriterionVerdict(
        criterion=rule.criterion,
        passed=False,
        reason=f"규칙 통과 / Judge 불합격: {judge.reason}",
        source="judge",
        owners=rule.owners,
    )

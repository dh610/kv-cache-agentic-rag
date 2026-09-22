"""Deterministic report assembly and final validation; generated files are not approval.

The report node's LLM output (claims per section, SUMMARY, limitations, per-section
coverage assessments) is placed into the design's E.2 outline here. Chapters 1-2, the
TRL table, comparison tables, 6.2 and REFERENCE are code-rendered from State and the
fixed prose in prompts/report/sections.j2, as design E.3 specifies.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from xml.sax.saxutils import escape

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from rag.evidence import merge_evidence
from runtime.handoff import reference_issues
from runtime.settings import ROOT
from schemas.contracts import Gap

# Design E.1: SUMMARY must stay within half a page. Counted on the LLM summary only.
SUMMARY_MAX_CHARS = 600
# Guide E: the report never recommends a technology or ranks the two.
RECOMMENDATION_PHRASES = (
    "추천한다",
    "추천합니다",
    "권장한다",
    "권장합니다",
    "더 우수",
    "더 낫다",
    "더 나은 선택",
    "우위에 있",
    "선택해야",
)

LABELS = {
    "mechanism": "원리",
    "maturity": "성숙도",
    "limitations": "기술 한계",
    "growth": "시장 규모·성장성",
    "adoption": "상용화·채택",
    "ecosystem": "생태계 지지",
    "competitors": "경쟁 기술 진영",
    "adopters": "도입 기업·개발자",
    "industry": "투자·업계",
    "cost": "비용(TCO)",
    "performance": "성능",
    "quality": "품질(정확도)",
    "operations": "도입·운영 난이도",
    "scalability": "확장성",
    "consistency": "관점 간 일치·상충",
    "implications": "조건부 시사점",
}
ROLE_LABELS = {
    "tech": "기술 성숙도",
    "market": "시장성",
    "stakeholder": "이해관계자",
    "domain": "도메인 적용",
    "synthesis": "평가 종합",
    "report": "보고서 생성",
}
# Report rubric criterion -> report section it fills (design E.3).
REPORT_SECTIONS = {
    "overview": "3.1/3.2 기술 개요",
    "market": "4.1 시장성",
    "stakeholder": "4.2 이해관계자",
    "domain": "4.3 도메인 적용",
    "implications": "5 시사점",
}


def used_ids(run):
    return {
        eid
        for item in [*run.result.claims, *run.result.assessments, *run.result.trl_estimates]
        for eid in item.evidence_ids
    }


def report_sources(state, result_keys):
    """Keep the cumulative ledger, but only publish citations in current results."""
    used = set().union(*(used_ids(state[key]) for key in result_keys.values()))
    for estimate in state.get("trl_result", {}).values():
        used.update(estimate.get("evidence_ids", []))
    return [e for e in merge_evidence(state["sources"]) if e.id in used]


def collect_gaps(runs, settings):
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
    return gaps


def reference_text(e):
    """Guide E / design E.4 formats. Fixture excerpts are never a final reference.

    논문: 저자(YYYY). 논문제목. 학술지/학회명, 권(호), 페이지.
    특허: 출원인(YYYY-MM). 특허명, 특허번호/공개번호, URL
    웹:   기관명 또는 작성자(YYYY-MM-DD). 제목. 사이트명, URL
    """
    if e.source_type == "paper":
        author = e.authors or "저자 미확인"
        venue = e.venue or "게재 정보 미확인"
        return (
            f"{author}({e.year or '연도 미확인'}). {e.title}. {venue}, "
            f"{e.citation_id or '권(호)·페이지 미확인'}."
        )
    if e.source_type == "patent":
        applicant = e.publisher or "출원인 미확인"
        return (
            f"{applicant}({e.published_at or '출원연월 미확인'}). {e.title}, "
            f"{e.citation_id or '특허번호 미확인'}, {e.url}"
        )
    if e.source_type == "web":
        # 발행 기관을 못 받은 페이지가 많다. 사이트 도메인은 지어낸 정보가 아니라
        # URL 에서 확인되는 발행 주체이므로 기관명 자리에 쓴다. 발행일은 추정하지 않고,
        # 대신 우리가 실제로 아는 수집일을 함께 적는다.
        author = e.publisher or e.authors or site_operator(e.site) or "기관/작성자 미확인"
        site = e.site or "사이트 미확인"
        when = e.published_at or (
            f"발행일 미확인, 수집 {e.retrieved_at[:10]}" if e.retrieved_at else "발행일 미확인"
        )
        return f"{author}({when}). {e.title}. {site}, {e.url}"
    return f"개발용 고정 발췌(제출 불가). {e.title}. {e.url}"


def site_operator(host: str | None) -> str:
    """도메인에서 발행 주체 이름을 만든다. 예: www.solidigmtechnology.kr -> Solidigm Technology."""
    if not host:
        return ""
    generic = {"www", "m", "blog", "news", "docs", "doc", "developer", "dev", "support", "en", "ko"}
    parts = [p for p in host.lower().split(".") if p not in generic]
    if not parts:
        return ""
    name = parts[0]
    for suffix in ("technology", "research", "labs", "group", "news", "tech"):
        if name.endswith(suffix) and len(name) > len(suffix) + 2:
            name = f"{name[: -len(suffix)]} {suffix}"
            break
    return name.replace("-", " ").title()


def affiliation_marker(e):
    return {"first_party": "[자사 자료]", "independent": "[독립 자료]"}.get(
        e.affiliation, "[출처 관계 미분류]"
    )


TYPE_ORDER = {"paper": 0, "patent": 1, "web": 2, "fixture": 3}


def _reference_groups(sources):
    groups: dict[tuple, list] = {}
    for e in merge_evidence(sources):
        groups.setdefault((e.source_type, e.document_id or e.url), []).append(e)
    return sorted(
        groups.values(), key=lambda items: (TYPE_ORDER[items[0].source_type], items[0].title)
    )


def reference_entries(sources):
    """One bibliographic entry per document; cited chunk IDs and pages stay traceable."""
    ordered = _reference_groups(sources)
    lines = []
    for number, items in enumerate(ordered, 1):
        first = items[0]
        cited = ", ".join(f"[{e.id}]" + (f" p.{e.page}" if e.page else "") for e in items)
        # The paper format carries no URL, so the source location follows the entry.
        origin = f" 원문: {first.url}" if first.source_type == "paper" else ""
        lines.append(
            f"{number}. {reference_text(first)} {affiliation_marker(first)}{origin} 인용: {cited}"
        )
    return lines


INTERNAL = re.compile(
    r"\s*[\(\[]\s*E\d+(\s*[,;·]\s*E\d+)*\s*[\)\]]"  # (E1, E3) 같은 임시 근거 번호
    r"|\b(tech|market|stakeholder|domain|synthesis|report)/[A-Za-z]+/[a-z]+\s*:\s*"  # 내부 키
)


def strip_internal(value: str) -> str:
    """모델이 사유에 섞어 쓴 내부 표기를 지운다.

    E 번호는 프롬프트에서만 쓰는 임시 근거 번호이고 `tech/KIVI/overview:` 는 노드 내부
    키다. 독자에게는 의미가 없고, 인용은 REFERENCE 번호로 따로 붙는다.
    """
    return re.sub(r"\s{2,}", " ", INTERNAL.sub("", value)).strip()


def _cell(text):
    return strip_internal(text.replace("|", "/").replace("\n", " "))


# 본문 인용을 REFERENCE 번호로 바꾸기 위한 현재 보고서의 지도. assemble_report 가 채운다.
_CITATIONS: dict[str, str] = {}


def citation_numbers(sources) -> dict[str, tuple[int, int | None]]:
    """근거 ID → (REFERENCE 번호, 쪽). 번호와 묶음은 REFERENCE 목록과 같은 순서를 쓴다."""
    out = {}
    for number, items in enumerate(_reference_groups(sources), 1):
        for e in items:
            out[e.id] = (number, e.page)
    return out


def _ids(ids):
    """긴 원문 ID 대신 REFERENCE 번호로 인용한다. 같은 문서의 쪽은 한 번호로 묶는다."""
    if not ids:
        return ""
    pages: dict[int, list[int]] = {}
    unknown = []
    for i in ids:
        if i not in _CITATIONS:
            unknown.append(i)
            continue
        number, page = _CITATIONS[i]
        slot = pages.setdefault(number, [])
        if page and page not in slot:
            slot.append(page)
    parts = [
        f"{number} p.{','.join(str(p) for p in sorted(slot))}" if slot else str(number)
        for number, slot in sorted(pages.items())
    ]
    return f" [{'; '.join(parts + unknown)}]" if parts or unknown else ""


def _unique_claim_lines(claims, shown):
    lines = []
    for c in claims:
        if c.text in shown:
            continue
        shown.add(c.text)
        lines.append(_claim_line(c))
    return lines


def _claim_line(c):
    line = f"- {c.technology}: {c.text}{_ids(c.evidence_ids)}"
    if c.conditions:
        line += " 조건: " + "; ".join(c.conditions)
    return _cell(line)


def _basis(assessment, evidence) -> str:
    """설계서 A.4: 평가 단위는 접근 전반이므로 칸마다 판정 기준과 직접 근거 유무를 함께 적는다."""
    if assessment.judgment == "확인 불가":
        return "기준 없음, 직접 근거 없음"
    scopes = {e.scope for e in evidence if e.id in set(assessment.evidence_ids)}
    direct = "직접 근거 있음" if "target" in scopes else "직접 근거 없음"
    basis = "선정 기술 기준" if scopes == {"target"} else "접근 전반 기준"
    return f"{basis}, {direct}"


def _assessment_table(run, techs):
    criteria = list(dict.fromkeys(a.criterion for a in run.result.assessments))
    rows = [f"| 항목 | {' | '.join(techs)} |", f"| --- |{' --- |' * len(techs)}"]
    for criterion in criteria:
        cells = []
        for tech in techs:
            a = next(
                (
                    a
                    for a in run.result.assessments
                    if a.technology == tech and a.criterion == criterion
                ),
                None,
            )
            cells.append(
                _cell(
                    f"{a.judgment} ({_basis(a, run.evidence)}): {a.rationale}{_ids(a.evidence_ids)}"
                )
                if a
                else "확인 불가 (기준 없음, 직접 근거 없음): 결과 누락"
            )
        rows.append(f"| {LABELS.get(criterion, criterion)} | {' | '.join(cells)} |")
    return rows


def _narrative(report_run, criterion, tech, shown=None):
    """Report-node sentences for one section/technology plus its coverage judgment.

    같은 내용을 보고서 노드와 역할 노드가 각각 써내는 경우가 많다. shown 에 이미 실은
    문장을 모아 두면 절 안에서 같은 문장을 두 번 싣지 않는다.
    """
    seen = shown if shown is not None else set()
    lines = []
    a = next(
        (
            a
            for a in report_run.result.assessments
            if a.technology == tech and a.criterion == criterion
        ),
        None,
    )
    status = (
        f"{a.judgment}. {a.rationale}{_ids(a.evidence_ids)}"
        if a
        else "확인 불가. 보고서 노드 결과 누락"
    )
    lines.append(_cell(f"{tech} 절 구성: {status}"))
    claims = [
        c for c in report_run.result.claims if c.criterion == criterion and c.technology == tech
    ]
    for c in claims:
        if c.text in seen:
            continue  # 같은 문장을 여러 주장으로 나눠 써도 본문에는 한 번만 싣는다.
        seen.add(c.text)
        lines.append(_claim_line(c))
    if not claims:
        lines.append(f"- {tech}: 확인 불가. 이 절에 인용 가능한 보고서 문장이 없음")
    return lines


def _control_rows(state, result_keys, techs):
    """Design C.7 check items that code can count; the rest stays with human review."""
    rows = [
        "| 노드 | 긍정·비판 양쪽 검색 완료 질문 | 출처 없는 등급 · 인용 검증 · 수정 · 확인 불가 |",
        "| --- | --- | --- |",
    ]
    for role in ("tech", "market", "stakeholder", "domain"):
        run = state[result_keys[role]]
        questions = sorted({r.question_id for r in run.searches})
        both = sum(
            1
            for q in questions
            if {"positive", "critical"}
            <= {r.intent for r in run.searches if r.question_id == q and not r.error}
        )
        labels = Counter(c.label for c in run.checks)
        items = run.result.assessments
        unknown = sum(1 for a in items if a.judgment == "확인 불가")
        unsourced = sum(1 for a in items if a.judgment != "확인 불가" and not a.evidence_ids)
        rows.append(
            f"| {ROLE_LABELS[role]} | {both}/{len(questions)} | "
            f"출처 없는 등급 {unsourced}건; supported {labels['supported']}/{sum(labels.values())}; "
            f"수정 {run.fix_count}회; 확인 불가 {unknown}/{len(items)}; 상태 {run.status} |"
        )
    return rows


def fixed_sections():
    env = Environment(
        loader=FileSystemLoader(ROOT / "prompts"),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env.get_template("report/sections.j2").module


def assemble_report(state, result_keys, mode):
    global _CITATIONS
    _CITATIONS = citation_numbers(report_sources(state, result_keys))
    techs = list(state["target_techs"].values())
    sw = state["target_techs"].get("sw", techs[0])
    hw = state["target_techs"].get("hw", techs[-1])
    sections = fixed_sections()
    report = state[result_keys["report"]]
    synthesis = state[result_keys["synthesis"]]
    tech_run = state[result_keys["tech"]]

    lines = [
        "# SUMMARY",
        report.result.summary,
        f"자동 생성 초안 / 실행 모드: {mode}. 공개 정보 기반 추정. 사람의 원문·등급 검토가 필요합니다.",
        "# 1. 분석 배경",
        sections.background(domain=state["domain"], sw=sw, hw=hw),
        "# 2. 기술 선정",
        sections.selection(sw=sw, hw=hw),
        "# 3. 기술 개요",
    ]
    for number, tech in enumerate(techs, 1):
        lines.append(f"## 3.{number} {tech}")
        shown: set[str] = set()
        lines.extend(_narrative(report, "overview", tech, shown))
        lines.extend(
            _unique_claim_lines(
                [c for c in tech_run.result.claims if c.technology == tech], shown
            )
        )
    # Design E.1: the two-technology comparison table closes the section.
    lines.append(
        f"표 3-1 기술 조사 결과 비교 (기술 조사 노드 요약: {_cell(tech_run.result.summary)})"
    )
    lines.extend(_assessment_table(tech_run, techs))

    lines.append(f"## 3.{len(techs) + 1} 기술 성숙도(TRL)")
    lines.append(
        "TRL은 기술 조사 노드가 원문 근거로 잠정 추정하고 평가 종합 노드가 시장 직접 근거와 대조해 확정한다. "
        "모든 단계는 공개 정보 기반 추정이며, KV cache 기술은 논문 발표 시점과 실제 채택 사이에 시차가 있어 "
        "TRL 4~6 구간의 공개 정보가 특히 비어 있을 수 있다."
    )
    lines.append(f"| 항목 | {' | '.join(techs)} |")
    lines.append(f"| --- |{' --- |' * len(techs)}")
    provisional = []
    for tech in techs:
        a = next(
            (
                a
                for a in tech_run.result.assessments
                if a.technology == tech and a.criterion == "maturity"
            ),
            None,
        )
        provisional.append(
            _cell(f"{a.judgment}: {a.rationale}{_ids(a.evidence_ids)}" if a else "확인 불가")
        )
    lines.append(f"| 잠정 TRL (기술 조사) | {' | '.join(provisional)} |")
    final_levels, final_reasons = [], []
    for tech in techs:
        item = state["trl_result"].get(tech, {})
        level = item.get("level")
        final_levels.append(f"TRL {level}" if level is not None else "확인 불가")
        final_reasons.append(
            _cell(
                f"{item.get('rationale', '확정 TRL 근거 부족')}{_ids(item.get('evidence_ids', []))} "
                "공개 정보 기반 추정"
            )
        )
    lines.append(f"| 확정 TRL (평가 종합) | {' | '.join(final_levels)} |")
    lines.append(f"| 확정 사유·근거 | {' | '.join(final_reasons)} |")

    lines.append("# 4. 관점별 평가")
    lines.append(
        "각 절은 보고서 노드의 서술, 해당 평가 노드의 요약과 검증된 근거 문장, 절 끝의 두 기술 비교표 순서로 구성한다. "
        "등급은 관점별 평가이며 두 기술의 우열이 아니다."
    )
    for number, (role, heading) in enumerate(
        [("market", "시장성"), ("stakeholder", "이해관계자"), ("domain", "도메인 적용")], 1
    ):
        run = state[result_keys[role]]
        lines.append(f"## 4.{number} {heading}")
        shown = set()
        for tech in techs:
            lines.extend(_narrative(report, role, tech, shown))
        lines.append(f"{ROLE_LABELS[role]} 노드 요약: {_cell(run.result.summary)}")
        lines.extend(_unique_claim_lines(run.result.claims, shown))
        lines.append(f"표 4-{number} {heading} 비교")
        lines.extend(_assessment_table(run, techs))

    lines.append("# 5. 시사점")
    shown = set()
    for tech in techs:
        lines.extend(_narrative(report, "implications", tech, shown))
    lines.append("## 5.1 관점 간 상충 지점")
    lines.append(f"평가 종합 노드 요약: {_cell(synthesis.result.summary)}")
    for a in synthesis.result.assessments:
        if a.criterion == "consistency":
            lines.append(
                _cell(
                    f"- {a.technology} {LABELS['consistency']}: {a.judgment}. "
                    f"{a.rationale}{_ids(a.evidence_ids)}"
                )
            )
    lines.append("표 5-1 관점×기술 교차표")
    lines.append(f"| 관점 | {' | '.join(techs)} |")
    lines.append(f"| --- |{' --- |' * len(techs)}")
    for role in ("tech", "market", "stakeholder", "domain"):
        cells = []
        for tech in techs:
            cells.append(
                _cell(
                    "; ".join(
                        f"{LABELS.get(a.criterion, a.criterion)}: {a.judgment}"
                        for a in state[result_keys[role]].result.assessments
                        if a.technology == tech
                    )
                    or "확인 불가"
                )
            )
        lines.append(f"| {ROLE_LABELS[role]} | {' | '.join(cells)} |")
    lines.append("## 5.2 조건부 시사점과 보완 관계 가능성")
    for a in synthesis.result.assessments:
        if a.criterion != "consistency":
            lines.append(
                _cell(
                    f"- {a.technology} {LABELS.get(a.criterion, a.criterion)}: {a.judgment}. "
                    f"{a.rationale}{_ids(a.evidence_ids)}"
                )
            )
    lines.extend(_claim_line(c) for c in synthesis.result.claims)

    lines.append("# 6. 한계점")
    lines.append("## 6.1 정보의 한계")
    lines.append(
        "모든 판정은 공개 정보 기반 추정이다. 아래는 보고서 노드가 통합한 한계, "
        "코드가 기록한 근거 부족(gaps), 노드별 한계와 미확인 사항이다."
    )
    lines.extend(f"- {s}" for s in report.result.limitations)
    lines.extend(f"- gap {g.role}/{g.criterion}: {g.reason}" for g in state["gaps"])
    for role in ("tech", "market", "stakeholder", "domain", "synthesis"):
        run = state[result_keys[role]]
        lines.extend(f"- {ROLE_LABELS[role]} 한계: {s}" for s in run.result.limitations)
        lines.extend(f"- {ROLE_LABELS[role]} 미확인: {s}" for s in run.result.unverified)
    lines.extend(f"- 보고서 생성 미확인: {s}" for s in report.result.unverified)
    lines.append("## 6.2 확증편향 방지 조치")
    lines.append(sections.controls(mode=mode))
    lines.extend(_control_rows(state, result_keys, techs))

    lines.append("# REFERENCE")
    lines.append(
        "보고서 작성에 실제로 인용한 자료만 기재한다. 표기 형식: "
        "논문 저자(YYYY). 논문제목. 학술지/학회명, 권(호), 페이지. / "
        "특허 출원인(YYYY-MM). 특허명, 특허번호/공개번호, URL / "
        "웹페이지 기관명 또는 작성자(YYYY-MM-DD). 제목. 사이트명, URL"
    )
    entries = reference_entries(report_sources(state, result_keys))
    lines.extend(entries or ["인용된 출처 없음"])
    return "\n\n".join(lines) + "\n"


def report_text_issues(report_run):
    """Guide E constraints on the LLM-authored report material."""
    problems = []
    summary = report_run.result.summary
    if len(summary) > SUMMARY_MAX_CHARS:
        problems.append(
            f"SUMMARY가 1/2 페이지 한도({SUMMARY_MAX_CHARS}자)를 초과: {len(summary)}자"
        )
    texts = [summary, *(c.text for c in report_run.result.claims)]
    for phrase in RECOMMENDATION_PHRASES:
        if any(phrase in t for t in texts):
            problems.append(f"우열·추천 표현 검출: {phrase}")
    return problems


def validate_report(state, result_keys, text, mode):
    problems = []
    if mode == "mock":
        problems.append("mock은 제출 보고서가 아닙니다")
    evidence = {e.id: e for e in report_sources(state, result_keys)}
    for key in result_keys.values():
        run = state[key]
        if run.status != "completed":
            problems.append(f"{run.node}: {run.status}")
        for eid in used_ids(run):
            if eid not in evidence:
                problems.append(f"{run.node}: 인용 출처 누락 {eid}")
    for e in evidence.values():
        problems.extend(reference_issues(e))
        if e.affiliation == "unknown":
            problems.append(f"{e.id}: 출처 이해관계 미분류")
    for tech in state["target_techs"].values():
        if state["trl_result"][tech].get("level") is None:
            problems.append(f"{tech}: TRL 미확인")
    if state["gaps"]:
        problems.append("미해결 gaps가 있습니다")
    if not text.startswith("# SUMMARY") or "\n# REFERENCE\n" not in text:
        problems.append("SUMMARY/REFERENCE 형식 오류")
    problems.extend(report_text_issues(state[result_keys["report"]]))
    return {
        "ready": not problems,
        "problems": sorted(set(problems)),
        "human_review_required": True,
        "note": "형식·인용 연결 검사이며 사실성/평가 타당성의 최종 승인이 아님",
    }


def pdf_text(value: str) -> str:
    """CID 폰트에 없는 글자를 같은 뜻의 한글 문장부호로 바꾼다.

    가운뎃점 U+00B7 은 HYSMyeongJo-Medium 에서 엉뚱한 글리프(∬)로 찍힌다. 마크다운 원문은
    그대로 두고 조판할 때만 U+318D 로 바꾼다.
    """
    return escape(value).replace("\u00b7", "\u318d")


def write_report(text, output: Path, meta=None):
    """설계서 표지와 같은 구성으로 조판한다. 한국어 CID 폰트로 팀 환경 차이를 없앤다."""
    from datetime import date

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import (
        LongTable,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        TableStyle,
    )

    output.mkdir(parents=True, exist_ok=True)
    (output / "report.md").write_text(text, encoding="utf-8")
    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    ink, rule, band = (
        colors.HexColor("#1F2933"),
        colors.HexColor("#B9C2CC"),
        colors.HexColor("#EEF2F6"),
    )
    normal = ParagraphStyle(
        "body",
        fontName="HYSMyeongJo-Medium",
        fontSize=9.5,
        leading=15.5,
        wordWrap="CJK",
        alignment=TA_LEFT,
        textColor=ink,
        spaceAfter=7,
    )
    cell = ParagraphStyle("cell", parent=normal, fontSize=8.5, leading=12.5, spaceAfter=0)
    heading = ParagraphStyle(
        "heading",
        parent=normal,
        fontSize=15,
        leading=21,
        spaceBefore=20,
        spaceAfter=9,
        textColor=colors.HexColor("#14202B"),
        keepWithNext=True,
    )
    subheading = ParagraphStyle(
        "subheading",
        parent=normal,
        fontSize=11.5,
        leading=17,
        spaceBefore=13,
        spaceAfter=6,
        keepWithNext=True,
    )
    centered = ParagraphStyle("centered", parent=normal, alignment=TA_CENTER, spaceAfter=0)
    story = []
    if meta:
        big = ParagraphStyle("t", parent=centered, fontSize=20, leading=30)
        mid = ParagraphStyle("s", parent=centered, fontSize=12, leading=20)
        small = ParagraphStyle("m", parent=centered, fontSize=9.5, leading=17)
        label = " ".join("R A G - O U T P U T 평 가  산 출 물".split())
        story += [
            Spacer(1, 120),
            Paragraph(label, ParagraphStyle("l", parent=small, textColor=rule)),
            Spacer(1, 26),
            Paragraph(pdf_text(meta.subtitle), mid),
            Paragraph(pdf_text(meta.title), big),
            Paragraph(pdf_text(meta.lead), mid),
            Spacer(1, 60),
            Paragraph(pdf_text(f"캠퍼스 · 반 {meta.campus}"), small),
            Paragraph(pdf_text("조원 " + " · ".join(meta.members)), small),
            Paragraph(f"작성 {date.today():%Y년 %-m월 %-d일}", small),
            Spacer(1, 30),
            Paragraph(
                "자동 생성 초안입니다. 모든 판정은 공개 정보 기반 추정이며 사람의 검토가 필요합니다.",
                ParagraphStyle("d", parent=small, fontSize=8.5, textColor=rule),
            ),
            PageBreak(),
        ]
    rows = []

    def flush():
        if not rows:
            return
        columns = len(rows[0])
        first = min(80, 505 / columns)
        widths = (
            [505] if columns == 1 else [first] + [(505 - first) / (columns - 1)] * (columns - 1)
        )
        table = LongTable(rows, colWidths=widths, repeatRows=1, hAlign="LEFT", splitInRow=1)
        style = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, 0), band),
            ("LINEBELOW", (0, 0), (-1, 0), 0.7, rule),
            ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#DCE1E7")),
            ("LINEABOVE", (0, 0), (-1, 0), 0.7, rule),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ]
        table.setStyle(TableStyle(style))
        story.extend([Spacer(1, 2), table, Spacer(1, 12)])
        rows.clear()

    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("|"):
            if line.startswith("| ---"):
                continue
            style = cell
            rows.append([Paragraph(pdf_text(c.strip()), style) for c in line.strip("|").split("|")])
            continue
        flush()
        style = (
            subheading if line.startswith("## ") else heading if line.startswith("#") else normal
        )
        story.append(Paragraph(pdf_text(line.lstrip("# ").strip()), style))
    flush()
    path = output / "report.pdf"
    label = meta.title if meta else "평가 보고서"

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(rule)
        canvas.setLineWidth(0.3)
        canvas.line(40, 38, 555, 38)
        canvas.setFont("HYSMyeongJo-Medium", 7.5)
        canvas.setFillColor(rule)
        canvas.drawString(40, 26, label)
        canvas.drawRightString(555, 26, str(doc.page))
        canvas.restoreState()

    def cover(canvas, doc):
        return None

    SimpleDocTemplate(
        str(path), leftMargin=45, rightMargin=45, topMargin=48, bottomMargin=52, title=label
    ).build(story, onFirstPage=cover if meta else footer, onLaterPages=footer)
    return str(path)

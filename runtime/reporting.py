"""Deterministic report assembly and final validation; generated files are not approval.

The report node's LLM output (claims per section, SUMMARY, limitations, per-section
coverage assessments) is placed into the design's E.2 outline here. Chapters 1-2, the
TRL table, comparison tables, 6.2 and REFERENCE are code-rendered from State and the
fixed prose in prompts/report/sections.j2, as design E.3 specifies.
"""

from __future__ import annotations

import os
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


def _page_spans(items) -> str:
    """인용한 쪽을 연속 구간으로 묶는다. 청크 ID 는 산출물 JSON 에 그대로 남는다."""
    pages = sorted({e.page for e in items if e.page})
    if not pages:
        return f"인용 {len(items)}건"
    spans, start, prev = [], pages[0], pages[0]
    for page in pages[1:]:
        if page == prev + 1:
            prev = page
            continue
        spans.append((start, prev))
        start = prev = page
    spans.append((start, prev))
    joined = ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in spans)
    return f"인용 {len(items)}건 p.{joined}"


def reference_entries(sources):
    """One bibliographic entry per document; cited chunk IDs and pages stay traceable.

    압축 조판에서는 청크 ID 나열 대신 쪽 범위만 싣는다. 본문 인용이 이미 `[2 p.1]` 처럼
    REFERENCE 번호와 쪽으로 가리키므로 독자가 청크 ID 를 쓸 일이 없고, 이 나열이 보고서
    분량의 절반을 차지했다 (live 실측 52%). 추적은 nodes/*.json 의 evidence_ids 로 한다.
    """
    ordered = _reference_groups(sources)
    lines = []
    for number, items in enumerate(ordered, 1):
        first = items[0]
        cited = (
            _page_spans(items)
            if _COMPACT
            else "인용: "
            + ", ".join(f"[{e.id}]" + (f" p.{e.page}" if e.page else "") for e in items)
        )
        # The paper format carries no URL, so the source location follows the entry.
        origin = f" 원문: {first.url}" if first.source_type == "paper" else ""
        lines.append(
            f"{number}. {reference_text(first)} {affiliation_marker(first)}{origin} {cited}"
        )
    return lines


# 모델이 프롬프트의 임시 근거 번호(E1, E12 …)를 문장에 그대로 섞어 쓴다. 괄호에 묶인
# 형태뿐 아니라 "이는 E1, E12, E240 근거에 기반한" 처럼 맨몸으로 나열하거나 "E1~E375"
# 처럼 범위로 쓰기도 한다. 독자에게는 의미가 없고 인용은 REFERENCE 번호로 따로 붙는다.
_E_RUN = r"E\d+(?:\s*[,;·~]\s*E\d+)*"
INTERNAL = re.compile(
    rf"\s*(이는|이것은)?\s*{_E_RUN}\s*근거(?:\s*전반)?에\s*기반한\s*"  # 문장 속 나열
    rf"|\s*[\(\[]\s*{_E_RUN}\s*[\)\]]"  # (E1, E3) 같은 임시 근거 번호
    rf"|\s*\b{_E_RUN}\b"  # 남은 맨몸 번호
    r"|\b(tech|market|stakeholder|domain|synthesis|report)/[A-Za-z]+/[a-z_]+"
    r"(\s*[·,]\s*[a-z_]+)*\s*:?\s*"  # 내부 키 경로(나열 포함)
    # rubric 이 "market/기술/기준: 등급" 형식을 지시하는데, 모델이 그 형식 설명을
    # 그대로 베껴 적는 경우가 있다. 값이 아니라 틀이므로 독자에게는 의미가 없다.
    r"|\brole/technology/criterion\s*:?\s*"
)


def strip_internal(value: str) -> str:
    """모델이 사유에 섞어 쓴 내부 표기를 지운다.

    E 번호는 프롬프트에서만 쓰는 임시 근거 번호이고 `tech/KIVI/overview:` 는 노드 내부
    키다. 독자에게는 의미가 없고, 인용은 REFERENCE 번호로 따로 붙는다.
    """
    cleaned = INTERNAL.sub(" ", value)  # 지운 자리에 공백을 남겨 단어가 붙지 않게 한다
    cleaned = re.sub(r"\s+([.,;)\]])", r"\1", cleaned)  # 구두점 앞에 생긴 공백은 거둔다
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _cell(text):
    return strip_internal(text.replace("|", "/").replace("\n", " "))


# 본문 인용을 REFERENCE 번호로 바꾸기 위한 현재 보고서의 지도. assemble_report 가 채운다.
_CITATIONS: dict[str, str] = {}
# 압축 조판 여부. 과제 규칙의 10장 한도를 코드로 지키기 위한 것이며, assemble_report 가 채운다.
# 줄이는 것은 "같은 내용의 반복"뿐이고, 판정·근거·조건·출처는 표와 REFERENCE 에 그대로 남는다.
_COMPACT = False
# 이번 조판에서 본문이 실제로 인용한 근거 ID. _ids 가 채우고 압축 조판이 읽는다.
_CITED: set[str] = set()
# 절·기술당 본문에 싣는 주장 문장 수. 나머지는 산출물 JSON 에 남는다.
COMPACT_CLAIMS = 1
# 한계점에서 같은 분류로 싣는 줄 수.
COMPACT_BULLETS = 1
# 한 문장·한 칸이 본문에 드러내는 REFERENCE 번호의 수. 판정 하나가 근거를 열 건 넘게
# 달고 있어서, 이 제한이 없으면 REFERENCE 가 본문보다 길어진다 (live 실측: 본문 7.3쪽에
# REFERENCE 11.7쪽, 177건). 가린 인용은 산출물 JSON 의 evidence_ids 에 그대로 있다.
COMPACT_CITATIONS = 2


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
    by_number: dict[int, list[str]] = {}
    unknown = []
    for i in ids:
        if i not in _CITATIONS:
            unknown.append(i)
            continue
        number, page = _CITATIONS[i]
        by_number.setdefault(number, []).append(i)
        slot = pages.setdefault(number, [])
        if page and page not in slot:
            slot.append(page)
    shown = sorted(pages.items())
    hidden = 0
    if _COMPACT:
        # 압축 조판은 두 번 돌고, 두 번째에는 목록이 좁아져 있다. 거기서 빠진 ID 를
        # unknown 으로 흘리면 긴 원문 청크 ID 가 본문에 그대로 찍힌다 (실측에서 본문이
        # 24,986자에서 45,451자로 늘었다). 가린 인용은 건수로만 센다.
        hidden = len(unknown)
        unknown = []
        if len(shown) > COMPACT_CITATIONS:
            hidden += len(shown) - COMPACT_CITATIONS
            shown = shown[:COMPACT_CITATIONS]
    # 실제로 드러낸 번호만 인용으로 센다. REFERENCE 는 본문이 가리키는 것만 싣는다.
    for number, _ in shown:
        _CITED.update(by_number.get(number, ()))
    parts = [
        f"{number} p.{','.join(str(p) for p in sorted(slot))}" if slot else str(number)
        for number, slot in shown
    ]
    if hidden:
        parts.append(f"외 {hidden}건")
    return f" [{'; '.join(parts + unknown)}]" if parts or unknown else ""


def _unique_claim_lines(claims, shown, *, per_technology=False):
    """근거 문장 나열. 압축 조판에서는 기술마다 앞의 몇 건만 싣는다.

    접은 문장의 판정과 등급은 같은 절의 비교표에 그대로 있고, 원문 연결은 산출물
    JSON 의 evidence_ids 에 남는다. 본문에서 빠지면 그 문장이 끌고 오던 자료도
    REFERENCE 에서 빠지므로, 목록이 "본문이 실제로 인용한 것"과 일치하게 된다.
    """
    lines = []
    count: dict[str, int] = {}
    hidden: dict[str, int] = {}
    for c in claims:
        if c.text in shown:
            continue
        shown.add(c.text)
        key = c.technology if per_technology else ""
        if _COMPACT and count.get(key, 0) >= COMPACT_CLAIMS:
            hidden[key] = hidden.get(key, 0) + 1
            continue
        count[key] = count.get(key, 0) + 1
        lines.append(_claim_line(c))
    if not _COMPACT:
        for key, number in hidden.items():
            label = f"{key}: " if key else ""
            lines.append(f"- {label}같은 절의 근거 문장 {number}건은 산출물 JSON 에 남겼다")
    return lines


def _claim_line(c):
    if _COMPACT:
        # 제출본은 문장을 그대로 싣는다. "- KIVI:" 접두사와 "조건:" 꼬리는 노트의
        # 표기이지 보고서의 문장이 아니다. 적용 조건은 비교표의 각 칸이 따로 적는다.
        return _cell(f"{c.text}{_ids(c.evidence_ids)}")
    line = f"- {c.technology}: {c.text}{_ids(c.evidence_ids)}"
    if c.conditions:
        line += " 조건: " + "; ".join(c.conditions)
    return _cell(line)


def _basis(assessment, evidence) -> str:
    """설계서 A.4: 평가 단위는 접근 전반이므로 칸마다 판정 기준과 직접 근거 유무를 함께 적는다."""
    if assessment.judgment == "확인 불가":
        return "기준 없음 · 직접 근거 없음"
    scopes = {e.scope for e in evidence if e.id in set(assessment.evidence_ids)}
    direct = "직접 근거 있음" if "target" in scopes else "직접 근거 없음"
    basis = "선정 기술 기준" if scopes == {"target"} else "접근 전반 기준"
    return f"{basis} · {direct}"


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
                    f'<font color="#2F5D8C">{a.judgment}</font><br/>'
                    f"{_basis(a, run.evidence)}<br/>"
                    f"{a.rationale}{_ids(a.evidence_ids)}"
                )
                if a
                else '<font color="#2F5D8C">확인 불가</font><br/>'
                "기준 없음 · 직접 근거 없음<br/>결과 누락"
            )
        rows.append(f"| {LABELS.get(criterion, criterion)} | {' | '.join(cells)} |")
    return rows


def findings_line(run, tech) -> str:
    """그 관점이 이 기술에 대해 실제로 내린 판정을 한 줄로 먼저 보여 준다.

    절이 "KIVI 절 구성: 확인 불가" 로 시작하면 읽는 사람은 아무것도 안 나왔다고 읽는다.
    그 문장은 기술에 대한 평가가 아니라 **이 절이 구성됐는지**에 대한 파이프라인 상태인데,
    실제 판정은 절 끝 비교표에 묻혀 있었다. 결과를 먼저 놓고 상태는 뒤로 보낸다.
    """
    items = [a for a in run.result.assessments if a.technology == tech]
    if not items:
        return f"> {tech}: 이 관점의 평가 결과가 없다."
    decided = [a for a in items if a.judgment != "확인 불가"]
    def entry(a):
        label = LABELS.get(a.criterion, a.criterion)
        # 일부 루브릭은 판정 값 자체가 항목명을 품는다("원리" / "원리 확인"). 둘을 그대로
        # 이으면 "원리 원리 확인"이 된다.
        return a.judgment if a.judgment.startswith(label) else f"{label} {a.judgment}"

    parts = ", ".join(entry(a) for a in items)
    head = f"{tech} — {parts}."
    unknown = len(items) - len(decided)
    if not decided:
        head += " (공개 근거 부족으로 전 항목 보류)"
    elif unknown:
        head += f" ({len(items)}항목 중 {unknown}항목은 공개 근거 부족으로 보류)"
    else:
        head += f" ({len(items)}항목 모두 판정)"
    return "> " + _cell(head)


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
    # "절 구성"은 이 절이 조판됐는지에 대한 파이프라인 상태이지 기술 평가가 아니다.
    # 제출본에서는 싣지 않는다 — 6장이 관점별 상태를 이미 표로 보고하고, 앞에 둔
    # 판정 줄과 겹쳐 읽는 사람이 결과를 못 찾게 만든다.
    if not _COMPACT:
        lines.append(_cell(f"{tech} 절 구성: {status}"))
    claims = [
        c for c in report_run.result.claims if c.criterion == criterion and c.technology == tech
    ]
    shown = 0
    for c in claims:
        if c.text in seen:
            continue  # 같은 문장을 여러 주장으로 나눠 써도 본문에는 한 번만 싣는다.
        seen.add(c.text)
        if _COMPACT and shown >= COMPACT_CLAIMS:
            continue
        shown += 1
        lines.append(_claim_line(c))
    if not claims and not _COMPACT:
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


def assemble_report(state, result_keys, mode, *, compact: bool = False):
    """기본 동작은 그대로이고, compact=True 일 때만 반복을 접는다 (과제 규칙 10장 한도).

    압축 조판은 두 번 조판한다. 첫 조판에서 본문이 실제로 인용한 자료를 모으고, 그것만
    남겨 번호를 다시 매긴 뒤 다시 조판한다. REFERENCE 절 스스로 "보고서 작성에 실제로
    인용한 자료만 기재한다"고 적고 있는데, 지금까지는 모든 노드가 인용한 자료를 합쳐
    실었다. live 실행에서 그렇게 모인 199건이 보고서 분량의 절반을 차지했다.
    본문에서 접은 주장이 끌고 오던 자료는 본문에 없으므로 목록에서도 빠진다.
    """
    global _CITATIONS, _COMPACT, _CITED
    _COMPACT = compact
    sources = report_sources(state, result_keys)
    _CITATIONS = citation_numbers(sources)
    _CITED = set()
    text = _render_report(state, result_keys, mode, sources)
    if not compact:
        return text
    kept = [e for e in sources if e.id in _CITED]
    if not kept or len(kept) == len(sources):
        return text
    _CITATIONS = citation_numbers(kept)
    _CITED = set()
    return _render_report(state, result_keys, mode, kept)


def _render_report(state, result_keys, mode, sources):
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
        lines.append(findings_line(tech_run, tech))
        lines.extend(_narrative(report, "overview", tech, shown))
        lines.extend(
            _unique_claim_lines([c for c in tech_run.result.claims if c.technology == tech], shown)
        )
    # Design E.1: the two-technology comparison table closes the section.
    tech_summary = _cell(tech_run.result.summary)
    lines.append(tech_summary if _COMPACT else f"기술 조사 노드 요약: {tech_summary}")
    lines.append("표 3-1 기술 조사 결과 비교")
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
        # 판정 → 그 관점의 서술 → 근거 문장 → 비교표 순서로 읽히게 한다.
        for tech in techs:
            lines.append(findings_line(run, tech))
        # 제출본에서는 "시장성 노드 요약:" 같은 파이프라인 말투를 빼고 문단만 싣는다.
        summary = _cell(run.result.summary)
        lines.append(summary if _COMPACT else f"{ROLE_LABELS[role]} 노드 요약: {summary}")
        for tech in techs:
            lines.extend(_narrative(report, role, tech, shown))
        lines.extend(_unique_claim_lines(run.result.claims, shown, per_technology=True))
        lines.append(f"표 4-{number} {heading} 비교")
        lines.extend(_assessment_table(run, techs))

    lines.append("# 5. 시사점")
    shown = set()
    for tech in techs:
        lines.extend(_narrative(report, "implications", tech, shown))
    lines.append("## 5.1 관점 간 상충 지점")
    synth_summary = _cell(synthesis.result.summary)
    lines.append(synth_summary if _COMPACT else f"평가 종합 노드 요약: {synth_summary}")
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
        "모든 판정은 공개 정보 기반 추정이다. 먼저 관점별로 무엇이 확인되지 않았는지 세고, "
        "이어서 그 사유를 관점별로 적는다. 근거가 없어 확인 불가로 남긴 항목은 삭제하지 않는다."
    )
    roles = ("tech", "market", "stakeholder", "domain", "synthesis")
    lines.append("표 6-1 관점별 확인 불가 현황")
    lines.append("| 관점 | 확인 불가 / 전체 | 미확인 기록 | 근거 부족 | 상태 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for role in roles:
        run = state[result_keys[role]]
        items = run.result.assessments
        unknown = sum(1 for a in items if a.judgment == "확인 불가")
        shortfall = sum(
            1 for g in state["gaps"] if g.role == role and g.criterion != "verification"
        )
        lines.append(
            f"| {ROLE_LABELS[role]} | {unknown}/{len(items)} | {len(run.result.unverified)}건 | "
            f"{shortfall}건 | {run.status} |"
        )
    # 같은 문장이 여러 노드에서 반복되고, 질문마다 같은 기록이 쌓인다. 한 번만 싣는다.
    printed: set[str] = set()
    ROUTINE = (
        ("긍정·비판 양쪽 실제 검색 미완료", "긍정·비판 양쪽 검색을 마치지 못한 질문"),
        ("확인 불가", "확인 불가로 남은 항목"),
    )

    def bullets(label, values):
        out, grouped = [], {}
        for value in values:
            text = _cell(str(value))
            if not text:
                continue
            # 같은 형태의 기록은 항목 이름만 모아 한 줄로 싣는다.
            for marker, title in ROUTINE:
                if text.endswith(marker) and ":" in text:
                    grouped.setdefault(title, []).append(text.split(":")[0].strip())
                    break
            else:
                key = f"{label}|{text[:60]}"  # 끝부분만 다른 반복 문장도 한 번만 싣는다.
                if key in printed:
                    continue
                printed.add(key)
                if _COMPACT and len(out) >= COMPACT_BULLETS:
                    continue
                out.append(f"- {label}: {text}")
        for title, items in grouped.items():
            key = f"{label}|{title}"
            if key in printed:
                continue
            printed.add(key)
            names = ", ".join(dict.fromkeys(items))
            out.append(f"- {label}: {title} — {names}")
        remaining = len(values) - len(out)
        if _COMPACT and remaining > 0 and out:
            out.append(f"- {label}: 같은 분류 {remaining}건은 산출물 JSON 에 남겼다")
        return out

    lines.extend(bullets("보고서 통합 한계", report.result.limitations))
    # 압축 조판에서는 관점별 나열을 접는다. 바로 위 표 6-1 이 관점마다 확인 불가·미확인·
    # 근거 부족 건수를 이미 싣고 있어, 아래 줄들은 같은 사실을 문장으로 한 번 더 쓰는
    # 것이 된다. 사유 원문은 산출물 JSON 의 limitations·unverified 에 그대로 남는다.
    for role in [] if _COMPACT else roles:
        run = state[result_keys[role]]
        lines.extend(bullets(f"{ROLE_LABELS[role]} 한계", run.result.limitations))
        lines.extend(bullets(f"{ROLE_LABELS[role]} 확인 필요", run.result.unverified))
        shortfall = [g for g in state["gaps"] if g.role == role and g.criterion != "verification"]
        lines.extend(bullets(f"{ROLE_LABELS[role]} 근거 부족", [g.reason for g in shortfall]))
    if _COMPACT:
        lines.append(
            "관점별 미확인 사유와 근거 부족 항목의 원문은 산출물 JSON(nodes/*.json 의 "
            "limitations·unverified)에 있다. 위 표가 그 건수를 관점별로 싣는다."
        )
    lines.extend(bullets("보고서 생성 확인 필요", report.result.unverified))
    checks = [] if _COMPACT else [g for g in state["gaps"] if g.criterion == "verification"]
    if checks:
        lines.append(
            "아래는 자동 검사가 남긴 기록이다. 판정 내용이 아니라 인용·형식 검사 결과이며, "
            "해당 항목은 위 표에서 확인 불가로 처리했다."
        )
        for role in roles:
            reasons = [g.reason for g in checks if g.role == role]
            if reasons:
                lines.extend(bullets(f"{ROLE_LABELS[role]} 검사 기록", reasons))
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
    entries = reference_entries(sources)
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


def _rule(color, width):
    from reportlab.platypus import Table, TableStyle

    line = Table([[""]], colWidths=[width], rowHeights=[1])
    line.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.9, color)]))
    line.hAlign = "CENTER" if width < 300 else "LEFT"
    return line


def pdf_text(value: str) -> str:
    """표 칸에서 쓰는 최소 서식만 남기고 나머지는 이스케이프한다.

    내장 글꼴이 가운뎃점을 제대로 그리므로 문자 치환은 하지 않는다. 표 칸은 등급·판단
    기준·사유를 줄로 나누고 등급에 색을 주기 위해 <br/> 와 <font> 만 통과시킨다.
    """
    out = escape(value)
    for tag in ("<br/>", '<font color="#2F5D8C">', "</font>"):
        out = out.replace(escape(tag), tag)
    return out


def write_report(
    text,
    output: Path,
    meta=None,
    mode: str = "live",
    *,
    compact: bool = False,
    publish: bool = True,
):
    """설계서 표지·목차 구성으로 조판하고 한글 글꼴을 PDF 안에 내장한다.

    compact 는 줄간격과 문단 간격만 좁힌다. 조판기는 본문의 모든 줄을 개별 문단으로
    올리기 때문에 문단 간격이 쪽수를 크게 좌우하는데, 기본값(글꼴 9.5pt 에 줄간격 16)은
    쪽당 1,800자 수준이라 과제 규칙의 10장 한도 안에서 내용을 더 버려야만 했다.
    여백을 먼저 줄이면 버리는 내용이 그만큼 줄어든다. 글자 크기는 건드리지 않는다.
    """
    from datetime import date

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        BaseDocTemplate,
        Frame,
        LongTable,
        PageBreak,
        PageTemplate,
        Paragraph,
        Spacer,
        Table,
        TableStyle,
    )
    from reportlab.platypus.tableofcontents import TableOfContents

    output.mkdir(parents=True, exist_ok=True)
    (output / "report.md").write_text(text, encoding="utf-8")
    font = "ReportKorean"
    pdfmetrics.registerFont(TTFont(font, str(ROOT / "assets/fonts/NanumGothic-Regular.ttf")))
    ink = colors.HexColor("#1B2530")
    accent = colors.HexColor("#2F5D8C")
    faint = colors.HexColor("#8A97A6")
    hair = colors.HexColor("#D7DDE4")
    wash = colors.HexColor("#F4F7FA")

    def style(name, **kw):
        base = dict(fontName=font, textColor=ink, wordWrap="CJK", alignment=TA_LEFT)
        return ParagraphStyle(name, **{**base, **kw})

    dense = 0.72 if compact else 1.0
    normal = style("body", fontSize=9.5, leading=16 * dense, spaceAfter=8 * dense)
    cellst = style("cell", fontSize=8.5, leading=13 * dense, spaceAfter=0)
    cellhd = style("cellhead", fontSize=8.5, leading=13 * dense, spaceAfter=0, textColor=accent)
    chapter = style(
        "chapter",
        fontSize=16,
        leading=22 * dense,
        spaceBefore=22 * dense,
        spaceAfter=2,
        textColor=accent,
    )
    section = style(
        "section", fontSize=11.5, leading=17 * dense, spaceBefore=15 * dense, spaceAfter=5 * dense
    )
    caption = style(
        "caption",
        fontSize=8.5,
        leading=13 * dense,
        spaceBefore=6 * dense,
        spaceAfter=3 * dense,
        textColor=faint,
    )

    # 절 머리의 판정 줄. 본문에서 가장 먼저 읽어야 할 내용이라 따로 꾸민다.
    finding = style("finding", fontSize=9.5, leading=15, spaceAfter=2)
    story = []
    if meta and compact:
        # 제출본은 표지에 한 쪽을 쓰지 않는다. 같은 정보를 머리말로 올리고 본문이
        # 바로 이어진다. 10장 한도에서 표지 한 쪽은 본문 한 쪽과 바꾸는 선택이다.
        story += [
            Paragraph(
                "A G E N T - O U T P U T",
                style("l", fontSize=8, textColor=faint),
            ),
            Spacer(1, 6),
            Paragraph(pdf_text(meta.title), style("t", fontSize=18, leading=24)),
            Spacer(1, 2),
            Paragraph(
                pdf_text(f"{meta.subtitle} · {meta.lead}"),
                style("s", fontSize=9.5, leading=15, textColor=faint),
            ),
            Spacer(1, 7),
            _rule(accent, 505),
            Spacer(1, 5),
            Paragraph(
                pdf_text(
                    f"{meta.campus}  ·  {' · '.join(meta.members)}  ·  "
                    f"{date.today():%Y년 %m월 %d일}"
                ),
                style("m", fontSize=8.5, leading=13, textColor=faint),
            ),
            Paragraph(
                "자동 생성 초안입니다. 모든 판정은 공개 정보 기반 추정이며 사람의 검토가 필요합니다.",
                style("d", fontSize=8, leading=13, textColor=faint),
            ),
            Spacer(1, 10),
        ]
    elif meta:
        big = style("t", alignment=TA_CENTER, fontSize=21, leading=32)
        mid = style("s", alignment=TA_CENTER, fontSize=12, leading=21, textColor=faint)
        small = style("m", alignment=TA_CENTER, fontSize=9.5, leading=18)
        story += [
            Spacer(1, 150),
            Paragraph(
                "R A G - O U T P U T",
                style("l", alignment=TA_CENTER, fontSize=8.5, textColor=faint),
            ),
            Spacer(1, 24),
            Paragraph(pdf_text(meta.subtitle), mid),
            Paragraph(pdf_text(meta.title), big),
            Paragraph(pdf_text(meta.lead), mid),
            Spacer(1, 18),
            _rule(accent, 90),
            Spacer(1, 54),
            Paragraph(pdf_text(f"캠퍼스 · 반    {meta.campus}"), small),
            Paragraph(pdf_text("조원    " + " · ".join(meta.members)), small),
            Paragraph(f"작성    {date.today():%Y년 %m월 %d일}", small),
            Spacer(1, 34),
            Paragraph(
                "자동 생성 초안입니다. 모든 판정은 공개 정보 기반 추정이며 사람의 검토가 필요합니다.",
                style("d", alignment=TA_CENTER, fontSize=8, textColor=faint),
            ),
            PageBreak(),
        ]
        if True:
            toc = TableOfContents()
            toc.levelStyles = [
                style("toc0", fontSize=10, leading=20, spaceAfter=2),
                style("toc1", fontSize=9, leading=17, leftIndent=16, textColor=faint),
            ]
            story += [Paragraph("목차", chapter), Spacer(1, 6), toc, PageBreak()]

    rows = []

    def flush():
        if not rows:
            return
        columns = len(rows[0])
        first = min(84, 505 / columns)
        widths = (
            [505] if columns == 1 else [first] + [(505 - first) / (columns - 1)] * (columns - 1)
        )
        table = LongTable(rows, colWidths=widths, repeatRows=1, hAlign="LEFT", splitInRow=1)
        commands = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, 0), wash),
            ("LINEABOVE", (0, 0), (-1, 0), 0.8, accent),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, hair),
            ("LINEBELOW", (0, -1), (-1, -1), 0.8, accent),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ]
        for index in range(2, len(rows), 2):
            commands.append(("BACKGROUND", (0, index), (-1, index), colors.HexColor("#FAFBFD")))
        for index in range(1, len(rows)):
            commands.append(("LINEBELOW", (0, index), (-1, index), 0.25, hair))
        table.setStyle(TableStyle(commands))
        story.extend([Spacer(1, 3), table, Spacer(1, 14)])
        rows.clear()

    def summary_box(lines):
        body = [Paragraph(pdf_text(line), normal) for line in lines]
        box = Table([[body]], colWidths=[505])
        box.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), wash),
                    ("LINEBEFORE", (0, 0), (0, -1), 2.2, accent),
                    ("LEFTPADDING", (0, 0), (-1, -1), 14),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                    ("TOPPADDING", (0, 0), (-1, -1), 12),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.extend([box, Spacer(1, 16)])

    def findings_band(lines):
        """절 머리의 판정 줄 묶음. 읽는 사람이 이 절의 결론을 먼저 보게 한다."""
        body = [Paragraph(pdf_text(line), finding) for line in lines]
        box = Table([[body]], colWidths=[505])
        box.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), wash),
                    ("LINEBEFORE", (0, 0), (0, -1), 2.2, accent),
                    ("LEFTPADDING", (0, 0), (-1, -1), 11),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 11),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.extend([box, Spacer(1, 9)])

    pending_summary, in_summary = [], False
    pending_findings = []

    def flush_findings():
        if pending_findings:
            findings_band(list(pending_findings))
            pending_findings.clear()

    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("> "):
            pending_findings.append(line[2:])
            continue
        flush_findings()
        if line.startswith("|"):
            if line.startswith("| ---"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            first_row = not rows
            rows.append([Paragraph(pdf_text(c), cellhd if first_row else cellst) for c in cells])
            continue
        flush()
        if line.startswith("#"):
            if in_summary and pending_summary:
                summary_box(pending_summary)
                pending_summary, in_summary = [], False
            title = line.lstrip("# ").strip()
            level = 1 if line.startswith("## ") else 0
            paragraph = Paragraph(pdf_text(title), section if level else chapter)
            paragraph._toc_level = level
            story.append(paragraph)
            if not level:
                story.append(_rule(hair, 505))
            in_summary = title == "SUMMARY"
            continue
        if in_summary:
            pending_summary.append(line)
            continue
        style_for = caption if line.startswith("표 ") else normal
        story.append(Paragraph(pdf_text(line), style_for))
    flush_findings()
    if in_summary and pending_summary:
        summary_box(pending_summary)
    flush()

    path = output / "report.pdf"
    label = meta.title if meta else "평가 보고서"
    chapters = {}

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(hair)
        canvas.setLineWidth(0.4)
        canvas.line(45, 40, 550, 40)
        canvas.setFont(font, 7.5)
        canvas.setFillColor(faint)
        canvas.drawString(45, 28, chapters.get(canvas.getPageNumber(), label))
        canvas.drawRightString(550, 28, str(doc.page))
        canvas.restoreState()

    class Report(BaseDocTemplate):
        def afterFlowable(self, flowable):
            level = getattr(flowable, "_toc_level", None)
            if level is None:
                return
            title = flowable.getPlainText()
            self.notify("TOCEntry", (level, title, self.page))
            if level == 0:
                chapters.setdefault(self.page, title)

    from reportlab.lib.pagesizes import A4

    doc = Report(
        str(path),
        pagesize=A4,
        leftMargin=45,
        rightMargin=45,
        topMargin=50,
        bottomMargin=56,
        title=label,
    )
    frame = Frame(45, 56, 505, doc.height, id="body", showBoundary=0)
    doc.addPageTemplates(
        [
            PageTemplate(id="cover", frames=[frame]),
            PageTemplate(id="page", frames=[frame], onPage=footer),
        ]
    )
    doc.multiBuild(story)
    if publish and meta and meta.submission and mode != "mock":
        # 과제 제출 파일명으로 한 부 더 둔다. 실행 폴더의 원본은 그대로 남는다.
        # runner.execute 와 같은 규칙으로 RUN_OUTPUT_DIR 을 따른다. 이 복사는 고정 경로를
        # 덮어쓰므로, 테스트나 실험이 제출본 자리를 건드리지 않게 하려면 반드시 필요하다.
        base = (
            Path(os.environ["RUN_OUTPUT_DIR"]) if os.getenv("RUN_OUTPUT_DIR") else ROOT / "outputs"
        )
        submitted = base / meta.submission
        submitted.parent.mkdir(parents=True, exist_ok=True)
        submitted.write_bytes(path.read_bytes())
    return str(path)

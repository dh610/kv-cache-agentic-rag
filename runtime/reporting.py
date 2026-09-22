"""Deterministic report assembly and final validation; generated files are not approval."""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

from rag.evidence import merge_evidence
from runtime.handoff import reference_issues
from schemas.contracts import Gap


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
    author = e.authors or e.publisher or "기관/저자 미확인"
    if e.source_type == "paper":
        return f"{author} ({e.year or '연도 미확인'}). {e.title}. {e.venue or '게재 정보 미확인'}, p.{e.page or '?'}. {e.url}"
    return f"{author} ({e.published_at or '발행일 미확인'}). {e.title}. {e.site or '사이트 미확인'}. {e.url}"


def assemble_report(state, result_keys, mode):
    labels = {
        "mechanism": "원리",
        "maturity": "성숙도",
        "limitations": "기술 한계",
        "growth": "시장 성장성",
        "adoption": "상용화·채택",
        "ecosystem": "생태계",
        "competitors": "경쟁 기술 진영",
        "adopters": "도입 기업·개발자",
        "industry": "투자·업계",
        "cost": "비용",
        "performance": "성능",
        "quality": "품질",
        "operations": "도입·운영",
        "scalability": "확장성",
    }
    lines = [
        "# SUMMARY",
        state["synthesis"].result.summary,
        f"자동 생성 초안 / 실행 모드: {mode}. 공개 정보 기반 추정. 사람의 원문·등급 검토가 필요합니다.",
        "# 1. 분석 배경",
        state["domain"],
        "# 2. 기술 선정",
        ", ".join(f"{role.upper()}: {tech}" for role, tech in state["target_techs"].items()),
        "# 3. 기술 개요 및 TRL",
    ]
    for role, heading in [
        ("tech", "기술별 원리·조건"),
        ("market", "4.1 시장성"),
        ("stakeholder", "4.2 이해관계자"),
        ("domain", "4.3 도메인 적용"),
    ]:
        run = state[result_keys[role]]
        lines.extend([f"## {heading}", run.result.summary])
        criteria = list(dict.fromkeys(a.criterion for a in run.result.assessments))
        lines.append("| 항목 | KIVI | ITME |")
        lines.append("| --- | --- | --- |")
        for criterion in criteria:
            cells = []
            for tech in ("KIVI", "ITME"):
                a = next(
                    (
                        a
                        for a in run.result.assessments
                        if a.technology == tech and a.criterion == criterion
                    ),
                    None,
                )
                cells.append(
                    (
                        f"{a.judgment}: {a.rationale} [{', '.join(a.evidence_ids)}]"
                        if a
                        else "확인 불가: 결과 누락"
                    )
                    .replace("|", "/")
                    .replace("\n", " ")
                )
            lines.append(f"| {labels.get(criterion, criterion)} | {cells[0]} | {cells[1]} |")
        for c in run.result.claims:
            lines.append(
                f"- {c.technology}: {c.text} [{', '.join(c.evidence_ids)}] 조건: {'; '.join(c.conditions)}"
            )
        if role == "tech":
            lines.append("## 3.3 TRL")
            for technology, item in state["trl_result"].items():
                lines.append(
                    f"- {technology}: {item.get('level') or '확인 불가'} / {item.get('rationale')} [{', '.join(item.get('evidence_ids', []))}] 공개 정보 기반 추정"
                )
    lines.extend(["# 5. 관점별 시사점", state["synthesis"].result.summary])
    lines.extend(["| 관점 | KIVI | ITME |", "| --- | --- | --- |"])
    for role in ("tech", "market", "stakeholder", "domain"):
        cells = []
        for tech in ("KIVI", "ITME"):
            cells.append(
                "; ".join(
                    f"{labels.get(a.criterion, a.criterion)}: {a.judgment}"
                    for a in state[result_keys[role]].result.assessments
                    if a.technology == tech
                )
                or "확인 불가"
            )
        lines.append(f"| {role} | {cells[0]} | {cells[1]} |")
    for role in ("synthesis", "report"):
        run = state[result_keys[role]]
        for a in run.result.assessments:
            lines.append(
                f"- {role}/{a.technology}/{a.criterion}: {a.judgment}. {a.rationale} [{', '.join(a.evidence_ids)}]"
            )
        for c in run.result.claims:
            lines.append(f"- {c.technology}: {c.text} [{', '.join(c.evidence_ids)}]")
    lines.append("# 6. 한계점 및 확증편향 방지 조치")
    lines.extend(f"- {g.role}/{g.criterion}: {g.reason}" for g in state["gaps"])
    for key in result_keys.values():
        lines.extend(f"- {state[key].node}: {s}" for s in state[key].result.limitations)
    lines.append("# REFERENCE")
    for e in report_sources(state, result_keys):
        lines.append(
            f"- [{e.id}] {reference_text(e)} (출처 관계: {e.affiliation}; {e.affiliation_reason or '미확인'}; 입장: {e.stance})"
        )
    return "\n\n".join(lines) + "\n"


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
    return {
        "ready": not problems,
        "problems": sorted(set(problems)),
        "human_review_required": True,
        "note": "형식·인용 연결 검사이며 사실성/평가 타당성의 최종 승인이 아님",
    }


def write_report(text, output: Path):
    """Korean CID font avoids a platform-specific font path in team clones."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

    output.mkdir(parents=True, exist_ok=True)
    (output / "report.md").write_text(text, encoding="utf-8")
    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    normal = ParagraphStyle(
        "body",
        fontName="HYSMyeongJo-Medium",
        fontSize=9,
        leading=14,
        wordWrap="CJK",
        alignment=TA_LEFT,
        spaceAfter=6,
    )
    heading = ParagraphStyle(
        "heading", parent=normal, fontSize=14, leading=20, spaceBefore=12, keepWithNext=True
    )
    story = []
    rows = []

    def flush():
        if not rows:
            return
        table = LongTable(rows, colWidths=[65, 220, 220], repeatRows=1, hAlign="LEFT", splitInRow=1)
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EDF3")),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.extend([table, Spacer(1, 8)])
        rows.clear()

    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("|"):
            if line.startswith("| ---"):
                continue
            rows.append([Paragraph(escape(c.strip()), normal) for c in line.strip("|").split("|")])
            continue
        flush()
        story.append(
            Paragraph(
                escape(line.lstrip("# ").strip()), heading if line.startswith("#") else normal
            )
        )
    flush()
    path = output / "report.pdf"

    def footer(canvas, doc):
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(550, 25, str(doc.page))

    SimpleDocTemplate(
        str(path), leftMargin=40, rightMargin=40, topMargin=40, bottomMargin=40
    ).build(story, onFirstPage=footer, onLaterPages=footer)
    return str(path)

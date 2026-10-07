"""보고서를 PDF 로 조판한다. 내용을 고르거나 판정하지 않는다.

조립된 마크다운 한 덩어리를 받아 표지·목차·표·각주로 그린다. 무엇을 실을지는
``builder`` 가, 제출 가능한지는 ``validation`` 이 정한다.
"""

from __future__ import annotations

import os
from pathlib import Path
from xml.sax.saxutils import escape

from runtime.settings import ROOT

# 이보다 긴 요약은 강조 상자에 넣지 않는다. A4 한 쪽(약 724pt)에 들어가지 않는 단일 셀
# 표는 쪼개지지 못하고 조판 전체를 중단시킨다.
SUMMARY_BOX_CHARS = 1400


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

    # 제출본은 글자를 조금 줄이고 줄간격 **비율**은 지킨다. 줄간격만 조이면 글자 크기와
    # 거의 같아져(9.5pt 에 9.9pt) 읽기 어려워진다.
    dense = 0.75 if compact else 1.0
    base = 9.0 if compact else 9.5
    normal = style("body", fontSize=base, leading=16 * dense, spaceAfter=8 * dense)
    cellst = style("cell", fontSize=base - 0.8, leading=13 * dense, spaceAfter=0)
    cellhd = style(
        "cellhead", fontSize=base - 0.8, leading=13 * dense, spaceAfter=0, textColor=accent
    )
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
    finding = style("finding", fontSize=base, leading=15 * dense, spaceAfter=2)
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
        if sum(len(line) for line in lines) > SUMMARY_BOX_CHARS:
            # 단일 셀 표는 쪽 사이로 쪼개지지 않는다. 한 쪽에 안 들어갈 만큼 긴 요약을
            # 표에 넣으면 조판이 LayoutError 로 죽고 보고서가 아예 안 나온다. 분량
            # 규칙 위반은 validate_report 가 따로 잡으므로, 여기서는 상자를 포기하고
            # 본문으로 흘려보내 산출물은 반드시 만든다.
            story.extend([_rule(accent, 505), *body, _rule(hair, 505), Spacer(1, 12)])
            return
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

"""보고서를 PDF 로 조판한다 (renderer). 내용을 고르거나 판정하지 않는다.

무엇을 실을지는 ``builder`` 가, 제출 가능한지는 ``validation`` 이 정한다. 이 모듈은 조립된
마크다운을 받아 그리기만 하며, 마크다운의 줄 모양이 곧 입력 규약이다.

    # 장 / ## 절                 장·절 제목 (목차에 오른다)
    > KIVI — 판정 요약            절 머리의 판정. 기술별 줄이 잇달아 나오면 항목 × 기술 표로 놓는다
    > 갈리는 지점: …              두 기술의 판정이 갈린 항목. 판정 표 아래 한 줄
    | a | b |                   표 (둘째 줄 ``| --- |`` 는 구분선)
    표 4-1 …                     표 제목
    - 기술: 문장 [1] 조건: …      근거 문장. 진짜 글머리표로 올리고 조건은 회색 보조 줄
    시장성 노드 요약: …           평가 노드 요약. 테두리 카드
    KIVI 절 구성: …              절 구성 상태. 작은 회색 글씨 (판정이 아니라 파이프라인 상태)
    그 밖의 줄                    본문 문단

글꼴은 Noto Sans KR(Regular·Bold)을 PDF 에 내장한다. 본문 10pt 에 줄간격 1.7배로, 화면에서
읽는 문서의 밀도에 맞춘다. 줄은 낱말 경계에서만 바꾼다. 색은 네이비 하나와 회색만 쓴다
(제출용 문서). 제출본(compact)은 과제 규칙의 10장 한도 때문에 같은 구성으로 간격만 줄인다.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from runtime.settings import ROOT

FONT = "NotoSansKR"
FONT_BOLD = "NotoSansKR-Bold"
FONT_DIR = ROOT / "assets/fonts"
A4_WIDTH = 595.28

# 색. 글자는 회색 계열, 강조는 네이비 하나만 쓴다 (기업 보고서 관행: 남색 + 회색).
INK = "#1A1F2B"
INK2 = "#333A47"
INK3 = "#6B7280"
LINE = "#DDE1E7"
HEAD = "#F3F4F6"
ACCENT = "#1F3A5F"
# (배경, 테두리, 글자)
TINT = ("#F4F6F9", "#D9DEE5", "#2A3A52")  # 판정·요약 상자. 옅은 회청색
CARD = ("#FFFFFF", LINE, INK2)  # 노드 요약 카드

_SUMMARY_LINE = re.compile(r"^(?P<label>[^:：]{1,16} 노드 요약): (?P<body>.+)$")
_STATUS_LINE = re.compile(r"^(?P<tech>\S{1,24}) 절 구성: ")
_CITATION = re.compile(r"\[(\d[^\]]*)\]")


def _register_fonts():
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if FONT in pdfmetrics.getRegisteredFontNames():
        return
    pdfmetrics.registerFont(TTFont(FONT, str(FONT_DIR / "NotoSansKR-Regular.ttf")))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, str(FONT_DIR / "NotoSansKR-Bold.ttf")))
    # <b> 가 굵은 글꼴로 가게 한다. 기울임은 없으므로 같은 글꼴로 돌린다.
    addMapping(FONT, 0, 0, FONT)
    addMapping(FONT, 1, 0, FONT_BOLD)
    addMapping(FONT, 0, 1, FONT)
    addMapping(FONT, 1, 1, FONT_BOLD)


def pdf_text(value: str) -> str:
    """표 칸에서 쓰는 최소 서식만 남기고 나머지는 이스케이프한다.

    builder 는 표 칸의 등급에 색을 주고 줄을 나누기 위해 <br/> 와 <font> 만 쓴다. 그 둘만
    통과시키고, 등급은 굵게 올린다. 인용 번호 ``[1; 3]`` 는 본문보다 옅은 색으로 내린다.
    """
    out = escape(value)
    out = out.replace(escape("<br/>"), "<br/>")
    out = out.replace(escape('<font color="#2F5D8C">'), f'<b><font color="{ACCENT}">')
    out = out.replace(escape("</font>"), "</font></b>")
    return _CITATION.sub(rf'<font color="{INK3}">[\1]</font>', out)


def _rule(color, width):
    from reportlab.platypus import Table, TableStyle

    line = Table([[""]], colWidths=[width], rowHeights=[1])
    line.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.9, color)]))
    line.hAlign = "CENTER" if width < 300 else "LEFT"
    return line


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

    compact 는 구성을 바꾸지 않고 줄간격·문단 간격·상자 여백만 줄인다. 조판기는 본문의
    모든 줄을 개별 문단으로 올리기 때문에 문단 간격이 쪽수를 크게 좌우하는데, 제출본은
    과제 규칙의 10장 한도 안에 들어야 한다. 글자 크기는 한 단계만 내린다.
    """
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
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
    _register_fonts()

    ink, ink2, ink3 = colors.HexColor(INK), colors.HexColor(INK2), colors.HexColor(INK3)
    line, head, accent = colors.HexColor(LINE), colors.HexColor(HEAD), colors.HexColor(ACCENT)
    tint_bg = colors.HexColor(TINT[0])

    def style(name, **kw):
        # wordWrap 을 두지 않아 낱말 경계에서만 줄을 바꾼다 (CJK 모드는 글자 사이 아무 데서나
        # 끊어 낱말이 두 줄로 갈라진다). 한 줄보다 긴 낱말(URL)만 글자 단위로 나뉜다.
        base = dict(fontName=FONT, textColor=ink2, alignment=TA_LEFT, splitLongWords=1)
        return ParagraphStyle(name, **{**base, **kw})

    # 밀도. 전체본은 본문 10pt 에 줄간격 1.7배, 제출본은 9pt 에 1.4배.
    body_size = 9 if compact else 10
    body_leading = round(body_size * (1.45 if compact else 1.7), 1)
    gap = 0.45 if compact else 1.0  # 문단·상자 간격 배율
    pad = 6 if compact else 12  # 상자 안쪽 여백
    margin = 36 if compact else 45  # 좌우 여백. 제출본은 간격을 살리는 대신 여백을 조금 줄인다
    PAGE_WIDTH = A4_WIDTH - 2 * margin

    normal = style("body", fontSize=body_size, leading=body_leading, spaceAfter=8 * gap)
    # REFERENCE 목록. 제출본에서는 한 단계 더 작게 — 본문보다 출처 목록이 길어지지 않게 한다.
    small = style(
        "small",
        fontSize=body_size - (1.5 if compact else 1),
        leading=body_leading - (3.5 if compact else 2),
        spaceAfter=(3 if compact else 6) * gap,
    )
    bullet = style(
        "bullet",
        fontSize=body_size,
        leading=body_leading,
        spaceAfter=5 * gap,
        leftIndent=16,
        bulletIndent=4,
        bulletFontName=FONT,
    )
    status = style(
        "status",
        fontSize=body_size - 1.5,
        leading=body_leading - 3,
        spaceBefore=3,
        spaceAfter=6 * gap,
        textColor=ink3,
    )
    caption = style(
        "caption",
        fontSize=body_size - 1.5,
        leading=body_leading - 3,
        spaceBefore=8 * gap,
        spaceAfter=4,
        textColor=ink3,
        fontName=FONT_BOLD,
    )
    cellst = style(
        "cell",
        fontSize=body_size - 1,
        leading=body_leading - (3 if compact else 2.5),
        spaceAfter=0,
    )
    cellhd = style(
        "cellhead",
        fontSize=body_size - 1.5,
        leading=body_leading - 3,
        spaceAfter=0,
        textColor=ink3,
        fontName=FONT_BOLD,
    )
    chapter = style(
        "chapter",
        fontSize=13 if compact else 15,
        leading=18 if compact else 21,
        textColor=ink,
        fontName=FONT_BOLD,
    )
    section = style(
        "section",
        fontSize=11 if compact else 11.5,
        leading=16 if compact else 17,
        spaceBefore=14 * gap,
        spaceAfter=6 * gap,
        textColor=ink,
        fontName=FONT_BOLD,
        keepWithNext=1,
    )
    # 4장 각 절 안의 기술별 소절 머리(### KIVI). 목차·꼬리말 챕터명에는 올리지 않는다.
    subsection = style(
        "subsection",
        fontSize=body_size + 0.5,
        leading=body_leading,
        spaceBefore=10 * gap,
        spaceAfter=3 * gap,
        textColor=ink,
        fontName=FONT_BOLD,
        keepWithNext=1,
    )
    boxed = style("boxed", fontSize=body_size, leading=body_leading, spaceAfter=0)
    # 판정 상자 안의 기술 이름과 설명.
    tech_name = style(
        "tech",
        fontSize=(12 if compact else 14),
        leading=(16 if compact else 19),
        alignment=TA_CENTER,
        fontName=FONT_BOLD,
        textColor=accent,
        spaceAfter=3,
    )
    tech_desc = style(
        "techdesc",
        fontSize=body_size - 0.5,
        leading=body_leading - 1.5,
        alignment=TA_CENTER,
        textColor=colors.HexColor(TINT[2]),
    )

    story = []

    def card(parts, palette, *, after=12, label=None, size=None):
        """둥근 테두리 상자. palette 는 (배경, 테두리, 글자).

        표 칸이 아니라 테두리를 두른 문단으로 만든다. 문단은 쪽 끝에서 글처럼 나뉘지만,
        표 칸은 한 덩어리라 쪽 아래에 빈 조각을 남기거나 반 쪽을 비우고 넘어간다.
        """
        bg, border, color = palette
        st = ParagraphStyle(
            "card",
            parent=boxed,
            fontSize=size or body_size,
            textColor=colors.HexColor(color),
            backColor=colors.HexColor(bg),
            borderColor=colors.HexColor(border),
            borderWidth=0.8,
            borderRadius=8,
            borderPadding=(pad, pad + 2, pad, pad + 2),
            # 테두리는 문단 바깥에 그려지므로 그만큼 간격을 둬야 이웃과 겹치지 않는다.
            spaceBefore=pad + 2,
            spaceAfter=pad + 2 + after * gap,
        )
        prefix = f"<b>{escape(label)}</b>  " if label else ""
        story.append(Paragraph(prefix + "<br/><br/>".join(parts), st))

    def findings_row(items):
        """절 머리의 판정. 기술마다 한 칸씩 나란히 놓아 두 기술이 비교로 읽히게 한다.

        칸 위에는 기술 이름을 크게 가운데 두고, 그 아래에 판정 요약을 적는다.
        """
        cells = []
        for item in items:
            name, _, desc = item.partition(" — ")
            if not desc:
                name, desc = "", item
            cell = []
            if name:
                cell.append(Paragraph(pdf_text(name), tech_name))
            cell.append(Paragraph(pdf_text(desc), tech_desc))
            cells.append(cell)
        count = len(cells)
        spacing = 10 if count > 1 else 0
        width = (PAGE_WIDTH - spacing * (count - 1)) / count
        row, widths = [], []
        for index, cell in enumerate(cells):
            if index:
                row.append("")
                widths.append(spacing)
            row.append(cell)
            widths.append(width)
        table = Table([row], colWidths=widths, hAlign="LEFT")
        commands = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), pad),
            ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
            ("LEFTPADDING", (0, 0), (-1, -1), pad),
            ("RIGHTPADDING", (0, 0), (-1, -1), pad),
            ("ROUNDEDCORNERS", [8] * 4),
        ]
        for column in range(0, len(row), 2):
            commands += [
                ("BACKGROUND", (column, 0), (column, 0), colors.HexColor(TINT[0])),
                ("BOX", (column, 0), (column, 0), 0.8, colors.HexColor(TINT[1])),
            ]
        table.setStyle(TableStyle(commands))
        story.extend([table, Spacer(1, 12 * gap)])

    def chapter_heading(title):
        """장 제목. 왼쪽의 파란 세로 막대와 굵은 글씨."""
        label = Paragraph(pdf_text(title), chapter)
        bar = Table([["", label]], colWidths=[5, PAGE_WIDTH - 5])
        bar.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (0, 0), accent),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("LEFTPADDING", (1, 0), (1, 0), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 1),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ]
            )
        )
        bar.hAlign = "LEFT"
        bar._toc_level = 0
        bar._toc_title = title
        bar.keepWithNext = 1
        story.extend([Spacer(1, (10 if compact else 24) * gap), bar, Spacer(1, 8 * gap)])

    if meta and compact:
        # 제출본은 표지에 한 쪽을 쓰지 않는다. 같은 정보를 머리말로 올리고 본문이
        # 바로 이어진다. 10장 한도에서 표지 한 쪽은 본문 한 쪽과 바꾸는 선택이다.
        story += [
            Paragraph(
                pdf_text(meta.title),
                style("t", fontSize=18, leading=24, fontName=FONT_BOLD, textColor=ink),
            ),
            Spacer(1, 2),
            Paragraph(
                pdf_text(f"{meta.subtitle} · {meta.lead}"),
                style("s", fontSize=9.5, leading=15, textColor=ink3),
            ),
            Spacer(1, 7),
            _rule(accent, PAGE_WIDTH),
            Spacer(1, 5),
            Paragraph(
                pdf_text(
                    f"{meta.campus}  ·  {' · '.join(meta.members)}  ·  "
                    f"{date.today():%Y년 %m월 %d일}"
                ),
                style("m", fontSize=8.5, leading=13, textColor=ink3),
            ),
            Spacer(1, 10),
        ]
    elif meta:
        big = style(
            "t", alignment=TA_CENTER, fontSize=22, leading=32, fontName=FONT_BOLD, textColor=ink
        )
        mid = style("s", alignment=TA_CENTER, fontSize=12, leading=21, textColor=ink3)
        small_c = style("m", alignment=TA_CENTER, fontSize=9.5, leading=18)
        story += [
            Spacer(1, 170),
            Paragraph(pdf_text(meta.subtitle), mid),
            Paragraph(pdf_text(meta.title), big),
            Paragraph(pdf_text(meta.lead), mid),
            Spacer(1, 18),
            _rule(accent, 90),
            Spacer(1, 54),
            Paragraph(pdf_text(f"캠퍼스 · 반    {meta.campus}"), small_c),
            Paragraph(pdf_text("조원    " + " · ".join(meta.members)), small_c),
            Paragraph(f"작성    {date.today():%Y년 %m월 %d일}", small_c),
            PageBreak(),
        ]
        toc = TableOfContents()
        toc.levelStyles = [
            style("toc0", fontSize=10, leading=20, spaceAfter=2, textColor=ink),
            style("toc1", fontSize=9, leading=17, leftIndent=16, textColor=ink3),
        ]
        chapter_heading("목차")
        story += [toc, PageBreak()]

    rows = []

    def flush():
        if not rows:
            return
        columns = len(rows[0])
        first = min(84, PAGE_WIDTH / columns)
        widths = (
            [PAGE_WIDTH]
            if columns == 1
            else [first] + [(PAGE_WIDTH - first) / (columns - 1)] * (columns - 1)
        )
        table = LongTable(rows, colWidths=widths, repeatRows=1, hAlign="LEFT", splitInRow=1)
        commands = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, 0), head),
            ("LINEBELOW", (0, 0), (-1, -1), 0.6, line),
            ("LINEABOVE", (0, 0), (-1, 0), 0.6, line),
            ("TOPPADDING", (0, 0), (-1, -1), 7 * gap + 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7 * gap + 2),
            ("LEFTPADDING", (0, 0), (-1, -1), 10 * gap + 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10 * gap + 4),
        ]
        table.setStyle(TableStyle(commands))
        story.extend([table, Spacer(1, 16 * gap)])
        rows.clear()

    pending_summary, in_summary = [], False
    pending_findings = []
    chapter_name = ""

    def findings_table(items):
        """절 머리의 판정을 항목 × 기술 표로 놓는다. 상자 두 개를 나란히 두는 것보다 비교가 바로 읽힌다.

        입력은 ``KIVI — 상용화·채택 보통, 시장 규모·성장성 확인 불가. (3항목 모두 판정)`` 꼴이다.
        항목 이름은 builder 의 LABELS 로 찾는다. 한 줄이라도 그 꼴이 아니면 표로 만들 수 없어
        False 를 돌려주고, 호출자가 상자로 대신 그린다.
        """
        from runtime.report.builder import (
            LABELS,  # 항목 이름. builder 가 이 모듈을 먼저 import 한다
        )

        labels = sorted(LABELS.values(), key=len, reverse=True)
        # 기술별 판정 줄 뒤에 builder 가 "갈리는 지점: …" 줄을 붙인다. 표의 재료가 아니라
        # 표 아래에 적는 한 줄이다.
        remarks_extra = [item for item in items if item.startswith("갈리는 ")]
        items = [item for item in items if not item.startswith("갈리는 ")]
        if not items:
            return False
        names, notes, verdicts = [], [], []
        for item in items:
            name, _, desc = item.partition(" — ")
            if not desc:
                return False
            found = re.search(r"\s*\(([^()]*)\)\s*$", desc)
            notes.append(found.group(1) if found else "")
            main = desc[: found.start()] if found else desc
            parsed = {}
            for part in main.rstrip(". ").split(", "):
                # 판정 값이 항목명을 품는 루브릭("조건부 시사점")은 항목명만 온다.
                label = next((lb for lb in labels if part == lb or part.startswith(lb + " ")), None)
                if label is None:
                    return False
                verdict = part[len(label) + 1 :] if part != label else part
                # 같은 항목이 되풀이되면(생성기가 같은 판정을 여러 번 낸 경우) 첫 실제 판정을 쓴다.
                if label not in parsed or parsed[label] == "확인 불가":
                    parsed[label] = verdict
            names.append(name)
            verdicts.append(parsed)
        order = list(dict.fromkeys(label for v in verdicts for label in v))
        if not order:
            return False
        tech_head = ParagraphStyle(
            "techhead",
            parent=cellhd,
            alignment=TA_CENTER,
            textColor=accent,
            fontSize=body_size + 1,
            leading=body_leading,
        )
        verdict_st = ParagraphStyle("verdict", parent=cellst, alignment=TA_CENTER)
        rows = [[Paragraph("항목", cellhd)] + [Paragraph(pdf_text(n), tech_head) for n in names]]
        for label in order:
            rows.append(
                [Paragraph(pdf_text(label), cellst)]
                + [
                    Paragraph(
                        f'<b><font color="{ACCENT}">{pdf_text(v.get(label, "—"))}</font></b>',
                        verdict_st,
                    )
                    for v in verdicts
                ]
            )
        first = 110
        widths = [first] + [(PAGE_WIDTH - first) / len(names)] * len(names)
        table = Table(rows, colWidths=widths, hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("BACKGROUND", (0, 0), (-1, 0), tint_bg),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.6, line),
                    ("LINEABOVE", (0, 0), (-1, 0), 0.6, line),
                    ("TOPPADDING", (0, 0), (-1, -1), 5 * gap + 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5 * gap + 2),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ]
            )
        )
        story.append(table)
        remarks = " · ".join(f"{n}: {note}" for n, note in zip(names, notes) if note)
        for line_text in [remarks, *remarks_extra]:
            if line_text:
                story.append(Paragraph(pdf_text(line_text), status))
        story.append(Spacer(1, 10 * gap))
        return True

    def flush_findings():
        if pending_findings and not findings_table(list(pending_findings)):
            # 표로 못 만들면 상자로 그린다. "갈리는 지점" 줄은 그때도 상자가 아니라 한 줄이다.
            cards = [item for item in pending_findings if not item.startswith("갈리는 ")]
            if cards:
                findings_row(cards)
            for item in pending_findings:
                if item.startswith("갈리는 "):
                    story.append(Paragraph(pdf_text(item), status))
        pending_findings.clear()

    def flush_summary():
        nonlocal pending_summary, in_summary
        if in_summary and pending_summary:
            card([pdf_text(t) for t in pending_summary], TINT, after=16)
        pending_summary, in_summary = [], False

    for raw in text.splitlines():
        line_text = raw.strip()
        if not line_text:
            continue
        if line_text.startswith("> "):
            pending_findings.append(line_text[2:])
            continue
        flush_findings()
        if line_text.startswith("|"):
            if line_text.startswith("| ---"):
                continue
            cells = [c.strip() for c in line_text.strip("|").split("|")]
            first_row = not rows
            rows.append([Paragraph(pdf_text(c), cellhd if first_row else cellst) for c in cells])
            continue
        flush()
        if line_text.startswith("### "):
            # 기술별 소절 머리. 목차·꼬리말 챕터명에는 올리지 않는다.
            story.append(Paragraph(pdf_text(line_text[4:].strip()), subsection))
            continue
        if line_text.startswith("#"):
            flush_summary()
            title = line_text.lstrip("# ").strip()
            level = 1 if line_text.startswith("## ") else 0
            if level:
                paragraph = Paragraph(pdf_text(title), section)
                paragraph._toc_level = 1
                story.append(paragraph)
            else:
                chapter_heading(title)
                chapter_name = title
            in_summary = title == "SUMMARY"
            continue
        if in_summary:
            pending_summary.append(line_text)
            continue
        if line_text.startswith("표 "):
            story.append(Paragraph(pdf_text(line_text), caption))
            continue
        if line_text.startswith("- "):
            body, _, conditions = line_text[2:].partition(" 조건: ")
            html = pdf_text(body)
            if conditions:
                html += (
                    f'<br/><font color="{INK3}" size="{body_size - 1.5}">조건: '
                    f"{pdf_text(conditions)}</font>"
                )
            story.append(Paragraph(html, bullet, bulletText="•"))
            continue
        found = _SUMMARY_LINE.match(line_text)
        if found:
            card([pdf_text(found["body"])], CARD, label=found["label"])
            continue
        if _STATUS_LINE.match(line_text):
            story.append(Paragraph(pdf_text(line_text), status))
            continue
        story.append(
            Paragraph(pdf_text(line_text), small if chapter_name == "REFERENCE" else normal)
        )
    flush_findings()
    flush_summary()
    flush()

    path = output / "report.pdf"
    label = meta.title if meta else "평가 보고서"
    chapters = {}

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(line)
        canvas.setLineWidth(0.4)
        canvas.line(margin, 40, A4_WIDTH - margin, 40)
        canvas.setFont(FONT, 7.5)
        canvas.setFillColor(ink3)
        canvas.drawString(margin, 28, chapters.get(canvas.getPageNumber(), label))
        canvas.drawRightString(A4_WIDTH - margin, 28, str(doc.page))
        canvas.restoreState()

    class Report(BaseDocTemplate):
        def afterFlowable(self, flowable):
            level = getattr(flowable, "_toc_level", None)
            if level is None:
                return
            title = getattr(flowable, "_toc_title", None) or flowable.getPlainText()
            self.notify("TOCEntry", (level, title, self.page))
            if level == 0:
                chapters.setdefault(self.page, title)

    doc = Report(
        str(path),
        pagesize=A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=40 if compact else 50,
        bottomMargin=48 if compact else 56,
        title=label,
    )
    frame = Frame(margin, doc.bottomMargin, PAGE_WIDTH, doc.height, id="body", showBoundary=0)
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

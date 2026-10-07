"""컴파일된 그래프에서 아키텍처 다이어그램을 그린다.

    uv run python -m app.draw_architecture

손으로 그린 그림은 코드가 바뀌면 조용히 틀린 그림이 된다. 여기서는 **실제로 컴파일된
LangGraph 객체의 노드·간선**을 읽어 그리므로, 하위 에이전트끼리 간선이 생기면 그림에도
그대로 나타난다. 외부 렌더링 서비스(mermaid.ink 등)를 쓰지 않고 로컬에서만 그린다.

같은 구조의 Mermaid 원본도 함께 저장해 편집 가능하게 남긴다.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from graph.supervisor_graph import build_supervisor_graph
from runtime.models import MockBackend
from runtime.runner import load_input
from runtime.settings import ROOT, load_settings
from schemas.contracts import NODES

FONT = ROOT / "assets/fonts/NanumGothic-Regular.ttf"
SCALE = 2  # 2배로 그린 뒤 축소해 글자를 또렷하게 만든다

INK = (38, 42, 58)
MUTED = (122, 130, 150)
LINE = (170, 178, 196)
HUB_FILL = (232, 236, 255)
HUB_EDGE = (92, 104, 196)
AGENT_FILL = (255, 255, 255)
AGENT_EDGE = (186, 194, 214)
CODE_FILL = (246, 247, 250)
DISPATCH = (92, 104, 196)
RETURN = (150, 158, 178)

# 역할 상자에 적을 한 줄 설명. 그래프의 노드 이름과 1:1 로 맞춘다.
LABELS = {
    "tech": ("기술 조사", "원리 · 적용 조건 · 한계 · 잠정 TRL"),
    "market": ("시장성 평가", "규모 · 성장성 · 채택 · 생태계"),
    "stakeholder": ("이해관계자 평가", "경쟁 진영 · 도입자 · 업계 반응"),
    "domain": ("도메인 평가", "비용 · 성능 · 품질 · 운영 · 확장성"),
    "synthesis": ("평가 종합", "관점 간 일치·상충, 확정 TRL"),
    "report": ("보고서 작성", "SUMMARY ~ REFERENCE 재료"),
    "quality": ("보고서 품질 평가", "근거성 · 중립성 · 편향 · 커버리지"),
}
ORDER = ("tech", "market", "stakeholder", "domain", "synthesis", "report", "quality")


def font(size: int):
    return ImageFont.truetype(str(FONT), size * SCALE)


def box(draw, xy, fill, edge, radius=10, width=2):
    draw.rounded_rectangle(
        [c * SCALE for c in xy], radius=radius * SCALE, fill=fill, outline=edge, width=width
    )


def text(draw, xy, value, size, colour=INK, anchor="la"):
    draw.text((xy[0] * SCALE, xy[1] * SCALE), value, font=font(size), fill=colour, anchor=anchor)


def arrow(draw, points, colour, dashed=False, width=2):
    """꺾은선 + 끝점 화살표. dashed 는 조건부 간선(동적 라우팅)을 뜻한다."""
    scaled = [(x * SCALE, y * SCALE) for x, y in points]
    for start, end in zip(scaled, scaled[1:]):
        if dashed:
            _dashed_line(draw, start, end, colour, width)
        else:
            draw.line([start, end], fill=colour, width=width * SCALE)
    _head(draw, scaled[-2], scaled[-1], colour)


def _dashed_line(draw, start, end, colour, width, dash=7, gap=5):
    (x0, y0), (x1, y1) = start, end
    length = max(abs(x1 - x0), abs(y1 - y0))
    if not length:
        return
    steps = int(length // ((dash + gap) * SCALE)) or 1
    for i in range(steps + 1):
        a = i * (dash + gap) * SCALE / length
        b = min((i * (dash + gap) + dash) * SCALE / length, 1.0)
        if a >= 1:
            break
        draw.line(
            [
                (x0 + (x1 - x0) * a, y0 + (y1 - y0) * a),
                (x0 + (x1 - x0) * b, y0 + (y1 - y0) * b),
            ],
            fill=colour,
            width=width * SCALE,
        )


def _head(draw, previous, tip, colour, size=7):
    x0, y0 = previous
    x1, y1 = tip
    s = size * SCALE
    if abs(x1 - x0) >= abs(y1 - y0):
        d = 1 if x1 > x0 else -1
        draw.polygon(
            [(x1, y1), (x1 - d * s, y1 - s * 0.6), (x1 - d * s, y1 + s * 0.6)], fill=colour
        )
    else:
        d = 1 if y1 > y0 else -1
        draw.polygon(
            [(x1, y1), (x1 - s * 0.6, y1 - d * s), (x1 + s * 0.6, y1 - d * s)], fill=colour
        )


def verify(graph) -> list[str]:
    """그림을 그리기 전에 구조를 확인한다. 통신 제약이 깨지면 여기서 드러난다."""
    agents = set(ORDER)
    problems = []
    for edge in graph.edges:
        if edge.source in agents and edge.target != "supervisor":
            problems.append(f"{edge.source}→{edge.target}: 하위 에이전트 간 직접 간선")
        if edge.target in agents and edge.source != "supervisor":
            problems.append(f"{edge.source}→{edge.target}: Supervisor 를 거치지 않은 배정")
    return problems


def draw(output: Path) -> Path:
    inputs = {node: load_input(node, "acceptance") for node in NODES}
    compiled = build_supervisor_graph(inputs, "mock", load_settings(), MockBackend())
    structure = compiled.get_graph()
    problems = verify(structure)
    if problems:
        raise ValueError("그래프 구조가 Supervisor 제약을 위반합니다: " + "; ".join(problems))

    width, height = 1180, 760
    canvas = Image.new("RGB", (width * SCALE, height * SCALE), "white")
    d = ImageDraw.Draw(canvas)

    text(d, (40, 30), "Supervisor 기반 Multi-Agent 평가 그래프", 19)
    text(
        d,
        (40, 60),
        "순서는 간선이 아니라 Supervisor 의 판단에 있다. 모든 하위 에이전트는 Supervisor 하고만 통신한다.",
        11,
        MUTED,
    )

    # 입력 경로
    box(d, (40, 330, 150, 380), CODE_FILL, LINE)
    text(d, (95, 355), "START", 12, MUTED, anchor="mm")
    box(d, (180, 325, 330, 385), CODE_FILL, LINE)
    text(d, (255, 345), "initialize", 13, anchor="mm")
    text(d, (255, 366), "trace_id · 제어 메타 · 한도", 9, MUTED, anchor="mm")
    arrow(d, [(150, 355), (180, 355)], LINE)

    # 허브
    box(d, (380, 300, 610, 410), HUB_FILL, HUB_EDGE, width=3)
    text(d, (495, 327), "SUPERVISOR", 16, HUB_EDGE, anchor="mm")
    text(d, (495, 352), "현재 State 만 읽고 다음 분기를 고른다", 9.5, INK, anchor="mm")
    text(d, (495, 372), "add_conditional_edges", 9.5, MUTED, anchor="mm")
    text(d, (495, 391), "수집된 관점 · 근거 충분도 · 남은 한도", 9, MUTED, anchor="mm")
    arrow(d, [(330, 355), (380, 355)], LINE)

    # 하위 에이전트
    top, box_h, gap = 95, 66, 13
    left, right = 790, 1140
    for index, role in enumerate(ORDER):
        y = top + index * (box_h + gap)
        title, detail = LABELS[role]
        edge_colour = HUB_EDGE if role == "quality" else AGENT_EDGE
        box(d, (left, y, right, y + box_h), AGENT_FILL, edge_colour)
        text(d, (left + 18, y + 16), title, 12.5)
        text(d, (left + 18, y + 40), detail, 9, MUTED)
        mid = y + box_h / 2

        # 배정(조건부·동적)
        arrow(d, [(610, 345), (690, 345), (690, mid - 9), (left, mid - 9)], DISPATCH, dashed=True)
        # 보고(무조건) — Supervisor 로만 돌아온다
        arrow(d, [(left, mid + 9), (740, mid + 9), (740, 390), (610, 390)], RETURN)

    text(d, (790, 95 + 7 * (box_h + gap) + 4), "quality 는 보고서 생성 뒤에만 배정된다", 9, MUTED)

    # 종료 경로
    box(d, (380, 500, 610, 560), CODE_FILL, LINE)
    text(d, (495, 520), "finalize", 13, anchor="mm")
    text(d, (495, 541), "조판 · 인용 검사 · 재개용 상태 저장", 9, MUTED, anchor="mm")
    arrow(d, [(495, 410), (495, 500)], DISPATCH, dashed=True)
    box(d, (380, 610, 610, 655), CODE_FILL, LINE)
    text(d, (495, 632), "END", 12, MUTED, anchor="mm")
    arrow(d, [(495, 560), (495, 610)], LINE)

    # 자기 루프 (예산 소진 역할 제외 후 재판단)
    arrow(d, [(380, 390), (340, 390), (340, 430), (430, 430), (430, 410)], DISPATCH, dashed=True)
    text(d, (300, 447), "예산 소진 역할 제외 후 재판단", 8.5, MUTED)

    # 범례
    legend_y = 690
    d.line(
        [(40 * SCALE, legend_y * SCALE), (1140 * SCALE, legend_y * SCALE)],
        fill=(226, 230, 240),
        width=SCALE,
    )
    _dashed_line(
        d, (46 * SCALE, (legend_y + 24) * SCALE), (96 * SCALE, (legend_y + 24) * SCALE), DISPATCH, 2
    )
    text(d, (106, legend_y + 18), "조건부 배정 — 매 스텝 State 로 결정 (재작업 포함)", 9.5, INK)
    d.line(
        [(446 * SCALE, (legend_y + 24) * SCALE), (496 * SCALE, (legend_y + 24) * SCALE)],
        fill=RETURN,
        width=2 * SCALE,
    )
    text(d, (506, legend_y + 18), "보고 — 하위 에이전트는 Supervisor 로만 돌아온다", 9.5, INK)
    text(
        d,
        (846, legend_y + 18),
        "종료 보장: 스텝 · 시도 · 재작업 · 품질 라운드 상한",
        9.5,
        MUTED,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.resize((width, height), Image.LANCZOS).save(output)
    (output.with_suffix(".mmd")).write_text(structure.draw_mermaid(), encoding="utf-8")
    return output


def main() -> int:
    target = draw(ROOT / "docs/images/architecture-supervisor.png")
    print(f"아키텍처 그림: {target}")
    print(f"Mermaid 원본: {target.with_suffix('.mmd')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

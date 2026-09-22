"""Report node: rubric sections, E.2 outline assembly, guide E gates. Never content approval."""

import re

from graph.main_graph import RESULT_KEYS, build_main_graph
from runtime.models import MockBackend
from runtime.prompts import load_rubric, render
from runtime.reporting import (
    REPORT_SECTIONS,
    SUMMARY_MAX_CHARS,
    assemble_report,
    reference_entries,
    validate_report,
    write_report,
)
from runtime.runner import load_input
from runtime.settings import load_settings
from schemas.contracts import NODES, Evidence

OUTLINE = [
    "# SUMMARY",
    "# 1. 분석 배경",
    "# 2. 기술 선정",
    "# 3. 기술 개요",
    "## 3.1 KIVI",
    "## 3.2 ITME",
    "## 3.3 기술 성숙도(TRL)",
    "# 4. 관점별 평가",
    "## 4.1 시장성",
    "## 4.2 이해관계자",
    "## 4.3 도메인 적용",
    "# 5. 시사점",
    "## 5.1 관점 간 상충 지점",
    "## 5.2 조건부 시사점과 보완 관계 가능성",
    "# 6. 한계점",
    "## 6.1 정보의 한계",
    "## 6.2 확증편향 방지 조치",
    "# REFERENCE",
]


def mock_pipeline():
    inputs = {n: load_input(n, "acceptance") for n in NODES}
    return build_main_graph(inputs, "mock", load_settings(), MockBackend()).invoke({})


def test_report_rubric_criteria_are_the_design_sections():
    rubric = load_rubric("report")
    assert [c.id for c in rubric.criteria] == list(REPORT_SECTIONS)
    assert all(c.judgments == ["구성 충족", "부분 구성", "확인 불가"] for c in rubric.criteria)
    # Two technologies x five sections fits the shared per-node question limit.
    assert 2 * len(rubric.criteria) <= load_settings().limits.questions
    assert len(load_input("report", "acceptance").questions) == 2 * len(rubric.criteria)


def test_report_prompt_states_section_mapping_and_bans_ranking():
    data = load_input("report", "acceptance")
    system, user, _ = render("report", data, data.evidence)
    for section in REPORT_SECTIONS:
        assert section in system and section in user
    assert "우열" in system and "추천" in system and "공개 정보 기반 추정" in system
    assert "600자" in system  # SUMMARY half-page rule from design E.1
    assert "prior_results" in user and '"synthesis"' in user  # upstream results are visible


def test_assembled_report_follows_design_e2_outline():
    state = mock_pipeline()
    text = assemble_report(state, RESULT_KEYS, "mock")
    positions = [text.index(h + "\n") for h in OUTLINE]
    assert positions == sorted(positions)
    assert text.startswith("# SUMMARY") and text.rstrip().rsplit("\n# ", 1)[1].startswith(
        "REFERENCE"
    )
    # Report-node sentences are routed by criterion into their sections.
    kivi_overview = text[text.index("## 3.1 KIVI") : text.index("## 3.2 ITME")]
    assert "- KIVI: " in kivi_overview and "- ITME: " not in kivi_overview
    market = text[text.index("## 4.1 시장성") : text.index("## 4.2 이해관계자")]
    assert "KIVI 절 구성:" in market and "ITME 절 구성:" in market
    assert "| 항목 | KIVI | ITME |" in market and "| 관점 | KIVI | ITME |" in text
    assert "잠정 TRL (기술 조사)" in text and "확정 TRL (평가 종합)" in text
    # Fixed chapters come from the design document and never carry performance numbers.
    background = text[text.index("# 1. 분석 배경") : text.index("# 3. 기술 개요")]
    assert "우열을 판정하는 문서가 아니다" in background and "탈락 후보" in background
    assert not re.search(r"\d+(\.\d+)?\s*(배|%|×)", background)


def test_report_gate_rejects_long_summary_and_ranking_language():
    state = mock_pipeline()
    text = assemble_report(state, RESULT_KEYS, "mock")
    baseline = set(validate_report(state, RESULT_KEYS, text, "fixture")["problems"])
    report = state["report"].model_copy(deep=True)
    report.result.summary = "가" * (SUMMARY_MAX_CHARS + 1)
    report.result.claims[0].text = "KIVI를 데이터센터에 추천한다."
    state["report"] = report
    problems = set(validate_report(state, RESULT_KEYS, text, "fixture")["problems"]) - baseline
    assert any("SUMMARY가 1/2 페이지" in p for p in problems)
    assert any("우열·추천 표현 검출" in p for p in problems)


def test_reference_groups_chunks_per_document_in_guide_format():
    base = (
        load_input("tech")
        .evidence[0]
        .model_copy(
            update={
                "source_type": "paper",
                "authors": "Zirui Liu et al.",
                "year": 2024,
                "venue": "ICML 2024",
                "affiliation": "first_party",
            }
        )
    )
    chunk = base.model_copy(update={"id": "kivi-chunk-p3", "page": 3})
    web = Evidence(
        id="web-1",
        text="x",
        title="Example post",
        url="https://example.invalid/post",
        technology="KIVI",
        source_type="web",
        scope="context",
        publisher="Example Org",
        site="example.invalid",
        published_at="2026-09-01",
        retrieved_at="2026-09-22",
        affiliation="independent",
    )
    entries = reference_entries([base, chunk, web])
    assert len(entries) == 2
    assert entries[0].startswith(
        f"1. Zirui Liu et al.(2024). {base.title}. ICML 2024. {base.url} [자사 자료]"
    )
    assert f"[{base.id}] p.1, [kivi-chunk-p3] p.3" in entries[0]
    assert entries[1] == (
        "2. Example Org(2026-09-01). Example post. example.invalid, https://example.invalid/post "
        "[독립 자료] 인용: [web-1]"
    )


def test_report_pdf_renders_every_table_width(tmp_path):
    state = mock_pipeline()
    text = assemble_report(state, RESULT_KEYS, "mock")
    path = write_report(text, tmp_path)
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == text
    assert (tmp_path / "report.pdf").stat().st_size > 0 and path.endswith("report.pdf")

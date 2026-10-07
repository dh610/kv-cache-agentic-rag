"""조판된 보고서가 제출 가능한지 검사한다. 사실성 승인이 아니라 인용·형식 검사다."""

from __future__ import annotations

from runtime.handoff import reference_issues
from runtime.report.builder import report_sources, used_ids

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

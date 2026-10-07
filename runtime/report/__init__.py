"""보고서 계층. 세 책임을 세 모듈로 나눈다.

    builder     무엇을 어떤 순서로 실을지 — 설계서 목차로 조립
    validation  제출 가능한지 — 인용 연결·서지 필드·분량·금지 표현
    renderer    어떻게 보이는지 — PDF 조판

예전에는 셋이 한 파일(1,188줄)에 있었다. 압축 조판을 넣을 때 세 책임이 서로 얽혀
모듈 전역 플래그를 함수 사이로 흘려보내야 했고, 그 과정에서 생긴 결함이 실행을
마지막 단계에서 통째로 날렸다.
"""

from runtime.report.builder import (
    LABELS,
    REPORT_SECTIONS,
    ROLE_LABELS,
    assemble_report,
    citation_numbers,
    findings_line,
    reference_entries,
    reference_text,
    report_sources,
    strip_internal,
    used_ids,
)
from runtime.report.renderer import pdf_text, write_report
from runtime.report.validation import (
    RECOMMENDATION_PHRASES,
    SUMMARY_MAX_CHARS,
    report_text_issues,
    validate_report,
)

__all__ = [
    "LABELS",
    "RECOMMENDATION_PHRASES",
    "REPORT_SECTIONS",
    "ROLE_LABELS",
    "SUMMARY_MAX_CHARS",
    "assemble_report",
    "citation_numbers",
    "findings_line",
    "pdf_text",
    "reference_entries",
    "reference_text",
    "report_sources",
    "report_text_issues",
    "strip_internal",
    "used_ids",
    "validate_report",
    "write_report",
]

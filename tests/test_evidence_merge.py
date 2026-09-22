"""근거 병합: 정체성 충돌은 오류, 서지 메타데이터 차이는 병합 (병렬 노드가 같은 웹 페이지를 각자 가져오는 경우)."""

import pytest

from rag.evidence import merge_evidence
from schemas.contracts import Evidence


def web(**kw):
    base = dict(
        id="web-same",
        text="same body",
        title="Page",
        url="https://x.invalid/p",
        technology="KIVI",
        source_type="web",
        scope="context",
        site="x.invalid",
    )
    return Evidence(**{**base, **kw})


def test_same_page_with_different_title_or_date_merges_and_fills_missing():
    first = web(title="Page (short)", published_at=None, publisher=None)
    second = web(title="Page (full title)", published_at="2026-01-02", publisher="Org")
    merged = merge_evidence([first], [second])
    assert len(merged) == 1
    out = merged[0]
    assert out.title == "Page (short)"  # 먼저 본 값 유지
    assert out.published_at == "2026-01-02" and out.publisher == "Org"  # 비어 있던 필드만 채움


def test_same_id_with_different_text_is_a_conflict():
    with pytest.raises(ValueError, match="Conflicting evidence"):
        merge_evidence([web(text="body A")], [web(text="body B")])


def test_same_id_with_different_technology_or_page_is_a_conflict():
    with pytest.raises(ValueError):
        merge_evidence([web()], [web(technology="ITME")])
    paper = dict(source_type="paper", document_role="target", document_id="kivi", scope="target")
    with pytest.raises(ValueError):
        merge_evidence([web(**paper, page=3)], [web(**paper, page=4)])


def test_known_annotation_is_not_overwritten_by_unknown():
    first = web(affiliation="first_party", affiliation_reason="자사 논문", stance="positive")
    second = web()  # unknown 기본값
    out = merge_evidence([first], [second])[0]
    assert out.affiliation == "first_party" and out.stance == "positive"
    out = merge_evidence([second], [first])[0]
    assert out.affiliation == "first_party" and out.affiliation_reason == "자사 논문"

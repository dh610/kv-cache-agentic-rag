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


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("field,other", [("affiliation", "independent"), ("stance", "critical")])
def test_conflicting_semantic_labels_are_not_resolved_by_input_order(reverse, field, other):
    first = web(affiliation="first_party", stance="positive")
    second = first.model_copy(update={field: other})
    pair = [second, first] if reverse else [first, second]
    with pytest.raises(ValueError, match="Conflicting evidence"):
        merge_evidence(pair)


@pytest.mark.parametrize("reverse", [False, True])
def test_a_document_found_by_both_search_layers_counts_as_direct(reverse):
    """직접 검색과 접근 전반 검색이 같은 문서를 찾으면 직접 근거로 본다.

    설계서 A.4 의 2층 검색에서 흔히 생긴다. 이를 충돌로 보면 실행 전체가 죽는다
    (live 점검에서 마지막 집계 단계가 중단됨). 입력 순서와 무관하게 같은 결과를 준다.
    """
    direct = web(scope="target")
    background = direct.model_copy(update={"scope": "context"})
    pair = [background, direct] if reverse else [direct, background]
    assert merge_evidence(pair)[0].scope == "target"


def test_filling_affiliation_uses_its_own_reason_without_mutating_inputs():
    first = web(affiliation_reason="아직 미분류")
    second = web(affiliation="first_party", affiliation_reason="저자 자사 연구")
    out = merge_evidence([first], [second])[0]
    assert out.affiliation == "first_party"
    assert out.affiliation_reason == "저자 자사 연구"
    assert first.affiliation == "unknown" and first.affiliation_reason == "아직 미분류"

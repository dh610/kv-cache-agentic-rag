from schemas.contracts import Evidence

# 인용 대상의 정체성. 이 필드가 다르면 같은 ID 라도 다른 근거이므로 병합하지 않고 오류로 본다.
IDENTITY_FIELDS = (
    "text",
    "url",
    "technology",
    "source_type",
    "document_role",
    "document_id",
    "page",
)
# 서지·주석 메타데이터. 같은 페이지를 다시 가져올 때 제목·발행일 등이 달라질 수 있어 충돌로 보지 않는다.
METADATA_FIELDS = (
    "title",
    "authors",
    "year",
    "venue",
    "publisher",
    "site",
    "published_at",
    "affiliation",
    "affiliation_reason",
    "stance",
    "scope",
)
_UNSET = (None, "", "unknown")


def merge_evidence(*groups: list[Evidence]) -> list[Evidence]:
    """An ID is an immutable citation target; conflicting content must not overwrite it.

    같은 ID 의 본문·URL·기술·문서·페이지가 다르면 ValueError. 메타데이터만 다르면 먼저 본 항목을 유지하고,
    비어 있던 필드(None/""/"unknown")만 나중 항목으로 채운다. 병렬 노드가 같은 웹 페이지를 각자 가져오면
    Tavily 응답의 제목·발행일이 달라질 수 있는데, 그 때문에 실행 전체가 죽어서는 안 된다.
    """
    merged: dict[str, Evidence] = {}
    for group in groups:
        for item in group:
            current = merged.get(item.id)
            if current is None:
                merged[item.id] = item
                continue
            if any(getattr(current, f) != getattr(item, f) for f in IDENTITY_FIELDS):
                raise ValueError(f"Conflicting evidence for ID {item.id}")
            fill = {
                f: getattr(item, f)
                for f in METADATA_FIELDS
                if getattr(current, f) in _UNSET and getattr(item, f) not in _UNSET
            }
            if fill:
                merged[item.id] = current.model_copy(update=fill)
    return list(merged.values())

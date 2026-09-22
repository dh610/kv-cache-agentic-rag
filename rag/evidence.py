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
    "scope",
)
# 서지 차이는 허용한다. 출처 관계·입장의 상충하는 확정값은 아래에서 별도로 거부한다.
METADATA_FIELDS = (
    "title",
    "authors",
    "year",
    "venue",
    "citation_id",
    "publisher",
    "site",
    "published_at",
    "affiliation",
    "affiliation_reason",
    "stance",
)
ANNOTATION_FIELDS = ("affiliation", "stance")
_UNSET = (None, "", "unknown")


def merge_evidence(*groups: list[Evidence]) -> list[Evidence]:
    """An ID is an immutable citation target; conflicting content must not overwrite it.

    같은 ID 의 본문·URL·기술·문서·페이지·scope가 다르거나 출처 관계/입장의 확정값이 상충하면 ValueError.
    서지 메타데이터만 다르면 먼저 본 항목을 유지하고,
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
            # 출처 관계/입장은 등급 해석에 사용된다. 상충하는 확정값을 입력 순서로 선택하지 않는다.
            if any(
                getattr(current, f) not in _UNSET
                and getattr(item, f) not in _UNSET
                and getattr(current, f) != getattr(item, f)
                for f in ANNOTATION_FIELDS
            ):
                raise ValueError(f"Conflicting evidence annotation for ID {item.id}")
            fill = {
                f: getattr(item, f)
                for f in METADATA_FIELDS
                if getattr(current, f) in _UNSET and getattr(item, f) not in _UNSET
            }
            if "affiliation" in fill:
                # unknown의 옛 사유를 새 first_party/independent 판정에 붙이지 않는다.
                fill["affiliation_reason"] = item.affiliation_reason
            if fill:
                merged[item.id] = current.model_copy(update=fill)
    return list(merged.values())

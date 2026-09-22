from schemas.contracts import Evidence


def merge_evidence(*groups: list[Evidence]) -> list[Evidence]:
    """An ID is an immutable citation target; conflicting content must not overwrite it."""
    merged = {}
    for group in groups:
        for item in group:
            if item.id in merged and merged[item.id].model_dump(
                exclude={"retrieved_at"}
            ) != item.model_dump(exclude={"retrieved_at"}):
                raise ValueError(f"Conflicting evidence for ID {item.id}")
            merged.setdefault(item.id, item)
    return list(merged.values())

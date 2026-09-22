"""모델에게는 짧은 근거 번호(E1~En)를 보여주고, 받은 뒤 실제 ID로 되돌린다.

실제 근거 ID 는 `kivi-df31ef32d71b-p6-0`, `web-6a64049b0c54d037` 처럼 길다. 모델이 이를
그대로 옮겨 적게 하면 오타·혼동이 나고, live 점검에서 `missing/unknown evidence IDs` 와
`Judge cited evidence not supplied by claim` 이 반복됐다. 번호를 쓰면 실수 여지가 줄고,
모르는 번호는 코드에서 버릴 수 있다.
"""

from __future__ import annotations

import re

from schemas.contracts import ClaimCheck, Coverage, Evidence, NodeResult

# "공개 근거에서 확인되지 않는다" 류는 부재 진술이다. 원문이 뒷받침할 수 있는 종류의
# 문장이 아니므로 Judge 에 보내면 언제나 unsupported 가 되고, 그 때문에 노드 전체가
# 미검증으로 떨어졌다. 부재는 판정 대상이 아니라 그대로 미확인 항목이다.
ABSENCE = re.compile(
    r"확인되지\s*않|확인할\s*수\s*없|공개(되지|된\s*자료가)?\s*않|"
    r"근거가?\s*(없|부재|부족)|자료가?\s*(없|부재)|정보가?\s*(없|부재)|"
    r"판단이?\s*(불가|어렵)|미확인|확인\s*불가"
)


def split_absence_claims(result: NodeResult) -> tuple[NodeResult, list[str]]:
    """부재를 말하는 주장을 claims 에서 빼내 미확인 목록으로 옮긴다."""
    out = result.model_copy(deep=True)
    moved = [c.text for c in out.claims if ABSENCE.search(c.text)]
    out.claims = [c for c in out.claims if not ABSENCE.search(c.text)]
    out.unverified.extend(moved)
    return out, moved


def alias(evidence: list[Evidence]) -> tuple[list[Evidence], dict[str, str]]:
    """근거 목록을 E1.. 번호판으로 바꾸고, 번호→실제 ID 사전을 돌려준다."""
    labelled, back = [], {}
    for index, item in enumerate(evidence, start=1):
        label = f"E{index}"
        back[label] = item.id
        labelled.append(item.model_copy(update={"id": label}))
    return labelled, back


def _ids(values: list[str], back: dict[str, str]) -> list[str]:
    """아는 번호는 실제 ID 로 바꾸고, 모르는 값은 그대로 남긴다.

    모르는 값을 여기서 지우면 지어낸 인용이 조용히 사라져 계약 검사가 잡지 못한다.
    그대로 두면 `missing/unknown evidence IDs` 로 드러나고 수정 재시도가 걸린다.
    """
    return list(dict.fromkeys(back.get(value, value) for value in values))


CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_text(value: str) -> str:
    return CONTROL.sub("", value).strip()


def restore_result(result: NodeResult, back: dict[str, str]) -> NodeResult:
    restored = result.model_copy(deep=True)
    # 원문 PDF·웹에서 옮겨 붙은 제어문자가 판정 값까지 오염시킨 적이 있다(live 점검).
    restored.summary = clean_text(restored.summary)
    restored.unverified = [clean_text(u) for u in restored.unverified]
    restored.limitations = [clean_text(x) for x in restored.limitations]
    for claim in restored.claims:
        claim.text = clean_text(claim.text)
        claim.conditions = [clean_text(c) for c in claim.conditions]
    for item in restored.assessments:
        item.judgment = clean_text(item.judgment)
        item.rationale = clean_text(item.rationale)
    for estimate in restored.trl_estimates:
        estimate.rationale = clean_text(estimate.rationale)
    for item in [*restored.claims, *restored.assessments, *restored.trl_estimates]:
        item.evidence_ids = _ids(item.evidence_ids, back)
    return restored


def drop_cross_technology(result: NodeResult, evidence: list[Evidence]) -> NodeResult:
    """다른 기술의 근거를 인용한 항목에서 그 인용만 떼어낸다.

    설계서 C.2: 다른 기술의 실험 결과를 대상 기술의 근거로 전용하지 않는다. 기술별로 나눠
    생성해도 근거 번호는 전체 목록 기준이라, 모델이 자기 묶음에 없는 앞 번호를 적으면 다른
    기술의 근거로 복원됐다(live 점검에서 ITME 도메인 판정이 전부 기각됨). 목록에 없는
    ID(지어낸 인용)는 남겨 계약 검사가 잡게 한다.
    """
    by_id = {e.id: e for e in evidence}

    def allowed(item, eid: str) -> bool:
        found = by_id.get(eid)
        if found is None:
            return True
        return (
            found.technology in (item.technology, "other")
            or found.document_role == "reference"
        )

    out = result.model_copy(deep=True)
    for item in [*out.claims, *out.assessments, *out.trl_estimates]:
        item.evidence_ids = [eid for eid in item.evidence_ids if allowed(item, eid)]
    return out


def restore_coverage(items: list[Coverage], back: dict[str, str]) -> list[Coverage]:
    return [c.model_copy(update={"evidence_ids": _ids(c.evidence_ids, back)}) for c in items]


def restore_checks(checks: list[ClaimCheck], back: dict[str, str]) -> list[ClaimCheck]:
    return [c.model_copy(update={"evidence_ids": _ids(c.evidence_ids, back)}) for c in checks]

"""Evidence and TRL checks shared by the runtime and offline scorer."""

from collections import Counter

from schemas.contracts import ClaimCheck, Evidence, NodeResult


def verified_evidence_ids(
    result: NodeResult,
    checks: list[ClaimCheck],
    evidence: list[Evidence],
    technology: str,
    criterion: str | None = None,
) -> set[str]:
    """Only count supplied citations confirmed by a unique check of the same claim."""
    known = {e.id for e in evidence}
    claim_counts = Counter(c.id for c in result.claims)
    check_counts = Counter(c.claim_id for c in checks)
    checked = {c.claim_id: c for c in checks}
    verified = set()
    for claim in result.claims:
        if (
            claim.technology != technology
            or (criterion is not None and claim.criterion != criterion)
            or claim_counts[claim.id] != 1
            or check_counts[claim.id] != 1
        ):
            continue
        check = checked[claim.id]
        if check.label == "supported":
            verified.update(set(claim.evidence_ids) & set(check.evidence_ids) & known)
    return verified


def trim_to_verified(result: NodeResult, checks: list[ClaimCheck], evidence: list[Evidence]):
    """확인된 인용이 하나라도 있는 항목에서, 확인되지 않은 인용만 지운다.

    Judge 는 "판단에 사용한 근거"만 적으므로 생성기가 5건을 인용하고 Judge 가 4건을
    확인하는 일이 정상적으로 생긴다. 이를 위반으로 처리하면 근거가 확인된 등급까지
    버려진다. 반대로 확인된 인용이 하나도 없으면 손대지 않는다. 그 경우는 정리할 표현
    오류가 아니라 보고해야 할 계약 위반이며, 이후 단계가 확인 불가로 내린다.
    근거 목록에 없는 ID(지어낸 인용)는 지우지 않는다. 계약 검사가 잡아야 한다.
    """
    out = result.model_copy(deep=True)
    known = {e.id for e in evidence}

    def repair(item, verified):
        if not verified or not set(item.evidence_ids) & verified:
            return False
        item.evidence_ids = [i for i in item.evidence_ids if i in verified or i not in known]
        return True

    for item in out.assessments:
        verified = verified_evidence_ids(out, checks, evidence, item.technology, item.criterion)
        if repair(item, verified) and not item.evidence_ids and item.judgment != "확인 불가":
            item.judgment = "확인 불가"
            item.rationale = "확인된 근거가 남지 않아 판정을 보류합니다."
    for estimate in out.trl_estimates:
        verified = verified_evidence_ids(out, checks, evidence, estimate.technology)
        if repair(estimate, verified) and not estimate.evidence_ids:
            estimate.level = None
            estimate.rationale = "확인된 근거가 남지 않아 단계를 보류합니다."
    return out


def enforce_direct_evidence(
    result: NodeResult, evidence: list[Evidence], criteria: list[str]
) -> NodeResult:
    """선정 기술 자체를 말하는 항목은 직접 근거(scope=target)만 쓰게 한다.

    설계서 A.4·C.2: 시장·이해관계자 등급은 접근 전반(background) 근거로도 매길 수 있지만,
    선정 기술 자체의 채택과 TRL 은 그 기술을 직접 다룬 자료로만 판단한다. 직접 근거가
    없으면 추정하지 않고 확인 불가로 남긴다.
    """
    out = result.model_copy(deep=True)
    known = {e.id for e in evidence}
    # 근거 목록에 없는 ID(지어낸 인용)는 남겨 계약 검사가 잡게 한다.
    direct = {e.id for e in evidence if e.scope == "target"} | {
        i
        for item in [*out.assessments, *out.trl_estimates]
        for i in item.evidence_ids
        if i not in known
    }
    for item in out.assessments:
        if item.criterion not in criteria or item.judgment == "확인 불가":
            continue
        item.evidence_ids = [i for i in item.evidence_ids if i in direct]
        if not item.evidence_ids:
            item.judgment = "확인 불가"
            item.rationale = (
                "선정 기술을 직접 다룬 근거가 없어 확인 불가로 남깁니다. "
                "접근 전반 근거는 이 항목의 판정에 쓰지 않습니다."
            )
    for estimate in out.trl_estimates:
        if estimate.level is None:
            continue
        estimate.evidence_ids = [i for i in estimate.evidence_ids if i in direct]
        if not estimate.evidence_ids:
            estimate.level = None
            estimate.rationale = "선정 기술을 직접 다룬 근거가 없어 단계를 보류합니다."
    return out


def tech_trl_errors(result: NodeResult) -> list[str]:
    """Validate optional structured TRL against the same tech node's assessment.

    Empty estimates remain compatible with older callers. Synthesis may reassess
    maturity using additional evidence and is not compared with the tech stage.
    """
    if result.node != "tech":
        return []
    errors = []
    if len({t.technology for t in result.trl_estimates}) != len(result.trl_estimates):
        errors.append("Duplicate TRL technologies")
    for estimate in result.trl_estimates:
        maturity = [
            a
            for a in result.assessments
            if a.technology == estimate.technology and a.criterion == "maturity"
        ]
        expected = f"TRL {estimate.level}" if estimate.level is not None else "확인 불가"
        if len(maturity) != 1 or maturity[0].judgment != expected:
            errors.append(f"{estimate.technology}: TRL estimate disagrees with maturity assessment")
    return errors

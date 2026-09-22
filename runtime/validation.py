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

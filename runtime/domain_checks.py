"""Deterministic domain-node rules that the shared citation Judge does not cover.

The shared contract check verifies that citations exist and that non-unknown
judgments rest on supported claims. It does not know which evidence counts as a
direct source for a technology, which rubric grades need external cases, or
which grades may never rest on inference alone. These rules encode those
domain-specific necessary conditions. Passing them is not a factual review.

``draft_rule_errors`` needs no Judge output, so the node graph can run it before
verification and route violations through the bounded expression-fix retry.
``domain_rule_errors`` adds the checks that need Judge labels, for handoff.
"""

from __future__ import annotations

from schemas.contracts import Evidence, NodeInput, NodeResult, NodeRun

DISCLAIMER = "공개 정보 기반 추정"
EXPERIMENTAL_CRITERIA = ("performance", "quality", "scalability")
# Phrases that report missing information; such content belongs in unverified/limitations.
ABSENCE_PHRASES = (
    "명시되지 않",
    "언급되지 않",
    "제공되지 않",
    "보고되지 않",
    "확인되지 않",
    "정의되지 않",
    "제시되지 않",
    "포함하지 않",
    "미명시",
    "미확인",
    "미상",
    "알 수 없",
    "확인할 수 없",
    "판단할 수 없",
    "근거가 없",
    "정보가 없",
    "자료가 없",
    "수치가 없",
)


def is_direct(evidence: Evidence, technology: str) -> bool:
    return evidence.technology == technology and evidence.scope == "target"


def is_external_case(evidence: Evidence, technology: str) -> bool:
    """An application case independent of the paper: a web source about this technology."""
    return evidence.technology == technology and evidence.source_type == "web"


def draft_rule_errors(
    data: NodeInput, result: NodeResult, evidence_list: list[Evidence]
) -> list[str]:
    """Rules checkable on the Generator draft alone, before any Judge call."""
    errors: list[str] = []
    if result.node != "domain":
        return errors
    evidence = {e.id: e for e in evidence_list}
    for claim in result.claims:
        for eid in claim.evidence_ids:
            item = evidence.get(eid)
            if item and item.technology not in (claim.technology, "other"):
                errors.append(
                    f"{claim.id}: cites {item.technology} evidence for {claim.technology}"
                )
        text = claim.text + " " + " ".join(claim.conditions)
        hit = next((p for p in ABSENCE_PHRASES if p in text), None)
        if hit:
            errors.append(
                f"{claim.id}: claim states absence of information ('{hit}'); "
                "keep only what the source reports and move the gap to unverified"
            )
        if (
            claim.kind == "fact"
            and claim.criterion in EXPERIMENTAL_CRITERIA
            and not any(c.strip() for c in claim.conditions)
        ):
            errors.append(
                f"{claim.id}: experimental claim lacks model/hardware/workload/baseline conditions"
            )
    for item in result.assessments:
        key = f"{item.technology}/{item.criterion}"
        if item.judgment == "확인 불가":
            # The runtime may overwrite an unknown item's rationale; only graded items are checked.
            continue
        if DISCLAIMER not in item.rationale:
            errors.append(f"{key}: rationale must state {DISCLAIMER}")
        cited = [evidence[eid] for eid in item.evidence_ids if eid in evidence]
        if not any(is_direct(e, item.technology) for e in cited):
            errors.append(f"{key}: judgment lacks direct target evidence for the technology")
        if any(e.technology not in (item.technology, "other") for e in cited):
            errors.append(f"{key}: cites another technology's evidence")
        if item.criterion == "performance" and item.judgment == "적합":
            if not any(is_external_case(e, item.technology) for e in cited):
                errors.append(f"{key}: 적합 requires an external case beyond the paper")
    return errors


def domain_rule_errors(data: NodeInput, run: NodeRun) -> list[str]:
    """Draft rules plus the checks that need Judge labels; used at handoff and scoring."""
    errors = draft_rule_errors(data, run.result, run.evidence)
    if run.node != "domain":
        return errors
    supported = {c.claim_id for c in run.checks if c.label == "supported"}
    for item in run.result.assessments:
        if item.judgment != "부적합":
            continue
        facts = [
            c
            for c in run.result.claims
            if c.id in supported
            and c.kind == "fact"
            and c.technology == item.technology
            and c.criterion == item.criterion
        ]
        if not facts:
            errors.append(
                f"{item.technology}/{item.criterion}: 부적합 requires a supported fact claim, not inference"
            )
    return errors

"""Synthesis-specific contract checks that the shared citation Judge cannot make.

The shared Judge only verifies that a claim's wording matches the evidence it cites.
These rules check what the synthesis role owes its neighbours: an agreement or
conflict judgment needs two evaluated perspectives and evidence from both, the final
TRL cannot rise above the tech node's provisional level without market target
evidence, one technology's evidence is never borrowed for the other, and upstream
unresolved items survive. Passing them is a contract check, not a factual review.
"""

from __future__ import annotations

from schemas.contracts import Evidence, NodeInput, NodeResult

UNKNOWN = "확인 불가"


def cited_ids(result: NodeResult) -> set[str]:
    return {
        eid
        for item in [*result.claims, *result.assessments, *result.trl_estimates]
        for eid in item.evidence_ids
    }


def evaluated_roles(data: NodeInput, technology: str) -> set[str]:
    """Upstream roles holding at least one judgment other than 확인 불가 for a technology."""
    return {
        role
        for role, prior in data.prior_results.items()
        if any(a.technology == technology and a.judgment != UNKNOWN for a in prior.assessments)
    }


def synthesis_errors(data: NodeInput, result: NodeResult, evidence: list[Evidence]) -> list[str]:
    errors: list[str] = []
    by_id = {e.id: e for e in evidence}
    targets = set(data.target_techs.values())
    role_ids = {role: cited_ids(prior) for role, prior in data.prior_results.items()}

    for item in [*result.claims, *result.assessments]:
        label = getattr(item, "id", None) or f"{item.technology}/{item.criterion}"
        for eid in item.evidence_ids:
            found = by_id.get(eid)
            if found and found.technology in targets and found.technology != item.technology:
                errors.append(
                    f"{label}: {found.technology} evidence {eid} cannot support {item.technology}"
                )

    for a in result.assessments:
        if a.judgment == UNKNOWN:
            continue
        roles = evaluated_roles(data, a.technology)
        if a.criterion == "consistency":
            if len(roles) < 2:
                errors.append(
                    f"{a.technology}/consistency: {a.judgment} needs two evaluated perspectives, "
                    f"found {len(roles)}"
                )
            citing = {role for role, ids in role_ids.items() if ids & set(a.evidence_ids)}
            if len(citing) < 2:
                errors.append(
                    f"{a.technology}/consistency: cite evidence used by at least two perspectives"
                )
        elif a.criterion == "implications" and not roles:
            errors.append(
                f"{a.technology}/implications: {a.judgment} needs an evaluated perspective"
            )

    tech_prior = data.prior_results.get("tech")
    provisional = {t.technology: t for t in tech_prior.trl_estimates} if tech_prior else {}
    market_ids = role_ids.get("market", set())
    final = {t.technology for t in result.trl_estimates}
    for t in result.trl_estimates:
        if t.provisional:
            errors.append(f"TRL {t.technology}: synthesis must return provisional=false")
        for eid in t.evidence_ids:
            found = by_id.get(eid)
            if found and (found.technology != t.technology or found.scope != "target"):
                errors.append(f"TRL {t.technology}: {eid} is not direct target evidence")
        base = provisional.get(t.technology)
        floor = base.level if base and base.level is not None else 0
        if t.level is not None and t.level > floor and not (set(t.evidence_ids) & market_ids):
            errors.append(
                f"TRL {t.technology}: level {t.level} exceeds provisional "
                f"{base.level if base else None} without market target evidence"
            )
    for technology in provisional:
        if technology not in final:
            errors.append(
                f"TRL {technology}: provisional estimate needs a final level or explicit level=null"
            )

    for role, prior in data.prior_results.items():
        if prior.unverified and not any(role in u.lower() for u in result.unverified):
            errors.append(f"{role}: upstream unverified items must be preserved as '{role}: ...'")
    return errors

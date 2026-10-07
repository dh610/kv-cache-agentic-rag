"""Assemble a first report from validated results without rewriting facts with an LLM."""

import hashlib

from rag.evidence import merge_evidence
from runtime.reporting import used_ids
from schemas.contracts import Assessment, NodeResult, NodeRun

SECTION_ROLES = {
    "overview": "tech",
    "market": "market",
    "stakeholder": "stakeholder",
    "domain": "domain",
    "implications": "synthesis",
}


def assemble_report_node(data, runs, mode):
    required = set(SECTION_ROLES.values())
    if not required.issubset(runs):
        raise ValueError("All four evaluations and synthesis are required before report assembly")
    claims, checks, assessments = [], [], []
    for q in data.questions:
        role = SECTION_ROLES[q.criterion]
        run = runs[role]
        supported = {c.claim_id: c for c in run.checks if c.label == "supported"}
        originals = [
            c for c in run.result.claims if c.technology == q.technology and c.id in supported
        ]
        verified = set()
        for claim in originals:
            check = supported[claim.id]
            ids = set(claim.evidence_ids) & set(check.evidence_ids)
            if not ids:
                continue
            cid = f"report-{role}-{claim.id}"
            claims.append(claim.model_copy(update={"id": cid, "criterion": q.criterion}))
            checks.append(check.model_copy(update={"claim_id": cid}))
            verified.update(ids)
        items = [a for a in run.result.assessments if a.technology == q.technology]
        incomplete = (
            not items or any(a.judgment == "확인 불가" for a in items) or run.status != "completed"
        )
        assessments.append(
            Assessment(
                technology=q.technology,
                criterion=q.criterion,
                judgment=("부분 구성" if incomplete else "구성 충족") if verified else "확인 불가",
                rationale="; ".join(
                    f"{role}/{a.technology}/{a.criterion}: {a.judgment}. {a.rationale}"
                    for a in items
                )
                or "해당 절에 검증된 원문 근거가 없습니다.",
                evidence_ids=sorted(verified),
            )
        )
    unverified = [
        f"{role}: {note}" for role in sorted(required) for note in runs[role].result.unverified
    ]
    unverified.extend(
        f"{a.technology}/{a.criterion}: 확인 불가" for a in assessments if a.judgment == "확인 불가"
    )
    errors = [
        f"{role}: {error}" for role in sorted(required) for error in runs[role].validation_errors
    ]
    limitations = list(
        dict.fromkeys(note for role in sorted(required) for note in runs[role].result.limitations)
    )
    limitations.append(
        "1차 보고서: 상위 노드의 검증된 문장·판정을 그대로 조립했으며 새로운 사실을 생성하지 않았습니다."
    )
    result = NodeResult(
        node="report",
        summary=runs["synthesis"].result.summary,
        claims=claims,
        assessments=assessments,
        unverified=unverified,
        limitations=limitations,
    )
    evidence = merge_evidence(
        *(
            [e for e in runs[role].evidence if e.id in used_ids(runs[role])]
            for role in sorted(required)
        )
    )
    incomplete = errors or unverified or any(runs[r].status != "completed" for r in required)
    failed = any(runs[r].status == "failed" for r in required)
    return NodeRun(
        node="report",
        mode=mode,
        status="failed" if failed else "needs_revision" if incomplete else "completed",
        result=result,
        evidence=evidence,
        checks=checks,
        validation_errors=errors,
        searches=[],
        prompt_hash=hashlib.sha256(result.model_dump_json().encode()).hexdigest(),
        model="validated-result-assembler",
        verdict="추가 근거 필요" if incomplete else "통과",
    )

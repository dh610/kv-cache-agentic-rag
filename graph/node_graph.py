from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from rag.evidence import merge_evidence
from rag.interface import EvidenceSource
from runtime.models import ModelBackend
from runtime.prompts import load_rubric, render
from runtime.settings import Settings
from schemas.contracts import (
    Evidence,
    JudgeResult,
    NodeInput,
    NodeName,
    NodeResult,
    NodeRun,
    SearchRecord,
    empty_result,
)


class WorkState(TypedDict, total=False):
    evidence: list[Evidence]
    searches: list[SearchRecord]
    attempts: dict[str, int]
    draft: NodeResult
    judge: JudgeResult
    errors: list[str]
    fatal: bool
    prompt_hash: str
    output: NodeRun


def contract_errors(
    node: NodeName,
    data: NodeInput,
    result: NodeResult,
    evidence: list[Evidence],
    judge: JudgeResult,
) -> list[str]:
    errors = []
    known = {e.id for e in evidence}
    techs = set(data.target_techs.values())
    rubric = {c.id: c for c in load_rubric(node).criteria}
    if result.node != node:
        errors.append(f"node mismatch: expected {node}")
    ids = [c.id for c in result.claims]
    if len(ids) != len(set(ids)):
        errors.append("duplicate claim IDs")
    checked = [c.claim_id for c in judge.checks]
    if len(checked) != len(set(checked)) or set(checked) != set(ids):
        errors.append("Judge must return exactly one check per claim")
    claims = {c.id: c for c in result.claims}
    for check in judge.checks:
        cited = set(claims[check.claim_id].evidence_ids) if check.claim_id in claims else set()
        if not set(check.evidence_ids).issubset(cited & known):
            errors.append(f"{check.claim_id}: Judge cited evidence not supplied by claim")
        if check.label == "supported" and not check.evidence_ids:
            errors.append(f"{check.claim_id}: supported without evidence")
    for claim in result.claims:
        if claim.technology not in techs or claim.criterion not in rubric:
            errors.append(f"{claim.id}: unknown technology/criterion")
        if not claim.evidence_ids or not set(claim.evidence_ids).issubset(known):
            errors.append(f"{claim.id}: missing/unknown evidence IDs")
    for item in result.assessments:
        criterion = rubric.get(item.criterion)
        if item.technology not in techs or not criterion:
            errors.append("assessment has unknown technology/criterion")
        elif item.judgment not in criterion.judgments:
            errors.append(f"{item.criterion}: judgment not allowed by rubric")
        if not set(item.evidence_ids).issubset(known):
            errors.append(f"{item.criterion}: assessment has unknown evidence")
        if item.judgment != "확인 불가" and not item.evidence_ids:
            errors.append(f"{item.criterion}: assessment without evidence")
        if item.judgment != "확인 불가":
            supported_ids = {check.claim_id for check in judge.checks if check.label == "supported"}
            premises = [
                c
                for c in result.claims
                if c.id in supported_ids
                and c.technology == item.technology
                and c.criterion == item.criterion
            ]
            if not premises or not set(item.evidence_ids).issubset(
                {eid for c in premises for eid in c.evidence_ids}
            ):
                errors.append(f"{item.criterion}: assessment lacks verified claim premises")
    addressed = {(a.technology, a.criterion) for a in result.assessments}
    if len(addressed) != len(result.assessments):
        errors.append("Duplicate technology/criterion assessments")
    for q in data.questions:
        if (q.technology, q.criterion) not in addressed:
            errors.append(f"{q.id}: no assessment for requested criterion")
    return errors


def build_node_graph(
    node: NodeName,
    data: NodeInput,
    mode: str,
    settings: Settings,
    backend: ModelBackend,
    source: EvidenceSource,
):
    """One shared bounded graph, injected backend/source and node-owned prompts."""
    if not data.questions or len(data.questions) > settings.limits.questions:
        raise ValueError(f"Provide 1..{settings.limits.questions} questions")
    criteria = {c.id for c in load_rubric(node).criteria}
    if any(q.criterion not in criteria for q in data.questions):
        raise ValueError("Input question criterion is missing from the node rubric")
    if any(q.technology not in data.target_techs.values() for q in data.questions):
        raise ValueError("Question technology is not one of target_techs")

    def search(state: WorkState):
        evidence = list(state.get("evidence", []))
        attempts = dict(state.get("attempts", {}))
        records = list(state.get("searches", []))
        for q in data.questions:
            count = attempts.get(q.id, 0)
            if count >= settings.limits.search:
                continue
            attempts[q.id] = count + 1
            try:
                found = source.search(q, count + 1)
                evidence = merge_evidence(evidence, found)
                records.append(
                    SearchRecord(
                        question_id=q.id,
                        attempt=count + 1,
                        query=q.text,
                        evidence_ids=[e.id for e in found],
                    )
                )
            except Exception as exc:
                records.append(
                    SearchRecord(
                        question_id=q.id,
                        attempt=count + 1,
                        query=q.text,
                        evidence_ids=[],
                        error=f"Search failed: {type(exc).__name__}",
                    )
                )
        return {"evidence": evidence, "attempts": attempts, "searches": records}

    def generate(state: WorkState):
        # Render even in mock mode so broken templates fail the offline check.
        system, user, digest = render(node, data, state["evidence"])
        try:
            draft = backend.generate(node, data, state["evidence"], system, user)
            return {
                "draft": NodeResult.model_validate(draft),
                "prompt_hash": digest,
                "fatal": False,
                "errors": [],
            }
        except Exception as exc:
            reason = f"Generator failed: {type(exc).__name__}; check provider configuration"
            return {
                "draft": empty_result(node, reason),
                "prompt_hash": digest,
                "fatal": True,
                "errors": [reason],
            }

    def verify(state: WorkState):
        if state.get("fatal"):
            return {"judge": JudgeResult(checks=[])}
        try:
            judge = JudgeResult.model_validate(backend.judge(state["draft"], state["evidence"]))
            errors = contract_errors(node, data, state["draft"], state["evidence"], judge)
            return {"judge": judge, "errors": errors}
        except Exception as exc:
            return {
                "judge": JudgeResult(checks=[]),
                "fatal": True,
                "errors": [f"Judge failed: {type(exc).__name__}"],
            }

    def route(state: WorkState):
        unsupported = any(c.label == "unsupported" for c in state["judge"].checks)
        absent = (
            not state["evidence"]
            or bool(state["draft"].unverified)
            or any(a.judgment == "확인 불가" for a in state["draft"].assessments)
        )
        remaining = any(
            state["attempts"].get(q.id, 0) < settings.limits.search for q in data.questions
        )
        if not state.get("fatal") and source.retryable and remaining and (unsupported or absent):
            return "search"
        return "finalize"

    def finalize(state: WorkState):
        result = state["draft"].model_copy(deep=True)
        errors = list(state.get("errors", []))
        rejected = {c.claim_id for c in state["judge"].checks if c.label != "supported"}
        for c in state["judge"].checks:
            if c.label != "supported":
                errors.append(f"{c.claim_id}: {c.label}: {c.reason}")
        for c in result.claims:
            if c.id in rejected:
                result.unverified.append(f"{c.id}: {c.text}")
        result.claims = [c for c in result.claims if c.id not in rejected]
        # A malformed Judge response or contract violation is not accepted as verified output.
        if state.get("errors"):
            result.unverified.extend(c.text for c in result.claims)
            result.claims = []
        if rejected or state.get("errors"):
            result.summary = "검증을 통과하지 못한 내용이 있어 수정이 필요합니다. unverified와 validation_errors를 확인하세요."
            for assessment in result.assessments:
                assessment.judgment = "확인 불가"
                assessment.rationale = "근거 검증 실패로 판정을 보류합니다."
                assessment.evidence_ids = []
        for record in state["searches"]:
            if record.error:
                errors.append(f"{record.question_id} attempt {record.attempt}: {record.error}")
        if not state["evidence"]:
            errors.append("No evidence available")
        for assessment in result.assessments:
            if assessment.judgment == "확인 불가" and mode != "mock":
                result.unverified.append(
                    f"{assessment.technology}/{assessment.criterion}: 확인 불가"
                )
        status = (
            "failed"
            if state.get("fatal")
            else ("needs_revision" if errors or result.unverified else "completed")
        )
        return {
            "output": NodeRun(
                node=node,
                mode=mode,
                status=status,
                result=result,
                evidence=state["evidence"],
                checks=state["judge"].checks,
                validation_errors=errors,
                searches=state["searches"],
                prompt_hash=state["prompt_hash"],
                model=backend.name,
            )
        }

    builder = StateGraph(WorkState)
    for fn in (search, generate, verify, finalize):
        builder.add_node(fn.__name__, fn)
    builder.add_edge(START, "search")
    builder.add_edge("search", "generate")
    builder.add_edge("generate", "verify")
    builder.add_conditional_edges("verify", route, ["search", "finalize"])
    builder.add_edge("finalize", END)
    return builder.compile()

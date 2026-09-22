"""공통 RAG 서브그래프 (설계서 D.2 / 그림 2 / 표 15).

tech·market·stakeholder·domain 네 역할이 그대로 재사용한다. 역할별로 다른 것은 질문과 출처뿐이다.

    plan → search → check_sufficiency ─┬─ 충분 ──────────────→ draft
                                       ├─ 부족·예산 남음 ──→ rewrite_query → search
                                       └─ 부족·한도 도달 ──→ draft (확인된 내용 + 미확인 항목으로 반환)
    draft → verify ─┬─ 통과 ──────────────────────────→ finalize
                    ├─ 표현 오류 · fix_count < limits.fix → fix → verify
                    └─ 추가 근거 필요 · 예산 남음 ──────→ rewrite_query → search

교재 대응: Judge 기반 Recursive Loop(질의 변환 + 재검색) + LLM-as-a-Judge 근거 검증.
한도(표 13)는 세부 질문마다 따로 적용한다. 재검색은 부족하다고 판정된 질문만 다시 한다.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from rag.evidence import merge_evidence
from rag.interface import EvidenceSource
from runtime.models import ModelBackend
from runtime.prompts import load_rubric, render
from runtime.settings import Settings
from schemas.contracts import (
    ClaimCheck,
    Evidence,
    JudgeResult,
    NodeInput,
    NodeName,
    NodeResult,
    NodeRun,
    Question,
    SearchRecord,
    Verdict,
    empty_result,
)


class WorkState(TypedDict, total=False):
    """설계서 표 15 RAG Sub State. 앞 10키가 표의 키이고, 아래는 NodeRun을 만들기 위한 내부 기록이다."""

    role: NodeName
    questions: list[Question]
    current_query: dict[str, str]  # question_id → 현재 검색어. rewrite_query가 갱신한다.
    search_results: list[Evidence]
    is_sufficient: bool
    draft: NodeResult
    verdict: Verdict
    search_count: dict[str, int]  # question_id → 검색 횟수. 표 13: 세부 질문당 최대 3.
    fix_count: int
    output: NodeRun
    # --- 내부 기록 (표 15 밖) ---
    pending: list[str]  # 다음 검색 라운드 대상 질문 id (부족하다고 판정된 것만)
    insufficient: list[str]  # 마지막 충분성 판정에서 근거가 부족했던 질문 id
    searches: list[SearchRecord]
    checks: list[ClaimCheck]
    errors: list[
        str
    ]  # 검색 외 실행 오류 (판정기·재작성·생성기 실패). 재시도 성공과 무관하게 남긴다.
    verify_errors: list[str]  # 계약 검사 결과. 검증할 때마다 다시 계산한다.
    fatal: bool
    prompt_hash: str


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


def unmet_pairs(result: NodeResult, judge: JudgeResult) -> set[tuple[str, str]]:
    """질문 충족 실패로 볼 (technology, criterion).

    - 근거 없는(unsupported) 주장이 달린 항목
    - 확인 불가인데 그 항목을 뒷받침하는 supported 주장이 하나도 없는 항목
    근거가 있는데도 확인 불가로 판정된 항목은 재검색해도 달라지지 않으므로 미충족으로 보지 않는다.
    """
    labels = {c.claim_id: c.label for c in judge.checks}
    supported = {
        (c.technology, c.criterion) for c in result.claims if labels.get(c.id) == "supported"
    }
    pairs = {
        (c.technology, c.criterion) for c in result.claims if labels.get(c.id) == "unsupported"
    }
    pairs |= {
        (a.technology, a.criterion)
        for a in result.assessments
        if a.judgment == "확인 불가" and (a.technology, a.criterion) not in supported
    }
    return pairs


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

    limit = settings.limits.search
    known_ids = {q.id for q in data.questions}

    def retry_targets(state: WorkState) -> list[Question]:
        """이번 라운드에 검색할 질문: 부족 판정을 받았고 예산이 남은 것."""
        pending = set(state.get("pending", []))
        counts = state.get("search_count", {})
        return [q for q in data.questions if q.id in pending and counts.get(q.id, 0) < limit]

    # ── 노드 ────────────────────────────────────────────────────────────────

    def plan(state: WorkState):
        """검색 계획: 질문별 초기 검색어와 검색 대상. 출처(논문/웹)는 호출 노드가 source로 주입한다."""
        return {
            "role": node,
            "questions": list(data.questions),
            "current_query": {q.id: q.text for q in data.questions},
            "search_results": [],
            "search_count": {q.id: 0 for q in data.questions},
            "fix_count": 0,
            "pending": [q.id for q in data.questions],
            "insufficient": [],
            "searches": [],
            "checks": [],
            "errors": [],
            "verify_errors": [],
            "fatal": False,
        }

    def search(state: WorkState):
        evidence = list(state["search_results"])
        counts = dict(state["search_count"])
        records = list(state["searches"])
        for q in retry_targets(state):
            attempt = counts[q.id] + 1
            counts[q.id] = attempt
            query = state["current_query"][q.id]
            try:
                found = source.search(q.model_copy(update={"text": query}), attempt)
                evidence = merge_evidence(evidence, found)
                records.append(
                    SearchRecord(
                        question_id=q.id,
                        attempt=attempt,
                        query=query,
                        evidence_ids=[e.id for e in found],
                    )
                )
            except Exception as exc:
                records.append(
                    SearchRecord(
                        question_id=q.id,
                        attempt=attempt,
                        query=query,
                        evidence_ids=[],
                        error=f"Search failed: {type(exc).__name__}",
                    )
                )
        return {"search_results": evidence, "search_count": counts, "searches": records}

    def check_sufficiency(state: WorkState):
        """근거 관련성·충분성 확인 (Judge). 부족한 질문만 다음 검색 대상으로 남긴다."""
        try:
            verdict = backend.sufficiency(data.questions, state["search_results"])
            missing = [i for i in verdict.missing_question_ids if i in known_ids]
            sufficient = bool(verdict.sufficient) and not missing
            return {"is_sufficient": sufficient, "pending": missing, "insufficient": missing}
        except Exception as exc:
            # 판정기 실패는 근거 부족으로 다루고 사유를 남긴다. 예산이 남아 있으면 재검색으로 이어진다.
            missing = [q.id for q in data.questions]
            return {
                "is_sufficient": False,
                "pending": missing,
                "insufficient": missing,
                "errors": state["errors"] + [f"Sufficiency judge failed: {type(exc).__name__}"],
            }

    def rewrite_query(state: WorkState):
        """질문 수정: 부족한 질문의 검색어를 이중언어(한국어 + 영어 핵심어)로 다시 쓴다 (D.2)."""
        queries = dict(state["current_query"])
        errors = list(state["errors"])
        for q in retry_targets(state):
            attempt = state["search_count"][q.id] + 1
            try:
                queries[q.id] = backend.rewrite(q, queries[q.id], attempt)
            except Exception as exc:
                errors.append(f"{q.id}: Rewrite failed: {type(exc).__name__}")
        return {"current_query": queries, "errors": errors}

    def draft(state: WorkState):
        """근거 기반 결과 작성 (Generator)."""
        # Render even in mock mode so broken templates fail the offline check.
        system, user, digest = render(node, data, state["search_results"])
        try:
            result = backend.generate(node, data, state["search_results"], system, user)
            return {
                "draft": NodeResult.model_validate(result),
                "prompt_hash": digest,
                "fatal": False,
            }
        except Exception as exc:
            reason = f"Generator failed: {type(exc).__name__}; check provider configuration"
            return {
                "draft": empty_result(node, reason),
                "prompt_hash": digest,
                "fatal": True,
                "errors": state["errors"] + [reason],
            }

    def verify(state: WorkState):
        """주장·인용·질문 충족 검증 (LLM-as-a-Judge, 주장 단위) → 결과 단위 verdict."""
        if state.get("fatal"):
            return {"checks": [], "verify_errors": []}
        try:
            judge = JudgeResult.model_validate(
                backend.judge(state["draft"], state["search_results"])
            )
            errors = contract_errors(node, data, state["draft"], state["search_results"], judge)
        except Exception as exc:
            return {
                "checks": [],
                "verify_errors": [],
                "fatal": True,
                "errors": state["errors"] + [f"Judge failed: {type(exc).__name__}"],
            }
        labels = {c.label for c in judge.checks}
        unmet = unmet_pairs(state["draft"], judge)
        pending = [q.id for q in data.questions if (q.technology, q.criterion) in unmet]
        counts = state["search_count"]
        can_retry = source.retryable and any(counts.get(i, 0) < limit for i in pending)
        # 표 15 verdict: 근거 없는 주장 → 추가 근거 필요, 수치·표현만 틀림 → 표현 오류, 그 외 통과.
        # 생성기가 스스로 확인 불가로 남긴 질문도 재검색 여지가 있으면 추가 근거 필요로 본다.
        if "unsupported" in labels or (pending and can_retry):
            verdict: Verdict = "추가 근거 필요"
        elif "misstated" in labels:
            verdict = "표현 오류"
        else:
            verdict = "통과"
        return {
            "checks": judge.checks,
            "verify_errors": errors,
            "verdict": verdict,
            "pending": pending,
        }

    def fix(state: WorkState):
        """답변 수정: misstated 주장의 표현만 고친다 (표 13: 최대 1회)."""
        try:
            fixed = backend.fix(state["draft"], state["checks"], state["search_results"])
            return {"draft": NodeResult.model_validate(fixed), "fix_count": state["fix_count"] + 1}
        except Exception as exc:
            return {
                "fix_count": state["fix_count"] + 1,
                "errors": state["errors"] + [f"Fix failed: {type(exc).__name__}"],
            }

    def finalize(state: WorkState):
        """결과 반환: 결과 + 원문 근거 + 검증 기록 + 미확인 사항. 상태는 코드가 정한다."""
        result = state["draft"].model_copy(deep=True)
        errors = list(state.get("errors", [])) + list(state.get("verify_errors", []))
        checks = list(state.get("checks", []))
        rejected = {c.claim_id for c in checks if c.label != "supported"}
        for c in checks:
            if c.label != "supported":
                errors.append(f"{c.claim_id}: {c.label}: {c.reason}")
        for c in result.claims:
            if c.id in rejected:
                result.unverified.append(f"{c.id}: {c.text}")
        result.claims = [c for c in result.claims if c.id not in rejected]
        # A malformed Judge response or contract violation is not accepted as verified output.
        if errors:
            result.unverified.extend(c.text for c in result.claims)
            result.claims = []
        if rejected or errors:
            result.summary = "검증을 통과하지 못한 내용이 있어 수정이 필요합니다. unverified와 validation_errors를 확인하세요."
            for assessment in result.assessments:
                assessment.judgment = "확인 불가"
                assessment.rationale = "근거 검증 실패로 판정을 보류합니다."
                assessment.evidence_ids = []
        for record in state["searches"]:
            if record.error:
                errors.append(f"{record.question_id} attempt {record.attempt}: {record.error}")
        if not state["search_results"]:
            errors.append("No evidence available")
        # 한도 도달 또는 재검색 불가로 끝난 부족 항목은 미확인으로 남긴다 (그림 2 '확인된 내용 + 미확인 항목').
        if not state.get("is_sufficient", True):
            for qid in state.get("insufficient", []):
                result.unverified.append(
                    f"{qid}: 근거 부족 (검색 {state['search_count'].get(qid, 0)}회)"
                )
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
                evidence=state["search_results"],
                checks=checks,
                validation_errors=errors,
                searches=state["searches"],
                prompt_hash=state["prompt_hash"],
                model=backend.name,
                verdict=state.get("verdict"),
                is_sufficient=state.get("is_sufficient"),
                search_count=dict(state["search_count"]),
                fix_count=state["fix_count"],
            )
        }

    # ── 분기 ────────────────────────────────────────────────────────────────

    def route_after_sufficiency(state: WorkState) -> str:
        if state["is_sufficient"]:
            return "draft"
        if source.retryable and retry_targets(state):
            return "rewrite_query"
        # 한도 도달 또는 고정 근거: 확인된 내용으로 작성하고 부족분은 finalize가 미확인으로 남긴다.
        return "draft"

    def route_after_verify(state: WorkState) -> str:
        if state.get("fatal"):
            return "finalize"
        verdict = state.get("verdict", "통과")
        if verdict == "추가 근거 필요" and source.retryable and retry_targets(state):
            return "rewrite_query"
        if verdict == "표현 오류" and state["fix_count"] < settings.limits.fix:
            return "fix"
        return "finalize"

    # ── 조립 (그림 2) ────────────────────────────────────────────────────────

    builder = StateGraph(WorkState)
    for fn in (plan, search, check_sufficiency, rewrite_query, draft, verify, fix, finalize):
        builder.add_node(fn.__name__, fn)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "search")
    builder.add_edge("search", "check_sufficiency")
    builder.add_conditional_edges(
        "check_sufficiency", route_after_sufficiency, ["draft", "rewrite_query"]
    )
    builder.add_edge("rewrite_query", "search")
    builder.add_edge("draft", "verify")
    builder.add_conditional_edges(
        "verify", route_after_verify, ["finalize", "fix", "rewrite_query"]
    )
    builder.add_edge("fix", "verify")
    builder.add_edge("finalize", END)
    return builder.compile()

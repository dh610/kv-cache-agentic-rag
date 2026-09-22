from __future__ import annotations

from typing import TypedDict

from langchain_core.runnables.config import ContextThreadPoolExecutor
from langgraph.graph import END, START, StateGraph

from rag.evidence import merge_evidence
from runtime.node_rules import DRAFT_RULES
from runtime.prompts import load_rubric, render
from runtime.synthesis_check import synthesis_errors
from runtime.validation import tech_trl_errors, verified_evidence_ids
from schemas.contracts import (
    Coverage,
    Evidence,
    JudgeResult,
    NodeInput,
    NodeName,
    NodeResult,
    NodeRun,
    QueryPlan,
    SearchRecord,
    SufficiencyResult,
    empty_result,
)


class RAGSubState(TypedDict, total=False):
    role: str
    questions: list
    current_query: str
    search_results: list[Evidence]
    is_sufficient: bool
    draft: NodeResult
    verdict: str
    search_count: dict[str, int]  # per-question, never a shared batch counter
    fix_count: int
    output: NodeRun
    # Execution metadata extends the design's ten public keys.
    queries: dict
    coverage: list
    searches: list[SearchRecord]
    judge: JudgeResult
    errors: list[str]
    fatal: bool
    prompt_hash: str
    rendered_system: str
    rendered_user: str
    searched_question_ids: list[str]
    retry_from_verification: bool
    coverage_refreshed: bool


WorkState = RAGSubState


def normalize_coverage(review, questions, evidence) -> list:
    """충분성 판정 결과를 질문 집합에 맞게 정리한다.

    - 질문마다 정확히 하나의 Coverage 를 남긴다: 빠진/중복 질문은 부족, 미지 question_id 는 무시.
    - 현재 근거에 없거나 다른 기술인 evidence_id 는 버리고 부족 사유를 남긴다.
    - '충분'인데 인용이 없거나 다른 기술의 근거만 인용했으면 부족으로 강등하고 사유를 남긴다.
    판정기(nano)는 근거가 수십 건일 때 id 를 빠뜨리거나 다른 기술 청크를 인용하기 쉽다. 그 실수로
    노드 전체를 failed 로 만들면 실제 실행에서 모든 역할이 죽는다 (live 점검에서 실제로 발생).
    """
    by_id = {e.id: e for e in evidence}
    tech_of = {q.id: q.technology for q in questions}
    seen: dict[str, object] = {}
    duplicates = set()
    for c in review.items:
        if c.question_id in tech_of:
            if c.question_id in seen:
                duplicates.add(c.question_id)
            else:
                seen[c.question_id] = c
    out = []
    for q in questions:
        c = seen.get(q.id)
        if c is None:
            out.append(
                Coverage(
                    question_id=q.id,
                    sufficient=False,
                    evidence_ids=[],
                    reason="판정기가 이 질문을 빠뜨려 부족으로 처리",
                )
            )
            continue
        ids = list(
            dict.fromkeys(
                eid
                for eid in c.evidence_ids
                if eid in by_id and by_id[eid].technology in (q.technology, "other")
            )
        )
        sufficient, reason = c.sufficient, c.reason
        if q.id in duplicates:
            sufficient, reason = False, f"{reason} (중복 질문 판정 → 부족)"
        if set(c.evidence_ids) - set(ids):
            sufficient, reason = False, f"{reason} (모르는/다른 기술 근거 제거 → 부족)"
        elif sufficient and not ids:
            sufficient, reason = False, f"{reason} (인용 근거 없음 → 부족)"
        out.append(
            Coverage(question_id=q.id, sufficient=sufficient, evidence_ids=ids, reason=reason)
        )
    return out


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
            verified = verified_evidence_ids(
                result, judge.checks, evidence, item.technology, item.criterion
            )
            if not verified or not set(item.evidence_ids).issubset(verified):
                errors.append(f"{item.criterion}: assessment lacks verified claim premises")
    addressed = {(a.technology, a.criterion) for a in result.assessments}
    if len(addressed) != len(result.assessments):
        errors.append("Duplicate technology/criterion assessments")
    for q in data.questions:
        if (q.technology, q.criterion) not in addressed:
            errors.append(f"{q.id}: no assessment for requested criterion")
    for estimate in result.trl_estimates:
        if estimate.technology not in techs or not set(estimate.evidence_ids).issubset(known):
            errors.append("TRL has unknown technology/evidence")
        if estimate.level is not None:
            supported = verified_evidence_ids(result, judge.checks, evidence, estimate.technology)
            if not estimate.evidence_ids or not set(estimate.evidence_ids).issubset(supported):
                errors.append("TRL lacks verified claim premises")
    if node == "tech":
        errors.extend(tech_trl_errors(result))
    elif len({t.technology for t in result.trl_estimates}) != len(result.trl_estimates):
        errors.append("Duplicate TRL technologies")
    if node == "synthesis":
        # Role-specific rules; other nodes keep the shared contract unchanged.
        errors.extend(synthesis_errors(data, result, evidence))
    return errors


def build_node_graph(
    node,
    data,
    mode,
    settings,
    backend,
    source,
    *,
    previous: NodeRun | None = None,
    refresh_technologies: set[str] | None = None,
):
    """Eight bounded stages shared by every role; every question retains its own budget."""
    if not data.questions or len(data.questions) > settings.limits.questions:
        raise ValueError(f"Provide 1..{settings.limits.questions} questions")
    criteria = {c.id for c in load_rubric(node).criteria}
    if any(q.criterion not in criteria for q in data.questions):
        raise ValueError("Input question criterion is missing from the node rubric")
    if any(q.technology not in data.target_techs.values() for q in data.questions):
        raise ValueError("Question technology is not one of target_techs")
    live_search = source.retryable and mode not in ("mock", "fixture")
    # Reuse retrieval, not the previous Generator/Judge verdict. A supplement still
    # evaluates the complete question set against current upstream results.
    previous = previous if live_search else None
    if previous is not None and (previous.node != node or previous.mode != mode):
        raise ValueError("Supplement retrieval must belong to the same node and mode")
    question_ids = {q.id for q in data.questions}
    old_searches = (
        [r for r in previous.searches if r.question_id in question_ids] if previous else []
    )
    offsets = {
        q.id: max((r.attempt for r in old_searches if r.question_id == q.id), default=0)
        for q in data.questions
    }

    def intents(state, question_id):
        return {r.intent for r in state["searches"] if r.question_id == question_id and not r.error}

    def pending(state, *, verification=False):
        """Select only unresolved questions with budget; never infer success from silence."""
        coverage = {c.question_id: c for c in state.get("coverage", [])}
        unresolved_pairs = set()
        unresolved_ids = set()
        if verification and "draft" in state:
            draft = state["draft"]
            assessments = {(a.technology, a.criterion): a for a in draft.assessments}
            unresolved_pairs.update(
                (q.technology, q.criterion)
                for q in data.questions
                if (q.technology, q.criterion) not in assessments
                or assessments[q.technology, q.criterion].judgment == "확인 불가"
            )
            rejected = {
                c.claim_id
                for c in state.get("judge", JudgeResult(checks=[])).checks
                if c.label == "unsupported"
            }
            unresolved_pairs.update(
                (c.technology, c.criterion) for c in draft.claims if c.id in rejected
            )
            for q in data.questions:
                if any(
                    q.id in note or f"{q.technology}/{q.criterion}" in note
                    for note in draft.unverified
                ):
                    unresolved_ids.add(q.id)
            # Unscoped free-text warnings remain unverified; they are not an excuse
            # to repeat every already-sufficient query or to declare success.
        return [
            q
            for q in data.questions
            if state["search_count"].get(q.id, offsets[q.id]) - offsets[q.id]
            < settings.limits.search
            and (
                q.id not in coverage
                or not coverage[q.id].sufficient
                or not {"positive", "critical"}.issubset(intents(state, q.id))
                or (q.technology, q.criterion) in unresolved_pairs
                or q.id in unresolved_ids
            )
        ]

    def planned(questions, feedback):
        if not questions:
            return {}
        scoped = data.model_copy(update={"questions": questions})
        pairs = QueryPlan.model_validate(backend.plan(scoped, feedback)).queries
        if (
            len(pairs) != len(questions)
            or {p.question_id for p in pairs} != {q.id for q in questions}
            or any(not p.positive.strip() or not p.critical.strip() for p in pairs)
        ):
            raise ValueError("Query plan must cover requested questions exactly once")
        return {p.question_id: p for p in pairs}

    def plan(state):
        initial = {
            "role": node,
            "questions": data.questions,
            "search_count": dict(offsets),
            "fix_count": 0,
            "search_results": (
                merge_evidence(previous.evidence, data.evidence if mode == "live" else [])
                if previous
                else []
            ),
            "searches": list(old_searches),
            "errors": [],
            "coverage": [],
            "fatal": False,
            "current_query": "",
            "queries": {},
            "searched_question_ids": [],
            "retry_from_verification": False,
        }
        if previous:
            initial["coverage"] = normalize_coverage(
                SufficiencyResult(items=previous.coverage),
                data.questions,
                initial["search_results"],
            )
        try:
            initial["queries"] = planned(pending(initial) if live_search else data.questions, [])
        except Exception as exc:
            initial.update(fatal=True, errors=[f"Planning failed: {type(exc).__name__}"])
        return initial

    def search(state):
        if state.get("fatal"):
            return {}
        evidence = list(state["search_results"])
        counts, records = dict(state["search_count"]), list(state["searches"])
        jobs = []
        for q in data.questions:
            count = counts.get(q.id, offsets[q.id])
            budget = settings.limits.search - (count - offsets[q.id])
            if q.id not in state["queries"] or budget <= 0:
                continue
            missing = [i for i in ("positive", "critical") if i not in intents(state, q.id)]
            wanted = (missing or ["followup"]) if live_search else ["fixture"]
            pair = state["queries"][q.id]
            for intent in wanted[:budget]:
                count += 1
                query = pair.critical if intent == "critical" else pair.positive
                searched = q.model_copy(update={"text": query}) if live_search else q
                jobs.append((searched, count, intent))
            counts[q.id] = count

        def fetch(job):
            q, count, intent = job
            try:
                found = source.search(q, count)
                return found, SearchRecord(
                    question_id=q.id,
                    attempt=count,
                    query=q.text,
                    intent=intent,
                    evidence_ids=[e.id for e in found],
                )
            except Exception as exc:
                return [], SearchRecord(
                    question_id=q.id,
                    attempt=count,
                    query=q.text,
                    intent=intent,
                    evidence_ids=[],
                    error=f"Search failed: {type(exc).__name__}",
                )

        # Bounded provider concurrency; merge in request order to keep identity checks stable.
        with ContextThreadPoolExecutor(max_workers=4) as pool:
            for found, record in pool.map(fetch, jobs):
                try:
                    evidence = merge_evidence(evidence, found)
                except ValueError as exc:
                    record.error = f"Search failed: {type(exc).__name__}"
                    record.evidence_ids = []
                records.append(record)
        return {
            "search_results": evidence,
            "search_count": counts,
            "searches": records,
            "current_query": jobs[-1][0].text if jobs else "",
            "searched_question_ids": list(dict.fromkeys(q.id for q, _, _ in jobs)),
        }

    def relevant_evidence(state, questions, *, generation=False):
        selected = {q.id for q in questions}
        ids = {
            eid
            for record in state["searches"]
            if record.question_id in selected
            for eid in record.evidence_ids
        }
        ids.update(
            eid
            for c in state.get("coverage", [])
            if c.question_id in selected
            for eid in c.evidence_ids
        )
        if generation:
            # Once judged, prefer the cited sufficient premises plus both search intents.
            covered = {c.question_id for c in state.get("coverage", []) if c.sufficient}
            ids = {eid for c in state.get("coverage", []) for eid in c.evidence_ids}
            lookup = {e.id: e for e in state["search_results"]}
            for record in state["searches"]:
                if record.question_id not in covered:
                    ids.update(record.evidence_ids)
                    continue
                by_kind = {}
                for eid in record.evidence_ids:
                    if eid not in lookup:
                        continue
                    kind = lookup[eid].source_type
                    by_kind[kind] = by_kind.get(kind, 0) + 1
                    if by_kind[kind] <= 2 or lookup[eid].stance in ("critical", "mixed"):
                        ids.add(eid)
        ids.update(e.id for e in data.evidence)
        techs = {q.technology for q in questions}
        return [
            e
            for e in state["search_results"]
            if (e.id in ids or not live_search)
            and (e.technology in techs or e.technology == "other" or e.document_role == "reference")
        ]

    def check_sufficiency(state):
        if state.get("fatal"):
            return {"is_sufficient": False}
        selected = set(state["searched_question_ids"])
        if previous and not state.get("coverage_refreshed"):
            selected.update(
                q.id for q in data.questions if q.technology in (refresh_technologies or set())
            )
        questions = [q for q in data.questions if q.id in selected]
        base = {"retry_from_verification": False, "coverage_refreshed": True}
        if not questions:
            return {**base, "is_sufficient": all(c.sufficient for c in state.get("coverage", []))}
        if not state["search_results"]:
            # Nothing to judge: record insufficiency without a model call so an
            # empty search stays "insufficient evidence" instead of an execution failure.
            return {
                **base,
                "coverage": [
                    Coverage(
                        question_id=q.id, sufficient=False, evidence_ids=[], reason="근거 없음"
                    )
                    for q in questions
                ],
                "is_sufficient": False,
            }
        try:
            review = SufficiencyResult.model_validate(
                backend.sufficiency(
                    data.model_copy(update={"questions": questions}),
                    relevant_evidence(state, questions),
                )
            )
            # 질문에 대응하는 항목이 하나도 없으면 쓸 수 없는 출력이다 → 기존대로 fail-closed.
            if not any(c.question_id in selected for c in review.items):
                raise ValueError("Sufficiency returned no usable item")
            # 부분적 형식 실수(빠진 질문, 모르는 id, 다른 기술 근거 인용)는 실행 실패가 아니다.
            # 설계서 D.3: 근거 부족은 실행 실패와 구분한다. 정리한 뒤 부족으로 처리하고 계속 진행한다.
            updated = {c.question_id: c for c in state.get("coverage", [])}
            updated.update(
                {
                    c.question_id: c
                    for c in normalize_coverage(review, questions, state["search_results"])
                }
            )
            coverage = [updated[q.id] for q in data.questions]
            return {
                **base,
                "coverage": coverage,
                "is_sufficient": all(c.sufficient for c in coverage),
            }
        except Exception as exc:
            return {
                "is_sufficient": False,
                "fatal": True,
                "errors": [f"Sufficiency failed: {type(exc).__name__}"],
            }

    def remaining(state, *, verification=False):
        return live_search and bool(pending(state, verification=verification))

    def enough_route(state):
        if state.get("fatal"):
            return "write_draft"
        if remaining(state):
            return "rewrite_query"
        return "write_draft"

    def rewrite_query(state):
        try:
            questions = pending(state, verification=state.get("retry_from_verification", False))
            selected = {q.id for q in questions}
            feedback = [
                c.model_dump() for c in state.get("coverage", []) if c.question_id in selected
            ]
            feedback += [
                c.model_dump()
                for c in state.get("judge", JudgeResult(checks=[])).checks
                if c.label != "supported"
            ]
            return {"queries": planned(questions, feedback)}
        except Exception as exc:
            return {"fatal": True, "errors": [f"Rewrite failed: {type(exc).__name__}"]}

    def write_draft(state):
        current = data.model_copy(deep=True)
        current.description += "\n근거 충분성 검사: " + str(
            [c.model_dump() for c in state.get("coverage", [])]
        )
        prompt_evidence = relevant_evidence(state, data.questions, generation=True)
        system, user, digest = render(node, current, prompt_evidence)
        if state.get("fatal"):
            return {
                "draft": empty_result(node, "; ".join(state["errors"])),
                "prompt_hash": digest,
                "rendered_system": system,
                "rendered_user": user,
            }
        try:
            draft = backend.generate(node, current, prompt_evidence, system, user)
            return {
                "draft": NodeResult.model_validate(draft),
                "prompt_hash": digest,
                "rendered_system": system,
                "rendered_user": user,
                "errors": [],
            }
        except Exception as exc:
            reason = f"Generator failed: {type(exc).__name__}; check provider configuration"
            return {
                "draft": empty_result(node, reason),
                "prompt_hash": digest,
                "rendered_system": system,
                "rendered_user": user,
                "fatal": True,
                "errors": [reason],
            }

    def verify(state):
        if state.get("fatal"):
            return {"judge": JudgeResult(checks=[]), "verdict": "추가 근거 필요"}
        # Role rules that need no Judge run first; a violation is an expression error the
        # bounded fix retry can repair before any Judge call is spent.
        rule_errors = DRAFT_RULES.get(node, lambda d, r, e: [])(
            data, state["draft"], state["search_results"]
        )
        if rule_errors and state["fix_count"] < settings.limits.fix:
            return {"judge": JudgeResult(checks=[]), "errors": rule_errors, "verdict": "표현 오류"}
        try:
            # No claims means nothing to verify; never let a model invent checks for them.
            judge = (
                JudgeResult.model_validate(backend.judge(state["draft"], state["search_results"]))
                if state["draft"].claims
                else JudgeResult(checks=[])
            )
            errors = rule_errors + contract_errors(
                node, data, state["draft"], state["search_results"], judge
            )
            labels = {c.label for c in judge.checks}
            absent = bool(state["draft"].unverified) or (
                mode != "mock"
                and any(a.judgment == "확인 불가" for a in state["draft"].assessments)
            )
            expression_error = "misstated" in labels or bool(errors)
            # While the single fix retry is available, repair expression errors before
            # concluding that more evidence is needed; afterwards the stricter verdict wins.
            verdict = (
                "표현 오류"
                if expression_error and state["fix_count"] < settings.limits.fix
                else "추가 근거 필요"
                if "unsupported" in labels or absent
                else "표현 오류"
                if expression_error
                else "통과"
            )
            return {
                "judge": judge,
                "errors": errors,
                "verdict": verdict,
                "retry_from_verification": True,
            }
        except Exception as exc:
            return {
                "judge": JudgeResult(checks=[]),
                "fatal": True,
                "verdict": "추가 근거 필요",
                "errors": [f"Judge failed: {type(exc).__name__}"],
            }

    def verify_route(state):
        if not state.get("fatal"):
            if state["verdict"] == "표현 오류" and state["fix_count"] < settings.limits.fix:
                return "fix"
            if state["verdict"] == "추가 근거 필요" and remaining(state, verification=True):
                return "rewrite_query"
        return "return_result"

    def fix(state):
        current = data.model_copy(deep=True)
        current.description += (
            "\n기존 근거만 사용해 표현/형식 오류를 수정하라. 새 사실을 만들지 마라.\n이전 결과: "
            + state["draft"].model_dump_json()
        )
        current.description += (
            "\n검증 피드백: " + state["judge"].model_dump_json() + str(state["errors"])
        )
        system, user, digest = render(node, current, state["search_results"])
        try:
            draft = NodeResult.model_validate(
                backend.generate(node, current, state["search_results"], system, user)
            )
            return {
                "draft": draft,
                "fix_count": state["fix_count"] + 1,
                "prompt_hash": digest,
                "rendered_system": system,
                "rendered_user": user,
                "errors": [],
            }
        except Exception as exc:
            return {
                "fatal": True,
                "fix_count": state["fix_count"] + 1,
                "errors": [f"Fix failed: {type(exc).__name__}"],
                "prompt_hash": digest,
                "rendered_system": system,
                "rendered_user": user,
            }

    def return_result(state):
        result = state["draft"].model_copy(deep=True)
        errors = list(state.get("errors", []))
        rejected = {c.claim_id for c in state["judge"].checks if c.label != "supported"}
        for c in state["judge"].checks:
            if c.label != "supported":
                errors.append(f"{c.claim_id}: {c.label}: {c.reason}")
        result.unverified.extend(c.text for c in result.claims if c.id in rejected)
        result.claims = [c for c in result.claims if c.id not in rejected]
        if state.get("errors") or state.get("fatal"):
            result.unverified.extend(c.text for c in result.claims)
            result.claims = []
        if rejected or state.get("errors") or state.get("fatal"):
            result.summary = "검증을 통과하지 못한 내용이 있어 수정이 필요합니다."
            for a in result.assessments:
                a.judgment, a.rationale, a.evidence_ids = (
                    "확인 불가",
                    "근거 검증 실패로 판정을 보류합니다.",
                    [],
                )
            for t in result.trl_estimates:
                t.level, t.evidence_ids, t.rationale = None, [], "근거 검증 실패"
        errors.extend(
            f"{r.question_id} attempt {r.attempt}: {r.error}" for r in state["searches"] if r.error
        )
        if not state["search_results"]:
            errors.append("No evidence available")
        if mode != "mock":
            for a in result.assessments:
                if a.judgment == "확인 불가":
                    result.unverified.append(f"{a.technology}/{a.criterion}: 확인 불가")
            for q in data.questions:
                intents = {
                    r.intent for r in state["searches"] if r.question_id == q.id and not r.error
                }
                if not {"positive", "critical"}.issubset(intents) and node in (
                    "tech",
                    "market",
                    "stakeholder",
                    "domain",
                ):
                    result.limitations.append(f"{q.id}: 긍정·비판 양쪽 실제 검색 미완료")
            stances = {e.stance for e in state["search_results"]}
            if not ({"positive", "critical"}.issubset(stances) or "mixed" in stances):
                result.limitations.append(
                    "긍정·비판 양쪽 자료의 원문 분류가 확인되지 않음. 검색 수행과 자료의 실제 입장은 다릅니다."
                )
        status = (
            "failed"
            if state.get("fatal")
            else "needs_revision"
            if errors or result.unverified
            else "completed"
        )
        return {
            "output": NodeRun(
                node=node,
                mode=mode,
                status=status,
                result=result,
                evidence=state["search_results"],
                checks=state["judge"].checks,
                validation_errors=errors,
                searches=state["searches"],
                prompt_hash=state["prompt_hash"],
                model=backend.name,
                verdict=state["verdict"],
                fix_count=state["fix_count"],
                coverage=state.get("coverage", []),
            )
        }

    builder = StateGraph(RAGSubState)
    for fn in (
        plan,
        search,
        check_sufficiency,
        rewrite_query,
        write_draft,
        verify,
        fix,
        return_result,
    ):
        builder.add_node(fn.__name__, fn)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "search")
    builder.add_edge("search", "check_sufficiency")
    builder.add_conditional_edges(
        "check_sufficiency", enough_route, ["rewrite_query", "write_draft"]
    )
    builder.add_edge("rewrite_query", "search")
    builder.add_edge("write_draft", "verify")
    builder.add_conditional_edges("verify", verify_route, ["fix", "rewrite_query", "return_result"])
    builder.add_edge("fix", "verify")
    builder.add_edge("return_result", END)
    return builder.compile()

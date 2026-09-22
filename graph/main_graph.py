"""Main graph (설계서 D.1 / 그림 1 / 표 12 / 표 14).

    START → init → tech → [market, stakeholder, domain] → collect → synthesis
          → (gaps 있음 · supplement_round < limits.supplement ? supplement → synthesis : report)
          → report → citation_check → END

init / collect / supplement / citation_check 는 LLM 없는 코드 노드다.
supplement 는 결과 수집을 거치지 않고 synthesis 로 직접 돌아오며, 기술 조사 결과가 바뀌면 의존 평가도 다시 돈다.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from graph.node_graph import build_node_graph
from rag.evidence import merge_evidence
from rag.interface import CombinedSource, EvidenceSource, FixedEvidence
from runtime.handoff import reference_issues
from runtime.models import ModelBackend
from runtime.settings import Settings
from schemas.contracts import RESEARCH_NODES, Evidence, NodeInput, NodeRun

# Preserve the result key names from the shared team starter (= 설계서 표 14).
RESULT_KEYS = {
    "tech": "tech_result",
    "market": "market_result",
    "stakeholder": "stakeholder_result",
    "domain": "domain_result",
    "synthesis": "synthesis",
    "report": "report",
}
EVAL_NODES = ("market", "stakeholder", "domain")
# 각 노드가 입력으로 받는 상위 결과. 병렬 가지는 서로를 보지 않고 기술 조사만 본다.
UPSTREAM: dict[str, tuple[str, ...]] = {
    "tech": (),
    "market": ("tech",),
    "stakeholder": ("tech",),
    "domain": ("tech",),
    "synthesis": RESEARCH_NODES,
    "report": RESEARCH_NODES + ("synthesis",),
}


class MainState(TypedDict, total=False):
    """설계서 표 14 Main State. 키 이름을 바꾸지 않는다."""

    target_techs: dict[str, str]
    domain: str
    limits: dict[str, int]
    tech_result: NodeRun
    market_result: NodeRun
    stakeholder_result: NodeRun
    domain_result: NodeRun
    # 병렬 노드가 함께 쓰는 유일한 키. 누적 리듀서라 덮어쓰기가 일어나지 않는다 (설계서 D.4.1).
    sources: Annotated[list[Evidence], operator.add]
    trl_result: dict[str, Any]
    synthesis: NodeRun
    gaps: list[dict[str, str]]
    supplement_round: int
    report_path: str
    # --- 표 14 밖, 기존 구현 계승 ---
    report: NodeRun  # 보고서 생성 노드의 결과 본문. report_path 는 산출물 경로와 검사 결과.
    run_status: str


def cited_evidence(run: NodeRun) -> list[Evidence]:
    """REFERENCE 에는 검색한 모든 자료가 아니라 실제 인용한 근거만 넘긴다 (과제 가이드)."""
    used = {eid for c in run.result.claims for eid in c.evidence_ids}
    used |= {eid for a in run.result.assessments for eid in a.evidence_ids}
    return [e for e in run.evidence if e.id in used]


def derive_gaps(state: MainState) -> list[dict[str, str]]:
    """표 14 gaps: 근거 부족 항목(관점, 항목, 사유)과 원문 불일치 주장. 조사·평가 결과에서 코드로 뽑는다."""
    gaps: list[dict[str, str]] = []
    for name in RESEARCH_NODES:
        run = state.get(RESULT_KEYS[name])
        if run is None:
            continue
        if run.status == "failed":
            gaps.append(
                {"role": name, "item": "실행 실패", "reason": "; ".join(run.validation_errors[:2])}
            )
        for item in run.result.unverified:
            gaps.append({"role": name, "item": item, "reason": "근거 부족"})
        for check in run.checks:
            if check.label != "supported":
                gaps.append(
                    {"role": name, "item": check.claim_id, "reason": f"원문 불일치 ({check.label})"}
                )
    return gaps


def derive_trl(
    target_techs: dict[str, str], tech_run: NodeRun | None, synthesis_run: NodeRun | None
) -> dict[str, Any]:
    """표 14 trl_result: 기술 조사가 잠정 추정하고 종합이 확정한다 (설계서 C.3).

    기술 rubric 의 TRL/maturity 기준 판정을 잠정값으로, 종합 rubric 에 TRL 기준이 있으면 확정값으로 옮긴다.
    현재 synthesis rubric 에는 TRL 기준이 없어 확정값은 비어 있을 수 있다. 모든 판정은 공개 정보 기반 추정이다.
    """

    def pick(run: NodeRun | None, keys: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for a in run.result.assessments if run else []:
            if any(k in a.criterion.lower() for k in keys):
                found.setdefault(
                    a.technology,
                    {
                        "criterion": a.criterion,
                        "judgment": a.judgment,
                        "rationale": a.rationale,
                        "evidence_ids": list(a.evidence_ids),
                    },
                )
        return found

    tentative = pick(tech_run, ("trl", "maturity"))
    final = pick(synthesis_run, ("trl",))
    return {
        tech: {
            "tentative": tentative.get(tech),
            "final": final.get(tech),
            "caveat": "공개 정보 기반 추정. 논문 발표와 실제 채택 사이 시차로 TRL 4~6 구간 정보가 가장 비어 있다.",
        }
        for tech in target_techs.values()
    }


def build_main_graph(
    inputs: dict[str, NodeInput],
    mode: str,
    settings: Settings,
    backend: ModelBackend,
    sources: dict[str, EvidenceSource] | None = None,
):
    if mode not in ("mock", "fixture", "live"):
        raise ValueError("Pipeline mode must be mock, fixture or live")
    if mode == "live" and (not sources or set(sources) != set(RESEARCH_NODES)):
        raise ValueError("live mode requires the four research-node sources")
    builder = StateGraph(MainState)

    def run_role(name: str, state: MainState) -> NodeRun:
        """공통 서브그래프 실행. 상위 결과는 입력이며 원문 근거를 대신하지 않는다."""
        data = inputs[name].model_copy(deep=True)
        if mode == "live":
            data.evidence = []  # Never mix demo fixture excerpts with live search.
        runs = [state[RESULT_KEYS[n]] for n in UPSTREAM[name] if RESULT_KEYS[n] in state]
        data.prior_results = {r.node: r.result for r in runs}
        for r in runs:
            if r.status != "completed":
                data.description += (
                    f"\n상위 노드 {r.node}: {r.status}. 미확인/실패 내용을 보존하세요."
                )
        data.evidence = merge_evidence(data.evidence, *(r.evidence for r in runs))
        source = (
            CombinedSource(FixedEvidence(data), sources[name])
            if sources and name in sources
            else FixedEvidence(data)
        )
        graph = build_node_graph(name, data, mode, settings, backend, source)
        return graph.invoke({}, config={"recursion_limit": 30})["output"]

    # ── 코드 노드 ────────────────────────────────────────────────────────────

    def init(state: MainState):
        """입력 초기화: 대상 기술, 도메인, 실행 한도를 State 에 설정 (설계서 B.1 2안)."""
        return {
            "target_techs": dict(settings.target_techs),
            "domain": settings.domain,
            "limits": settings.limits.model_dump(),
            "supplement_round": 0,
            "gaps": [],
            "report_path": "",
            "sources": [],
        }

    def make_node(name: str):
        def node(state: MainState):
            run = run_role(name, state)
            return {RESULT_KEYS[name]: run, "sources": cited_evidence(run)}

        return node

    def collect(state: MainState):
        """결과 수집: 세 평가의 완료를 기다려 종합으로 전달. 상태를 바꾸지 않는다."""
        return {}

    def synthesis(state: MainState):
        run = run_role("synthesis", state)
        return {
            "synthesis": run,
            "sources": cited_evidence(run),
            "trl_result": derive_trl(state["target_techs"], state.get("tech_result"), run),
            "gaps": derive_gaps(state),
        }

    def supplement(state: MainState):
        """보완 재실행: gaps 의 담당 역할만 1회 재실행하고 결과 키를 교체 (설계서 표 12·13).

        기술 사실이 바뀌면 그것을 참고한 평가도 다시 돈다. 출처는 리듀서가 누적하므로 덮어쓰지 않는다.
        """
        roles = {g["role"] for g in state["gaps"] if g["role"] in RESEARCH_NODES}
        if "tech" in roles:
            roles |= set(EVAL_NODES)
        updates: dict[str, Any] = {"supplement_round": state["supplement_round"] + 1}
        working: dict[str, Any] = dict(state)
        new_sources: list[Evidence] = []
        for name in [n for n in ("tech",) + EVAL_NODES if n in roles]:
            run = run_role(name, working)  # type: ignore[arg-type]
            working[RESULT_KEYS[name]] = run  # 재실행된 기술 조사 결과를 뒤 평가가 보게 한다.
            updates[RESULT_KEYS[name]] = run
            new_sources.extend(cited_evidence(run))
        updates["sources"] = new_sources
        return updates

    def report(state: MainState):
        run = run_role("report", state)
        return {"report": run, "sources": cited_evidence(run)}

    def citation_check(state: MainState):
        """인용·형식 검사 (설계서 표 18). 실패하면 정상 완료로 처리하지 않는다.

        REFERENCE 대상은 실제 인용된 출처뿐이며, 서지 필드 검사는 runtime.handoff 와 같은 규칙을 쓴다.
        mock 은 고정 문자열 연결 점검이라 서지 검사를 하지 않는다.
        """
        seen: set[str] = set()
        references: list[Evidence] = []
        for e in state.get("sources", []):
            if e.id not in seen:
                seen.add(e.id)
                references.append(e)
        problems: list[str] = []
        if mode != "mock":
            for e in references:
                problems.extend(reference_issues(e))
        report_run = state.get("report")
        if report_run is None:
            problems.append("report node produced no output")
        else:
            report_run = report_run.model_copy(deep=True)
            if problems:
                report_run.validation_errors.extend(f"citation_check: {p}" for p in problems)
                if report_run.status == "completed":
                    report_run.status = "needs_revision"
        statuses = [state[k].status for k in RESULT_KEYS.values() if k in state]
        if report_run is not None:
            statuses.append(report_run.status)
        run_status = (
            "failed"
            if "failed" in statuses
            else ("needs_revision" if "needs_revision" in statuses or problems else "completed")
        )
        verdict = "OK" if not problems else f"FAIL {len(problems)}건"
        updates: dict[str, Any] = {
            "run_status": run_status,
            "report_path": f"report.md [citation_check: {verdict}; references: {len(references)}]",
        }
        if report_run is not None:
            updates["report"] = report_run
        return updates

    # ── 분기 ────────────────────────────────────────────────────────────────

    def route_after_synthesis(state: MainState) -> str:
        if state.get("gaps") and state.get("supplement_round", 0) < settings.limits.supplement:
            return "supplement"
        return "report"

    # ── 조립 (그림 1) ────────────────────────────────────────────────────────

    builder.add_node("init", init)
    for name in RESEARCH_NODES:
        builder.add_node(name, make_node(name))
    builder.add_node("collect", collect)
    builder.add_node("synthesis", synthesis)
    builder.add_node("supplement", supplement)
    builder.add_node("report", report)
    builder.add_node("citation_check", citation_check)

    builder.add_edge(START, "init")
    builder.add_edge("init", "tech")
    for role in EVAL_NODES:
        builder.add_edge("tech", role)
    builder.add_edge(list(EVAL_NODES), "collect")
    builder.add_edge("collect", "synthesis")
    builder.add_conditional_edges("synthesis", route_after_synthesis, ["supplement", "report"])
    builder.add_edge("supplement", "synthesis")
    builder.add_edge("report", "citation_check")
    builder.add_edge("citation_check", END)
    return builder.compile()

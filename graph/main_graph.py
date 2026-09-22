from __future__ import annotations

import json
import operator
from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from graph.node_graph import build_node_graph
from rag.evidence import merge_evidence
from rag.interface import CombinedSource, EvidenceSource, FixedEvidence
from runtime.models import ModelBackend
from runtime.reporting import assemble_report, collect_gaps, used_ids, validate_report, write_report
from runtime.settings import Settings
from schemas.contracts import NODES, Evidence, Gap, NodeInput, NodeRun

# Preserve the result key names from the shared team starter.
RESULT_KEYS = {
    "tech": "tech_result",
    "market": "market_result",
    "stakeholder": "stakeholder_result",
    "domain": "domain_result",
    "synthesis": "synthesis",
    "report": "report",
}


class MainState(TypedDict, total=False):
    target_techs: dict
    domain: str
    limits: dict
    sources: Annotated[list[Evidence], operator.add]
    trl_result: dict
    gaps: list[Gap]
    supplement_round: int
    report_path: str
    report_check: dict
    tech_result: NodeRun
    market_result: NodeRun
    stakeholder_result: NodeRun
    domain_result: NodeRun
    synthesis: NodeRun
    report: NodeRun
    run_status: str


def build_main_graph(
    inputs: dict[str, NodeInput],
    mode: str,
    settings: Settings,
    backend: ModelBackend,
    sources: dict[str, EvidenceSource] | None = None,
):
    if mode not in ("mock", "fixture", "live"):
        raise ValueError("Pipeline mode must be mock, fixture or live")
    if mode == "live" and (
        not sources or set(sources) != {"tech", "market", "stakeholder", "domain"}
    ):
        raise ValueError("live mode requires the four research-node sources")
    builder = StateGraph(MainState)

    def initialize(state):
        return {
            "target_techs": settings.target_techs,
            "domain": settings.domain,
            "limits": settings.limits.model_dump(),
            "sources": [],
            "gaps": [],
            "trl_result": {},
            "supplement_round": 0,
            "report_path": "",
        }

    builder.add_node("initialize", initialize)

    def make_node(name):
        def run(state):
            data = inputs[name].model_copy(deep=True)
            if mode == "live":
                data.evidence = []  # Never mix demo fixture excerpts with live search.
            keys = [RESULT_KEYS[n] for n in NODES if RESULT_KEYS[n] in state]
            runs = [state[key] for key in keys]
            data.prior_results = {r.node: r.result for r in runs}
            for r in runs:
                if r.status != "completed":
                    data.description += (
                        f"\n상위 노드 {r.node}: {r.status}. 미확인/실패 내용을 보존하세요."
                    )
            data.evidence = merge_evidence(data.evidence, *(r.evidence for r in runs))
            if name == "synthesis" and state.get("gaps"):
                # Code-rule gaps are data for the synthesis prompt, never a judgment to fill in.
                data.description += "\n코드 규칙 gaps: " + json.dumps(
                    [g.model_dump() for g in state["gaps"]], ensure_ascii=False
                )
            if name in ("market", "stakeholder", "domain") and "tech_result" in state:
                # Prior result is an input, never a replacement for original evidence.
                data.prior_results = {"tech": state["tech_result"].result}
            source = (
                CombinedSource(FixedEvidence(data), sources[name])
                if sources and name in sources
                else FixedEvidence(data)
            )
            graph = build_node_graph(name, data, mode, settings, backend, source)
            out = graph.invoke({}, config={"recursion_limit": 80})["output"]
            cited = used_ids(out)
            delta = {RESULT_KEYS[name]: out, "sources": [e for e in out.evidence if e.id in cited]}
            if name == "synthesis":
                estimates = {
                    t.technology: t.model_dump()
                    for t in out.result.trl_estimates
                    if not t.provisional
                }
                delta["trl_result"] = {
                    tech: estimates.get(
                        tech,
                        {
                            "technology": tech,
                            "level": None,
                            "rationale": "확정 TRL 근거 부족",
                            "evidence_ids": [],
                            "disclaimer": "공개 정보 기반 추정",
                        },
                    )
                    for tech in settings.target_techs.values()
                }
            return delta

        return run

    for name in NODES:
        builder.add_node(name, make_node(name))

    def collect(state):
        runs = {role: state[RESULT_KEYS[role]] for role in NODES[:4]}
        # Validate reducer entries before using them; concatenation alone is not deduplication.
        merge_evidence(state["sources"])
        return {"gaps": collect_gaps(runs, settings)}

    builder.add_node("collect", collect)

    def check_report(state, config: RunnableConfig):
        text = assemble_report(state, RESULT_KEYS, mode)
        validation = validate_report(state, RESULT_KEYS, text, mode)
        folder = config.get("configurable", {}).get("output_dir")
        path = write_report(text, Path(folder)) if folder else ""
        if not path:
            validation["ready"] = False
            validation["problems"].append("No report output directory was configured")
        statuses = [state[key].status for key in RESULT_KEYS.values()]
        status = (
            "failed"
            if "failed" in statuses
            else "needs_revision"
            if "needs_revision" in statuses or (mode != "mock" and not validation["ready"])
            else "completed"
        )
        return {"run_status": status, "report_path": path, "report_check": validation}

    builder.add_node("check_report", check_report)
    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "tech")
    for role in ("market", "stakeholder", "domain"):
        builder.add_edge("tech", role)
    builder.add_edge(["market", "stakeholder", "domain"], "collect")
    builder.add_edge("collect", "synthesis")
    builder.add_edge("synthesis", "report")
    builder.add_edge("report", "check_report")
    builder.add_edge("check_report", END)
    return builder.compile()

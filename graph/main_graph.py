from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from graph.node_graph import build_node_graph
from rag.evidence import merge_evidence
from rag.interface import CombinedSource, EvidenceSource, FixedEvidence
from runtime.models import ModelBackend
from runtime.settings import Settings
from schemas.contracts import NODES, NodeInput, NodeRun

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
            if name in ("market", "stakeholder", "domain") and "tech_result" in state:
                # Prior result is an input, never a replacement for original evidence.
                data.prior_results = {"tech": state["tech_result"].result}
            source = (
                CombinedSource(FixedEvidence(data), sources[name])
                if sources and name in sources
                else FixedEvidence(data)
            )
            graph = build_node_graph(name, data, mode, settings, backend, source)
            out = graph.invoke({}, config={"recursion_limit": 30})["output"]
            return {RESULT_KEYS[name]: out}

        return run

    for name in NODES:
        builder.add_node(name, make_node(name))
    builder.add_node("collect", lambda state: {})

    def finish(state):
        statuses = [state[key].status for key in RESULT_KEYS.values()]
        status = (
            "failed"
            if "failed" in statuses
            else ("needs_revision" if "needs_revision" in statuses else "completed")
        )
        return {"run_status": status}

    builder.add_node("finish", finish)
    builder.add_edge(START, "tech")
    for role in ("market", "stakeholder", "domain"):
        builder.add_edge("tech", role)
    builder.add_edge(["market", "stakeholder", "domain"], "collect")
    builder.add_edge("collect", "synthesis")
    builder.add_edge("synthesis", "report")
    builder.add_edge("report", "finish")
    builder.add_edge("finish", END)
    return builder.compile()

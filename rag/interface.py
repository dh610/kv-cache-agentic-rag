from __future__ import annotations

from typing import Protocol

from rag.evidence import merge_evidence
from schemas.contracts import Evidence, NodeInput, NodeName, Question


class EvidenceSource(Protocol):
    retryable: bool

    def search(
        self, question: Question, attempt: int, scope: str = "target"
    ) -> list[Evidence]: ...


class FixedEvidence:
    retryable = False

    def __init__(self, data: NodeInput):
        self.evidence = data.evidence

    def search(self, question: Question, attempt: int, scope: str = "target") -> list[Evidence]:
        return [e for e in self.evidence if e.technology in (question.technology, "other")]


class CombinedSource:
    retryable = True

    def __init__(self, *sources: EvidenceSource):
        self.sources = sources

    def search(self, question: Question, attempt: int, scope: str = "target") -> list[Evidence]:
        # Each provider must succeed; a failed provider is recorded by the node runtime.
        found = []
        for source in self.sources:
            found = merge_evidence(found, source.search(question, attempt, scope))
        return found


def live_sources(settings):
    from copy import copy

    from rag.local_index import PaperSource
    from rag.web import WebSource

    web = WebSource(settings)
    papers = PaperSource(settings, "tech")
    competitor_refs = copy(papers)
    competitor_refs.node = "stakeholder"
    sources = {"tech": papers, "stakeholder": CombinedSource(web, competitor_refs)}
    for role in ("market", "domain"):
        adapter = copy(papers)  # Share the heavyweight encoder/index and its lock.
        adapter.node = role
        sources[role] = CombinedSource(adapter, web)
    return sources


def make_source(mode: str, node: NodeName, data: NodeInput, settings):
    if mode in ("mock", "fixture"):
        return FixedEvidence(data)
    if node in ("synthesis", "report"):
        raise ValueError(
            "synthesis/report use fixture inputs or upstream results, not new searches"
        )
    if mode == "rag":
        if node == "stakeholder":
            raise ValueError("stakeholder is web-only in the simplified baseline; use web/fixture")
        from rag.local_index import PaperSource

        return PaperSource(settings, node)
    if mode == "web":
        if node == "tech":
            raise ValueError("tech requires paper evidence; use rag/fixture")
        from rag.web import WebSource

        return WebSource(settings)
    raise ValueError(f"Unknown evidence mode: {mode}")

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


class PartialSearch(Exception):
    """공급자 일부만 실패했다. 성공한 근거는 살리고 실패 사실은 기록한다."""

    def __init__(self, found: list[Evidence], reason: str):
        super().__init__(reason)
        self.found = found


class CombinedSource:
    retryable = True

    def __init__(self, *sources: EvidenceSource):
        self.sources = sources

    def search(self, question: Question, attempt: int, scope: str = "target") -> list[Evidence]:
        """한 공급자가 실패해도 다른 공급자의 근거는 버리지 않는다.

        웹 API 한도 초과로 웹 검색이 실패했을 때 같은 노드의 논문 근거까지 사라져
        도메인 평가가 근거 0건이 된 적이 있다 (live 점검). 설계서 D.3에 따라 근거 부족은
        실행 실패와 구분한다. 전부 실패하면 그대로 예외를 올린다.
        """
        found: list[Evidence] = []
        failures = []
        for source in self.sources:
            try:
                found = merge_evidence(found, source.search(question, attempt, scope))
            except Exception as exc:
                failures.append(f"{type(source).__name__}: {type(exc).__name__}")
        if failures and not found:
            raise RuntimeError("; ".join(failures))
        if failures:
            raise PartialSearch(found, "; ".join(failures))
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

from __future__ import annotations

from typing import Protocol

from rag.evidence import merge_evidence
from schemas.contracts import Evidence, NodeInput, NodeName, Question


class EvidenceSource(Protocol):
    retryable: bool

    def search(self, question: Question, attempt: int) -> list[Evidence]: ...


class FixedEvidence:
    retryable = False

    def __init__(self, data: NodeInput):
        self.evidence = data.evidence

    def search(self, question: Question, attempt: int) -> list[Evidence]:
        return [e for e in self.evidence if e.technology in (question.technology, "other")]


class CombinedSource:
    retryable = True

    def __init__(self, *sources: EvidenceSource):
        self.sources = sources

    def search(self, question: Question, attempt: int) -> list[Evidence]:
        # Each provider must succeed; a failed provider is recorded by the node runtime.
        found = []
        for source in self.sources:
            found = merge_evidence(found, source.search(question, attempt))
        return found


class RoleFilter:
    """논문 근거를 문서 role 로 거른다 (설계서 B.3).

    기술 조사는 target 만, 시장은 target+reference, 도메인은 target 만, 이해관계자는 경쟁 진영 근거(reference)만.
    PaperSource.accepts() 는 검색 담당 파일이므로 여기서 결과를 후처리한다. 웹 근거는 그대로 통과한다.
    """

    retryable = True

    def __init__(self, inner: EvidenceSource, roles: set[str]):
        self.inner, self.roles = inner, set(roles)

    def search(self, question: Question, attempt: int) -> list[Evidence]:
        return [
            e
            for e in self.inner.search(question, attempt)
            if e.source_type != "paper" or e.document_role in self.roles
        ]


PAPER_ROLES: dict[str, set[str]] = {
    "tech": {"target"},
    "market": {"target", "reference"},
    "domain": {"target"},
    "stakeholder": {"reference"},
}


def live_sources(settings):
    from copy import copy

    from rag.local_index import PaperSource
    from rag.web import WebSource

    web = WebSource(settings)
    papers = PaperSource(settings, "tech")

    def paper_for(role: str) -> EvidenceSource:
        adapter = copy(papers)  # Share the heavyweight encoder/index and its lock.
        adapter.node = role
        return RoleFilter(adapter, PAPER_ROLES[role])

    return {
        "tech": papers,
        "market": CombinedSource(paper_for("market"), web),
        "domain": CombinedSource(paper_for("domain"), web),
        # 이해관계자: 웹이 주 경로, 경쟁 기술 진영의 근거에 한해 보조 문서(reference)를 조회한다 (표 3 각주·D.2).
        "stakeholder": CombinedSource(web, paper_for("stakeholder")),
    }


def make_source(mode: str, node: NodeName, data: NodeInput, settings):
    if mode in ("mock", "fixture"):
        return FixedEvidence(data)
    if node in ("synthesis", "report"):
        raise ValueError(
            "synthesis/report use fixture inputs or upstream results, not new searches"
        )
    if mode == "rag":
        from rag.local_index import PaperSource

        # 단독 실행에서도 설계서 B.3 의 문서 role 범위를 지킨다.
        return RoleFilter(PaperSource(settings, node), PAPER_ROLES[node])
    if mode == "web":
        if node == "tech":
            raise ValueError("tech requires paper evidence; use rag/fixture")
        from rag.web import WebSource

        return WebSource(settings)
    raise ValueError(f"Unknown evidence mode: {mode}")

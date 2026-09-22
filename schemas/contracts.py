from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

NodeName = Literal["tech", "market", "stakeholder", "domain", "synthesis", "report"]
NODES: tuple[NodeName, ...] = ("tech", "market", "stakeholder", "domain", "synthesis", "report")
RESEARCH_NODES = NODES[:4]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(Contract):
    id: str
    text: str
    title: str
    url: str
    technology: str
    source_type: Literal["paper", "web", "patent", "fixture"]
    scope: Literal["target", "context"]
    document_role: Literal["target", "reference"] = "target"
    page: int | None = Field(default=None, ge=1)
    document_id: str | None = None
    authors: str | None = None
    year: int | None = Field(default=None, ge=1900, le=2100)
    venue: str | None = None
    # Guide REFERENCE format: paper 권(호)·페이지 or arXiv number; patent 특허번호/공개번호.
    citation_id: str | None = None
    publisher: str | None = None
    site: str | None = None
    published_at: str | None = None
    retrieved_at: str | None = None
    affiliation: Literal["first_party", "independent", "unknown"] = "unknown"
    affiliation_reason: str | None = None
    stance: Literal["positive", "critical", "mixed", "neutral", "unknown"] = "unknown"


class Question(Contract):
    id: str
    technology: str
    criterion: str
    text: str


class Claim(Contract):
    id: str
    technology: str
    criterion: str
    text: str
    kind: Literal["fact", "inference"]
    evidence_ids: list[str]
    conditions: list[str]


class Assessment(Contract):
    technology: str
    criterion: str
    judgment: str
    rationale: str
    evidence_ids: list[str]


class TRLEstimate(Contract):
    technology: str
    level: int | None = Field(default=None, ge=1, le=9)
    rationale: str
    evidence_ids: list[str]
    provisional: bool = True
    disclaimer: Literal["공개 정보 기반 추정"] = "공개 정보 기반 추정"


class Gap(Contract):
    role: str
    criterion: str
    reason: str


class QueryPair(Contract):
    question_id: str
    positive: str
    critical: str


class QueryPlan(Contract):
    queries: list[QueryPair]


class Coverage(Contract):
    question_id: str
    sufficient: bool
    evidence_ids: list[str]
    reason: str


class SufficiencyResult(Contract):
    items: list[Coverage]


class NodeResult(Contract):
    """All nodes return the same outer envelope; the runtime decides run status."""

    node: NodeName
    summary: str
    claims: list[Claim]
    assessments: list[Assessment]
    unverified: list[str]
    limitations: list[str]
    trl_estimates: list[TRLEstimate] = Field(default_factory=list)


class NodeInput(Contract):
    case_id: str
    description: str
    target_techs: dict[str, str]
    domain: str
    questions: list[Question]
    evidence: list[Evidence]
    prior_results: dict[str, NodeResult] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_ids(self):
        for name in ("questions", "evidence"):
            ids = [x.id for x in getattr(self, name)]
            if len(ids) != len(set(ids)):
                raise ValueError(f"Duplicate {name} IDs")
        return self


class ClaimCheck(Contract):
    claim_id: str
    label: Literal["supported", "misstated", "unsupported"]
    evidence_ids: list[str]
    reason: str


class JudgeResult(Contract):
    checks: list[ClaimCheck]


class SearchRecord(Contract):
    question_id: str
    attempt: int
    query: str
    evidence_ids: list[str]
    error: str | None = None
    intent: Literal["positive", "critical", "followup", "fixture"] = "fixture"
    # 검색어 층: direct 검색은 target, 접근 전반(background) 검색은 context.
    scope: Literal["target", "context"] = "target"


class NodeRun(Contract):
    node: NodeName
    mode: str
    status: Literal["completed", "needs_revision", "failed"]
    result: NodeResult
    evidence: list[Evidence]
    checks: list[ClaimCheck]
    validation_errors: list[str]
    searches: list[SearchRecord]
    prompt_hash: str
    model: str
    verdict: Literal["통과", "표현 오류", "추가 근거 필요"] = "추가 근거 필요"
    fix_count: int = 0
    coverage: list[Coverage] = Field(default_factory=list)


def empty_result(node: NodeName, reason: str) -> NodeResult:
    return NodeResult(
        node=node, summary=reason, claims=[], assessments=[], unverified=[reason], limitations=[]
    )

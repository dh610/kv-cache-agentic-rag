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
    source_type: Literal["paper", "web", "fixture"]
    scope: Literal["target", "context"]
    document_role: Literal["target", "reference"] = "target"
    page: int | None = Field(default=None, ge=1)
    document_id: str | None = None
    authors: str | None = None
    year: int | None = Field(default=None, ge=1900, le=2100)
    venue: str | None = None
    publisher: str | None = None
    site: str | None = None
    published_at: str | None = None
    retrieved_at: str | None = None


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


class NodeResult(Contract):
    """All nodes return the same outer envelope; the runtime decides run status."""

    node: NodeName
    summary: str
    claims: list[Claim]
    assessments: list[Assessment]
    unverified: list[str]
    limitations: list[str]


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


# 설계서 표 15 verdict: 검증 노드가 결과 단위로 내리는 판정. 서브그래프 분기에 그대로 쓴다.
Verdict = Literal["통과", "표현 오류", "추가 근거 필요"]


class SufficiencyResult(Contract):
    """근거 관련성·충분성 판정 (설계서 그림 2 '근거 관련성·충분성 확인')."""

    sufficient: bool
    missing_question_ids: list[str]
    reason: str


class RewriteResult(Contract):
    """질문 수정 결과: 한국어 질문 + 영어 핵심어를 덧붙인 이중언어 검색어 (설계서 D.2)."""

    query: str


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
    # 설계서 표 15의 서브그래프 상태 중 상위 그래프가 참고할 값. 없던 실행 결과와도 호환되게 기본값을 둔다.
    verdict: Verdict | None = None
    is_sufficient: bool | None = None
    search_count: dict[str, int] = Field(default_factory=dict)
    fix_count: int = 0


def empty_result(node: NodeName, reason: str) -> NodeResult:
    return NodeResult(
        node=node, summary=reason, claims=[], assessments=[], unverified=[reason], limitations=[]
    )

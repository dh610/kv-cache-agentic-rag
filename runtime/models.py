from __future__ import annotations

from typing import Protocol

from runtime.settings import Settings, require_key
from schemas.contracts import (
    Assessment,
    Claim,
    ClaimCheck,
    Evidence,
    JudgeResult,
    NodeInput,
    NodeName,
    NodeResult,
)


class ModelBackend(Protocol):
    name: str

    def generate(
        self, node: NodeName, data: NodeInput, evidence: list[Evidence], system: str, user: str
    ) -> NodeResult: ...

    def judge(self, result: NodeResult, evidence: list[Evidence]) -> JudgeResult: ...


class MockBackend:
    """Offline wiring check, never a technology evaluation or quality benchmark."""

    name = "mock-offline"

    def generate(self, node, data, evidence, system, user):
        claims, assessments, unknown = [], [], []
        for question in data.questions:
            match = next((e for e in evidence if e.technology == question.technology), None)
            if not match:
                unknown.append(f"{question.id}: fixed evidence unavailable")
                continue
            claims.append(
                Claim(
                    id=f"{node}-{question.id}",
                    technology=question.technology,
                    criterion=question.criterion,
                    text=match.text,
                    kind="fact",
                    evidence_ids=[match.id],
                    conditions=["테스트용 고정 발췌; 평가 결론 아님"],
                )
            )
            assessments.append(
                Assessment(
                    technology=question.technology,
                    criterion=question.criterion,
                    judgment="확인 불가",
                    rationale="mock은 연결만 검증하고 평가하지 않습니다.",
                    evidence_ids=[match.id],
                )
            )
        return NodeResult(
            node=node,
            summary=f"[MOCK] {node}: 연결 점검",
            claims=claims,
            assessments=assessments,
            unverified=unknown,
            limitations=["MOCK: LLM 호출 및 실제 판단 없음"],
        )

    def judge(self, result, evidence):
        lookup = {e.id: e for e in evidence}
        return JudgeResult(
            checks=[
                ClaimCheck(
                    claim_id=c.id,
                    label="supported"
                    if any(eid in lookup and c.text == lookup[eid].text for eid in c.evidence_ids)
                    else "unsupported",
                    evidence_ids=c.evidence_ids,
                    reason="MOCK: 원문 문자열 일치만 점검",
                )
                for c in result.claims
            ]
        )


class OpenAIBackend:
    def __init__(self, settings: Settings):
        from langchain_openai import ChatOpenAI

        require_key("OPENAI_API_KEY")
        self.name = settings.models.generator
        kwargs = {
            "temperature": settings.models.temperature,
            "timeout": settings.models.timeout_seconds,
            "max_retries": settings.models.max_retries,
        }
        self.generator = ChatOpenAI(model=self.name, **kwargs).with_structured_output(
            NodeResult, method="json_schema"
        )
        self.evaluator = ChatOpenAI(model=settings.models.judge, **kwargs).with_structured_output(
            JudgeResult, method="json_schema"
        )

    def generate(self, node, data, evidence, system, user):
        return self.generator.invoke([("system", system), ("human", user)])

    def judge(self, result, evidence):
        import json

        from runtime.settings import ROOT

        # Nothing to verify: an honest all-unknown result must not receive invented checks.
        if not result.claims:
            return JudgeResult(checks=[])
        prompt = (ROOT / "prompts/shared/judge.j2").read_text(encoding="utf-8")
        # This shared prompt has no variables; claim/evidence JSON is a separate message.
        payload = json.dumps(
            {
                "result": result.model_dump(),
                "evidence": [e.model_dump() for e in evidence],
            },
            ensure_ascii=False,
        )
        return self.evaluator.invoke([("system", prompt), ("human", payload)])

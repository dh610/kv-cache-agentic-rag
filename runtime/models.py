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
    Question,
    RewriteResult,
    SufficiencyResult,
)


class ModelBackend(Protocol):
    name: str

    def generate(
        self, node: NodeName, data: NodeInput, evidence: list[Evidence], system: str, user: str
    ) -> NodeResult: ...

    def judge(self, result: NodeResult, evidence: list[Evidence]) -> JudgeResult: ...

    def sufficiency(
        self, questions: list[Question], evidence: list[Evidence]
    ) -> SufficiencyResult: ...

    def rewrite(self, question: Question, previous_query: str, attempt: int) -> str: ...

    def fix(
        self, result: NodeResult, checks: list[ClaimCheck], evidence: list[Evidence]
    ) -> NodeResult: ...


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

    def sufficiency(self, questions, evidence):
        # 연결 점검용 규칙: 질문의 technology 근거가 하나라도 있으면 충분으로 본다 (generate와 같은 기준).
        techs = {e.technology for e in evidence}
        missing = [q.id for q in questions if q.technology not in techs]
        return SufficiencyResult(
            sufficient=not missing,
            missing_question_ids=missing,
            reason="MOCK: technology 일치 근거 유무만 점검",
        )

    def rewrite(self, question, previous_query, attempt):
        # 결정적 이중언어 확장: 한국어 원문 유지 + 영어 핵심어 추가. LLM 재작성의 자리 표시자다.
        suffix = f"{question.technology} {question.criterion} evidence attempt{attempt}"
        return previous_query if previous_query.endswith(suffix) else f"{previous_query} {suffix}"

    def fix(self, result, checks, evidence):
        # mock은 표현을 고치지 못한다. 그대로 돌려보내 fix 한도(1회)와 재검증 흐름만 점검한다.
        return result


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
        # 충분성 판정·질의 재작성은 반복 호출이 많아 judge(nano) 모델, 수정은 generator(mini) 모델.
        judge_llm = ChatOpenAI(model=settings.models.judge, **kwargs)
        self.sufficiency_judge = judge_llm.with_structured_output(
            SufficiencyResult, method="json_schema"
        )
        self.rewriter = judge_llm.with_structured_output(RewriteResult, method="json_schema")
        self.fixer = ChatOpenAI(model=self.name, **kwargs).with_structured_output(
            NodeResult, method="json_schema"
        )

    def generate(self, node, data, evidence, system, user):
        return self.generator.invoke([("system", system), ("human", user)])

    def judge(self, result, evidence):
        import json

        from runtime.settings import ROOT

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

    @staticmethod
    def _shared(name: str, payload: dict) -> list[tuple[str, str]]:
        import json

        from runtime.settings import ROOT

        # 공유 프롬프트는 변수가 없고, 데이터 JSON은 별도 메시지로 넘긴다 (judge와 같은 방식).
        prompt = (ROOT / "prompts/shared" / f"{name}.j2").read_text(encoding="utf-8")
        return [("system", prompt), ("human", json.dumps(payload, ensure_ascii=False))]

    def sufficiency(self, questions, evidence):
        result = self.sufficiency_judge.invoke(
            self._shared(
                "sufficiency",
                {
                    "questions": [q.model_dump() for q in questions],
                    "evidence": [e.model_dump() for e in evidence],
                },
            )
        )
        # 모델이 지어낸 id는 버린다. 질문 밖의 id로 분기가 흔들리면 안 된다.
        known = {q.id for q in questions}
        result.missing_question_ids = [i for i in result.missing_question_ids if i in known]
        result.sufficient = result.sufficient and not result.missing_question_ids
        return result

    def rewrite(self, question, previous_query, attempt):
        result = self.rewriter.invoke(
            self._shared(
                "rewrite",
                {
                    "question": question.model_dump(),
                    "previous_query": previous_query,
                    "attempt": attempt,
                },
            )
        )
        query = result.query.strip() or previous_query
        # 설계서 D.2: 한국어 질문에 영어 핵심어를 '덧붙인다'. 모델이 원문을 버리면 코드가 되살린다.
        if question.text not in query:
            query = f"{question.text} {query}"
        return query

    def fix(self, result, checks, evidence):
        return self.fixer.invoke(
            self._shared(
                "fix",
                {
                    "result": result.model_dump(),
                    "checks": [c.model_dump() for c in checks],
                    "evidence": [e.model_dump() for e in evidence],
                },
            )
        )

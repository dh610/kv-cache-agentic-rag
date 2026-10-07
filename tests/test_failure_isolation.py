"""검증 실패는 결함이 있는 항목에만 적용된다.

live 점검에서 형식 오류 한 건이 노드 전체를 덮어써, 근거가 확인된 내용까지 모두
"확인 불가"로 바뀌고 REFERENCE 가 비는 문제가 있었다. 부분 실패가 부분에만 남는지 확인한다.
"""

from app.evaluate_tech_trl import load_case
from graph.node_graph import build_node_graph
from rag.interface import FixedEvidence
from runtime.models import MockBackend
from runtime.settings import load_settings
from schemas.contracts import (
    Assessment,
    Claim,
    ClaimCheck,
    JudgeResult,
    NodeResult,
)


def tech_run(extra_claim: Claim | None, checks: list[ClaimCheck]):
    case, data = load_case("itme_paper")
    data.questions = [q for q in data.questions if q.criterion == "maturity"]
    ids = case["required_maturity_evidence_ids"]
    good = Claim(
        id="maturity-fact",
        technology="ITME",
        criterion="maturity",
        text="고정 근거에 기술된 검증 수준",
        kind="fact",
        evidence_ids=ids,
        conditions=["고정 근거 범위"],
    )
    result = NodeResult(
        node="tech",
        summary="평가용 결과",
        claims=[good] + ([extra_claim] if extra_claim else []),
        assessments=[
            Assessment(
                technology="ITME",
                criterion="maturity",
                judgment="TRL 6",
                rationale="공개 정보 기반 잠정 추정",
                evidence_ids=ids,
            )
        ],
        unverified=[],
        limitations=[],
    )

    class FixedBackend(MockBackend):
        def generate(self, *args):
            return result.model_copy(deep=True)

        def judge(self, *args):
            return JudgeResult(checks=list(checks))

    state = build_node_graph(
        "tech", data, "fixture", load_settings(), FixedBackend(), FixedEvidence(data)
    ).invoke({})
    return case, state["output"]


def supported(claim_id: str, ids: list[str]) -> ClaimCheck:
    return ClaimCheck(claim_id=claim_id, label="supported", evidence_ids=ids, reason="근거 일치")


def test_rejected_claim_does_not_remove_the_confirmed_one():
    ids = load_case("itme_paper")[0]["required_maturity_evidence_ids"]
    _, output = tech_run(
        Claim(
            id="extra-fact",
            technology="ITME",
            criterion="maturity",
            text="근거가 뒷받침하지 않는 주장",
            kind="fact",
            evidence_ids=ids,
            conditions=[],
        ),
        [
            supported("maturity-fact", ids),
            ClaimCheck(
                claim_id="extra-fact",
                label="unsupported",
                evidence_ids=[],
                reason="원문이 뒷받침하지 않음",
            ),
        ],
    )
    assert [c.id for c in output.result.claims] == ["maturity-fact"]
    assert any("근거가 뒷받침하지 않는 주장" in u for u in output.result.unverified)
    assert [a.judgment for a in output.result.assessments] == ["TRL 6"]


def test_unchecked_claim_stays_unverified_without_blanking_the_node():
    ids = load_case("itme_paper")[0]["required_maturity_evidence_ids"]
    _, output = tech_run(
        Claim(
            id="unchecked-fact",
            technology="ITME",
            criterion="maturity",
            text="Judge 가 빠뜨린 주장",
            kind="fact",
            evidence_ids=ids,
            conditions=[],
        ),
        [supported("maturity-fact", ids)],
    )
    # 판정이 없는 주장은 미확인으로 남지만, 확인된 주장과 등급은 보존된다.
    assert [c.id for c in output.result.claims] == ["maturity-fact"]
    assert any("Judge 가 빠뜨린 주장" in u for u in output.result.unverified)
    assert [a.judgment for a in output.result.assessments] == ["TRL 6"]
    assert output.validation_errors


def test_one_failing_provider_does_not_discard_the_others():
    """웹 API 한도 초과로 도메인 평가가 근거 0건이 된 실제 사례를 막는다."""
    import pytest

    from rag.interface import CombinedSource, PartialSearch
    from schemas.contracts import Question

    class Works:
        retryable = True

        def __init__(self, items):
            self.items = items

        def search(self, question, attempt, scope="target"):
            return self.items

    class Fails:
        retryable = True

        def search(self, question, attempt, scope="target"):
            raise RuntimeError("HTTPStatusError")

    _, data = load_case("itme_paper")
    question = Question(id="q", technology="ITME", criterion="maturity", text="근거")
    kept = data.evidence[:2]

    with pytest.raises(PartialSearch) as partial:
        CombinedSource(Works(kept), Fails()).search(question, 1)
    assert [e.id for e in partial.value.found] == [e.id for e in kept]

    with pytest.raises(RuntimeError):
        CombinedSource(Fails(), Fails()).search(question, 1)


def test_nested_combined_sources_keep_partial_results():
    """중첩된 CombinedSource 에서도 성공한 공급자의 근거는 남는다."""
    import pytest

    from rag.interface import CombinedSource, PartialSearch
    from schemas.contracts import Question

    class Works:
        retryable = True

        def __init__(self, items):
            self.items = items

        def search(self, question, attempt, scope="target"):
            return self.items

    class Fails:
        retryable = True

        def search(self, question, attempt, scope="target"):
            raise RuntimeError("HTTPStatusError")

    _, data = load_case("itme_paper")
    question = Question(id="q", technology="ITME", criterion="maturity", text="근거")
    kept = data.evidence[:2]
    inner = CombinedSource(Works(kept), Fails())
    outer = CombinedSource(Works([]), inner)

    with pytest.raises(PartialSearch) as partial:
        outer.search(question, 1)
    assert [e.id for e in partial.value.found] == [e.id for e in kept]

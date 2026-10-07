"""Supervisor State → 기존 보고서 조립기가 기대하는 평면 State 로의 어댑터.

보고서 조판(``runtime.reporting``)은 RAG 과제에서 검증된 코드 그대로 쓴다. 조정 계층만
패턴에 맞게 바뀌었으므로, 여기서 키 이름만 맞춰 준다. 조정 계층(``agents``)과 산출
계층(``runtime``)을 섞지 않으려고 어댑터를 따로 둔다.
"""

from __future__ import annotations

from runtime.reporting import collect_gaps
from schemas.contracts import NODES, NodeRun, empty_result

# 기존 메인 그래프가 쓰던 결과 키 이름. 보고서 조립기의 계약이다.
RESULT_KEYS = {
    "tech": "tech_result",
    "market": "market_result",
    "stakeholder": "stakeholder_result",
    "domain": "domain_result",
    "synthesis": "synthesis",
    "report": "report",
}


def report_ready(state) -> bool:
    return all(role in state.get("results", {}) for role in NODES)


def gaps_from_results(state, settings):
    """6장 한계점에 쓰는 보완 대상. 기존 gap 정책(config.yaml)을 그대로 쓴다."""
    results = state.get("results", {})
    runs = {role: results[role] for role in NODES[:4] if role in results}
    return collect_gaps(runs, settings) if runs else []


def placeholder(role: str, mode: str, control=None) -> NodeRun:
    """결과가 없는 역할의 빈 자리.

    Fall-back 경로에서 필요하다. 하위 에이전트가 끝내 실패하거나 예산 소진으로 제외되면
    그 역할의 ``NodeRun`` 이 없는데, 보고서 조립기는 여섯 역할을 모두 요구한다. 빈 자리를
    만들어 **나머지 근거로 보고서는 내고**, 그 역할이 비었다는 사실을 보고서에 남긴다.
    실패를 숨기는 기본값이 아니라, 실패를 보이게 만드는 빈 자리다.
    """
    reason = "하위 에이전트 결과 없음 (실패 또는 시도 한도 소진)"
    if control is not None and control.last_error:
        reason += f": {control.last_error}"
    return NodeRun(
        node=role,
        mode=mode,
        status="failed",
        result=empty_result(role, reason),
        evidence=[],
        checks=[],
        validation_errors=[reason],
        searches=[],
        prompt_hash="",
        model="none",
    )


def flat_state(state, settings, mode: str = "mock") -> dict:
    """``assemble_report`` · ``validate_report`` 가 읽는 키만 추려 평면 dict 로 만든다."""
    results = state.get("results", {})
    control = state.get("control", {})
    view = {
        "target_techs": state["target_techs"],
        "domain": state["domain"],
        "trl_result": {
            tech: state.get("trl_result", {}).get(
                tech,
                {
                    "technology": tech,
                    "level": None,
                    "rationale": "평가 종합 결과 없음",
                    "evidence_ids": [],
                    "disclaimer": "공개 정보 기반 추정",
                },
            )
            for tech in state["target_techs"].values()
        },
        "sources": state.get("sources", []),
        "gaps": gaps_from_results(state, settings),
    }
    view.update(
        {
            RESULT_KEYS[role]: results.get(role) or placeholder(role, mode, control.get(role))
            for role in NODES
        }
    )
    return view

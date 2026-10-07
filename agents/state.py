"""Supervisor 패턴의 State 스키마와 병합 규칙 (과제 가이드 C).

설계 원칙 — 각 항목은 README 의 "State Schema" 절과 1:1 로 대응한다.

제어 vs 페이로드 분리
    조정에 필요한 최소 상태(``trace_id`` · ``step`` · ``control`` · ``route`` · 한도)와
    작업 결과(``results`` · ``sources`` · ``report_text`` …)를 아래 두 블록으로 나눈다.
    Supervisor 의 라우팅 함수는 **제어 블록만** 읽고 분기한다. 페이로드를 읽어야
    분기할 수 있다면 그것은 제어 상태로 승격해야 한다는 뜻이고, 그 승격을 하는 곳이
    ``agents/sufficiency.py`` 다 (NodeRun 을 읽어 RoleControl 로 요약한다).

관측성 위치
    결정 로그의 **본문**(사유·대상·시각)은 State 가 아니라 외부 관측성 계층
    (``agents/observability.py`` → ``decisions.jsonl``)에 적재한다. State 에는 재개 판단에
    필요한 최근 ``DECISION_WINDOW`` 건의 요약만 남긴다. 전부를 State 에 담으면 체크포인트
    마다 로그 전체가 복사되고, 전부를 빼면 중단 후 재개할 때 직전 판단을 알 수 없다.

지속성 비용
    근거 원문·검색 이력 같은 대용량 누적 데이터는 노드가 끝나는 즉시 디스크
    (``<output_dir>/nodes/<role>.json``)로 내보내고, State 의 ``sources`` 에는 **실제 인용된**
    근거만 올린다. ``decisions`` 는 윈도우로, ``control`` 은 역할당 한 건으로 상한이 있다.

상관
    ``trace_id`` 와 ``run_id`` 가 State · decisions.jsonl · LangSmith 메타데이터에 모두
    실린다. 외부 로그 한 줄에서 State 의 어느 스텝인지 역추적할 수 있다.

재개/복구
    ``control[role]`` 이 역할별 ``status`` · ``attempts`` · ``last_error`` 를 들고 있어,
    중단 지점부터 "무엇이 끝났고 무엇을 몇 번 시도했는지"를 State 만으로 복원한다.

동시 처리
    Supervisor 는 서로 독립인 관점을 한 스텝에 **함께** 보낼 수 있다(라우팅 함수가 노드
    이름 리스트를 반환). 그래서 ``results`` · ``sources`` · ``decisions`` · ``control`` 은
    동시 쓰기 필드이며 전부 reducer 를 갖는다.

종료 보장
    ``step`` / ``max_steps``, ``RoleControl.attempts`` / ``max_attempts``,
    ``revision_round`` / ``max_revisions``, ``quality_round`` / ``max_quality_rounds`` 의
    네 겹 상한을 둔다. 어느 하나라도 소진되면 Supervisor 는 ``finalize`` 로만 분기한다.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from agents.merge import merge_runs
from rag.evidence import merge_evidence
from schemas.contracts import (
    CriterionVerdict,  # noqa: F401  (조정 계층에서 재노출: agents.state 가 State 계약의 창구)
    Evidence,
    NodeRun,
    QualityVerdict,
)

# State 에 남기는 결정 요약의 상한. 본문은 decisions.jsonl 에 전부 남는다.
DECISION_WINDOW = 40

RoleStatus = Literal["pending", "completed", "needs_revision", "failed", "skipped"]
Action = Literal[
    "dispatch",  # 하위 에이전트에게 조사 지시
    "rework",  # 근거 부족으로 같은 하위 에이전트에게 재작업 지시
    "synthesize",
    "report",
    "quality",
    "finalize",
]


class Decision(BaseModel):
    """Supervisor 가 내린 분기 하나. 사유 없이 기록하지 않는다."""

    model_config = ConfigDict(extra="forbid")

    step: int
    action: Action
    targets: list[str]
    reason: str
    at: str


class OpenItem(BaseModel):
    """아직 판정이 나오지 않은 (기술, 평가 항목). 재작업 배정의 단위다."""

    model_config = ConfigDict(extra="forbid")

    technology: str
    criterion: str

    def key(self) -> str:
        return f"{self.technology}/{self.criterion}"


class WorkItem(BaseModel):
    """Supervisor 가 하위 에이전트 하나에게 내리는 배정.

    ``Send`` 의 payload 로 실려 간다. 작업 범위가 배정 자체에 적혀 있으므로 하위
    에이전트가 공유 State 를 뒤져 자기 할 일을 다시 추론하지 않는다.

    ``technologies`` · ``criteria`` 가 비면 그 역할의 질문 전체를 뜻한다. 재작업에서는
    부족한 항목만 담기므로, 한 스텝에 나가는 배정의 **개수와 범위가 실행마다 달라진다**.
    """

    model_config = ConfigDict(extra="forbid")

    role: str
    technologies: list[str] = Field(default_factory=list)
    criteria: list[str] = Field(default_factory=list)
    reason: str = ""
    attempt: int = 1

    def label(self) -> str:
        scope = "+".join(self.technologies) if self.technologies else "전체"
        items = f"/{','.join(self.criteria)}" if self.criteria else ""
        return f"{self.role}:{scope}{items}"


class RoleControl(BaseModel):
    """하위 에이전트 하나의 제어 메타. 결과 본문이 아니라 '조정에 필요한 요약'이다."""

    model_config = ConfigDict(extra="forbid")

    role: str
    status: RoleStatus = "pending"
    attempts: int = 0
    # 근거 충분도 0..1. None 은 아직 평가 전.
    sufficiency: float | None = None
    sufficient: bool = False
    # 미해결 항목의 식별자만 담는다(사유 본문은 외부 로그로).
    open_gaps: list[str] = Field(default_factory=list)
    # 그중 재작업으로 다시 시도할 수 있는 (기술, 항목). 배정을 만드는 입력이다.
    open_items: list[OpenItem] = Field(default_factory=list)
    last_error: str | None = None
    # 직전 실행이 확보한 근거 수와, 재작업해도 근거가 늘지 않았는지 여부.
    # "아직 덜 찾은 것"과 "원래 없는 것"을 구분하는 신호다.
    evidence_count: int = 0
    stalled: bool = False
    # 결과 본문이 저장된 경로. State 가 아니라 디스크가 원본이다.
    artifact: str | None = None


def merge_results(current: dict[str, NodeRun] | None, update: dict[str, NodeRun] | None):
    """역할별 결과를 합친다.

    한 역할이 기술 단위로 쪼개져 **동시에** 돌아오므로 같은 키에 여러 번 쓰인다.
    덮어쓰면 먼저 도착한 기술의 조사 결과가 사라지고, 이어 붙이면 재작업한 항목의 옛
    판정이 함께 남는다. 교체 범위를 (기술, 항목)으로 한정하는 규칙은 agents/merge.py 에 있다.
    """
    merged = dict(current or {})
    for role, run in (update or {}).items():
        merged[role] = merge_runs(merged.get(role), run)
    return merged


def merge_control(current: dict[str, RoleControl] | None, update: dict[str, RoleControl] | None):
    return {**(current or {}), **(update or {})}


def merge_sources(current: list[Evidence] | None, update: list[Evidence] | None):
    """ID 단위 병합. 단순 concat 은 중복 제거가 아니고, 같은 ID 의 상충은 오류로 드러나야 한다."""
    return merge_evidence(list(current or []), list(update or []))


def keep_error(current: str | None, update: str | None):
    """마지막으로 보고된 오류를 남긴다.

    한 역할이 기술별로 쪼개져 동시에 돌므로 같은 스텝에 여러 조각이 실패할 수 있다.
    reducer 가 없으면 LangGraph 가 동시 쓰기로 보고 실행 전체를 중단시킨다 — 조사
    에이전트 하나가 실패해도 나머지 근거로 보고서까지 간다는 fall-back 과 어긋난다.
    역할별 오류는 control[role].last_error 에 따로 남으므로 여기서는 최근 것만 든다.
    """
    return update or current


def append_decisions(current: list[Decision] | None, update: list[Decision] | None):
    """누적하되 윈도우로 자른다 (지속성 비용). 전체 이력은 decisions.jsonl 에 있다."""
    return [*(current or []), *(update or [])][-DECISION_WINDOW:]


class SupervisorState(TypedDict, total=False):
    # ── 작업 페이로드 ────────────────────────────────────────────────
    target_techs: dict[str, str]
    domain: str
    results: Annotated[dict[str, NodeRun], merge_results]
    sources: Annotated[list[Evidence], merge_sources]
    trl_result: dict
    report_text: str
    report_path: str
    submission_path: str  # 10장 한도에 맞춘 제출본
    report_check: dict
    quality: QualityVerdict | None

    # ── 제어 메타데이터 ──────────────────────────────────────────────
    trace_id: str  # ★ 외부 로그·LangSmith 와 State 를 잇는 상관 키
    run_id: str
    step: int  # 종료 가드 (Supervisor 분기 횟수)
    max_steps: int
    control: Annotated[dict[str, RoleControl], merge_control]
    route: list[WorkItem]  # 이번 스텝의 배정 목록. 개수가 실행마다 달라진다
    assignment: WorkItem | None  # Send 로 하위 에이전트에게 실려 가는 자기 배정
    decisions: Annotated[list[Decision], append_decisions]
    revision_round: int  # 근거 부족에 따른 재작업 라운드
    max_revisions: int
    quality_round: int  # 품질 미달에 따른 보고서 재작성 라운드
    max_quality_rounds: int
    last_error: Annotated[str | None, keep_error]
    deadline_at: float | None  # 벽시계 예산 소진 시각 (epoch). 재작업만 중단시킨다.
    run_status: str
    stop_reason: str


# 스칼라 제어 필드(step·route·라운드 수)는 Supervisor 만 쓰므로 reducer 없이 마지막 쓰기가
# 이긴다. 병렬로 도는 하위 에이전트는 이 필드들을 건드리지 않는다.

"""관측성 계층: Supervisor 의 결정 로그를 State 밖으로 뺀다 (가이드 C "관측성 위치").

State 에 남는 것은 최근 ``DECISION_WINDOW`` 건의 요약뿐이고, 사유를 포함한 전체 결정
이력은 여기서 ``<output_dir>/decisions.jsonl`` 에 한 줄씩 적재한다. 두 곳은 ``trace_id``
와 ``run_id`` 로 상관시킨다 (가이드 C "상관").

한 줄의 형태::

    {"trace_id": ..., "run_id": ..., "step": 3, "node": "supervisor",
     "action": "rework", "targets": ["market"], "reason": "...", "ts": "..."}

LangSmith 를 켜면 같은 ``trace_id`` 가 실행 메타데이터에도 실리므로, 트레이스 한 건과
이 파일의 몇 번째 줄이 같은 분기인지 맞출 수 있다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from agents.state import Decision

logger = logging.getLogger("supervisor.decisions")


class DecisionLog:
    """결정 로그 적재기. 파일 경로가 없으면 표준 로거로만 남긴다."""

    def __init__(self, trace_id: str, run_id: str, output_dir: str | Path | None = None):
        self.trace_id = trace_id
        self.run_id = run_id
        self.path = Path(output_dir) / "decisions.jsonl" if output_dir else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        step: int,
        action: str,
        targets: list[str],
        reason: str,
        node: str = "supervisor",
        extra: dict | None = None,
    ) -> Decision:
        """결정 하나를 외부에 적재하고, State 에 담을 요약을 돌려준다."""
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        line = {
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "step": step,
            "node": node,
            "action": action,
            "targets": targets,
            "reason": reason,
            "ts": at,
            **(extra or {}),
        }
        logger.info("%s", json.dumps(line, ensure_ascii=False))
        if self.path:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(line, ensure_ascii=False) + "\n")
        return Decision(step=step, action=action, targets=targets, reason=reason, at=at)


def log_from_config(state, config) -> DecisionLog:
    """노드 안에서 실행 설정(configurable.output_dir)으로 로거를 만든다."""
    folder = (config or {}).get("configurable", {}).get("output_dir")
    return DecisionLog(state.get("trace_id", "unknown"), state.get("run_id", "unknown"), folder)

"""Append-only JSONL progress log so a real run can be tailed while it is still executing.

One line per pipeline/node-graph event. Written best-effort: a logging failure must never
break the run it is describing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class ProgressLog:
    """No-op when constructed without an output directory (e.g. isolated node tests)."""

    def __init__(self, output_dir: str | Path | None):
        self.path = Path(output_dir) / "progress.jsonl" if output_dir else None

    def emit(self, event: str, **fields) -> None:
        if self.path is None:
            return
        record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

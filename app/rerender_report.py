"""저장된 실행 상태에서 보고서만 다시 만든다.

보고서 목차·표·문구를 고칠 때마다 전체 live 실행(20~40분, API 비용)을 반복할 수는 없다.
이 명령은 모델을 호출하지 않고 `state.json` 의 노드 결과만 다시 조판한다. 평가 내용은
그대로이므로 새 조사 결과가 아니라 같은 결과의 다른 조판이다.

    uv run python -m app.rerender_report --from outputs/local/<실행 디렉터리>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from graph.main_graph import RESULT_KEYS
from runtime.reporting import assemble_report, validate_report, write_report
from schemas.contracts import Evidence, Gap, NodeRun


def load_state(folder: Path) -> tuple[dict, str]:
    raw = json.loads((folder / "state.json").read_text(encoding="utf-8"))
    mode = json.loads((folder / "run.json").read_text(encoding="utf-8"))["mode"]
    state = dict(raw)
    for key in RESULT_KEYS.values():
        if key in state:
            state[key] = NodeRun.model_validate(state[key])
    state["gaps"] = [Gap.model_validate(g) for g in raw.get("gaps", [])]
    state["sources"] = [Evidence.model_validate(e) for e in raw.get("sources", [])]
    return state, mode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="folder", type=Path, required=True)
    parser.add_argument("--out", type=Path, help="기본값: 같은 디렉터리에 덮어쓰기")
    args = parser.parse_args(argv)
    state, mode = load_state(args.folder)
    text = assemble_report(state, RESULT_KEYS, mode)
    validation = validate_report(state, RESULT_KEYS, text, mode)
    target = args.out or args.folder
    target.mkdir(parents=True, exist_ok=True)
    path = write_report(text, target)
    print(f"Report Markdown: {target / 'report.md'}\nReport PDF: {path}")
    print(f"모드: {mode} (모델 호출 없이 재조판) / 제출 준비: {validation['ready']}")
    for problem in validation["problems"]:
        print(f"  - {problem}")
    return 0 if validation["ready"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, FileNotFoundError, KeyError) as exc:
        print(f"Setup error: {exc}")
        raise SystemExit(1) from None

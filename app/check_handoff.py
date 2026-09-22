import argparse
import json
from pathlib import Path

from runtime.handoff import check_handoff
from runtime.runner import load_input
from schemas.contracts import NODES, NodeRun


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check node handoff coverage, references and placeholders"
    )
    parser.add_argument("--node", required=True, choices=NODES)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--input", type=Path, help="Defaults to the node's acceptance case")
    args = parser.parse_args(argv)
    data = load_input(args.node, "acceptance", args.input)
    run = NodeRun.model_validate_json(args.result.read_text(encoding="utf-8"))
    if run.node != args.node:
        raise ValueError("Result belongs to a different node")
    report = check_handoff(data, run)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, FileNotFoundError) as exc:
        print(f"Setup error: {exc}")
        raise SystemExit(1) from None

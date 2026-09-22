from __future__ import annotations

import argparse

from graph.main_graph import RESULT_KEYS, build_main_graph
from rag.interface import live_sources
from runtime.models import MockBackend, OpenAIBackend
from runtime.runner import execute, load_input, save_json, show_run
from runtime.settings import load_settings
from schemas.contracts import NODES, Evidence


def format_reference(e: Evidence) -> str:
    """설계서 표 18 REFERENCE 표기. 없는 서지 필드는 만들지 않고 '미상'으로 남긴다."""
    if e.source_type == "paper":
        page = f", p.{e.page}" if e.page else ""
        return f"{e.authors or '저자 미상'}({e.year or '연도 미상'}). {e.title}. {e.venue or '게재 정보 미상'}{page}."
    who = e.publisher or e.authors or "작성자 미상"
    return f"{who}({e.published_at or '날짜 미상'}). {e.title}. {e.site or '사이트 미상'}, {e.url}"


def write_report(output, final) -> None:
    """개발용 report.md: 노드 요약 + 실제 인용된 출처만으로 만든 REFERENCE + 인용 검사 결과."""
    seen, refs = set(), []
    for e in final.get("sources", []):
        if e.id not in seen:
            seen.add(e.id)
            refs.append(e)
    body = [
        "# Report (development output)",
        "",
        f"Status: {final['run_status']}  |  {final.get('report_path', '')}",
        "",
        "개발용 결과입니다. 과제 제출용 최종 평가 보고서(PDF)가 아닙니다.",
        "",
    ]
    for node, key in RESULT_KEYS.items():
        run = final[key]
        body.extend([f"## {node}", "", run.result.summary, ""])
        body.extend(f"- 확인 필요: {s}" for s in run.result.unverified + run.validation_errors)
        body.append("")
    trl = final.get("trl_result", {})
    if trl:
        body.extend(["## TRL (공개 정보 기반 추정)", ""])
        for tech, item in trl.items():
            t, f = item.get("tentative"), item.get("final")
            body.append(
                f"- {tech}: 잠정 {t['judgment'] if t else '미판정'} / 확정 {f['judgment'] if f else '미판정'}"
            )
        body.append("")
    gaps = final.get("gaps", [])
    body.extend(
        ["## 한계 (gaps)", ""]
        + [f"- [{g['role']}] {g['item']} — {g['reason']}" for g in gaps]
        + [""]
    )
    body.extend(["## REFERENCE", ""] + [f"- {format_reference(e)}" for e in refs] + [""])
    (output / "report.md").write_text("\n".join(body), encoding="utf-8")
    save_json(output / "references.json", refs)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the common graph with fixed evidence")
    parser.add_argument("--mode", choices=("mock", "fixture", "live"), default="mock")
    args = parser.parse_args(argv)
    settings = load_settings()
    inputs = {node: load_input(node) for node in NODES}
    backend = MockBackend() if args.mode == "mock" else OpenAIBackend(settings)
    sources = live_sources(settings) if args.mode == "live" else None
    graph = build_main_graph(inputs, args.mode, settings, backend, sources)
    final, output = execute(graph, "pipeline", args.mode, {"settings": settings.model_dump()})
    save_json(output / "state.json", final)
    body = [
        f"# Development preview ({args.mode})",
        "",
        f"Status: {final['run_status']}",
        "",
        "개발용 노드 결과입니다. 과제 제출용 최종 평가 보고서가 아닙니다.",
        "",
    ]
    for node, key in RESULT_KEYS.items():
        run = final[key]
        body.extend([f"## {node}", "", run.result.summary, ""])
        body.extend(f"- 확인 필요: {s}" for s in run.result.unverified + run.validation_errors)
        body.append("")
    (output / "preview.md").write_text("\n".join(body), encoding="utf-8")
    write_report(output, final)
    return show_run(output, final["run_status"])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, FileNotFoundError, ImportError) as exc:
        print(f"Setup error: {exc}")
        raise SystemExit(1) from None

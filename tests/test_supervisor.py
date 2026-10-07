"""Supervisor 패턴의 필수 항목을 코드로 고정한다 (과제 가이드 B·C·D).

여기서 보장하는 것:
  * 하위 에이전트 간 직접 간선이 없다 (통신 제약)
  * 라우팅이 State 에 따라 달라진다 (순서 하드코딩 금지)
  * 근거가 충분해지기 전에는 보고서로 넘어가지 않는다 (스텝 수 고정 금지)
  * 근거 부족이면 해당 에이전트에게만 재작업을 지시한다
  * 보고서 뒤 품질 평가가 돌고, 미달이면 루프한다
  * 어떤 State 에서도 유한 스텝 안에 끝난다 (종료 보장)
"""

from copy import deepcopy

import pytest

from agents.quality import (
    PERSPECTIVES,
    _remediation,
    check_bias_control,
    check_coverage,
    check_groundedness,
    check_neutrality,
)
from agents.state import (
    DECISION_WINDOW,
    Decision,
    RoleControl,
    SupervisorState,
    append_decisions,
    merge_control,
    merge_results,
    merge_sources,
)
from agents.sufficiency import assess, rework_feedback
from agents.supervisor import PIPELINE_ROLES, ROUTES, decide, initial_control, route
from graph.supervisor_graph import WORKER_ROLES, build_supervisor_graph
from runtime.models import MockBackend
from runtime.runner import load_input
from runtime.settings import load_settings
from schemas.contracts import NODES, Evidence, QualityVerdict


def settings():
    return load_settings()


def base_state(**overrides):
    """모든 역할이 아직 돌지 않은 초기 제어 상태."""
    state = {
        "control": initial_control(),
        "step": 0,
        "max_steps": 24,
        "revision_round": 0,
        "max_revisions": 2,
        "quality_round": 0,
        "max_quality_rounds": 1,
        "quality": None,
        "deadline_at": None,
    }
    state.update(overrides)
    return state


def done(role, *, sufficient=True, attempts=1, stalled=False, status="completed"):
    return RoleControl(
        role=role,
        status=status,
        attempts=attempts,
        sufficiency=0.9 if sufficient else 0.4,
        sufficient=sufficient,
        stalled=stalled,
        evidence_count=5,
    )


def all_research_done(**kw):
    control = initial_control()
    for role in PIPELINE_ROLES[:4]:
        control[role] = done(role, **kw)
    return control


# ── 통신 제약: 하위 에이전트끼리 잇는 간선이 없다 ───────────────────────────────


def build(mode="mock", **kwargs):
    inputs = {node: load_input(node, "acceptance") for node in NODES}
    return build_supervisor_graph(inputs, mode, settings(), MockBackend(), **kwargs)


def test_sub_agents_only_talk_to_the_supervisor():
    graph = build().get_graph()
    agents = set(WORKER_ROLES) | {"quality"}
    for edge in graph.edges:
        if edge.source in agents:
            assert edge.target == "supervisor", f"{edge.source}→{edge.target} 는 금지된 직접 통신"
        if edge.target in agents:
            assert edge.source == "supervisor", f"{edge.source}→{edge.target} 는 금지된 직접 통신"


def test_every_route_target_is_a_real_node():
    nodes = set(build().get_graph().nodes)
    assert set(ROUTES) <= nodes


# ── 동적 라우팅: 같은 그래프가 State 에 따라 다른 곳으로 간다 ─────────────────────


def test_routing_depends_on_state_not_on_a_fixed_order():
    policy = settings().supervisor
    seen = set()
    # 1) 아무것도 없을 때 → 기술 조사
    action, targets, _ = decide(base_state(), policy)
    seen.add((action, tuple(targets)))
    assert targets == ["tech"]

    # 2) 기술 조사가 충분하면 → 세 관점을 한 번에 (병렬)
    control = initial_control()
    control["tech"] = done("tech")
    action, targets, _ = decide(base_state(control=control), policy)
    seen.add((action, tuple(targets)))
    assert set(targets) == {"market", "stakeholder", "domain"}

    # 3) 네 관점이 모두 충분하면 → 종합
    action, targets, _ = decide(base_state(control=all_research_done()), policy)
    seen.add((action, tuple(targets)))
    assert targets == ["synthesis"]

    # 같은 decide() 가 State 에 따라 세 가지 다른 분기를 냈다.
    assert len(seen) == 3


def test_router_returns_a_list_so_independent_agents_run_in_parallel():
    assert route({"route": ["market", "stakeholder", "domain"]}) == [
        "market",
        "stakeholder",
        "domain",
    ]
    # 알 수 없는 대상은 버리고 반드시 유효한 노드로 간다.
    assert route({"route": ["nonexistent"]}) == ["finalize"]
    assert route({}) == ["finalize"]


# ── 근거 충분성: 부족하면 그 에이전트에게만 재작업 ──────────────────────────────


def test_insufficient_evidence_reworks_only_the_owning_agent():
    policy = settings().supervisor
    control = all_research_done()
    control["market"] = done("market", sufficient=False)
    action, targets, reason = decide(base_state(control=control), policy)
    assert action == "rework"
    assert targets == ["market"], "근거가 부족한 역할만 재작업해야 한다"
    assert "근거 부족" in reason


def test_report_waits_for_the_sufficiency_verdict():
    """충분성 판정이 끝나기 전에는 어떤 경로로도 report 로 가지 않는다."""
    policy = settings().supervisor
    control = initial_control()
    control["tech"] = done("tech")
    for _ in range(10):
        action, targets, _ = decide(base_state(control=control), policy)
        assert targets != ["report"]
        if targets == ["synthesis"]:
            break
        for role in targets:
            if role in control:
                control[role] = done(role)
    assert control["synthesis"].status == "pending"


def test_rework_feedback_names_the_missing_items():
    control = done("market", sufficient=False)
    control = control.model_copy(update={"open_gaps": ["KIVI/adoption", "ITME/growth"]})
    message = rework_feedback(control)
    assert "KIVI/adoption" in message and "ITME/growth" in message
    assert "추측하지 말고" in message


# ── 종료 보장 ──────────────────────────────────────────────────────────────


def test_step_ceiling_forces_finalize():
    policy = settings().supervisor
    action, targets, reason = decide(base_state(step=24, max_steps=24), policy)
    assert (action, targets) == ("finalize", ["finalize"])
    assert "스텝 상한" in reason


def test_exhausted_attempts_stop_the_rework_loop():
    policy = settings().supervisor
    control = all_research_done(sufficient=False, attempts=policy.max_attempts)
    action, targets, _ = decide(base_state(control=control), policy)
    assert action == "synthesize", "예산을 소진하면 미해결을 남긴 채 다음 단계로 간다"


def test_revision_round_ceiling_stops_reworking():
    policy = settings().supervisor
    control = all_research_done(sufficient=False)
    state = base_state(control=control, revision_round=2, max_revisions=2)
    action, _, _ = decide(state, policy)
    assert action == "synthesize"


def test_no_new_evidence_stops_further_rework():
    """수확 체감: 재작업해도 근거가 안 늘면 같은 질의로 또 돌지 않는다."""
    policy = settings().supervisor
    control = all_research_done()
    control["domain"] = done("domain", sufficient=False, attempts=1, stalled=True)
    action, targets, _ = decide(base_state(control=control), policy)
    assert action == "synthesize", "헛도는 재작업은 하지 않는다"
    assert "근거 증가 없음" in decide(base_state(control=control), policy)[2]


def test_time_budget_stops_rework_but_still_produces_a_report():
    policy = settings().supervisor
    control = all_research_done(sufficient=False)
    state = base_state(control=control, deadline_at=1000.0)
    # 예산 안: 재작업한다.
    assert decide(state, policy, now=999.0)[0] == "rework"
    # 예산 밖: 재작업을 멈추고 산출물 경로로 간다.
    action, targets, reason = decide(state, policy, now=1001.0)
    assert action == "synthesize"
    assert "시간 예산 소진" in reason


def test_mock_run_terminates_and_writes_a_report(tmp_path):
    graph = build()
    final = graph.invoke(
        {},
        config={"recursion_limit": 120, "configurable": {"output_dir": str(tmp_path)}},
    )
    assert final["step"] <= final["max_steps"], "스텝 상한 안에서 끝나야 한다"
    assert (tmp_path / "report.md").exists()
    assert (tmp_path / "decisions.jsonl").exists(), "결정 로그는 State 밖 외부 파일로 남는다"
    assert (tmp_path / "supervisor.json").exists(), "재개용 최소 상태가 남아야 한다"
    assert set(final["results"]) == set(NODES), "여섯 역할의 결과가 모두 모여야 한다"
    assert final["quality"] is not None, "보고서 뒤 품질 평가가 반드시 돈다"


def test_mock_run_actually_reworks_and_runs_agents_in_parallel(tmp_path):
    final = build().invoke(
        {},
        config={"recursion_limit": 120, "configurable": {"output_dir": str(tmp_path)}},
    )
    decisions = final["decisions"]
    assert any(d.action == "rework" for d in decisions), "동적 재작업이 실제로 일어나야 한다"
    assert any(len(d.targets) > 1 for d in decisions), "독립 관점은 한 스텝에 병렬 배정된다"
    # 결정마다 사유가 있어야 관측성이 성립한다.
    assert all(d.reason.strip() for d in decisions)


# ── 품질 평가 노드와 루프 ────────────────────────────────────────────────────


def test_quality_failure_routes_back_to_the_responsible_agents():
    policy = settings().supervisor
    control = all_research_done()
    control["synthesis"] = done("synthesis")
    control["report"] = done("report")
    verdict = QualityVerdict(
        passed=False,
        checks=[
            {"criterion": "neutrality", "passed": False, "reason": "우열 표현", "source": "rule"}
        ],
        remediation_roles=["report"],
    )
    action, targets, reason = decide(base_state(control=control, quality=verdict), policy)
    assert action == "rework"
    assert targets == ["report"], "서술 문제는 보고서 에이전트에게 돌린다"
    assert "품질 미달" in reason


def test_quality_pass_finalizes():
    policy = settings().supervisor
    control = all_research_done()
    control["synthesis"] = done("synthesis")
    control["report"] = done("report")
    verdict = QualityVerdict(passed=True, checks=[])
    action, targets, _ = decide(base_state(control=control, quality=verdict), policy)
    assert (action, targets) == ("finalize", ["finalize"])


def test_quality_round_ceiling_ends_the_loop():
    policy = settings().supervisor
    control = all_research_done()
    control["synthesis"] = done("synthesis")
    control["report"] = done("report")
    verdict = QualityVerdict(
        passed=False,
        checks=[{"criterion": "neutrality", "passed": False, "reason": "x", "source": "rule"}],
        remediation_roles=["report"],
    )
    state = base_state(control=control, quality=verdict, quality_round=1, max_quality_rounds=1)
    action, _, reason = decide(state, policy)
    assert action == "finalize"
    assert "한도 소진" in reason


def test_quality_evaluates_all_four_required_criteria(tmp_path):
    final = build().invoke(
        {},
        config={"recursion_limit": 120, "configurable": {"output_dir": str(tmp_path)}},
    )
    assert {c.criterion for c in final["quality"].checks} == {
        "groundedness",
        "neutrality",
        "bias_control",
        "coverage",
    }


def test_mock_reports_that_the_llm_judge_did_not_run(tmp_path):
    """mock 은 Judge 를 쓸 수 없다. '검사 안 함'과 '통과'를 구분해야 한다."""
    final = build().invoke(
        {},
        config={"recursion_limit": 120, "configurable": {"output_dir": str(tmp_path)}},
    )
    assert final["quality"].judge_available is False


def test_quality_failure_is_not_reported_as_completed(tmp_path):
    final = build().invoke(
        {},
        config={"recursion_limit": 120, "configurable": {"output_dir": str(tmp_path)}},
    )
    if not final["quality"].passed:
        assert final["run_status"] != "completed"
        assert any("품질 미달" in p for p in final["report_check"]["problems"])


def test_remediation_maps_each_criterion_to_an_owner():
    coverage_only = QualityVerdict(
        passed=False,
        checks=[{"criterion": "coverage", "passed": False, "reason": "x", "source": "rule"}],
    )
    assert set(_remediation(coverage_only)) == {role for role, _ in PERSPECTIVES.values()}

    neutrality_only = QualityVerdict(
        passed=False,
        checks=[{"criterion": "neutrality", "passed": False, "reason": "x", "source": "rule"}],
    )
    assert _remediation(neutrality_only) == ["report"]


def test_neutrality_rule_catches_a_ranking_sentence():
    class FakeRun:
        class result:
            summary = "KIVI가 ITME보다 더 우수하다."
            claims = []

    verdict = check_neutrality({"results": {"report": FakeRun}}, "본문")
    assert verdict.passed is False
    assert "우수하다" in verdict.reason


# ── State 스키마: 제어/페이로드 분리, reducer, 상관, 재개 ───────────────────────


def test_state_separates_control_metadata_from_payload():
    keys = SupervisorState.__annotations__.keys()
    payload = {"results", "sources", "trl_result", "report_text", "quality"}
    control = {"trace_id", "run_id", "step", "max_steps", "control", "route", "decisions"}
    assert payload <= keys and control <= keys


def test_routing_reads_only_control_fields():
    """라우터는 페이로드(NodeRun 본문) 없이도 분기할 수 있어야 한다."""
    policy = settings().supervisor
    state = base_state()  # results/sources 가 아예 없다
    action, targets, _ = decide(state, policy)
    assert targets == ["tech"]


def test_reducers_merge_concurrent_writes():
    # 병렬 하위 에이전트가 같은 필드에 동시에 쓴다.
    assert merge_results({"tech": 1}, {"market": 2}) == {"tech": 1, "market": 2}
    assert set(merge_control({"tech": 1}, {"market": 2})) == {"tech", "market"}

    def ev(eid, tech):
        return Evidence(
            id=eid,
            text="t",
            title="T",
            url="https://example.com/a",
            technology=tech,
            source_type="web",
            scope="target",
        )

    # 같은 ID 는 한 번만 남는다. concat 은 중복 제거가 아니다.
    merged = merge_sources([ev("a", "KIVI")], [ev("a", "KIVI"), ev("b", "KIVI")])
    assert sorted(e.id for e in merged) == ["a", "b"]


def test_conflicting_evidence_for_one_id_is_rejected():
    def ev(eid, text):
        return Evidence(
            id=eid,
            text=text,
            title="T",
            url="https://example.com/a",
            technology="KIVI",
            source_type="web",
            scope="target",
        )

    with pytest.raises(ValueError):
        merge_sources([ev("a", "원문 1")], [ev("a", "다른 원문")])


def test_decision_log_in_state_is_bounded():
    """지속성 비용: 체크포인트마다 저장되는 결정 요약이 무한 증식하지 않는다."""
    many = [
        Decision(step=i, action="dispatch", targets=["tech"], reason="r", at="2026-01-01T00:00:00")
        for i in range(DECISION_WINDOW * 3)
    ]
    assert len(append_decisions([], many)) == DECISION_WINDOW
    assert append_decisions([], many)[-1].step == DECISION_WINDOW * 3 - 1


def test_trace_id_correlates_state_with_the_external_log(tmp_path):
    import json

    final = build(trace_id="fixed-trace-id").invoke(
        {},
        config={"recursion_limit": 120, "configurable": {"output_dir": str(tmp_path)}},
    )
    assert final["trace_id"] == "fixed-trace-id"
    lines = [
        json.loads(line)
        for line in (tmp_path / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert lines and all(entry["trace_id"] == "fixed-trace-id" for entry in lines)
    assert all({"step", "action", "reason", "ts"} <= entry.keys() for entry in lines)


def test_resume_state_records_status_attempts_and_errors(tmp_path):
    import json

    build().invoke(
        {},
        config={"recursion_limit": 120, "configurable": {"output_dir": str(tmp_path)}},
    )
    digest = json.loads((tmp_path / "supervisor.json").read_text(encoding="utf-8"))
    assert digest["trace_id"] and digest["stop_reason"]
    for role, control in digest["control"].items():
        assert {"status", "attempts", "last_error", "sufficiency"} <= control.keys()


def test_large_payloads_live_on_disk_not_in_the_decision_log(tmp_path):
    """지속성 비용: 근거 원문은 nodes/*.json 에 있고 결정 로그에는 없다."""
    build().invoke(
        {},
        config={"recursion_limit": 120, "configurable": {"output_dir": str(tmp_path)}},
    )
    assert (tmp_path / "nodes" / "tech.json").exists()
    log = (tmp_path / "decisions.jsonl").read_text(encoding="utf-8")
    # 결정 로그에는 사유·대상만 있고 근거 원문이 들어가면 안 된다.
    for excerpt in load_input("tech", "acceptance").evidence:
        assert excerpt.text[:80] not in log, "근거 원문은 결정 로그에 복사되지 않는다"


# ── 하위 에이전트 실패 시 Fall-back ──────────────────────────────────────────


def test_a_failing_sub_agent_does_not_stop_the_run(tmp_path, monkeypatch):
    """한 에이전트가 터져도 나머지 근거로 보고서까지 간다."""
    import graph.node_graph as node_graph

    original = node_graph.build_node_graph

    def explode(node, *args, **kwargs):
        if node == "stakeholder":
            raise RuntimeError("조사 중 실패")
        return original(node, *args, **kwargs)

    monkeypatch.setattr("agents.workers.build_node_graph", explode)
    final = build().invoke(
        {},
        config={"recursion_limit": 120, "configurable": {"output_dir": str(tmp_path)}},
    )
    assert final["control"]["stakeholder"].status in ("failed", "skipped")
    assert final["control"]["stakeholder"].last_error
    assert (tmp_path / "report.md").exists(), "실패한 역할이 있어도 산출물은 나와야 한다"
    assert final["run_status"] in ("failed", "needs_revision")


def test_sufficiency_score_rises_with_resolved_items():
    config = settings()
    data = load_input("market", "acceptance")
    graph = build()
    final = graph.invoke({}, config={"recursion_limit": 120})
    run = deepcopy(final["results"]["market"])
    low = assess("market", run, data, config)
    # mock 은 모든 항목을 확인 불가로 두므로 충분하지 않아야 한다.
    assert low.sufficient is False
    assert low.open_gaps

    for item in run.result.assessments:
        item.judgment = "보통"
    high = assess("market", run, data, config)
    assert high.sufficiency > low.sufficiency


# ── 품질 규칙 각각 ──────────────────────────────────────────────────────────


def test_quality_rules_run_against_a_real_mock_report(tmp_path):
    from agents.report_view import RESULT_KEYS, flat_state
    from runtime.reporting import assemble_report, report_sources

    final = build().invoke(
        {},
        config={"recursion_limit": 120, "configurable": {"output_dir": str(tmp_path)}},
    )
    view = flat_state(final, settings())
    text = assemble_report(view, RESULT_KEYS, "mock")
    sources = report_sources(view, RESULT_KEYS)

    assert check_groundedness(final, settings(), sources).passed, "mock 인용은 모두 추적 가능"
    assert check_coverage(final, text).passed, "네 관점 절이 모두 구성되어야 한다"
    # 고정 발췌는 기술당 한 건뿐이라 편향 통제는 통과하지 못한다 — 그 사실이 드러나야 한다.
    bias = check_bias_control(final, settings(), sources)
    assert bias.passed is False and "출처" in bias.reason

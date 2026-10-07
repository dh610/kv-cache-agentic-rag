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
    RESEARCH_OWNERS,
    _remediation,
    check_bias_control,
    check_coverage,
    check_groundedness,
    check_neutrality,
)
from agents.state import (
    DECISION_WINDOW,
    Decision,
    OpenItem,
    RoleControl,
    SupervisorState,
    WorkItem,
    append_decisions,
    merge_control,
    merge_results,
    merge_sources,
)
from agents.sufficiency import assess, rework_feedback
from agents.supervisor import (
    PIPELINE_ROLES,
    ROUTES,
    decide,
    initial_control,
    make_supervisor,
    plan_items,
    route,
)
from graph.supervisor_graph import WORKER_ROLES, build_supervisor_graph
from runtime.models import MockBackend
from runtime.runner import load_input
from runtime.settings import load_settings
from schemas.contracts import NODES, Evidence, QualityVerdict


def settings():
    return load_settings()


def roles(items):
    """배정 목록에서 역할만 뽑는다 (순서 무관 비교용)."""
    return sorted({item.role for item in items})


def base_state(**overrides):
    """모든 역할이 아직 돌지 않은 초기 제어 상태."""
    state = {
        "target_techs": {"sw": "KIVI", "hw": "ITME"},
        "domain": "데이터센터·클라우드 LLM 서빙",
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


def node_inputs():
    return {node: load_input(node, "acceptance") for node in NODES}


def build(mode="mock", **kwargs):
    return build_supervisor_graph(node_inputs(), mode, settings(), MockBackend(), **kwargs)


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
    action, items, _ = decide(base_state(), policy)
    seen.add((action, tuple(roles(items))))
    assert roles(items) == ["tech"]
    # 기술마다 배정이 하나씩 나간다 — fan-out 개수가 코드에 고정돼 있지 않다.
    assert sorted(i.technologies[0] for i in items) == ["ITME", "KIVI"]

    # 2) 기술 조사가 충분하면 → 세 관점을 한 번에 (병렬)
    control = initial_control()
    control["tech"] = done("tech")
    action, items, _ = decide(base_state(control=control), policy)
    seen.add((action, tuple(roles(items))))
    assert set(roles(items)) == {"market", "stakeholder", "domain"}
    assert len(items) == 6, "세 관점 × 두 기술이 한 스텝에 함께 나간다"

    # 3) 네 관점이 모두 충분하면 → 종합
    action, items, _ = decide(base_state(control=all_research_done()), policy)
    seen.add((action, tuple(roles(items))))
    assert roles(items) == ["synthesis"]

    # 같은 decide() 가 State 에 따라 세 가지 다른 분기를 냈다.
    assert len(seen) == 3


def test_router_sends_one_assignment_per_work_item():
    """Send 하나가 배정 하나를 들고 간다. 개수는 Supervisor 가 실행 중에 정한다."""
    from langgraph.types import Send

    items = [
        WorkItem(role="market", technologies=["KIVI"]),
        WorkItem(role="market", technologies=["ITME"]),
        WorkItem(role="domain", technologies=["KIVI"]),
    ]
    sends = route({**base_state(), "route": items})
    assert all(isinstance(s, Send) for s in sends)
    assert [s.node for s in sends] == ["market", "market", "domain"]
    # 배정 내용이 분기 자체에 실려 간다 — 워커가 공유 State 를 뒤지지 않는다.
    assert [s.arg["assignment"].technologies for s in sends] == [["KIVI"], ["ITME"], ["KIVI"]]
    assert route({**base_state(), "route": []}) == ["finalize"]
    assert route({**base_state(), "route": [WorkItem(role="finalize")]}) == ["finalize"]


def test_fan_out_width_is_decided_at_runtime_not_in_code():
    """같은 역할이라도 부족한 항목 수에 따라 배정 개수가 달라진다."""
    control = done("market", sufficient=False)
    wide = plan_items("market", control, ["KIVI", "ITME"], "r", first=True)
    assert len(wide) == 2

    narrow_control = control.model_copy(
        update={"open_items": [OpenItem(technology="ITME", criterion="adoption")]}
    )
    narrow = plan_items("market", narrow_control, ["KIVI", "ITME"], "r", first=False)
    assert len(narrow) == 1, "부족한 기술만 다시 본다"
    assert narrow[0].technologies == ["ITME"] and narrow[0].criteria == ["adoption"]


# ── 근거 충분성: 부족하면 그 에이전트에게만 재작업 ──────────────────────────────


def test_insufficient_evidence_reworks_only_the_owning_agent():
    policy = settings().supervisor
    control = all_research_done()
    control["market"] = done("market", sufficient=False)
    action, items, reason = decide(base_state(control=control), policy)
    assert action == "rework"
    assert roles(items) == ["market"], "근거가 부족한 역할만 재작업해야 한다"
    assert "근거 부족" in reason


def test_report_waits_for_the_sufficiency_verdict():
    """충분성 판정이 끝나기 전에는 어떤 경로로도 report 로 가지 않는다."""
    policy = settings().supervisor
    control = initial_control()
    control["tech"] = done("tech")
    for _ in range(10):
        action, items, _ = decide(base_state(control=control), policy)
        assert roles(items) != ["report"]
        if roles(items) == ["synthesis"]:
            break
        for role in roles(items):
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
    action, items, reason = decide(base_state(step=24, max_steps=24), policy)
    assert (action, roles(items)) == ("finalize", ["finalize"])
    assert "스텝 상한" in reason


def test_exhausted_attempts_stop_the_rework_loop():
    policy = settings().supervisor
    control = all_research_done(sufficient=False, attempts=policy.max_attempts)
    action, _, _ = decide(base_state(control=control), policy)
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
    action, _, _ = decide(base_state(control=control), policy)
    assert action == "synthesize", "헛도는 재작업은 하지 않는다"
    assert "근거 증가 없음" in decide(base_state(control=control), policy)[2]


def test_time_budget_stops_rework_but_still_produces_a_report():
    policy = settings().supervisor
    control = all_research_done(sufficient=False)
    state = base_state(control=control, deadline_at=1000.0)
    # 예산 안: 재작업한다.
    assert decide(state, policy, now=999.0)[0] == "rework"
    # 예산 밖: 재작업을 멈추고 산출물 경로로 간다.
    action, _, reason = decide(state, policy, now=1001.0)
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
    action, items, reason = decide(base_state(control=control, quality=verdict), policy)
    assert action == "rework"
    assert roles(items) == ["report"], "서술 문제는 보고서 에이전트에게 돌린다"
    assert "품질 미달" in reason


def test_quality_pass_finalizes():
    policy = settings().supervisor
    control = all_research_done()
    control["synthesis"] = done("synthesis")
    control["report"] = done("report")
    verdict = QualityVerdict(passed=True, checks=[])
    action, items, _ = decide(base_state(control=control, quality=verdict), policy)
    assert (action, roles(items)) == ("finalize", ["finalize"])


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


def test_remediation_follows_whoever_can_actually_fix_it():
    """같은 항목이라도 원인에 따라 고칠 사람이 다르다."""

    def verdict(criterion, owners):
        return QualityVerdict(
            passed=False,
            checks=[
                {
                    "criterion": criterion,
                    "passed": False,
                    "reason": "x",
                    "source": "rule",
                    "owners": owners,
                }
            ],
        )

    # 근거 없이 등급을 쓴 것은 보고서 노드가 고칠 일이다.
    assert _remediation(verdict("groundedness", ["report"])) == ["report"]
    # 인용할 근거 자체가 모자란 것은 조사 에이전트가 고칠 일이다.
    assert _remediation(verdict("bias_control", list(RESEARCH_OWNERS))) == sorted(RESEARCH_OWNERS)
    # 아무도 지목되지 않으면 고칠 수 없는 곳으로 보내지 않는다.
    assert _remediation(verdict("neutrality", [])) == ["report"]


def test_ungrounded_judgements_are_sent_to_the_report_agent_not_the_researchers():
    """live 실행에서 이 오배정으로 재작업 한 라운드를 통째로 버렸다."""
    from schemas.contracts import Assessment, Claim, NodeResult, NodeRun

    run = NodeRun(
        node="report",
        mode="mock",
        status="completed",
        result=NodeResult(
            node="report",
            summary="s",
            claims=[
                Claim(
                    id="c1",
                    technology="KIVI",
                    criterion="implications",
                    text="t",
                    kind="fact",
                    evidence_ids=["e1"],
                    conditions=[],
                )
            ],
            # 근거 없이 등급만 적었다 — 확인 불가로 내렸어야 할 자리.
            assessments=[
                Assessment(
                    technology="KIVI",
                    criterion="implications",
                    judgment="구성 충족",
                    rationale="r",
                    evidence_ids=[],
                )
            ],
            unverified=[],
            limitations=[],
        ),
        evidence=[],
        checks=[],
        validation_errors=[],
        searches=[],
        prompt_hash="h",
        model="m",
    )
    source = Evidence(
        id="e1",
        text="t",
        title="T",
        url="https://example.com/e1",
        technology="KIVI",
        source_type="web",
        scope="target",
    )
    check = check_groundedness({"results": {"report": run}}, settings(), [source])
    assert check.passed is False
    assert check.owners == ["report"], "조사 에이전트를 다시 돌려도 고쳐지지 않는다"


def test_coverage_is_not_second_guessed_by_the_judge():
    """확인 불가는 이 설계의 정상 산출이다. Judge 가 그걸로 커버리지를 깎으면 안 된다."""
    from agents.quality import RULE_ONLY

    assert "coverage" in RULE_ONLY


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
    action, items, _ = decide(state, policy)
    assert roles(items) == ["tech"]


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
    assert list((tmp_path / "nodes").glob("tech.*.json")), "조사 조각이 디스크에 남아야 한다"
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


def test_a_wording_only_fix_does_not_re_run_synthesis():
    """중립성 미달은 보고서 서술 문제다. 입력이 같은 평가 종합을 다시 돌릴 이유가 없다."""
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
    supervise = make_supervisor(settings(), node_inputs())
    delta = supervise(base_state(control=control, quality=verdict), {})
    assert roles(delta["route"]) == ["report"]
    assert delta["control"]["report"].status == "pending"
    assert "synthesis" not in delta["control"], "평가 종합은 그대로 두어야 한다"


def test_changed_research_does_re_run_synthesis_and_report():
    """반대로 조사 결과가 바뀌면 그것을 종합한 결과와 보고서는 반드시 다시 만든다."""
    control = all_research_done(sufficient=False)
    control["synthesis"] = done("synthesis")
    control["report"] = done("report")
    supervise = make_supervisor(settings(), node_inputs())
    delta = supervise(base_state(control=control), {})
    assert delta["control"]["synthesis"].status == "pending"
    assert delta["control"]["report"].status == "pending"


def test_rework_gets_a_narrower_search_budget_than_the_first_attempt(monkeypatch):
    """재작업은 '처음부터 다시'가 아니라 '부족한 것만 더'. 예산이 줄어야 한다."""
    from agents.workers import make_worker

    config = settings()
    config.supervisor.rework_search = 1
    config.limits.search = 3
    seen = []

    import agents.workers as workers

    original = workers.build_node_graph

    def record(role, data, mode, used, *args, **kwargs):
        seen.append(used.limits.search)
        return original(role, data, mode, used, *args, **kwargs)

    monkeypatch.setattr(workers, "build_node_graph", record)
    inputs = {node: load_input(node, "acceptance") for node in NODES}
    worker = make_worker("tech", inputs, "mock", config, MockBackend())

    first = worker(base_state(results={}, sources=[]), {})
    assert seen == [3], "첫 실행은 설정된 검색 예산을 그대로 쓴다"

    control = {**initial_control(), "tech": done("tech", attempts=1)}
    worker(base_state(control=control, results=first["results"], sources=[]), {})
    assert seen == [3, 1], "재작업은 축소된 예산을 쓴다"


def test_compact_layout_keeps_required_structure_and_cuts_bulk(tmp_path):
    """제출본은 분량만 줄이고 필수 목차·비교표·판정은 전체본과 같아야 한다."""
    from agents.report_view import RESULT_KEYS, flat_state
    from runtime.reporting import assemble_report

    final = build().invoke(
        {},
        config={"recursion_limit": 120, "configurable": {"output_dir": str(tmp_path)}},
    )
    view = flat_state(final, settings(), "mock")
    full = assemble_report(view, RESULT_KEYS, "mock")
    compact = assemble_report(view, RESULT_KEYS, "mock", compact=True)

    required = [
        "# SUMMARY",
        "# 1. 분석 배경",
        "# 2. 기술 선정",
        "# 3. 기술 개요",
        "# 4. 관점별 평가",
        "# 5. 시사점",
        "# 6. 한계점",
        "# REFERENCE",
    ]
    for heading in required:
        assert heading in compact, f"제출본에서 필수 목차 {heading} 가 빠졌다"
    assert full.count("\n표 ") == compact.count("\n표 "), "비교표는 그대로 남아야 한다"
    assert len(compact) < len(full), "제출본은 전체본보다 짧아야 한다"


def test_compact_reference_lists_only_what_the_text_cites(tmp_path):
    """REFERENCE 절 스스로 '실제로 인용한 자료만'이라고 적고 있다. 압축본은 그 규칙을 지킨다."""
    import re

    from agents.report_view import RESULT_KEYS, flat_state
    from runtime.reporting import assemble_report

    final = build().invoke(
        {},
        config={"recursion_limit": 120, "configurable": {"output_dir": str(tmp_path)}},
    )
    view = flat_state(final, settings(), "mock")
    compact = assemble_report(view, RESULT_KEYS, "mock", compact=True)
    body, reference = compact.split("# REFERENCE")
    listed = {
        int(line.split(".")[0])
        for line in reference.splitlines()
        if line.strip()[:1].isdigit() and ". " in line[:6]
    }
    cited = {int(n) for n in re.findall(r"\[(\d+)(?:\s+p\.[\d,]+)?[;\]]", body)}
    assert cited, "본문에 인용 표기가 있어야 한다"
    assert cited <= listed, "본문이 가리키는 번호가 REFERENCE 에 모두 있어야 한다"
    assert listed <= cited, "본문이 인용하지 않은 자료는 REFERENCE 에 싣지 않는다"


def test_compact_submission_is_written_next_to_the_full_report(tmp_path):
    final = build("fixture").invoke(
        {},
        config={"recursion_limit": 120, "configurable": {"output_dir": str(tmp_path)}},
    )
    assert (tmp_path / "report.pdf").exists()
    assert (tmp_path / "submission" / "report.pdf").exists()
    assert final["submission_path"]


def test_a_run_never_overwrites_the_submission_copy_outside_its_output_dir(tmp_path, monkeypatch):
    """제출본 복사는 고정 경로를 덮어쓴다. 테스트·실험이 실제 산출물 자리를 건드리면 안 된다."""
    from runtime.reporting import write_report
    from runtime.settings import ROOT, load_settings

    monkeypatch.setenv("RUN_OUTPUT_DIR", str(tmp_path))
    meta = load_settings().report
    assert meta.submission, "제출 파일명이 설정되어 있어야 이 보호가 의미를 갖는다"
    before = (ROOT / "outputs" / meta.submission).exists()
    write_report("# SUMMARY\n\n본문\n\n# REFERENCE\n\n없음\n", tmp_path, meta, "fixture")
    assert (tmp_path / meta.submission).exists(), "제출본은 지정된 출력 경로 아래로 간다"
    assert (ROOT / "outputs" / meta.submission).exists() == before


# ── Send 기반 동적 fan-out과 조각 병합 ─────────────────────────────────────


def test_merging_partial_results_replaces_only_what_was_redone():
    """재작업이 ITME 만 돌렸다면 KIVI 판정과 근거는 그대로 남아야 한다."""
    from agents.merge import merge_runs
    from schemas.contracts import Assessment, Claim, NodeResult, NodeRun

    def run(tech, judgment, claim_id, evidence_id):
        return NodeRun(
            node="market",
            mode="mock",
            status="completed",
            result=NodeResult(
                node="market",
                summary=f"{tech} 요약",
                claims=[
                    Claim(
                        id=claim_id,
                        technology=tech,
                        criterion="adoption",
                        text=f"{tech} 주장",
                        kind="fact",
                        evidence_ids=[evidence_id],
                        conditions=[],
                    )
                ],
                assessments=[
                    Assessment(
                        technology=tech,
                        criterion="adoption",
                        judgment=judgment,
                        rationale="r",
                        evidence_ids=[evidence_id],
                    )
                ],
                unverified=[f"{tech}/adoption: 확인 불가"],
                limitations=[],
            ),
            evidence=[
                Evidence(
                    id=evidence_id,
                    text="t",
                    title="T",
                    url=f"https://example.com/{evidence_id}",
                    technology=tech,
                    source_type="web",
                    scope="target",
                )
            ],
            checks=[],
            validation_errors=[],
            searches=[],
            prompt_hash="h",
            model="m",
        )

    merged = merge_runs(run("KIVI", "보통", "c1", "e-kivi"), run("ITME", "낮음", "c2", "e-itme"))
    assert {(a.technology, a.judgment) for a in merged.result.assessments} == {
        ("KIVI", "보통"),
        ("ITME", "낮음"),
    }
    assert {e.id for e in merged.evidence} == {"e-kivi", "e-itme"}

    # ITME 만 다시 돌린다: KIVI 는 보존되고 ITME 만 새 판정으로 바뀐다.
    reworked = merge_runs(merged, run("ITME", "높음", "c3", "e-itme2"))
    judgments = {a.technology: a.judgment for a in reworked.result.assessments}
    assert judgments == {"KIVI": "보통", "ITME": "높음"}
    assert len(reworked.result.assessments) == 2, "같은 항목이 두 번 남으면 안 된다"
    assert {e.id for e in reworked.evidence} == {"e-kivi", "e-itme", "e-itme2"}
    # 해결된 항목의 옛 미확인 기록은 따라가지 않는다.
    assert reworked.result.unverified.count("ITME/adoption: 확인 불가") == 1


def test_merging_renames_colliding_claim_ids_and_follows_the_checks():
    """조각마다 ID 를 따로 만들므로 겹칠 수 있다. 겹치면 검증 연결이 끊기면 안 된다."""
    from agents.merge import merge_runs
    from schemas.contracts import Assessment, Claim, ClaimCheck, NodeResult, NodeRun

    def run(tech, criterion):
        return NodeRun(
            node="market",
            mode="mock",
            status="completed",
            result=NodeResult(
                node="market",
                summary="s",
                claims=[
                    Claim(
                        id="c1",  # 두 조각이 같은 ID 를 만들었다
                        technology=tech,
                        criterion=criterion,
                        text=f"{tech} 주장",
                        kind="fact",
                        evidence_ids=["e1"],
                        conditions=[],
                    )
                ],
                assessments=[
                    Assessment(
                        technology=tech,
                        criterion=criterion,
                        judgment="보통",
                        rationale="r",
                        evidence_ids=["e1"],
                    )
                ],
                unverified=[],
                limitations=[],
            ),
            evidence=[
                Evidence(
                    id="e1",
                    text="t",
                    title="T",
                    url="https://example.com/e1",
                    technology="other",
                    source_type="web",
                    scope="target",
                )
            ],
            checks=[ClaimCheck(claim_id="c1", label="supported", evidence_ids=["e1"], reason="r")],
            validation_errors=[],
            searches=[],
            prompt_hash="h",
            model="m",
        )

    merged = merge_runs(run("KIVI", "adoption"), run("ITME", "growth"))
    ids = [c.id for c in merged.result.claims]
    assert len(ids) == len(set(ids)) == 2, "ID 가 겹친 채 남으면 인용이 어긋난다"
    assert {c.claim_id for c in merged.checks} == set(ids), "판정이 주장을 계속 가리켜야 한다"


def test_each_assignment_runs_only_its_own_questions(tmp_path):
    """배정 범위 밖의 질문은 그 조각이 건드리지 않는다."""
    final = build().invoke(
        {},
        config={"recursion_limit": 160, "configurable": {"output_dir": str(tmp_path)}},
    )
    import json

    rows = [
        json.loads(line)
        for line in (tmp_path / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    scoped = [r for r in rows if r.get("questions") and r["node"] in ("market", "domain")]
    assert scoped, "조사 조각의 질문 목록이 기록되어야 한다"
    assert any(len(r["questions"]) < 6 for r in scoped), "배정이 질문을 실제로 좁혀야 한다"
    # 역할 전체 결과는 조각이 합쳐져 완성된다.
    assert len(final["results"]["market"].result.assessments) == 6

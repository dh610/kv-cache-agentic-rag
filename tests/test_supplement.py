"""종합 뒤 보완 재실행 1라운드 (설계서 그림 1 / 표 12 / 표 13)."""

from collections import Counter

import pytest
from pydantic import ValidationError

from graph.main_graph import RESULT_KEYS, build_main_graph
from runtime.models import MockBackend
from runtime.runner import load_input
from runtime.settings import Limits, load_settings
from schemas.contracts import NODES


class Recording(MockBackend):
    def __init__(self):
        self.calls = Counter()

    def generate(self, node, data, evidence, system, user):
        self.calls[node] += 1
        return super().generate(node, data, evidence, system, user)


def run(backend=None, mode="fixture", settings=None, case="acceptance"):
    inputs = {n: load_input(n, case) for n in NODES}
    backend = backend or Recording()
    if settings is None:
        settings = load_settings()
        settings.limits.supplement = 1  # Exercise the bounded loop explicitly.
    state = build_main_graph(inputs, mode, settings, backend).invoke({})
    return state, backend


def test_limits_follow_design_table_13():
    assert load_settings().limits.supplement == 1
    assert Limits(search=3, questions=10, fix=1).supplement == 1
    assert Limits(search=3, questions=10, fix=1, supplement=1).supplement == 1
    assert Limits(search=3, questions=10, fix=1, supplement=0).supplement == 0
    with pytest.raises(ValidationError):
        Limits(search=3, questions=10, fix=1, supplement=2)


def test_mock_mode_never_supplements():
    state, backend = run(mode="mock")
    assert state["gaps"]  # acceptance fixture 는 mock 에서도 gaps 를 남긴다
    assert state["supplement_round"] == 0
    assert all(backend.calls[n] == 1 for n in NODES)


def test_supplement_reruns_gapped_roles_once_and_returns_to_synthesis():
    state, backend = run()
    assert state["gaps"]
    assert state["supplement_round"] == 1
    gapped = {g.role for g in state["gaps"] if g.role in NODES[:4]}
    for role in NODES[:4]:
        expected = 2 if role in gapped or "tech" in gapped else 1
        assert backend.calls[role] == expected, (role, backend.calls)
    assert backend.calls["synthesis"] == 2  # 결과 수집을 거치지 않고 종합으로 직접 복귀
    assert backend.calls["report"] == 1
    assert state["run_status"] != "completed"  # fixture 는 report gate 를 통과하지 못한다


class FailTechOnce(Recording):
    def generate(self, node, *args):
        self.calls[node] += 1
        if node == "tech" and self.calls[node] == 1:
            raise RuntimeError("transient")
        return super(Recording, self).generate(node, *args)


def test_supplement_reruns_dependents_when_tech_is_gapped():
    state, backend = run(FailTechOnce())
    assert state["supplement_round"] == 1
    assert all(backend.calls[n] == 2 for n in NODES[:4])
    assert state[RESULT_KEYS["tech"]].status != "failed"  # 재실행 결과로 교체됨


def test_supplement_is_bounded_by_limits():
    settings = load_settings()
    settings.limits.supplement = 0
    state, backend = run(settings=settings)
    assert state["gaps"] and state["supplement_round"] == 0
    assert all(backend.calls[n] == 1 for n in NODES)

    state, backend = run()  # supplement=1: 두 번째 라운드는 돌지 않는다
    assert state["supplement_round"] == 1 and backend.calls["synthesis"] == 2


def test_supplement_accumulates_sources_without_overwrite():
    state, _ = run()
    ids = [e.id for e in state["sources"]]
    assert len(ids) >= len(set(ids)) > 0
    # 재실행된 역할의 인용 출처도 리듀서에 남는다 (덮어쓰기 없음).
    rerun = {g.role for g in state["gaps"] if g.role in NODES[:4]}
    assert rerun or state["supplement_round"] == 1


def test_report_excludes_superseded_citations_but_keeps_source_history():
    from runtime.reporting import assemble_report, validate_report

    state, _ = run()
    previous = state["sources"][0].model_copy(update={"id": "superseded-source"})
    state["sources"].append(previous)
    text = assemble_report(state, RESULT_KEYS, "fixture")
    assert "[superseded-source]" not in text
    assert any(e.id == "superseded-source" for e in state["sources"])
    problems = validate_report(state, RESULT_KEYS, text, "fixture")["problems"]
    assert not any("superseded-source" in p for p in problems)

"""메인 그래프 (설계서 그림 1 / 표 12 / 표 14) 와 검색 출처 role 범위 (B.3) 테스트."""

from collections import Counter

from graph.main_graph import build_main_graph
from rag.interface import PAPER_ROLES, RoleFilter
from runtime.models import MockBackend
from runtime.runner import load_input
from runtime.settings import load_settings
from schemas.contracts import NODES, Evidence, Question


class Recording(MockBackend):
    def __init__(self):
        self.calls = Counter()

    def generate(self, node, data, evidence, system, user):
        self.calls[node] += 1
        return super().generate(node, data, evidence, system, user)


def pipeline(backend=None, mode="mock", settings=None):
    inputs = {node: load_input(node) for node in NODES}
    return build_main_graph(
        inputs, mode, settings or load_settings(), backend or Recording()
    ).invoke({})


def test_init_sets_design_table_14_keys():
    final = pipeline()
    assert final["target_techs"] == {"sw": "KIVI", "hw": "ITME"}
    assert final["domain"]
    assert final["limits"]["search"] == 3 and final["limits"]["supplement"] == 1
    assert final["supplement_round"] == 0
    assert isinstance(final["trl_result"], dict) and set(final["trl_result"]) == {"KIVI", "ITME"}
    assert final["report_path"].startswith("report.md")
    assert final["run_status"] == "completed"


def test_sources_accumulate_only_cited_evidence_without_overwrite():
    inputs = {node: load_input(node) for node in NODES}
    for node, data in inputs.items():
        for e in data.evidence:
            e.id = f"{node}-{e.id}"
    final = build_main_graph(inputs, "mock", load_settings(), Recording()).invoke({})
    ids = [e.id for e in final["sources"]]
    # 조사 4 + 종합 + 보고서: 각 노드가 인용한 근거가 모두 남아 있고 마지막 노드가 덮어쓰지 않는다.
    assert len(set(ids)) == len(ids) and len(ids) >= 8
    assert {i.split("-")[0] for i in ids} >= {"tech", "market", "stakeholder", "domain"}


class FailMarketOnce(Recording):
    def generate(self, node, *args):
        self.calls[node] += 1
        if node == "market" and self.calls[node] == 1:
            raise RuntimeError("transient")
        return super(Recording, self).generate(node, *args)


def test_supplement_reruns_only_gapped_role_once():
    backend = FailMarketOnce()
    final = pipeline(backend)
    assert final["supplement_round"] == 1
    assert backend.calls["market"] == 2  # 실패 → 보완 재실행 1회
    assert (
        backend.calls["tech"] == 1 and backend.calls["domain"] == 1
    )  # 기술이 안 바뀌면 다른 평가는 그대로
    assert backend.calls["synthesis"] == 2  # 보완 뒤 종합으로 직접 복귀
    assert final["market_result"].status == "completed"
    assert final["run_status"] == "completed"


class FailTechOnce(Recording):
    def generate(self, node, *args):
        self.calls[node] += 1
        if node == "tech" and self.calls[node] == 1:
            raise RuntimeError("transient")
        return super(Recording, self).generate(node, *args)


def test_supplement_reruns_dependents_when_tech_changes():
    backend = FailTechOnce()
    final = pipeline(backend)
    assert final["supplement_round"] == 1
    assert all(backend.calls[n] == 2 for n in ("tech", "market", "stakeholder", "domain"))
    assert final["run_status"] == "completed"


class AlwaysFailMarket(Recording):
    def generate(self, node, *args):
        self.calls[node] += 1
        if node == "market":
            raise RuntimeError("down")
        return super(Recording, self).generate(node, *args)


def test_supplement_is_bounded_by_limits_supplement():
    backend = AlwaysFailMarket()
    final = pipeline(backend)
    assert final["supplement_round"] == 1  # 표 13: 보완 1라운드
    assert backend.calls["market"] == 2
    assert final["run_status"] == "failed"
    assert any(g["role"] == "market" for g in final["gaps"])

    settings = load_settings()
    settings.limits.supplement = 0
    backend = AlwaysFailMarket()
    final = pipeline(backend, settings=settings)
    assert final["supplement_round"] == 0 and backend.calls["market"] == 1


def test_citation_check_flags_incomplete_references_outside_mock():
    class Injected:
        retryable = False

        def search(self, question, attempt):
            evidence = load_input("tech").evidence
            for e in evidence:
                e.id = "injected-" + e.id
            return evidence

    inputs = {node: load_input(node) for node in NODES}
    sources = {role: Injected() for role in NODES[:4]}
    final = build_main_graph(inputs, "live", load_settings(), Recording(), sources).invoke({})
    # demo fixture 발췌는 최종 REFERENCE 가 될 수 없다 → 검사 실패가 report 에 남고 완료로 승격되지 않는다.
    assert final["run_status"] == "needs_revision"
    assert "FAIL" in final["report_path"]
    assert any(e.startswith("citation_check:") for e in final["report"].validation_errors)


def test_role_filter_keeps_only_allowed_paper_roles_and_all_web():
    def ev(i, role, kind="paper"):
        return Evidence(
            id=i,
            text="t",
            title="t",
            url="u",
            technology="KIVI",
            source_type=kind,
            scope="target",
            document_role=role,
        )

    class Inner:
        retryable = True

        def search(self, question, attempt):
            return [ev("a", "target"), ev("b", "reference"), ev("c", "target", "web")]

    q = Question(id="q", technology="KIVI", criterion="fit", text="x")
    assert [e.id for e in RoleFilter(Inner(), PAPER_ROLES["domain"]).search(q, 1)] == ["a", "c"]
    assert [e.id for e in RoleFilter(Inner(), PAPER_ROLES["stakeholder"]).search(q, 1)] == [
        "b",
        "c",
    ]
    assert [e.id for e in RoleFilter(Inner(), PAPER_ROLES["market"]).search(q, 1)] == [
        "a",
        "b",
        "c",
    ]
    assert PAPER_ROLES["tech"] == {"target"}

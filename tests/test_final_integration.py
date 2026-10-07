import json

from runtime.models import OpenAIBackend
from schemas.contracts import ClaimCheck, JudgeResult
from tests.test_targeted_retries import Backend, Source, execute, input_data


def test_scoped_search_keeps_both_layers_and_bounded_attempts():
    class Layered(Source):
        def search(self, question, attempt, scope="target"):
            return [
                e.model_copy(update={"id": e.id + "-" + scope, "scope": scope})
                for e in super().search(question, attempt)
            ]

    run, backend = execute(Layered())
    assert run.status == "completed"
    assert len(run.searches) == 8
    assert max(r.attempt for r in run.searches) == 2
    assert {(r.intent, r.scope) for r in run.searches} == {
        ("positive", "target"),
        ("positive", "context"),
        ("critical", "target"),
        ("critical", "context"),
    }
    assert len(backend.reviewed) == 1


def test_aliases_restore_after_scoped_generation():
    class Scoped(Backend):
        def generate_scoped(self, node, packets):
            assert all(e.id.startswith("E") for p in packets for e in p[1])
            return OpenAIBackend.generate_scoped(self, node, packets)

    run, _ = execute(Source(), Scoped())
    known = {e.id for e in run.evidence}
    assert run.status == "completed"
    assert run.result.claims
    assert all(set(c.evidence_ids) <= known for c in run.result.claims)


def test_judge_fixes_claim_id_and_restores_only_supplied_citations():
    captured = []

    class Evaluator:
        def invoke(self, messages):
            captured.append(json.loads(messages[1][1]))
            return JudgeResult(
                checks=[
                    ClaimCheck(
                        claim_id="wrong-model-id",
                        label="supported",
                        evidence_ids=["E1"],
                        reason="matches",
                    )
                ]
            )

    data = input_data()
    result = Backend().generate("tech", data, data.evidence, "", "")
    backend = OpenAIBackend.__new__(OpenAIBackend)
    backend.evaluator = Evaluator()
    checks = backend.judge(result, data.evidence).checks
    assert len(captured) == len(result.claims)
    assert [c.claim_id for c in checks] == [c.id for c in result.claims]
    for claim, check in zip(result.claims, checks):
        assert set(check.evidence_ids) <= set(claim.evidence_ids)


def test_supported_check_cannot_rescue_unknown_claim_technology():
    class Foreign(Backend):
        def generate(self, *args):
            result = super().generate(*args)
            result.claims.append(
                result.claims[0].model_copy(update={"id": "foreign", "technology": "foreign-tech"})
            )
            return result

    run, _ = execute(Source(), Foreign())
    assert run.status == "needs_revision"
    assert run.result.claims
    assert all(c.technology != "foreign-tech" for c in run.result.claims)


def test_same_web_excerpt_has_distinct_ids_for_search_layers(monkeypatch):
    import rag.web as web
    from rag.evidence import merge_evidence

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "results": [
                    {
                        "url": "https://example.com/kivi",
                        "title": "KIVI KV cache",
                        "raw_content": "KIVI KV cache quantization reduces memory.",
                    }
                ]
            }

    monkeypatch.setattr(web.httpx, "post", lambda *a, **kw: Response())
    source = web.WebSource.__new__(web.WebSource)
    source.key, source.timeout, source.k = "test-only", 1, 1
    source.context_terms, source.excerpt_chars = ["KV cache"], 1200
    q = input_data().questions[0]
    direct = source.search(q, 1, "target")
    background = source.search(q, 1, "context")
    assert direct and background
    assert direct[0].id != background[0].id
    assert len(merge_evidence(direct, background)) == 2

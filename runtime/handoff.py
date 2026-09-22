"""Artifact handoff checks, separate from whether a research question has a known answer."""

import re

from graph.node_graph import contract_errors
from runtime.node_rules import HANDOFF_RULES
from runtime.prompts import load_rubric
from schemas.contracts import Evidence, JudgeResult, NodeInput, NodeRun


def reference_issues(evidence: Evidence) -> list[str]:
    required = ["id", "text", "title", "url"]
    if evidence.source_type == "paper":
        # Guide format: 저자(YYYY). 논문제목. 학술지/학회명, 권(호), 페이지.
        required += ["authors", "year", "venue", "citation_id", "document_id", "page"]
    elif evidence.source_type == "patent":
        # Guide format: 출원인(YYYY-MM). 특허명, 특허번호/공개번호, URL
        required += ["publisher", "published_at", "citation_id"]
    elif evidence.source_type == "web":
        required += ["site", "published_at", "retrieved_at"]
        if not (evidence.publisher or evidence.authors):
            required.append("publisher_or_authors")
    else:
        return [f"{evidence.id}: demo fixture cannot be a final reference"]
    return [
        f"{evidence.id}: missing {name}"
        for name in required
        if not str(getattr(evidence, name, None) or "").strip()
    ]


def check_handoff(data: NodeInput, run: NodeRun) -> dict:
    """Check coverage and source completeness, never certify model factual quality."""
    problems = []
    if run.mode == "mock" or run.model.startswith("mock"):
        problems.append("mock runs are not implementation handoffs")
    if run.status == "failed":
        problems.append("node execution failed")
    all_unknown = (
        bool(run.result.assessments)
        and all(a.judgment == "확인 불가" for a in run.result.assessments)
        and not run.result.claims
    )
    # Exhausted searches with no evidence can still be a complete, honest response.
    problems.extend(
        e for e in run.validation_errors if not (all_unknown and e == "No evidence available")
    )
    problems.extend(
        contract_errors(run.node, data, run.result, run.evidence, JudgeResult(checks=run.checks))
    )
    problems.extend(HANDOFF_RULES.get(run.node, lambda d, r: [])(data, run))
    if any(c.label != "supported" for c in run.checks):
        problems.append("claim verification did not pass")
    if any(s.error for s in run.searches):
        problems.append("search execution errors remain")
    if {s.question_id for s in run.searches} != {q.id for q in data.questions}:
        problems.append("search history must cover every input question")
    if any(s.attempt < 1 or s.attempt > 3 for s in run.searches):
        problems.append("search history exceeds the per-question attempt contract")
    if not run.prompt_hash or not run.model:
        problems.append("missing execution metadata")
    expected = {
        (tech, c.id) for tech in data.target_techs.values() for c in load_rubric(run.node).criteria
    }
    actual = {(a.technology, a.criterion) for a in run.result.assessments}
    requested = {(q.technology, q.criterion) for q in data.questions}
    if requested != expected:
        problems.append("input must request every technology/rubric criterion; use acceptance case")
    if actual != expected:
        problems.append("result must cover every technology/rubric criterion")
    unknown = []
    for item in run.result.assessments:
        if not item.rationale.strip():
            problems.append(f"{item.technology}/{item.criterion}: empty rationale")
        if item.judgment == "확인 불가":
            unknown.append(f"{item.technology}/{item.criterion}: {item.rationale}")
    if unknown and not run.result.unverified:
        problems.append("unknown judgments must remain in unverified")
    if re.search(r"\[MOCK\]|\[더미\]|\bTODO\b|dummy", run.result.model_dump_json(), re.I):
        problems.append("result contains development placeholder text")
    used = {eid for c in run.result.claims for eid in c.evidence_ids}
    used.update(eid for a in run.result.assessments for eid in a.evidence_ids)
    for evidence in run.evidence:
        if evidence.id in used:
            problems.extend(reference_issues(evidence))
    return {
        "ready": not problems,
        "problems": sorted(set(problems)),
        "unknown_items": unknown,
        "expected_items": len(expected),
        "actual_items": len(actual),
        "used_evidence_ids": sorted(used),
        "note": "Contract/coverage check only; human rubric and factual review still required.",
    }

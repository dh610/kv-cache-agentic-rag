"""Pre-registered retrieval evaluation; human-verified spans are mandatory."""

from __future__ import annotations

import re
from collections import Counter

from pydantic import BaseModel, ConfigDict, Field


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    technology: str
    question: str
    english_keywords: str
    document_id: str
    page: int = Field(ge=1)
    answer_quote: str
    verified: bool = False
    acronym_or_number: bool = False


def validate_dataset(cases, settings):
    policy = settings.evaluation
    if len(cases) != policy.samples or len({c.id for c in cases}) != len(cases):
        raise ValueError("Evaluation requires exactly 30 unique question-answer pairs")
    expected = {t: policy.per_technology for t in settings.target_techs.values()}
    if dict(Counter(c.technology for c in cases)) != expected:
        raise ValueError("Each target technology requires 15 pairs")
    if any(not re.search("[가-힣]", c.question) for c in cases):
        raise ValueError("Every evaluation query must contain Korean")
    special = [
        c for c in cases if c.acronym_or_number and re.search(r"\b[A-Z]{2,}\b|\d", c.question)
    ]
    if len(special) / len(cases) < policy.acronym_numeric_fraction:
        raise ValueError("At least one third of queries must contain acronyms/numbers")
    if any(
        not c.verified or not c.answer_quote.strip() or not c.english_keywords.strip()
        for c in cases
    ):
        raise ValueError(
            "Human verification, answer quote and English keywords required for every pair"
        )


def normalize(text):
    return re.sub(r"\s+", " ", text).strip()


def resolve_gold(cases, chunks):
    """Re-derive gold IDs after every chunk configuration; do not reuse stale IDs."""
    gold = {}
    for c in cases:
        ids = {
            e.id
            for e in chunks
            if e.document_id == c.document_id
            and e.page == c.page
            and e.technology == c.technology
            and normalize(c.answer_quote) in normalize(e.text)
        }
        if not ids:
            raise ValueError(
                f"{c.id}: verified quote not found in chunks; inspect page/span or shorten the label to a complete evidence sentence"
            )
        gold[c.id] = ids
    return gold


def metrics(rankings, gold):
    if set(rankings) != set(gold) or not gold:
        raise ValueError("Rankings must cover every evaluation case exactly")
    ranks = []
    for q, ranked in rankings.items():
        unique = list(dict.fromkeys(ranked))
        ranks.append(next((i for i, eid in enumerate(unique, 1) if eid in gold[q]), 0))
    n = len(ranks)
    return {
        **{f"hit@{k}": sum(0 < r <= k for r in ranks) / n for k in (1, 3, 5)},
        "mrr": sum(1 / r if r else 0 for r in ranks) / n,
        "n": n,
        "ranks": ranks,
    }


def passed(score, policy):
    return score["hit@5"] >= policy.min_hit5 and score["mrr"] >= policy.min_mrr


def select_model(results, policy):
    best = max(r["metrics"]["mrr"] for r in results)
    tied = [r for r in results if best - r["metrics"]["mrr"] <= policy.tie_mrr + 1e-12]
    # Tie is an agreed decision rule, not a statistical significance claim.
    return sorted(tied, key=lambda r: (-r["metrics"]["hit@1"], r["parameters"], r["model"]))[0]


def evaluate_dense(
    settings,
    cases,
    model_name,
    *,
    bilingual=False,
    rerank=False,
    sparse=False,
    trust_remote_code=False,
):
    import numpy as np
    from huggingface_hub import model_info
    from sentence_transformers import CrossEncoder, SentenceTransformer

    from rag.local_index import corpus_signature, load_catalog, read_chunks

    settings = settings.model_copy(deep=True)
    settings.retrieval.model = model_name
    catalog = load_catalog()
    chunks, _ = read_chunks(settings, catalog)
    gold = resolve_gold(cases, chunks)
    revision = model_info(model_name).sha
    query_texts = [c.question + (" " + c.english_keywords if bilingual else "") for c in cases]
    passages = [e.text for e in chunks]
    if "multilingual-e5" in model_name:
        query_texts = ["query: " + q for q in query_texts]
        passages = ["passage: " + p for p in passages]
    parameters = 0
    if sparse:
        if model_name != "BAAI/bge-m3":
            raise ValueError("Learned sparse experiment is only defined for BGE-M3")
        from FlagEmbedding import BGEM3FlagModel
        from huggingface_hub import snapshot_download

        # Pin the experiment to a resolved model commit.
        path = snapshot_download(model_name, revision=revision)
        encoder = BGEM3FlagModel(
            path, use_fp16=False, devices=["cpu"], query_max_length=8192, passage_max_length=8192
        )
        doc = encoder.encode(
            passages, return_dense=True, return_sparse=True, return_colbert_vecs=False
        )
        query = encoder.encode(
            query_texts, return_dense=True, return_sparse=True, return_colbert_vecs=False
        )
        scores = np.asarray(query["dense_vecs"]) @ np.asarray(doc["dense_vecs"]).T
        lexical = np.asarray(
            [
                [encoder.compute_lexical_matching_score(q, d) for d in doc["lexical_weights"]]
                for q in query["lexical_weights"]
            ]
        )
        # Reciprocal-rank fusion avoids adding incomparable raw dense/sparse scores.
        fused = np.zeros_like(scores)
        for channel in (scores, lexical):
            order = np.argsort(-channel, axis=1, kind="stable")
            for row in range(len(cases)):
                fused[row, order[row]] = (
                    1 / (60 + np.arange(1, len(chunks) + 1)) + fused[row, order[row]]
                )
        scores = fused
        parameters = 568_000_000
    else:
        encoder = SentenceTransformer(
            model_name, revision=revision, trust_remote_code=trust_remote_code, device="cpu"
        )
        if "multilingual-e5" in model_name:
            encoder.max_seq_length = 512
        parameters = sum(p.numel() for p in encoder.parameters())
        doc = encoder.encode(passages, normalize_embeddings=True)
        query = encoder.encode(query_texts, normalize_embeddings=True)
        scores = np.asarray(query) @ np.asarray(doc).T
    reranker_revision = model_info(settings.retrieval.reranker).sha if rerank else None
    ranker = (
        CrossEncoder(
            settings.retrieval.reranker,
            revision=reranker_revision,
            trust_remote_code=False,
            device="cpu",
        )
        if rerank
        else None
    )
    rankings = {}
    for row, c in enumerate(cases):
        candidates = [
            i
            for i in np.argsort(-scores[row], kind="stable")
            if chunks[i].technology == c.technology and chunks[i].document_role == "target"
        ]
        # MRR uses the entire filtered dense list, or the entire reranked candidate pool.
        if ranker:
            candidates = candidates[: settings.retrieval.candidates]
            values = ranker.predict([(query_texts[row], chunks[i].text) for i in candidates])
            candidates = [candidates[i] for i in np.argsort(-np.asarray(values), kind="stable")]
        rankings[c.id] = [chunks[i].id for i in candidates]
    return {
        "model": model_name,
        "revision": revision,
        "parameters": parameters,
        "bilingual": bilingual,
        "sparse": sparse,
        "rerank": rerank,
        "reranker_revision": reranker_revision,
        "settings": settings.model_dump(),
        "corpus_signature": corpus_signature(settings, catalog),
        "metrics": metrics(rankings, gold),
        "rankings": rankings,
        "gold": {key: sorted(value) for key, value in gold.items()},
    }

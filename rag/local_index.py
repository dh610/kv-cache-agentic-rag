from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Literal

import yaml
from langsmith import traceable
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pypdf import PdfReader

from runtime.settings import ROOT, Settings
from schemas.contracts import Evidence, Question


class Paper(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    path: str
    title: str
    url: str
    technology: str
    role: Literal["target", "reference"]
    body_pages: tuple[int, int]
    authors: str | None = None
    year: int | None = Field(default=None, ge=1900, le=2100)
    venue: str | None = None
    affiliation: Literal["first_party", "independent", "unknown"] = "unknown"
    affiliation_reason: str | None = None

    @model_validator(mode="after")
    def ordered_pages(self):
        start, end = self.body_pages
        if start < 1 or end < start:
            raise ValueError("body_pages must be an inclusive 1-based range")
        return self


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    documents: list[Paper] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_documents(self):
        for attr in ("id", "path"):
            values = [getattr(d, attr) for d in self.documents]
            if len(values) != len(set(values)):
                raise ValueError(f"Duplicate document {attr}")
        return self


def load_catalog(path: Path | None = None) -> Catalog:
    return Catalog.model_validate(
        yaml.safe_load((path or ROOT / "data/documents.yaml").read_text(encoding="utf-8"))
    )


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def corpus_signature(settings: Settings, catalog: Catalog, root: Path = ROOT) -> str:
    documents = []
    for paper in catalog.documents:
        path = root / paper.path
        if not path.is_file():
            raise FileNotFoundError(f"Missing {paper.path}; put the shared paper PDF there first")
        documents.append(
            {**paper.model_dump(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        )
    return digest(
        {
            "documents": documents,
            "model": settings.retrieval.model,
            "revision": settings.retrieval.revision,
            "size": settings.retrieval.chunk_size,
            "overlap": settings.retrieval.chunk_overlap,
            "format": 1,
        }
    )


def read_chunks(settings: Settings, catalog: Catalog, root: Path = ROOT):
    """Deterministic page-local character windows; no table/OCR reconstruction promised."""
    chunks, page_count, indexed_pages = [], 0, 0
    for paper in catalog.documents:
        content_hash = hashlib.sha256((root / paper.path).read_bytes()).hexdigest()[:12]
        reader = PdfReader(root / paper.path)
        page_count += len(reader.pages)
        if page_count > 200:
            raise ValueError("Document pool exceeds 200 full PDF pages")
        start, end = paper.body_pages
        if end > len(reader.pages):
            raise ValueError(f"{paper.id}: body range exceeds PDF length")
        for page_number in range(start, end + 1):
            text = reader.pages[page_number - 1].extract_text() or ""
            if not text.strip():
                raise ValueError(
                    f"{paper.id} p.{page_number}: empty extraction; inspect/OCR manually"
                )
            indexed_pages += 1
            step = settings.retrieval.chunk_size - settings.retrieval.chunk_overlap
            for offset in range(0, len(text), step):
                chunk = text[offset : offset + settings.retrieval.chunk_size].strip()
                if not chunk:
                    continue
                chunks.append(
                    Evidence(
                        id=f"{paper.id}-{content_hash}-p{page_number}-{offset}",
                        text=chunk,
                        title=paper.title,
                        url=paper.url,
                        technology=paper.technology,
                        source_type="paper",
                        scope="target" if paper.role == "target" else "context",
                        document_role=paper.role,
                        page=page_number,
                        document_id=paper.id,
                        authors=paper.authors,
                        year=paper.year,
                        venue=paper.venue,
                        affiliation=paper.affiliation,
                        affiliation_reason=paper.affiliation_reason,
                    )
                )
                if offset + settings.retrieval.chunk_size >= len(text):
                    break
    return chunks, {"full_pdf_pages": page_count, "indexed_body_pages": indexed_pages}


def load_encoder(settings: Settings, revision: str | None = None):
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise ImportError("Install local RAG dependencies: uv sync --extra rag") from exc
    # HF cache is personal; no model weights enter Git.
    return SentenceTransformer(
        settings.retrieval.model,
        revision=revision or settings.retrieval.revision,
        device=os.getenv("EMBEDDING_DEVICE", "cpu"),
        trust_remote_code=False,
    )


def build_index(settings: Settings, encoder=None) -> dict:
    try:
        import faiss
        import numpy as np
    except ImportError as exc:
        raise ImportError("Install local RAG dependencies: uv sync --extra rag") from exc
    catalog = load_catalog()
    signature = corpus_signature(settings, catalog)
    chunks, counts = read_chunks(settings, catalog)
    encoder = encoder or load_encoder(settings)
    vectors = np.asarray(
        encoder.encode([c.text for c in chunks], normalize_embeddings=True, show_progress_bar=True),
        dtype="float32",
    )
    faiss.normalize_L2(vectors)
    index = faiss.IndexFlatIP(vectors.shape[1])
    index.add(vectors)
    resolved = getattr(
        getattr(getattr(encoder[0], "auto_model", None), "config", None), "_commit_hash", None
    )
    if not resolved:
        raise ValueError("Could not resolve embedding model revision; index was not saved")
    target = ROOT / settings.retrieval.index_dir
    target.mkdir(parents=True, exist_ok=True)
    chunk_text = json.dumps([c.model_dump() for c in chunks], ensure_ascii=False, indent=2)
    faiss.write_index(index, str(target / "vectors.faiss"))
    (target / "chunks.json").write_text(chunk_text, encoding="utf-8")
    manifest = {
        "signature": signature,
        "model": settings.retrieval.model,
        "resolved_revision": resolved,
        "dimensions": vectors.shape[1],
        "chunks": len(chunks),
        **counts,
        "chunks_sha256": hashlib.sha256(chunk_text.encode()).hexdigest(),
        "index_sha256": hashlib.sha256((target / "vectors.faiss").read_bytes()).hexdigest(),
    }
    # Manifest is written last; partial writes fail validation at load time.
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


class PaperSource:
    retryable = True

    def __init__(self, settings: Settings, node: str):
        try:
            import faiss
        except ImportError as exc:
            raise ImportError("Install local RAG dependencies: uv sync --extra rag") from exc
        self.settings, self.node = settings, node
        target = ROOT / settings.retrieval.index_dir
        if not (target / "manifest.json").exists():
            raise FileNotFoundError(
                "Build the paper index first: uv run --extra rag python -m app.index"
            )
        self.manifest = json.loads((target / "manifest.json").read_text())
        if self.manifest["signature"] != corpus_signature(settings, load_catalog()):
            raise ValueError("Paper/config changed; rebuild with python -m app.index")
        for filename, key in (("vectors.faiss", "index_sha256"), ("chunks.json", "chunks_sha256")):
            if hashlib.sha256((target / filename).read_bytes()).hexdigest() != self.manifest[key]:
                raise ValueError("Incomplete/changed local index; rebuild it")
        self.chunks = [
            Evidence.model_validate(c) for c in json.loads((target / "chunks.json").read_text())
        ]
        self.index = faiss.read_index(str(target / "vectors.faiss"))
        if self.index.ntotal != len(self.chunks) or self.index.d != self.manifest["dimensions"]:
            raise ValueError("Index dimensions/count mismatch; rebuild it")
        self.encoder = load_encoder(settings, self.manifest["resolved_revision"])
        self.lock = threading.Lock()
        self.reranker = None
        if settings.retrieval.rerank:
            from sentence_transformers import CrossEncoder

            self.reranker = CrossEncoder(
                settings.retrieval.reranker,
                device=os.getenv("EMBEDDING_DEVICE", "cpu"),
                trust_remote_code=False,
            )

    @traceable(run_type="retriever", name="paper_search")
    def search(self, question: Question, attempt: int) -> list[Evidence]:
        import faiss
        import numpy as np

        query = f"{question.technology} {question.text}"
        with self.lock:
            vectors = np.asarray(
                self.encoder.encode([query], normalize_embeddings=True), dtype="float32"
            )
        faiss.normalize_L2(vectors)
        # Tiny course corpus: filter all candidates before selecting top-k.
        _, ids = self.index.search(vectors, self.index.ntotal)
        candidates = [self.chunks[int(i)] for i in ids[0] if i >= 0]
        candidates = [e for e in candidates if self.accepts(e, question)]
        candidates = candidates[: self.settings.retrieval.candidates]
        if self.reranker is not None and candidates:
            with self.lock:
                scores = self.reranker.predict([(query, e.text) for e in candidates])
            candidates = [
                e
                for _, e in sorted(zip(scores, candidates), key=lambda x: float(x[0]), reverse=True)
            ]
        return candidates[: self.settings.retrieval.top_k]

    def accepts(self, evidence: Evidence, question: Question) -> bool:
        if self.node in ("tech", "domain"):
            return evidence.document_role == "target" and evidence.technology == question.technology
        if self.node == "stakeholder":
            return question.criterion == "competitors" and evidence.document_role == "reference"
        return evidence.technology == question.technology or evidence.document_role == "reference"

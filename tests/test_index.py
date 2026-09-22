import json
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

import rag.local_index as local_index
from rag.evidence import merge_evidence
from rag.local_index import Catalog, Paper, PaperSource, corpus_signature, read_chunks
from runtime.runner import load_input
from runtime.settings import load_settings


def write_pdf(path, texts):
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 20 700 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(path)


@pytest.fixture
def corpus(tmp_path):
    write_pdf(tmp_path / "paper.pdf", ["KIVI per token " * 30, "Excluded reference page"])
    catalog = Catalog(
        documents=[
            Paper(
                id="kivi",
                path="paper.pdf",
                title="Test document",
                url="https://example.invalid/paper",
                technology="KIVI",
                role="target",
                body_pages=(1, 1),
            )
        ]
    )
    return tmp_path, catalog


def test_page_range_chunk_identity_and_overlap(corpus):
    root, catalog = corpus
    settings = load_settings()
    settings.retrieval.chunk_size = 120
    settings.retrieval.chunk_overlap = 20
    chunks, counts = read_chunks(settings, catalog, root)
    assert counts == {"full_pdf_pages": 2, "indexed_body_pages": 1}
    assert len(chunks) > 1
    assert all(c.page == 1 and len(c.text) <= 120 for c in chunks)
    assert all("Excluded" not in c.text for c in chunks)
    assert len({c.id for c in chunks}) == len(chunks)
    assert chunks == read_chunks(settings, catalog, root)[0]


def test_signature_changes_when_metadata_text_or_config_changes(corpus):
    root, catalog = corpus
    settings = load_settings()
    original = corpus_signature(settings, catalog, root)
    catalog.documents[0].role = "reference"
    assert corpus_signature(settings, catalog, root) != original
    catalog.documents[0].role = "target"
    settings.retrieval.chunk_overlap += 1
    assert corpus_signature(settings, catalog, root) != original
    settings = load_settings()
    write_pdf(root / "paper.pdf", ["Changed content"])
    assert corpus_signature(settings, catalog, root) != original


def test_empty_page_and_invalid_page_range_fail(corpus):
    root, catalog = corpus
    catalog.documents[0].body_pages = (1, 3)
    with pytest.raises(ValueError, match="range"):
        read_chunks(load_settings(), catalog, root)
    catalog.documents[0].body_pages = (1, 1)
    write_pdf(root / "paper.pdf", [""])
    with pytest.raises(ValueError, match="empty extraction"):
        read_chunks(load_settings(), catalog, root)


def test_200_page_limit_counts_entire_pdfs(corpus):
    root, catalog = corpus
    write_pdf(root / "paper.pdf", ["Test"] * 201)
    with pytest.raises(ValueError, match="200"):
        read_chunks(load_settings(), catalog, root)


def test_target_and_reference_filter():
    adapter = PaperSource.__new__(PaperSource)
    adapter.node = "tech"
    data = load_input("tech")
    kivi, itme = data.evidence
    question = data.questions[0]
    assert adapter.accepts(kivi, question)
    assert not adapter.accepts(itme, question)
    itme.document_role = "reference"
    assert not adapter.accepts(itme, question)
    adapter.node = "domain"
    assert adapter.accepts(itme, question)


def test_conflicting_citation_id_is_never_overwritten():
    evidence = load_input("tech").evidence[0]
    changed = evidence.model_copy(update={"text": "Contradicting content"})
    with pytest.raises(ValueError, match="Conflicting evidence"):
        merge_evidence([evidence], [changed])
    assert merge_evidence([evidence], [evidence]) == [evidence]


def test_index_roundtrip_and_stale_detection_with_fake_encoder(corpus, monkeypatch):
    pytest.importorskip(
        "faiss", reason="Optional: uv run --with faiss-cpu --with numpy pytest tests/test_index.py"
    )
    import numpy as np

    root, catalog = corpus

    class FakeEncoder:
        def __getitem__(self, index):
            return SimpleNamespace(
                auto_model=SimpleNamespace(config=SimpleNamespace(_commit_hash="test-revision"))
            )

        def encode(self, texts, **kwargs):
            return np.array([[1.0, 0.0] for _ in texts], dtype="float32")

    monkeypatch.setattr(local_index, "ROOT", root)
    monkeypatch.setattr(local_index, "load_catalog", lambda: catalog)
    real_signature, real_chunks = corpus_signature, read_chunks
    monkeypatch.setattr(local_index, "corpus_signature", lambda s, c: real_signature(s, c, root))
    monkeypatch.setattr(local_index, "read_chunks", lambda s, c: real_chunks(s, c, root))
    monkeypatch.setattr(local_index, "load_encoder", lambda *a: FakeEncoder())
    settings = load_settings()
    manifest = local_index.build_index(settings, FakeEncoder())
    assert manifest["resolved_revision"] == "test-revision"
    source = PaperSource(settings, "tech")
    found = source.search(load_input("tech").questions[0], 1)
    assert found and all(e.technology == "KIVI" for e in found)
    # Tampering or an interrupted rebuild must never yield a mismatched catalog/index.
    target = root / settings.retrieval.index_dir / "chunks.json"
    target.write_text(json.dumps([]))
    with pytest.raises(ValueError, match="Incomplete/changed"):
        PaperSource(settings, "tech")
    local_index.build_index(settings, FakeEncoder())
    settings.retrieval.chunk_overlap += 1
    with pytest.raises(ValueError, match="Paper/config changed"):
        PaperSource(settings, "tech")

"""Prepare missing local resources and generate a report via ./run-report.sh."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

import httpx
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from rag.local_index import build_index, corpus_signature, load_catalog, load_encoder, read_chunks
from runtime.settings import ROOT, load_settings

MAX_PDF_BYTES = 50 * 1024 * 1024


def check_keys() -> None:
    missing = [
        name for name in ("OPENAI_API_KEY", "TAVILY_API_KEY") if not os.getenv(name, "").strip()
    ]
    if os.getenv("LANGSMITH_TRACING", "false").lower() == "true":
        if not os.getenv("LANGSMITH_API_KEY", "").strip():
            missing.append("LANGSMITH_API_KEY")
        project = os.getenv("LANGSMITH_PROJECT", "").strip()
        if not project or "yourname" in project:
            missing.append("LANGSMITH_PROJECT (개인 프로젝트 이름)")
    if missing:
        raise ValueError(
            "설정 필요: "
            + ", ".join(missing)
            + ". .env.example을 .env로 복사하고 값을 입력하거나 환경변수로 export하세요. "
            "키 없이 설치만 하려면 ./run-report.sh --prepare-only를 사용하세요."
        )


def validate_pdf(path: Path, pages: int, body_end: int) -> None:
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ValueError(f"{path.name}: PDF 파일이 아닙니다")
    try:
        reader = PdfReader(path)
    except PdfReadError as exc:
        raise ValueError(f"{path.name}: 손상된 PDF입니다. 등록 판본을 다시 준비하세요") from exc
    if reader.is_encrypted or len(reader.pages) != pages or body_end > len(reader.pages):
        raise ValueError(
            f"{path.name}: 등록 판본({pages}페이지)과 다릅니다. 원문/본문 범위를 확인하세요"
        )


def download_pdf(url: str, destination: Path, pages: int, body_end: int) -> None:
    """Validate a temporary download before publishing it; never overwrite a user's PDF."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as temp:
        temporary = Path(temp.name)
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=90) as response:
            response.raise_for_status()
            size = 0
            with temporary.open("wb") as stream:
                for block in response.iter_bytes():
                    size += len(block)
                    if size > MAX_PDF_BYTES:
                        raise ValueError("PDF 다운로드가 50MB 제한을 초과했습니다")
                    stream.write(block)
        validate_pdf(temporary, pages, body_end)
        try:
            os.link(temporary, destination)  # fails atomically if an existing PDF appeared
        except FileExistsError:
            validate_pdf(destination, pages, body_end)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_papers(catalog, root: Path = ROOT) -> None:
    specs = json.loads((root / "data/paper_downloads.json").read_text())
    for paper in catalog.documents:
        destination = root / paper.path
        spec = specs.get(paper.id)
        if spec is None:
            raise ValueError(
                f"{paper.id}: data/paper_downloads.json에 판본 URL/페이지를 등록하세요"
            )
        if destination.exists():
            validate_pdf(destination, spec["pages"], paper.body_pages[1])
            print(f"  재사용: {paper.path}", flush=True)
            continue
        print(f"  다운로드: {paper.id} — {spec['url']}", flush=True)
        try:
            download_pdf(spec["url"], destination, spec["pages"], paper.body_pages[1])
        except (httpx.HTTPError, ValueError, OSError) as exc:
            raise ValueError(
                f"{paper.id} PDF 준비 실패 ({type(exc).__name__}). {spec['url']}의 등록 판본을 "
                f"{paper.path}에 직접 저장한 뒤 재실행하세요."
            ) from exc


def index_current(settings, catalog, root: Path = ROOT) -> bool:
    """Check cache integrity without loading/downloading an embedding model."""
    import faiss

    target = root / settings.retrieval.index_dir
    try:
        manifest = json.loads((target / "manifest.json").read_text())
        if manifest["model"] != settings.retrieval.model or not manifest["resolved_revision"]:
            return False
        if manifest["signature"] != corpus_signature(settings, catalog, root):
            return False
        for name, key in (("chunks.json", "chunks_sha256"), ("vectors.faiss", "index_sha256")):
            if hashlib.sha256((target / name).read_bytes()).hexdigest() != manifest[key]:
                return False
        chunks = json.loads((target / "chunks.json").read_text())
        index = faiss.read_index(str(target / "vectors.faiss"))
        return (
            index.ntotal == len(chunks) == manifest["chunks"] and index.d == manifest["dimensions"]
        )
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true")
    mode.add_argument("--prepare-only", action="store_true")
    mode.add_argument("--check-keys", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    settings = load_settings()  # loads .env without overriding exported credentials
    if not args.mock and not args.prepare_only:
        check_keys()
    if args.check_keys:
        return 0
    if not args.mock:
        print("[2/4] 논문 PDF 준비", flush=True)
        catalog = load_catalog()
        prepare_papers(catalog)
        _, counts = read_chunks(settings, catalog)
        print(
            f"  전체 {counts['full_pdf_pages']}페이지 / 본문 {counts['indexed_body_pages']}페이지",
            flush=True,
        )
        print("[3/4] BGE-M3 임베딩·FAISS 인덱스 준비", flush=True)
        if index_current(settings, catalog):
            print("  현재 PDF·설정과 일치하는 인덱스를 재사용합니다", flush=True)
            if args.prepare_only:
                # A copied valid index may exist on a machine with no model cache.
                load_encoder(settings)
        else:
            print(
                "  인덱스를 생성합니다. 처음에는 임베딩 모델 다운로드로 시간이 걸립니다", flush=True
            )
            manifest = build_index(settings)
            print(f"  완료: {manifest['chunks']}청크 / {manifest['dimensions']}차원", flush=True)
    if args.prepare_only:
        print("준비 완료. 키 설정 후 ./run-report.sh를 실행하세요. 유료 API는 호출하지 않았습니다.")
        return 0
    from app.run_pipeline import main as run_pipeline

    print(
        "[4/4] 보고서 생성 (mock 연결 점검)"
        if args.mock
        else "[4/4] 실제 조사·보고서 생성 (LLM·검색 API 사용)",
        flush=True,
    )
    code = run_pipeline(["--mode", "mock" if args.mock else "live"])
    if code == 2:
        print(
            "보고서 초안이 생성됐지만 미확인/검증 항목이 남았습니다. state.json의 report_check를 확인하세요."
        )
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, ImportError, httpx.HTTPError) as exc:
        print(f"실행 준비/처리 실패: {exc}")
        raise SystemExit(1) from None

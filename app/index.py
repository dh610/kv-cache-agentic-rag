from __future__ import annotations

import argparse
import json

from rag.local_index import build_index, corpus_signature, load_catalog, read_chunks
from runtime.settings import load_settings


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build local BGE-M3 dense / FAISS paper index")
    parser.add_argument(
        "--check", action="store_true", help="Parse PDFs/count pages only; no model download"
    )
    args = parser.parse_args(argv)
    settings = load_settings()
    if args.check:
        catalog = load_catalog()
        signature = corpus_signature(settings, catalog)
        chunks, counts = read_chunks(settings, catalog)
        print(json.dumps({**counts, "chunks": len(chunks), "signature": signature}, indent=2))
    else:
        print(json.dumps(build_index(settings), indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, FileNotFoundError, ImportError) as exc:
        print(f"Setup error: {exc}")
        raise SystemExit(1) from None

"""Load every supported file from a folder into Qdrant.

    python -m scripts.ingest_folder                 # uses DOCUMENTS_DIR (data/documents)
    python -m scripts.ingest_folder path/to/folder
"""
from __future__ import annotations

import sys
from pathlib import Path

from app.ingestion.pipeline import ALLOWED_EXT, IngestError, ingest_file
from app.services import build_services


def main() -> int:
    svc = build_services()
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else svc.settings.documents_dir
    files = sorted(p for p in folder.glob("*") if p.is_file() and p.suffix.lower() in ALLOWED_EXT)
    if not files:
        print(f"No supported files in {folder}")
        return 1
    svc.store.ensure_collection()
    print(f"Loading the embedding model ({svc.settings.embed_model}). The first run downloads it.")
    svc.embedder.warmup()
    failed = 0
    for f in files:
        try:
            res = ingest_file(f, svc.ingest_ctx, lambda stage, pct, msg: print(f"  [{pct:3d}%] {stage}: {msg}"))
            print(f"OK   {f.name}: {res.chunk_count} chunks, {res.classification}, {res.strategies} {res.warnings or ''}")
        except IngestError as exc:
            failed += 1
            print(f"FAIL {f.name}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

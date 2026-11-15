"""Document ingestion: validate, extract, clean, chunk, label, embed, store."""
from __future__ import annotations

import hashlib
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..access import CLASSIFICATIONS
from ..config import Settings
from ..directory import save_managers
from ..models import Chunk, DocMeta, DocStructure
from .chunking import chunk_structure
from .convert import assign_pages, docx_to_pdf
from .loaders_docx import parse_docx
from .loaders_excel import chunk_excel, read_xls, read_xlsx
from .loaders_html import parse_html
from .loaders_pdf import parse_pdf, pdf_page_texts
from .loaders_text import parse_text
from .policy import DocPolicy, default_policy, find_policy
from .text_utils import slugify

log = logging.getLogger(__name__)

ALLOWED_EXT = {".pdf", ".docx", ".xlsx", ".xls", ".html", ".htm", ".txt", ".md"}
Progress = Callable[[str, int, str], None]


class IngestError(Exception):
    """A file that cannot be ingested. The message is safe to show to the user."""


@dataclass
class IngestResult:
    doc_id: str
    doc_name: str
    doc_type: str
    classification: str
    chunk_count: int
    strategies: dict[str, int]
    warnings: list[str] = field(default_factory=list)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def validate_file(path: Path, max_mb: int) -> str:
    """Step 1: file type, size and format checks. Returns the extension."""
    ext = path.suffix.lower()
    if ext not in ALLOWED_EXT:
        raise IngestError(f"Unsupported file type {ext or '(none)'}. Allowed: {', '.join(sorted(ALLOWED_EXT))}.")
    size = path.stat().st_size
    if size == 0:
        raise IngestError("The file is empty.")
    if size > max_mb * 1024 * 1024:
        raise IngestError(f"The file is larger than {max_mb} MB.")
    with open(path, "rb") as f:
        head = f.read(8)
    if ext == ".pdf" and not head.startswith(b"%PDF"):
        raise IngestError("The file is not a valid PDF.")
    if ext in {".docx", ".xlsx"} and not zipfile.is_zipfile(path):
        raise IngestError(f"The file is not a valid {ext} file.")
    if ext == ".xls" and not head.startswith(b"\xd0\xcf\x11\xe0"):
        raise IngestError("The file is not a valid .xls file.")
    return ext


def _doc_identity(meta: dict[str, str], title: str, filename: str) -> tuple[str, str, str]:
    doc_id = (meta.get("Document ID") or "").strip() or slugify(Path(filename).stem)
    name = (meta.get("Title") or "").strip() or title
    version = (meta.get("Version") or "").strip() or "n/a"
    return doc_id, name, version


@dataclass
class IngestContext:
    settings: Settings
    embedder: object  # has .embed_passages(list[str]) -> list[EmbeddedText]
    store: object  # has .replace_document(doc_id, chunks, embeddings)
    cache: object  # has .invalidate_document(doc_id) and .clear_not_found()


def ingest_file(
    path: Path,
    ctx: IngestContext,
    progress: Optional[Progress] = None,
    classification: str = "unlabelled",
    display_name: Optional[str] = None,
) -> IngestResult:
    s = ctx.settings
    say: Progress = progress or (lambda *_: None)
    filename = display_name or path.name
    warnings: list[str] = []

    # 1. validate
    say("validate", 5, "Checking file type, size and format")
    ext = validate_file(path, s.max_upload_mb)
    file_hash = file_sha256(path)

    # 2. extract text (parser or OCR is not needed for these formats)
    say("extract", 15, "Reading structure, tables and pages")
    structure: Optional[DocStructure] = None
    excel = None
    try:
        if ext == ".pdf":
            structure = parse_pdf(path)
        elif ext == ".docx":
            structure = parse_docx(path)
            if s.convert_docx_to_pdf:
                pdf = docx_to_pdf(path, s.data_dir / "work", s.soffice_bin)
                if pdf:
                    assign_pages(structure, pdf_page_texts(pdf))
                else:
                    warnings.append("LibreOffice not available: Word file is cited by section only.")
        elif ext in {".html", ".htm"}:
            structure = parse_html(path)
        elif ext in {".txt", ".md"}:
            structure = parse_text(path)
        elif ext == ".xlsx":
            excel = read_xlsx(path)
        elif ext == ".xls":
            excel = read_xls(path)
    except IngestError:
        raise
    except Exception as exc:  # parser errors become a readable message
        log.exception("Parsing failed for %s", filename)
        raise IngestError(f"Could not read the file: {exc}") from exc

    meta_dict = (structure.meta if structure else excel.meta) if (structure or excel) else {}
    title = structure.title if structure else (meta_dict.get("Title") or path.stem)
    doc_id, doc_name, version = _doc_identity(meta_dict, title, filename)

    # 3. access label
    policy: DocPolicy = find_policy(filename, doc_id) or default_policy(
    classification
    )
    doc = DocMeta(
        doc_id=doc_id, doc_name=doc_name, doc_type=ext.lstrip("."), version=version,
        source_file=filename, file_hash=file_hash, classification=policy.classification,
        allowed_roles=list(policy.roles), org=policy.org,
    )

    # 4. clean and split into chunks (loaders already normalised text)
    say("chunk", 40, "Splitting into chunks and adding label cards")
    managers: set[str] = set()
    if structure is not None:
        chunks = chunk_structure(doc, structure, s.chunk_max_tokens, s.chunk_overlap_tokens, policy.section_rules)
        # tiny front-matter chunks carry no answers
        chunks = [c for c in chunks if not (c.section == "Document control" and not c.section_path[1:] and len(c.text) < 120)]
    else:
        chunks, managers = chunk_excel(doc, excel, policy.sheet_rules, s.chunk_max_tokens)
    if not chunks:
        raise IngestError("No text could be extracted from the file.")

    # 5. embed
    say("embed", 55, f"Embedding {len(chunks)} chunks")
    embeddings = []
    batch = 16
    for i in range(0, len(chunks), batch):
        part = chunks[i:i + batch]
        embeddings.extend(ctx.embedder.embed_passages([c.embed_text for c in part]))
        say("embed", 55 + int(30 * min(1.0, (i + batch) / len(chunks))), f"Embedded {min(i + batch, len(chunks))} of {len(chunks)}")

    # 6. store, replacing any earlier version of this document
    say("store", 90, "Saving to the vector database")
    ctx.store.replace_document(doc_id, chunks, embeddings)
    if managers:
        from ..directory import load_managers

        save_managers(s.data_dir, set(load_managers(s.data_dir)) | managers)
    ctx.cache.invalidate_document(doc_id)
    ctx.cache.clear_not_found()  # a new document may hold an answer that was "Not found"

    strategies: dict[str, int] = {}
    for c in chunks:
        strategies[c.strategy] = strategies.get(c.strategy, 0) + 1
    say("done", 100, f"Stored {len(chunks)} chunks")
    return IngestResult(
        doc_id=doc_id, doc_name=doc_name, doc_type=doc.doc_type, classification=doc.classification,
        chunk_count=len(chunks), strategies=strategies, warnings=warnings,
    )

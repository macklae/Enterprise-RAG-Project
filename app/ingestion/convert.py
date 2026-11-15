"""Word to PDF conversion (LibreOffice) so Word files get page numbers."""
from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from ..models import DocStructure
from .text_utils import norm_key

log = logging.getLogger(__name__)

_MAC_CANDIDATES = [
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/opt/homebrew/bin/soffice",
    "/usr/local/bin/soffice",
]


def find_soffice(configured: str = "soffice") -> Optional[str]:
    found = shutil.which(configured)
    if found:
        return found
    if platform.system() == "Darwin":
        for c in _MAC_CANDIDATES:
            if Path(c).exists():
                return c
    return None


def docx_to_pdf(src: Path, out_dir: Path, soffice: str = "soffice", timeout: int = 180) -> Optional[Path]:
    """Return the PDF path, or None when LibreOffice is missing or fails."""
    exe = find_soffice(soffice)
    if not exe:
        log.warning("LibreOffice not found. Word files will be cited by section only.")
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [exe, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(src)],
            check=True, capture_output=True, timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("Word to PDF conversion failed for %s: %s", src.name, exc)
        return None
    pdf = out_dir / (src.stem + ".pdf")
    return pdf if pdf.exists() else None


def assign_pages(structure: DocStructure, page_texts: list[str]) -> None:
    """Give every section and block a page number by finding its first words in the PDF text.
    Pages only move forward, so repeated phrases resolve to the right place."""
    pages = [norm_key(t) for t in page_texts]
    cursor = 0

    def locate(snippet: str) -> Optional[int]:
        nonlocal cursor
        key = norm_key(snippet)[:48]
        if len(key) < 6:
            return None
        for i in range(cursor, len(pages)):
            if key in pages[i]:
                cursor = i
                return i + 1
        return None

    for sec in structure.sections:
        found = locate(sec.label) if sec.number or sec.title != "Document control" else None
        sec.page = found or (pages and (cursor + 1)) or None
        last = sec.page
        for b in sec.blocks:
            snippet = b.text if b.kind == "text" else " ".join(b.rows[0]) if b.rows else ""
            pg = locate(snippet)
            b.page = pg or last
            last = b.page

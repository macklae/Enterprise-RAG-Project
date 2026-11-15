"""Chunking strategies.

Structure-aware (main): one section = one chunk. Tables stay with their header row
and with the sentence that introduces them. Long tables are split by rows and every
part repeats the header row.

Recursive (backup): paragraph, then line, then sentence cuts, with a small overlap.
Used only when a single paragraph is longer than the chunk limit.

Row-based and sheet-based chunking for Excel lives in `loaders_excel.py`.
"""
from __future__ import annotations

import uuid
from typing import Optional

from ..models import Block, Chunk, DocMeta, DocStructure, Section
from .text_utils import est_tokens, render_table

_NS = uuid.UUID("6f1c9b0e-3c1e-4b43-9a0e-5d3b2f7a9c11")
SEPARATORS = ["\n\n", "\n", ". ", "; ", ", ", " "]


def make_chunk_id(doc_id: str, location: str, file_hash: str) -> str:
    """Stable id. Re-uploading the same file gives the same ids."""
    return str(uuid.uuid5(_NS, f"{doc_id}|{location}|{file_hash}"))


# --------------------------------------------------------------------------- recursive
def _split_rec(text: str, max_chars: int, seps: list[str]) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    for i, sep in enumerate(seps):
        if sep in text:
            out: list[str] = []
            cur = ""
            for part in text.split(sep):
                piece = part if not cur else cur + sep + part
                if len(piece) <= max_chars:
                    cur = piece
                    continue
                if cur:
                    out.append(cur)
                if len(part) > max_chars:
                    out.extend(_split_rec(part, max_chars, seps[i + 1:]))
                    cur = ""
                else:
                    cur = part
            if cur:
                out.append(cur)
            return out
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def recursive_split(text: str, max_tokens: int, overlap_tokens: int = 0) -> list[str]:
    max_chars = max(80, max_tokens * 4)
    pieces = [p.strip() for p in _split_rec(text.strip(), max_chars, SEPARATORS) if p.strip()]
    if overlap_tokens <= 0 or len(pieces) < 2:
        return pieces
    overlap_chars = overlap_tokens * 4
    out = [pieces[0]]
    for prev, cur in zip(pieces, pieces[1:]):
        tail = prev[-overlap_chars:]
        cut = tail.find(" ")
        tail = tail[cut + 1:] if 0 <= cut < len(tail) - 1 else tail
        out.append((tail + " " + cur).strip())
    return out


# --------------------------------------------------------------------------- structure
def _split_table(rows: list[list[str]], max_tokens: int) -> list[str]:
    """Split a long table by rows. Each part repeats the header row."""
    header, body = rows[0], rows[1:]
    parts: list[str] = []
    cur: list[list[str]] = []
    for row in body:
        trial = render_table(cur + [row], header=header)
        if cur and est_tokens(trial) > max_tokens:
            parts.append(render_table(cur, header=header))
            cur = [row]
        else:
            cur.append(row)
    if cur or not parts:
        parts.append(render_table(cur, header=header))
    return parts


def _units(section: Section, max_tokens: int, overlap: int) -> list[tuple[str, str, Optional[int], str]]:
    """Turn blocks into (kind, text, page, strategy) units that each fit the limit."""
    units: list[tuple[str, str, Optional[int], str]] = []
    for b in section.blocks:
        if b.kind == "table":
            if not b.rows:
                continue
            full = render_table(b.rows)
            if est_tokens(full) <= max_tokens:
                units.append(("table", full, b.page, "structure"))
            else:
                for part in _split_table(b.rows, max_tokens):
                    units.append(("table", part, b.page, "structure"))
        else:
            if not b.text.strip():
                continue
            if est_tokens(b.text) <= max_tokens:
                units.append(("text", b.text.strip(), b.page, "structure"))
            else:
                for part in recursive_split(b.text, max_tokens, overlap):
                    units.append(("text", part, b.page, "recursive"))
    return units


def chunk_section(
    doc: DocMeta,
    section: Section,
    max_tokens: int,
    overlap: int,
    restricted_override: Optional[tuple[str, list[str]]] = None,
) -> list[Chunk]:
    units = _units(section, max_tokens, overlap)
    if not units:
        return []

    heading_line = section.label or "Document control"
    budget = max_tokens - est_tokens(heading_line) - 4

    groups: list[list[tuple[str, str, Optional[int], str]]] = []
    cur: list[tuple[str, str, Optional[int], str]] = []
    size = 0
    for u in units:
        t = est_tokens(u[1])
        if cur and size + t > budget:
            carry = None
            # keep a short introducing sentence together with the table that follows it
            if u[0] == "table" and cur[-1][0] == "text" and est_tokens(cur[-1][1]) <= 100 and len(cur) > 1:
                carry = cur.pop()
            groups.append(cur)
            cur = [carry] if carry else []
            size = sum(est_tokens(x[1]) for x in cur)
        cur.append(u)
        size += t
    if cur:
        groups.append(cur)

    chunks: list[Chunk] = []
    trail = " > ".join(section.path) if section.path else heading_line
    for i, g in enumerate(groups):
        head = heading_line if i == 0 else f"{heading_line} (continued)"
        body = "\n\n".join(x[1] for x in g)
        text = f"{head}\n{body}"
        pages = [x[2] for x in g if x[2] is not None]
        strategy = "recursive" if any(x[3] == "recursive" for x in g) else "structure"
        context = f"{doc.doc_name} ({doc.doc_id}) | Section: {trail}"
        c = Chunk(
            chunk_id=make_chunk_id(doc.doc_id, f"{section.label}|{i}", doc.file_hash),
            text=text,
            context=context,
            doc=doc,
            strategy=strategy,
            section=section.label or "Document control",
            section_path=list(section.path),
            page=min(pages) if pages else section.page,
            page_end=max(pages) if pages else section.page,
        )
        if restricted_override:
            c.classification, c.allowed_roles = restricted_override[0], list(restricted_override[1])
        chunks.append(c)
    return chunks


def chunk_structure(
    doc: DocMeta,
    structure: DocStructure,
    max_tokens: int,
    overlap: int,
    section_rules: Optional[list[tuple[str, str, list[str]]]] = None,
) -> list[Chunk]:
    """Chunk a whole document. `section_rules` is a list of (heading_regex, classification, roles)
    applied to the first matching heading and to every later section (an appendix, say)."""
    import re

    chunks: list[Chunk] = []
    active: Optional[tuple[str, list[str]]] = None
    for sec in structure.sections:
        matched = False
        for pattern, cls, roles in section_rules or []:
            if re.search(pattern, sec.label, re.I):
                active, matched = (cls, roles), True
        if not matched and len(sec.path) <= 1:
            active = None  # a new top-level section without the label ends the rule
        chunks.extend(chunk_section(doc, sec, max_tokens, overlap, active))
    return chunks

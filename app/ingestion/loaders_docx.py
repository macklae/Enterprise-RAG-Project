"""Route A (Word): headings from styles or numbering, tables, lists."""
from __future__ import annotations

import re
from pathlib import Path

from ..models import Block, DocStructure, Section
from .text_utils import clean_cell, clean_text, drop_pii_columns, merge_split_tables

NUM_HEADING_RE = re.compile(r"^(?P<num>\d+(?:\.\d+)+)\s+(?P<title>[A-Z].{0,90})$")
TOP_HEADING_RE = re.compile(r"^(?:(?P<num>\d+)\.|(?P<app>Appendix\s+[A-Z]\.?))\s+(?P<title>\S.{0,110})$")
META_KEYS = {"document id", "title", "version", "classification", "owner", "effective date", "supersedes"}


def _style_name(p) -> str:
    try:
        return p.style.name or ""
    except Exception:
        return ""


def _list_kind(doc, p) -> str | None:
    """Return 'number', 'bullet' or None for a paragraph."""
    ppr = p._p.pPr
    if ppr is None or ppr.numPr is None:
        return "bullet" if _style_name(p).lower().startswith("list") else None
    try:
        num_id = ppr.numPr.numId.val
        root = doc.part.numbering_part.element
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        abs_id = root.xpath(f"w:num[@w:numId='{num_id}']/w:abstractNumId/@w:val", namespaces=ns)
        if abs_id:
            fmt = root.xpath(
                f"w:abstractNum[@w:abstractNumId='{abs_id[0]}']/w:lvl[@w:ilvl='0']/w:numFmt/@w:val", namespaces=ns
            )
            if fmt and fmt[0] != "bullet":
                return "number"
    except Exception:
        pass
    return "bullet"


def parse_docx(path: Path) -> DocStructure:
    import docx
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = docx.Document(str(path))
    title = ""
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    current = Section(number="", title="Document control", path=["Document control"])
    list_counter = 0

    def close():
        current.blocks = merge_split_tables(current.blocks)
        if current.blocks:
            sections.append(current)

    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            p = Paragraph(child, doc)
            text = clean_text(p.text)
            if not text:
                list_counter = 0
                continue
            style = _style_name(p)
            level = None
            num, name = "", text
            if style == "Title":
                title = title or text
                continue
            m_style = re.match(r"Heading (\d)", style)
            if m_style:
                level = int(m_style.group(1))
                m = TOP_HEADING_RE.match(text) or NUM_HEADING_RE.match(text)
                if m:
                    num = (m.groupdict().get("num") or m.groupdict().get("app") or "").rstrip(".")
                    name = m.group("title")
            else:
                m = NUM_HEADING_RE.match(text)
                if m and len(text) <= 90 and not text.endswith("."):
                    num, name = m.group("num"), m.group("title")
                    level = num.count(".") + 1
            if level is not None:
                close()
                depth = (num.count(".") + 1) if num and num[0].isdigit() else level
                while stack and stack[-1][0] >= depth:
                    stack.pop()
                label = Section(number=num, title=name, path=[]).label
                stack.append((depth, label))
                current = Section(number=num, title=name, path=[s[1] for s in stack])
                list_counter = 0
                continue
            kind = _list_kind(doc, p)
            if kind:
                list_counter += 1
                bullet = f"{list_counter}. " if kind == "number" else "- "
                current.blocks.append(Block(kind="text", text=bullet + text))
            else:
                list_counter = 0
                current.blocks.append(Block(kind="text", text=text))
        elif child.tag == qn("w:tbl"):
            list_counter = 0
            t = Table(child, doc)
            rows: list[list[str]] = []
            for r in t.rows:
                cells, seen = [], set()
                for c in r.cells:
                    if id(c._tc) in seen:
                        continue  # merged cell repeats
                    seen.add(id(c._tc))
                    cells.append(clean_cell(c.text))
                if any(cells):
                    rows.append(cells)
            rows = drop_pii_columns(rows)
            if rows:
                current.blocks.append(Block(kind="table", rows=rows))
    close()

    meta: dict[str, str] = {}
    for sec in sections:
        for b in sec.blocks:
            if b.kind == "table" and b.rows and len(b.rows[0]) == 2:
                for r in b.rows:
                    key = r[0].strip()
                    if key.lower() in META_KEYS and key not in meta:
                        meta[key] = r[1].strip()
    return DocStructure(title=title or path.stem.replace("_", " "), sections=sections, meta=meta)

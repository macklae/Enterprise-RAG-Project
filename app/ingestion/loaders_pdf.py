"""Route A (PDF): headings, paragraphs and tables with page numbers."""
from __future__ import annotations

import re
from pathlib import Path

from ..models import Block, DocStructure, Section
from .text_utils import clean_cell, clean_text, drop_pii_columns, merge_split_tables

HEADING_RE = re.compile(r"^(?:(?P<num>\d+(?:\.\d+)*)\.?|(?P<app>Appendix\s+[A-Z]\.?))\s+(?P<title>\S.*)$")
LIST_NUM_RE = re.compile(r"^(\d+)\s+(?=\S)")
META_KEYS = {"document id", "title", "version", "classification", "owner", "effective date", "supersedes"}


def _in_boxes(obj, boxes) -> bool:
    cx = (obj["x0"] + obj["x1"]) / 2
    cy = (obj["top"] + obj["bottom"]) / 2
    return any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in boxes)


def _is_bold(line) -> bool:
    fonts = {c["fontname"] for c in line["chars"] if c["text"].strip()}
    return bool(fonts) and all("bold" in f.lower() for f in fonts)


def _line_size(line) -> float:
    sizes = [c["size"] for c in line["chars"] if c["text"].strip()]
    return max(sizes) if sizes else 0.0


def _table_rows(raw) -> list[list[str]]:
    rows = [[clean_cell(c) for c in r] for r in raw if r and any((c or "").strip() for c in r)]
    return drop_pii_columns(rows)


def parse_pdf(path: Path) -> DocStructure:
    import pdfplumber

    items: list[dict] = []  # reading-order items across the whole document
    with pdfplumber.open(str(path)) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            h = float(page.height)
            tables = page.find_tables()
            boxes = [t.bbox for t in tables]

            def keep(obj, boxes=boxes):
                return obj.get("object_type") != "char" or not _in_boxes(obj, boxes)

            for t in tables:
                rows = _table_rows(t.extract())
                if rows:
                    items.append({"kind": "table", "top": t.bbox[1], "page": pno, "rows": rows})
            lines = page.filter(keep).extract_text_lines(return_chars=True)
            for ln in lines:
                if ln["top"] < 0.06 * h or ln["bottom"] > 0.94 * h:
                    continue  # running header and footer
                items.append({
                    "kind": "line", "top": ln["top"], "bottom": ln["bottom"], "x0": ln["x0"], "page": pno,
                    "text": clean_text(ln["text"]), "bold": _is_bold(ln), "size": _line_size(ln),
                })
    items.sort(key=lambda i: (i["page"], i["top"]))

    title = ""
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []  # (depth, label)
    current = Section(number="", title="Document control", path=["Document control"], page=1)
    para: list[dict] = []

    def flush_para():
        nonlocal para
        if not para:
            return
        text = " ".join(p["text"] for p in para).strip()
        if text:
            if LIST_NUM_RE.match(text) and para[0]["x0"] < 61:
                text = LIST_NUM_RE.sub(lambda m: f"{m.group(1)}. ", text, count=1)
            current.blocks.append(Block(kind="text", text=text, page=para[0]["page"]))
        para = []

    def close_section():
        flush_para()
        current.blocks = merge_split_tables(current.blocks)
        if current.blocks:
            sections.append(current)

    prev = None
    for it in items:
        if it["kind"] == "table":
            flush_para()
            current.blocks.append(Block(kind="table", rows=it["rows"], page=it["page"]))
            prev = None
            continue
        text = it["text"]
        if not text:
            continue
        if it["bold"] and it["size"] >= 17 and not title:
            title = text
            prev = None
            continue
        m = HEADING_RE.match(text)
        if it["bold"] and 10.5 <= it["size"] <= 16 and (m or it["size"] >= 12):
            close_section()
            num = ""
            name = text
            if m:
                num = (m.group("num") or m.group("app") or "").rstrip(".")
                name = m.group("title")
            depth = (num.count(".") + 1) if re.match(r"^\d", num) else 1
            while stack and stack[-1][0] >= depth:
                stack.pop()
            label = Section(number=num, title=name, path=[]).label
            stack.append((depth, label))
            current = Section(number=num, title=name, path=[s[1] for s in stack], page=it["page"])
            prev = None
            continue
        new_para = (
            prev is None
            or it["page"] != prev["page"]
            or it["top"] - prev["bottom"] > 6
        )
        if new_para:
            flush_para()
        para.append(it)
        prev = it
    close_section()

    meta: dict[str, str] = {}
    for sec in sections:
        for b in sec.blocks:
            if b.kind == "table" and b.rows and len(b.rows[0]) == 2:
                for r in b.rows:
                    if r[0].strip().lower() in META_KEYS and r[0].strip().lower() not in {k.lower() for k in meta}:
                        meta[r[0].strip()] = r[1].strip()
    return DocStructure(title=title or path.stem.replace("_", " "), sections=sections, meta=meta)


def pdf_page_texts(path: Path) -> list[str]:
    import pdfplumber

    with pdfplumber.open(str(path)) as pdf:
        return [(p.extract_text() or "") for p in pdf.pages]

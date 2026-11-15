"""Route C (web pages): remove menus, scripts and styles, then cut at headings."""
from __future__ import annotations

import re
from pathlib import Path

from ..models import Block, DocStructure, Section
from .text_utils import clean_cell, clean_text, drop_pii_columns, merge_split_tables

DROP_TAGS = ["script", "style", "noscript", "svg", "nav", "footer", "form", "button", "iframe", "template", "canvas", "select", "option"]
BLOCK_TAGS = ["p", "div", "section", "article", "li", "ul", "ol", "br", "tr", "blockquote", "pre", "figure", "figcaption", "dt", "dd", "details", "summary"]
H_MARK = "§§H"
T_MARK = "§§T"


def parse_html(path: Path) -> DocStructure:
    from bs4 import BeautifulSoup, NavigableString

    html = path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    page_title = clean_cell(soup.title.get_text()) if soup.title else ""
    for t in soup(DROP_TAGS):
        t.decompose()
    for t in soup.find_all(attrs={"hidden": True}):
        t.decompose()

    tables: list[list[list[str]]] = []
    for tbl in soup.find_all("table"):
        rows = []
        for tr in tbl.find_all("tr"):
            cells = [clean_cell(c.get_text(" ")) for c in tr.find_all(["th", "td"])]
            if any(cells):
                rows.append(cells)
        rows = drop_pii_columns(rows)
        tables.append(rows)
        tbl.replace_with(NavigableString(f"\n{T_MARK}{len(tables) - 1}\n"))

    for h in soup.find_all(["h1", "h2", "h3", "h4", "h5"]):
        level = int(h.name[1])
        text = clean_cell(h.get_text(" "))
        h.replace_with(NavigableString(f"\n{H_MARK}{level}|{text}\n" if text else "\n"))

    for li in soup.find_all("li"):
        li.insert(0, NavigableString("- "))
    for t in soup.find_all(BLOCK_TAGS):
        t.insert_before(NavigableString("\n"))
        t.insert_after(NavigableString("\n"))

    body = soup.body or soup
    text = body.get_text("")

    title = ""
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    current = Section(number="", title=page_title or path.stem, path=[page_title or path.stem])
    para: list[str] = []
    last_text = ""

    def flush():
        nonlocal para, last_text
        t = clean_text(" ".join(para))
        para = []
        if t and t != last_text:  # skip repeated blocks (tabs, duplicated widgets)
            current.blocks.append(Block(kind="text", text=t))
            last_text = t

    def close():
        flush()
        current.blocks = merge_split_tables(current.blocks)
        if current.blocks:
            sections.append(current)

    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith(H_MARK):
            level_s, _, name = line[len(H_MARK):].partition("|")
            level = int(level_s)
            if level == 1 and not title:
                title = name
            close()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, name))
            current = Section(number="", title=name, path=[s[1] for s in stack])
            continue
        if line.startswith(T_MARK):
            flush()
            rows = tables[int(line[len(T_MARK):])]
            if rows:
                current.blocks.append(Block(kind="table", rows=rows))
            continue
        if line.startswith("- "):
            flush()
            current.blocks.append(Block(kind="text", text=clean_text(line)))
            continue
        para.append(line)
    close()

    return DocStructure(title=title or page_title or path.stem, sections=sections, meta={})

"""Plain text and Markdown. Markdown headings become sections. Plain text relies on the recursive splitter."""
from __future__ import annotations

import re
from pathlib import Path

from ..models import Block, DocStructure, Section
from .text_utils import clean_text

MD_HEADING = re.compile(r"^(#{1,5})\s+(.+?)\s*$")


def parse_text(path: Path) -> DocStructure:
    raw = path.read_text(encoding="utf-8", errors="replace")
    title = path.stem.replace("_", " ")
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    current = Section(number="", title=title, path=[title])
    para: list[str] = []

    def flush():
        nonlocal para
        t = clean_text("\n".join(para))
        para = []
        if t:
            current.blocks.append(Block(kind="text", text=t))

    def close():
        flush()
        if current.blocks:
            sections.append(current)

    for line in raw.splitlines():
        m = MD_HEADING.match(line)
        if m:
            close()
            level, name = len(m.group(1)), m.group(2)
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, name))
            current = Section(number="", title=name, path=[s[1] for s in stack])
        elif not line.strip():
            flush()
        else:
            para.append(line)
    close()
    return DocStructure(title=title, sections=sections, meta={})

"""Small text helpers used by every loader and by the chunker."""
from __future__ import annotations

import re
import unicodedata

# Columns that hold personal phone numbers. They are dropped at load time.
PII_COLUMN_RE = re.compile(r"^(mobile|mobile number|phone|phone number|cell|contact number)$", re.I)

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)
_SMART = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", " ": " ", "•": "-",
}


def clean_text(text: str) -> str:
    """Normalise encoding, remove invisible characters and collapse blanks."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    for a, b in _SMART.items():
        text = text.replace(a, b)
    text = text.replace("(cid:127)", "-")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_cell(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", clean_text(str(value))).strip()


def est_tokens(text: str) -> int:
    """Cheap token estimate (about 4 characters per token)."""
    return max(1, (len(text) + 3) // 4) if text else 0


def norm_key(text: str) -> str:
    """Lower-case letters and digits only. Used to match text across formats."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def render_table(rows: list[list[str]], header: list[str] | None = None) -> str:
    """Render rows as a Markdown table. The first row is the header unless one is given."""
    if not rows and not header:
        return ""
    if header is None:
        header, body = rows[0], rows[1:]
    else:
        body = rows
    width = max([len(header)] + [len(r) for r in body]) if (header or body) else 0

    def fmt(r: list[str]) -> str:
        cells = [(c or "").replace("|", "/") for c in r] + [""] * (width - len(r))
        return "| " + " | ".join(cells) + " |"

    lines = [fmt(header), "| " + " | ".join(["---"] * width) + " |"]
    lines += [fmt(r) for r in body]
    return "\n".join(lines)


def drop_pii_columns(rows: list[list[str]]) -> list[list[str]]:
    """Remove columns whose header looks like a personal phone number."""
    if not rows:
        return rows
    drop = {i for i, h in enumerate(rows[0]) if PII_COLUMN_RE.match((h or "").strip())}
    if not drop:
        return rows
    return [[c for i, c in enumerate(r) if i not in drop] for r in rows]


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def merge_split_tables(blocks):
    """A table that runs across a page break shows up as two tables with the header repeated.
    Join them into one and drop the repeated header row."""
    from ..models import Block

    out: list[Block] = []
    for b in blocks:
        if (
            b.kind == "table" and out and out[-1].kind == "table"
            and out[-1].rows and b.rows and out[-1].rows[0] == b.rows[0]
        ):
            out[-1] = Block(kind="table", rows=out[-1].rows + b.rows[1:], page=out[-1].page)
        else:
            out.append(b)
    return out

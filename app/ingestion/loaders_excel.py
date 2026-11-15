"""Route B (Excel .xlsx and .xls): one chunk per row, or one chunk for a small reference sheet."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..models import Chunk, DocMeta
from .chunking import make_chunk_id
from .policy import DocPolicy, SheetRule
from .text_utils import PII_COLUMN_RE, clean_cell, drop_pii_columns, est_tokens, render_table

META_KEYS = {"document id", "title", "version", "classification", "owner", "last updated"}
DATE_HINTS = ("date", "completed", "joining")


@dataclass
class Sheet:
    name: str
    rows: list[list[str]]  # blank cells are "" ; row 0 is the header row


@dataclass
class ExcelContent:
    sheets: list[Sheet]
    meta: dict[str, str] = field(default_factory=dict)


def _fmt_date(d) -> str:
    if isinstance(d, dt.datetime):
        d = d.date()
    return d.strftime("%d %b %Y")


def _excel_serial_to_date(v: float) -> str:
    return _fmt_date(dt.date(1899, 12, 30) + dt.timedelta(days=int(v)))


def _fmt_value(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dt.datetime, dt.date)):
        return _fmt_date(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return clean_cell(v)


def _fix_serial_dates(rows: list[list]) -> list[list]:
    """Some files store dates as plain numbers. Convert those columns using the header text."""
    if not rows:
        return rows
    hints = [any(h in str(c).lower() for h in DATE_HINTS) for c in rows[0]]
    out = [rows[0]]
    for r in rows[1:]:
        new = []
        for i, v in enumerate(r):
            if i < len(hints) and hints[i] and isinstance(v, (int, float)) and not isinstance(v, bool) and 20000 < v < 80000:
                new.append(_excel_serial_to_date(v))
            else:
                new.append(v)
        out.append(new)
    return out


def read_xlsx(path: Path) -> ExcelContent:
    import openpyxl

    wb = openpyxl.load_workbook(str(path), data_only=True)  # saved formula results
    sheets = []
    for ws in wb.worksheets:
        raw = [[c for c in row] for row in ws.iter_rows(values_only=True)]
        raw = _fix_serial_dates(raw)
        rows = [[_fmt_value(c) for c in r] for r in raw]
        sheets.append(Sheet(ws.title, rows))
    return ExcelContent(sheets, _meta_from(sheets))


def read_xls(path: Path) -> ExcelContent:
    import xlrd

    book = xlrd.open_workbook(str(path))
    sheets = []
    for s in book.sheets():
        raw: list[list] = []
        for r in range(s.nrows):
            row = []
            for c in range(s.ncols):
                cell = s.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    row.append(xlrd.xldate_as_datetime(cell.value, book.datemode))
                elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    row.append("")
                else:
                    row.append(cell.value)
            raw.append(row)
        raw = _fix_serial_dates(raw)
        sheets.append(Sheet(s.name, [[_fmt_value(c) for c in r] for r in raw]))
    return ExcelContent(sheets, _meta_from(sheets))


def _meta_from(sheets: list[Sheet]) -> dict[str, str]:
    meta: dict[str, str] = {}
    for s in sheets:
        for r in s.rows:
            if len(r) >= 2 and r[0].strip().lower() in META_KEYS and r[0].strip() not in meta and r[1]:
                meta[r[0].strip()] = r[1].strip()
    return meta


# ------------------------------------------------------------------------------------ chunking
def _blank_label(header: str, row: dict[str, str]) -> str:
    base = header.lower().replace("completed", "").strip()
    for h, v in row.items():
        if h.lower().replace("required", "").strip() == base and "required" in h.lower() and v.strip().lower() == "no":
            return "not required"
    return "not completed"


def _lookup(content: ExcelContent, sheet: str, key_col: str) -> dict[str, dict[str, str]]:
    for s in content.sheets:
        if s.name == sheet and s.rows:
            head = s.rows[0]
            if key_col not in head:
                return {}
            k = head.index(key_col)
            return {r[k]: dict(zip(head, r)) for r in s.rows[1:] if len(r) > k and r[k]}
    return {}


def _about_text(sheet: Sheet) -> str:
    lines = []
    for r in sheet.rows:
        cells = [c for c in r if c]
        if not cells or r[:2] == ["Field", "Detail"]:
            continue
        lines.append(f"{cells[0]}: {cells[1]}" if len(cells) >= 2 else cells[0])
    return "\n".join(lines)


def chunk_excel(
    doc: DocMeta,
    content: ExcelContent,
    rules: dict[str, SheetRule],
    max_tokens: int,
) -> tuple[list[Chunk], set[str]]:
    """Returns (chunks, manager names found in team-scoped sheets)."""
    chunks: list[Chunk] = []
    managers: set[str] = set()

    for sheet in content.sheets:
        rule = rules.get(sheet.name, SheetRule())
        rows = [r for r in sheet.rows if any(c for c in r)]
        if not rows:
            continue
        context = f"{doc.doc_name} ({doc.doc_id}) | Sheet: {sheet.name}"

        def apply_rule(c: Chunk, rule=rule):
            if rule.classification:
                c.classification = rule.classification
            if rule.roles:
                c.allowed_roles = list(rule.roles)

        # ---- one chunk for the whole sheet -------------------------------------------
        mode = rule.mode
        if mode == "auto":
            mode = "sheet" if len(rows) <= 12 else "row"
        if mode == "sheet":
            if sheet.name.lower() == "about":
                body = _about_text(Sheet(sheet.name, rows))
            else:
                body = render_table(drop_pii_columns(rows))
            text = f"Sheet: {sheet.name}\n{body}"
            if est_tokens(text) <= max_tokens * 1.5:
                c = Chunk(
                    chunk_id=make_chunk_id(doc.doc_id, f"sheet|{sheet.name}", doc.file_hash),
                    text=text, context=context, doc=doc, strategy="sheet",
                    section=f"{sheet.name} sheet", sheet=sheet.name, row=None,
                )
                apply_rule(c)
                chunks.append(c)
                continue
            mode = "row"

        # ---- one chunk per row ---------------------------------------------------------
        raw = sheet.rows
        h_idx = next(i for i, r in enumerate(raw) if any(r))
        raw_header = raw[h_idx]
        keep = [i for i, h in enumerate(raw_header) if not PII_COLUMN_RE.match(h.strip())]
        header = [raw_header[i] for i in keep]
        join = _lookup(content, rule.join_sheet, rule.join_key) if rule.join_sheet and rule.join_key else {}

        for rownum, r in enumerate(raw[h_idx + 1:], start=h_idx + 2):  # Excel rows start at 1
            if not any(r):
                continue
            row = {header[j]: (r[i] if i < len(r) else "") for j, i in enumerate(keep)}
            extra: dict[str, str] = {}
            if join and rule.join_key in row:
                other = join.get(row[rule.join_key], {})
                extra = {k: other.get(k, "") for k in rule.join_columns if k not in row}
            facts = []
            for h, v in {**extra, **row}.items():
                if not v and rule.blank_completed and "completed" in h.lower():
                    v = _blank_label(h, row)
                if v:
                    facts.append(f"{h}: {v}")
            team = "*"
            if rule.team_column:
                team = (row.get(rule.team_column) or extra.get(rule.team_column) or "").strip() or "*"
                if team != "*":
                    managers.add(team)
            c = Chunk(
                chunk_id=make_chunk_id(doc.doc_id, f"row|{sheet.name}|{rownum}", doc.file_hash),
                text=f"{sheet.name} sheet, row {rownum}\n" + "; ".join(facts),
                context=context, doc=doc, strategy="row",
                section=f"{sheet.name} sheet", sheet=sheet.name, row=rownum, team_manager=team,
            )
            apply_rule(c)
            chunks.append(c)
    return chunks, managers

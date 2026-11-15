"""Access labels and per-document rules.

Each rule matches a file by name or by Document ID. It sets who may see the document
and how Excel sheets are chunked. Files that match no rule use the classification chosen
at upload time (default: Internal).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..access import ALL_ROLES, DEFAULT_ROLES_BY_CLASS


@dataclass
class SheetRule:
    mode: str = "auto"  # "row" | "sheet" | "auto" (one chunk if small, else rows)
    classification: Optional[str] = None
    roles: Optional[list[str]] = None
    team_column: Optional[str] = None  # column holding the manager name for team-scoped rows
    join_sheet: Optional[str] = None  # copy extra columns from another sheet by key
    join_key: Optional[str] = None
    join_columns: list[str] = field(default_factory=list)
    blank_completed: bool = False  # blank "...completed" cell means not completed


@dataclass
class DocPolicy:
    pattern: str
    classification: str
    roles: list[str]
    org: str = "swiftpay"
    name: Optional[str] = None
    sheet_rules: dict[str, SheetRule] = field(default_factory=dict)
    section_rules: list[tuple[str, str, list[str]]] = field(default_factory=list)


POLICIES: list[DocPolicy] = [
    DocPolicy(r"SOP-OPS-001", "internal", list(ALL_ROLES)),
    DocPolicy(r"SOP-OPS-005", "internal", list(ALL_ROLES)),
    DocPolicy(r"POL-HR-010", "internal", list(ALL_ROLES)),
    # Confidential. Appendix B (on-call roster) is Restricted: SRE / Operations only.
    DocPolicy(
        r"RPT-OPS-2026-09", "confidential", ["line_manager", "sre_ops"],
        section_rules=[(r"restricted", "restricted", ["sre_ops"])],
    ),
    DocPolicy(
        r"Service_Catalogue|REG-OPS-003", "internal", list(ALL_ROLES),
        sheet_rules={
            "About": SheetRule(mode="sheet"),
            "Services": SheetRule(mode="row"),
            "Teams": SheetRule(mode="sheet"),
            "Tiers": SheetRule(mode="sheet"),
        },
    ),
    # Restricted. HR / L&D see every row. A Line Manager sees only their own team's rows.
    DocPolicy(
        r"Employee_Training|REG-HR-007", "restricted", ["hr_ld", "line_manager"],
        sheet_rules={
            "About": SheetRule(mode="sheet", classification="restricted", roles=["hr_ld", "line_manager"]),
            "Employees": SheetRule(mode="row", team_column="Manager"),
            "Training": SheetRule(
                mode="row", team_column="Manager", join_sheet="Employees", join_key="Employee ID",
                join_columns=["Team", "Role", "Manager"], blank_completed=True,
            ),
            "Summary": SheetRule(mode="sheet", roles=["hr_ld"]),
        },
    ),
    # Unrelated web pages. No label. Used to test "Not found".
    DocPolicy(r"TimesFM", "unlabelled", list(ALL_ROLES), org="external"),
    DocPolicy(r"kimi", "unlabelled", list(ALL_ROLES), org="external"),
]


def find_policy(filename: str, doc_id: str = "") -> Optional[DocPolicy]:
    for p in POLICIES:
        if re.search(p.pattern, filename, re.I) or (doc_id and re.search(p.pattern, doc_id, re.I)):
            return p
    return None


def default_policy(classification: str = "internal") -> DocPolicy:
    if classification not in DEFAULT_ROLES_BY_CLASS:
        raise ValueError(
            f"Invalid document classification: {classification}"
        )

    return DocPolicy(
        pattern=".*",
        classification=classification,
        roles=list(DEFAULT_ROLES_BY_CLASS[classification]),
    )


# Documents that other files mention but that are not in the knowledge base.
KNOWN_MISSING_DOCS = {"SOP-OPS-002", "SOP-OPS-003", "SOP-OPS-006", "SOP-SEC-004"}
DOC_CODE_RE = re.compile(r"\b(?:SOP|POL|RPT|REG)-[A-Z]{2,4}-\d{3,4}(?:-\d{2})?\b", re.I)

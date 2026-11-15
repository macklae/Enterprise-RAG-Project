"""Roles, access labels and the permission filter.

Every chunk carries `allowed_roles` and `team_manager`. A chunk is visible when:
  1. the user's role is in `allowed_roles`, and
  2. for Line Managers, `team_manager` is "*" or equals the manager's own name.

The same rule is applied twice: inside the Qdrant search (so hidden chunks are
never returned) and again in Python after the search (defence in depth).
"""
from __future__ import annotations

from typing import Any

from .models import Identity

ROLES: dict[str, str] = {
    "employee": "Employee",
    "line_manager": "Line Manager",
    "hr_ld": "HR / L&D",
    "sre_ops": "SRE / Operations",
}
ALL_ROLES = tuple(ROLES)
ADMIN_ROLES = ("hr_ld", "sre_ops")  # may upload and delete documents

TEAM_ANY = "*"

CLASSIFICATIONS = ("internal", "confidential", "restricted", "unlabelled")

# Default audience per classification, used when a policy does not say otherwise.
DEFAULT_ROLES_BY_CLASS: dict[str, list[str]] = {
    "internal": list(ALL_ROLES),
    "unlabelled": [],
    "confidential": ["line_manager", "sre_ops"],
    "restricted": ["sre_ops"],
}


def make_identity(role: str, user: str | None = None) -> Identity:
    role = (role or "").strip()
    if role not in ROLES:
        raise ValueError(f"Unknown role: {role!r}")
    user = (user or "").strip()[:80] or None
    return Identity(role=role, user=user)


def chunk_visible(payload: dict[str, Any], identity: Identity) -> bool:
    """Python mirror of the Qdrant filter."""
    if identity.role not in (payload.get("allowed_roles") or []):
        return False
    if identity.role == "line_manager":
        team = payload.get("team_manager", TEAM_ANY)
        if team != TEAM_ANY and team != (identity.user or ""):
            return False
    return True


def build_qdrant_filter(identity: Identity):
    """Qdrant filter equal to `chunk_visible`."""
    from qdrant_client import models as qm

    must = [qm.FieldCondition(key="allowed_roles", match=qm.MatchValue(value=identity.role))]
    if identity.role == "line_manager":
        allowed_teams = [TEAM_ANY]
        if identity.user:
            allowed_teams.append(identity.user)
        must.append(qm.FieldCondition(key="team_manager", match=qm.MatchAny(any=allowed_teams)))
    return qm.Filter(must=must)

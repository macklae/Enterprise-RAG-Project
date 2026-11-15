"""Line manager names, used by the identity picker. Written when the training records are ingested."""
from __future__ import annotations

import json
from pathlib import Path


def _path(data_dir: Path) -> Path:
    return data_dir / "managers.json"


def save_managers(data_dir: Path, names: set[str]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    _path(data_dir).write_text(json.dumps(sorted(names), indent=2), encoding="utf-8")


def load_managers(data_dir: Path) -> list[str]:
    p = _path(data_dir)
    if not p.exists():
        return []
    try:
        return [str(x) for x in json.loads(p.read_text(encoding="utf-8"))]
    except (OSError, ValueError):
        return []

from __future__ import annotations

from pathlib import Path
from typing import Mapping


REPORT_FIELDS = (
    "state",
    "user outcome",
    "tests",
    "independent review",
    "visual artifacts",
    "residual risk",
    "rollback",
    "pending approval",
)


def render_report(values: Mapping[str, str]) -> str:
    return "\n\n".join(f"## {field}\n\n{values.get(field, 'None')}" for field in REPORT_FIELDS) + "\n"


def write_report(path: Path, values: Mapping[str, str]) -> Path:
    path.write_text(render_report(values), encoding="utf-8")
    return path

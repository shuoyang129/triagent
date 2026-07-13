from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping

from triagent.adapters._cli import REDACTED, sanitize
from triagent.store import TaskStore


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


_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|authorization|credential)\b\s*[:=]\s*[^\s,;]+"
)


def _safe_text(value: str, secrets: tuple[str, ...]) -> str:
    cleaned = sanitize(value, secrets)
    assert isinstance(cleaned, str)
    return _CREDENTIAL_ASSIGNMENT.sub(lambda match: f"{match.group(1)}={REDACTED}", cleaned)


def render_report(values: Mapping[str, str], *, secret_values: tuple[str, ...] = ()) -> str:
    environment_secrets = tuple(
        value for key, value in os.environ.items()
        if value and re.search(r"(?i)(?:api[_-]?key|token|secret|password|credential)", key)
    )
    secrets = tuple(dict.fromkeys([*secret_values, *environment_secrets]))
    return "\n\n".join(
        f"## {field}\n\n{_safe_text(str(values.get(field, 'None')), secrets)}"
        for field in REPORT_FIELDS
    ) + "\n"


def render_persisted_report(store: TaskStore, task_id: str) -> str:
    task = store.load(task_id); outcomes = store.outcomes(task_id)
    verify = outcomes.get("verify"); review = outcomes.get("review"); setup = outcomes.get("setup")
    pending=", ".join(store.outstanding_approvals(task_id)) or "none"
    values = {
        "state": task.state.value,
        "user outcome": ((setup or outcomes.get("implement")).diagnostic or (setup or outcomes.get("implement")).summary) if (setup or outcomes.get("implement")) else "unknown/missing",
        "tests": f"{verify.summary}: {', '.join(verify.evidence)}" if verify and verify.evidence else (verify.summary if verify else "unknown/missing"),
        "independent review": f"{review.summary}: {', '.join(review.evidence)}" if review and review.evidence else (review.summary if review else "unknown/missing"),
        "visual artifacts": ", ".join(review.artifacts) if review and review.artifacts else "unknown/missing",
        "residual risk": "unknown/missing",
        "rollback": next((o.rollback for o in outcomes.values() if o.rollback != "unknown/missing"), "unknown/missing"),
        "pending approval": pending,
    }
    return render_report(values)

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Sequence

from triagent.adapters.base import AgentResult, AgentStatus
from triagent.adapters.process import ProcessRunner


def invoke_json(
    runner: ProcessRunner,
    argv: Sequence[str],
    cwd: Path,
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> AgentResult:
    try:
        process = runner.run(argv, cwd, timeout, env or {})
    except (FileNotFoundError, OSError):
        return AgentResult(status=AgentStatus.UNAVAILABLE, summary="CLI executable is unavailable")
    if process.timed_out:
        return AgentResult(status=AgentStatus.TIMED_OUT, summary="CLI execution timed out")
    if process.returncode != 0:
        diagnostic = f"{process.stdout}\n{process.stderr}".lower()
        if any(marker in diagnostic for marker in ("not logged in", "unauthenticated", "authentication required", "login required")):
            return AgentResult(status=AgentStatus.UNAVAILABLE, summary="CLI authentication is unavailable", stderr=process.stderr)
        return AgentResult(status=AgentStatus.FAILED, summary="CLI execution failed", stderr=process.stderr)
    try:
        payload = json.loads(process.stdout)
    except (json.JSONDecodeError, TypeError):
        return AgentResult(status=AgentStatus.INVALID_OUTPUT, summary="CLI returned malformed structured output")
    if not isinstance(payload, dict) or not isinstance(payload.get("summary"), str):
        return AgentResult(status=AgentStatus.INVALID_OUTPUT, summary="CLI returned invalid structured output")
    return AgentResult(
        status=AgentStatus.SUCCEEDED,
        summary=payload["summary"],
        data=payload,
        stdout=process.stdout,
        stderr=process.stderr,
    )


def probe(runner: ProcessRunner, argv: Sequence[str]) -> tuple[bool, str]:
    try:
        result = runner.run(argv, Path.cwd(), 10, {})
    except (FileNotFoundError, OSError):
        return False, ""
    return (not result.timed_out and result.returncode == 0), result.stdout.strip()

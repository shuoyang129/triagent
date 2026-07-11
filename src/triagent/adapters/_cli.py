from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Mapping, Sequence

from triagent.adapters.base import AgentResult, AgentStatus
from triagent.adapters.process import ProcessRunner

REDACTED = "[REDACTED]"
_SECRET_KEY = re.compile(r"(?:api[_-]?key|token|secret|password|authorization|credential)", re.IGNORECASE)
_AUTH_FAILURES = (
    "401", "403", "unauthorized", "forbidden", "not logged in", "unauthenticated",
    "authentication required", "login required", "sign-in required", "signin required",
    "invalid token", "expired token", "missing api key", "api key required",
)


def runtime(names: Sequence[str], secret_values: Sequence[str] = ()) -> tuple[ProcessRunner, dict[str, str], tuple[str, ...]]:
    env = {name: os.environ[name] for name in names if os.environ.get(name)}
    secrets = tuple(dict.fromkeys([*secret_values, *env.values()]))
    return ProcessRunner(redactions=secrets), env, secrets


def sanitize(value: object, secrets: Sequence[str], key: str = "") -> object:
    if key and _SECRET_KEY.search(key):
        return REDACTED
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, dict):
        return {str(k): sanitize(v, secrets, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(item, secrets) for item in value]
    return value


def read_prompt(request) -> tuple[str | None, AgentResult | None]:
    try:
        return request.task_file.read_text(encoding="utf-8"), None
    except (OSError, UnicodeError):
        return None, AgentResult(status=AgentStatus.FAILED, summary="Unable to read task file")


def has_tool_evidence(output: str) -> bool:
    try:
        payload = json.loads(output)
        evidence = payload.get("triagent_tool_evidence", {})
        return (
            isinstance(evidence, dict)
            and evidence.get("operation") in {"write_file", "create_file", "edit_file", "delete_file"}
            and isinstance(evidence.get("path"), str)
            and bool(evidence["path"])
            and evidence.get("succeeded") is True
        )
    except (json.JSONDecodeError, AttributeError, TypeError):
        return False


def invoke_json(
    runner: ProcessRunner,
    argv: Sequence[str],
    cwd: Path,
    timeout: float,
    env: Mapping[str, str] | None = None,
    secret_values: Sequence[str] = (),
) -> AgentResult:
    try:
        process = runner.run(argv, cwd, timeout, env or {})
    except (FileNotFoundError, OSError):
        return AgentResult(status=AgentStatus.UNAVAILABLE, summary="CLI executable is unavailable")
    if process.timed_out:
        return AgentResult(status=AgentStatus.TIMED_OUT, summary="CLI execution timed out")
    if process.returncode != 0:
        diagnostic = f"{process.stdout}\n{process.stderr}".lower()
        stderr = sanitize(process.stderr, secret_values)
        if any(marker in diagnostic for marker in _AUTH_FAILURES):
            return AgentResult(status=AgentStatus.UNAVAILABLE, summary="CLI authentication or configuration is unavailable", stderr=str(stderr))
        return AgentResult(status=AgentStatus.FAILED, summary="CLI execution failed", stderr=str(stderr))
    try:
        payload = json.loads(process.stdout)
    except (json.JSONDecodeError, TypeError):
        return AgentResult(status=AgentStatus.INVALID_OUTPUT, summary="CLI returned malformed structured output")
    if not isinstance(payload, dict) or not isinstance(payload.get("summary"), str):
        return AgentResult(status=AgentStatus.INVALID_OUTPUT, summary="CLI returned invalid structured output")
    payload = sanitize(payload, secret_values)
    assert isinstance(payload, dict)
    return AgentResult(
        status=AgentStatus.SUCCEEDED,
        summary=payload["summary"],
        data=payload,
        stdout=str(sanitize(process.stdout, secret_values)),
        stderr=str(sanitize(process.stderr, secret_values)),
    )


def probe(runner: ProcessRunner, argv: Sequence[str], env: Mapping[str, str] | None = None) -> tuple[bool, str]:
    try:
        result = runner.run(argv, Path.cwd(), 10, env or {})
    except (FileNotFoundError, OSError):
        return False, ""
    return (not result.timed_out and result.returncode == 0), result.stdout.strip()

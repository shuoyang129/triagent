from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Mapping, Sequence

from triagent.adapters.base import AgentResult, AgentStatus
from triagent.adapters.process import ProcessRunner

REDACTED = "[REDACTED]"
_SECRET_KEY = re.compile(r"(?:api[_-]?key|token|secret|password|authorization|credential)", re.IGNORECASE)
_AUTH_FAILURES = (
    "unauthorized", "forbidden", "not logged in", "unauthenticated",
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


def filesystem_probe(
    runner: ProcessRunner,
    argv_prefix: Sequence[str],
    probe_dir: Path,
    env: Mapping[str, str],
    prompt_path=None,
) -> bool:
    nonce = uuid.uuid4().hex
    target = probe_dir / f"triagent-probe-{uuid.uuid4().hex}.txt"
    displayed_path = prompt_path(target) if prompt_path else str(target)
    contract = json.dumps({"path": displayed_path, "nonce": nonce}, separators=(",", ":"))
    prompt = f"Write the exact nonce to the exact file path using a file tool. TRIAGENT_SENTINEL={contract}"
    success = False
    try:
        target.unlink(missing_ok=True)
        result = runner.run([*argv_prefix, prompt], probe_dir, 30, env)
        success = not result.timed_out and result.returncode == 0 and target.read_text(encoding="utf-8") == nonce
    except (OSError, UnicodeError):
        success = False
    try:
        target.unlink(missing_ok=True)
    except OSError:
        return False
    return success


def invoke_codex_jsonl(
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
        auth_code = re.search(r"(?<!\d)(?:401|403)(?!\d)", diagnostic) is not None
        if auth_code or any(marker in diagnostic for marker in _AUTH_FAILURES):
            return AgentResult(status=AgentStatus.UNAVAILABLE, summary="CLI authentication or configuration is unavailable")
        return AgentResult(status=AgentStatus.FAILED, summary="CLI execution failed")
    messages: list[str] = []
    try:
        for line in process.stdout.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError
            item = event.get("item")
            if event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                messages.append(item["text"])
                continue
            if event.get("type") == "agent_message" and isinstance(event.get("message"), str):
                messages.append(event["message"])
                continue
            if event.get("type") == "message" and event.get("role") == "assistant" and isinstance(event.get("content"), list):
                text = "".join(part.get("text", "") for part in event["content"] if isinstance(part, dict) and part.get("type") in {"output_text", "text"})
                if text:
                    messages.append(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return AgentResult(status=AgentStatus.INVALID_OUTPUT, summary="CLI returned malformed structured output")
    if not messages:
        return AgentResult(status=AgentStatus.INVALID_OUTPUT, summary="CLI returned no final agent message")
    message = sanitize(messages[-1], secret_values)
    assert isinstance(message, str)
    return AgentResult(status=AgentStatus.SUCCEEDED, summary=message, data={"message": message})


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
        auth_code = re.search(r"(?<!\d)(?:401|403)(?!\d)", diagnostic) is not None
        if auth_code or any(marker in diagnostic for marker in _AUTH_FAILURES):
            return AgentResult(status=AgentStatus.UNAVAILABLE, summary="CLI authentication or configuration is unavailable")
        return AgentResult(status=AgentStatus.FAILED, summary="CLI execution failed")
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
        stdout="",
        stderr="",
    )


def probe(runner: ProcessRunner, argv: Sequence[str], env: Mapping[str, str] | None = None) -> tuple[bool, str]:
    try:
        result = runner.run(argv, Path.cwd(), 10, env or {})
    except (FileNotFoundError, OSError):
        return False, ""
    return (not result.timed_out and result.returncode == 0), result.stdout.strip()

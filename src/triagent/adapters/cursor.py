from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from pathlib import Path

from triagent.adapters._cli import invoke_json, probe, read_prompt, runtime
from triagent.adapters.base import AgentAdapter, AgentCapabilities, AgentRequest, AgentResult, AgentRole, AgentStatus, CostEstimate
from triagent.adapters.process import ProcessRunner, StreamPolicy, StreamingProcessRunner, safe_progress_event_sink


class CursorCapabilities(AgentCapabilities):
    pass


class CursorAdapter(AgentAdapter):
    identity = "cursor"
    allowed_roles = frozenset({AgentRole.IMPLEMENTER})
    _prefix = ["wsl.exe", "--distribution", "Ubuntu-24.04", "--exec", "bash", "--noprofile", "-c"]

    def __init__(self, runner: ProcessRunner | None = None, secret_values: Sequence[str] = (), command: Sequence[str] | None = None, estimated_usd: float | None = None, *, stream_v2: bool = False, stream_runner: StreamingProcessRunner | None = None, stream_policy: StreamPolicy | None = None) -> None:
        """Cursor streaming is explicit; the public default remains legacy."""
        if not isinstance(stream_v2, bool):
            raise TypeError("stream_v2 must be a bool")
        default_runner, self._env, self._secrets = runtime(("CURSOR_API_KEY",), secret_values)
        if "CURSOR_API_KEY" in self._env:
            self._env["WSLENV"] = "CURSOR_API_KEY/u"
        self._runner = runner or default_runner
        self._estimated_usd=estimated_usd
        self._configured_command = list(command) if command is not None else None
        self._stream_v2 = stream_v2
        self._stream_runner = stream_runner or StreamingProcessRunner(redactions=self._secrets)
        self._stream_policy = stream_policy

    def estimate_cost(self, request): return CostEstimate(self._estimated_usd)

    def _command(self, *args: str) -> list[str]:
        if self._configured_command is not None:
            return [*self._configured_command, *args]
        return [*self._prefix, 'exec "$HOME/.local/bin/cursor-agent" "$@"', "cursor", *args]

    def capabilities(self) -> CursorCapabilities:
        installed, version = probe(self._runner, self._command("--version"), self._env)
        authenticated = False
        if installed:
            authenticated, _ = probe(self._runner, self._command("status"), self._env)
        ready = installed and authenticated
        return CursorCapabilities(available=ready, installed=installed, version=version or None, authenticated=authenticated, headless=installed, ready=ready)

    def run(self, request: AgentRequest) -> AgentResult:
        payload,error=read_prompt(request)
        if error:return error
        argv = self._command("--trust", "--print", "--output-format", "json")
        if not self._stream_v2:
            return invoke_json(self._runner,argv,request.workdir,request.timeout_seconds,self._env,self._secrets,request.role,stdin=payload,cursor_envelope=True)
        return _invoke_cursor_envelope_stream(
            self._stream_runner, argv, request.workdir,
            self._stream_policy or _compat_stream_policy(request.timeout_seconds),
            self._env, self._secrets, stdin=payload,
            on_event=safe_progress_event_sink(request.task_file.parent / "events.jsonl"),
        )


def _compat_stream_policy(timeout_seconds: float) -> StreamPolicy:
    hard = float(timeout_seconds)
    return StreamPolicy(startup_timeout=min(30.0, hard), idle_timeout=hard,
                        hard_timeout=hard, finalize_grace=min(60.0, hard),
                        terminate_grace=min(2.0, hard))


_AUTH_FAILURES = ("authentication", "unauthorized", "invalid api key", "not logged")


class _FinalEnvelopeClassifier:
    """Only a complete local Cursor envelope may refresh stream liveness."""

    def __init__(self) -> None:
        self._stdout = ""
        self.terminal_seen = False

    def progress(self, stream: str, text: str) -> bool:
        if stream != "stdout":
            return False
        self._stdout += text
        if _valid_final_envelope(self._stdout) is _INVALID:
            return False
        self.terminal_seen = True
        return True

    def terminal(self, _stream: str, _text: str) -> bool:
        return self.terminal_seen


def _valid_final_envelope(raw: str) -> float | None | object:
    """Return cost for a strict success envelope; a sentinel means invalid."""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return _INVALID
    if not isinstance(payload, dict) or payload.get("type") != "result" or payload.get("subtype") != "success" or payload.get("is_error") is not False or not isinstance(payload.get("result"), str):
        return _INVALID
    cost = payload.get("total_cost_usd")
    if cost is None:
        return None
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
        return _INVALID
    return float(cost)


_INVALID = object()


def _invoke_cursor_envelope_stream(
    runner: StreamingProcessRunner,
    argv: Sequence[str], cwd: Path, policy: StreamPolicy,
    env: dict[str, str], secret_values: Sequence[str], *, stdin: str | None,
    on_event=None,
) -> AgentResult:
    """Independent Cursor stream-v2 transport; no capability probe or fallback."""
    classifier = _FinalEnvelopeClassifier()
    try:
        process = runner.run(argv, cwd, policy, env, stdin=stdin,
                             is_progress=classifier.progress,
                             is_terminal_result=classifier.terminal,
                             on_event=on_event)
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
    cost = _valid_final_envelope(process.stdout)
    if cost is _INVALID:
        return AgentResult(status=AgentStatus.INVALID_OUTPUT, summary="Cursor returned invalid result envelope", data={"diagnostic_code": "cursor-envelope-invalid"})
    return AgentResult(status=AgentStatus.SUCCEEDED, data={"status": "passed", "summary_code": "completed", "evidence": [], "artifacts": []}, actual_usd=cost)

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from triagent.adapters._cli import invoke_json, probe, read_prompt, runtime
from triagent.adapters.base import AgentAdapter, AgentCapabilities, AgentRequest, AgentResult, AgentRole, CostEstimate
from triagent.adapters.process import ProcessRunner


class CursorCapabilities(AgentCapabilities):
    pass


class CursorAdapter(AgentAdapter):
    identity = "cursor"
    allowed_roles = frozenset({AgentRole.IMPLEMENTER})
    _prefix = ["wsl.exe", "--distribution", "Ubuntu-24.04", "--exec", "bash", "--noprofile", "-c"]

    def __init__(self, runner: ProcessRunner | None = None, secret_values: Sequence[str] = (), command: Sequence[str] | None = None, estimated_usd: float | None = None) -> None:
        default_runner, self._env, self._secrets = runtime(("CURSOR_API_KEY",), secret_values)
        if "CURSOR_API_KEY" in self._env:
            self._env["WSLENV"] = "CURSOR_API_KEY/u"
        self._runner = runner or default_runner
        self._estimated_usd=estimated_usd
        self._configured_command = list(command) if command is not None else None

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
        return invoke_json(self._runner,self._command("--trust","--print","--output-format","json"),request.workdir,request.timeout_seconds,self._env,self._secrets,request.role,stdin=payload,cursor_envelope=True)

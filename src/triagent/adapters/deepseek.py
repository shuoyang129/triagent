from __future__ import annotations

import json
import tempfile
from collections.abc import Sequence
from pathlib import Path

from pydantic import ConfigDict

from triagent.adapters._cli import filesystem_probe, invoke_json, probe, read_prompt, runtime
from triagent.adapters.base import AgentAdapter, AgentCapabilities, AgentRequest, AgentResult, AgentRole, CostEstimate
from triagent.adapters.process import ProcessRunner


class DeepSeekCapabilities(AgentCapabilities):
    model_config = ConfigDict(frozen=True)
    enabled: bool = False
    api_configured_reachable: bool = False
    model_listed: bool = False
    agent_tool_smoke_test: bool = False
    billing_confirmed: bool = False


class DeepSeekAdapter(AgentAdapter):
    identity = "deepseek"
    allowed_roles = frozenset({AgentRole.IMPLEMENTER})
    def __init__(self, runner: ProcessRunner | None = None, enabled: bool = False, billing_confirmed: bool = False, live_confirmed: bool = False, secret_values: Sequence[str] = (), probe_dir: Path | None = None, command: Sequence[str] = ("opencode.exe",), probe_installed: bool = False, estimated_usd: float | None = None) -> None:
        default_runner, self._env, self._secrets = runtime(("DEEPSEEK_API_KEY", "OPENCODE_API_KEY"), secret_values)
        self._runner = runner or default_runner
        self._enabled = enabled
        self._billing = billing_confirmed
        self._live_confirmed = live_confirmed
        self._readiness: tuple[tuple[str, ...], float] | None = None
        self._probe_dir = probe_dir or Path(tempfile.gettempdir())
        self._command = list(command)
        self._probe_installed = probe_installed
        self._estimated_usd=estimated_usd
    def estimate_cost(self, request): return CostEstimate(self._estimated_usd)

    def capabilities(self) -> DeepSeekCapabilities:
        if not self._enabled:
            installed = probe(self._runner, [*self._command, "--version"], self._env)[0] if self._probe_installed else None
            return DeepSeekCapabilities(available=False, installed=installed, authenticated=None, ready=False, enabled=False, billing_confirmed=self._billing)
        if not self._billing:
            installed = probe(self._runner, [*self._command, "--version"], self._env)[0] if self._probe_installed else None
            return DeepSeekCapabilities(available=False, installed=installed, authenticated=None, ready=False, enabled=True, billing_confirmed=False)
        if not self._live_confirmed:
            return DeepSeekCapabilities(available=False, installed=None, authenticated=None, ready=False, enabled=True, billing_confirmed=True)
        installed, version = probe(self._runner, [*self._command, "--version"], self._env)
        reachable = listed = smoke = False
        if installed:
            ok, output = probe(self._runner, [*self._command, "deepseek", "probe", "--json"], self._env)
            try:
                reachable = ok and json.loads(output).get("reachable") is True
            except (json.JSONDecodeError, AttributeError):
                pass
        if reachable:
            ok, output = probe(self._runner, [*self._command, "models", "--json"], self._env)
            try:
                listed = ok and "deepseek/deepseek-chat" in json.loads(output).get("models", [])
            except (json.JSONDecodeError, AttributeError):
                pass
        if listed:
            smoke = filesystem_probe(self._runner, [*self._command, "run", "--model", "deepseek/deepseek-chat", "--json"], self._probe_dir, self._env)
        available = installed and reachable and listed and smoke and self._billing
        if available:
            import time
            self._readiness = (tuple(self._command), time.monotonic() + 60)
        return DeepSeekCapabilities(available=available, installed=installed, version=version or None, authenticated=reachable, headless=installed, ready=available, enabled=True, api_configured_reachable=reachable, model_listed=listed, agent_tool_smoke_test=smoke, billing_confirmed=self._billing)

    def run(self, request: AgentRequest) -> AgentResult:
        import time
        ready = self._readiness is not None and self._readiness[0] == tuple(self._command) and self._readiness[1] >= time.monotonic()
        if not (self._enabled and self._billing and self._live_confirmed and ready):
            from triagent.adapters.base import AgentStatus
            return AgentResult(status=AgentStatus.UNAVAILABLE, summary="DeepSeek/OpenCode live, billing, and readiness gates are incomplete")
        prompt, error = read_prompt(request)
        if error:
            return error
        assert prompt is not None
        return invoke_json(self._runner, [*self._command, "run", "--model", "deepseek/deepseek-chat", "--json", prompt], request.workdir, request.timeout_seconds, self._env, self._secrets)

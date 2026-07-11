from __future__ import annotations

import json
import tempfile
from collections.abc import Sequence
from pathlib import Path

from pydantic import ConfigDict

from triagent.adapters._cli import filesystem_probe, invoke_json, probe, read_prompt, runtime
from triagent.adapters.base import AgentAdapter, AgentCapabilities, AgentRequest, AgentResult
from triagent.adapters.process import ProcessRunner


class DeepSeekCapabilities(AgentCapabilities):
    model_config = ConfigDict(frozen=True)
    enabled: bool = False
    api_configured_reachable: bool = False
    model_listed: bool = False
    agent_tool_smoke_test: bool = False
    billing_confirmed: bool = False


class DeepSeekAdapter(AgentAdapter):
    def __init__(self, runner: ProcessRunner | None = None, enabled: bool = False, billing_confirmed: bool = False, secret_values: Sequence[str] = (), probe_dir: Path | None = None) -> None:
        default_runner, self._env, self._secrets = runtime(("DEEPSEEK_API_KEY", "OPENCODE_API_KEY"), secret_values)
        self._runner = runner or default_runner
        self._enabled = enabled
        self._billing = billing_confirmed
        self._probe_dir = probe_dir or Path(tempfile.gettempdir())

    def capabilities(self) -> DeepSeekCapabilities:
        if not self._enabled:
            return DeepSeekCapabilities(available=False, enabled=False, billing_confirmed=self._billing)
        if not self._billing:
            return DeepSeekCapabilities(available=False, enabled=True, billing_confirmed=False)
        installed, version = probe(self._runner, ["opencode.exe", "--version"], self._env)
        reachable = listed = smoke = False
        if installed:
            ok, output = probe(self._runner, ["opencode.exe", "deepseek", "probe", "--json"], self._env)
            try:
                reachable = ok and json.loads(output).get("reachable") is True
            except (json.JSONDecodeError, AttributeError):
                pass
        if reachable:
            ok, output = probe(self._runner, ["opencode.exe", "models", "--json"], self._env)
            try:
                listed = ok and "deepseek/deepseek-chat" in json.loads(output).get("models", [])
            except (json.JSONDecodeError, AttributeError):
                pass
        if listed:
            smoke = filesystem_probe(self._runner, ["opencode.exe", "run", "--model", "deepseek/deepseek-chat", "--json"], self._probe_dir, self._env)
        available = installed and reachable and listed and smoke and self._billing
        return DeepSeekCapabilities(available=available, version=version or None, authenticated=reachable, headless=installed, enabled=True, api_configured_reachable=reachable, model_listed=listed, agent_tool_smoke_test=smoke, billing_confirmed=self._billing)

    def run(self, request: AgentRequest) -> AgentResult:
        if not self._enabled:
            from triagent.adapters.base import AgentStatus
            return AgentResult(status=AgentStatus.UNAVAILABLE, summary="DeepSeek/OpenCode adapter is disabled")
        prompt, error = read_prompt(request)
        if error:
            return error
        assert prompt is not None
        return invoke_json(self._runner, ["opencode.exe", "run", "--model", "deepseek/deepseek-chat", "--json", prompt], request.workdir, request.timeout_seconds, self._env, self._secrets)

from __future__ import annotations

import json

from pydantic import ConfigDict

from triagent.adapters._cli import invoke_json, probe
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
    def __init__(self, runner: ProcessRunner | None = None, enabled: bool = False, billing_confirmed: bool = False) -> None:
        self._runner = runner or ProcessRunner()
        self._enabled = enabled
        self._billing = billing_confirmed

    def capabilities(self) -> DeepSeekCapabilities:
        if not self._enabled:
            return DeepSeekCapabilities(available=False, enabled=False, billing_confirmed=self._billing)
        installed, version = probe(self._runner, ["opencode.exe", "--version"])
        reachable = listed = smoke = False
        if installed:
            ok, output = probe(self._runner, ["opencode.exe", "deepseek", "probe", "--json"])
            try:
                reachable = ok and json.loads(output).get("reachable") is True
            except (json.JSONDecodeError, AttributeError):
                pass
        if reachable:
            ok, output = probe(self._runner, ["opencode.exe", "models", "--json"])
            try:
                listed = ok and "deepseek/deepseek-chat" in json.loads(output).get("models", [])
            except (json.JSONDecodeError, AttributeError):
                pass
        if listed:
            smoke, _ = probe(self._runner, ["opencode.exe", "run", "--model", "deepseek/deepseek-chat", "--json", "Use one agent tool"])
        available = installed and reachable and listed and smoke and self._billing
        return DeepSeekCapabilities(available=available, version=version or None, authenticated=reachable, headless=installed, enabled=True, api_configured_reachable=reachable, model_listed=listed, agent_tool_smoke_test=smoke, billing_confirmed=self._billing)

    def run(self, request: AgentRequest) -> AgentResult:
        if not self._enabled:
            from triagent.adapters.base import AgentStatus
            return AgentResult(status=AgentStatus.UNAVAILABLE, summary="DeepSeek/OpenCode adapter is disabled")
        prompt = request.task_file.read_text(encoding="utf-8")
        return invoke_json(self._runner, ["opencode.exe", "run", "--model", "deepseek/deepseek-chat", "--json", prompt], request.workdir, request.timeout_seconds)

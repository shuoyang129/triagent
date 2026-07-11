from __future__ import annotations

import json

from pydantic import ConfigDict

from triagent.adapters._cli import invoke_json, probe
from triagent.adapters.base import AgentAdapter, AgentCapabilities, AgentRequest, AgentResult
from triagent.adapters.process import ProcessRunner


class CursorCapabilities(AgentCapabilities):
    model_config = ConfigDict(frozen=True)
    deepseek_model_listed: bool = False
    deepseek_agent_smoke_test: bool = False
    deepseek_billing_confirmed: bool = False
    deepseek_byok_available: bool = False


class CursorAdapter(AgentAdapter):
    _prefix = ["wsl.exe", "--distribution", "Ubuntu-24.04", "--exec", "bash", "--noprofile", "-c"]

    def __init__(self, runner: ProcessRunner | None = None, deepseek_billing_confirmed: bool = False) -> None:
        self._runner = runner or ProcessRunner()
        self._billing = deepseek_billing_confirmed

    def _command(self, *args: str) -> list[str]:
        return [*self._prefix, 'exec "$HOME/.local/bin/cursor-agent" "$@"', "cursor", *args]

    def capabilities(self) -> CursorCapabilities:
        installed, version = probe(self._runner, self._command("--version"))
        authenticated = False
        model_listed = False
        smoke = False
        if installed:
            authenticated, _ = probe(self._runner, self._command("status"))
        if authenticated:
            models_ok, models = probe(self._runner, self._command("models", "--json"))
            try:
                model_listed = models_ok and "deepseek-v3" in json.loads(models).get("models", [])
            except (json.JSONDecodeError, AttributeError):
                model_listed = False
            smoke, _ = probe(self._runner, self._command("--print", "--json", "Use one agent tool and report JSON"))
        byok = model_listed and smoke and self._billing
        return CursorCapabilities(available=installed and authenticated, version=version or None, authenticated=authenticated, headless=installed, deepseek_model_listed=model_listed, deepseek_agent_smoke_test=smoke, deepseek_billing_confirmed=self._billing, deepseek_byok_available=byok)

    def run(self, request: AgentRequest) -> AgentResult:
        prompt = request.task_file.read_text(encoding="utf-8")
        return invoke_json(self._runner, self._command("--print", "--json", prompt), request.workdir, request.timeout_seconds)

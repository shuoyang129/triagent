from __future__ import annotations

from triagent.adapters._cli import invoke_json, probe
from triagent.adapters.base import AgentAdapter, AgentCapabilities, AgentRequest, AgentResult
from triagent.adapters.process import ProcessRunner


class AntigravityAdapter(AgentAdapter):
    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self._runner = runner or ProcessRunner()

    def capabilities(self) -> AgentCapabilities:
        installed, version = probe(self._runner, ["agy.exe", "--version"])
        authenticated = False
        if installed:
            authenticated, _ = probe(self._runner, ["agy.exe", "auth", "status"])
        return AgentCapabilities(available=installed and authenticated, version=version or None, authenticated=authenticated, headless=installed)

    def run(self, request: AgentRequest) -> AgentResult:
        prompt = request.task_file.read_text(encoding="utf-8")
        return invoke_json(self._runner, ["agy.exe", "--print", "--json", prompt], request.workdir, request.timeout_seconds)

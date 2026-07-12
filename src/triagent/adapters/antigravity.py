from __future__ import annotations

from collections.abc import Sequence

from triagent.adapters._cli import invoke_json, probe, read_prompt, runtime
from triagent.adapters.base import AgentAdapter, AgentCapabilities, AgentRequest, AgentResult
from triagent.adapters.process import ProcessRunner


class AntigravityAdapter(AgentAdapter):
    def __init__(self, runner: ProcessRunner | None = None, secret_values: Sequence[str] = (), command: Sequence[str] = ("agy.exe",)) -> None:
        default_runner, self._env, self._secrets = runtime(("AGY_API_KEY", "GOOGLE_API_KEY"), secret_values)
        self._runner = runner or default_runner
        self._command = list(command)

    def capabilities(self) -> AgentCapabilities:
        installed, version = probe(self._runner, [*self._command, "--version"], self._env)
        return AgentCapabilities(available=False, installed=installed, version=version or None, authenticated=None, headless=installed, ready=None)

    def run(self, request: AgentRequest) -> AgentResult:
        prompt, error = read_prompt(request)
        if error:
            return error
        assert prompt is not None
        return invoke_json(self._runner, [*self._command, "--print", "--json", prompt], request.workdir, request.timeout_seconds, self._env, self._secrets)

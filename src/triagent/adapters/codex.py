from __future__ import annotations

from collections.abc import Sequence

from triagent.adapters._cli import invoke_codex_jsonl, probe, read_prompt, runtime
from triagent.adapters.base import AgentAdapter, AgentCapabilities, AgentRequest, AgentResult
from triagent.adapters.process import ProcessRunner


class CodexAdapter(AgentAdapter):
    def __init__(self, runner: ProcessRunner | None = None, secret_values: Sequence[str] = ()) -> None:
        default_runner, self._env, self._secrets = runtime(("OPENAI_API_KEY", "CODEX_HOME"), secret_values)
        self._runner = runner or default_runner

    def capabilities(self) -> AgentCapabilities:
        installed, version = probe(self._runner, ["codex.exe", "--version"], self._env)
        authenticated = False
        if installed:
            authenticated, _ = probe(self._runner, ["codex.exe", "login", "status"], self._env)
        return AgentCapabilities(available=installed and authenticated, version=version or None, authenticated=authenticated, headless=installed)

    def run(self, request: AgentRequest) -> AgentResult:
        prompt, error = read_prompt(request)
        if error:
            return error
        assert prompt is not None
        argv = ["codex.exe", "exec", "--sandbox", "read-only", "--json", prompt]
        return invoke_codex_jsonl(self._runner, argv, request.workdir, request.timeout_seconds, self._env, self._secrets)

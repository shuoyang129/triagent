from __future__ import annotations

from collections.abc import Sequence

from triagent.adapters._cli import invoke_codex_jsonl, probe, read_prompt, runtime
from triagent.adapters.base import AgentAdapter, AgentCapabilities, AgentRequest, AgentResult, AgentRole, CostEstimate
from triagent.adapters.process import ProcessRunner


class CodexAdapter(AgentAdapter):
    identity = "codex"
    allowed_roles = frozenset({AgentRole.VERIFIER})
    def __init__(self, runner: ProcessRunner | None = None, secret_values: Sequence[str] = (), command: Sequence[str] = ("codex.exe",), estimated_usd: float | None = None) -> None:
        default_runner, self._env, self._secrets = runtime(("OPENAI_API_KEY", "CODEX_HOME"), secret_values)
        self._runner = runner or default_runner
        self._command = list(command)
        self._estimated_usd=estimated_usd
    def estimate_cost(self, request): return CostEstimate(self._estimated_usd)

    def capabilities(self) -> AgentCapabilities:
        installed, version = probe(self._runner, [*self._command, "--version"], self._env)
        authenticated = False
        if installed:
            authenticated, _ = probe(self._runner, [*self._command, "login", "status"], self._env)
        ready = installed and authenticated
        return AgentCapabilities(available=ready, installed=installed, version=version or None, authenticated=authenticated, headless=installed, ready=ready)

    def run(self, request: AgentRequest) -> AgentResult:
        payload,error=read_prompt(request)
        if error:return error
        return invoke_codex_jsonl(self._runner,[*self._command,"exec","--sandbox","read-only","--json","-"],request.workdir,request.timeout_seconds,self._env,self._secrets,request.role,stdin=payload)

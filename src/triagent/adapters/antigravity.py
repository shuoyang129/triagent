from __future__ import annotations

from collections.abc import Sequence

from triagent.adapters._cli import invoke_json, probe, restricted_input, runtime
from triagent.adapters.base import AgentAdapter, AgentCapabilities, AgentRequest, AgentResult, AgentRole, CostEstimate
from triagent.adapters.process import ProcessRunner


class AntigravityAdapter(AgentAdapter):
    identity = "antigravity"
    allowed_roles = frozenset({AgentRole.REVIEWER})
    def __init__(self, runner: ProcessRunner | None = None, secret_values: Sequence[str] = (), command: Sequence[str] = ("agy.exe",), estimated_usd: float | None = None) -> None:
        default_runner, self._env, self._secrets = runtime(("AGY_API_KEY", "GOOGLE_API_KEY"), secret_values)
        self._runner = runner or default_runner
        self._command = list(command)
        self._estimated_usd=estimated_usd
    def estimate_cost(self, request): return CostEstimate(self._estimated_usd)

    def capabilities(self) -> AgentCapabilities:
        installed, version = probe(self._runner, [*self._command, "--version"], self._env)
        return AgentCapabilities(available=False, installed=installed, version=version or None, authenticated=None, headless=installed, ready=None)

    def run(self, request: AgentRequest) -> AgentResult:
        with restricted_input(request) as (path,error):
            if error:return error
            return invoke_json(self._runner,[*self._command,"--print","--json","--input-file",str(path)],request.workdir,request.timeout_seconds,self._env,self._secrets,request.role)

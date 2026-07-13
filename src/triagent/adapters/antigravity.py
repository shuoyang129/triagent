from __future__ import annotations

from collections.abc import Sequence

from triagent.adapters._cli import TransportSecurityError,external_restricted_input,invoke_json, probe, runtime
from triagent.adapters.base import AgentAdapter, AgentCapabilities, AgentRequest, AgentResult, AgentRole, AgentStatus, CostEstimate
from triagent.adapters.process import ProcessRunner


class AntigravityAdapter(AgentAdapter):
    identity = "antigravity"
    allowed_roles = frozenset({AgentRole.REVIEWER})
    def __init__(self, runner: ProcessRunner | None = None, secret_values: Sequence[str] = (), command: Sequence[str] = ("agy.exe",), estimated_usd: float | None = None, acl_verifier=None) -> None:
        default_runner, self._env, self._secrets = runtime(("AGY_API_KEY", "GOOGLE_API_KEY"), secret_values)
        self._runner = runner or default_runner
        self._command = list(command)
        self._estimated_usd=estimated_usd
        self._acl_verifier=acl_verifier
    def estimate_cost(self, request): return CostEstimate(self._estimated_usd)

    def capabilities(self) -> AgentCapabilities:
        installed, version = probe(self._runner, [*self._command, "--version"], self._env)
        return AgentCapabilities(available=False, installed=installed, version=version or None, authenticated=None, headless=installed, ready=None)

    def run(self, request: AgentRequest) -> AgentResult:
        try:
            with external_restricted_input(request,self._acl_verifier) as (path,error):
                if error:return error
                instruction=f"Read and follow the complete instructions in this local file: {path}"
                return invoke_json(self._runner,[*self._command,"-p",instruction],request.workdir,request.timeout_seconds,self._env,self._secrets,request.role)
        except TransportSecurityError as error:
            return AgentResult(status=AgentStatus.FAILED,summary=error.code,data={"diagnostic_code":error.code})

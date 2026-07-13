from __future__ import annotations

import json
import tempfile
from collections.abc import Sequence
from pathlib import Path

from pydantic import ConfigDict

from triagent.adapters._cli import filesystem_probe, invoke_json, probe, read_prompt, runtime
from triagent.adapters.base import AgentAdapter, AgentCapabilities, AgentRequest, AgentResult, AgentRole, CostEstimate
from triagent.adapters.process import ProcessRunner


class CursorCapabilities(AgentCapabilities):
    model_config = ConfigDict(frozen=True)
    deepseek_model_listed: bool = False
    deepseek_agent_smoke_test: bool = False
    deepseek_billing_confirmed: bool = False
    deepseek_byok_available: bool = False


class CursorAdapter(AgentAdapter):
    identity = "cursor"
    allowed_roles = frozenset({AgentRole.IMPLEMENTER})
    _prefix = ["wsl.exe", "--distribution", "Ubuntu-24.04", "--exec", "bash", "--noprofile", "-c"]

    def __init__(self, runner: ProcessRunner | None = None, deepseek_billing_confirmed: bool = False, secret_values: Sequence[str] = (), probe_dir: Path | None = None, command: Sequence[str] | None = None, estimated_usd: float | None = None) -> None:
        default_runner, self._env, self._secrets = runtime(("CURSOR_API_KEY", "DEEPSEEK_API_KEY"), secret_values)
        bridge = [f"{name}/u" for name in ("CURSOR_API_KEY", "DEEPSEEK_API_KEY") if name in self._env]
        if bridge:
            self._env["WSLENV"] = ":".join(bridge)
        self._runner = runner or default_runner
        self._estimated_usd=estimated_usd
        self._billing = deepseek_billing_confirmed
        self._probe_dir = probe_dir or Path(tempfile.gettempdir())
        self._configured_command = list(command) if command is not None else None

    def estimate_cost(self, request): return CostEstimate(self._estimated_usd)

    def _command(self, *args: str) -> list[str]:
        if self._configured_command is not None:
            return [*self._configured_command, *args]
        return [*self._prefix, 'exec "$HOME/.local/bin/cursor-agent" "$@"', "cursor", *args]

    @staticmethod
    def _wsl_path(path: Path) -> str:
        drive = path.drive.rstrip(":").lower()
        tail = path.as_posix().split(":", 1)[-1]
        return f"/mnt/{drive}{tail}" if drive else path.as_posix()

    def capabilities(self) -> CursorCapabilities:
        installed, version = probe(self._runner, self._command("--version"), self._env)
        authenticated = False
        model_listed = False
        smoke = False
        if installed:
            authenticated, _ = probe(self._runner, self._command("status"), self._env)
        if authenticated and self._billing:
            models_ok, models = probe(self._runner, self._command("models", "--json"), self._env)
            try:
                model_listed = models_ok and "deepseek-v3" in json.loads(models).get("models", [])
            except (json.JSONDecodeError, AttributeError):
                model_listed = False
            smoke = filesystem_probe(self._runner, self._command("--print", "--output-format", "json"), self._probe_dir, self._env, self._wsl_path)
        byok = model_listed and smoke and self._billing
        ready = installed and authenticated
        return CursorCapabilities(available=ready, installed=installed, version=version or None, authenticated=authenticated, headless=installed, ready=ready, deepseek_model_listed=model_listed, deepseek_agent_smoke_test=smoke, deepseek_billing_confirmed=self._billing, deepseek_byok_available=byok)

    def run(self, request: AgentRequest) -> AgentResult:
        payload,error=read_prompt(request)
        if error:return error
        return invoke_json(self._runner,self._command("--trust","--print","--output-format","json"),request.workdir,request.timeout_seconds,self._env,self._secrets,request.role,stdin=payload,cursor_envelope=True)

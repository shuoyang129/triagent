from __future__ import annotations

import json
import tempfile
from collections.abc import Sequence
from pathlib import Path

from pydantic import ConfigDict

from triagent.adapters._cli import filesystem_probe, invoke_json, probe, read_prompt, runtime
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

    def __init__(self, runner: ProcessRunner | None = None, deepseek_billing_confirmed: bool = False, secret_values: Sequence[str] = (), probe_dir: Path | None = None) -> None:
        default_runner, self._env, self._secrets = runtime(("CURSOR_API_KEY", "DEEPSEEK_API_KEY"), secret_values)
        bridge = [f"{name}/u" for name in ("CURSOR_API_KEY", "DEEPSEEK_API_KEY") if name in self._env]
        if bridge:
            self._env["WSLENV"] = ":".join(bridge)
        self._runner = runner or default_runner
        self._billing = deepseek_billing_confirmed
        self._probe_dir = probe_dir or Path(tempfile.gettempdir())

    def _command(self, *args: str) -> list[str]:
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
            smoke = filesystem_probe(self._runner, self._command("--print", "--json"), self._probe_dir, self._env, self._wsl_path)
        byok = model_listed and smoke and self._billing
        return CursorCapabilities(available=installed and authenticated, version=version or None, authenticated=authenticated, headless=installed, deepseek_model_listed=model_listed, deepseek_agent_smoke_test=smoke, deepseek_billing_confirmed=self._billing, deepseek_byok_available=byok)

    def run(self, request: AgentRequest) -> AgentResult:
        prompt, error = read_prompt(request)
        if error:
            return error
        assert prompt is not None
        return invoke_json(self._runner, self._command("--print", "--json", prompt), request.workdir, request.timeout_seconds, self._env, self._secrets)

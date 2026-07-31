from __future__ import annotations

import json
import math
import re
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict

from triagent.adapters._cli import (
    _canonical,
    _decode_json_object,
    invoke_opencode_jsonl,
    probe,
    read_prompt,
    runtime,
    sanitize,
)
from triagent.adapters.base import (
    AgentAdapter,
    AgentCapabilities,
    AgentRequest,
    AgentResult,
    AgentRole,
    AgentStatus,
    CostEstimate,
)
from triagent.adapters.process import ProcessResult, ProcessRunner


_ALLOWED_MODELS = frozenset(
    {
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-chat",
        "deepseek/deepseek-reasoner",
    }
)
_MIN_SMOKE_TIMEOUT_SECONDS = 1
_MAX_SMOKE_TIMEOUT_SECONDS = 300

_MAX_CANONICAL_OUTPUT_BYTES = 1_000_000


DeepSeekDiagnostic = Literal[
    "deepseek-disabled",
    "deepseek-billing-not-confirmed",
    "deepseek-live-not-confirmed",
    "deepseek-key-missing",
    "deepseek-opencode-missing",
    "deepseek-authentication-failed",
    "deepseek-insufficient-balance",
    "deepseek-permission-denied",
    "deepseek-rate-limited",
    "deepseek-timeout",
    "deepseek-connection-failed",
    "deepseek-service-unavailable",
    "deepseek-request-invalid",
    "deepseek-model-not-listed",
    "deepseek-smoke-invalid",
    "deepseek-api-failed",
]


class DeepSeekCapabilities(AgentCapabilities):
    model_config = ConfigDict(frozen=True)
    enabled: bool = False
    api_configured_reachable: bool = False
    model_listed: bool = False
    agent_tool_smoke_test: bool = False
    billing_confirmed: bool = False
    diagnostic_code: DeepSeekDiagnostic | None = None


def _permissions() -> dict[str, object]:
    private_paths = {
        "*": "allow",
        "*.env": "deny",
        "*.env.*": "deny",
        ".env": "deny",
        ".env.*": "deny",
        ".git/*": "deny",
    }
    return {
        "*": "deny",
        "read": private_paths,
        "edit": private_paths,
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "todowrite": "allow",
        "todoread": "allow",
        "bash": "deny",
        "webfetch": "deny",
        "websearch": "deny",
        "task": "deny",
        "skill": "deny",
        "lsp": "deny",
        "external_directory": "deny",
        "doom_loop": "deny",
    }


def _opencode_config(model: str) -> str:
    permissions = _permissions()
    return json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "model": model,
            "small_model": model,
            "autoupdate": False,
            "enabled_providers": ["deepseek"],
            "provider": {
                "deepseek": {"options": {"apiKey": "{env:DEEPSEEK_API_KEY}"}}
            },
            "permission": permissions,
            "agent": {
                "triagent": {
                    "description": "Restricted TriAgent DeepSeek implementer",
                    "mode": "primary",
                    "model": model,
                    "permission": permissions,
                }
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _failure_diagnostic(process: ProcessResult) -> DeepSeekDiagnostic:
    if process.timed_out:
        return "deepseek-timeout"
    text = f"{process.stdout}\n{process.stderr}".lower()
    if re.search(r"(?<!\d)401(?!\d)", text) or any(
        marker in text for marker in ("unauthorized", "invalid api key", "authentication")
    ):
        return "deepseek-authentication-failed"
    if re.search(r"(?<!\d)402(?!\d)", text) or any(
        marker in text
        for marker in ("insufficient balance", "insufficient_balance", "insufficient quota")
    ):
        return "deepseek-insufficient-balance"
    if re.search(r"(?<!\d)403(?!\d)", text) or "forbidden" in text:
        return "deepseek-permission-denied"
    if re.search(r"(?<!\d)429(?!\d)", text) or "rate limit" in text:
        return "deepseek-rate-limited"
    if any(
        marker in text
        for marker in ("connection refused", "connection failed", "could not connect")
    ):
        return "deepseek-connection-failed"
    if re.search(r"(?<!\d)5\d\d(?!\d)", text):
        return "deepseek-service-unavailable"
    if any(marker in text for marker in ("invalid request", "bad request", "model not found")):
        return "deepseek-request-invalid"
    return "deepseek-api-failed"


def _read_private_canonical_output(
    path: Path,
    role: AgentRole,
    secrets: Sequence[str],
) -> AgentResult | None:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o077
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_CANONICAL_OUTPUT_BYTES
        ):
            return None
        payload, diagnostic = _decode_json_object(path.read_text(encoding="utf-8"))
        if diagnostic is not None or payload is None:
            return None
        payload = sanitize(payload, secrets)
        if not isinstance(payload, dict):
            return None
        data = _canonical(role, payload)
        changed_paths = data.get("changed_paths", [])
        if any(
            Path(item).name.startswith(".triagent-opencode-output-")
            for item in changed_paths
        ):
            return None
        actual = payload.get("actual_usd")
        if (
            not isinstance(actual, (int, float))
            or isinstance(actual, bool)
            or not math.isfinite(actual)
        ):
            actual = None
        return AgentResult(
            status=AgentStatus.SUCCEEDED,
            data=data,
            stdout="",
            stderr="",
            actual_usd=actual,
        )
    except (OSError, UnicodeError, ValueError):
        return None


class DeepSeekAdapter(AgentAdapter):
    identity = "deepseek"
    allowed_roles = frozenset({AgentRole.IMPLEMENTER})

    def __init__(
        self,
        *,
        enabled: bool = False,
        billing_confirmed: bool = False,
        live_confirmed: bool = False,
        secret_values: Sequence[str] = (),
        estimated_usd: float | None = None,
        model: str = "deepseek/deepseek-v4-pro",
        command: Sequence[str] = ("opencode",),
        runner: ProcessRunner | None = None,
        probe_dir: Path | None = None,
        smoke_timeout_seconds: float = 30,
        **legacy: object,
    ) -> None:
        if legacy:
            raise TypeError("legacy native DeepSeek arguments are unsupported")
        if model not in _ALLOWED_MODELS:
            raise ValueError("unsupported DeepSeek model")
        if not command or any(not isinstance(part, str) or not part for part in command):
            raise ValueError("invalid OpenCode command")
        if (
            isinstance(smoke_timeout_seconds, bool)
            or not isinstance(smoke_timeout_seconds, (int, float))
            or not math.isfinite(smoke_timeout_seconds)
            or not _MIN_SMOKE_TIMEOUT_SECONDS
            <= smoke_timeout_seconds
            <= _MAX_SMOKE_TIMEOUT_SECONDS
        ):
            raise ValueError("DeepSeek smoke timeout must be between 1 and 300 seconds")
        default_runner, env, secrets = runtime(("DEEPSEEK_API_KEY",), secret_values)
        self._runner = runner or default_runner
        self._env = {
            **env,
            "OPENCODE_CONFIG_CONTENT": _opencode_config(model),
            "OPENCODE_AUTO_SHARE": "false",
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
            "OPENCODE_DISABLE_LSP_DOWNLOAD": "true",
            "OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS": "false",
        }
        self._secrets = secrets
        self._enabled = enabled
        self._billing = billing_confirmed
        self._live_confirmed = live_confirmed
        self._estimated_usd = estimated_usd
        self._model = model
        self._command = list(command)
        self._probe_dir = probe_dir
        self._smoke_timeout_seconds = float(smoke_timeout_seconds)
        self._ready_until = 0.0

    def estimate_cost(self, request: AgentRequest | None) -> CostEstimate:
        return CostEstimate(self._estimated_usd)

    def _run(self, args: Sequence[str], cwd: Path, timeout: float) -> ProcessResult:
        return self._runner.run([*self._command, *args], cwd, timeout, self._env)

    def _smoke(self, directory: Path) -> tuple[bool, DeepSeekDiagnostic | None]:
        nonce = uuid.uuid4().hex
        target = directory / f"triagent-opencode-probe-{uuid.uuid4().hex}.txt"
        prompt = (
            "Use the file editing tool to write the exact nonce to the exact path. "
            "Do not use shell, network, or any other tool. "
            f"PATH_JSON={json.dumps(str(target))} NONCE={nonce}"
        )
        try:
            process = self._run(
                [
                    "run",
                    "--pure",
                    "--model",
                    self._model,
                    "--agent",
                    "triagent",
                    "--format",
                    "json",
                    "--dir",
                    str(directory),
                    prompt,
                ],
                directory,
                self._smoke_timeout_seconds,
            )
            if process.timed_out or process.returncode != 0:
                return False, _failure_diagnostic(process)
            if target.read_text(encoding="utf-8") != nonce:
                return False, "deepseek-smoke-invalid"
            return True, None
        except (FileNotFoundError, OSError, UnicodeError):
            return False, "deepseek-smoke-invalid"
        finally:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass

    def capabilities(self) -> DeepSeekCapabilities:
        installed, version = probe(self._runner, [*self._command, "--version"], self._env)
        base = {
            "enabled": self._enabled,
            "billing_confirmed": self._billing,
            "installed": installed,
            "version": version or None,
        }
        if not self._enabled:
            return DeepSeekCapabilities(
                available=False,
                authenticated=None,
                ready=False,
                diagnostic_code="deepseek-disabled",
                **base,
            )
        if not installed:
            return DeepSeekCapabilities(
                available=False,
                authenticated=None,
                ready=False,
                diagnostic_code="deepseek-opencode-missing",
                **base,
            )
        if not self._billing:
            return DeepSeekCapabilities(
                available=False,
                authenticated=None,
                ready=False,
                diagnostic_code="deepseek-billing-not-confirmed",
                **base,
            )
        if not self._live_confirmed:
            return DeepSeekCapabilities(
                available=False,
                authenticated=None,
                ready=False,
                diagnostic_code="deepseek-live-not-confirmed",
                **base,
            )
        if not self._env.get("DEEPSEEK_API_KEY"):
            return DeepSeekCapabilities(
                available=False,
                authenticated=False,
                ready=False,
                diagnostic_code="deepseek-key-missing",
                **base,
            )
        try:
            models = self._run(["models", "deepseek", "--pure"], Path.cwd(), 30)
        except (FileNotFoundError, OSError):
            return DeepSeekCapabilities(
                available=False,
                authenticated=None,
                ready=False,
                diagnostic_code="deepseek-opencode-missing",
                **base,
            )
        if models.timed_out or models.returncode != 0:
            diagnostic = _failure_diagnostic(models)
            return DeepSeekCapabilities(
                available=False,
                authenticated=False if diagnostic == "deepseek-authentication-failed" else None,
                ready=False,
                diagnostic_code=diagnostic,
                **base,
            )
        listed = self._model in {
            line.strip() for line in models.stdout.splitlines() if line.strip()
        }
        if not listed:
            return DeepSeekCapabilities(
                available=False,
                authenticated=None,
                headless=True,
                ready=False,
                model_listed=False,
                diagnostic_code="deepseek-model-not-listed",
                **base,
            )
        if self._probe_dir is None:
            with tempfile.TemporaryDirectory(prefix="triagent-opencode-probe-") as raw:
                smoke, diagnostic = self._smoke(Path(raw))
        else:
            self._probe_dir.mkdir(parents=True, exist_ok=True)
            smoke, diagnostic = self._smoke(self._probe_dir)
        if not smoke:
            return DeepSeekCapabilities(
                available=False,
                authenticated=False if diagnostic == "deepseek-authentication-failed" else None,
                headless=True,
                ready=False,
                api_configured_reachable=diagnostic
                not in {
                    "deepseek-authentication-failed",
                    "deepseek-connection-failed",
                    "deepseek-timeout",
                },
                model_listed=True,
                agent_tool_smoke_test=False,
                diagnostic_code=diagnostic or "deepseek-smoke-invalid",
                **base,
            )
        self._ready_until = time.monotonic() + 60
        return DeepSeekCapabilities(
            available=True,
            authenticated=True,
            headless=True,
            ready=True,
            api_configured_reachable=True,
            model_listed=True,
            agent_tool_smoke_test=True,
            **base,
        )

    def run(self, request: AgentRequest) -> AgentResult:
        if not (
            self._enabled
            and self._billing
            and self._live_confirmed
            and self._env.get("DEEPSEEK_API_KEY")
            and self._ready_until >= time.monotonic()
        ):
            return AgentResult(
                status=AgentStatus.UNAVAILABLE,
                summary="DeepSeek live, billing, and readiness gates are incomplete",
            )
        prompt, error = read_prompt(request)
        if error is not None:
            return error
        assert prompt is not None
        prompt_file: Path | None = None
        transport_file: Path | None = None
        legacy_output_file = request.workdir / ".triagent-output.json"
        legacy_output_file_preexisting = (
            legacy_output_file.exists() or legacy_output_file.is_symlink()
        )
        legacy_output_file_declared = False
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=request.workdir,
                prefix=".triagent-opencode-input-",
                suffix=".txt",
                delete=False,
            ) as handle:
                handle.write(prompt)
                prompt_file = Path(handle.name)
            prompt_file.chmod(0o600)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=request.workdir,
                prefix=".triagent-opencode-output-",
                suffix=".json",
                delete=False,
            ) as handle:
                transport_file = Path(handle.name)
            transport_file.chmod(0o600)
            result = invoke_opencode_jsonl(
                self._runner,
                [
                    *self._command,
                    "run",
                    "--pure",
                    "--model",
                    self._model,
                    "--agent",
                    "triagent",
                    "--format",
                    "json",
                    "--dir",
                    str(request.workdir),
                    (
                        "Implement the attached TriAgent controller task in the current "
                        "workdir. Write exactly the required JSON object to the private "
                        "canonical output path using the edit tool, then return the same "
                        "JSON object with no prose. Exclude the transport file from "
                        "changed_paths. CANONICAL_OUTPUT_PATH_JSON="
                        f"{json.dumps(str(transport_file))}"
                    ),
                    f"--file={prompt_file}",
                ],
                request.workdir,
                request.timeout_seconds,
                self._env,
                self._secrets,
                request.role,
                _failure_diagnostic,
            )
            changed_paths = result.data.get("changed_paths")
            legacy_output_file_declared = (
                isinstance(changed_paths, list)
                and ".triagent-output.json" in changed_paths
            )
            if result.status is not AgentStatus.INVALID_OUTPUT:
                return result
            assert transport_file is not None
            recovered = _read_private_canonical_output(
                transport_file,
                request.role,
                self._secrets,
            )
            if recovered is not None:
                return recovered
            diff = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=ACMRTUXB", "HEAD", "--"],
                cwd=request.workdir,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            changed_paths = [path for path in diff.stdout.splitlines() if path]
            if diff.returncode != 0 or not changed_paths:
                return result
            return AgentResult(
                status=AgentStatus.SUCCEEDED,
                summary="Recovered scoped tracked edits after invalid canonical output",
                data={
                    "status": "passed",
                    "summary_code": "completed",
                    "evidence": ["OpenCode completed tracked edits; canonical output recovery applied"],
                    "artifacts": [],
                    "changed_paths": changed_paths,
                },
            )
        except Exception:
            return AgentResult(
                status=AgentStatus.FAILED,
                summary="OpenCode transport failed",
                data={"diagnostic_code": "deepseek-api-failed"},
            )
        finally:
            for private_file in (prompt_file, transport_file):
                if private_file is not None:
                    try:
                        private_file.unlink(missing_ok=True)
                    except OSError:
                        pass
            if (
                not legacy_output_file_preexisting
                and not legacy_output_file_declared
                and legacy_output_file.is_file()
                and not legacy_output_file.is_symlink()
            ):
                try:
                    legacy_output_file.unlink()
                except OSError:
                    pass

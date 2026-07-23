from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, model_validator

from triagent.adapters._cli import _decode_json_object, read_prompt
from triagent.adapters.base import (
    AgentAdapter,
    AgentCapabilities,
    AgentRequest,
    AgentResult,
    AgentRole,
    AgentStatus,
    CostEstimate,
)

_MAX_FILES = 256
_MAX_FILE_BYTES = 128 * 1024
_MAX_SNAPSHOT_BYTES = 768 * 1024
_MAX_CHANGES = 100
_MAX_CHANGE_BYTES = 1024 * 1024
_MAX_TOTAL_CHANGE_BYTES = 4 * 1024 * 1024
_ALLOWED_MODELS = frozenset({
    "deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner",
})
DeepSeekDiagnostic = Literal[
    "deepseek-disabled",
    "deepseek-billing-not-confirmed",
    "deepseek-live-not-confirmed",
    "deepseek-key-missing",
    "deepseek-sdk-missing",
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


class FileChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: StrictStr = Field(min_length=1, max_length=1024)
    action: Literal["write", "delete"]
    content: StrictStr | None = None

    @model_validator(mode="after")
    def content_matches_action(self) -> "FileChange":
        if self.action == "write" and self.content is None:
            raise ValueError("write requires content")
        if self.action == "delete" and self.content is not None:
            raise ValueError("delete forbids content")
        return self


class DeepSeekOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["passed", "failed"]
    evidence: list[StrictStr] = Field(max_length=50)
    artifacts: list[StrictStr] = Field(max_length=50)
    changes: list[FileChange] = Field(max_length=_MAX_CHANGES)


def _default_client_factory(*, api_key: str, base_url: str, timeout: float):
    from openai import OpenAI
    return OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)


def _safe_base_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.deepseek.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in {"", "/v1"}
    ):
        raise ValueError("DeepSeek base URL must be the official HTTPS API endpoint")
    return value.rstrip("/")


def _message_content(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        raise ValueError("missing choices")
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if not isinstance(content, str):
        raise ValueError("missing message content")
    return content


def _model_ids(response: Any) -> set[str]:
    data = getattr(response, "data", None)
    if not isinstance(data, list):
        return set()
    return {item.id for item in data if isinstance(getattr(item, "id", None), str)}


def _api_error_diagnostic(error: Exception) -> DeepSeekDiagnostic:
    status = getattr(error, "status_code", None)
    markers: list[str] = []
    for value in (getattr(error, "code", None), getattr(error, "type", None)):
        if isinstance(value, str):
            markers.append(value.lower())
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        payload = body.get("error", body)
        if isinstance(payload, dict):
            for key in ("code", "type"):
                value = payload.get(key)
                if isinstance(value, str):
                    markers.append(value.lower())
    marker = " ".join(markers)
    class_name = type(error).__name__.lower()
    if status == 401 or "authentication" in class_name or "invalid_api_key" in marker:
        return "deepseek-authentication-failed"
    if status == 402 or "insufficient_balance" in marker or "insufficient_quota" in marker:
        return "deepseek-insufficient-balance"
    if status == 403 or "permission" in class_name:
        return "deepseek-permission-denied"
    if status == 429 or "ratelimit" in class_name or "rate_limit" in marker:
        return "deepseek-rate-limited"
    if isinstance(error, TimeoutError) or "timeout" in class_name:
        return "deepseek-timeout"
    if "connection" in class_name:
        return "deepseek-connection-failed"
    if isinstance(status, int) and status >= 500:
        return "deepseek-service-unavailable"
    if status in {400, 404, 409, 422} or any(
        token in class_name for token in ("badrequest", "notfound", "unprocessable")
    ):
        return "deepseek-request-invalid"
    return "deepseek-api-failed"


def _relative_path(raw: str) -> PurePosixPath:
    if "\\" in raw or "\x00" in raw:
        raise ValueError("invalid change path")
    path = PurePosixPath(raw)
    if path.is_absolute() or path == PurePosixPath(".") or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("invalid change path")
    if path.parts[0] == ".git":
        raise ValueError("git metadata is forbidden")
    return path


def _safe_target(workdir: Path, raw: str) -> Path:
    relative = _relative_path(raw)
    target = workdir.joinpath(*relative.parts)
    current = workdir
    for part in relative.parts[:-1]:
        current /= part
        if current.exists() and current.is_symlink():
            raise ValueError("symlink traversal is forbidden")
    if target.is_symlink():
        raise ValueError("symlink target is forbidden")
    return target


def _snapshot(workdir: Path) -> list[dict[str, str]]:
    process = subprocess.run(
        ["git", "ls-files", "-z"], cwd=workdir, capture_output=True, check=False, timeout=10,
    )
    if process.returncode != 0:
        raise ValueError("repository manifest unavailable")
    result: list[dict[str, str]] = []
    total = 0
    for raw in process.stdout.split(b"\0"):
        if not raw or len(result) >= _MAX_FILES:
            continue
        try:
            relative = raw.decode("utf-8")
            target = _safe_target(workdir, relative)
            mode = target.stat(follow_symlinks=False).st_mode
            if not stat.S_ISREG(mode):
                continue
            data = target.read_bytes()
            if len(data) > _MAX_FILE_BYTES or total + len(data) > _MAX_SNAPSHOT_BYTES or b"\0" in data:
                continue
            text = data.decode("utf-8")
        except (OSError, UnicodeError, ValueError):
            continue
        result.append({"path": relative, "content": text})
        total += len(data)
    return result


def _validate_changes(workdir: Path, changes: Sequence[FileChange]) -> list[tuple[FileChange, Path]]:
    if not changes:
        raise ValueError("DeepSeek returned no changes")
    seen: set[str] = set()
    total = 0
    validated: list[tuple[FileChange, Path]] = []
    for change in changes:
        normalized = _relative_path(change.path).as_posix()
        if normalized in seen:
            raise ValueError("duplicate change path")
        seen.add(normalized)
        target = _safe_target(workdir, normalized)
        if target.exists() and not target.is_file():
            raise ValueError("non-file change target")
        if change.action == "delete" and not target.is_file():
            raise ValueError("delete target missing")
        if change.content is not None:
            size = len(change.content.encode("utf-8"))
            if size > _MAX_CHANGE_BYTES:
                raise ValueError("change too large")
            total += size
        validated.append((change.model_copy(update={"path": normalized}), target))
    if total > _MAX_TOTAL_CHANGE_BYTES:
        raise ValueError("changes too large")
    return validated


def _apply_changes(workdir: Path, changes: Sequence[FileChange]) -> list[str]:
    validated = _validate_changes(workdir, changes)
    originals: dict[Path, bytes | None] = {
        target: target.read_bytes() if target.exists() else None for _, target in validated
    }
    temporaries: set[Path] = set()
    try:
        for change, target in validated:
            if change.action == "delete":
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            encoded = change.content.encode("utf-8") if change.content is not None else b""
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".triagent-deepseek-", delete=False) as handle:
                handle.write(encoded)
                temporary = Path(handle.name)
            temporaries.add(temporary)
            os.replace(temporary, target)
            temporaries.discard(temporary)
    except Exception:
        for target, original in originals.items():
            try:
                if original is None:
                    if target.exists():
                        target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(original)
            except OSError:
                pass
        raise
    finally:
        for temporary in temporaries:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return [change.path for change, _ in validated]


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
        model: str = "deepseek-v4-flash",
        base_url: str = "https://api.deepseek.com",
        client_factory: Callable[..., Any] | None = None,
        api_key: str | None = None,
        runner: Any | None = None,
        probe_dir: Path | None = None,
        **legacy: Any,
    ) -> None:
        if legacy:
            raise TypeError("legacy OpenCode arguments are unsupported")
        if model not in _ALLOWED_MODELS:
            raise ValueError("unsupported DeepSeek model")
        self._enabled = enabled
        self._billing = billing_confirmed
        self._live_confirmed = live_confirmed
        self._estimated_usd = estimated_usd
        self._model = model
        self._base_url = _safe_base_url(base_url)
        self._api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        self._secrets = tuple(value for value in (*secret_values, self._api_key) if value)
        self._client_factory = client_factory or _default_client_factory
        self._client: Any | None = None
        self._ready_until = 0.0

    def estimate_cost(self, request: AgentRequest | None) -> CostEstimate:
        return CostEstimate(self._estimated_usd)

    def _new_client(self, timeout: float = 30):
        return self._client_factory(api_key=self._api_key, base_url=self._base_url, timeout=timeout)

    def capabilities(self) -> DeepSeekCapabilities:
        installed = importlib.util.find_spec("openai") is not None or self._client_factory is not _default_client_factory
        base = dict(enabled=self._enabled, billing_confirmed=self._billing)
        if not self._enabled:
            return DeepSeekCapabilities(available=False, installed=installed, authenticated=None, ready=False, diagnostic_code="deepseek-disabled", **base)
        if not self._billing:
            return DeepSeekCapabilities(available=False, installed=installed, authenticated=None, ready=False, diagnostic_code="deepseek-billing-not-confirmed", **base)
        if not self._live_confirmed:
            return DeepSeekCapabilities(available=False, installed=installed, authenticated=None, ready=False, diagnostic_code="deepseek-live-not-confirmed", **base)
        if not self._api_key:
            return DeepSeekCapabilities(available=False, installed=installed, authenticated=False, ready=False, diagnostic_code="deepseek-key-missing", **base)
        if not installed:
            return DeepSeekCapabilities(available=False, installed=False, authenticated=None, ready=False, diagnostic_code="deepseek-sdk-missing", **base)
        try:
            client = self._new_client()
            listed = self._model in _model_ids(client.models.list())
        except Exception as error:
            diagnostic = _api_error_diagnostic(error)
            authenticated = False if diagnostic == "deepseek-authentication-failed" else None
            return DeepSeekCapabilities(available=False, installed=installed, authenticated=authenticated, ready=False, diagnostic_code=diagnostic, **base)
        if not listed:
            return DeepSeekCapabilities(
                available=False, installed=installed, authenticated=True, headless=True,
                ready=False, api_configured_reachable=True, model_listed=False,
                diagnostic_code="deepseek-model-not-listed", **base,
            )
        try:
            response = client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": 'Return only this JSON object: {"status":"ok"}.'}],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=32,
            )
            payload, error = _decode_json_object(_message_content(response))
        except Exception as error:
            diagnostic = _api_error_diagnostic(error)
            return DeepSeekCapabilities(
                available=False, installed=installed,
                authenticated=False if diagnostic == "deepseek-authentication-failed" else True,
                headless=True, ready=False, api_configured_reachable=True,
                model_listed=True, diagnostic_code=diagnostic, **base,
            )
        if error is not None or payload != {"status": "ok"}:
            return DeepSeekCapabilities(
                available=False, installed=installed, authenticated=True, headless=True,
                ready=False, api_configured_reachable=True, model_listed=True,
                agent_tool_smoke_test=False, diagnostic_code="deepseek-smoke-invalid",
                **base,
            )
        self._client = client
        self._ready_until = time.monotonic() + 60
        return DeepSeekCapabilities(
            available=True, installed=installed, authenticated=True, headless=True,
            ready=True, api_configured_reachable=True, model_listed=True,
            agent_tool_smoke_test=True, **base,
        )

    def run(self, request: AgentRequest) -> AgentResult:
        if not (
            self._enabled and self._billing and self._live_confirmed and self._api_key
            and self._client is not None and self._ready_until >= time.monotonic()
        ):
            return AgentResult(status=AgentStatus.UNAVAILABLE, summary="DeepSeek live, billing, and readiness gates are incomplete")
        prompt, error = read_prompt(request)
        if error is not None:
            return error
        try:
            snapshot = _snapshot(request.workdir)
            schema = {
                "status": "passed|failed", "evidence": ["bounded factual evidence"],
                "artifacts": [],
                "changes": [{"path": "relative/path", "action": "write|delete", "content": "required for write only"}],
            }
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": "You are a patch-only coding agent. Never request or reveal secrets. Use only the supplied repository snapshot. Return exactly one JSON object."},
                    {"role": "user", "content": f"{prompt}\nDEEPSEEK_OUTPUT_SCHEMA_JSON={json.dumps(schema, separators=(',', ':'))}\nREPOSITORY_SNAPSHOT_JSON={json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))}"},
                ],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=8192,
            )
            payload, decode_error = _decode_json_object(_message_content(response))
            if decode_error is not None or payload is None:
                raise ValueError("invalid JSON")
            output = DeepSeekOutput.model_validate(payload)
            if output.status == "failed":
                if output.changes:
                    raise ValueError("failed result contains changes")
                return AgentResult(status=AgentStatus.SUCCEEDED, data={"status": "failed", "evidence": list(output.evidence), "artifacts": list(output.artifacts)})
            changed_paths = _apply_changes(request.workdir, output.changes)
            return AgentResult(
                status=AgentStatus.SUCCEEDED,
                data={"status": "passed", "evidence": list(output.evidence), "artifacts": list(output.artifacts), "changed_paths": changed_paths},
            )
        except (OSError, subprocess.SubprocessError):
            return AgentResult(status=AgentStatus.FAILED, summary="DeepSeek local patch preparation failed", data={"diagnostic_code": "deepseek-local-failure"})
        except (ValueError, ValidationError, TypeError, AttributeError):
            return AgentResult(status=AgentStatus.INVALID_OUTPUT, summary="DeepSeek returned an invalid patch", data={"diagnostic_code": "deepseek-patch-invalid"})
        except Exception:
            return AgentResult(status=AgentStatus.FAILED, summary="DeepSeek API request failed", data={"diagnostic_code": "deepseek-api-failed"})

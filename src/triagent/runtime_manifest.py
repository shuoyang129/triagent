"""Immutable, non-secret runtime bindings for v2 tasks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from triagent import __version__
_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
_SECRET_NAMES = {
    "codex": ("OPENAI_API_KEY",), "cursor": ("CURSOR_API_KEY",),
    "antigravity": ("AGY_API_KEY", "GOOGLE_API_KEY"),
    "deepseek": ("DEEPSEEK_API_KEY",), "fake": (),
}

class RuntimeManifestError(ValueError):
    """Raised when runtime bindings cannot be represented safely."""

def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()

def controller_commit(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    explicit = env.get("TRIAGENT_CONTROLLER_COMMIT", "")
    if explicit:
        if not _COMMIT.fullmatch(explicit):
            raise RuntimeManifestError("TRIAGENT_CONTROLLER_COMMIT must be a lowercase commit SHA")
        return explicit
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2], text=True, capture_output=True, check=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return "unattested-wheel"
    return result.stdout.strip() if _COMMIT.fullmatch(result.stdout.strip()) else "unattested-wheel"

def timeout_policy(environ: Mapping[str, str] | None = None) -> dict[str, object]:
    env = os.environ if environ is None else environ
    raw = env.get("TRIAGENT_AGENT_TIMEOUT_SECONDS", "900")
    try: seconds = int(raw)
    except ValueError as error: raise RuntimeManifestError("TRIAGENT_AGENT_TIMEOUT_SECONDS must be an integer") from error
    if not 60 <= seconds <= 3600: raise RuntimeManifestError("TRIAGENT_AGENT_TIMEOUT_SECONDS must be between 60 and 3600")
    payload: dict[str, object] = {"schema_version": 1, "kind": "legacy-global-timeout", "seconds": seconds}
    return {**payload, "digest": digest(payload)}

def _safe_command(command: Sequence[str]) -> list[str]:
    if not command or not all(isinstance(part, str) and part and "\n" not in part for part in command): raise RuntimeManifestError("runtime command must be a non-empty literal array")
    return list(command)

def _safe_version(version: object) -> str:
    if version is None: return "unprobed"
    if not isinstance(version, str) or not version or "\n" in version or len(version) > 512: raise RuntimeManifestError("provider version is invalid")
    return version

def build_runtime_manifest(*, profile_digest: str, providers: Mapping[str, tuple[str, Sequence[str], str | None, str | None]], environ: Mapping[str, str] | None = None) -> dict[str, object]:
    if not isinstance(profile_digest, str) or not _COMMIT.fullmatch(profile_digest): raise RuntimeManifestError("profile digest must be a SHA-256 hex digest")
    env = os.environ if environ is None else environ; provider_records: dict[str, dict[str, object]] = {}; secret_presence: dict[str, bool] = {}
    for role, (identity, command, model, version) in sorted(providers.items()):
        if role not in {"implementer", "verifier", "reviewer"}: raise RuntimeManifestError("runtime manifest has an unknown role")
        if identity not in _SECRET_NAMES: raise RuntimeManifestError("runtime manifest has an unknown provider")
        if model is not None and (not isinstance(model, str) or not model or "\n" in model): raise RuntimeManifestError("runtime model is invalid")
        provider_records[role] = {"provider": identity, "command": _safe_command(command), "version": _safe_version(version), "model": model}
        for name in _SECRET_NAMES[identity]: secret_presence[name] = bool(env.get(name))
    return {"schema_version": 1, "controller": {"version": __version__, "commit": controller_commit(env)}, "profile_digest": profile_digest, "providers": provider_records, "timeout_policy": timeout_policy(env), "secret_presence": dict(sorted(secret_presence.items()))}

def compare_manifests(recorded: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    changes: list[str] = []
    def walk(left: object, right: object, prefix: str) -> None:
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)): walk(left.get(key), right.get(key), f"{prefix}.{key}" if prefix else key)
        elif left != right: changes.append(prefix)
    walk(dict(recorded), dict(current), "")
    return changes

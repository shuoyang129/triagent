"""Offline release-binding and documentation-drift checks for v2.

These helpers deliberately read only local files.  They never construct an
adapter, invoke a provider, or inspect ambient provider configuration.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class DriftValidationError(ValueError):
    """A release binding is malformed or cannot be inspected safely."""


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_file(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise DriftValidationError(f"release binding is not a regular file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def profile_binding(profile_path: Path) -> dict[str, object]:
    """Return the precise local v2 profile and AGY wrapper binding."""
    if not profile_path.is_file() or profile_path.is_symlink():
        raise DriftValidationError("profile must be a regular file")
    try:
        config = tomllib.loads(profile_path.read_text(encoding="utf-8"))
        agents = config["agents"]
        deepseek = agents["deepseek"]
        agy_command = agents["antigravity"]["command"]
    except (KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise DriftValidationError("profile lacks required v2 agent bindings") from error
    if not isinstance(agy_command, list) or len(agy_command) != 1 or not isinstance(agy_command[0], str):
        raise DriftValidationError("Antigravity command must contain exactly one wrapper path")
    wrapper = Path(agy_command[0])
    if not wrapper.is_absolute():
        raise DriftValidationError("Antigravity wrapper path must be absolute")
    model = deepseek.get("model") if isinstance(deepseek, dict) else None
    enabled = deepseek.get("enabled") if isinstance(deepseek, dict) else None
    if not isinstance(model, str) or not model or not isinstance(enabled, bool):
        raise DriftValidationError("DeepSeek binding must declare model and enabled state")
    return {
        "schema_version": 1,
        "profile": {"path": str(profile_path), "sha256": sha256_file(profile_path)},
        "antigravity_wrapper": {"path": str(wrapper), "sha256": sha256_file(wrapper)},
        "deepseek": {"model": model, "enabled": enabled},
    }


def release_binding(profile_path: Path, documents: Mapping[str, Path]) -> dict[str, object]:
    """Produce a deterministic, content-addressed offline release snapshot."""
    binding = profile_binding(profile_path)
    docs: dict[str, dict[str, str]] = {}
    for name, path in sorted(documents.items()):
        if not name or not isinstance(path, Path):
            raise DriftValidationError("document bindings require named Paths")
        docs[name] = {"path": str(path), "sha256": sha256_file(path)}
    payload = {**binding, "documents": docs}
    return {**payload, "digest": hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()}


def compare_bindings(recorded: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """Return stable field paths rather than silently accepting changed bytes."""
    changes: list[str] = []

    def walk(left: object, right: object, prefix: str) -> None:
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                walk(left.get(key), right.get(key), f"{prefix}.{key}" if prefix else key)
        elif left != right:
            changes.append(prefix)

    walk(dict(recorded), dict(current), "")
    return changes


def documentation_drift(binding: Mapping[str, Any], documents: Mapping[str, Path]) -> list[str]:
    """Report semantic docs conflicts with an inspected v2 profile.

    The return value is intentionally diagnostic (not an exception): release
    gates can surface known legacy documentation drift without hiding it.
    """
    try:
        model = binding["deepseek"]["model"]
        enabled = binding["deepseek"]["enabled"]
    except (KeyError, TypeError) as error:
        raise DriftValidationError("binding lacks DeepSeek facts") from error
    if not isinstance(model, str) or not isinstance(enabled, bool):
        raise DriftValidationError("binding has invalid DeepSeek facts")
    expected_profile = "profiles/dgx.spark.v2.toml"
    expected_enabled = "enabled" if enabled else "disabled"
    results: list[str] = []
    for name, path in sorted(documents.items()):
        text = path.read_text(encoding="utf-8") if path.is_file() and not path.is_symlink() else ""
        folded = text.lower()
        if expected_profile not in text:
            results.append(f"{name}: profile-path")
        if model not in text:
            results.append(f"{name}: deepseek-model")
        if expected_enabled not in folded:
            results.append(f"{name}: deepseek-enabled-state")
    return results

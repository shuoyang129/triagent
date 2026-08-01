"""Strict, non-secret host binding for the isolated v2 controller."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import triagent.runtime as runtime


RUNTIME_CONFIG_ENV = "TRIAGENT_RUNTIME_CONFIG"
_PATH_FIELDS = {"data_root", "python"}


class RuntimeConfigError(ValueError):
    """Raised for untrusted, malformed, or secret-bearing runtime settings."""


@dataclass(frozen=True)
class RuntimeConfig:
    source: Path | None
    paths: dict[str, str]
    commands: dict[str, tuple[str, ...]]

    @property
    def data_root(self) -> Path:
        return Path(self.paths.get("data_root", str(runtime.DEFAULT_V2_DATA_ROOT)))

    def doctor_lines(self) -> list[str]:
        source = str(self.source) if self.source is not None else "built-in default"
        lines = [f"Runtime config: {source}"]
        for name in sorted(self.paths):
            lines.append(f"{name}: {self.paths[name]} (runtime config)")
        if "data_root" not in self.paths:
            lines.append(f"data_root: {runtime.DEFAULT_V2_DATA_ROOT} (built-in default)")
        for name in sorted(self.commands):
            lines.append(f"{name}.command: {' '.join(self.commands[name])} (runtime config)")
        return lines


def _safe_path(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or "$" in value or "`" in value or "~" in value:
        raise RuntimeConfigError(f"runtime {field} must be a literal path")
    path = Path(value)
    if not path.is_absolute():
        raise RuntimeConfigError(f"runtime {field} must be absolute")
    return str(path)


def _safe_command(value: object, *, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(part, str) and part for part in value):
        raise RuntimeConfigError(f"runtime agent {name!r} requires a non-empty command array")
    if any("$" in part or "`" in part or "\n" in part for part in value):
        raise RuntimeConfigError(f"runtime agent {name!r} command must be literal")
    executable = Path(value[0])
    if not executable.is_absolute():
        raise RuntimeConfigError(f"runtime agent {name!r} executable must be absolute")
    return tuple(value)


def load_runtime_config(environ: Mapping[str, str] | None = None) -> RuntimeConfig:
    env = os.environ if environ is None else environ
    raw_path = env.get(RUNTIME_CONFIG_ENV)
    if not raw_path:
        return RuntimeConfig(source=None, paths={}, commands={})
    source = Path(_safe_path(raw_path, field=RUNTIME_CONFIG_ENV))
    try:
        parsed = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise RuntimeConfigError("cannot read runtime config") from error
    if not isinstance(parsed, dict) or set(parsed) - {"paths", "agents"}:
        raise RuntimeConfigError("runtime config permits only [paths] and [agents]")
    raw_paths = parsed.get("paths", {})
    raw_agents = parsed.get("agents", {})
    if not isinstance(raw_paths, dict) or set(raw_paths) - _PATH_FIELDS:
        raise RuntimeConfigError("runtime config has unsupported path fields")
    if not isinstance(raw_agents, dict):
        raise RuntimeConfigError("runtime agents must be a table")
    paths = {name: _safe_path(value, field=name) for name, value in raw_paths.items()}
    commands: dict[str, tuple[str, ...]] = {}
    for name, section in raw_agents.items():
        if not isinstance(name, str) or not isinstance(section, dict) or set(section) != {"command"}:
            raise RuntimeConfigError("runtime agent entries permit only command")
        commands[name] = _safe_command(section["command"], name=name)
    return RuntimeConfig(source=source, paths=paths, commands=commands)

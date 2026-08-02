"""Pure, persistable timeout selection for the v2 controller.

This module deliberately has no provider, process, environment, or storage
dependencies.  A caller chooses either the compatibility policy (the legacy
single 900-second call bound) or a fully-specified v2 matrix, then persists the
returned selection before executing a provider process.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class TimeoutPolicyError(ValueError):
    """Raised when a timeout policy is incomplete, ambiguous, or unsafe."""


class Provider(StrEnum):
    CURSOR = "cursor"
    DEEPSEEK = "deepseek"
    CODEX = "codex"
    ANTIGRAVITY = "antigravity"
    FAKE = "fake"  # Test-only provider; real policy entries remain explicit.


class Stage(StrEnum):
    READINESS = "readiness"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    REVIEW = "review"
    REPAIR = "repair"


class TaskSize(StrEnum):
    TINY = "tiny"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _enum(value: str | StrEnum, enum_type: type[StrEnum], field: str) -> StrEnum:
    # Do not normalize case or aliases: persisted selections must be unambiguous.
    if isinstance(value, bool) or not isinstance(value, str):
        raise TimeoutPolicyError(f"{field} must be one of: {', '.join(item.value for item in enum_type)}")
    try:
        return enum_type(value)
    except ValueError as error:
        raise TimeoutPolicyError(f"{field} must be one of: {', '.join(item.value for item in enum_type)}") from error


def _seconds(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TimeoutPolicyError(f"{field} must be a finite positive number")
    value = float(value)
    if not 0 < value <= 86_400:
        raise TimeoutPolicyError(f"{field} must be between 0 and 86400 seconds")
    return value


@dataclass(frozen=True)
class StreamTimeouts:
    """The five bounds consumed by :class:`StreamingProcessRunner`."""

    startup_timeout: float
    idle_timeout: float
    hard_timeout: float
    finalize_grace: float
    terminate_grace: float

    def __post_init__(self) -> None:
        for name in ("startup_timeout", "idle_timeout", "hard_timeout", "finalize_grace", "terminate_grace"):
            object.__setattr__(self, name, _seconds(getattr(self, name), name))
        if self.startup_timeout > self.hard_timeout:
            raise TimeoutPolicyError("startup_timeout cannot exceed hard_timeout")
        if self.idle_timeout > self.hard_timeout:
            raise TimeoutPolicyError("idle_timeout cannot exceed hard_timeout")
        if self.finalize_grace > self.hard_timeout:
            raise TimeoutPolicyError("finalize_grace cannot exceed hard_timeout")
        if self.terminate_grace > self.finalize_grace:
            raise TimeoutPolicyError("terminate_grace cannot exceed finalize_grace")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "StreamTimeouts":
        expected = {"startup_timeout", "idle_timeout", "hard_timeout", "finalize_grace", "terminate_grace"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise TimeoutPolicyError("stream timeout policy must contain exactly the five timeout fields")
        return cls(**{name: value[name] for name in expected})

    def as_dict(self) -> dict[str, float]:
        return {
            "startup_timeout": self.startup_timeout,
            "idle_timeout": self.idle_timeout,
            "hard_timeout": self.hard_timeout,
            "finalize_grace": self.finalize_grace,
            "terminate_grace": self.terminate_grace,
        }


@dataclass(frozen=True)
class TimeoutSelection:
    provider: Provider
    stage: Stage
    size: TaskSize
    policy: StreamTimeouts

    def persistable(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 1,
            "kind": "v2-stream-timeouts",
            "provider": self.provider.value,
            "stage": self.stage.value,
            "size": self.size.value,
            "policy": self.policy.as_dict(),
        }
        return {**payload, "digest": _digest(payload)}


class TimeoutMatrix:
    """A complete provider × stage × size policy table.

    Requiring every cell prevents accidental fallback to a shorter or otherwise
    unsafe bound when a provider or stage is added.
    """

    def __init__(self, entries: Mapping[tuple[Provider, Stage, TaskSize], StreamTimeouts]) -> None:
        required = {(provider, stage, size) for provider in Provider for stage in Stage for size in TaskSize}
        if set(entries) != required:
            missing = len(required - set(entries))
            extra = len(set(entries) - required)
            raise TimeoutPolicyError(f"timeout matrix must contain every provider/stage/size cell (missing={missing}, extra={extra})")
        if not all(isinstance(policy, StreamTimeouts) for policy in entries.values()):
            raise TimeoutPolicyError("timeout matrix entries must be StreamTimeouts")
        self._entries = dict(entries)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Mapping[str, object]]) -> "TimeoutMatrix":
        if not isinstance(value, Mapping):
            raise TimeoutPolicyError("timeout matrix must be a mapping")
        entries: dict[tuple[Provider, Stage, TaskSize], StreamTimeouts] = {}
        for key, policy in value.items():
            if not isinstance(key, str) or key.count(".") != 2:
                raise TimeoutPolicyError("timeout matrix keys must be provider.stage.size")
            raw_provider, raw_stage, raw_size = key.split(".")
            selector = (
                _enum(raw_provider, Provider, "provider"),
                _enum(raw_stage, Stage, "stage"),
                _enum(raw_size, TaskSize, "size"),
            )
            if selector in entries:
                raise TimeoutPolicyError("timeout matrix contains duplicate selector")
            entries[selector] = StreamTimeouts.from_mapping(policy)
        return cls(entries)

    def select(self, provider: str | Provider, stage: str | Stage, size: str | TaskSize) -> TimeoutSelection:
        selector = (
            _enum(provider, Provider, "provider"),
            _enum(stage, Stage, "stage"),
            _enum(size, TaskSize, "size"),
        )
        return TimeoutSelection(*selector, self._entries[selector])

    def persistable(self) -> dict[str, object]:
        cells = {
            f"{provider.value}.{stage.value}.{size.value}": self._entries[provider, stage, size].as_dict()
            for provider in Provider for stage in Stage for size in TaskSize
        }
        payload: dict[str, object] = {"schema_version": 1, "kind": "v2-timeout-matrix", "cells": cells}
        return {**payload, "digest": _digest(payload)}


@dataclass(frozen=True)
class LegacyTimeout:
    """The original controller's single call limit, intentionally unchanged."""

    seconds: int = 900

    def __post_init__(self) -> None:
        if isinstance(self.seconds, bool) or not isinstance(self.seconds, int) or not 60 <= self.seconds <= 3600:
            raise TimeoutPolicyError("TRIAGENT_AGENT_TIMEOUT_SECONDS must be between 60 and 3600")

    def persistable(self) -> dict[str, object]:
        payload: dict[str, object] = {"schema_version": 1, "kind": "legacy-global-timeout", "seconds": self.seconds}
        return {**payload, "digest": _digest(payload)}


def legacy_timeout(environ: Mapping[str, str] | None = None) -> LegacyTimeout:
    raw = (os.environ if environ is None else environ).get("TRIAGENT_AGENT_TIMEOUT_SECONDS", "900")
    try:
        # ``int`` deliberately rejects fractional strings; whitespace behavior
        # remains the original controller's behavior for compatibility.
        seconds = int(raw)
    except (TypeError, ValueError) as error:
        raise TimeoutPolicyError("TRIAGENT_AGENT_TIMEOUT_SECONDS must be an integer") from error
    return LegacyTimeout(seconds)


def default_v2_matrix() -> TimeoutMatrix:
    """Return a deterministic, explicit baseline for every v2 selector.

    The caller persists the selected cell, not this implicit default.  Values
    are conservative and all hard limits remain within the legacy maximum.
    """
    size_bounds = {
        TaskSize.TINY: (60, 45, 600, 60, 15),
        TaskSize.SMALL: (60, 60, 900, 60, 15),
        TaskSize.MEDIUM: (90, 90, 1800, 90, 30),
        TaskSize.LARGE: (120, 120, 3600, 120, 30),
    }
    # Codex may construct its read-only sandbox response before emitting its first JSON event.
    # This preserves the meaningful-progress idle bound and hard ceiling.
    startup_extra = {Provider.CURSOR: 30, Provider.DEEPSEEK: 60, Provider.CODEX: 120, Provider.ANTIGRAVITY: 60, Provider.FAKE: 0}
    stage_hard_factor = {Stage.READINESS: 0.5, Stage.IMPLEMENT: 1.0, Stage.VERIFY: 0.75, Stage.REVIEW: 0.75, Stage.REPAIR: 1.0}
    entries: dict[tuple[Provider, Stage, TaskSize], StreamTimeouts] = {}
    for provider in Provider:
        for stage in Stage:
            for size in TaskSize:
                startup, idle, hard, finalize, terminate = size_bounds[size]
                hard = max(startup + startup_extra[provider], int(hard * stage_hard_factor[stage]))
                entries[provider, stage, size] = StreamTimeouts(
                    startup + startup_extra[provider], min(idle, hard), hard, min(finalize, hard), min(terminate, finalize),
                )
    return TimeoutMatrix(entries)

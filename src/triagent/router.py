from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from triagent.domain import RiskLevel


@dataclass(frozen=True)
class ImplementationChoice:
    name: str


class ImplementationRouter:
    def choose(
        self,
        usage: float | None = None,
        capabilities: Mapping[str, Any] | None = None,
        risk: RiskLevel | str = RiskLevel.LOW,
        *,
        cursor_usage: float | None = None,
        cursor_quota_error: bool = False,
    ) -> ImplementationChoice:
        measured = cursor_usage if cursor_usage is not None else usage
        if measured is None or not 0 <= measured <= 1:
            raise ValueError("Cursor usage must be between 0 and 1")
        available = capabilities or {}
        cursor = self._available(available.get("cursor"))
        deepseek = self._available(available.get("deepseek"))
        risk_level = RiskLevel(risk)

        handoff = cursor_quota_error or measured >= 0.90
        saver = measured >= 0.70 and risk_level is RiskLevel.LOW
        if (handoff or saver) and deepseek:
            return ImplementationChoice("deepseek")
        if cursor_quota_error:
            raise RuntimeError("No suitable implementation agent is available")
        if cursor:
            return ImplementationChoice("cursor")
        if deepseek and risk_level is RiskLevel.LOW:
            return ImplementationChoice("deepseek")
        raise RuntimeError("No suitable implementation agent is available")

    @staticmethod
    def _available(value: Any) -> bool:
        return value if isinstance(value, bool) else bool(getattr(value, "available", False))

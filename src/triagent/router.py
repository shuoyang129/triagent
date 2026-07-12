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
        risk_level = RiskLevel(risk)
        cursor = self._suitable(available.get("cursor"), risk_level)
        deepseek = self._suitable(available.get("deepseek"), risk_level)

        handoff = cursor_quota_error or measured >= 0.90
        saver = measured >= 0.70 and risk_level is RiskLevel.LOW
        if handoff:
            if deepseek:
                return ImplementationChoice("deepseek")
            raise RuntimeError("No suitable implementation agent is available")
        if saver and deepseek:
            return ImplementationChoice("deepseek")
        if cursor:
            return ImplementationChoice("cursor")
        if deepseek and risk_level is RiskLevel.LOW:
            return ImplementationChoice("deepseek")
        raise RuntimeError("No suitable implementation agent is available")

    @staticmethod
    def _suitable(value: Any, risk: RiskLevel) -> bool:
        if isinstance(value, Mapping):
            available = bool(value.get("available", False))
            risks = value.get("risks")
        else:
            available = value if isinstance(value, bool) else bool(getattr(value, "available", False))
            risks = getattr(value, "risks", None)
        if not available:
            return False
        if risk is RiskLevel.LOW:
            return True
        return risks is not None and risk.value in {str(item) for item in risks}

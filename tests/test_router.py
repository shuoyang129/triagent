from types import SimpleNamespace

import pytest

from triagent.domain import RiskLevel
from triagent.router import ImplementationRouter


@pytest.mark.parametrize(("usage", "expected"), [(0.69, "cursor"), (0.70, "deepseek"), (0.90, "deepseek")])
def test_cursor_budget_thresholds(usage: float, expected: str) -> None:
    capabilities = {"cursor": SimpleNamespace(available=True), "deepseek": SimpleNamespace(available=True)}
    assert ImplementationRouter().choose(cursor_usage=usage, capabilities=capabilities, risk="low").name == expected


def test_high_risk_does_not_route_to_deepseek_saver() -> None:
    capabilities = {
        "cursor": {"available": True, "risks": {"low", "medium", "high"}},
        "deepseek": {"available": True, "risks": {"low"}},
    }
    assert ImplementationRouter().choose(0.75, capabilities, RiskLevel.HIGH).name == "cursor"


def test_high_risk_handoff_requires_explicit_deepseek_suitability() -> None:
    capabilities = {
        "cursor": {"available": True, "risks": {"low", "medium", "high"}},
        "deepseek": {"available": True, "risks": {"low"}},
    }
    with pytest.raises(RuntimeError, match="No suitable implementation agent"):
        ImplementationRouter().choose(0.90, capabilities, RiskLevel.HIGH)
    capabilities["deepseek"]["risks"].add("high")
    assert ImplementationRouter().choose(0.90, capabilities, RiskLevel.HIGH).name == "deepseek"


def test_quota_error_hands_off_and_unavailable_agents_fail_safely() -> None:
    router = ImplementationRouter()
    assert router.choose(0.1, {"cursor": True, "deepseek": True}, "low", cursor_quota_error=True).name == "deepseek"
    with pytest.raises(RuntimeError, match="No suitable implementation agent"):
        router.choose(0.5, {"cursor": False, "deepseek": False}, "low")
    with pytest.raises(RuntimeError, match="No suitable implementation agent"):
        router.choose(0.5, {"cursor": True, "deepseek": False}, "low", cursor_quota_error=True)

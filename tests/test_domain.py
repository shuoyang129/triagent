import pytest
from pydantic import ValidationError

from triagent.domain import Budget, RiskLevel, TaskSpec


def test_robot_safety_requires_visual_approval() -> None:
    spec = TaskSpec(
        goal="Validate a walking controller",
        scope=["robot/"],
        acceptance=["simulation passes"],
        risk=RiskLevel.ROBOT_SAFETY,
    )
    assert spec.visual_check == "required"


@pytest.mark.parametrize("field", ["goal", "scope", "acceptance"])
def test_task_spec_rejects_empty_required_values(field: str) -> None:
    values = {
        "goal": "Add endpoint",
        "scope": ["src/"],
        "acceptance": ["tests pass"],
    }
    values[field] = "" if field == "goal" else []
    with pytest.raises(ValidationError):
        TaskSpec(**values)


def test_budget_rejects_negative_values() -> None:
    with pytest.raises(ValidationError):
        Budget(max_agent_calls=-1, max_minutes=10, max_usd=0)

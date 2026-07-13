from pathlib import Path

from triagent.adapters.base import AgentResult, AgentStatus
from triagent.adapters.fake import FakeAgent
from triagent.domain import Budget, RiskLevel, TaskSpec, TaskState
from triagent.orchestrator import Orchestrator
from triagent.store import TaskStore


def make_spec(*, risk: RiskLevel = RiskLevel.LOW, max_calls: int = 20) -> TaskSpec:
    return TaskSpec(
        goal="Change behavior",
        scope=["src/"],
        acceptance=["tests pass"],
        risk=risk,
        budget=Budget(max_agent_calls=max_calls, max_minutes=60, max_usd=0),
    )


def make_orchestrator(root: Path, reviewer: FakeAgent, implementer: FakeAgent | None = None) -> tuple[Orchestrator, TaskStore]:
    store = TaskStore(root)
    verification = AgentResult(status=AgentStatus.SUCCEEDED, summary="verified")
    return (
        Orchestrator(
            store=store,
            implementer=implementer or FakeAgent.succeeding("implemented"),
            verifier=FakeAgent([verification for _ in range(10)]),
            reviewer=reviewer,
        ),
        store,
    )


def test_happy_path_reaches_approval(tmp_path: Path) -> None:
    orchestrator, store = make_orchestrator(tmp_path, FakeAgent.succeeding("clean"))
    task = store.create_task(make_spec())
    orchestrator.run_until_blocked(task.id)
    assert store.load(task.id).state is TaskState.APPROVAL


def test_robot_task_waits_for_visual_confirmation(tmp_path: Path) -> None:
    orchestrator, store = make_orchestrator(tmp_path, FakeAgent.succeeding("clean"))
    task = store.create_task(make_spec(risk=RiskLevel.ROBOT_SAFETY))
    import pytest
    with pytest.raises(ValueError, match="visual artifact"):
        orchestrator.run_until_blocked(task.id)
    assert store.load(task.id).state is TaskState.FAILED_RECOVERABLE


def test_budget_exhaustion_pauses_before_next_agent_call(tmp_path: Path) -> None:
    orchestrator, store = make_orchestrator(tmp_path, FakeAgent.succeeding("clean"))
    task = store.create_task(make_spec(max_calls=1))
    orchestrator.run_until_blocked(task.id)
    assert store.load(task.id).state is TaskState.PAUSED_BUDGET
    assert store.runtime(task.id).agent_calls == 1


def test_low_risk_task_stops_after_two_failed_repairs(tmp_path: Path) -> None:
    major = AgentResult(
        status=AgentStatus.SUCCEEDED,
        summary="major finding",
        data={"findings": [{"severity": "MAJOR", "message": "broken"}]},
    )
    reviewer = FakeAgent([major, major, major])
    implementer = FakeAgent(
        [
            AgentResult(status=AgentStatus.SUCCEEDED, summary="initial"),
            AgentResult(status=AgentStatus.SUCCEEDED, summary="repair one"),
            AgentResult(status=AgentStatus.SUCCEEDED, summary="repair two"),
        ]
    )
    orchestrator, store = make_orchestrator(tmp_path, reviewer, implementer)
    task = store.create_task(make_spec())
    orchestrator.run_until_blocked(task.id)
    assert store.load(task.id).state is TaskState.FAILED_FINAL
    assert store.runtime(task.id).repair_attempts == 2


def test_runtime_counters_survive_store_reopen(tmp_path: Path) -> None:
    orchestrator, store = make_orchestrator(tmp_path, FakeAgent.succeeding("clean"))
    task = store.create_task(make_spec(max_calls=1))
    orchestrator.run_until_blocked(task.id)
    reopened = TaskStore(tmp_path)
    assert reopened.runtime(task.id).agent_calls == 1


def test_failed_agent_call_persists_only_allowlisted_diagnostic_code(tmp_path: Path) -> None:
    failed = AgentResult(
        status=AgentStatus.INVALID_OUTPUT,
        summary="vendor text must not be persisted",
        data={"diagnostic_code": "cursor-result-non-json"},
    )
    orchestrator, store = make_orchestrator(
        tmp_path,
        FakeAgent.succeeding("clean"),
        implementer=FakeAgent([failed]),
    )
    task = store.create_task(make_spec())

    orchestrator.run_until_blocked(task.id)

    with store._connect() as connection:
        row = connection.execute(
            "SELECT diagnostic FROM agent_calls WHERE task_id = ?",
            (task.id,),
        ).fetchone()
    assert row["diagnostic"] == "cursor-result-non-json"

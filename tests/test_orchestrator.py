from pathlib import Path

import pytest

from triagent.adapters.base import AgentResult, AgentRole, AgentStatus
from triagent.adapters.fake import FakeAgent
from triagent.domain import Budget, RiskLevel, TaskSpec, TaskState
from triagent.orchestrator import Orchestrator
from triagent.store import LeaseConflict, TaskStore


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


def test_agent_call_lease_covers_request_timeout_through_post_call_renewal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRIAGENT_AGENT_TIMEOUT_SECONDS", "901")
    implementer = FakeAgent.succeeding("implemented")
    orchestrator, store = make_orchestrator(
        tmp_path, FakeAgent.succeeding("clean"), implementer
    )
    task = store.create_task(make_spec())
    request = orchestrator._request(
        task.id, AgentRole.IMPLEMENTER, "implementation-result-v1", implementer
    )
    owner = "lease-owner"
    store.acquire_lease(task.id, owner, 600)
    orchestrator._lease_owner = owner
    renewals: list[float] = []
    agent_returned = False
    original_renew = store.renew_lease
    original_run = implementer.run

    def tracked_renew(task_id: str, lease_owner: str, seconds: float) -> None:
        renewals.append(seconds)
        if agent_returned and seconds < request.timeout_seconds + 60:
            raise LeaseConflict("controller lease lost after long agent call")
        original_renew(task_id, lease_owner, seconds)

    def tracked_run(agent_request):
        nonlocal agent_returned
        result = original_run(agent_request)
        agent_returned = True
        return result

    monkeypatch.setattr(store, "renew_lease", tracked_renew)
    monkeypatch.setattr(implementer, "run", tracked_run)
    try:
        result = orchestrator._call(
            task.id, TaskState.SPEC, implementer, request
        )
    finally:
        orchestrator._lease_owner = None
        store.release_lease(task.id, owner)

    assert result is not None
    assert renewals == [961, 961]


def test_repair_limit_override_is_bounded_and_opt_in(monkeypatch) -> None:
    assert Orchestrator._repair_limit(RiskLevel.ROBOT_SAFETY) == 3
    monkeypatch.setenv("TRIAGENT_REPAIR_ATTEMPT_LIMIT", "6")
    assert Orchestrator._repair_limit(RiskLevel.ROBOT_SAFETY) == 6
    monkeypatch.setenv("TRIAGENT_REPAIR_ATTEMPT_LIMIT", "21")
    assert Orchestrator._repair_limit(RiskLevel.ROBOT_SAFETY) == 3
    monkeypatch.setenv("TRIAGENT_REPAIR_ATTEMPT_LIMIT", "invalid")
    assert Orchestrator._repair_limit(RiskLevel.ROBOT_SAFETY) == 3


def test_resume_value_error_after_checkpoint_acceptance_restores_recoverable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator, store = make_orchestrator(
        tmp_path, FakeAgent.succeeding("clean")
    )
    task = store.create_task(make_spec())
    store.record_execution_provenance(
        task.id,
        mode="simulation",
        implementer="fake",
        verifier="fake",
        reviewer="fake",
        profile_digest="fake-v1",
    )
    store.transition_recoverable(
        task.id, TaskState.SPEC, "review", "initial-review-failure"
    )
    original = store.recovery_checkpoint(task.id)
    assert original is not None
    monkeypatch.setattr(store, "restore_candidate_worktree", lambda _task_id: None)

    def fail_after_acceptance(_task_id: str) -> TaskState:
        assert store.load(task.id).state is TaskState.REVIEW
        raise ValueError("post-acceptance failure")

    monkeypatch.setattr(orchestrator, "advance", fail_after_acceptance)

    with pytest.raises(ValueError, match="post-acceptance failure"):
        orchestrator.resume_until_blocked(task.id)

    assert store.load(task.id).state is TaskState.FAILED_RECOVERABLE
    replacement = store.recovery_checkpoint(task.id)
    assert replacement is not None
    assert replacement["stage"] == "review"
    assert replacement["sequence"] > original["sequence"]


@pytest.mark.parametrize(
    "diagnostic_code",
    [
        "agy-empty-output",
        "cursor-result-non-json",
        "json-malformed",
        "json-non-object",
    ],
)
def test_failed_agent_call_persists_only_allowlisted_diagnostic_code(
    tmp_path: Path, diagnostic_code: str
) -> None:
    failed = AgentResult(
        status=AgentStatus.INVALID_OUTPUT,
        summary="vendor text must not be persisted",
        data={"diagnostic_code": diagnostic_code},
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
    assert row["diagnostic"] == diagnostic_code

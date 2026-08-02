import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from triagent.adapters.base import AgentResult, AgentRole, AgentStatus
from triagent.adapters.fake import FakeAgent
from triagent.adapters.cursor import CursorAdapter
from triagent.adapters.codex import CodexAdapter
from triagent.adapters.antigravity import AntigravityAdapter
from triagent.adapters.deepseek import DeepSeekAdapter
from triagent.domain import Budget, RiskLevel, TaskSpec, TaskState
from triagent.git_workspace import GitWorkspace
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


def test_reviewer_unavailable_is_retried_once_without_user_resume(
    tmp_path: Path,
) -> None:
    reviewer = FakeAgent([
        AgentResult(status=AgentStatus.UNAVAILABLE, summary="transient"),
        AgentResult(status=AgentStatus.SUCCEEDED, summary="clean"),
    ])
    orchestrator, store = make_orchestrator(tmp_path, reviewer)
    task = store.create_task(make_spec())

    assert orchestrator.run_until_blocked(task.id) is TaskState.APPROVAL
    assert store.runtime(task.id).agent_calls == 4
    assert store.recovery_checkpoint(task.id) is None
    assert store.outcomes(task.id)["review"].status == "passed"


def test_reviewer_second_unavailable_remains_recoverable(
    tmp_path: Path,
) -> None:
    reviewer = FakeAgent([
        AgentResult(status=AgentStatus.UNAVAILABLE, summary="first transient"),
        AgentResult(status=AgentStatus.UNAVAILABLE, summary="still unavailable"),
    ])
    orchestrator, store = make_orchestrator(tmp_path, reviewer)
    task = store.create_task(make_spec())

    assert orchestrator.run_until_blocked(task.id) is TaskState.FAILED_RECOVERABLE
    assert store.runtime(task.id).agent_calls == 4
    checkpoint = store.recovery_checkpoint(task.id)
    assert checkpoint is not None
    assert checkpoint["stage"] == "review"
    assert checkpoint["sequence"] == 1


def test_reviewer_unavailable_retry_never_bypasses_call_budget(
    tmp_path: Path,
) -> None:
    reviewer = FakeAgent([
        AgentResult(status=AgentStatus.UNAVAILABLE, summary="transient"),
        AgentResult(status=AgentStatus.SUCCEEDED, summary="clean"),
    ])
    orchestrator, store = make_orchestrator(tmp_path, reviewer)
    task = store.create_task(make_spec(max_calls=3))

    assert orchestrator.run_until_blocked(task.id) is TaskState.PAUSED_BUDGET
    assert store.runtime(task.id).agent_calls == 3
    assert store.recovery_checkpoint(task.id) is None


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


def test_expired_deepseek_readiness_refresh_is_budgeted_before_repair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = TaskStore(tmp_path / "data")
    task = store.create_task(make_spec(max_calls=4).model_copy(update={"budget": Budget(max_agent_calls=4, max_minutes=60, max_usd=10)}))
    adapter = DeepSeekAdapter(enabled=True, billing_confirmed=True, live_confirmed=True, estimated_usd=1.0)
    orchestrator = Orchestrator(store, adapter, FakeAgent.succeeding("verify"), FakeAgent.succeeding("review"), implementer_probe_estimated_usd=.25)
    store.acquire_lease(task.id, "owner", 600)
    orchestrator._lease_owner = "owner"
    calls: list[str] = []

    def refreshed():
        calls.append("probe")
        adapter._ready_until = time.monotonic() + 60
        return SimpleNamespace(available=True, ready=True)

    monkeypatch.setattr(adapter, "capabilities", refreshed)

    assert orchestrator._refresh_deepseek_readiness(task.id, adapter) is None
    assert calls == ["probe"]
    assert store.runtime(task.id).agent_calls == 1
    assert store.runtime(task.id).usd_spent == .25


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


def test_stream_hard_timeout_extends_controller_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    implementer = FakeAgent.succeeding("implemented")
    implementer._stream_policy = SimpleNamespace(hard_timeout=2700)
    orchestrator, store = make_orchestrator(tmp_path, FakeAgent.succeeding("clean"), implementer)
    task = store.create_task(make_spec())
    request = orchestrator._request(task.id, AgentRole.IMPLEMENTER, "implementation-result-v1", implementer)
    owner = "stream-lease-owner"
    store.acquire_lease(task.id, owner, 600)
    orchestrator._lease_owner = owner
    renewals: list[float] = []
    original_renew = store.renew_lease

    def tracked(task_id: str, lease_owner: str, seconds: float) -> None:
        renewals.append(seconds)
        original_renew(task_id, lease_owner, seconds)

    monkeypatch.setattr(store, "renew_lease", tracked)
    try:
        assert orchestrator._call(task.id, TaskState.SPEC, implementer, request) is not None
    finally:
        orchestrator._lease_owner = None
        store.release_lease(task.id, owner)

    assert renewals == [2760.0, 2760.0]


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
        "deepseek-api-failed",
        "deepseek-local-failure",
        "deepseek-patch-invalid",
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


@pytest.mark.parametrize(
    ("adapter_name", "role", "stage", "schema"),
    [
        ("codex", AgentRole.VERIFIER, TaskState.VERIFY, "verification-result-v1"),
        ("antigravity", AgentRole.REVIEWER, TaskState.REVIEW, "review-result-v1"),
    ],
)
def test_live_review_durable_result_recovers_after_crash_without_second_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter_name: str, role: AgentRole,
    stage: TaskState, schema: str,
) -> None:
    """A persisted real verifier/reviewer result is consumed after restart, not rerun."""
    store = TaskStore(tmp_path / "data")
    task = store.create_task(TaskSpec(
        goal="live durable verifier", scope=[str(tmp_path)], acceptance=["offline"],
        budget=Budget(max_agent_calls=4, max_minutes=60, max_usd=10),
    ))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store.set_workspace(task.id, str(worktree), "a" * 40, "triagent/test")
    with store._connect() as connection:
        connection.execute(
            "UPDATE workspace_meta SET reviewed_commit=?, candidate_ref=? WHERE task_id=?",
            ("b" * 40, "refs/triagent/reviewed/test", task.id),
        )
    (store.runs_root / task.id / "handoff.json").write_text("{}", encoding="utf-8")
    store.record_runtime_manifest(task.id, {"runtime": "live-v2-test"})
    store.transition(task.id, TaskState.SPEC, TaskState.IMPLEMENT, "test")
    store.transition(task.id, TaskState.IMPLEMENT, TaskState.VERIFY, "test")

    implementer = CursorAdapter(estimated_usd=1.0)
    verifier = CodexAdapter(estimated_usd=1.0)
    reviewer = AntigravityAdapter(estimated_usd=1.0)
    calls: list[str] = []

    def successful_verification(_request):
        calls.append(adapter_name)
        return AgentResult(
            status=AgentStatus.SUCCEEDED,
            data={"status": "passed", "evidence": ["offline-proof"], "artifacts": [], "findings": []},
            actual_usd=1.0,
        )

    adapter = verifier if adapter_name == "codex" else reviewer
    monkeypatch.setattr(adapter, "run", successful_verification)
    first = Orchestrator(store, implementer, verifier, reviewer, profile_digest="c" * 64)
    owner = "owner-first"
    store.acquire_lease(task.id, owner, 600)
    first._lease_owner = owner
    original = store.record_durable_completion

    def crash_after_atomic_commit(**kwargs):
        original(**kwargs)
        raise RuntimeError("injected crash after durable commit")

    monkeypatch.setattr(store, "record_durable_completion", crash_after_atomic_commit)
    request = first._request(task.id, role, schema, adapter)
    assert first._durable_context(task.id, stage, adapter, request) is not None
    with pytest.raises(RuntimeError, match="durable commit"):
        first._call(task.id, stage, adapter, request)
    assert calls == [adapter_name]
    store.release_lease(task.id, owner)

    monkeypatch.setattr(store, "record_durable_completion", original)
    restarted = Orchestrator(store, implementer, verifier, reviewer, profile_digest="c" * 64)
    resumed_owner = "owner-restarted"
    store.acquire_lease(task.id, resumed_owner, 600)
    restarted._lease_owner = resumed_owner
    replayed = restarted._call(task.id, stage, adapter, request)

    assert replayed is not None and replayed.data["evidence"] == ["offline-proof"]
    assert calls == [adapter_name]
    assert store.runtime(task.id).completed_calls == 1


def test_live_implementation_durable_result_recovers_after_crash_without_second_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test.invalid"],
        ["git", "config", "user.name", "TriAgent Test"],
        ["git", "add", "README.md"],
        ["git", "commit", "-qm", "base"],
    ):
        subprocess.run(command, cwd=repo, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    store = TaskStore(tmp_path / "data")
    task = store.create_task(TaskSpec(
        goal="durable implementation", scope=[str(repo)], acceptance=["offline"],
        budget=Budget(max_agent_calls=4, max_minutes=60, max_usd=10),
    ))
    worktree = store.runs_root / task.id / "worktree"
    worktree.rmdir()
    GitWorkspace.create(repo, task.id, destination=worktree)
    store.set_workspace(task.id, str(repo), base, f"triagent/{task.id}")
    store.record_runtime_manifest(task.id, {"runtime": "live-v2-test"})
    store.transition(task.id, TaskState.SPEC, TaskState.IMPLEMENT, "test")

    calls: list[str] = []
    implementer = DeepSeekAdapter(
        enabled=True, billing_confirmed=True, live_confirmed=True, estimated_usd=1.0,
    )

    def successful_implementation(request):
        calls.append("deepseek")
        (request.workdir / "README.md").write_text("durable candidate\n", encoding="utf-8")
        return AgentResult(
            status=AgentStatus.SUCCEEDED,
            data={"status": "passed", "evidence": ["offline-proof"], "artifacts": [], "changed_paths": ["README.md"]},
            actual_usd=1.0,
        )

    monkeypatch.setattr(implementer, "run", successful_implementation)
    verifier = CodexAdapter(estimated_usd=1.0)
    reviewer = AntigravityAdapter(estimated_usd=1.0)
    first = Orchestrator(store, implementer, verifier, reviewer, profile_digest="c" * 64)
    owner = "owner-first"
    store.acquire_lease(task.id, owner, 600)
    first._lease_owner = owner
    original = store.record_durable_completion

    def crash_after_atomic_commit(**kwargs):
        original(**kwargs)
        raise RuntimeError("injected crash after durable commit")

    monkeypatch.setattr(store, "record_durable_completion", crash_after_atomic_commit)
    request = first._request(task.id, AgentRole.IMPLEMENTER, "implementation-result-v1", implementer)
    with pytest.raises(RuntimeError, match="durable commit"):
        first._call(task.id, TaskState.IMPLEMENT, implementer, request)
    assert calls == ["deepseek"]
    assert store.workspace(task.id)["reviewed_commit"]
    store.release_lease(task.id, owner)

    monkeypatch.setattr(store, "record_durable_completion", original)
    restarted = Orchestrator(store, implementer, verifier, reviewer, profile_digest="c" * 64)
    resumed_owner = "owner-restarted"
    store.acquire_lease(task.id, resumed_owner, 600)
    restarted._lease_owner = resumed_owner
    replayed = restarted._call(task.id, TaskState.IMPLEMENT, implementer, request)

    assert replayed is not None and replayed.data["evidence"] == ["offline-proof"]
    assert calls == ["deepseek"]
    assert store.runtime(task.id).completed_calls == 1

"""Golden contracts inherited from the frozen public ``triagent`` controller.

These tests intentionally use only fake adapters.  They express the observable
legacy protocol that a v2 run must preserve while v2-only features are unused.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from triagent.adapters.antigravity import AntigravityAdapter
from triagent.adapters.base import AgentResult, AgentRole, AgentStatus
from triagent.adapters.codex import CodexAdapter
from triagent.adapters.cursor import CursorAdapter
from triagent.adapters.fake import FakeAgent
from triagent.adapters.process import ProcessResult
from triagent.domain import Budget, TaskSpec, TaskState
from triagent.orchestrator import Orchestrator
from triagent.store import LeaseConflict, TaskStore


def _spec() -> TaskSpec:
    return TaskSpec(
        goal="legacy contract",
        scope=["src/"],
        acceptance=["tests pass"],
        budget=Budget(max_agent_calls=20, max_minutes=60, max_usd=0),
    )


def _orchestrator(root, reviewer: FakeAgent) -> tuple[Orchestrator, TaskStore]:
    store = TaskStore(root)
    verified = AgentResult(status=AgentStatus.SUCCEEDED, summary="verified")
    return (
        Orchestrator(
            store=store,
            implementer=FakeAgent.succeeding("implemented"),
            verifier=FakeAgent([verified] * 3),
            reviewer=reviewer,
        ),
        store,
    )


@pytest.mark.parametrize(
    ("configured", "expected"), [("60", 60), ("900", 900), ("3600", 3600)]
)
def test_legacy_request_timeout_golden_bounds(tmp_path, monkeypatch, configured, expected) -> None:
    orchestrator, store = _orchestrator(tmp_path, FakeAgent.succeeding("clean"))
    task = store.create_task(_spec())
    monkeypatch.setenv("TRIAGENT_AGENT_TIMEOUT_SECONDS", configured)

    request = orchestrator._request(
        task.id, AgentRole.IMPLEMENTER, "implementation-result-v1", orchestrator.implementer
    )

    assert (request.agent_identity, request.timeout_seconds, request.handoff_file) == (
        "fake", expected, None
    )
    assert request.task_file == store.runs_root / task.id / "task.yaml"
    assert request.workdir == store.runs_root / task.id / "worktree"


@pytest.mark.parametrize("configured", ["59", "3601", "invalid"])
def test_legacy_request_timeout_golden_rejects_invalid_values(tmp_path, monkeypatch, configured) -> None:
    orchestrator, store = _orchestrator(tmp_path, FakeAgent.succeeding("clean"))
    task = store.create_task(_spec())
    monkeypatch.setenv("TRIAGENT_AGENT_TIMEOUT_SECONDS", configured)

    with pytest.raises(ValueError):
        orchestrator._request(task.id, AgentRole.REVIEWER, "review-result-v1", orchestrator.reviewer)


def test_legacy_review_retry_and_provenance_golden_snapshot(tmp_path) -> None:
    reviewer = FakeAgent([
        AgentResult(status=AgentStatus.UNAVAILABLE, summary="transient"),
        AgentResult(status=AgentStatus.SUCCEEDED, summary="clean"),
    ])
    orchestrator, store = _orchestrator(tmp_path, reviewer)
    task = store.create_task(_spec())

    assert orchestrator.run_until_blocked(task.id) is TaskState.APPROVAL
    assert store.runtime(task.id).agent_calls == 4
    assert store.outcomes(task.id)["review"].status == "passed"
    assert store.execution_provenance(task.id) == {
        "mode": "simulation",
        "implementer": "fake",
        "verifier": "fake",
        "reviewer": "fake",
        "profile_digest": "fake-v1",
    }
    assert orchestrator.approve(task.id, "outcome") is TaskState.APPROVAL

    with pytest.raises(ValueError, match="immutable"):
        store.record_execution_provenance(
            task.id,
            mode="simulation",
            implementer="fake",
            verifier="fake",
            reviewer="fake",
            profile_digest="different-profile",
        )


class _OfflineRunner:
    """Records subprocess contracts; it never starts a vendor executable."""

    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.calls: list[tuple[list[str], Path, float, dict[str, str], str | None]] = []

    def run(self, argv, cwd, timeout, env, stdin=None):
        self.calls.append((list(argv), Path(cwd), timeout, dict(env), stdin))
        return self.result


def _adapter_request(tmp_path: Path, role: AgentRole) -> object:
    from triagent.adapters.base import AgentRequest

    task = tmp_path / "task.yaml"
    task.write_text("goal: legacy contract\n", encoding="utf-8")
    handoff = tmp_path / "handoff.json"
    handoff.write_text(json.dumps({"final_diff": "", "tests": []}), encoding="utf-8")
    return AgentRequest(
        role=role,
        task_file=task,
        handoff_file=None if role is AgentRole.IMPLEMENTER else handoff,
        workdir=tmp_path,
        output_schema="implementation-result-v1",
        timeout_seconds=123,
    )


def _cursor_success() -> ProcessResult:
    return ProcessResult(
        0,
        json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "{}"}),
        "",
        False,
    )


def _codex_success() -> ProcessResult:
    canonical = json.dumps({"status": "passed", "evidence": [], "artifacts": []})
    return ProcessResult(0, json.dumps({"type": "agent_message", "message": canonical}), "", False)


def _agy_success() -> ProcessResult:
    return ProcessResult(
        0,
        json.dumps({"status": "passed", "evidence": [], "artifacts": [], "findings": []}),
        "",
        False,
    )


def test_legacy_provider_argv_env_and_secret_boundary_golden(tmp_path, monkeypatch) -> None:
    """Public adapter invocations retain their legacy wire contracts offline."""
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("CODEX_HOME", "/offline/codex-home")
    monkeypatch.setenv("AGY_API_KEY", "agy-secret")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-secret")
    monkeypatch.delenv("SSH_CONNECTION", raising=False)

    cursor_runner = _OfflineRunner(_cursor_success())
    cursor = CursorAdapter(runner=cursor_runner, command=("cursor-offline",))
    assert cursor.run(_adapter_request(tmp_path, AgentRole.IMPLEMENTER)).status is AgentStatus.SUCCEEDED
    argv, cwd, timeout, env, stdin = cursor_runner.calls[0]
    assert argv == ["cursor-offline", "--trust", "--print", "--output-format", "json"]
    assert cwd == tmp_path and timeout == 123 and env == {"CURSOR_API_KEY": "cursor-secret", "WSLENV": "CURSOR_API_KEY/u"}
    assert stdin is not None and "cursor-secret" not in stdin

    codex_runner = _OfflineRunner(_codex_success())
    codex = CodexAdapter(runner=codex_runner, command=("codex-offline",))
    assert codex.run(_adapter_request(tmp_path, AgentRole.VERIFIER)).status is AgentStatus.SUCCEEDED
    argv, cwd, timeout, env, stdin = codex_runner.calls[0]
    assert argv == ["codex-offline", "exec", "--sandbox", "workspace-write", "-C", str(tmp_path), "--json", "-"]
    assert cwd == tmp_path and timeout == 123 and env == {"OPENAI_API_KEY": "openai-secret", "CODEX_HOME": "/offline/codex-home"}
    assert stdin is not None and "openai-secret" not in stdin

    agy_runner = _OfflineRunner(_agy_success())
    agy = AntigravityAdapter(runner=agy_runner, command=("agy-offline",), acl_verifier=lambda *_: True)
    assert agy.run(_adapter_request(tmp_path, AgentRole.REVIEWER)).status is AgentStatus.SUCCEEDED
    argv, cwd, timeout, env, stdin = agy_runner.calls[0]
    assert argv[:2] == ["agy-offline", "-p"] and len(argv) == 3
    assert cwd == tmp_path and timeout == 123 and env == {"AGY_API_KEY": "agy-secret", "GOOGLE_API_KEY": "google-secret"}
    assert stdin is None and all("secret" not in value for value in argv)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (ProcessResult(None, "", "", True), AgentStatus.TIMED_OUT),
        (ProcessResult(1, "", "authentication required", False), AgentStatus.UNAVAILABLE),
        (ProcessResult(1, "", "ordinary failure", False), AgentStatus.FAILED),
        (ProcessResult(0, "not json", "", False), AgentStatus.INVALID_OUTPUT),
    ],
)
def test_legacy_provider_error_mapping_golden(tmp_path, result, expected) -> None:
    """Timeout/auth/process/format failures have stable public classifications."""
    adapters = (
        (CursorAdapter(runner=_OfflineRunner(result), command=("cursor-offline",)), AgentRole.IMPLEMENTER),
        (CodexAdapter(runner=_OfflineRunner(result), command=("codex-offline",)), AgentRole.VERIFIER),
        (AntigravityAdapter(runner=_OfflineRunner(result), command=("agy-offline",), acl_verifier=lambda *_: True), AgentRole.REVIEWER),
    )
    for adapter, role in adapters:
        assert adapter.run(_adapter_request(tmp_path, role)).status is expected


@pytest.mark.parametrize("status, calls", [(AgentStatus.UNAVAILABLE, 4), (AgentStatus.TIMED_OUT, 3), (AgentStatus.FAILED, 3), (AgentStatus.INVALID_OUTPUT, 3)])
def test_legacy_only_unavailable_review_retries_once_golden(tmp_path, status, calls) -> None:
    reviewer = FakeAgent([AgentResult(status=status, summary="offline failure"), AgentResult(status=AgentStatus.SUCCEEDED, summary="must not run")])
    orchestrator, store = _orchestrator(tmp_path, reviewer)
    task = store.create_task(_spec())
    assert orchestrator.run_until_blocked(task.id) is (TaskState.APPROVAL if status is AgentStatus.UNAVAILABLE else TaskState.FAILED_RECOVERABLE)
    assert store.runtime(task.id).agent_calls == calls
    assert len(reviewer.requests) == (2 if status is AgentStatus.UNAVAILABLE else 1)


def test_legacy_resume_provenance_lease_and_approval_golden(tmp_path) -> None:
    """A restart preserves the selected pipeline and one controller owns mutation."""
    failed_review = FakeAgent([AgentResult(status=AgentStatus.FAILED, summary="offline failure")])
    initial, store = _orchestrator(tmp_path, failed_review)
    initial.profile_digest = "legacy-golden"
    task = store.create_task(_spec())
    assert initial.run_until_blocked(task.id) is TaskState.FAILED_RECOVERABLE
    checkpoint = store.recovery_checkpoint(task.id)
    assert checkpoint is not None and checkpoint["stage"] == "review"

    mismatched, _ = _orchestrator(tmp_path, FakeAgent.succeeding("clean"))
    mismatched.profile_digest = "changed-profile"
    with pytest.raises(ValueError, match="provenance mismatch"):
        mismatched.resume_until_blocked(task.id)

    resumed, resumed_store = _orchestrator(tmp_path, FakeAgent.succeeding("clean"))
    resumed.profile_digest = "legacy-golden"
    # Fake legacy runs have no git candidate; this test isolates resume identity.
    resumed_store.restore_candidate_worktree = lambda _task_id: None
    assert resumed.resume_until_blocked(task.id) is TaskState.APPROVAL
    assert store.outstanding_approvals(task.id) == ["outcome"]
    assert resumed.approve(task.id, "outcome") is TaskState.APPROVAL
    assert store.runtime(task.id).approvals == frozenset({"outcome"})

    store.acquire_lease(task.id, "golden-owner", 60)
    with pytest.raises(LeaseConflict):
        store.acquire_lease(task.id, "contender", 60)
    store.release_lease(task.id, "golden-owner")

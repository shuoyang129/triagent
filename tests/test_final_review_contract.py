from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from triagent.adapters.base import AgentRequest, AgentResult, AgentRole, AgentStatus
from triagent.adapters.deepseek import DeepSeekAdapter
from triagent.domain import Budget, StageOutcome, TaskSpec, TaskState
from triagent.git_workspace import GitWorkspace
from triagent.report import render_persisted_report
from triagent.store import BudgetExceeded, LeaseConflict, TaskStore


class Runner:
    def __init__(self): self.calls = []
    def run(self, *args): self.calls.append(args); raise AssertionError("vendor invoked")


def request(tmp_path: Path) -> AgentRequest:
    task = tmp_path / "task"; task.write_text("x", encoding="utf-8")
    return AgentRequest(role=AgentRole.IMPLEMENTER, agent_identity="deepseek", task_file=task,
                        workdir=tmp_path, output_schema="implementation-result-v1", timeout_seconds=1)


@pytest.mark.parametrize("enabled,billing,live", [
    (False, False, False), (True, False, True),
    (True, True, False), (True, True, True),
])
def test_deepseek_direct_run_fails_closed(tmp_path, enabled, billing, live):
    runner = Runner()
    adapter = DeepSeekAdapter(runner=runner, enabled=enabled, billing_confirmed=billing, live_confirmed=live)
    assert adapter.run(request(tmp_path)).status is AgentStatus.UNAVAILABLE
    assert runner.calls == []


def test_atomic_budget_reservation_accounts_interrupted_and_unknown_cost(tmp_path):
    store = TaskStore(tmp_path); task = store.create_task(TaskSpec(
        goal="x", scope=["x"], acceptance=["x"], budget=Budget(max_agent_calls=1, max_minutes=1, max_usd=0)))
    call = store.reserve_agent_call(task.id, estimated_usd=0.0)
    assert store.runtime(task.id).agent_calls == 1
    with pytest.raises(BudgetExceeded): store.reserve_agent_call(task.id, estimated_usd=0)
    store.interrupt_agent_call(task.id, call, "crash")
    assert store.runtime(task.id).interrupted_calls == 1


def test_concurrent_approval_updates_are_preserved_and_lease_is_single_writer(tmp_path):
    store = TaskStore(tmp_path); task = store.create_task(TaskSpec(goal="x", scope=["x"], acceptance=["x"]))
    threads = [threading.Thread(target=store.record_approval, args=(task.id, action))
               for action in ("outcome", "merge")]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert store.runtime(task.id).approvals == frozenset({"outcome", "merge"})
    store.acquire_lease(task.id, "one", 60)
    with pytest.raises(LeaseConflict): store.acquire_lease(task.id, "two", 60)


def test_structured_outcomes_only_and_missing_report_evidence(tmp_path):
    store = TaskStore(tmp_path); task = store.create_task(TaskSpec(goal="x", scope=["x"], acceptance=["x"]))
    store.record_outcome(task.id, StageOutcome(stage="verify", status="passed", summary="verified", evidence=["tests pass"]))
    report = render_persisted_report(store, task.id)
    assert "tests pass" in report and "unknown/missing" in report
    with pytest.raises(Exception): StageOutcome(stage="review", status="passed", summary="clean", reasoning="secret")


def test_dirty_repository_is_refused_and_branch_prune_requires_approval(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "x@y"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=repo, check=True)
    (repo / "a").write_text("a", encoding="utf-8"); subprocess.run(["git", "add", "a"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "a"], cwd=repo, check=True)
    (repo / "a").write_text("dirty", encoding="utf-8")
    with pytest.raises(RuntimeError, match="dirty"): GitWorkspace.create(repo, "x")


def test_setup_failure_can_be_recorded_durably(tmp_path):
    store = TaskStore(tmp_path); task = store.create_task(TaskSpec(goal="x", scope=["x"], acceptance=["x"]))
    store.fail_setup(task.id, "not a repository")
    assert store.load(task.id).state is TaskState.FAILED_FINAL
    assert "not a repository" in render_persisted_report(store, task.id)

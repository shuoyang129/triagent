from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from triagent.adapters.fake import FakeAgent
from triagent.completion import CompletionBinding, CompletionControl, _digest
from triagent.domain import TaskSpec
from triagent.orchestrator import Orchestrator
from triagent.store import LeaseConflict, TaskStore


COMMIT = "a" * 40


def _prepared(tmp_path: Path) -> tuple[TaskStore, Orchestrator, CompletionControl, str]:
    store = TaskStore(tmp_path / "data")
    task = store.create_task(TaskSpec(goal="offline", scope=["."], acceptance=["offline"]))
    call_id = store.reserve_agent_call(task.id, estimated_usd=0.0)
    store.complete_agent_call(task.id, call_id, actual_usd=0.0)
    manifest = {"argv": ["fake", "--offline"], "schema": "implementation-result-v2"}
    binding = CompletionBinding(
        task_id=task.id,
        call_id=call_id,
        provider="fake",
        role="implementer",
        input_digest=_digest(manifest),
        profile_digest="c" * 64,
        runtime_manifest_digest="d" * 64,
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    control = CompletionControl(store.runs_root, binding, worktree)
    control.write_input_manifest(manifest)
    control.write_result(candidate_commit=COMMIT, outcome={"status": "ok"})
    fake = FakeAgent.succeeding()
    orchestrator = Orchestrator(store, fake, fake, fake)
    owner = str(uuid.uuid4())
    store.acquire_lease(task.id, owner, 60)
    orchestrator._lease_owner = owner
    return store, orchestrator, control, task.id


def test_fake_durable_completion_is_ledgered_before_one_time_receipt(tmp_path: Path) -> None:
    store, orchestrator, control, task_id = _prepared(tmp_path)

    record = orchestrator.consume_fake_durable_completion(
        control, expected_candidate_commit=COMMIT
    )

    assert control.receipt_path.exists()
    assert store.durable_completion(task_id, control.binding.call_id) == {
        "provider": "fake",
        "role": "implementer",
        "result_digest": record.result_digest,
        "candidate_commit": COMMIT,
        "outcome": {"status": "ok"},
    }
    assert not orchestrator.implementer.requests


def test_crash_after_store_commit_replays_without_provider_or_duplicate_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, orchestrator, control, task_id = _prepared(tmp_path)
    original = store.record_durable_completion
    calls = 0

    def crash_after_commit(**kwargs: object) -> bool:
        nonlocal calls
        calls += 1
        original(**kwargs)
        raise RuntimeError("crash after database commit")

    monkeypatch.setattr(store, "record_durable_completion", crash_after_commit)
    with pytest.raises(RuntimeError, match="database commit"):
        orchestrator.consume_fake_durable_completion(control)
    assert not control.receipt_path.exists()
    assert store.durable_completion(task_id, control.binding.call_id) is not None

    monkeypatch.setattr(store, "record_durable_completion", original)
    assert orchestrator.consume_fake_durable_completion(control).candidate_commit == COMMIT
    assert control.receipt_path.exists()
    assert calls == 1
    assert not orchestrator.implementer.requests


def test_durable_completion_requires_current_controller_lease(tmp_path: Path) -> None:
    store, orchestrator, control, _ = _prepared(tmp_path)
    orchestrator._lease_owner = None
    with pytest.raises(LeaseConflict):
        orchestrator.consume_fake_durable_completion(control)
    assert not control.receipt_path.exists()


def test_durable_ledger_atomically_completes_an_inflight_reserved_call(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "data")
    task = store.create_task(TaskSpec(goal="offline", scope=["."], acceptance=["offline"]))
    call_id = store.reserve_agent_call(task.id, estimated_usd=0.0)
    owner = str(uuid.uuid4())
    store.acquire_lease(task.id, owner, 60)

    assert store.record_durable_completion(
        task_id=task.id,
        call_id=call_id,
        provider="fake",
        role="verifier",
        result_digest="e" * 64,
        candidate_commit=COMMIT,
        outcome={"status": "ok"},
        lease_owner=owner,
        actual_usd=0.0,
    )
    assert store.runtime(task.id).completed_calls == 1
    with store._connect() as connection:
        row = connection.execute("SELECT status FROM agent_calls WHERE id=?", (call_id,)).fetchone()
    assert row["status"] == "completed"

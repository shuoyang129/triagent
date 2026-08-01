from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path

import pytest

from triagent.completion import (
    CompletionAlreadyConsumed,
    CompletionBinding,
    CompletionControl,
    CompletionError,
    _digest,
    find_recoverable_result,
)


COMMIT = "a" * 40


def _control(tmp_path: Path) -> tuple[CompletionControl, dict[str, object]]:
    runs = tmp_path / "runs"; runs.mkdir(mode=0o700, parents=True)
    worktree = tmp_path / "worktree"; worktree.mkdir()
    manifest = {"argv": ["fake", "--offline"], "prompt_digest": "b" * 64}
    binding = CompletionBinding(
        task_id=str(uuid.uuid4()), call_id=str(uuid.uuid4()), provider="fake", role="implementer",
        input_digest=_digest(manifest), profile_digest="c" * 64, runtime_manifest_digest="d" * 64,
    )
    return CompletionControl(runs, binding, worktree), manifest


def test_private_control_record_is_bound_atomic_and_replayable(tmp_path: Path) -> None:
    control, manifest = _control(tmp_path)
    assert control.write_input_manifest(manifest) == control.binding.input_digest
    control.append_event({"event": "started"})
    control.heartbeat({"meaningful_progress": True})
    record = control.write_result(candidate_commit=COMMIT, outcome={"status": "ok", "message": "offline"})

    assert control.read_result(expected_candidate_commit=COMMIT) == record
    assert not (control.control_root / "result.json.tmp").exists()
    assert json.loads(control.events_path.read_text())["event"] == "started"
    if os.name != "nt":
        for path in (control.control_root, control.input_manifest_path, control.events_path, control.heartbeat_path, control.result_path):
            expected = 0o700 if path.is_dir() else 0o600
            assert stat.S_IMODE(path.stat().st_mode) == expected


def test_result_is_consumed_once_and_crash_before_receipt_replays_without_provider(tmp_path: Path) -> None:
    control, manifest = _control(tmp_path)
    control.write_input_manifest(manifest)
    durable = control.write_result(candidate_commit=COMMIT, outcome={"status": "ok"})
    calls: list[str] = []

    def crash() -> None:
        calls.append("controller")
        raise RuntimeError("injected crash before receipt")

    with pytest.raises(RuntimeError, match="injected"):
        control.consume_once(before_receipt=crash)
    assert not control.receipt_path.exists()
    assert control.consume_once() == durable
    assert calls == ["controller"]
    with pytest.raises(CompletionAlreadyConsumed):
        control.consume_once()


def test_forged_corrupt_mismatched_and_changed_candidate_results_are_rejected(tmp_path: Path) -> None:
    control, manifest = _control(tmp_path)
    control.write_input_manifest(manifest)
    control.write_result(candidate_commit=COMMIT, outcome={"status": "ok"})

    with pytest.raises(CompletionError, match="candidate changed"):
        control.read_result(expected_candidate_commit="b" * 40)
    forged = json.loads(control.result_path.read_text())
    forged["provider"] = "attacker"
    control.result_path.write_text(json.dumps(forged))
    with pytest.raises(CompletionError, match="binding mismatch"):
        control.read_result()

    control, manifest = _control(tmp_path / "corrupt")
    control.write_input_manifest(manifest)
    control.result_path.write_text("not-json")
    with pytest.raises(CompletionError, match="invalid completion JSON"):
        control.read_result()


def test_rejects_worktree_overlap_and_links(tmp_path: Path) -> None:
    runs = tmp_path / "runs"; runs.mkdir()
    worktree = tmp_path / "worktree"; worktree.mkdir()
    manifest: dict[str, object] = {"call": "x"}
    binding = CompletionBinding(str(uuid.uuid4()), str(uuid.uuid4()), "fake", "reviewer", _digest(manifest), "c" * 64, "d" * 64)
    with pytest.raises(CompletionError, match="overlap"):
        CompletionControl(runs, binding, runs)
    if hasattr(os, "symlink"):
        linked = tmp_path / "linked-runs"; linked.symlink_to(runs, target_is_directory=True)
        with pytest.raises(CompletionError, match="links"):
            CompletionControl(linked, binding, worktree)


def test_input_manifest_and_result_are_immutable_for_a_binding(tmp_path: Path) -> None:
    control, manifest = _control(tmp_path)
    control.write_input_manifest(manifest)
    with pytest.raises(CompletionError, match="conflicts"):
        control.write_input_manifest({"different": True})
    control.write_result(candidate_commit=COMMIT, outcome={"status": "ok"}, written_at="2026-08-01T00:00:00+00:00")
    with pytest.raises(CompletionError, match="different content"):
        control.write_result(candidate_commit=COMMIT, outcome={"status": "changed"}, written_at="2026-08-01T00:00:00+00:00")


def test_recovery_discovers_only_an_exact_bound_durable_result(tmp_path: Path) -> None:
    control, manifest = _control(tmp_path)
    control.write_input_manifest(manifest)
    control.write_result(candidate_commit=COMMIT, outcome={"status": "ok"})

    found = find_recoverable_result(
        control.runs_root,
        task_id=control.binding.task_id,
        provider="fake",
        role="implementer",
        profile_digest="c" * 64,
        runtime_manifest_digest="d" * 64,
        candidate_commit=COMMIT,
        provider_worktree=control.provider_worktree,
    )

    assert found is not None
    assert found[0].binding == control.binding
    assert found[1].result_digest == control.read_result().result_digest
    assert find_recoverable_result(
        control.runs_root,
        task_id=control.binding.task_id,
        provider="fake",
        role="implementer",
        profile_digest="f" * 64,
        runtime_manifest_digest="d" * 64,
        candidate_commit=COMMIT,
        provider_worktree=control.provider_worktree,
    ) is None

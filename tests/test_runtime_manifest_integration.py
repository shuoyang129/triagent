from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import triagent.cli as cli
from triagent.cli import app
from triagent.domain import StageOutcome, TaskSpec, TaskState
from triagent.store import TaskStore


runner = CliRunner()


def _recoverable_fake_task(root: Path) -> tuple[TaskStore, str]:
    store = TaskStore(root)
    task = store.create_task(TaskSpec(goal="x", scope=[str(root)], acceptance=["x"]))
    store.record_execution_provenance(
        task.id, mode="simulation", implementer="fake", verifier="fake",
        reviewer="fake", profile_digest="fake-v1",
    )
    store.record_outcome(task.id, StageOutcome(stage="implement", status="failed", summary="requires-repair"))
    store.transition_recoverable(task.id, TaskState.SPEC, "implement", "test")
    return store, task.id


def test_store_runtime_manifest_is_immutable_and_idempotent(tmp_path: Path) -> None:
    store, task_id = _recoverable_fake_task(tmp_path / "data")
    manifest = cli._fake_runtime_manifest()
    first = store.record_runtime_manifest(task_id, manifest)

    assert store.record_runtime_manifest(task_id, manifest) == first
    changed = dict(manifest)
    changed["schema_version"] = 99
    try:
        store.record_runtime_manifest(task_id, changed)
    except ValueError as error:
        assert str(error) == "runtime manifest is immutable"
    else:
        raise AssertionError("changed manifest was accepted")


def test_fake_resume_rejects_runtime_drift_before_constructing_adapter(tmp_path: Path, monkeypatch) -> None:
    store, task_id = _recoverable_fake_task(tmp_path / "data")
    store.record_runtime_manifest(task_id, cli._fake_runtime_manifest())
    monkeypatch.setenv("TRIAGENT_AGENT_TIMEOUT_SECONDS", "901")
    monkeypatch.setattr(cli, "FakeAgent", lambda *args: (_ for _ in ()).throw(AssertionError("adapter constructed")))

    result = runner.invoke(app, ["resume", "--profile", "fake", "--data-root", str(tmp_path / "data"), task_id])

    assert result.exit_code != 0
    assert "task resume refused" in result.output
    assert store.load(task_id).state is TaskState.FAILED_RECOVERABLE


def test_doctor_compare_reports_only_paths_not_manifest_values(tmp_path: Path, monkeypatch) -> None:
    store, task_id = _recoverable_fake_task(tmp_path / "data")
    store.record_runtime_manifest(task_id, cli._fake_runtime_manifest())
    monkeypatch.setenv("TRIAGENT_AGENT_TIMEOUT_SECONDS", "901")

    result = runner.invoke(app, ["doctor", "--profile", "fake", "--data-root", str(tmp_path / "data"), "--compare", task_id])

    assert result.exit_code == 2
    assert "timeout_policy.digest" in result.output
    assert "timeout_policy.seconds" in result.output
    assert "900" not in result.output and "901" not in result.output

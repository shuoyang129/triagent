import json

import pytest

from triagent.domain import TaskSpec, TaskState
from triagent.store import StateConflict, TaskStore


@pytest.fixture
def spec() -> TaskSpec:
    return TaskSpec(goal="Add health endpoint", scope=["src/"], acceptance=["tests pass"])


def test_transition_rejects_stale_expected_state(tmp_path, spec: TaskSpec) -> None:
    store = TaskStore(tmp_path)
    task = store.create_task(spec)
    store.transition(task.id, TaskState.SPEC, TaskState.IMPLEMENT, "start")
    with pytest.raises(StateConflict):
        store.transition(task.id, TaskState.SPEC, TaskState.VERIFY, "stale")


def test_task_survives_store_reopen(tmp_path, spec: TaskSpec) -> None:
    task = TaskStore(tmp_path).create_task(spec)
    reopened = TaskStore(tmp_path).load(task.id)
    assert reopened.spec == spec
    assert reopened.state is TaskState.SPEC


def test_run_layout_contains_audit_files(tmp_path, spec: TaskSpec) -> None:
    store = TaskStore(tmp_path)
    task = store.create_task(spec)
    run_dir = tmp_path / "runs" / task.id
    assert (run_dir / "task.yaml").is_file()
    assert (run_dir / "state.json").is_file()
    assert (run_dir / "events.jsonl").is_file()
    assert (run_dir / "logs").is_dir()
    assert (run_dir / "artifacts").is_dir()
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["state"] == "SPEC"

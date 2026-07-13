from __future__ import annotations

import json
import subprocess
from pathlib import Path

from triagent.adapters.antigravity import AntigravityAdapter
from triagent.adapters.codex import CodexAdapter
from triagent.adapters.cursor import CursorAdapter
from triagent.adapters.process import ProcessResult
from triagent.domain import Budget, TaskSpec, TaskState
from triagent.git_workspace import GitWorkspace
from triagent.orchestrator import Orchestrator
from triagent.report import render_persisted_report
from triagent.store import TaskStore


VENDOR_MARKER = "VENDOR_FREE_TEXT_MUST_NOT_PERSIST"


class CursorRunner:
    def __init__(self, edit: bool) -> None:
        self.edit = edit

    def run(self, argv, cwd: Path, timeout, env_allowlist, stdin=None) -> ProcessResult:
        if self.edit:
            (cwd / "actual.txt").write_bytes(b"actual\n")
        vendor_result = json.dumps({
            "changed_paths": ["claimed.txt"],
            "note": VENDOR_MARKER,
        })
        envelope = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": vendor_result,
        }
        return ProcessResult(0, json.dumps(envelope), "", False)


class StageRunner:
    def __init__(self, role: str) -> None:
        self.role = role
        self.inputs: list[str | None] = []

    def run(self, argv, cwd, timeout, env_allowlist, stdin=None) -> ProcessResult:
        self.inputs.append(stdin)
        payload = {"status": "passed", "evidence": ["tests pass"], "artifacts": []}
        if self.role == "review":
            payload["findings"] = []
            return ProcessResult(0, json.dumps(payload), "", False)
        event = {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps(payload)},
        }
        return ProcessResult(0, json.dumps(event), "", False)


def setup_task(tmp_path: Path, *, edit: bool):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "x@y"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)

    store = TaskStore(tmp_path / "data")
    task = store.create_task(TaskSpec(
        goal="add actual.txt",
        scope=[str(repo)],
        acceptance=["verified"],
        budget=Budget(max_agent_calls=5, max_minutes=5, max_usd=5),
    ))
    work = store.runs_root / task.id / "worktree"
    work.rmdir()
    workspace = GitWorkspace.create(repo, task.id, destination=work)
    store.set_workspace(task.id, str(repo), workspace.base_commit, f"triagent/{task.id}")

    verifier = StageRunner("verify")
    reviewer = StageRunner("review")
    cursor = CursorAdapter(
        runner=CursorRunner(edit),
        command=["cursor-agent"],
        estimated_usd=0.5,
    )
    orchestrator = Orchestrator(
        store,
        cursor,
        CodexAdapter(runner=verifier, estimated_usd=0.5),
        AntigravityAdapter(
            runner=reviewer, estimated_usd=0.5, acl_verifier=lambda directory, file: True
        ),
    )
    return orchestrator, store, task, work, verifier, reviewer


def test_cursor_free_text_advances_on_git_derived_change(tmp_path: Path) -> None:
    orchestrator, store, task, work, verifier, reviewer = setup_task(tmp_path, edit=True)

    state = orchestrator.run_until_blocked(task.id)

    assert state is TaskState.APPROVAL
    meta = store.workspace(task.id)
    assert subprocess.run(
        ["git", "show", f"{meta['reviewed_commit']}:actual.txt"],
        cwd=work,
        check=True,
        capture_output=True,
    ).stdout == b"actual\n"
    assert subprocess.run(
        ["git", "cat-file", "-e", f"{meta['reviewed_commit']}:claimed.txt"],
        cwd=work,
        capture_output=True,
    ).returncode != 0
    run_dir = store.runs_root / task.id
    with store._connect() as connection:
        sqlite_text = repr(connection.execute(
            "SELECT status, diagnostic FROM agent_calls WHERE task_id = ?",
            (task.id,),
        ).fetchall()) + repr(connection.execute(
            "SELECT outcome_json FROM stage_outcomes WHERE task_id = ?",
            (task.id,),
        ).fetchall())
    persisted_text = "\n".join([
        sqlite_text,
        (run_dir / "events.jsonl").read_text(encoding="utf-8"),
        (run_dir / "handoff.json").read_text(encoding="utf-8"),
        render_persisted_report(store, task.id),
    ])
    assert VENDOR_MARKER not in persisted_text
    assert len(verifier.inputs) == 1
    assert len(reviewer.inputs) == 1


def test_cursor_no_change_stops_before_verification(tmp_path: Path) -> None:
    orchestrator, store, task, _work, verifier, reviewer = setup_task(tmp_path, edit=False)

    state = orchestrator.run_until_blocked(task.id)

    assert state is TaskState.FAILED_RECOVERABLE
    outcome = store.outcomes(task.id)["implement"]
    assert outcome.status == "failed"
    assert outcome.diagnostic == "cursor-no-worktree-change"
    assert verifier.inputs == []
    assert reviewer.inputs == []

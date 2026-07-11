from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from triagent.git_workspace import GitWorkspace, TestResults


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )
    return result.stdout.strip()


@pytest.fixture
def temp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "tests@example.invalid")
    git(repo, "config", "user.name", "TriAgent Tests")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "base")
    return repo


def test_task_workspace_does_not_modify_main_worktree(temp_repo: Path) -> None:
    ws = GitWorkspace.create(temp_repo, "task-1")

    (ws.path / "new.txt").write_text("x", encoding="utf-8")

    assert not (temp_repo / "new.txt").exists()
    assert git(ws.path, "branch", "--show-current") == "triagent/task-1"
    assert git(temp_repo, "branch", "--show-current") == "main"


@pytest.mark.parametrize(
    "task_id", ["../escape", "task/name", "task name", ";rm", "", ".", ".."]
)
def test_create_rejects_unsafe_task_ids(temp_repo: Path, task_id: str) -> None:
    with pytest.raises(ValueError, match="task_id"):
        GitWorkspace.create(temp_repo, task_id)


def test_diff_and_handoff_describe_workspace(temp_repo: Path) -> None:
    ws = GitWorkspace.create(temp_repo, "task-2")
    (ws.path / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (ws.path / "new.txt").write_text("new\n", encoding="utf-8")
    git(ws.path, "add", ".")
    git(ws.path, "commit", "-m", "task changes")

    handoff = ws.handoff(
        completed=["implementation"],
        remaining=["review"],
        tests=TestResults(passed=["unit"], failed=[]),
        known_issues=["none"],
    )

    assert "-base" in ws.diff()
    assert "+changed" in ws.diff()
    assert handoff.base_commit != handoff.current_commit
    assert handoff.current_commit == git(ws.path, "rev-parse", "HEAD")
    assert handoff.changed_files == ("new.txt", "tracked.txt")
    assert handoff.completed == ("implementation",)
    assert handoff.remaining == ("review",)
    assert handoff.tests.passed == ("unit",)
    assert handoff.known_issues == ("none",)


def test_failed_create_leaves_existing_worktree_for_explicit_cleanup(
    temp_repo: Path,
) -> None:
    ws = GitWorkspace.create(temp_repo, "task-3")

    with pytest.raises(RuntimeError):
        GitWorkspace.create(temp_repo, "task-3")

    assert ws.path.exists()


def test_cleanup_explicitly_removes_worktree(temp_repo: Path) -> None:
    ws = GitWorkspace.create(temp_repo, "task-4")

    ws.cleanup()

    assert not ws.path.exists()

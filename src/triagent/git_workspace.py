from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class TestResults:
    __test__ = False

    passed: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "passed", tuple(self.passed))
        object.__setattr__(self, "failed", tuple(self.failed))


@dataclass(frozen=True)
class Handoff:
    base_commit: str
    current_commit: str
    changed_files: tuple[str, ...]
    completed: tuple[str, ...]
    remaining: tuple[str, ...]
    tests: TestResults
    known_issues: tuple[str, ...]

    @property
    def test_results(self) -> TestResults:
        """Compatibility alias for callers that use the descriptive name."""
        return self.tests


@dataclass(frozen=True)
class GitWorkspace:
    path: Path
    repo: Path
    task_id: str
    base_commit: str

    @classmethod
    def create(cls, repo: Path, task_id: str, destination: Path | None = None) -> GitWorkspace:
        if not _TASK_ID.fullmatch(task_id) or task_id in {".", ".."}:
            raise ValueError(f"invalid task_id: {task_id!r}")

        repo = Path(repo).resolve()
        base_commit = _git(repo, "rev-parse", "HEAD")
        if destination is None:
            root = repo.parent / ".worktrees" / repo.name
            path = root / task_id
            root.mkdir(parents=True, exist_ok=True)
        else:
            path = Path(destination).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
        _git(
            repo,
            "worktree",
            "add",
            "-b",
            f"triagent/{task_id}",
            str(path),
            base_commit,
        )
        return cls(path=path, repo=repo, task_id=task_id, base_commit=base_commit)

    def diff(self) -> str:
        return _git(self.path, "diff", "--binary", self.base_commit)

    def handoff(
        self,
        *,
        completed: Iterable[str] = (),
        remaining: Iterable[str] = (),
        tests: TestResults | None = None,
        known_issues: Iterable[str] = (),
    ) -> Handoff:
        changed = set(
            filter(
                None,
                _git(self.path, "diff", "--name-only", self.base_commit).splitlines(),
            )
        )
        changed.update(
            filter(
                None,
                _git(
                    self.path, "ls-files", "--others", "--exclude-standard"
                ).splitlines(),
            )
        )
        return Handoff(
            base_commit=self.base_commit,
            current_commit=_git(self.path, "rev-parse", "HEAD"),
            changed_files=tuple(sorted(changed)),
            completed=tuple(completed),
            remaining=tuple(remaining),
            tests=tests or TestResults(),
            known_issues=tuple(known_issues),
        )

    def cleanup(self) -> None:
        _git(self.repo, "worktree", "remove", str(self.path))


def _git(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            shell=False,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", None) or str(error)
        raise RuntimeError(f"Git command failed: {detail.strip()}") from error
    return result.stdout.strip()

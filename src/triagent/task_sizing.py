"""Deterministic, fail-conservative task sizing for v2 timeout selection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from triagent.domain import RiskLevel, TaskSpec
from triagent.timeout_policy import TaskSize


@dataclass(frozen=True)
class TaskSizingEvidence:
    files: int
    bytes: int
    languages: int
    build_markers: int
    acceptance: int
    size: TaskSize

    def persistable(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "files": self.files,
            "bytes": self.bytes,
            "languages": self.languages,
            "build_markers": self.build_markers,
            "acceptance": self.acceptance,
            "size": self.size.value,
        }


_SKIP = {".git", ".triagent", "node_modules", "__pycache__", ".pytest_cache"}
_BUILD = {"pyproject.toml", "package.json", "CMakeLists.txt", "Cargo.toml", "go.mod", "Makefile", "BUILD", "WORKSPACE"}


def classify_task_size(spec: TaskSpec, root: Path) -> TaskSizingEvidence:
    """Classify scope deterministically; any incomplete inspection becomes large."""
    try:
        resolved = root.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("scope root is not a directory")
        files = 0
        total = 0
        languages: set[str] = set()
        build_markers = 0
        for path in sorted(resolved.rglob("*")):
            relative = path.relative_to(resolved)
            if any(part in _SKIP for part in relative.parts):
                continue
            if path.is_symlink():
                raise ValueError("scope contains a symlink")
            if not path.is_file():
                continue
            files += 1
            total += path.stat().st_size
            if total > 50_000_000 or files > 10_000:
                raise ValueError("scope exceeds bounded sizing scan")
            suffix = path.suffix.lower()
            if suffix:
                languages.add(suffix)
            if path.name in _BUILD:
                build_markers += 1
    except (OSError, ValueError):
        return TaskSizingEvidence(0, 0, 0, 0, len(spec.acceptance), TaskSize.LARGE)
    acceptance = len(spec.acceptance)
    if files <= 12 and total <= 100_000 and languages <= {".md", ".txt", ".rst"} and acceptance <= 2:
        selected = TaskSize.TINY
    elif files <= 80 and total <= 1_000_000 and len(languages) <= 5 and build_markers <= 1 and acceptance <= 4:
        selected = TaskSize.SMALL
    elif files <= 500 and total <= 10_000_000 and acceptance <= 8:
        selected = TaskSize.MEDIUM
    else:
        selected = TaskSize.LARGE
    if spec.risk in {RiskLevel.HIGH, RiskLevel.ROBOT_SAFETY} and selected in {TaskSize.TINY, TaskSize.SMALL}:
        selected = TaskSize.MEDIUM
    return TaskSizingEvidence(files, total, len(languages), build_markers, acceptance, selected)

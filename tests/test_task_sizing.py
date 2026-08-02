from pathlib import Path

from triagent.domain import RiskLevel, TaskSpec
from triagent.task_sizing import classify_task_size
from triagent.timeout_policy import TaskSize


def _spec(root: Path, *, risk: RiskLevel = RiskLevel.LOW, acceptance: list[str] | None = None) -> TaskSpec:
    return TaskSpec(goal="size", scope=[str(root)], acceptance=acceptance or ["test"] , risk=risk)


def test_docs_only_tiny_scope_is_classified_tiny(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x", encoding="utf-8")

    result = classify_task_size(_spec(tmp_path), tmp_path)

    assert result.size is TaskSize.TINY
    assert result.persistable()["files"] == 1


def test_robot_safety_never_selects_tiny_or_small(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x", encoding="utf-8")

    result = classify_task_size(_spec(tmp_path, risk=RiskLevel.ROBOT_SAFETY), tmp_path)

    assert result.size is TaskSize.MEDIUM


def test_symlink_or_unreadable_scope_fails_conservatively_large(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "linked.txt").symlink_to(outside)

    result = classify_task_size(_spec(tmp_path), tmp_path)

    assert result.size is TaskSize.LARGE


def test_large_file_count_is_classified_medium_or_larger(tmp_path: Path) -> None:
    for number in range(81):
        (tmp_path / f"file-{number}.py").write_text("pass\n", encoding="utf-8")

    result = classify_task_size(_spec(tmp_path), tmp_path)

    assert result.size in {TaskSize.MEDIUM, TaskSize.LARGE}

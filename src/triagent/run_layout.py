from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from triagent.domain import TaskSpec, TaskState


@dataclass(frozen=True)
class RunLayout:
    root: Path

    @classmethod
    def create(cls, runs_root: Path, task_id: str, spec: TaskSpec) -> "RunLayout":
        root = runs_root / task_id
        root.mkdir(parents=True, exist_ok=False)
        for directory in ("logs", "artifacts", "worktree"):
            (root / directory).mkdir()
        layout = cls(root)
        layout.task_file.write_text(
            json.dumps(spec.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        layout.events_file.touch()
        layout.write_state(TaskState.SPEC)
        return layout

    @property
    def task_file(self) -> Path:
        return self.root / "task.yaml"

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    @property
    def events_file(self) -> Path:
        return self.root / "events.jsonl"

    def write_state(self, state: TaskState) -> None:
        temporary = self.state_file.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({"state": state.value}, indent=2), encoding="utf-8")
        os.replace(temporary, self.state_file)

    def append_event(self, event: dict[str, str]) -> None:
        with self.events_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

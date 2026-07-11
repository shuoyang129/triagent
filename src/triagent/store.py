from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from triagent.domain import TaskSpec, TaskState
from triagent.run_layout import RunLayout


class StateConflict(RuntimeError):
    """Raised when a caller attempts a stale state transition."""


@dataclass(frozen=True)
class StoredTask:
    id: str
    spec: TaskSpec
    state: TaskState


class TaskStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.runs_root = self.root / "runs"
        self.runs_root.mkdir(exist_ok=True)
        self.database = self.root / "triagent.sqlite3"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    spec_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def create_task(self, spec: TaskSpec) -> StoredTask:
        task_id = str(uuid.uuid4())
        layout = RunLayout.create(self.runs_root, task_id, spec)
        timestamp = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO tasks(id, spec_json, state, updated_at) VALUES (?, ?, ?, ?)",
                (task_id, spec.model_dump_json(), TaskState.SPEC.value, timestamp),
            )
        layout.append_event({"at": timestamp, "event": "created", "state": TaskState.SPEC.value})
        return StoredTask(task_id, spec, TaskState.SPEC)

    def load(self, task_id: str) -> StoredTask:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return StoredTask(row["id"], TaskSpec.model_validate_json(row["spec_json"]), TaskState(row["state"]))

    def transition(
        self,
        task_id: str,
        expected: TaskState,
        target: TaskState,
        event: str,
    ) -> StoredTask:
        timestamp = datetime.now(UTC).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if TaskState(row["state"]) is not expected:
                raise StateConflict(f"expected {expected.value}, found {row['state']}")
            connection.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
                (target.value, timestamp, task_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        layout = RunLayout(self.runs_root / task_id)
        layout.write_state(target)
        layout.append_event({"at": timestamp, "event": event, "state": target.value})
        return self.load(task_id)

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from triagent.domain import StageOutcome, TaskSpec, TaskState
from triagent.run_layout import RunLayout


class StateConflict(RuntimeError):
    """Raised when a caller attempts a stale state transition."""

class BudgetExceeded(RuntimeError): pass
class LeaseConflict(RuntimeError): pass


@dataclass(frozen=True)
class StoredTask:
    id: str
    spec: TaskSpec
    state: TaskState


@dataclass(frozen=True)
class TaskRuntime:
    agent_calls: int
    repair_attempts: int
    approvals: frozenset[str]
    completed_calls: int = 0
    interrupted_calls: int = 0
    usd_spent: float = 0.0
    started_at: str = ""


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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_runtime (
                    task_id TEXT PRIMARY KEY,
                    agent_calls INTEGER NOT NULL DEFAULT 0,
                    repair_attempts INTEGER NOT NULL DEFAULT 0,
                    approvals_json TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            for sql in (
                "ALTER TABLE task_runtime ADD COLUMN completed_calls INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE task_runtime ADD COLUMN interrupted_calls INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE task_runtime ADD COLUMN usd_spent REAL NOT NULL DEFAULT 0",
                "ALTER TABLE task_runtime ADD COLUMN started_at TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE task_runtime ADD COLUMN lease_owner TEXT",
                "ALTER TABLE task_runtime ADD COLUMN lease_until REAL",
            ):
                try: connection.execute(sql)
                except sqlite3.OperationalError: pass
            connection.execute("CREATE TABLE IF NOT EXISTS agent_calls(id TEXT PRIMARY KEY, task_id TEXT NOT NULL, status TEXT NOT NULL, estimated_usd REAL, actual_usd REAL, diagnostic TEXT)")
            connection.execute("CREATE TABLE IF NOT EXISTS stage_outcomes(task_id TEXT NOT NULL, stage TEXT NOT NULL, outcome_json TEXT NOT NULL, PRIMARY KEY(task_id, stage))")

    def create_task(self, spec: TaskSpec) -> StoredTask:
        task_id = str(uuid.uuid4())
        layout = RunLayout.create(self.runs_root, task_id, spec)
        timestamp = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO tasks(id, spec_json, state, updated_at) VALUES (?, ?, ?, ?)",
                (task_id, spec.model_dump_json(), TaskState.SPEC.value, timestamp),
            )
            connection.execute("INSERT INTO task_runtime(task_id) VALUES (?)", (task_id,))
        layout.append_event({"at": timestamp, "event": "created", "state": TaskState.SPEC.value})
        return StoredTask(task_id, spec, TaskState.SPEC)

    def load(self, task_id: str) -> StoredTask:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return StoredTask(row["id"], TaskSpec.model_validate_json(row["spec_json"]), TaskState(row["state"]))

    def runtime(self, task_id: str) -> TaskRuntime:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_runtime WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        approvals = frozenset(json.loads(row["approvals_json"]))
        return TaskRuntime(row["agent_calls"], row["repair_attempts"], approvals, row["completed_calls"], row["interrupted_calls"], row["usd_spent"], row["started_at"])

    def reserve_agent_call(self, task_id: str, estimated_usd: float | None = None) -> str:
        connection = self._connect(); call_id = str(uuid.uuid4())
        try:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute("SELECT spec_json FROM tasks WHERE id=?", (task_id,)).fetchone()
            runtime = connection.execute("SELECT * FROM task_runtime WHERE task_id=?", (task_id,)).fetchone()
            if task is None or runtime is None: raise KeyError(task_id)
            spec = TaskSpec.model_validate_json(task["spec_json"]); now = datetime.now(UTC)
            started = datetime.fromisoformat(runtime["started_at"]) if runtime["started_at"] else now
            cost = estimated_usd
            if runtime["agent_calls"] >= spec.budget.max_agent_calls or (now-started).total_seconds() >= spec.budget.max_minutes*60:
                raise BudgetExceeded("call or elapsed-time budget exhausted")
            if cost is None and spec.budget.max_usd > runtime["usd_spent"]: raise BudgetExceeded("unknown call cost cannot be reserved conservatively")
            cost = cost or 0.0
            if runtime["usd_spent"] + cost > spec.budget.max_usd: raise BudgetExceeded("USD budget exhausted")
            connection.execute("UPDATE task_runtime SET agent_calls=agent_calls+1, started_at=CASE WHEN started_at='' THEN ? ELSE started_at END WHERE task_id=?", (now.isoformat(), task_id))
            connection.execute("INSERT INTO agent_calls VALUES(?,?,?,?,?,?)", (call_id, task_id, "started", cost, None, None)); connection.commit()
            return call_id
        except Exception: connection.rollback(); raise
        finally: connection.close()

    def _finish_call(self, task_id: str, call_id: str, status: str, actual_usd: float, diagnostic: str = "") -> None:
        column = "completed_calls" if status == "completed" else "interrupted_calls"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute("UPDATE agent_calls SET status=?,actual_usd=?,diagnostic=? WHERE id=? AND task_id=? AND status='started'", (status, actual_usd, diagnostic, call_id, task_id))
            if cursor.rowcount != 1: raise StateConflict("call is not pending")
            connection.execute(f"UPDATE task_runtime SET {column}={column}+1, usd_spent=usd_spent+? WHERE task_id=?", (actual_usd, task_id))
    def complete_agent_call(self, task_id: str, call_id: str, actual_usd: float = 0.0): self._finish_call(task_id, call_id, "completed", actual_usd)
    def interrupt_agent_call(self, task_id: str, call_id: str, diagnostic: str): self._finish_call(task_id, call_id, "interrupted", 0.0, diagnostic)

    def acquire_lease(self, task_id: str, owner: str, seconds: float) -> None:
        import time
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute("UPDATE task_runtime SET lease_owner=?,lease_until=? WHERE task_id=? AND (lease_owner IS NULL OR lease_until<? OR lease_owner=?)", (owner, time.time()+seconds, task_id, time.time(), owner))
            if cursor.rowcount != 1: raise LeaseConflict("another controller owns the task")

    def release_lease(self, task_id: str, owner: str) -> None:
        with self._connect() as connection: connection.execute("UPDATE task_runtime SET lease_owner=NULL,lease_until=NULL WHERE task_id=? AND lease_owner=?", (task_id, owner))

    def record_outcome(self, task_id: str, outcome: StageOutcome) -> None:
        with self._connect() as connection: connection.execute("INSERT OR REPLACE INTO stage_outcomes VALUES(?,?,?)", (task_id, outcome.stage, outcome.model_dump_json()))
    def outcomes(self, task_id: str) -> dict[str, StageOutcome]:
        with self._connect() as connection: rows=connection.execute("SELECT * FROM stage_outcomes WHERE task_id=?", (task_id,)).fetchall()
        return {r["stage"]: StageOutcome.model_validate_json(r["outcome_json"]) for r in rows}
    def fail_setup(self, task_id: str, diagnostic: str) -> None:
        self.record_outcome(task_id, StageOutcome(stage="setup", status="failed", summary=diagnostic))
        self.transition(task_id, TaskState.SPEC, TaskState.FAILED_FINAL, "setup-failed")

    def increment_agent_calls(self, task_id: str) -> TaskRuntime:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE task_runtime SET agent_calls = agent_calls + 1 WHERE task_id = ?",
                (task_id,),
            )
            if cursor.rowcount != 1:
                raise KeyError(task_id)
        return self.runtime(task_id)

    def increment_repair_attempts(self, task_id: str) -> TaskRuntime:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE task_runtime SET repair_attempts = repair_attempts + 1 WHERE task_id = ?",
                (task_id,),
            )
            if cursor.rowcount != 1:
                raise KeyError(task_id)
        return self.runtime(task_id)

    def record_approval(self, task_id: str, approval: str) -> TaskRuntime:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row=connection.execute("SELECT approvals_json FROM task_runtime WHERE task_id=?", (task_id,)).fetchone()
            if row is None: raise KeyError(task_id)
            approvals=sorted(set(json.loads(row[0])) | {approval})
            connection.execute("UPDATE task_runtime SET approvals_json=? WHERE task_id=?", (json.dumps(approvals), task_id))
        return self.runtime(task_id)

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

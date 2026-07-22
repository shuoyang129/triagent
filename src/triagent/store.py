from __future__ import annotations

import json
import sqlite3
import uuid
import os
import re
import hashlib
import subprocess
import stat
import struct
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from pathlib import PurePosixPath

from triagent.domain import StageOutcome, TaskSpec, TaskState
from triagent.run_layout import RunLayout
from triagent.git_runner import run_git


class StateConflict(RuntimeError):
    """Raised when a caller attempts a stale state transition."""

class BudgetExceeded(RuntimeError): pass
class LeaseConflict(RuntimeError): pass

_CREDENTIAL_PATH=re.compile(r"(?i)(?:^|/)(?:\.env(?:\..*)?|\.npmrc|\.pypirc|\.yarnrc(?:\.yml)?|id_[^/]+|auth[^/]*|credentials?[^/]*|secrets?[^/]*|\.docker/config\.json|\.config/containers/auth\.json|\.aws/credentials|\.config/gcloud/.*|\.azure/.*|\.cargo/credentials(?:\.toml)?)$|(?:^|[._-])(?:credentials?|secrets?|passwords?|tokens?|api[_-]?keys?|auth)(?:[._-]|$)|\.(?:pem|key)$")
_CREDENTIAL_CONTENT=re.compile(r"(?is)(?:api[_-]?key|access[_-]?key|client[_-]?secret|auths?|auth[_-]?token|basic[_-]?auth|registry[_-]?auth|token|password|secret|authorization|credential|_authToken)\s*['\"]?\s*[:=]\s*['\"]?(?:basic\s+|bearer\s+)?[^\s,'\"}]{12,}|-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|\b(?:basic|bearer)\s+[A-Za-z0-9._~+/=-]{12,}|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_STRUCTURED_AUTH_FIELD=re.compile(r"(?im)(?:^|[,{\s])['\"]?(?:auths?|basic[_-]?auth|registry[_-]?auth)['\"]?\s*[:=]")
_SAFE_RASTER_SUFFIXES={".png"}
_TEXT_LIMIT=1024*1024
_RASTER_LIMIT=10*1024*1024
_MAX_CHANGED_FILES=10_000
_MAX_CHANGED_BYTES=100*1024*1024
_MAX_PATH_LENGTH=1024
_MAX_DIRECTORY_DEPTH=32

def _safe_raster(data:bytes,suffix:str)->bool:
    if not data or len(data)>_RASTER_LIMIT:return False
    suffix=suffix.lower()
    if suffix==".png":
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):return False
        offset=8; width=height=None; idat=[]; seen_ihdr=False; seen_iend=False; bits=color=None; seen_idat=False; idat_ended=False
        while offset+12<=len(data):
            length=struct.unpack(">I",data[offset:offset+4])[0]; kind=data[offset+4:offset+8]; end=offset+12+length
            if length>_RASTER_LIMIT or end>len(data):return False
            payload=data[offset+8:offset+8+length]; crc=struct.unpack(">I",data[offset+8+length:end])[0]
            if zlib.crc32(kind+payload)&0xffffffff != crc:return False
            if kind==b"IHDR":
                if seen_ihdr or offset!=8 or length!=13:return False
                width,height,bits,color,compression,filter_method,interlace=struct.unpack(">IIBBBBB",payload)
                if not (1<=width<=8192 and 1<=height<=8192 and width*height<=40_000_000):return False
                if bits!=8 or color not in {2,6} or compression!=0 or filter_method!=0 or interlace!=0:return False
                seen_ihdr=True
            elif kind==b"IDAT":
                if not seen_ihdr or idat_ended:return False
                seen_idat=True; idat.append(payload)
            elif kind==b"IEND":
                if length!=0 or not seen_idat:return False
                seen_iend=True; offset=end; break
            elif kind[0]&32==0:
                return False
            elif seen_idat:
                idat_ended=True
            offset=end
        if not (seen_ihdr and seen_iend and idat and offset==len(data) and width and height and bits is not None and color is not None):return False
        channels={2:3,6:4}[color]; row_bytes=width*channels; expected=height*(1+row_bytes)
        decoder=zlib.decompressobj(); decoded=bytearray()
        try:
            for part in idat:
                decoded.extend(decoder.decompress(part,expected+1-len(decoded)))
                if len(decoded)>expected or decoder.unconsumed_tail:return False
            decoded.extend(decoder.flush(expected+1-len(decoded)))
        except (zlib.error,ValueError):return False
        if len(decoded)!=expected or not decoder.eof or decoder.unused_data:return False
        return all(decoded[row*(row_bytes+1)]<=4 for row in range(height))
    return False


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
                "ALTER TABLE task_runtime ADD COLUMN recovery_sequence INTEGER NOT NULL DEFAULT 0",
            ):
                try: connection.execute(sql)
                except sqlite3.OperationalError as error:
                    if "duplicate column name" not in str(error).lower(): raise
            connection.execute("CREATE TABLE IF NOT EXISTS agent_calls(id TEXT PRIMARY KEY, task_id TEXT NOT NULL, status TEXT NOT NULL, estimated_usd REAL, actual_usd REAL, diagnostic TEXT)")
            try: connection.execute("ALTER TABLE agent_calls ADD COLUMN charged_usd REAL")
            except sqlite3.OperationalError as error:
                if "duplicate column name" not in str(error).lower(): raise
            connection.execute("CREATE TABLE IF NOT EXISTS stage_outcomes(task_id TEXT NOT NULL, stage TEXT NOT NULL, outcome_json TEXT NOT NULL, PRIMARY KEY(task_id, stage))")
            connection.execute("CREATE TABLE IF NOT EXISTS workspace_meta(task_id TEXT PRIMARY KEY, repo TEXT NOT NULL, base_commit TEXT NOT NULL, branch TEXT NOT NULL)")
            try: connection.execute("ALTER TABLE workspace_meta ADD COLUMN reviewed_commit TEXT")
            except sqlite3.OperationalError as error:
                if "duplicate column name" not in str(error).lower():raise
            try: connection.execute("ALTER TABLE workspace_meta ADD COLUMN candidate_ref TEXT")
            except sqlite3.OperationalError as error:
                if "duplicate column name" not in str(error).lower():raise
            connection.execute("CREATE TABLE IF NOT EXISTS approval_records(task_id TEXT NOT NULL, action TEXT NOT NULL, resource_json TEXT NOT NULL, PRIMARY KEY(task_id,action))")
            connection.execute("CREATE TABLE IF NOT EXISTS approval_requests(task_id TEXT NOT NULL, action TEXT NOT NULL, resource_json TEXT NOT NULL, PRIMARY KEY(task_id,action))")
            connection.execute("CREATE TABLE IF NOT EXISTS approval_records_versions(task_id TEXT NOT NULL, action TEXT NOT NULL, resource_json TEXT NOT NULL, PRIMARY KEY(task_id,action,resource_json))")
            connection.execute("CREATE TABLE IF NOT EXISTS approval_requests_versions(task_id TEXT NOT NULL, action TEXT NOT NULL, resource_json TEXT NOT NULL, PRIMARY KEY(task_id,action,resource_json))")
            connection.execute("CREATE TABLE IF NOT EXISTS system_attestations(task_id TEXT NOT NULL, name TEXT NOT NULL, value INTEGER NOT NULL, PRIMARY KEY(task_id,name))")
            connection.execute("CREATE TABLE IF NOT EXISTS execution_provenance(task_id TEXT PRIMARY KEY, mode TEXT NOT NULL, implementer TEXT NOT NULL, verifier TEXT NOT NULL, reviewer TEXT NOT NULL, profile_digest TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS recovery_checkpoints(task_id TEXT PRIMARY KEY, stage TEXT NOT NULL, sequence INTEGER NOT NULL, updated_at TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS attention_items(task_id TEXT NOT NULL, code TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(task_id,code))")
            connection.execute("CREATE TABLE IF NOT EXISTS consumed_actions(task_id TEXT NOT NULL, action TEXT NOT NULL, resource_json TEXT NOT NULL, consumed_at TEXT NOT NULL, PRIMARY KEY(task_id,action))")
            for table in ("approval_records_versions","approval_requests_versions"):
                for column,kind in (("sequence","INTEGER"),("created_at","TEXT")):
                    try: connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
                    except sqlite3.OperationalError as error:
                        if "duplicate column name" not in str(error).lower():raise
                connection.execute(f"UPDATE {table} SET sequence=rowid WHERE sequence IS NULL")
                connection.execute(f"UPDATE {table} SET created_at='' WHERE created_at IS NULL")

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
        with self._connect() as connection: approvals=frozenset(r[0] for r in connection.execute("SELECT DISTINCT action FROM approval_records_versions WHERE task_id=?",(task_id,)).fetchall())
        return TaskRuntime(row["agent_calls"], row["repair_attempts"], approvals, row["completed_calls"], row["interrupted_calls"], row["usd_spent"], row["started_at"])

    def reserve_agent_call(self, task_id: str, estimated_usd: float | None = None, lease_owner: str | None = None) -> str:
        connection = self._connect(); call_id = str(uuid.uuid4())
        try:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute("SELECT spec_json FROM tasks WHERE id=?", (task_id,)).fetchone()
            runtime = connection.execute("SELECT * FROM task_runtime WHERE task_id=?", (task_id,)).fetchone()
            if task is None or runtime is None: raise KeyError(task_id)
            import time
            if lease_owner is not None and (
                runtime["lease_owner"] != lease_owner
                or runtime["lease_until"] is None
                or runtime["lease_until"] < time.time()
            ):
                raise LeaseConflict("controller lease is not owned")
            if lease_owner is None and runtime["lease_owner"] is not None:
                raise LeaseConflict("controller lease is not owned")
            spec = TaskSpec.model_validate_json(task["spec_json"]); now = datetime.now(UTC)
            started = datetime.fromisoformat(runtime["started_at"]) if runtime["started_at"] else now
            cost = estimated_usd
            if runtime["agent_calls"] >= spec.budget.max_agent_calls or (now-started).total_seconds() >= spec.budget.max_minutes*60:
                raise BudgetExceeded("call or elapsed-time budget exhausted")
            if cost is None: raise BudgetExceeded("unknown call cost cannot be reserved conservatively")
            cost = cost or 0.0
            pending=connection.execute("SELECT COALESCE(SUM(estimated_usd),0) FROM agent_calls WHERE task_id=? AND status='started'",(task_id,)).fetchone()[0]
            if runtime["usd_spent"] + pending + cost > spec.budget.max_usd: raise BudgetExceeded("USD budget exhausted")
            connection.execute("UPDATE task_runtime SET agent_calls=agent_calls+1, started_at=CASE WHEN started_at='' THEN ? ELSE started_at END WHERE task_id=?", (now.isoformat(), task_id))
            connection.execute("INSERT INTO agent_calls(id,task_id,status,estimated_usd,actual_usd,diagnostic,charged_usd) VALUES(?,?,?,?,?,?,?)", (call_id, task_id, "started", cost, None, None, None)); connection.commit()
            return call_id
        except Exception: connection.rollback(); raise
        finally: connection.close()

    def assert_agent_call_available(
        self,
        task_id: str,
        estimated_usd: float | None,
        *,
        lease_owner: str,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT spec_json FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            runtime = connection.execute(
                "SELECT * FROM task_runtime WHERE task_id=?", (task_id,)
            ).fetchone()
            if task is None or runtime is None:
                raise KeyError(task_id)
            import time
            if (
                runtime["lease_owner"] != lease_owner
                or runtime["lease_until"] is None
                or runtime["lease_until"] < time.time()
            ):
                raise LeaseConflict("controller lease is not owned")
            if estimated_usd is None:
                raise BudgetExceeded("unknown call cost cannot be reserved conservatively")
            spec = TaskSpec.model_validate_json(task["spec_json"])
            now = datetime.now(UTC)
            started = datetime.fromisoformat(runtime["started_at"]) if runtime["started_at"] else now
            if (
                runtime["agent_calls"] >= spec.budget.max_agent_calls
                or (now - started).total_seconds() >= spec.budget.max_minutes * 60
            ):
                raise BudgetExceeded("call or elapsed-time budget exhausted")
            pending = connection.execute(
                "SELECT COALESCE(SUM(estimated_usd),0) FROM agent_calls WHERE task_id=? AND status='started'",
                (task_id,),
            ).fetchone()[0]
            if runtime["usd_spent"] + pending + estimated_usd > spec.budget.max_usd:
                raise BudgetExceeded("USD budget exhausted")
        finally:
            connection.close()

    def assert_paid_operations_available(
        self,
        task_id: str,
        estimated_usd: tuple[float | None, ...],
        *,
        lease_owner: str,
    ) -> None:
        connection = self._connect()
        try:
            import math
            import time
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT spec_json FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            runtime = connection.execute(
                "SELECT * FROM task_runtime WHERE task_id=?", (task_id,)
            ).fetchone()
            if task is None or runtime is None:
                raise KeyError(task_id)
            if (
                runtime["lease_owner"] != lease_owner
                or runtime["lease_until"] is None
                or runtime["lease_until"] < time.time()
            ):
                raise LeaseConflict("controller lease is not owned")
            if not estimated_usd or any(
                value is None
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
                for value in estimated_usd
            ):
                raise BudgetExceeded("paid operations require positive conservative estimates")
            spec = TaskSpec.model_validate_json(task["spec_json"])
            now = datetime.now(UTC)
            started = datetime.fromisoformat(runtime["started_at"]) if runtime["started_at"] else now
            if (
                runtime["agent_calls"] + len(estimated_usd) > spec.budget.max_agent_calls
                or (now - started).total_seconds() >= spec.budget.max_minutes * 60
            ):
                raise BudgetExceeded("call or elapsed-time budget exhausted")
            pending = connection.execute(
                "SELECT COALESCE(SUM(estimated_usd),0) FROM agent_calls WHERE task_id=? AND status='started'",
                (task_id,),
            ).fetchone()[0]
            if runtime["usd_spent"] + pending + sum(estimated_usd) > spec.budget.max_usd:
                raise BudgetExceeded("USD budget exhausted")
        finally:
            connection.close()

    def _finish_call(self, task_id: str, call_id: str, status: str, actual_usd: float | None, diagnostic: str = "") -> None:
        column = "completed_calls" if status == "completed" else "interrupted_calls"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row=connection.execute("SELECT estimated_usd FROM agent_calls WHERE id=? AND task_id=? AND status='started'", (call_id, task_id)).fetchone()
            if row is None: raise StateConflict("call is not pending")
            if actual_usd is not None and actual_usd > row["estimated_usd"]: raise BudgetExceeded("actual cost exceeds conservative reservation")
            charged=row["estimated_usd"] if actual_usd is None else actual_usd
            cursor = connection.execute("UPDATE agent_calls SET status=?,actual_usd=?,charged_usd=?,diagnostic=? WHERE id=? AND task_id=? AND status='started'", (status, actual_usd, charged, diagnostic, call_id, task_id))
            if cursor.rowcount != 1: raise StateConflict("call is not pending")
            connection.execute(f"UPDATE task_runtime SET {column}={column}+1, usd_spent=usd_spent+? WHERE task_id=?", (charged, task_id))
    def complete_agent_call(self, task_id: str, call_id: str, actual_usd: float | None = None, diagnostic: str = ""): self._finish_call(task_id, call_id, "completed", actual_usd, diagnostic[:200])
    def interrupt_agent_call(self, task_id: str, call_id: str, diagnostic: str, actual_usd: float | None = None):
        with self._connect() as connection: row=connection.execute("SELECT estimated_usd FROM agent_calls WHERE id=? AND task_id=?",(call_id,task_id)).fetchone()
        if row is None: raise StateConflict("call is not pending")
        self._finish_call(task_id,call_id,"interrupted",actual_usd,diagnostic[:200])
    def execute_paid_operation(
        self,
        task_id: str,
        estimated_usd: float | None,
        operation,
        *,
        lease_owner: str | None = None,
    ):
        if estimated_usd is None or estimated_usd <= 0: raise BudgetExceeded("paid operation requires a positive conservative estimate")
        call=self.reserve_agent_call(task_id,estimated_usd,lease_owner)
        try: result=operation()
        except BaseException as error:
            self.interrupt_agent_call(task_id,call,type(error).__name__); raise
        self.complete_agent_call(task_id,call,estimated_usd)
        return result

    def finalize_overrun_and_pause(self, task_id: str, call_id: str, actual_usd: float, expected: TaskState) -> None:
        now=datetime.now(UTC).isoformat(); connection=self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            call=connection.execute("SELECT estimated_usd FROM agent_calls WHERE id=? AND task_id=? AND status='started'",(call_id,task_id)).fetchone()
            task=connection.execute("SELECT state FROM tasks WHERE id=?",(task_id,)).fetchone()
            if call is None or task is None or task[0] != expected.value: raise StateConflict("overrun finalization conflict")
            connection.execute("UPDATE agent_calls SET status='overrun',actual_usd=?,diagnostic='actual cost exceeded reservation' WHERE id=?",(actual_usd,call_id))
            connection.execute("UPDATE task_runtime SET interrupted_calls=interrupted_calls+1,usd_spent=usd_spent+? WHERE task_id=?",(actual_usd,task_id))
            connection.execute("UPDATE tasks SET state=?,updated_at=? WHERE id=?",(TaskState.PAUSED_BUDGET.value,now,task_id)); connection.commit()
        except Exception: connection.rollback(); raise
        finally: connection.close()
        layout=RunLayout(self.runs_root/task_id); layout.write_state(TaskState.PAUSED_BUDGET); layout.append_event({"at":now,"event":"cost-overrun","state":TaskState.PAUSED_BUDGET.value})

    def set_workspace(self, task_id: str, repo: str, base_commit: str, branch: str) -> None:
        with self._connect() as connection: connection.execute("INSERT OR REPLACE INTO workspace_meta(task_id,repo,base_commit,branch,reviewed_commit,candidate_ref) VALUES(?,?,?,?,NULL,NULL)",(task_id,repo,base_commit,branch))
    def workspace(self, task_id: str):
        with self._connect() as connection: return connection.execute("SELECT * FROM workspace_meta WHERE task_id=?",(task_id,)).fetchone()

    def record_execution_provenance(
        self,
        task_id: str,
        *,
        mode: str,
        implementer: str,
        verifier: str,
        reviewer: str,
        profile_digest: str,
    ) -> None:
        value = {
            "mode": mode,
            "implementer": implementer,
            "verifier": verifier,
            "reviewer": reviewer,
            "profile_digest": profile_digest,
        }
        valid = (
            mode in {"simulation", "live"}
            and isinstance(profile_digest, str)
            and 0 < len(profile_digest) <= 128
            and (
                (mode == "simulation" and (implementer, verifier, reviewer) == ("fake", "fake", "fake"))
                or (
                    mode == "live"
                    and implementer in {"cursor", "deepseek"}
                    and verifier == "codex"
                    and reviewer == "antigravity"
                )
            )
        )
        if not valid:
            raise ValueError("invalid execution provenance")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT mode,implementer,verifier,reviewer,profile_digest FROM execution_provenance WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if existing is not None:
                if any(existing[key] != expected for key, expected in value.items()):
                    raise ValueError("execution provenance is immutable")
                return
            if connection.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is None:
                raise KeyError(task_id)
            connection.execute(
                "INSERT INTO execution_provenance(task_id,mode,implementer,verifier,reviewer,profile_digest) VALUES(?,?,?,?,?,?)",
                (task_id, mode, implementer, verifier, reviewer, profile_digest),
            )

    def execution_provenance(self, task_id: str) -> dict[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT mode,implementer,verifier,reviewer,profile_digest FROM execution_provenance WHERE task_id=?",
                (task_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def recovery_checkpoint(self, task_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT stage,sequence,updated_at FROM recovery_checkpoints WHERE task_id=?",
                (task_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def transition_recoverable(
        self,
        task_id: str,
        expected: TaskState,
        stage: str,
        event: str,
    ) -> StoredTask:
        if stage not in {"implement", "verify", "review"}:
            raise ValueError("invalid recovery checkpoint stage")
        timestamp = datetime.now(UTC).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT state FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            runtime = connection.execute(
                "SELECT recovery_sequence FROM task_runtime WHERE task_id=?", (task_id,)
            ).fetchone()
            if task is None or runtime is None:
                raise KeyError(task_id)
            if task["state"] != expected.value:
                raise StateConflict(f"expected {expected.value}, found {task['state']}")
            sequence = runtime["recovery_sequence"] + 1
            connection.execute(
                "UPDATE task_runtime SET recovery_sequence=? WHERE task_id=?",
                (sequence, task_id),
            )
            connection.execute(
                "INSERT INTO recovery_checkpoints(task_id,stage,sequence,updated_at) VALUES(?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET stage=excluded.stage,sequence=excluded.sequence,updated_at=excluded.updated_at",
                (task_id, stage, sequence, timestamp),
            )
            connection.execute(
                "UPDATE tasks SET state=?,updated_at=? WHERE id=?",
                (TaskState.FAILED_RECOVERABLE.value, timestamp, task_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        layout = RunLayout(self.runs_root / task_id)
        layout.write_state(TaskState.FAILED_RECOVERABLE)
        layout.append_event({
            "at": timestamp, "event": event,
            "state": TaskState.FAILED_RECOVERABLE.value,
            "recovery_stage": stage, "recovery_sequence": sequence,
        })
        return self.load(task_id)

    def accept_recovery(
        self,
        task_id: str,
        *,
        stage: str,
        sequence: int,
        target: TaskState,
        lease_owner: str,
    ) -> StoredTask:
        expected_target = {
            "implement": TaskState.IMPLEMENT,
            "verify": TaskState.VERIFY,
            "review": TaskState.REVIEW,
        }.get(stage)
        if expected_target is not target:
            raise ValueError("recovery checkpoint target mismatch")
        timestamp = datetime.now(UTC).isoformat()
        connection = self._connect()
        try:
            import time
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT state FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            runtime = connection.execute(
                "SELECT lease_owner,lease_until FROM task_runtime WHERE task_id=?", (task_id,)
            ).fetchone()
            checkpoint = connection.execute(
                "SELECT stage,sequence FROM recovery_checkpoints WHERE task_id=?", (task_id,)
            ).fetchone()
            if task is None or runtime is None:
                raise KeyError(task_id)
            if task["state"] != TaskState.FAILED_RECOVERABLE.value:
                raise StateConflict("task is not recoverable")
            if (
                runtime["lease_owner"] != lease_owner
                or runtime["lease_until"] is None
                or runtime["lease_until"] < time.time()
            ):
                raise LeaseConflict("controller lease is not owned")
            if (
                checkpoint is None
                or checkpoint["stage"] != stage
                or checkpoint["sequence"] != sequence
            ):
                raise StateConflict("recovery checkpoint changed")
            connection.execute(
                "UPDATE task_runtime SET repair_attempts=repair_attempts+1 WHERE task_id=?",
                (task_id,),
            )
            connection.execute("DELETE FROM recovery_checkpoints WHERE task_id=?", (task_id,))
            connection.execute(
                "UPDATE tasks SET state=?,updated_at=? WHERE id=?",
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
        layout.append_event({
            "at": timestamp, "event": f"resume-{stage}", "state": target.value,
        })
        return self.load(task_id)

    @staticmethod
    def _git_plumbing(work:Path,args:list[str],stdin:bytes|None=None)->bytes:
        environment={
            "GIT_AUTHOR_NAME":"TriAgent Controller","GIT_AUTHOR_EMAIL":"triagent@localhost",
            "GIT_COMMITTER_NAME":"TriAgent Controller","GIT_COMMITTER_EMAIL":"triagent@localhost",
        }
        return run_git(work,args,stdin=stdin,extra_env=environment).stdout

    @staticmethod
    def _declared_repo_path(raw: str, repo: Path, *, diagnostic: str) -> str:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(diagnostic)
        raw = raw.strip()
        candidate = Path(raw)
        if candidate.is_absolute():
            resolved = candidate.resolve()
            if resolved == repo:
                return ""
            if repo not in resolved.parents:
                raise ValueError(diagnostic)
            return resolved.relative_to(repo).as_posix().rstrip("/")
        normalized = raw.replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or any(part in {"", ".."} for part in path.parts):
            raise ValueError(diagnostic)
        if path == PurePosixPath("."):
            return ""
        return path.as_posix().rstrip("/")

    @staticmethod
    def _path_matches(relative: str, declaration: str) -> bool:
        return declaration == "" or relative == declaration or relative.startswith(declaration + "/")

    def _enforce_candidate_scope(self, task_id: str, actual_changes: set[str]) -> None:
        task = self.load(task_id)
        meta = self.workspace(task_id)
        if meta is None:
            raise ValueError("candidate scope rejected: invalid scope")
        repo = Path(meta["repo"]).resolve()
        scopes = [
            self._declared_repo_path(
                item, repo, diagnostic="candidate scope rejected: invalid scope"
            )
            for item in task.spec.scope
        ]
        if not scopes:
            raise ValueError("candidate scope rejected: invalid scope")
        for relative in actual_changes:
            if not any(self._path_matches(relative, scope) for scope in scopes):
                raise ValueError("candidate scope rejected: out of scope")

        forbidden_paths = []
        for item in task.spec.forbidden:
            if not isinstance(item, str) or not item.strip():
                continue
            value = item.strip()
            path_like = Path(value).is_absolute() or "/" in value or "\\" in value or not any(char.isspace() for char in value)
            if not path_like:
                continue
            forbidden_paths.append(
                self._declared_repo_path(
                    value,
                    repo,
                    diagnostic="candidate scope rejected: invalid forbidden path",
                )
            )
        for relative in actual_changes:
            if any(self._path_matches(relative, forbidden) for forbidden in forbidden_paths):
                raise ValueError("candidate scope rejected: forbidden path")

    def _candidate_manifest(
        self,
        task_id: str,
        work: Path,
        base: str,
        changed_paths: list[str] | None = None,
        require_changes: bool = False,
    ) -> dict[str, tuple[str, str, bytes]]:
        try:
            base_rows=self._git_plumbing(work,["ls-tree","-r","-z",base]).split(b"\0")
        except (OSError,subprocess.CalledProcessError) as error:raise ValueError("candidate manifest rejected") from error
        base_entries={}
        for row in base_rows:
            if not row:continue
            meta,name=row.split(b"\t",1); mode,kind,oid=meta.decode("ascii").split()
            if kind=="commit" or mode=="160000":raise ValueError("candidate manifest rejected: gitlink")
            if kind!="blob" or mode not in {"100644","100755"}:raise ValueError("candidate manifest rejected")
            base_entries[name.decode("utf-8")]=(mode,kind,oid)
        try:
            control_basenames={".gitignore",".gitattributes",".gitmodules"}
            base_controls={name for name in base_entries if PurePosixPath(name).name in control_basenames}
            current_controls=set()
            for root,dirs,files in os.walk(work,topdown=True,followlinks=False):
                dirs[:]=[name for name in dirs if name!=".git"]
                for filename in files:
                    if filename in control_basenames:current_controls.add((Path(root)/filename).relative_to(work).as_posix())
            if current_controls!=base_controls:raise ValueError("candidate manifest rejected")
            for name in base_controls:
                current=(work/name).read_bytes();original=self._git_plumbing(work,["cat-file","blob",base_entries[name][2]])
                if current.replace(b"\r\n",b"\n")!=original.replace(b"\r\n",b"\n"):raise ValueError("candidate manifest rejected")
            tracked=set(filter(None,self._git_plumbing(work,["ls-files","--cached","-z"]).decode("utf-8").split("\0")))
            untracked=set(filter(None,self._git_plumbing(work,["ls-files","--others","--exclude-per-directory=.gitignore","-z"]).decode("utf-8").split("\0")))
            tracked_changes=set()
            for name in set(base_entries)|tracked:
                target=work/name; base_entry=base_entries.get(name)
                if base_entry is None or not target.exists():
                    tracked_changes.add(name);continue
                info=target.lstat()
                if not stat.S_ISREG(info.st_mode):tracked_changes.add(name);continue
                digest=hashlib.sha1(usedforsecurity=False);digest.update(b"blob "+str(info.st_size).encode()+b"\0")
                with target.open("rb") as stream:
                    while chunk:=stream.read(64*1024):digest.update(chunk)
                if digest.hexdigest()==base_entry[2]:continue
                equivalent=False
                if info.st_size<=_TEXT_LIMIT:
                    current=target.read_bytes(); original=self._git_plumbing(work,["cat-file","blob",base_entry[2]])
                    if b"\0" not in current and b"\0" not in original:equivalent=current.replace(b"\r\n",b"\n")==original.replace(b"\r\n",b"\n")
                if not equivalent:tracked_changes.add(name)
            actual_changes=tracked_changes|untracked
            if require_changes and not actual_changes:
                raise ValueError("candidate manifest rejected: no changes")
        except (OSError,UnicodeError,subprocess.CalledProcessError) as error:raise ValueError("candidate manifest rejected") from error
        if changed_paths is not None:
            supplied=set(changed_paths)
            if len(supplied)!=len(changed_paths) or supplied!=actual_changes:raise ValueError("changed path manifest mismatch")
        else:
            changed_paths=sorted(actual_changes)
        if len(actual_changes)>_MAX_CHANGED_FILES:raise ValueError("candidate limits exceeded")
        total_bytes=0
        for name in actual_changes:
            path=PurePosixPath(name)
            if path.is_absolute() or "\\" in name or any(part in {"",".",".."} for part in path.parts):raise ValueError("candidate manifest rejected")
            if len(name)>_MAX_PATH_LENGTH or len(path.parts)>_MAX_DIRECTORY_DEPTH:raise ValueError("candidate limits exceeded")
            target=work/name
            if target.exists():
                try:total_bytes+=target.stat().st_size
                except OSError as error:raise ValueError("candidate manifest rejected") from error
                if total_bytes>_MAX_CHANGED_BYTES:raise ValueError("candidate limits exceeded")
        self._enforce_candidate_scope(task_id, actual_changes)
        control_names={name for name in actual_changes if PurePosixPath(name).name in control_basenames}
        if control_names:raise ValueError("candidate manifest rejected")
        manifest={}
        try:
            members=set(base_entries)|untracked|tracked_changes
            for relative in sorted(members):
                path=work/relative
                if not path.exists():continue
                info=path.lstat()
                if not stat.S_ISREG(info.st_mode):raise ValueError("candidate manifest rejected")
                base_entry=base_entries.get(relative)
                if relative not in actual_changes and base_entry is not None:
                    manifest[relative]=(base_entry[0],base_entry[2],b"")
                    continue
                limit=_RASTER_LIMIT if path.suffix.lower() in _SAFE_RASTER_SUFFIXES else _TEXT_LIMIT
                if info.st_size>limit:raise ValueError("candidate manifest rejected")
                chunks=[]; size=0
                with path.open("rb") as stream:
                    while chunk:=stream.read(64*1024):
                        size+=len(chunk)
                        if size>limit:raise ValueError("candidate manifest rejected")
                        chunks.append(chunk)
                data=b"".join(chunks)
                if "\n" in relative or "\r" in relative or "\t" in relative or _CREDENTIAL_PATH.search(relative):raise ValueError("candidate manifest rejected")
                if any(part in {".triagent",".superpowers"} for part in PurePosixPath(relative).parts):raise ValueError("candidate manifest rejected")
                suffix=path.suffix.lower()
                if suffix in _SAFE_RASTER_SUFFIXES:
                    if not _safe_raster(data,suffix):raise ValueError("candidate manifest rejected")
                else:
                    if len(data)>_TEXT_LIMIT or b"\0" in data:raise ValueError("candidate manifest rejected")
                    try:text=data.decode("utf-8")
                    except UnicodeDecodeError as error:raise ValueError("candidate manifest rejected") from error
                    if _CREDENTIAL_CONTENT.search(text) or _STRUCTURED_AUTH_FIELD.search(text):raise ValueError("candidate manifest rejected")
                base_mode=base_entries.get(relative,("100644",None,None))[0]
                mode="100755" if base_mode=="100755" or (os.name!="nt" and info.st_mode&stat.S_IXUSR) else "100644"
                manifest[relative]=(mode,"",data)
        except (OSError,UnicodeError,subprocess.CalledProcessError) as error:raise ValueError("candidate manifest rejected") from error
        for name,(mode,oid,data) in list(manifest.items()):
            if not oid:
                oid=self._git_plumbing(work,["hash-object","-w","--stdin"],data).decode("ascii").strip()
                manifest[name]=(mode,oid,data)
        return manifest

    def _candidate_tree(self,work:Path,manifest:dict[str,tuple[str,str,bytes]])->str:
        nodes={}
        for name,(mode,oid,_data) in manifest.items():
            parts=PurePosixPath(name).parts; node=nodes
            for part in parts[:-1]:node=node.setdefault(part,{})
            node[parts[-1]]=(mode,oid)
        def build(node)->str:
            records=[]
            for name,value in sorted(node.items()):
                if isinstance(value,dict):records.append(("040000","tree",build(value),name))
                else:records.append((value[0],"blob",value[1],name))
            payload=b"".join(f"{mode} {kind} {oid}\t".encode()+name.encode("utf-8")+b"\0" for mode,kind,oid,name in records)
            return self._git_plumbing(work,["mktree","-z"],payload).decode("ascii").strip()
        return build(nodes)

    def _persist_candidate(self,task_id:str,reviewed:str,candidate_ref:str,old:str)->bool:
        with self._connect() as connection:
            cursor=connection.execute("UPDATE workspace_meta SET reviewed_commit=?,candidate_ref=? WHERE task_id=? AND COALESCE(reviewed_commit,'')=?",(reviewed,candidate_ref,task_id,old))
            return cursor.rowcount==1

    def materialize_reviewed_commit(
        self,
        task_id: str,
        changed_paths: list[str] | None = None,
        require_changes: bool = False,
    ) -> str:
        meta=self.workspace(task_id)
        if meta is None:
            if self.load(task_id).spec.visual_check == "required":raise ValueError("required visual artifact unavailable")
            return "fake"
        work=self.runs_root/task_id/"worktree"
        if not work.exists():raise ValueError("candidate materialization failed")
        try:
            manifest = self._candidate_manifest(
                task_id, work, meta["base_commit"], changed_paths, require_changes
            )
            tree=self._candidate_tree(work,manifest)
            reviewed=self._git_plumbing(work,["commit-tree",tree,"-p",meta["base_commit"]],f"triagent candidate {task_id}\n".encode()).decode("ascii").strip()
            actual={}
            for row in self._git_plumbing(work,["ls-tree","-r","-z",reviewed]).split(b"\0"):
                if not row:continue
                entry,name=row.split(b"\t",1); mode,kind,oid=entry.decode("ascii").split(); actual[name.decode("utf-8")]=(mode,oid)
            expected={name:(mode,oid) for name,(mode,oid,_data) in manifest.items()}
            if actual!=expected:raise ValueError("candidate tree mismatch")
            stable = self._candidate_manifest(
                task_id, work, meta["base_commit"], changed_paths, require_changes
            )
            if {name:(mode,oid) for name,(mode,oid,_data) in stable.items()}!=expected:raise ValueError("candidate manifest changed")
            candidate_ref=f"refs/triagent/reviewed/{task_id}"
            try:prior_ref=self._git_plumbing(work,["rev-parse","--verify",candidate_ref]).decode().strip()
            except subprocess.CalledProcessError:prior_ref=""
            self._git_plumbing(work,["update-ref",candidate_ref,reviewed,prior_ref or "0"*40])
            if self._git_plumbing(work,["rev-parse",candidate_ref]).decode().strip()!=reviewed:raise ValueError("candidate ref mismatch")
        except (OSError,subprocess.CalledProcessError,UnicodeError,ValueError) as error:
            if isinstance(error,ValueError) and str(error).startswith(("candidate manifest rejected","changed path manifest mismatch","candidate limits exceeded","candidate scope rejected")):raise
            raise ValueError("candidate materialization failed") from error
        old=meta["reviewed_commit"] or ""
        try:persisted=self._persist_candidate(task_id,reviewed,candidate_ref,old)
        except Exception:
            persisted=False
        if not persisted:
            try:
                if prior_ref:self._git_plumbing(work,["update-ref",candidate_ref,prior_ref,reviewed])
                else:self._git_plumbing(work,["update-ref","-d",candidate_ref,reviewed])
                if prior_ref:
                    if self._git_plumbing(work,["rev-parse","--verify",candidate_ref]).decode().strip()!=prior_ref:raise ValueError
                else:
                    try:self._git_plumbing(work,["rev-parse","--verify",candidate_ref]);raise ValueError
                    except subprocess.CalledProcessError:pass
            except Exception as error:raise ValueError("candidate rollback failed") from error
            raise ValueError("candidate persistence conflict")
        return reviewed

    def restore_candidate_worktree(self,task_id:str)->None:
        meta=self.workspace(task_id); work=(self.runs_root/task_id/"worktree").resolve()
        if meta is None or not meta["reviewed_commit"] or not meta["candidate_ref"] or not work.exists():raise ValueError("candidate restore unavailable")
        candidate=meta["reviewed_commit"]
        try:
            if self._git_plumbing(work,["rev-parse",meta["candidate_ref"]]).decode().strip()!=candidate:raise ValueError
            expected={}
            for row in self._git_plumbing(work,["ls-tree","-r","-z",candidate]).split(b"\0"):
                if not row:continue
                entry,name_bytes=row.split(b"\t",1); mode,kind,oid=entry.decode("ascii").split(); name=name_bytes.decode("utf-8")
                if kind!="blob" or mode not in {"100644","100755"}:raise ValueError
                expected[name]=(mode,oid)
            for root,dirs,files in os.walk(work,topdown=False,followlinks=False):
                root_path=Path(root)
                for name in files:
                    path=root_path/name; relative=path.relative_to(work).as_posix()
                    if relative==".git":continue
                    if relative not in expected:path.unlink()
                for name in dirs:
                    path=root_path/name; relative=path.relative_to(work).as_posix()
                    if path.is_symlink():path.unlink()
                    elif not any(item==relative or item.startswith(relative+"/") for item in expected):
                        try:path.rmdir()
                        except OSError:pass
            for name,(mode,oid) in expected.items():
                path=(work/name).resolve()
                if work not in path.parents:raise ValueError
                path.parent.mkdir(parents=True,exist_ok=True)
                data=self._git_plumbing(work,["cat-file","blob",oid])
                path.write_bytes(data)
                if os.name!="nt":path.chmod(0o755 if mode=="100755" else 0o644)
            current_head=self._git_plumbing(work,["rev-parse","HEAD"]).decode("ascii").strip()
            if current_head!=candidate:
                self._git_plumbing(work,["update-ref","--no-deref","HEAD",candidate,current_head])
            self._git_plumbing(work,["read-tree",candidate])
        except (OSError,subprocess.CalledProcessError,UnicodeError,ValueError) as error:raise ValueError("candidate restore unavailable") from error

    def approval_manifest(self, task_id: str) -> dict[str,str]:
        meta=self.workspace(task_id)
        empty=hashlib.sha256(b"").hexdigest()
        if meta is None:
            if self.load(task_id).spec.visual_check == "required":raise ValueError("required visual artifact unavailable")
            return {"repo":"fake","task_id":task_id,"branch":"fake","base_commit":"fake","reviewed_commit":"fake","reviewed_head":"fake","candidate_ref":"fake","canonical_diff_digest":empty,"visual_artifact_digest":empty,"visual_artifact_version":empty}
        work=self.runs_root/task_id/"worktree"
        if not work.exists():work=Path(meta["repo"])
        reviewed=meta["reviewed_commit"]
        if not reviewed:raise ValueError("reviewed commit unavailable")
        try:
            candidate_ref=meta["candidate_ref"]
            if not candidate_ref or self._git_plumbing(work,["rev-parse",candidate_ref]).decode().strip()!=reviewed:raise ValueError("candidate ref mismatch")
            if self._git_plumbing(work,["cat-file","-t",reviewed]).decode().strip()!="commit":raise ValueError("candidate type mismatch")
            diff=self._git_plumbing(work,["diff","--binary",meta["base_commit"],reviewed])
        except (OSError,subprocess.CalledProcessError,UnicodeError,ValueError) as error:
            raise ValueError("approval manifest unavailable") from error
        diff_digest=hashlib.sha256(diff).hexdigest()
        artifacts=[]
        for outcome in self.outcomes(task_id).values():
            for name in outcome.artifacts:
                path=PurePosixPath(name)
                if path.is_absolute() or "\\" in name or any(part in {"",".",".."} for part in path.parts) or path.suffix.lower() not in _SAFE_RASTER_SUFFIXES:continue
                try:
                    entry=self._git_plumbing(work,["ls-tree",reviewed,"--",name]).decode("utf-8").strip()
                    if not entry or not entry.startswith(("100644 blob ","100755 blob ")):continue
                    data=self._git_plumbing(work,["show",f"{reviewed}:{name}"])
                except (OSError,subprocess.CalledProcessError):continue
                if not _safe_raster(data,path.suffix):continue
                artifacts.append({"name":name,"sha256":hashlib.sha256(data).hexdigest(),"size":len(data)})
        if self.load(task_id).spec.visual_check == "required" and not artifacts:raise ValueError("required visual artifact unavailable")
        artifact_digest=hashlib.sha256(json.dumps(sorted(artifacts,key=lambda x:x["name"]),sort_keys=True,separators=(",",":")).encode()).hexdigest()
        return {"repo":meta["repo"],"task_id":task_id,"branch":meta["branch"],"base_commit":meta["base_commit"],"reviewed_commit":reviewed,"reviewed_head":reviewed,"candidate_ref":meta["candidate_ref"],"canonical_diff_digest":diff_digest,"visual_artifact_digest":artifact_digest,"visual_artifact_version":artifact_digest}

    def _verify_approval_manifest(self, task_id:str,action:str,resource_json:str)->None:
        if action not in {"visual","outcome","merge"} or self.workspace(task_id) is None:return
        resource=json.loads(resource_json); current=self.approval_manifest(task_id)
        if any(resource.get(key)!=value for key,value in current.items()):raise ValueError("approval manifest changed")

    def record_attention(self,task_id:str,code:str)->None:
        allowed={"transport-cleanup-failed"}
        if code not in allowed:raise ValueError("unknown attention code")
        with self._connect() as connection:connection.execute("INSERT OR REPLACE INTO attention_items VALUES(?,?,?)",(task_id,code,datetime.now(UTC).isoformat()))

    def attention_items(self,task_id:str)->list[str]:
        with self._connect() as connection:rows=connection.execute("SELECT code FROM attention_items WHERE task_id=? ORDER BY created_at,code",(task_id,)).fetchall()
        return [row[0] for row in rows]

    def acquire_lease(self, task_id: str, owner: str, seconds: float) -> None:
        import time
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute("UPDATE task_runtime SET lease_owner=?,lease_until=? WHERE task_id=? AND (lease_owner IS NULL OR lease_until<? OR lease_owner=?)", (owner, time.time()+seconds, task_id, time.time(), owner))
            if cursor.rowcount != 1: raise LeaseConflict("another controller owns the task")

    def release_lease(self, task_id: str, owner: str) -> None:
        with self._connect() as connection: connection.execute("UPDATE task_runtime SET lease_owner=NULL,lease_until=NULL WHERE task_id=? AND lease_owner=?", (task_id, owner))

    def renew_lease(self, task_id: str, owner: str, seconds: float) -> None:
        import time
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor=connection.execute("UPDATE task_runtime SET lease_until=? WHERE task_id=? AND lease_owner=? AND lease_until>=?", (time.time()+seconds, task_id, owner, time.time()))
            if cursor.rowcount != 1: raise LeaseConflict("controller lease lost")

    def record_outcome(self, task_id: str, outcome: StageOutcome) -> None:
        from triagent.adapters._cli import sanitize
        secrets=tuple(value for key,value in os.environ.items() if value and re.search(r"(?i)(api[_-]?key|token|secret|password|credential)",key))
        cleaned=StageOutcome.model_validate(sanitize(outcome.model_dump(mode="json"),secrets))
        with self._connect() as connection: connection.execute("INSERT OR REPLACE INTO stage_outcomes VALUES(?,?,?)", (task_id, cleaned.stage, cleaned.model_dump_json()))
    def outcomes(self, task_id: str) -> dict[str, StageOutcome]:
        with self._connect() as connection: rows=connection.execute("SELECT * FROM stage_outcomes WHERE task_id=?", (task_id,)).fetchall()
        return {r["stage"]: StageOutcome.model_validate_json(r["outcome_json"]) for r in rows}
    def fail_setup(self, task_id: str, diagnostic: str, preserved_resources: list[str] | None = None) -> None:
        self.record_outcome(task_id, StageOutcome(stage="setup", status="failed", summary="setup-failed", diagnostic=diagnostic[:500], evidence=preserved_resources or []))
        self.transition(task_id, TaskState.SPEC, TaskState.FAILED_FINAL, "setup-failed")

    def approve_and_transition(self, task_id: str, action: str, expected: TaskState, target: TaskState) -> None:
        timestamp=datetime.now(UTC).isoformat(); connection=self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row=connection.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            runtime=connection.execute("SELECT approvals_json FROM task_runtime WHERE task_id=?", (task_id,)).fetchone(); requests=connection.execute("SELECT r.resource_json FROM approval_requests_versions r WHERE task_id=? AND action=? AND NOT EXISTS(SELECT 1 FROM approval_records_versions a WHERE a.task_id=r.task_id AND a.action=r.action AND a.resource_json=r.resource_json)",(task_id,action)).fetchall()
            if row is None or runtime is None or len(requests)!=1: raise ValueError("approval request version is missing or ambiguous")
            request=requests[0]; self._verify_approval_manifest(task_id,action,request[0])
            if row["state"] != expected.value: raise StateConflict("approval state changed")
            approvals=sorted(set(json.loads(runtime[0]))|{action})
            connection.execute("UPDATE task_runtime SET approvals_json=? WHERE task_id=?", (json.dumps(approvals),task_id))
            record_sequence=connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM approval_records_versions WHERE task_id=? AND action=?",(task_id,action)).fetchone()[0]
            connection.execute("INSERT INTO approval_records_versions(task_id,action,resource_json,sequence,created_at) VALUES(?,?,?,?,?)",(task_id,action,request[0],record_sequence,timestamp))
            connection.execute("INSERT INTO consumed_actions(task_id,action,resource_json,consumed_at) VALUES(?,?,?,?)",(task_id,action,request[0],timestamp))
            meta=connection.execute("SELECT * FROM workspace_meta WHERE task_id=?",(task_id,)).fetchone()
            version=self.approval_manifest(task_id)
            canonical=json.dumps(version,sort_keys=True,separators=(",",":")); version["resource_version"]=hashlib.sha256(canonical.encode()).hexdigest(); version["requested_at"]=timestamp
            for pending in ("outcome","merge"):
                sequence=connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM approval_requests_versions WHERE task_id=? AND action=?",(task_id,pending)).fetchone()[0]
                connection.execute("INSERT OR REPLACE INTO approval_requests_versions(task_id,action,resource_json,sequence,created_at) VALUES(?,?,?,?,?)",(task_id,pending,json.dumps(version,sort_keys=True),sequence,timestamp))
            connection.execute("UPDATE tasks SET state=?,updated_at=? WHERE id=?", (target.value,timestamp,task_id)); connection.commit()
        except Exception: connection.rollback(); raise
        finally: connection.close()
        layout=RunLayout(self.runs_root/task_id); layout.write_state(target); layout.append_event({"at":timestamp,"event":f"{action}-approved","state":target.value})

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

    def record_approval(self, task_id: str, approval: str, resource: dict[str,str] | None = None) -> TaskRuntime:
        raise PermissionError("operator approvals require an exact outstanding request")
    def record_attestation(self, task_id: str, name: str, value: bool) -> None:
        if name not in {"live-confirmed","billing-confirmed"}: raise ValueError("unknown system attestation")
        with self._connect() as connection: connection.execute("INSERT OR REPLACE INTO system_attestations VALUES(?,?,?)",(task_id,name,int(value)))

    def approval_resource(self, task_id: str, action: str) -> dict[str,str] | None:
        with self._connect() as connection: row=connection.execute("SELECT resource_json FROM approval_records_versions WHERE task_id=? AND action=? ORDER BY sequence DESC,created_at DESC LIMIT 1",(task_id,action)).fetchone()
        return json.loads(row[0]) if row else None
    def consumed_actions(self,task_id:str)->list[str]:
        with self._connect() as connection:rows=connection.execute("SELECT action FROM consumed_actions WHERE task_id=? ORDER BY consumed_at,action",(task_id,)).fetchall()
        return [row[0] for row in rows]
    def consume_approval(self,task_id:str,action:str)->str:
        if action not in {"outcome","merge","prune-branch"}:raise PermissionError("action has no candidate consumption contract")
        connection=self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row=connection.execute("SELECT resource_json FROM approval_records_versions WHERE task_id=? AND action=? ORDER BY sequence DESC,created_at DESC LIMIT 1",(task_id,action)).fetchone()
            consumed=connection.execute("SELECT 1 FROM consumed_actions WHERE task_id=? AND action=?",(task_id,action)).fetchone()
            meta=connection.execute("SELECT * FROM workspace_meta WHERE task_id=?",(task_id,)).fetchone()
            if row is None or consumed is not None or meta is None:raise PermissionError("exact candidate approval required")
            resource=json.loads(row[0]); unsigned={key:value for key,value in resource.items() if key not in {"resource_version","requested_at"}}
            expected_version=hashlib.sha256(json.dumps(unsigned,sort_keys=True,separators=(",",":")).encode()).hexdigest()
            candidate=resource.get("reviewed_commit"); candidate_ref=resource.get("candidate_ref"); expected_ref=f"refs/triagent/reviewed/{task_id}"
            if resource.get("resource_version")!=expected_version or resource.get("repo")!=meta["repo"] or resource.get("branch")!=meta["branch"] or resource.get("base_commit")!=meta["base_commit"] or candidate!=meta["reviewed_commit"] or candidate_ref!=meta["candidate_ref"] or candidate_ref!=expected_ref:raise PermissionError("exact candidate approval required")
            work=self.runs_root/task_id/"worktree"
            if not work.exists():work=Path(meta["repo"])
            if self._git_plumbing(work,["rev-parse","--verify",candidate_ref]).decode().strip()!=candidate:raise PermissionError("exact candidate approval required")
            if self._git_plumbing(work,["cat-file","-t",candidate]).decode().strip()!="commit":raise PermissionError("exact candidate approval required")
            current=self.approval_manifest(task_id)
            if any(resource.get(key)!=value for key,value in current.items()):raise PermissionError("exact candidate approval required")
            connection.execute("INSERT INTO consumed_actions(task_id,action,resource_json,consumed_at) VALUES(?,?,?,?)",(task_id,action,row[0],datetime.now(UTC).isoformat()))
            connection.commit();return candidate
        except (OSError,subprocess.CalledProcessError,ValueError,PermissionError) as error:
            connection.rollback()
            if isinstance(error,PermissionError):raise
            raise PermissionError("exact candidate approval required") from error
        finally:connection.close()

    def delete_candidate_ref(self,task_id:str,candidate:str|None=None)->None:
        if candidate is None:candidate=self.consume_approval(task_id,"prune-branch")
        meta=self.workspace(task_id); work=self.runs_root/task_id/"worktree"
        if not work.exists():work=Path(meta["repo"])
        self._git_plumbing(work,["update-ref","-d",meta["candidate_ref"],candidate])
    def approve_requested(self, task_id: str, action: str) -> TaskRuntime:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            requests=connection.execute("SELECT resource_json FROM approval_requests_versions r WHERE task_id=? AND action=? AND NOT EXISTS(SELECT 1 FROM approval_records_versions a WHERE a.task_id=r.task_id AND a.action=r.action AND a.resource_json=r.resource_json)",(task_id,action)).fetchall()
            if len(requests)!=1: raise ValueError("approval request version is missing or ambiguous")
            request=requests[0]; self._verify_approval_manifest(task_id,action,request[0])
            now=datetime.now(UTC).isoformat(); sequence=connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM approval_records_versions WHERE task_id=? AND action=?",(task_id,action)).fetchone()[0]
            connection.execute("INSERT INTO approval_records_versions(task_id,action,resource_json,sequence,created_at) VALUES(?,?,?,?,?)",(task_id,action,request[0],sequence,now))
            actions=[r[0] for r in connection.execute("SELECT DISTINCT action FROM approval_records_versions WHERE task_id=? ORDER BY action",(task_id,)).fetchall()]
            connection.execute("UPDATE task_runtime SET approvals_json=? WHERE task_id=?",(json.dumps(actions),task_id))
        return self.runtime(task_id)
    def request_approval(self, task_id: str, action: str, resource: dict[str,str] | None = None) -> None:
        value=dict(resource or {}); canonical=json.dumps(value,sort_keys=True,separators=(",",":")); value["resource_version"]=hashlib.sha256(canonical.encode()).hexdigest(); now=datetime.now(UTC).isoformat(); value["requested_at"]=now
        with self._connect() as connection:
            sequence=connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM approval_requests_versions WHERE task_id=? AND action=?",(task_id,action)).fetchone()[0]
            connection.execute("INSERT OR REPLACE INTO approval_requests_versions(task_id,action,resource_json,sequence,created_at) VALUES(?,?,?,?,?)",(task_id,action,json.dumps(value,sort_keys=True),sequence,now))
    def outstanding_approvals(self, task_id: str) -> list[str]:
        with self._connect() as connection:
            rows=connection.execute("SELECT r.action FROM approval_requests_versions r LEFT JOIN approval_records_versions a ON a.task_id=r.task_id AND a.action=r.action AND a.resource_json=r.resource_json WHERE r.task_id=? AND a.action IS NULL ORDER BY r.action",(task_id,)).fetchall()
        return [r[0] for r in rows]

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

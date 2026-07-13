from __future__ import annotations

import json
import sqlite3
import uuid
import os
import re
import hashlib
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from pathlib import PurePosixPath

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
            connection.execute("CREATE TABLE IF NOT EXISTS approval_records(task_id TEXT NOT NULL, action TEXT NOT NULL, resource_json TEXT NOT NULL, PRIMARY KEY(task_id,action))")
            connection.execute("CREATE TABLE IF NOT EXISTS approval_requests(task_id TEXT NOT NULL, action TEXT NOT NULL, resource_json TEXT NOT NULL, PRIMARY KEY(task_id,action))")
            connection.execute("CREATE TABLE IF NOT EXISTS approval_records_versions(task_id TEXT NOT NULL, action TEXT NOT NULL, resource_json TEXT NOT NULL, PRIMARY KEY(task_id,action,resource_json))")
            connection.execute("CREATE TABLE IF NOT EXISTS approval_requests_versions(task_id TEXT NOT NULL, action TEXT NOT NULL, resource_json TEXT NOT NULL, PRIMARY KEY(task_id,action,resource_json))")
            connection.execute("CREATE TABLE IF NOT EXISTS system_attestations(task_id TEXT NOT NULL, name TEXT NOT NULL, value INTEGER NOT NULL, PRIMARY KEY(task_id,name))")
            connection.execute("CREATE TABLE IF NOT EXISTS attention_items(task_id TEXT NOT NULL, code TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(task_id,code))")
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
            if runtime["lease_owner"] is not None and (runtime["lease_owner"] != lease_owner or runtime["lease_until"] < time.time()):
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
    def complete_agent_call(self, task_id: str, call_id: str, actual_usd: float | None = None): self._finish_call(task_id, call_id, "completed", actual_usd)
    def interrupt_agent_call(self, task_id: str, call_id: str, diagnostic: str, actual_usd: float | None = None):
        with self._connect() as connection: row=connection.execute("SELECT estimated_usd FROM agent_calls WHERE id=? AND task_id=?",(call_id,task_id)).fetchone()
        if row is None: raise StateConflict("call is not pending")
        self._finish_call(task_id,call_id,"interrupted",actual_usd,diagnostic[:200])
    def execute_paid_operation(self, task_id: str, estimated_usd: float | None, operation):
        if estimated_usd is None or estimated_usd <= 0: raise BudgetExceeded("paid operation requires a positive conservative estimate")
        call=self.reserve_agent_call(task_id,estimated_usd)
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
        with self._connect() as connection: connection.execute("INSERT OR REPLACE INTO workspace_meta(task_id,repo,base_commit,branch,reviewed_commit) VALUES(?,?,?,?,NULL)",(task_id,repo,base_commit,branch))
    def workspace(self, task_id: str):
        with self._connect() as connection: return connection.execute("SELECT * FROM workspace_meta WHERE task_id=?",(task_id,)).fetchone()

    def materialize_reviewed_commit(self,task_id:str)->str:
        meta=self.workspace(task_id)
        if meta is None:
            if self.load(task_id).spec.visual_check == "required":raise ValueError("required visual artifact unavailable")
            return "fake"
        if meta["reviewed_commit"]:
            reviewed=meta["reviewed_commit"]
            check=subprocess.run(["git","cat-file","-e",f"{reviewed}^{{commit}}"],cwd=self.runs_root/task_id/"worktree",capture_output=True)
            if check.returncode!=0:raise ValueError("reviewed commit unavailable")
            return reviewed
        work=self.runs_root/task_id/"worktree"
        try:
            subprocess.run(["git","add","-A"],cwd=work,check=True,capture_output=True)
            staged=subprocess.run(["git","diff","--cached","--quiet"],cwd=work,capture_output=True).returncode
            if staged not in {0,1}:raise ValueError("cannot inspect reviewed snapshot")
            if staged==1:
                subprocess.run(["git","-c","user.name=TriAgent Controller","-c","user.email=triagent@localhost","commit","--no-gpg-sign","-m",f"triagent reviewed snapshot {task_id}"],cwd=work,check=True,capture_output=True)
            reviewed=subprocess.run(["git","rev-parse","HEAD"],cwd=work,check=True,capture_output=True,text=True).stdout.strip()
            subprocess.run(["git","cat-file","-e",f"{reviewed}^{{commit}}"],cwd=work,check=True,capture_output=True)
        except (OSError,subprocess.CalledProcessError,UnicodeError,ValueError) as error:
            raise ValueError("reviewed commit unavailable") from error
        with self._connect() as connection:connection.execute("UPDATE workspace_meta SET reviewed_commit=? WHERE task_id=? AND reviewed_commit IS NULL",(reviewed,task_id))
        persisted=self.workspace(task_id)["reviewed_commit"]
        if persisted!=reviewed:raise ValueError("reviewed commit conflict")
        return reviewed

    def approval_manifest(self, task_id: str) -> dict[str,str]:
        meta=self.workspace(task_id)
        empty=hashlib.sha256(b"").hexdigest()
        if meta is None:
            if self.load(task_id).spec.visual_check == "required":raise ValueError("required visual artifact unavailable")
            return {"repo":"fake","task_id":task_id,"branch":"fake","base_commit":"fake","reviewed_commit":"fake","reviewed_head":"fake","canonical_diff_digest":empty,"visual_artifact_digest":empty,"visual_artifact_version":empty}
        work=self.runs_root/task_id/"worktree"
        reviewed=meta["reviewed_commit"]
        if not reviewed:raise ValueError("reviewed commit unavailable")
        try:
            subprocess.run(["git","cat-file","-e",f"{reviewed}^{{commit}}"],cwd=work,check=True,capture_output=True)
            diff=subprocess.run(["git","diff","--binary",meta["base_commit"],reviewed],cwd=work,check=True,capture_output=True).stdout
        except (OSError,subprocess.CalledProcessError,UnicodeError,ValueError) as error:
            raise ValueError("approval manifest unavailable") from error
        diff_digest=hashlib.sha256(diff).hexdigest()
        artifacts=[]
        allow={".png",".jpg",".jpeg",".webp",".gif",".svg",".pdf"}
        for outcome in self.outcomes(task_id).values():
            for name in outcome.artifacts:
                path=PurePosixPath(name)
                if path.is_absolute() or "\\" in name or any(part in {"",".",".."} for part in path.parts) or path.suffix.lower() not in allow:continue
                try:
                    entry=subprocess.run(["git","ls-tree",reviewed,"--",name],cwd=work,check=True,capture_output=True,text=True).stdout.strip()
                    if not entry or not entry.startswith(("100644 blob ","100755 blob ")):continue
                    data=subprocess.run(["git","show",f"{reviewed}:{name}"],cwd=work,check=True,capture_output=True).stdout
                except (OSError,subprocess.CalledProcessError):continue
                artifacts.append({"name":name,"sha256":hashlib.sha256(data).hexdigest(),"size":len(data)})
        if self.load(task_id).spec.visual_check == "required" and not artifacts:raise ValueError("required visual artifact unavailable")
        artifact_digest=hashlib.sha256(json.dumps(sorted(artifacts,key=lambda x:x["name"]),sort_keys=True,separators=(",",":")).encode()).hexdigest()
        return {"repo":meta["repo"],"task_id":task_id,"branch":meta["branch"],"base_commit":meta["base_commit"],"reviewed_commit":reviewed,"reviewed_head":reviewed,"canonical_diff_digest":diff_digest,"visual_artifact_digest":artifact_digest,"visual_artifact_version":artifact_digest}

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

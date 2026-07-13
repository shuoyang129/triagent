from __future__ import annotations

from pathlib import Path
import uuid
import json
import base64,hashlib

from triagent.adapters.base import AgentAdapter, AgentRequest, AgentRole, AgentStatus
from triagent.domain import ReviewSeverity, RiskLevel, StageOutcome, TaskState
from triagent.store import BudgetExceeded, TaskStore
from triagent.adapters.cursor import CursorAdapter
from triagent.adapters.codex import CodexAdapter
from triagent.adapters.antigravity import AntigravityAdapter
from triagent.adapters.deepseek import DeepSeekAdapter
from triagent.adapters.fake import FakeAgent

_TRUSTED={CursorAdapter:("cursor",frozenset({AgentRole.IMPLEMENTER})),CodexAdapter:("codex",frozenset({AgentRole.VERIFIER})),AntigravityAdapter:("antigravity",frozenset({AgentRole.REVIEWER})),DeepSeekAdapter:("deepseek",frozenset({AgentRole.IMPLEMENTER}))}
def _contract(adapter):
    if type(adapter) is FakeAgent: return "fake",frozenset({AgentRole.IMPLEMENTER,AgentRole.VERIFIER,AgentRole.REVIEWER})
    try: return _TRUSTED[type(adapter)]
    except KeyError as error: raise ValueError("untrusted adapter concrete type") from error


BLOCKED_STATES = {
    TaskState.APPROVAL,
    TaskState.WAITING_FOR_USER,
    TaskState.WAITING_FOR_VISUAL_APPROVAL,
    TaskState.WAITING_FOR_GUI,
    TaskState.PAUSED_BUDGET,
    TaskState.FAILED_RECOVERABLE,
    TaskState.FAILED_FINAL,
}


class Orchestrator:
    def __init__(
        self,
        store: TaskStore,
        implementer: AgentAdapter,
        verifier: AgentAdapter,
        reviewer: AgentAdapter,
    ) -> None:
        self.store = store
        self.implementer = implementer
        self.verifier = verifier
        self.reviewer = reviewer
        self._lease_owner: str | None = None

    def _request(self, task_id: str, role: AgentRole, schema: str, adapter: AgentAdapter) -> AgentRequest:
        run_dir = self.store.runs_root / task_id
        return AgentRequest(
            role=role,
            agent_identity=_contract(adapter)[0],
            task_file=run_dir / "task.yaml",
            handoff_file=(run_dir / "handoff.json") if role in {AgentRole.VERIFIER, AgentRole.REVIEWER} else None,
            workdir=run_dir / "worktree",
            output_schema=schema,
            timeout_seconds=300,
        )

    def _call(self, task_id: str, state: TaskState, adapter: AgentAdapter, request: AgentRequest):
        task = self.store.load(task_id)
        identity,roles=_contract(adapter)
        if request.role not in roles or request.agent_identity != identity:
            raise ValueError("adapter identity/role mismatch")
        if self._lease_owner: self.store.renew_lease(task_id, self._lease_owner, 600)
        estimate=adapter.estimate_cost(request)
        if estimate.estimated_usd == 0 and not estimate.zero_cost_enforced:
            raise BudgetExceeded("zero estimate is not enforced")
        try:
            call_id = self.store.reserve_agent_call(task_id, estimated_usd=estimate.estimated_usd, lease_owner=self._lease_owner)
        except BudgetExceeded:
            self.store.transition(task_id, state, TaskState.PAUSED_BUDGET, "agent-call-budget-exhausted")
            return None
        try: result = adapter.run(request)
        except BaseException as error:
            self.store.interrupt_agent_call(task_id, call_id, type(error).__name__); raise
        if self._lease_owner: self.store.renew_lease(task_id, self._lease_owner, 600)
        actual = 0.0 if estimate.zero_cost_enforced else result.actual_usd
        try: self.store.complete_agent_call(task_id, call_id, actual)
        except BudgetExceeded:
            self.store.finalize_overrun_and_pause(task_id,call_id,actual,state); return None
        if result.status is not AgentStatus.SUCCEEDED:
            diagnostic=result.data.get("diagnostic_code")
            if diagnostic == "transport-cleanup-failed":self.store.record_attention(task_id,diagnostic)
            event=diagnostic if isinstance(diagnostic,str) else result.status.value
            self.store.transition(task_id, state, TaskState.FAILED_RECOVERABLE, event)
            return None
        if result.data.get("status") == "failed":
            stage={AgentRole.IMPLEMENTER:"implement",AgentRole.VERIFIER:"verify",AgentRole.REVIEWER:"review"}[request.role]
            self.store.record_outcome(task_id,self._outcome(stage,result,status="failed"))
            self.store.transition(task_id,state,TaskState.FAILED_RECOVERABLE,"canonical-stage-failed")
            return None
        return result

    def _write_handoff(self, task_id: str, *, tests: list[str] | None = None) -> Path:
        task=self.store.load(task_id); run=self.store.runs_root/task_id; work=run/"worktree"; meta=self.store.workspace(task_id)
        if meta is None and all(type(a) is FakeAgent for a in (self.implementer,self.verifier,self.reviewer)):
            diff=""
        else:
          try:
            import subprocess
            base=meta["base_commit"] if meta else "HEAD"
            diff=subprocess.run(["git","diff","--binary",base],cwd=work,check=True,capture_output=True,text=True).stdout
            untracked=subprocess.run(["git","ls-files","--others","--exclude-standard"],cwd=work,check=True,capture_output=True,text=True).stdout.splitlines()
            for name in untracked:
                data=(work/name).read_bytes(); record={"path":name,"size":len(data),"sha256":hashlib.sha256(data).hexdigest(),"base64":base64.b64encode(data).decode("ascii")}
                diff += "\nUNTRACKED_BINARY_JSON "+json.dumps(record,sort_keys=True,separators=(",",":"))+"\n"
          except Exception as error: raise RuntimeError("canonical handoff generation failed") from error
        payload={"task_spec":task.spec.model_dump(mode="json"),"final_diff":diff,"tests":tests or [],"artifacts":[],"rollback":"preserve task branch; remove worktree only after approval","completed":["implementation"]}
        path=run/"handoff.json"; path.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8"); return path

    @staticmethod
    def _outcome(stage: str, result, *, status: str = "passed") -> StageOutcome:
        data=result.data
        allowed={"status","summary_code","evidence","artifacts","findings","tests","actual_usd"}
        if set(data)-allowed: raise ValueError("adapter result contains non-allowlisted fields")
        evidence=data.get("evidence", []); artifacts=data.get("artifacts", []); findings=data.get("findings",[])
        if not isinstance(evidence,list) or not all(isinstance(x,str) for x in evidence): raise ValueError("invalid evidence schema")
        if not isinstance(artifacts,list) or not all(isinstance(x,str) for x in artifacts): raise ValueError("invalid artifact schema")
        normalized=[]
        if not isinstance(findings,list): raise ValueError("invalid findings schema")
        for item in findings:
            if not isinstance(item,dict): raise ValueError("invalid finding")
            normalized.append({"severity":item.get("severity"),"code":item.get("code","finding"),"message":item.get("message")})
        code={"implement":"completed","verify":"verified","review":"clean"}.get(stage,"unknown") if status=="passed" else "requires-repair"
        return StageOutcome(stage=stage,status=status,summary=code,evidence=evidence,artifacts=artifacts,findings=normalized)

    def advance(self, task_id: str) -> TaskState:
        if self._lease_owner: self.store.renew_lease(task_id, self._lease_owner, 600)
        task = self.store.load(task_id)
        state = task.state
        if state in BLOCKED_STATES:
            return state
        if state is TaskState.SPEC:
            return self.store.transition(task_id, state, TaskState.IMPLEMENT, "spec-accepted").state
        if state in {TaskState.IMPLEMENT, TaskState.REPAIR}:
            result = self._call(
                task_id,
                state,
                self.implementer,
                self._request(task_id, AgentRole.IMPLEMENTER, "implementation-result-v1", self.implementer),
            )
            if result is None:
                return self.store.load(task_id).state
            self.store.record_outcome(task_id, self._outcome("implement", result)); self._write_handoff(task_id)
            return self.store.transition(task_id, state, TaskState.VERIFY, "implementation-complete").state
        if state is TaskState.VERIFY:
            result = self._call(
                task_id,
                state,
                self.verifier,
                self._request(task_id, AgentRole.VERIFIER, "verification-result-v1", self.verifier),
            )
            if result is None:
                return self.store.load(task_id).state
            self.store.record_outcome(task_id, self._outcome("verify", result)); self._write_handoff(task_id,tests=self.store.outcomes(task_id)["verify"].evidence)
            return self.store.transition(task_id, state, TaskState.REVIEW, "verification-complete").state
        if state is TaskState.REVIEW:
            result = self._call(
                task_id,
                state,
                self.reviewer,
                self._request(task_id, AgentRole.REVIEWER, "review-result-v1", self.reviewer),
            )
            if result is None:
                return self.store.load(task_id).state
            severities = {
                ReviewSeverity(item["severity"])
                for item in result.data.get("findings", [])
                if isinstance(item, dict) and item.get("severity") in ReviewSeverity
            }
            if severities & {ReviewSeverity.BLOCKER, ReviewSeverity.MAJOR}:
                self.store.record_outcome(task_id, self._outcome("review", result, status="failed"))
                runtime = self.store.runtime(task_id)
                repair_limit = 3 if task.spec.risk in {RiskLevel.HIGH, RiskLevel.ROBOT_SAFETY} else 2
                if runtime.repair_attempts >= repair_limit:
                    return self.store.transition(task_id, state, TaskState.FAILED_FINAL, "repair-limit-reached").state
                self.store.increment_repair_attempts(task_id)
                return self.store.transition(task_id, state, TaskState.REPAIR, "review-requires-repair").state
            self.store.record_outcome(task_id, self._outcome("review", result))
            target = (
                TaskState.WAITING_FOR_VISUAL_APPROVAL
                if task.spec.visual_check == "required"
                else TaskState.APPROVAL
            )
            self.store.materialize_reviewed_commit(task_id)
            manifest=self.store.approval_manifest(task_id)
            if target is TaskState.WAITING_FOR_VISUAL_APPROVAL: self.store.request_approval(task_id,"visual",manifest)
            else:
                self.store.request_approval(task_id,"outcome",manifest)
                meta=self.store.workspace(task_id)
                if meta: self.store.request_approval(task_id,"merge",manifest)
            return self.store.transition(task_id, state, target, "review-passed").state
        raise RuntimeError(f"unsupported state: {state.value}")

    def run_until_blocked(self, task_id: str) -> TaskState:
        owner = str(uuid.uuid4()); self.store.acquire_lease(task_id, owner, 600); self._lease_owner=owner
        try:
            for _ in range(100):
                state = self.advance(task_id)
                if state in BLOCKED_STATES:
                    return state
            raise RuntimeError("workflow did not reach a blocked state within 100 transitions")
        except Exception as error:
            task=self.store.load(task_id)
            if task.state not in BLOCKED_STATES:
                self.store.record_outcome(task_id,StageOutcome(stage="setup",status="failed",summary="execution-failed",diagnostic=type(error).__name__))
                self.store.transition(task_id,task.state,TaskState.FAILED_RECOVERABLE,"execution-failed")
            raise
        finally: self.store.release_lease(task_id, owner); self._lease_owner=None

    def approve(self, task_id: str, action: str) -> TaskState:
        task = self.store.load(task_id)
        if task.state is TaskState.WAITING_FOR_VISUAL_APPROVAL and action == "visual":
            self.store.approve_and_transition(task_id, action, TaskState.WAITING_FOR_VISUAL_APPROVAL, TaskState.APPROVAL)
            return TaskState.APPROVAL
        if task.state is TaskState.APPROVAL and action in {"outcome", "merge", "deploy", "destructive", "prune-branch", "live", "billing"}:
            self.store.approve_requested(task_id, action)
            return task.state
        raise ValueError(f"approval {action!r} is invalid for state {task.state.value}")

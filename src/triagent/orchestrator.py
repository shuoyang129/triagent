from __future__ import annotations

import os
from pathlib import Path
import uuid
import json
import hashlib

from triagent.adapters.base import AgentAdapter, AgentRequest, AgentRole, AgentStatus
from triagent.domain import ReviewSeverity, RiskLevel, StageOutcome, TaskState
from triagent.store import BudgetExceeded, LeaseConflict, TaskStore
from triagent.adapters.cursor import CursorAdapter
from triagent.adapters.codex import CodexAdapter
from triagent.adapters.antigravity import AntigravityAdapter
from triagent.adapters.deepseek import DeepSeekAdapter
from triagent.adapters.fake import FakeAgent
from triagent.completion import CompletionControl, DurableResult

_TRUSTED={CursorAdapter:("cursor",frozenset({AgentRole.IMPLEMENTER})),CodexAdapter:("codex",frozenset({AgentRole.VERIFIER})),AntigravityAdapter:("antigravity",frozenset({AgentRole.REVIEWER})),DeepSeekAdapter:("deepseek",frozenset({AgentRole.IMPLEMENTER}))}
_SAFE_DIAGNOSTICS=frozenset({
    "agy-empty-output",
    "canonical-output-invalid",
    "cursor-envelope-invalid",
    "cursor-result-non-json",
    "deepseek-api-failed",
    "deepseek-authentication-failed",
    "deepseek-connection-failed",
    "deepseek-insufficient-balance",
    "deepseek-permission-denied",
    "deepseek-rate-limited",
    "deepseek-request-invalid",
    "deepseek-service-unavailable",
    "deepseek-timeout",
    "deepseek-local-failure",
    "deepseek-patch-invalid",
    "json-malformed",
    "json-non-object",
    "transport-acl-setup-failed",
    "transport-acl-verification-failed",
    "transport-cleanup-failed",
})
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

_BASE_LEASE_SECONDS = 600
_AGENT_LEASE_SAFETY_SECONDS = 60


class Orchestrator:
    def __init__(
        self,
        store: TaskStore,
        implementer: AgentAdapter,
        verifier: AgentAdapter,
        reviewer: AgentAdapter,
        profile_digest: str | None = None,
        expected_recovery_checkpoint: tuple[str, int] | None = None,
        implementer_probe_estimated_usd: float | None = None,
    ) -> None:
        self.store = store
        self.implementer = implementer
        self.verifier = verifier
        self.reviewer = reviewer
        self.profile_digest = profile_digest
        self.expected_recovery_checkpoint = expected_recovery_checkpoint
        self.implementer_probe_estimated_usd = implementer_probe_estimated_usd
        self._lease_owner: str | None = None

    def consume_fake_durable_completion(
        self,
        control: CompletionControl,
        *,
        expected_candidate_commit: str | None = None,
    ) -> DurableResult:
        """Persist and receipt a validated fake result without a second call.

        This is deliberately a narrow M6 bridge, rather than a replacement for
        ``_call``: no real adapter can enter it and no provider is invoked here.
        The store ledger is committed before the filesystem receipt.  If the
        controller dies in that gap, retrying the same durable record is
        idempotent and only writes the missing receipt.
        """
        if not all(type(adapter) is FakeAgent for adapter in (
            self.implementer, self.verifier, self.reviewer,
        )):
            raise ValueError("durable completion bridge is fake-only")
        if self._lease_owner is None:
            raise LeaseConflict("controller lease is required for durable completion")
        binding = control.binding
        if binding.provider != "fake":
            raise ValueError("durable completion binding is not fake")

        def persist() -> None:
            result = control.read_result(
                expected_candidate_commit=expected_candidate_commit
            )
            self.store.record_durable_completion(
                task_id=binding.task_id,
                call_id=binding.call_id,
                provider=binding.provider,
                role=binding.role,
                result_digest=result.result_digest,
                candidate_commit=result.candidate_commit,
                outcome=dict(result.outcome),
                lease_owner=self._lease_owner,
            )

        return control.consume_once(
            expected_candidate_commit=expected_candidate_commit,
            before_receipt=persist,
        )

    def _execution_provenance(self) -> dict[str, str]:
        implementer = _contract(self.implementer)[0]
        verifier = _contract(self.verifier)[0]
        reviewer = _contract(self.reviewer)[0]
        identities = (implementer, verifier, reviewer)
        if identities == ("fake", "fake", "fake"):
            mode = "simulation"
            digest = self.profile_digest or "fake-v1"
        elif implementer in {"cursor", "deepseek"} and identities[1:] == ("codex", "antigravity"):
            mode = "live"
            digest = self.profile_digest or hashlib.sha256(
                ("direct:" + ":".join(identities)).encode("utf-8")
            ).hexdigest()
        else:
            raise ValueError("adapter pipeline has no valid execution provenance")
        return {
            "mode": mode,
            "implementer": implementer,
            "verifier": verifier,
            "reviewer": reviewer,
            "profile_digest": digest,
        }

    @staticmethod
    def _failed_stage(role: AgentRole) -> str:
        return {
            AgentRole.IMPLEMENTER: "implement",
            AgentRole.VERIFIER: "verify",
            AgentRole.REVIEWER: "review",
        }[role]

    def _record_transport_failure(self, task_id: str, role: AgentRole, diagnostic: str) -> None:
        self.store.record_outcome(
            task_id,
            StageOutcome(
                stage=self._failed_stage(role),
                status="failed",
                summary="requires-repair",
                diagnostic=diagnostic,
            ),
        )

    def _request(self, task_id: str, role: AgentRole, schema: str, adapter: AgentAdapter) -> AgentRequest:
        run_dir = self.store.runs_root / task_id
        raw_timeout = os.environ.get("TRIAGENT_AGENT_TIMEOUT_SECONDS", "900")
        try:
            timeout_seconds = int(raw_timeout)
        except ValueError as exc:
            raise ValueError("TRIAGENT_AGENT_TIMEOUT_SECONDS must be an integer") from exc
        if not 60 <= timeout_seconds <= 3600:
            raise ValueError("TRIAGENT_AGENT_TIMEOUT_SECONDS must be between 60 and 3600")
        return AgentRequest(
            role=role,
            agent_identity=_contract(adapter)[0],
            task_file=run_dir / "task.yaml",
            handoff_file=(run_dir / "handoff.json") if role in {AgentRole.VERIFIER, AgentRole.REVIEWER} else None,
            workdir=run_dir / "worktree",
            output_schema=schema,
            timeout_seconds=timeout_seconds,
        )

    def _call(
        self,
        task_id: str,
        state: TaskState,
        adapter: AgentAdapter,
        request: AgentRequest,
        *,
        allow_review_unavailable_retry: bool = True,
    ):
        task = self.store.load(task_id)
        identity,roles=_contract(adapter)
        if request.role not in roles or request.agent_identity != identity:
            raise ValueError("adapter identity/role mismatch")
        lease_seconds = max(
            _BASE_LEASE_SECONDS,
            request.timeout_seconds + _AGENT_LEASE_SAFETY_SECONDS,
        )
        if self._lease_owner:
            self.store.renew_lease(task_id, self._lease_owner, lease_seconds)
        estimate=adapter.estimate_cost(request)
        if estimate.estimated_usd == 0 and not estimate.zero_cost_enforced:
            raise BudgetExceeded("zero estimate is not enforced")
        try:
            call_id = self.store.reserve_agent_call(task_id, estimated_usd=estimate.estimated_usd, lease_owner=self._lease_owner)
        except BudgetExceeded:
            self.store.transition(task_id, state, TaskState.PAUSED_BUDGET, "agent-call-budget-exhausted")
            return None
        try: result = adapter.run(request)
        except BaseException:
            diagnostic = "adapter-exception"
            self.store.interrupt_agent_call(task_id, call_id, diagnostic)
            self._record_transport_failure(task_id, request.role, diagnostic)
            self.store.transition_recoverable(
                task_id, state, self._failed_stage(request.role), diagnostic
            )
            return None
        if self._lease_owner:
            self.store.renew_lease(task_id, self._lease_owner, lease_seconds)
        actual = 0.0 if estimate.zero_cost_enforced else result.actual_usd
        raw_diagnostic=result.data.get("diagnostic_code")
        diagnostic=raw_diagnostic if isinstance(raw_diagnostic,str) and raw_diagnostic in _SAFE_DIAGNOSTICS else (result.status.value if result.status is not AgentStatus.SUCCEEDED else "")
        try: self.store.complete_agent_call(task_id, call_id, actual, diagnostic)
        except BudgetExceeded:
            self.store.finalize_overrun_and_pause(task_id,call_id,actual,state); return None
        if result.status is not AgentStatus.SUCCEEDED:
            if (
                allow_review_unavailable_retry
                and request.role is AgentRole.REVIEWER
                and result.status is AgentStatus.UNAVAILABLE
            ):
                return self._call(
                    task_id,
                    state,
                    adapter,
                    request,
                    allow_review_unavailable_retry=False,
                )
            if diagnostic == "transport-cleanup-failed":self.store.record_attention(task_id,diagnostic)
            self._record_transport_failure(task_id, request.role, diagnostic)
            self.store.transition_recoverable(
                task_id, state, self._failed_stage(request.role), diagnostic
            )
            return None
        if result.data.get("status") == "failed":
            stage=self._failed_stage(request.role)
            self.store.record_outcome(task_id,self._outcome(stage,result,status="failed"))
            self.store.transition_recoverable(
                task_id, state, stage, "canonical-stage-failed"
            )
            return None
        return result

    def _write_handoff(self, task_id: str, *, tests: list[str] | None = None) -> Path:
        task=self.store.load(task_id); run=self.store.runs_root/task_id; work=run/"worktree"; meta=self.store.workspace(task_id)
        implement_outcome=self.store.outcomes(task_id).get("implement")
        artifacts=list(implement_outcome.artifacts) if implement_outcome is not None else []
        if meta is None and all(type(a) is FakeAgent for a in (self.implementer,self.verifier,self.reviewer)):
            diff=""
        else:
          try:
            if meta is None or not meta["reviewed_commit"]:raise ValueError("candidate missing")
            diff=self.store._git_plumbing(work,["diff","--binary",meta["base_commit"],meta["reviewed_commit"]]).decode("utf-8","replace")
          except Exception as error: raise RuntimeError("canonical handoff generation failed") from error
        payload={"task_spec":task.spec.model_dump(mode="json"),"final_diff":diff,"tests":tests or [],"artifacts":artifacts,"rollback":"preserve task branch; remove worktree only after approval","completed":["implementation"]}
        path=run/"handoff.json"; path.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8"); return path

    @staticmethod
    def _outcome(stage: str, result, *, status: str = "passed") -> StageOutcome:
        data=result.data
        allowed={"status","summary_code","evidence","artifacts","findings","tests","actual_usd","changed_paths"}
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
            if state is TaskState.REPAIR and self.store.workspace(task_id) is not None:self.store.restore_candidate_worktree(task_id)
            result = self._call(
                task_id,
                state,
                self.implementer,
                self._request(task_id, AgentRole.IMPLEMENTER, "implementation-result-v1", self.implementer),
            )
            if result is None:
                return self.store.load(task_id).state
            cursor_implementation = type(self.implementer) is CursorAdapter
            try:
                self.store.materialize_reviewed_commit(
                    task_id,
                    None if cursor_implementation else result.data.get("changed_paths"),
                    require_changes=cursor_implementation,
                )
                if self.store.workspace(task_id) is not None:
                    self.store.restore_candidate_worktree(task_id)
            except ValueError as error:
                if cursor_implementation and str(error) == "candidate manifest rejected: no changes":
                    diagnostic = "cursor-no-worktree-change"
                    self.store.record_outcome(
                        task_id,
                        StageOutcome(
                            stage="implement",
                            status="failed",
                            summary="requires-repair",
                            diagnostic=diagnostic,
                        ),
                    )
                    return self.store.transition_recoverable(
                        task_id,
                        state,
                        "implement",
                        diagnostic,
                    ).state
                raise
            self.store.record_outcome(task_id, self._outcome("implement", result))
            self._write_handoff(task_id)
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
            self.store.record_outcome(task_id, self._outcome("verify", result))
            if self.store.workspace(task_id) is not None:self.store.restore_candidate_worktree(task_id)
            self._write_handoff(task_id,tests=self.store.outcomes(task_id)["verify"].evidence)
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
                if isinstance(item, dict)
                and item.get("severity") in {severity.value for severity in ReviewSeverity}
            }
            if severities & {ReviewSeverity.BLOCKER, ReviewSeverity.MAJOR}:
                self.store.record_outcome(task_id, self._outcome("review", result, status="failed"))
                runtime = self.store.runtime(task_id)
                repair_limit = self._repair_limit(task.spec.risk)
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
            self.store.record_execution_provenance(task_id, **self._execution_provenance())
            for _ in range(100):
                state = self.advance(task_id)
                if state in BLOCKED_STATES:
                    return state
            raise RuntimeError("workflow did not reach a blocked state within 100 transitions")
        except Exception as error:
            task=self.store.load(task_id)
            if task.state not in BLOCKED_STATES:
                self.store.record_outcome(task_id,StageOutcome(stage="setup",status="failed",summary="execution-failed",diagnostic=type(error).__name__))
                stage = {
                    TaskState.IMPLEMENT: "implement", TaskState.REPAIR: "implement",
                    TaskState.VERIFY: "verify", TaskState.REVIEW: "review",
                }.get(task.state)
                if stage is None:
                    self.store.transition(task_id,task.state,TaskState.FAILED_RECOVERABLE,"execution-failed")
                else:
                    self.store.transition_recoverable(
                        task_id, task.state, stage, "execution-failed"
                    )
            raise
        finally: self.store.release_lease(task_id, owner); self._lease_owner=None

    @staticmethod
    def _repair_limit(risk: RiskLevel) -> int:
        default = 3 if risk in {RiskLevel.HIGH, RiskLevel.ROBOT_SAFETY} else 2
        raw = os.environ.get("TRIAGENT_REPAIR_ATTEMPT_LIMIT")
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            return default
        return value if default <= value <= 20 else default

    def resume_until_blocked(self, task_id: str) -> TaskState:
        owner = str(uuid.uuid4())
        recovery_accepted = False
        self.store.acquire_lease(task_id, owner, 600)
        self._lease_owner = owner
        try:
            task = self.store.load(task_id)
            if task.state is not TaskState.FAILED_RECOVERABLE:
                raise ValueError("only FAILED_RECOVERABLE tasks can be resumed")
            persisted_provenance = self.store.execution_provenance(task_id)
            if persisted_provenance is None:
                raise ValueError("execution provenance is missing")
            if persisted_provenance != self._execution_provenance():
                raise ValueError("execution provenance mismatch")
            checkpoint = self.store.recovery_checkpoint(task_id)
            if (
                checkpoint is None
                or checkpoint.get("stage") not in {"implement", "verify", "review"}
                or not isinstance(checkpoint.get("sequence"), int)
                or checkpoint["sequence"] < 1
            ):
                raise ValueError("recovery checkpoint is missing or invalid")
            current_checkpoint = (checkpoint["stage"], checkpoint["sequence"])
            if (
                self.expected_recovery_checkpoint is not None
                and current_checkpoint != self.expected_recovery_checkpoint
            ):
                raise ValueError("recovery checkpoint changed")
            stage = checkpoint["stage"]
            target = {
                "implement": TaskState.IMPLEMENT,
                "verify": TaskState.VERIFY,
                "review": TaskState.REVIEW,
            }[stage]
            role, schema, adapter = {
                "implement": (AgentRole.IMPLEMENTER, "implementation-result-v1", self.implementer),
                "verify": (AgentRole.VERIFIER, "verification-result-v1", self.verifier),
                "review": (AgentRole.REVIEWER, "review-result-v1", self.reviewer),
            }[stage]
            runtime = self.store.runtime(task_id)
            if runtime.repair_attempts >= self._repair_limit(task.spec.risk):
                raise ValueError("repair-attempt limit exhausted")
            request = self._request(task_id, role, schema, adapter)
            estimate = adapter.estimate_cost(request)
            if estimate.estimated_usd == 0 and not estimate.zero_cost_enforced:
                raise BudgetExceeded("zero estimate is not enforced")
            self.store.assert_agent_call_available(
                task_id, estimate.estimated_usd, lease_owner=owner
            )
            if stage in {"verify", "review"}:
                self.store.restore_candidate_worktree(task_id)
            if stage == "implement" and type(self.implementer) is DeepSeekAdapter:
                self.store.assert_paid_operations_available(
                    task_id,
                    (
                        self.implementer_probe_estimated_usd,
                        estimate.estimated_usd,
                    ),
                    lease_owner=owner,
                )
                capability = self.store.execute_paid_operation(
                    task_id,
                    self.implementer_probe_estimated_usd,
                    self.implementer.capabilities,
                    lease_owner=owner,
                )
                if not capability.available or capability.ready is not True:
                    diagnostic = getattr(capability, "diagnostic_code", None) or "deepseek-api-failed"
                    self.store.record_outcome(
                        task_id,
                        StageOutcome(
                            stage="implement",
                            status="failed",
                            summary="requires-repair",
                            diagnostic=diagnostic,
                        ),
                    )
                    raise ValueError("persisted DeepSeek implementer is not ready")
            self.store.accept_recovery(
                task_id,
                stage=stage,
                sequence=checkpoint["sequence"],
                target=target,
                lease_owner=owner,
            )
            recovery_accepted = True
            for _ in range(100):
                state = self.advance(task_id)
                if state in BLOCKED_STATES:
                    return state
            raise RuntimeError("workflow did not reach a blocked state within 100 transitions")
        except Exception as error:
            if (
                isinstance(error, (ValueError, BudgetExceeded, LeaseConflict))
                and not recovery_accepted
            ):
                raise
            current = self.store.load(task_id)
            if current.state not in BLOCKED_STATES:
                self.store.record_outcome(
                    task_id,
                    StageOutcome(
                        stage="setup",
                        status="failed",
                        summary="execution-failed",
                        diagnostic=type(error).__name__,
                    ),
                )
                stage = {
                    TaskState.IMPLEMENT: "implement", TaskState.REPAIR: "implement",
                    TaskState.VERIFY: "verify", TaskState.REVIEW: "review",
                }.get(current.state)
                if stage is None:
                    self.store.transition(
                        task_id, current.state, TaskState.FAILED_RECOVERABLE,
                        "execution-failed",
                    )
                else:
                    self.store.transition_recoverable(
                        task_id, current.state, stage, "execution-failed"
                    )
            raise
        finally:
            self.store.release_lease(task_id, owner)
            self._lease_owner = None

    def approve(self, task_id: str, action: str) -> TaskState:
        task = self.store.load(task_id)
        if task.state is TaskState.WAITING_FOR_VISUAL_APPROVAL and action == "visual":
            self.store.approve_and_transition(task_id, action, TaskState.WAITING_FOR_VISUAL_APPROVAL, TaskState.APPROVAL)
            return TaskState.APPROVAL
        if task.state is TaskState.APPROVAL and action in {"outcome", "merge", "deploy", "destructive", "prune-branch", "live", "billing"}:
            self.store.approve_requested(task_id, action)
            return task.state
        raise ValueError(f"approval {action!r} is invalid for state {task.state.value}")

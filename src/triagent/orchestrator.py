from __future__ import annotations

from pathlib import Path

from triagent.adapters.base import AgentAdapter, AgentRequest, AgentRole, AgentStatus
from triagent.domain import ReviewSeverity, RiskLevel, TaskState
from triagent.store import TaskStore


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

    def _request(self, task_id: str, role: AgentRole, schema: str) -> AgentRequest:
        run_dir = self.store.runs_root / task_id
        return AgentRequest(
            role=role,
            task_file=run_dir / "task.yaml",
            workdir=run_dir / "worktree",
            output_schema=schema,
            timeout_seconds=300,
        )

    def _call(self, task_id: str, state: TaskState, adapter: AgentAdapter, request: AgentRequest):
        task = self.store.load(task_id)
        runtime = self.store.runtime(task_id)
        if runtime.agent_calls >= task.spec.budget.max_agent_calls:
            self.store.transition(task_id, state, TaskState.PAUSED_BUDGET, "agent-call-budget-exhausted")
            return None
        result = adapter.run(request)
        self.store.increment_agent_calls(task_id)
        if result.status is not AgentStatus.SUCCEEDED:
            self.store.transition(task_id, state, TaskState.FAILED_RECOVERABLE, result.status.value)
            return None
        return result

    def advance(self, task_id: str) -> TaskState:
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
                self._request(task_id, AgentRole.IMPLEMENTER, "implementation-result-v1"),
            )
            if result is None:
                return self.store.load(task_id).state
            return self.store.transition(task_id, state, TaskState.VERIFY, "implementation-complete").state
        if state is TaskState.VERIFY:
            result = self._call(
                task_id,
                state,
                self.verifier,
                self._request(task_id, AgentRole.VERIFIER, "verification-result-v1"),
            )
            if result is None:
                return self.store.load(task_id).state
            return self.store.transition(task_id, state, TaskState.REVIEW, "verification-complete").state
        if state is TaskState.REVIEW:
            result = self._call(
                task_id,
                state,
                self.reviewer,
                self._request(task_id, AgentRole.REVIEWER, "review-result-v1"),
            )
            if result is None:
                return self.store.load(task_id).state
            severities = {
                ReviewSeverity(item["severity"])
                for item in result.data.get("findings", [])
                if isinstance(item, dict) and item.get("severity") in ReviewSeverity
            }
            if severities & {ReviewSeverity.BLOCKER, ReviewSeverity.MAJOR}:
                runtime = self.store.runtime(task_id)
                repair_limit = 3 if task.spec.risk in {RiskLevel.HIGH, RiskLevel.ROBOT_SAFETY} else 2
                if runtime.repair_attempts >= repair_limit:
                    return self.store.transition(task_id, state, TaskState.FAILED_FINAL, "repair-limit-reached").state
                self.store.increment_repair_attempts(task_id)
                return self.store.transition(task_id, state, TaskState.REPAIR, "review-requires-repair").state
            target = (
                TaskState.WAITING_FOR_VISUAL_APPROVAL
                if task.spec.visual_check == "required"
                else TaskState.APPROVAL
            )
            return self.store.transition(task_id, state, target, "review-passed").state
        raise RuntimeError(f"unsupported state: {state.value}")

    def run_until_blocked(self, task_id: str) -> TaskState:
        for _ in range(100):
            state = self.advance(task_id)
            if state in BLOCKED_STATES:
                return state
        raise RuntimeError("workflow did not reach a blocked state within 100 transitions")

    def approve(self, task_id: str, action: str) -> TaskState:
        task = self.store.load(task_id)
        if task.state is TaskState.WAITING_FOR_VISUAL_APPROVAL and action == "visual":
            self.store.record_approval(task_id, action)
            return self.store.transition(
                task_id,
                TaskState.WAITING_FOR_VISUAL_APPROVAL,
                TaskState.APPROVAL,
                "visual-approved",
            ).state
        raise ValueError(f"approval {action!r} is invalid for state {task.state.value}")

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from triagent.adapters.base import AgentAdapter, AgentRole
from triagent.domain import ReviewSeverity, StageOutcome, TaskState
from triagent.orchestrator import BLOCKED_STATES, Orchestrator


def source_snapshot(repo: Path) -> dict[str, str]:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout
    status = git("status", "--porcelain=v2", "--", ".")
    return {
        "repo": str(repo.resolve()),
        "head": git("rev-parse", "HEAD").strip(),
        "status_sha256": hashlib.sha256(status.encode()).hexdigest(),
    }


def save_source_snapshot(run_dir: Path, snapshot: dict[str, str]) -> None:
    (run_dir / "source-snapshot.json").write_text(
        json.dumps(snapshot, sort_keys=True), encoding="utf-8"
    )


def load_source_snapshot(run_dir: Path) -> dict[str, str]:
    value = json.loads((run_dir / "source-snapshot.json").read_text(encoding="utf-8"))
    required = {"repo", "head", "status_sha256"}
    if not isinstance(value, dict) or set(value) != required or not all(isinstance(value[key], str) and value[key] for key in required):
        raise ValueError("read-only source snapshot is invalid")
    return value


class ReadOnlyOrchestrator(Orchestrator):
    """Two-stage inspection that never creates a task worktree or candidate."""

    def __init__(self, *args, source: Path, source_before: dict[str, str], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.source = source.resolve()
        self.source_before = source_before

    def _execution_provenance(self) -> dict[str, str]:
        return {
            "mode": "read-only", "implementer": "none", "verifier": "codex",
            "reviewer": "antigravity", "profile_digest": self.profile_digest or "read-only-v1",
        }

    def _request(self, task_id: str, role: AgentRole, schema: str, adapter: AgentAdapter):
        request = super()._request(task_id, role, schema, adapter)
        return request.model_copy(update={"workdir": self.source, "read_only": True})

    def _assert_unchanged(self) -> None:
        if source_snapshot(self.source) != self.source_before:
            raise RuntimeError("read-only-source-changed")

    def _write_readonly_handoff(self, task_id: str, *, tests: list[str] | None = None) -> None:
        task = self.store.load(task_id)
        outcome = self.store.outcomes(task_id).get("verify")
        payload = {
            "task_spec": task.spec.model_dump(mode="json"), "final_diff": "",
            "tests": tests or [], "artifacts": list(outcome.artifacts) if outcome else [],
            "rollback": "no candidate or source mutation; v2 report only",
            "completed": ["read-only inspection"], "source_snapshot": self.source_before,
        }
        (self.store.runs_root / task_id / "handoff.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def advance(self, task_id: str) -> TaskState:
        if self._lease_owner:
            self.store.renew_lease(task_id, self._lease_owner, 600)
        task = self.store.load(task_id)
        state = task.state
        if state in BLOCKED_STATES:
            return state
        if state is TaskState.SPEC:
            self._assert_unchanged()
            self._write_readonly_handoff(task_id)
            return self.store.transition(task_id, state, TaskState.VERIFY, "read-only-spec-accepted").state
        if state is TaskState.VERIFY:
            result = self._call(task_id, state, self.verifier, self._request(task_id, AgentRole.VERIFIER, "verification-result-v1", self.verifier), accept_completed_failure=True)
            if result is None:
                return self.store.load(task_id).state
            self._assert_unchanged()
            self.store.record_outcome(task_id, self._outcome("verify", self._completed_inspection_result(result)))
            self._write_readonly_handoff(task_id, tests=self.store.outcomes(task_id)["verify"].evidence)
            return self.store.transition(task_id, state, TaskState.REVIEW, "read-only-verification-complete").state
        if state is TaskState.REVIEW:
            result = self._call(task_id, state, self.reviewer, self._request(task_id, AgentRole.REVIEWER, "review-result-v1", self.reviewer), accept_completed_failure=True)
            if result is None:
                return self.store.load(task_id).state
            self._assert_unchanged()
            result = self._completed_inspection_result(result)
            severities = {ReviewSeverity(item["severity"]) for item in result.data.get("findings", []) if isinstance(item, dict) and item.get("severity") in {x.value for x in ReviewSeverity}}
            if severities & {ReviewSeverity.BLOCKER, ReviewSeverity.MAJOR}:
                self.store.record_outcome(task_id, self._outcome("review", result, status="failed"))
                return self.store.transition(task_id, state, TaskState.FAILED_FINAL, "read-only-review-findings").state
            self.store.record_outcome(task_id, self._outcome("review", result))
            resource = {"mode": "read-only", **self.source_before, "canonical_diff_digest": hashlib.sha256(b"").hexdigest()}
            self.store.request_approval(task_id, "outcome", resource)
            return self.store.transition(task_id, state, TaskState.APPROVAL, "read-only-review-passed").state
        raise RuntimeError(f"unsupported state: {state.value}")

    @staticmethod
    def _completed_inspection_result(result):
        """A provider's failed *inspection conclusion* is not a transport failure.

        The independent review stage must still receive it to classify concrete
        findings.  Preserve that fact in bounded evidence while normalizing the
        workflow result to completion; adapter/process failures never reach
        this method because ``_call`` already returns ``None`` for them.
        """
        if result.data.get("status") != "failed":
            return result
        data = dict(result.data)
        evidence = list(data.get("evidence", []))
        if len(evidence) < 50:
            evidence.append("read-only provider reported an inspection failure conclusion; independent review required")
        data["evidence"] = evidence
        data["status"] = "passed"
        return result.model_copy(update={"data": data})

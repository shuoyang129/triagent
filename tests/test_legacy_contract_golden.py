"""Golden contracts inherited from the frozen public ``triagent`` controller.

These tests intentionally use only fake adapters.  They express the observable
legacy protocol that a v2 run must preserve while v2-only features are unused.
"""

from __future__ import annotations

import pytest

from triagent.adapters.base import AgentResult, AgentRole, AgentStatus
from triagent.adapters.fake import FakeAgent
from triagent.domain import Budget, TaskSpec, TaskState
from triagent.orchestrator import Orchestrator
from triagent.store import TaskStore


def _spec() -> TaskSpec:
    return TaskSpec(
        goal="legacy contract",
        scope=["src/"],
        acceptance=["tests pass"],
        budget=Budget(max_agent_calls=20, max_minutes=60, max_usd=0),
    )


def _orchestrator(root, reviewer: FakeAgent) -> tuple[Orchestrator, TaskStore]:
    store = TaskStore(root)
    verified = AgentResult(status=AgentStatus.SUCCEEDED, summary="verified")
    return (
        Orchestrator(
            store=store,
            implementer=FakeAgent.succeeding("implemented"),
            verifier=FakeAgent([verified] * 3),
            reviewer=reviewer,
        ),
        store,
    )


@pytest.mark.parametrize(
    ("configured", "expected"), [("60", 60), ("900", 900), ("3600", 3600)]
)
def test_legacy_request_timeout_golden_bounds(tmp_path, monkeypatch, configured, expected) -> None:
    orchestrator, store = _orchestrator(tmp_path, FakeAgent.succeeding("clean"))
    task = store.create_task(_spec())
    monkeypatch.setenv("TRIAGENT_AGENT_TIMEOUT_SECONDS", configured)

    request = orchestrator._request(
        task.id, AgentRole.IMPLEMENTER, "implementation-result-v1", orchestrator.implementer
    )

    assert (request.agent_identity, request.timeout_seconds, request.handoff_file) == (
        "fake", expected, None
    )
    assert request.task_file == store.runs_root / task.id / "task.yaml"
    assert request.workdir == store.runs_root / task.id / "worktree"


@pytest.mark.parametrize("configured", ["59", "3601", "invalid"])
def test_legacy_request_timeout_golden_rejects_invalid_values(tmp_path, monkeypatch, configured) -> None:
    orchestrator, store = _orchestrator(tmp_path, FakeAgent.succeeding("clean"))
    task = store.create_task(_spec())
    monkeypatch.setenv("TRIAGENT_AGENT_TIMEOUT_SECONDS", configured)

    with pytest.raises(ValueError):
        orchestrator._request(task.id, AgentRole.REVIEWER, "review-result-v1", orchestrator.reviewer)


def test_legacy_review_retry_and_provenance_golden_snapshot(tmp_path) -> None:
    reviewer = FakeAgent([
        AgentResult(status=AgentStatus.UNAVAILABLE, summary="transient"),
        AgentResult(status=AgentStatus.SUCCEEDED, summary="clean"),
    ])
    orchestrator, store = _orchestrator(tmp_path, reviewer)
    task = store.create_task(_spec())

    assert orchestrator.run_until_blocked(task.id) is TaskState.APPROVAL
    assert store.runtime(task.id).agent_calls == 4
    assert store.outcomes(task.id)["review"].status == "passed"
    assert store.execution_provenance(task.id) == {
        "mode": "simulation",
        "implementer": "fake",
        "verifier": "fake",
        "reviewer": "fake",
        "profile_digest": "fake-v1",
    }
    assert orchestrator.approve(task.id, "outcome") is TaskState.APPROVAL

    with pytest.raises(ValueError, match="immutable"):
        store.record_execution_provenance(
            task.id,
            mode="simulation",
            implementer="fake",
            verifier="fake",
            reviewer="fake",
            profile_digest="different-profile",
        )

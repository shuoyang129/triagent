from pathlib import Path
import subprocess

from triagent.adapters.base import AgentResult, AgentStatus
from triagent.adapters.fake import FakeAgent
from triagent.domain import TaskSpec, TaskState
from triagent.read_only import ReadOnlyOrchestrator, load_source_snapshot, save_source_snapshot, source_snapshot
from triagent.store import TaskStore


def test_read_only_orchestrator_never_creates_source_worktree_or_candidate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "README.md").write_text("unchanged\n", encoding="utf-8")
    for args in (["git", "init", "-q"], ["git", "config", "user.email", "test@example.invalid"], ["git", "config", "user.name", "Test"], ["git", "add", "."], ["git", "commit", "-qm", "base"]):
        subprocess.run(args, cwd=repo, check=True)
    store = TaskStore(tmp_path / "data")
    task = store.create_task(TaskSpec(goal="inspect", scope=[str(repo)], acceptance=["clean"], read_only=True))
    before = source_snapshot(repo)
    save_source_snapshot(store.runs_root / task.id, before)
    passed = AgentResult(status=AgentStatus.SUCCEEDED, data={"status": "passed", "evidence": [], "artifacts": [], "findings": []})
    orchestrator = ReadOnlyOrchestrator(store, FakeAgent([passed]), FakeAgent([passed]), FakeAgent([passed]), source=repo, source_before=before)
    assert orchestrator.run_until_blocked(task.id) is TaskState.APPROVAL
    assert source_snapshot(repo)["status_sha256"] == source_snapshot(repo)["status_sha256"]
    assert store.workspace(task.id) is None
    assert not (store.runs_root / task.id / "worktree" / ".git").exists()
    assert store.execution_provenance(task.id)["mode"] == "read-only"
    assert load_source_snapshot(store.runs_root / task.id) == before
    assert (store.runs_root / task.id / "handoff.json").exists()


def test_read_only_completed_failure_reaches_independent_review(tmp_path: Path) -> None:
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "README.md").write_text("unchanged\n", encoding="utf-8")
    for args in (["git", "init", "-q"], ["git", "config", "user.email", "test@example.invalid"], ["git", "config", "user.name", "Test"], ["git", "add", "."], ["git", "commit", "-qm", "base"]):
        subprocess.run(args, cwd=repo, check=True)
    store = TaskStore(tmp_path / "data")
    task = store.create_task(TaskSpec(goal="inspect", scope=[str(repo)], acceptance=["clean"], read_only=True))
    before = source_snapshot(repo); save_source_snapshot(store.runs_root / task.id, before)
    failed_conclusion = AgentResult(status=AgentStatus.SUCCEEDED, data={"status": "failed", "evidence": ["risk uncertain"], "artifacts": []})
    clean_review = AgentResult(status=AgentStatus.SUCCEEDED, data={"status": "passed", "evidence": [], "artifacts": [], "findings": []})
    orchestrator = ReadOnlyOrchestrator(store, FakeAgent([failed_conclusion]), FakeAgent([failed_conclusion]), FakeAgent([clean_review]), source=repo, source_before=before)
    assert orchestrator.run_until_blocked(task.id) is TaskState.APPROVAL
    assert store.outcomes(task.id)["verify"].status == "passed"
    assert "inspection failure conclusion" in store.outcomes(task.id)["verify"].evidence[-1]


def test_read_only_blocking_findings_create_admission_hold_without_approval(tmp_path: Path) -> None:
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "README.md").write_text("unchanged\n", encoding="utf-8")
    for args in (["git", "init", "-q"], ["git", "config", "user.email", "test.invalid"], ["git", "config", "user.name", "Test"], ["git", "add", "."], ["git", "commit", "-qm", "base"]):
        subprocess.run(args, cwd=repo, check=True)
    store = TaskStore(tmp_path / "data")
    task = store.create_task(TaskSpec(goal="inspect", scope=[str(repo)], acceptance=["clean"], read_only=True))
    before = source_snapshot(repo); save_source_snapshot(store.runs_root / task.id, before)
    verified = AgentResult(status=AgentStatus.SUCCEEDED, data={"status": "passed", "evidence": [], "artifacts": []})
    finding = AgentResult(status=AgentStatus.SUCCEEDED, data={"status": "failed", "evidence": ["risk"], "artifacts": [], "findings": [{"severity": "MAJOR", "code": "target-risk", "message": "admission denied"}]})
    orchestrator = ReadOnlyOrchestrator(store, FakeAgent([verified]), FakeAgent([verified]), FakeAgent([finding]), source=repo, source_before=before)

    assert orchestrator.run_until_blocked(task.id) is TaskState.INSPECTION_HOLD
    assert store.outcomes(task.id)["review"].status == "failed"
    assert store.outstanding_approvals(task.id) == []
    assert store.workspace(task.id) is None
    assert source_snapshot(repo) == before

def test_read_only_transport_recovery_never_restores_candidate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "README.md").write_text("unchanged\n", encoding="utf-8")
    for args in (["git", "init", "-q"], ["git", "config", "user.email", "test@example.invalid"], ["git", "config", "user.name", "Test"], ["git", "add", "."], ["git", "commit", "-qm", "base"]):
        subprocess.run(args, cwd=repo, check=True)
    store = TaskStore(tmp_path / "data")
    task = store.create_task(TaskSpec(goal="inspect", scope=[str(repo)], acceptance=["clean"], read_only=True))
    before = source_snapshot(repo); save_source_snapshot(store.runs_root / task.id, before)
    transport_failed = AgentResult(status=AgentStatus.FAILED, data={"diagnostic_code": "json-malformed"})
    passed = AgentResult(status=AgentStatus.SUCCEEDED, data={"status": "passed", "evidence": [], "artifacts": []})
    reviewed = AgentResult(status=AgentStatus.SUCCEEDED, data={"status": "passed", "evidence": [], "artifacts": [], "findings": []})
    first = ReadOnlyOrchestrator(store, FakeAgent([transport_failed]), FakeAgent([transport_failed]), FakeAgent([reviewed]), source=repo, source_before=before)
    assert first.run_until_blocked(task.id) is TaskState.FAILED_RECOVERABLE
    checkpoint = store.recovery_checkpoint(task.id)
    resumed = ReadOnlyOrchestrator(store, FakeAgent([passed]), FakeAgent([passed]), FakeAgent([reviewed]), source=repo, source_before=before, expected_recovery_checkpoint=(checkpoint["stage"], checkpoint["sequence"]))
    assert resumed.resume_until_blocked(task.id) is TaskState.APPROVAL
    assert store.workspace(task.id) is None

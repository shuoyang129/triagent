from __future__ import annotations

import base64
import json
import os
import subprocess
import zlib
from pathlib import Path

import pytest

from triagent.adapters.antigravity import AntigravityAdapter
from triagent.adapters.base import AgentResult, AgentRole, AgentStatus
from triagent.adapters.codex import CodexAdapter
from triagent.adapters.cursor import CursorAdapter
from triagent.adapters.fake import FakeAgent
from triagent.adapters.process import ProcessResult
from triagent.domain import Budget, RiskLevel, StageOutcome, TaskSpec, TaskState
from triagent.git_workspace import GitWorkspace
from triagent.orchestrator import Orchestrator
from triagent.store import BudgetExceeded, StateConflict, TaskStore


def _png_chunk(kind:bytes,payload:bytes)->bytes:
    return len(payload).to_bytes(4,"big")+kind+payload+zlib.crc32(kind+payload).to_bytes(4,"big")
VALID_PNG=b"\x89PNG\r\n\x1a\n"+_png_chunk(b"IHDR",(1).to_bytes(4,"big")*2+bytes([8,2,0,0,0]))+_png_chunk(b"IDAT",zlib.compress(b"\x00\xff\x00\x00"))+_png_chunk(b"IEND",b"")


def init_repo(path: Path, extra: dict[str, bytes] | None = None) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "x@y"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=path, check=True)
    (path / "base.txt").write_text("base", encoding="utf-8")
    for name, data in (extra or {}).items():
        target = path / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def workspace_store(tmp_path: Path, *, visual: bool = False, extra: dict[str, bytes] | None = None):
    repo = tmp_path / "repo"; base = init_repo(repo, extra)
    store = TaskStore(tmp_path / "data")
    spec = TaskSpec(goal="x", scope=[str(repo)], acceptance=["x"], risk=RiskLevel.ROBOT_SAFETY if visual else RiskLevel.LOW, budget=Budget(max_usd=3))
    task = store.create_task(spec)
    work = store.runs_root / task.id / "worktree"; work.rmdir()
    ws = GitWorkspace.create(repo, task.id, destination=work)
    store.set_workspace(task.id, str(repo), base, f"triagent/{task.id}")
    return store, task, work, ws


class StageRunner:
    def __init__(self, output: ProcessResult, mutation: str | None = None):
        self.output = output; self.mutation = mutation; self.inputs = []
    def run(self, argv, cwd, timeout, env, stdin=None):
        if stdin is None and "-p" in argv:
            stdin=Path(argv[-1].rsplit(": ",1)[1]).read_text(encoding="utf-8")
        self.inputs.append(stdin)
        if self.mutation: (Path(cwd) / self.mutation).write_text("unreviewed", encoding="utf-8")
        return self.output


def strict(role: AgentRole) -> dict:
    value = {"status": "passed", "evidence": [], "artifacts": []}
    if role is AgentRole.IMPLEMENTER: value["changed_paths"] = ["candidate.txt"]
    if role is AgentRole.REVIEWER: value["findings"] = []
    return value


def test_verify_failure_resumes_with_codex_without_second_cursor_call(tmp_path):
    store, task, work, _ = workspace_store(tmp_path)

    class ImplementRunner(StageRunner):
        def run(self, argv, cwd, timeout, env, stdin=None):
            (Path(cwd) / "candidate.txt").write_text("candidate", encoding="utf-8")
            return super().run(argv, cwd, timeout, env, stdin)

    cursor_output = ProcessResult(0, json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": json.dumps(strict(AgentRole.IMPLEMENTER)), "total_cost_usd": .1,
    }), "", False)
    failed_verify = strict(AgentRole.VERIFIER) | {"status": "failed"}
    initial_codex = StageRunner(ProcessResult(0, json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": json.dumps(failed_verify)},
    }), "", False))
    initial_cursor = ImplementRunner(cursor_output)
    unused_review = StageRunner(ProcessResult(0, "{}", "", False))
    initial = Orchestrator(
        store,
        CursorAdapter(runner=initial_cursor, estimated_usd=.5),
        CodexAdapter(runner=initial_codex, estimated_usd=.5),
        AntigravityAdapter(runner=unused_review, estimated_usd=.5, acl_verifier=lambda d, f: True),
    )

    assert initial.run_until_blocked(task.id) is TaskState.FAILED_RECOVERABLE
    assert len(initial_cursor.inputs) == 1
    assert len(initial_codex.inputs) == 1
    assert unused_review.inputs == []
    (work / "unreviewed.txt").write_text("remove me", encoding="utf-8")

    resume_cursor = StageRunner(cursor_output)
    resumed_codex = StageRunner(ProcessResult(0, json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": json.dumps(strict(AgentRole.VERIFIER))},
    }), "", False))
    resumed_review = StageRunner(ProcessResult(
        0, json.dumps(strict(AgentRole.REVIEWER) | {"actual_usd": .1}), "", False
    ))
    resumed = Orchestrator(
        store,
        CursorAdapter(runner=resume_cursor, estimated_usd=.5),
        CodexAdapter(runner=resumed_codex, estimated_usd=.5),
        AntigravityAdapter(runner=resumed_review, estimated_usd=.5, acl_verifier=lambda d, f: True),
    )

    assert resumed.resume_until_blocked(task.id) is TaskState.APPROVAL
    assert store.load(task.id).id == task.id
    assert resume_cursor.inputs == []
    assert len(resumed_codex.inputs) == 1
    assert len(resumed_review.inputs) == 1
    assert not (work / "unreviewed.txt").exists()
    assert store.runtime(task.id).repair_attempts == 1


class FailingCodexRunner:
    def __init__(self, shape: str):
        self.shape = shape
        self.inputs = []

    def run(self, argv, cwd, timeout, env, stdin=None):
        self.inputs.append(stdin)
        if self.shape == "unavailable":
            raise FileNotFoundError("vendor path must not persist")
        if self.shape == "exception":
            raise RuntimeError("vendor secret text must not persist")
        if self.shape == "timeout":
            return ProcessResult(None, "vendor timeout text", "", True)
        if self.shape == "invalid":
            return ProcessResult(0, "not-json vendor secret text", "", False)
        raise AssertionError(self.shape)


@pytest.mark.parametrize("shape", ["unavailable", "timeout", "invalid", "exception"])
def test_transport_verify_failure_persists_stage_and_resumes_at_codex(tmp_path, shape):
    store, task, work, _ = workspace_store(tmp_path)

    class ImplementRunner(StageRunner):
        def run(self, argv, cwd, timeout, env, stdin=None):
            (Path(cwd) / "candidate.txt").write_text("candidate", encoding="utf-8")
            return super().run(argv, cwd, timeout, env, stdin)

    cursor_output = ProcessResult(0, json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": json.dumps(strict(AgentRole.IMPLEMENTER)), "total_cost_usd": .1,
    }), "", False)
    initial_cursor = ImplementRunner(cursor_output)
    failing_codex = FailingCodexRunner(shape)
    initial = Orchestrator(
        store,
        CursorAdapter(runner=initial_cursor, estimated_usd=.5),
        CodexAdapter(runner=failing_codex, estimated_usd=.5),
        AntigravityAdapter(
            runner=StageRunner(ProcessResult(0, "{}", "", False)),
            estimated_usd=.5, acl_verifier=lambda d, f: True,
        ),
    )

    assert initial.run_until_blocked(task.id) is TaskState.FAILED_RECOVERABLE
    outcome = store.outcomes(task.id)["verify"]
    assert outcome.status == "failed"
    assert "vendor" not in (outcome.diagnostic or "").lower()

    resume_cursor = StageRunner(cursor_output)
    resumed_codex = StageRunner(ProcessResult(0, json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": json.dumps(strict(AgentRole.VERIFIER))},
    }), "", False))
    resumed_review = StageRunner(ProcessResult(
        0, json.dumps(strict(AgentRole.REVIEWER) | {"actual_usd": .1}), "", False
    ))
    resumed = Orchestrator(
        store,
        CursorAdapter(runner=resume_cursor, estimated_usd=.5),
        CodexAdapter(runner=resumed_codex, estimated_usd=.5),
        AntigravityAdapter(
            runner=resumed_review, estimated_usd=.5, acl_verifier=lambda d, f: True
        ),
    )

    assert resumed.resume_until_blocked(task.id) is TaskState.APPROVAL
    assert resume_cursor.inputs == []
    assert len(resumed_codex.inputs) == 1
    assert len(resumed_review.inputs) == 1


def test_resume_rejects_invalid_state_missing_outcome_and_exhausted_limits(tmp_path):
    cases = []
    for name in ("invalid", "missing", "repairs", "budget"):
        root = tmp_path / name
        store = TaskStore(root)
        max_calls = 0 if name == "budget" else 20
        task = store.create_task(TaskSpec(
            goal="x", scope=[str(root)], acceptance=["x"],
            budget=Budget(max_agent_calls=max_calls, max_usd=0),
        ))
        store.record_execution_provenance(
            task.id, mode="simulation", implementer="fake", verifier="fake",
            reviewer="fake", profile_digest="fake-v1",
        )
        if name == "missing":
            store.transition(task.id, TaskState.SPEC, TaskState.FAILED_RECOVERABLE, "test")
        elif name != "invalid":
            store.transition_recoverable(task.id, TaskState.SPEC, "verify", "test")
        if name in {"repairs", "budget"}:
            store.record_outcome(task.id, StageOutcome(
                stage="verify", status="failed", summary="requires-repair"
            ))
        if name == "repairs":
            store.increment_repair_attempts(task.id)
            store.increment_repair_attempts(task.id)
        agents = (FakeAgent([]), FakeAgent([]), FakeAgent([]))
        cases.append((name, Orchestrator(store, *agents), store, task, agents))

    for name, orchestrator, store, task, agents in cases:
        expected = BudgetExceeded if name == "budget" else ValueError
        with pytest.raises(expected):
            orchestrator.resume_until_blocked(task.id)
        assert all(agent.requests == [] for agent in agents)
        assert store.runtime(task.id).repair_attempts == (2 if name == "repairs" else 0)


def test_review_repair_implementation_failure_replaces_recovery_checkpoint(tmp_path):
    major = AgentResult(
        status=AgentStatus.SUCCEEDED,
        data={"status": "passed", "findings": [{"severity": "MAJOR", "message": "repair"}]},
    )
    initial = Orchestrator(
        TaskStore(tmp_path),
        FakeAgent([
            AgentResult(status=AgentStatus.SUCCEEDED),
            AgentResult(status=AgentStatus.UNAVAILABLE),
        ]),
        FakeAgent([AgentResult(status=AgentStatus.SUCCEEDED)]),
        FakeAgent([major]),
    )
    store = initial.store
    task = store.create_task(TaskSpec(goal="x", scope=[str(tmp_path)], acceptance=["x"]))

    assert initial.run_until_blocked(task.id) is TaskState.FAILED_RECOVERABLE
    assert store.outcomes(task.id)["review"].status == "failed"
    assert store.recovery_checkpoint(task.id)["stage"] == "implement"

    implementer = FakeAgent([AgentResult(status=AgentStatus.SUCCEEDED)])
    verifier = FakeAgent([AgentResult(status=AgentStatus.SUCCEEDED)])
    reviewer = FakeAgent([AgentResult(status=AgentStatus.SUCCEEDED)])
    resumed = Orchestrator(store, implementer, verifier, reviewer)

    assert resumed.resume_until_blocked(task.id) is TaskState.APPROVAL
    assert len(implementer.requests) == 1
    assert len(verifier.requests) == 1
    assert len(reviewer.requests) == 1
    assert store.recovery_checkpoint(task.id) is None


def test_review_repair_verification_failure_replaces_recovery_checkpoint(tmp_path):
    major = AgentResult(
        status=AgentStatus.SUCCEEDED,
        data={"status": "passed", "findings": [{"severity": "MAJOR", "message": "repair"}]},
    )
    store = TaskStore(tmp_path)
    initial = Orchestrator(
        store,
        FakeAgent([
            AgentResult(status=AgentStatus.SUCCEEDED),
            AgentResult(status=AgentStatus.SUCCEEDED),
        ]),
        FakeAgent([
            AgentResult(status=AgentStatus.SUCCEEDED),
            AgentResult(status=AgentStatus.INVALID_OUTPUT),
        ]),
        FakeAgent([major]),
    )
    task = store.create_task(TaskSpec(goal="x", scope=[str(tmp_path)], acceptance=["x"]))

    assert initial.run_until_blocked(task.id) is TaskState.FAILED_RECOVERABLE
    assert store.outcomes(task.id)["review"].status == "failed"
    assert store.recovery_checkpoint(task.id)["stage"] == "verify"
    # This regression isolates checkpoint selection; production verification
    # resumes restore a real reviewed candidate before consuming the checkpoint.
    store.restore_candidate_worktree = lambda _task_id: None

    implementer = FakeAgent([])
    verifier = FakeAgent([AgentResult(status=AgentStatus.SUCCEEDED)])
    reviewer = FakeAgent([AgentResult(status=AgentStatus.SUCCEEDED)])
    resumed = Orchestrator(store, implementer, verifier, reviewer)

    assert resumed.resume_until_blocked(task.id) is TaskState.APPROVAL
    assert implementer.requests == []
    assert len(verifier.requests) == 1
    assert len(reviewer.requests) == 1
    assert store.recovery_checkpoint(task.id) is None


def test_accept_recovery_rejects_a_replaced_same_stage_checkpoint(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create_task(TaskSpec(goal="x", scope=[str(tmp_path)], acceptance=["x"]))
    first = store.transition_recoverable(task.id, TaskState.SPEC, "implement", "first")
    first_sequence = store.recovery_checkpoint(task.id)["sequence"]
    store.transition_recoverable(first.id, TaskState.FAILED_RECOVERABLE, "implement", "newer")
    owner = "resume-controller"
    store.acquire_lease(task.id, owner, 60)

    with pytest.raises(StateConflict, match="changed"):
        store.accept_recovery(
            task.id,
            stage="implement",
            sequence=first_sequence,
            target=TaskState.IMPLEMENT,
            lease_owner=owner,
        )

    assert store.load(task.id).state is TaskState.FAILED_RECOVERABLE
    assert store.recovery_checkpoint(task.id)["sequence"] > first_sequence


def test_candidate_exists_before_verify_and_reviewer_mutations_never_enter_it(tmp_path):
    store, task, work, _ = workspace_store(tmp_path)
    class ImplementRunner(StageRunner):
        def run(self, argv, cwd, timeout, env, stdin=None):
            (Path(cwd) / "candidate.txt").write_text("candidate", encoding="utf-8")
            return super().run(argv, cwd, timeout, env, stdin)
    cursor_output = ProcessResult(0, json.dumps({"type":"result","subtype":"success","is_error":False,"result":json.dumps(strict(AgentRole.IMPLEMENTER)),"total_cost_usd":.1}), "", False)
    codex_output = ProcessResult(0, json.dumps({"type":"item.completed","item":{"type":"agent_message","text":json.dumps(strict(AgentRole.VERIFIER))}}), "", False)
    review_output = ProcessResult(0, json.dumps(strict(AgentRole.REVIEWER) | {"actual_usd": .1}), "", False)
    implement = ImplementRunner(cursor_output)
    verify = StageRunner(codex_output, "verifier-mutation.txt")
    review = StageRunner(review_output, "reviewer-mutation.txt")
    orchestrator = Orchestrator(store, CursorAdapter(runner=implement, estimated_usd=.5), CodexAdapter(runner=verify, estimated_usd=.5), AntigravityAdapter(runner=review, estimated_usd=.5, acl_verifier=lambda d,f: True))
    assert orchestrator.run_until_blocked(task.id) is TaskState.APPROVAL
    resource = store.approval_manifest(task.id); candidate = resource["reviewed_commit"]
    tree = subprocess.run(["git", "ls-tree", "-r", "--name-only", candidate], cwd=work, check=True, capture_output=True, text=True).stdout.splitlines()
    assert "candidate.txt" in tree and "verifier-mutation.txt" not in tree and "reviewer-mutation.txt" not in tree
    assert all(candidate in value for value in [store.approval_manifest(task.id)["reviewed_commit"]])
    for prompt in (verify.inputs[0], review.inputs[0]):
        assert "candidate.txt" in prompt and "verifier-mutation.txt" not in prompt and "reviewer-mutation.txt" not in prompt


def test_candidate_plumbing_ignores_hooks_and_filters(tmp_path):
    store, task, work, _ = workspace_store(tmp_path, extra={".gitattributes": b"filtered.txt filter=evil\n", "filtered.txt": b"base\n"})
    subprocess.run(["git", "config", "filter.evil.required", "true"], cwd=work, check=True)
    subprocess.run(["git", "config", "filter.evil.clean", "missing-filter-command"], cwd=work, check=True)
    hook = work / ".git" / "hooks" / "pre-commit"
    # Worktree .git is a file, so install the hook in the actual common git dir.
    git_dir = Path(subprocess.run(["git", "rev-parse", "--git-dir"], cwd=work, check=True, capture_output=True, text=True).stdout.strip())
    if not git_dir.is_absolute(): git_dir = work / git_dir
    hook = git_dir / "hooks" / "pre-commit"; hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho hook > hook-ran.txt\n", encoding="utf-8"); hook.chmod(0o755)
    (work / "filtered.txt").write_bytes(b"raw-controller-bytes\n")
    candidate = store.materialize_reviewed_commit(task.id)
    assert subprocess.run(["git", "show", f"{candidate}:filtered.txt"], cwd=work, check=True, capture_output=True).stdout == b"raw-controller-bytes\n"
    assert not (work / "hook-ran.txt").exists()


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("credentials.env", b"API_KEY=super-secret-value-123456789\n"),
        ("large.txt", b"x" * (1024 * 1024 + 1)),
        (".triagent/controller.txt", b"widen"),
    ],
    ids=["secret","oversize","controller-path"],
)
def test_candidate_manifest_rejects_secret_oversize_and_controller_paths(tmp_path, name, data):
    store, task, work, _ = workspace_store(tmp_path)
    target = work / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
    with pytest.raises(ValueError, match="candidate manifest rejected"):
        store.materialize_reviewed_commit(task.id)


def test_candidate_manifest_rejects_ignore_widening(tmp_path):
    store, task, work, _ = workspace_store(tmp_path, extra={".gitignore": b"*.cache\n"})
    (work / ".gitignore").write_text("*.cache\n*.env\n", encoding="utf-8")
    with pytest.raises(ValueError, match="candidate manifest rejected"):
        store.materialize_reviewed_commit(task.id)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not reliably available without Windows developer mode")
def test_candidate_manifest_rejects_symlink(tmp_path):
    store, task, work, _ = workspace_store(tmp_path)
    (work / "link.txt").symlink_to(work / "base.txt")
    with pytest.raises(ValueError, match="candidate manifest rejected"):
        store.materialize_reviewed_commit(task.id)


def test_protected_candidate_ref_survives_branch_deletion_and_gc(tmp_path):
    store, task, work, _ = workspace_store(tmp_path)
    (work / "candidate.txt").write_text("candidate", encoding="utf-8")
    candidate = store.materialize_reviewed_commit(task.id)
    ref = f"refs/triagent/reviewed/{task.id}"
    assert subprocess.run(["git", "rev-parse", ref], cwd=work, check=True, capture_output=True, text=True).stdout.strip() == candidate
    subprocess.run(["git", "update-ref", "-d", f"refs/heads/triagent/{task.id}"], cwd=work, check=True)
    subprocess.run(["git", "gc", "--prune=now"], cwd=work, check=True)
    assert subprocess.run(["git", "cat-file", "-t", candidate], cwd=work, check=True, capture_output=True, text=True).stdout.strip() == "commit"


def test_candidate_restore_removes_verifier_mutations_before_next_stage(tmp_path):
    store, task, work, _ = workspace_store(tmp_path)
    (work / "candidate.txt").write_text("candidate", encoding="utf-8")
    candidate = store.materialize_reviewed_commit(task.id)
    (work / "candidate.txt").write_text("mutated", encoding="utf-8")
    (work / "verifier-extra.txt").write_text("unreviewed", encoding="utf-8")
    store.restore_candidate_worktree(task.id)
    assert (work / "candidate.txt").read_text(encoding="utf-8") == "candidate"
    assert not (work / "verifier-extra.txt").exists()
    assert store.workspace(task.id)["reviewed_commit"] == candidate


def test_exact_approval_consumption_rejects_changed_protected_ref(tmp_path):
    store, task, work, _ = workspace_store(tmp_path)
    (work / "candidate.txt").write_text("candidate", encoding="utf-8")
    candidate = store.materialize_reviewed_commit(task.id); resource = store.approval_manifest(task.id)
    store.request_approval(task.id, "merge", resource); store.approve_requested(task.id, "merge")
    assert store.consume_approval(task.id, "merge") == candidate
    subprocess.run(["git", "update-ref", f"refs/triagent/reviewed/{task.id}", resource["base_commit"]], cwd=work, check=True)
    with pytest.raises(PermissionError, match="exact candidate"):
        store.consume_approval(task.id, "merge")


@pytest.mark.parametrize("name,data", [("fake.png", b"not-a-png"), ("active.svg", b"<svg><script/></svg>"), ("document.pdf", b"%PDF-1.4")])
def test_required_visual_rejects_fake_or_active_formats(tmp_path, name, data):
    store, task, work, _ = workspace_store(tmp_path, visual=True)
    (work / name).write_bytes(data)
    store.record_outcome(task.id, StageOutcome(stage="review", status="passed", summary="clean", artifacts=[name]))
    with pytest.raises(ValueError, match="candidate manifest rejected|visual artifact"):
        store.materialize_reviewed_commit(task.id)
        store.approval_manifest(task.id)


def test_required_visual_accepts_structurally_valid_png(tmp_path):
    store, task, work, _ = workspace_store(tmp_path, visual=True)
    (work / "visual.png").write_bytes(VALID_PNG)
    store.record_outcome(task.id, StageOutcome(stage="review", status="passed", summary="clean", artifacts=["visual.png"]))
    store.materialize_reviewed_commit(task.id)
    assert store.approval_manifest(task.id)["visual_artifact_digest"]

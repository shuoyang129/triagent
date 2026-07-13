from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

import pytest

from triagent.adapters.antigravity import AntigravityAdapter
from triagent.adapters.base import AgentRole
from triagent.adapters.codex import CodexAdapter
from triagent.adapters.cursor import CursorAdapter
from triagent.adapters.process import ProcessResult
from triagent.domain import Budget, RiskLevel, StageOutcome, TaskSpec, TaskState
from triagent.git_workspace import GitWorkspace
from triagent.orchestrator import Orchestrator
from triagent.store import TaskStore


VALID_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


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
    spec = TaskSpec(goal="x", scope=["x"], acceptance=["x"], risk=RiskLevel.ROBOT_SAFETY if visual else RiskLevel.LOW, budget=Budget(max_usd=3))
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
    if role is AgentRole.REVIEWER: value["findings"] = []
    return value


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

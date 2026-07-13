from __future__ import annotations

import json
import hashlib
import base64
import zlib
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import triagent.adapters._cli as cli_transport
import triagent.cli as cli_module
from triagent.adapters._cli import invoke_json, read_prompt
from triagent.adapters.base import AgentRequest, AgentRole, AgentStatus
from triagent.adapters.process import ProcessResult
from triagent.domain import RiskLevel, StageOutcome, TaskSpec, TaskState
from triagent.store import TaskStore

def _png_chunk(kind:bytes,payload:bytes)->bytes:
    return len(payload).to_bytes(4,"big")+kind+payload+zlib.crc32(kind+payload).to_bytes(4,"big")
VALID_PNG=b"\x89PNG\r\n\x1a\n"+_png_chunk(b"IHDR",(1).to_bytes(4,"big")*2+bytes([8,2,0,0,0]))+_png_chunk(b"IDAT",zlib.compress(b"\x00\xff\x00\x00"))+_png_chunk(b"IEND",b"")


def init_repo(path: Path) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "x@y"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=path, check=True)
    (path / "base.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def visual_store(tmp_path: Path, artifact: str | None) -> tuple[TaskStore, str, Path]:
    repo = tmp_path / "repo"
    base = init_repo(repo)
    store = TaskStore(tmp_path / "data")
    task = store.create_task(TaskSpec(goal="x", scope=[str(repo)], acceptance=["x"], risk=RiskLevel.ROBOT_SAFETY))
    work = store.runs_root / task.id / "worktree"
    work.rmdir()
    subprocess.run(["git", "clone", "-q", str(repo), str(work)], check=True)
    (work / "change.txt").write_text("reviewed", encoding="utf-8")
    artifacts = [] if artifact is None else [artifact]
    if artifact == "visual.png":
        (work / artifact).write_bytes(VALID_PNG)
    store.record_outcome(task.id, StageOutcome(stage="review", status="passed", summary="clean", artifacts=artifacts))
    store.set_workspace(task.id, str(repo), base, f"triagent/{task.id}")
    return store, task.id, work


def test_reviewed_commit_is_sole_approval_unit_after_worktree_changes(tmp_path):
    store, task_id, work = visual_store(tmp_path, "visual.png")
    reviewed = store.materialize_reviewed_commit(task_id)
    manifest = store.approval_manifest(task_id)
    assert manifest["reviewed_commit"] == reviewed
    store.transition(task_id, TaskState.SPEC, TaskState.WAITING_FOR_VISUAL_APPROVAL, "review-passed")
    store.request_approval(task_id, "visual", manifest)
    (work / "change.txt").write_text("unreviewed mutation", encoding="utf-8")
    assert store.approval_manifest(task_id) == manifest
    store.approve_and_transition(task_id, "visual", TaskState.WAITING_FOR_VISUAL_APPROVAL, TaskState.APPROVAL)
    store.approve_requested(task_id, "outcome")
    store.approve_requested(task_id, "merge")
    for action in ("visual", "outcome", "merge"):
        assert store.approval_resource(task_id, action)["reviewed_commit"] == reviewed
    reviewed_bytes = subprocess.run(["git", "show", f"{reviewed}:change.txt"], cwd=work, check=True, capture_output=True).stdout
    assert reviewed_bytes == b"reviewed"


@pytest.mark.parametrize("artifact", [None, "missing.png", "notes.txt"])
def test_required_visual_rejects_empty_missing_or_non_allowlisted_artifact(tmp_path, artifact):
    store, task_id, _ = visual_store(tmp_path, artifact)
    store.materialize_reviewed_commit(task_id)
    with pytest.raises(ValueError, match="visual artifact"):
        store.approval_manifest(task_id)


def test_required_visual_accepts_existing_allowlisted_artifact_bytes(tmp_path):
    store, task_id, _ = visual_store(tmp_path, "visual.png")
    store.materialize_reviewed_commit(task_id)
    manifest = store.approval_manifest(task_id)
    assert manifest["visual_artifact_digest"] != hashlib.sha256(b"").hexdigest()
    assert manifest["visual_artifact_version"] == manifest["visual_artifact_digest"]


@pytest.mark.parametrize("command", ["create", "run"])
def test_state_root_and_task_creation_failures_are_categorical(tmp_path, monkeypatch, command):
    secret = str(tmp_path / "private" / "triagent.sqlite3")
    monkeypatch.setattr(cli_module, "TaskStore", lambda root: (_ for _ in ()).throw(OSError(secret)))
    monkeypatch.setattr(cli_module.GitWorkspace, "validate", lambda repo: (Path(repo), "base"))
    args = [command, str(tmp_path), "x", "--risk", "low", "--acceptance", "tests pass", "--visual-check", "none", "--data-root", str(tmp_path / "data")]
    result = CliRunner().invoke(cli_module.app, args)
    assert result.exit_code != 0
    assert secret not in result.output
    assert "task creation failed" in result.output


@pytest.mark.parametrize(
    ("role", "operation"),
    [
        (AgentRole.IMPLEMENTER, "implement the supplied task and produce implementation evidence"),
        (AgentRole.VERIFIER, "verify the supplied implementation and produce verification evidence"),
        (AgentRole.REVIEWER, "independently review the supplied implementation and report findings"),
    ],
)
def test_prompt_operation_is_explicit_and_schema_matches_strict_parser(tmp_path, role, operation):
    task = tmp_path / "task"; task.write_text("x", encoding="utf-8")
    handoff = tmp_path / "handoff"; handoff.write_text("{}", encoding="utf-8")
    request = AgentRequest(role=role, task_file=task, handoff_file=handoff, workdir=tmp_path, output_schema="strict-v1", timeout_seconds=5)
    prompt, error = read_prompt(request)
    assert error is None and f"REQUIRED_OPERATION={operation}" in prompt
    schema = json.loads(prompt.split("OUTPUT_SCHEMA_JSON=", 1)[1].split("\nTASK\n", 1)[0])
    assert set(schema["required"]) >= {"status", "evidence", "artifacts"}
    class Runner:
        def run(self, *args, **kwargs): return ProcessResult(0, json.dumps({"status": "passed"}), "", False)
    assert invoke_json(Runner(), ["local"], tmp_path, 1, role=role).status is AgentStatus.INVALID_OUTPUT


def test_windows_directory_dacl_rejects_aces_without_inheritance_flags(tmp_path, monkeypatch):
    sid = "S-1-5-21-1"
    evidence = {"owner": sid, "protected": True, "rules": [
        {"sid": sid, "type": "Allow", "rights": "FullControl", "inheritance": "None", "propagation": "None"},
        {"sid": "S-1-5-18", "type": "Allow", "rights": "FullControl", "inheritance": "None", "propagation": "None"},
    ]}
    values = iter([sid, evidence])
    monkeypatch.setattr(cli_transport, "_powershell_json", lambda script: next(values))
    assert cli_transport._windows_acl(tmp_path, None) is False

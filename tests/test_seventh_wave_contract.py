from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from triagent.adapters import _cli as cli_support
from triagent.adapters._cli import _windows_acl
from triagent.adapters.antigravity import AntigravityAdapter
from triagent.adapters.base import AgentRequest, AgentRole, AgentStatus
from triagent.adapters.codex import CodexAdapter
from triagent.adapters.cursor import CursorAdapter
from triagent.adapters.process import ProcessResult, ProcessRunner
from triagent.cli import app
from triagent.domain import Budget, TaskSpec, TaskState
from triagent.orchestrator import Orchestrator
from triagent.store import TaskStore


class CaptureRunner:
    def __init__(self, output: ProcessResult) -> None:
        self.output = output
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []
        self.file_bytes: list[bytes] = []

    def run(self, argv, cwd, timeout, env, stdin=None):
        self.calls.append(list(argv))
        self.inputs.append(stdin)
        if "-p" in argv:
            self.file_bytes.append(Path(argv[-1].rsplit(": ", 1)[1]).read_bytes())
        return self.output


def request(tmp_path: Path, role: AgentRole) -> AgentRequest:
    task = tmp_path / f"{role}-task.txt"
    task.write_text("change only the requested file", encoding="utf-8")
    handoff = tmp_path / f"{role}-handoff.json"
    handoff.write_text(json.dumps({"final_diff": "diff"}), encoding="utf-8")
    return AgentRequest(
        role=role,
        task_file=task,
        handoff_file=handoff,
        workdir=tmp_path,
        output_schema=f"{role.value}-result-v1",
        timeout_seconds=5,
    )


@pytest.mark.parametrize(
    ("role", "adapter", "output"),
    [
        (AgentRole.IMPLEMENTER, CursorAdapter, ProcessResult(0, json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps({"status": "passed", "evidence": [], "artifacts": [], "changed_paths": []})}), "", False)),
        (AgentRole.VERIFIER, CodexAdapter, ProcessResult(0, json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"status": "passed", "evidence": [], "artifacts": []})}}), "", False)),
        (AgentRole.REVIEWER, AntigravityAdapter, ProcessResult(0, json.dumps({"status": "passed", "evidence": [], "artifacts": [], "findings": []}), "", False)),
    ],
)
def test_controller_prompt_reaches_vendor_bytes_with_exact_role_schema(tmp_path, role, adapter, output):
    runner = CaptureRunner(output)
    kwargs = {"acl_verifier": lambda directory, file: True} if adapter is AntigravityAdapter else {}
    assert adapter(runner=runner, **kwargs).run(request(tmp_path, role)).status is AgentStatus.SUCCEEDED
    prompt = (runner.file_bytes[0].decode() if runner.file_bytes else runner.inputs[0])
    assert f'IMMUTABLE_ROLE={role.value}' in prompt
    assert 'REQUIRED_OPERATION=' in prompt and f'REQUIRED_OPERATION={role.value}\n' not in prompt
    assert "SAFETY_BOUNDARY=" in prompt
    assert f'OUTPUT_SCHEMA_ID={role.value}-result-v1' in prompt
    schema = json.loads(prompt.split("OUTPUT_SCHEMA_JSON=", 1)[1].split("\nTASK\n", 1)[0])
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) >= {"status", "evidence", "artifacts"}
    assert ("findings" in schema["required"]) is (role is AgentRole.REVIEWER)


def test_antigravity_secures_directory_before_payload_write(tmp_path):
    order = []
    runner = CaptureRunner(ProcessResult(0, json.dumps({"status": "passed", "evidence": [], "artifacts": [], "findings": []}), "", False))

    def verify(directory: Path, file: Path | None) -> bool:
        order.append("directory" if file is None else "file")
        if file is None:
            assert not (directory / "input.txt").exists()
        else:
            assert file.read_bytes() == b""
        return True

    result = AntigravityAdapter(runner=runner, acl_verifier=verify).run(request(tmp_path, AgentRole.REVIEWER))
    assert result.status is AgentStatus.SUCCEEDED
    assert order == ["directory", "file"]


def test_antigravity_injected_acl_contract_is_platform_independent(tmp_path, monkeypatch):
    checked = []
    runner = CaptureRunner(ProcessResult(0, json.dumps({"status": "passed", "evidence": [], "artifacts": [], "findings": []}), "", False))
    adapter = AntigravityAdapter(runner=runner, acl_verifier=lambda directory, file: checked.append((directory, file)) or file is None)
    monkeypatch.setattr(cli_support, "os", SimpleNamespace(name="posix"))

    result = adapter.run(request(tmp_path, AgentRole.REVIEWER))
    assert result.status is AgentStatus.FAILED
    assert [file is None for _, file in checked] == [True, False]
    assert runner.calls == []


def test_cursor_capabilities_never_probe_models_or_run_paid_smoke(tmp_path):
    class ProbeRunner:
        def __init__(self): self.calls = []
        def run(self, argv, cwd, timeout, env, stdin=None):
            self.calls.append(list(argv))
            if "--version" in argv: return ProcessResult(0, "1.0", "", False)
            if "status" in argv: return ProcessResult(0, "ok", "", False)
            raise AssertionError("unexpected Cursor probe")
    runner = ProbeRunner()
    capability = CursorAdapter(runner=runner, command=["cursor-agent"]).capabilities()
    assert capability.available is True
    assert runner.calls == [["cursor-agent", "--version"], ["cursor-agent", "status"]]


def _init_repo(path: Path) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "x@y"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=path, check=True)
    (path / "a.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def test_approval_consumption_stays_bound_to_materialized_commit(tmp_path):
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    store = TaskStore(tmp_path / "data")
    task = store.create_task(TaskSpec(goal="x", scope=[str(repo)], acceptance=["x"]))
    work = store.runs_root / task.id / "worktree"
    work.rmdir()
    shutil.copytree(repo, work)
    store.set_workspace(task.id, str(repo), base, f"triagent/{task.id}")
    store.materialize_reviewed_commit(task.id)
    store.transition(task.id, TaskState.SPEC, TaskState.WAITING_FOR_VISUAL_APPROVAL, "review-passed")
    store.request_approval(task.id, "visual", store.approval_manifest(task.id))
    (work / "a.txt").write_text("changed after review", encoding="utf-8")
    store.approve_and_transition(task.id, "visual", TaskState.WAITING_FOR_VISUAL_APPROVAL, TaskState.APPROVAL)
    assert store.load(task.id).state is TaskState.APPROVAL


def test_cleanup_failure_persists_sanitized_attention(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "data")
    task = store.create_task(TaskSpec(goal="x", scope=["x"], acceptance=["x"], budget=Budget(max_usd=1)))
    store.transition(task.id, TaskState.SPEC, TaskState.REVIEW, "test")
    req = request(store.runs_root / task.id, AgentRole.REVIEWER).model_copy(update={"agent_identity":"antigravity"})
    runner = CaptureRunner(ProcessResult(0, json.dumps({"status": "passed", "findings": []}), "", False))
    monkeypatch.setattr(shutil, "rmtree", lambda path: (_ for _ in ()).throw(OSError(str(path))))
    adapter = AntigravityAdapter(runner=runner, estimated_usd=.1, acl_verifier=lambda directory, file: True)
    assert Orchestrator(store, adapter, adapter, adapter)._call(task.id, TaskState.REVIEW, adapter, req) is None
    assert store.attention_items(task.id) == ["transport-cleanup-failed"]


def test_run_precreation_errors_are_categorical_and_path_free(tmp_path):
    missing = tmp_path / "private" / "profile.toml"
    result = CliRunner().invoke(app, ["run", str(tmp_path), "x", "--risk", "low", "--acceptance", "tests pass", "--visual-check", "none", "--profile", str(missing), "--live-confirmed", "--billing-confirmed", "--data-root", str(tmp_path / "data")])
    assert result.exit_code != 0
    assert str(missing) not in result.output
    assert "task input validation failed" in result.output
    assert not (tmp_path / "data" / "triagent.sqlite3").exists()


def test_approval_resource_returns_latest_sequence_not_lexicographic_json(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create_task(TaskSpec(goal="x", scope=["x"], acceptance=["x"]))
    store.request_approval(task.id, "deploy", {"version": "z"})
    store.approve_requested(task.id, "deploy")
    store.request_approval(task.id, "deploy", {"version": "a"})
    store.approve_requested(task.id, "deploy")
    assert store.approval_resource(task.id, "deploy")["version"] == "a"


def test_process_runner_real_local_subprocess_echoes_exact_stdin(tmp_path):
    value = "stdin-✓-" * 1000
    result = ProcessRunner().run([sys.executable, "-c", "import sys;sys.stdout.write(sys.stdin.read())"], tmp_path, 10, {}, stdin=value)
    assert result.returncode == 0 and result.stdout == value


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL contract")
def test_windows_private_dacl_real_sid_contract(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir()
    file = directory / "input.txt"
    assert _windows_acl(directory, None)
    file.write_text("payload", encoding="utf-8")
    assert _windows_acl(directory, file)


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL contract")
def test_windows_private_dacl_supports_fresh_tempfile_directory():
    directory = Path(tempfile.mkdtemp(prefix="triagent-private-"))
    try:
        assert _windows_acl(directory, None)
    finally:
        shutil.rmtree(directory)


@pytest.mark.skipif(shutil.which("wsl.exe") is None, reason="WSL unavailable")
def test_process_runner_wsl_echoes_exact_stdin(tmp_path):
    value = "wsl-stdin-✓"
    result = ProcessRunner().run(["wsl.exe", "--exec", "python3", "-c", "import sys;sys.stdout.write(sys.stdin.read())"], tmp_path, 20, {}, stdin=value)
    assert result.returncode == 0 and result.stdout == value

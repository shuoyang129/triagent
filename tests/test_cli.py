from pathlib import Path
import json
import re
import subprocess
import tomllib
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from triagent.cli import app
import triagent.cli as cli_module
from triagent.adapters.antigravity import AntigravityAdapter as RealAntigravityAdapter
from triagent.adapters.codex import CodexAdapter as RealCodexAdapter
from triagent.adapters.deepseek import DeepSeekAdapter as RealDeepSeekAdapter
from triagent.adapters.process import ProcessResult
from triagent.domain import Budget, RiskLevel, StageOutcome, TaskSpec, TaskState
from triagent.git_workspace import GitWorkspace
from triagent.orchestrator import Orchestrator
from triagent.report import REPORT_FIELDS
from triagent.report import render_report
from triagent.store import TaskStore


runner = CliRunner()


def structured_args(*, risk: str = "low") -> list[str]:
    return ["--risk", risk, "--acceptance", "tests pass", "--visual-check", "none"]


def test_create_requires_explicit_risk_and_acceptance_before_task_creation(tmp_path: Path) -> None:
    data = tmp_path / "data"

    missing_risk = runner.invoke(app, ["create", "--acceptance", "tests pass", str(tmp_path), "goal"])
    missing_acceptance = runner.invoke(app, ["create", "--risk", "low", str(tmp_path), "goal"])

    assert missing_risk.exit_code != 0
    assert missing_acceptance.exit_code != 0
    assert not (data / "triagent.sqlite3").exists()


def test_create_persists_explicit_structured_spec_inputs(tmp_path: Path) -> None:
    data = tmp_path / "data"
    result = runner.invoke(app, [
        "create", "--data-root", str(data), "--risk", "medium",
        "--acceptance", "unit tests pass", "--acceptance", "docs updated",
        "--forbidden", "secrets/", "--forbidden", "deploy.py",
        "--visual-check", "optional", str(tmp_path), "goal",
    ])

    assert result.exit_code == 0, result.output
    task_id = result.output.split("Task: ", 1)[1].splitlines()[0]
    spec = TaskStore(data).load(task_id).spec
    assert spec.risk is RiskLevel.MEDIUM
    assert spec.acceptance == ["unit tests pass", "docs updated"]
    assert spec.forbidden == ["secrets/", "deploy.py"]
    assert spec.visual_check == "optional"
    assert spec.scope == [str(tmp_path.resolve())]


def test_robot_safety_cli_forces_required_visual_check(tmp_path: Path) -> None:
    data = tmp_path / "data"
    result = runner.invoke(app, [
        "create", "--data-root", str(data), "--risk", "robot-safety",
        "--acceptance", "safe stop verified", "--visual-check", "none",
        str(tmp_path), "goal",
    ])

    assert result.exit_code == 0, result.output
    task_id = result.output.split("Task: ", 1)[1].splitlines()[0]
    assert TaskStore(data).load(task_id).spec.visual_check == "required"


def test_resume_cli_preserves_task_id_and_reaches_approval(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = TaskStore(data)
    task = store.create_task(TaskSpec(
        goal="goal", scope=[str(tmp_path)], acceptance=["tests pass"]
    ))
    store.record_outcome(task.id, StageOutcome(
        stage="implement", status="failed", summary="requires-repair"
    ))
    store.record_execution_provenance(
        task.id, mode="simulation", implementer="fake", verifier="fake",
        reviewer="fake", profile_digest="fake-v1",
    )
    store.transition_recoverable(task.id, TaskState.SPEC, "implement", "test")

    result = runner.invoke(app, [
        "resume", "--profile", "fake", "--data-root", str(data), task.id
    ])

    assert result.exit_code == 0, result.output
    assert f"Task: {task.id}" in result.output
    assert "State: APPROVAL" in result.output
    assert store.runtime(task.id).repair_attempts == 1


def test_resume_requires_explicit_profile_and_rejects_live_to_fake_downgrade(
    tmp_path: Path, monkeypatch
) -> None:
    data = tmp_path / "data"
    store = TaskStore(data)
    task = store.create_task(TaskSpec(
        goal="goal", scope=[str(tmp_path)], acceptance=["tests pass"]
    ))
    store.record_outcome(task.id, StageOutcome(
        stage="verify", status="failed", summary="requires-repair"
    ))
    store.record_execution_provenance(
        task.id, mode="live", implementer="cursor", verifier="codex",
        reviewer="antigravity", profile_digest="live-profile-a",
    )
    store.transition(task.id, TaskState.SPEC, TaskState.FAILED_RECOVERABLE, "test")
    monkeypatch.setattr(
        cli_module, "FakeAgent",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fake adapter constructed")),
    )

    missing_profile = runner.invoke(app, ["resume", "--data-root", str(data), task.id])
    downgrade = runner.invoke(app, [
        "resume", "--profile", "fake", "--data-root", str(data), task.id
    ])

    assert missing_profile.exit_code != 0
    assert downgrade.exit_code != 0
    assert store.load(task.id).state is TaskState.FAILED_RECOVERABLE
    assert store.runtime(task.id).repair_attempts == 0


def _live_profile(path: Path, *, cursor_command: str = "cursor", deepseek: bool = False) -> dict:
    path.write_text(f'''\
[agents.cursor]
command=["{cursor_command}"]
estimated_usd=0.5
[agents.codex]
command=["codex"]
estimated_usd=0.5
[agents.antigravity]
command=["agy"]
estimated_usd=0.5
[agents.deepseek]
enabled={str(deepseek).lower()}
model="deepseek-v4-flash"
base_url="https://api.deepseek.com"
estimated_usd=0.5
probe_estimated_usd=0.25
''', encoding="utf-8")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_resume_rejects_mismatched_live_profile_before_adapter_construction(
    tmp_path: Path, monkeypatch
) -> None:
    data = tmp_path / "data"
    profile_a = tmp_path / "a.toml"
    profile_b = tmp_path / "b.toml"
    config_a = _live_profile(profile_a, cursor_command="cursor-a")
    _live_profile(profile_b, cursor_command="cursor-b")
    store = TaskStore(data)
    task = store.create_task(TaskSpec(goal="goal", scope=[str(tmp_path)], acceptance=["x"]))
    store.record_outcome(task.id, StageOutcome(
        stage="verify", status="failed", summary="requires-repair"
    ))
    store.record_execution_provenance(
        task.id, mode="live", implementer="cursor", verifier="codex",
        reviewer="antigravity", profile_digest=cli_module._profile_digest(config_a),
    )
    store.transition(task.id, TaskState.SPEC, TaskState.FAILED_RECOVERABLE, "test")
    monkeypatch.setattr(
        cli_module, "CursorAdapter",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("adapter constructed")),
    )

    result = runner.invoke(app, [
        "resume", "--profile", str(profile_b), "--live-confirmed", "--billing-confirmed",
        "--data-root", str(data), task.id,
    ])

    assert result.exit_code != 0
    assert store.runtime(task.id).repair_attempts == 0


def test_deepseek_origin_resume_reconstructs_deepseek_not_cursor(tmp_path: Path, monkeypatch) -> None:
    data = tmp_path / "data"
    profile = tmp_path / "deepseek.toml"
    config = _live_profile(profile, deepseek=True)
    store = TaskStore(data)
    task = store.create_task(TaskSpec(goal="goal", scope=[str(tmp_path)], acceptance=["x"]))
    store.record_outcome(task.id, StageOutcome(
        stage="verify", status="failed", summary="requires-repair"
    ))
    store.record_execution_provenance(
        task.id, mode="live", implementer="deepseek", verifier="codex",
        reviewer="antigravity", profile_digest=cli_module._profile_digest(config),
    )
    store.transition_recoverable(task.id, TaskState.SPEC, "verify", "test")
    constructed: list[str] = []
    monkeypatch.setattr(
        cli_module, "CursorAdapter",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Cursor substituted")),
    )
    monkeypatch.setattr(
        cli_module, "DeepSeekAdapter",
        lambda *args, **kwargs: constructed.append("deepseek") or object(),
    )
    monkeypatch.setattr(cli_module, "CodexAdapter", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli_module, "AntigravityAdapter", lambda *args, **kwargs: object())

    class StubOrchestrator:
        def __init__(self, *args, **kwargs): pass
        def resume_until_blocked(self, task_id): return TaskState.APPROVAL

    monkeypatch.setattr(cli_module, "Orchestrator", StubOrchestrator)
    result = runner.invoke(app, [
        "resume", "--profile", str(profile), "--live-confirmed", "--billing-confirmed",
        "--data-root", str(data), task.id,
    ])

    assert result.exit_code == 0, result.output
    assert constructed == ["deepseek"]


class DeepSeekResumeClient:
    def __init__(self, store, task_id, *, available: bool = True) -> None:
        self.store = store
        self.task_id = task_id
        self.available = available
        self.calls = []
        self.implementation_calls = 0
        self.smoke_complete = False
        self.models = type("Models", (), {"list": self.list_models})()
        self.chat = type("Chat", (), {"completions": type("Completions", (), {"create": self.create})()})()

    def _observe(self):
        with self.store._connect() as connection:
            row = connection.execute("SELECT lease_owner FROM task_runtime WHERE task_id=?", (self.task_id,)).fetchone()
        self.calls.append(row["lease_owner"] if row else None)

    def list_models(self):
        self._observe()
        if not self.available:
            raise RuntimeError("unavailable")
        return type("Response", (), {"data": [type("Model", (), {"id": "deepseek-v4-flash"})()]})()

    def create(self, **kwargs):
        self._observe()
        if not self.smoke_complete:
            content = json.dumps({"status": "ok"})
            self.smoke_complete = True
        else:
            content = json.dumps({"status": "passed", "evidence": [], "artifacts": [], "changes": [{"path": "candidate.txt", "action": "write", "content": "candidate"}]})
            self.implementation_calls += 1
        return type("Response", (), {"choices": [type("Choice", (), {"message": type("Message", (), {"content": content})()})()]})()


class ResumeStageRunner:
    def __init__(self, stage: str) -> None:
        self.stage = stage

    def run(self, argv, cwd, timeout, env_allowlist, stdin=None):
        payload = {"status": "passed", "evidence": [], "artifacts": []}
        if self.stage == "review":
            payload["findings"] = []
            return ProcessResult(0, json.dumps(payload | {"actual_usd": .1}), "", False)
        event = {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps(payload)},
        }
        return ProcessResult(0, json.dumps(event), "", False)


def _deepseek_resume_task(
    tmp_path: Path,
    profile: Path,
    *,
    max_usd: float = 5,
    max_agent_calls: int = 10,
):
    config = _live_profile(profile, deepseek=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "x@y"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    store = TaskStore(tmp_path / "data")
    task = store.create_task(TaskSpec(
        goal="x", scope=[str(repo)], acceptance=["x"],
        budget=Budget(
            max_agent_calls=max_agent_calls, max_minutes=60, max_usd=max_usd
        ),
    ))
    work = store.runs_root / task.id / "worktree"
    work.rmdir()
    workspace = GitWorkspace.create(repo, task.id, destination=work)
    store.set_workspace(task.id, str(repo), workspace.base_commit, f"triagent/{task.id}")
    store.record_outcome(task.id, StageOutcome(
        stage="implement", status="failed", summary="requires-repair"
    ))
    store.record_execution_provenance(
        task.id, mode="live", implementer="deepseek", verifier="codex",
        reviewer="antigravity", profile_digest=cli_module._profile_digest(config),
    )
    store.transition_recoverable(
        task.id, TaskState.SPEC, "implement", "test-implementation-failed"
    )
    return store, task


def test_deepseek_implementation_resume_passes_billed_readiness_and_uses_same_adapter(
    tmp_path: Path, monkeypatch
) -> None:
    profile = tmp_path / "deepseek.toml"
    store, task = _deepseek_resume_task(tmp_path, profile)
    deepseek_client = DeepSeekResumeClient(store, task.id)
    monkeypatch.setattr(
        cli_module, "CursorAdapter",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Cursor constructed")),
    )
    monkeypatch.setattr(
        cli_module, "DeepSeekAdapter",
        lambda *args, **kwargs: RealDeepSeekAdapter(
            client_factory=lambda **factory_kwargs: deepseek_client, api_key="secret", *args, **kwargs
        ),
    )
    monkeypatch.setattr(
        cli_module, "CodexAdapter",
        lambda *args, **kwargs: RealCodexAdapter(
            runner=ResumeStageRunner("verify"), *args, **kwargs
        ),
    )
    monkeypatch.setattr(
        cli_module, "AntigravityAdapter",
        lambda *args, **kwargs: RealAntigravityAdapter(
            runner=ResumeStageRunner("review"), acl_verifier=lambda d, f: True,
            *args, **kwargs
        ),
    )

    result = runner.invoke(app, [
        "resume", "--profile", str(profile), "--live-confirmed", "--billing-confirmed",
        "--data-root", str(store.root), task.id,
    ])

    assert result.exit_code == 0, result.output
    assert store.load(task.id).state is TaskState.APPROVAL
    assert deepseek_client.implementation_calls == 1
    assert store.runtime(task.id).agent_calls == 4
    assert deepseek_client.calls and all(owner is not None for owner in deepseek_client.calls)


def test_deepseek_resume_unavailable_readiness_refuses_before_implementation(
    tmp_path: Path, monkeypatch
) -> None:
    profile = tmp_path / "deepseek.toml"
    store, task = _deepseek_resume_task(tmp_path, profile)
    checkpoint = store.recovery_checkpoint(task.id)
    deepseek_client = DeepSeekResumeClient(store, task.id, available=False)
    monkeypatch.setattr(
        cli_module, "DeepSeekAdapter",
        lambda *args, **kwargs: RealDeepSeekAdapter(
            client_factory=lambda **factory_kwargs: deepseek_client, api_key="secret", *args, **kwargs
        ),
    )

    result = runner.invoke(app, [
        "resume", "--profile", str(profile), "--live-confirmed", "--billing-confirmed",
        "--data-root", str(store.root), task.id,
    ])

    assert result.exit_code != 0
    assert deepseek_client.implementation_calls == 0
    assert store.load(task.id).state is TaskState.FAILED_RECOVERABLE
    assert store.runtime(task.id).repair_attempts == 0
    assert store.recovery_checkpoint(task.id) == checkpoint
    assert store.outcomes(task.id)["implement"].diagnostic == "deepseek-api-failed"


def test_deepseek_resume_probe_budget_refuses_without_probe_or_implementation(
    tmp_path: Path, monkeypatch
) -> None:
    profile = tmp_path / "deepseek.toml"
    store, task = _deepseek_resume_task(tmp_path, profile, max_usd=.1)
    checkpoint = store.recovery_checkpoint(task.id)
    deepseek_client = DeepSeekResumeClient(store, task.id)
    monkeypatch.setattr(
        cli_module, "DeepSeekAdapter",
        lambda *args, **kwargs: RealDeepSeekAdapter(
            client_factory=lambda **factory_kwargs: deepseek_client, api_key="secret", *args, **kwargs
        ),
    )

    result = runner.invoke(app, [
        "resume", "--profile", str(profile), "--live-confirmed", "--billing-confirmed",
        "--data-root", str(store.root), task.id,
    ])

    assert result.exit_code != 0
    assert deepseek_client.calls == []
    assert store.load(task.id).state is TaskState.FAILED_RECOVERABLE
    assert store.runtime(task.id).repair_attempts == 0
    assert store.recovery_checkpoint(task.id) == checkpoint


def test_deepseek_resume_one_remaining_call_slot_refuses_before_probe(
    tmp_path: Path, monkeypatch
) -> None:
    profile = tmp_path / "deepseek.toml"
    store, task = _deepseek_resume_task(
        tmp_path, profile, max_agent_calls=1
    )
    checkpoint = store.recovery_checkpoint(task.id)
    deepseek_client = DeepSeekResumeClient(store, task.id)
    monkeypatch.setattr(
        cli_module, "DeepSeekAdapter",
        lambda *args, **kwargs: RealDeepSeekAdapter(
            client_factory=lambda **factory_kwargs: deepseek_client, api_key="secret", *args, **kwargs
        ),
    )

    result = runner.invoke(app, [
        "resume", "--profile", str(profile), "--live-confirmed", "--billing-confirmed",
        "--data-root", str(store.root), task.id,
    ])

    assert result.exit_code != 0
    assert deepseek_client.calls == []
    assert store.runtime(task.id).agent_calls == 0
    assert store.runtime(task.id).repair_attempts == 0
    assert store.load(task.id).state is TaskState.FAILED_RECOVERABLE
    assert store.recovery_checkpoint(task.id) == checkpoint


def test_deepseek_resume_combined_usd_shortfall_refuses_before_probe(
    tmp_path: Path, monkeypatch
) -> None:
    profile = tmp_path / "deepseek.toml"
    store, task = _deepseek_resume_task(tmp_path, profile, max_usd=.6)
    checkpoint = store.recovery_checkpoint(task.id)
    deepseek_client = DeepSeekResumeClient(store, task.id)
    monkeypatch.setattr(
        cli_module, "DeepSeekAdapter",
        lambda *args, **kwargs: RealDeepSeekAdapter(
            client_factory=lambda **factory_kwargs: deepseek_client, api_key="secret", *args, **kwargs
        ),
    )

    result = runner.invoke(app, [
        "resume", "--profile", str(profile), "--live-confirmed", "--billing-confirmed",
        "--data-root", str(store.root), task.id,
    ])

    assert result.exit_code != 0
    assert deepseek_client.calls == []
    assert store.runtime(task.id).agent_calls == 0
    assert store.runtime(task.id).repair_attempts == 0
    assert store.load(task.id).state is TaskState.FAILED_RECOVERABLE
    assert store.recovery_checkpoint(task.id) == checkpoint


def test_stale_same_stage_deepseek_resume_loser_makes_no_paid_probe(tmp_path: Path) -> None:
    profile = tmp_path / "deepseek.toml"
    store, task = _deepseek_resume_task(tmp_path, profile)
    expected = store.recovery_checkpoint(task.id)
    deepseek_client = DeepSeekResumeClient(store, task.id)
    config = tomllib.loads(profile.read_text(encoding="utf-8"))
    orchestrator = Orchestrator(
        store,
        RealDeepSeekAdapter(
            client_factory=lambda **factory_kwargs: deepseek_client, api_key="secret",
            enabled=True, billing_confirmed=True, live_confirmed=True, estimated_usd=.2,
        ),
        RealCodexAdapter(runner=ResumeStageRunner("verify"), command=["codex"], estimated_usd=.1),
        RealAntigravityAdapter(
            runner=ResumeStageRunner("review"), command=["review"], estimated_usd=.1,
            acl_verifier=lambda d, f: True,
        ),
        profile_digest=cli_module._profile_digest(config),
        expected_recovery_checkpoint=(expected["stage"], expected["sequence"]),
        implementer_probe_estimated_usd=.2,
    )
    store.transition_recoverable(
        task.id, TaskState.FAILED_RECOVERABLE, "implement", "newer-same-stage-failure"
    )

    with pytest.raises(ValueError, match="checkpoint changed"):
        orchestrator.resume_until_blocked(task.id)

    assert deepseek_client.calls == []
    assert store.runtime(task.id).agent_calls == 0
    assert store.load(task.id).state is TaskState.FAILED_RECOVERABLE
    assert store.recovery_checkpoint(task.id)["sequence"] > expected["sequence"]


def test_resume_cli_real_profile_requires_gates_and_available_configuration(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = TaskStore(data)
    task = store.create_task(TaskSpec(
        goal="goal", scope=[str(tmp_path)], acceptance=["tests pass"]
    ))
    store.record_outcome(task.id, StageOutcome(
        stage="implement", status="failed", summary="requires-repair"
    ))
    store.transition(task.id, TaskState.SPEC, TaskState.FAILED_RECOVERABLE, "test")
    missing = tmp_path / "missing.toml"

    gated = runner.invoke(app, ["resume", "--profile", str(missing), "--data-root", str(data), task.id])
    unavailable = runner.invoke(app, [
        "resume", "--profile", str(missing), "--live-confirmed", "--billing-confirmed",
        "--data-root", str(data), task.id,
    ])

    assert gated.exit_code != 0
    assert unavailable.exit_code != 0
    assert store.load(task.id).state is TaskState.FAILED_RECOVERABLE
    assert store.runtime(task.id).repair_attempts == 0


def test_fake_run_reaches_approval_writes_safe_report_and_preserves_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "sample-repo"
    repo.mkdir()
    tracked = repo / "README.md"
    tracked.write_text("unchanged\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "fake@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Fake Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    result = runner.invoke(app, ["run", *structured_args(), "--profile", "fake", "--data-root", str(tmp_path / "data"), str(repo), "add a health endpoint"])
    assert result.exit_code == 0, result.output
    assert "State: APPROVAL" in result.output
    reports = list((tmp_path / "data" / "runs").glob("*/final-report.md"))
    assert len(reports) == 1
    report = reports[0].read_text(encoding="utf-8")
    assert all(f"## {field}" in report for field in REPORT_FIELDS)
    assert "chain-of-thought" not in report.lower()
    assert tracked.read_text(encoding="utf-8") == "unchanged\n"


def test_exact_fixture_run_uses_persistent_git_worktree_and_preserves_source(tmp_path: Path) -> None:
    fixture_source = Path("tests/fixtures/sample-repo/README.md").resolve()
    source_before = fixture_source.read_text(encoding="utf-8")
    fixture = tmp_path / "sample-repo"
    fixture.mkdir()
    (fixture / "README.md").write_text(source_before, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=fixture, check=True)
    subprocess.run(["git", "config", "user.email", "fake@example.invalid"], cwd=fixture, check=True)
    subprocess.run(["git", "config", "user.name", "Fake Test"], cwd=fixture, check=True)
    subprocess.run(["git", "add", "README.md"], cwd=fixture, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=fixture, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=fixture, check=True, capture_output=True, text=True).stdout.strip()
    result = runner.invoke(app, ["run", *structured_args(), "--profile", "fake", "--data-root", str(tmp_path / "data"), str(fixture), "add a health endpoint"])
    assert result.exit_code == 0, result.output
    task_id = result.output.split("Task: ", 1)[1].splitlines()[0]
    isolated = tmp_path / "data" / "runs" / task_id / "worktree"
    assert (isolated / ".git").exists()
    assert (isolated / "health-endpoint.txt").read_text(encoding="utf-8") == "ok\n"
    assert (fixture / "README.md").read_text(encoding="utf-8") == source_before
    assert fixture_source.read_text(encoding="utf-8") == source_before
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=fixture, check=True, capture_output=True, text=True).stdout.strip() == head
    subprocess.run(["git", "worktree", "remove", "--force", str(isolated)], cwd=fixture, check=True)


def test_exact_default_root_invocation_isolated_and_preserves_fixture(tmp_path: Path, monkeypatch) -> None:
    fixture = tmp_path / "tests" / "fixtures" / "sample-repo"
    fixture.mkdir(parents=True)
    readme = fixture / "README.md"
    readme.write_text("fixture source\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=fixture, check=True)
    subprocess.run(["git", "config", "user.email", "fake@example.invalid"], cwd=fixture, check=True)
    subprocess.run(["git", "config", "user.name", "Fake Test"], cwd=fixture, check=True)
    subprocess.run(["git", "add", "README.md"], cwd=fixture, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=fixture, check=True)
    source_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=fixture, check=True, capture_output=True, text=True).stdout.strip()

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["run", *structured_args(), "--profile", "fake", "tests/fixtures/sample-repo", "add a health endpoint"])

    assert result.exit_code == 0, result.output
    assert "State: APPROVAL" in result.output
    task_id = result.output.split("Task: ", 1)[1].splitlines()[0]
    run_dir = tmp_path / ".triagent" / "runs" / task_id
    isolated = run_dir / "worktree"
    assert (run_dir / "final-report.md").exists()
    assert (isolated / ".git").exists()
    assert (isolated / "health-endpoint.txt").read_text(encoding="utf-8") == "ok\n"
    assert readme.read_text(encoding="utf-8") == "fixture source\n"
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=fixture, check=True, capture_output=True, text=True).stdout.strip() == source_head
    assert subprocess.run(["git", "status", "--porcelain"], cwd=fixture, check=True, capture_output=True, text=True).stdout == ""
    subprocess.run(["git", "worktree", "remove", "--force", str(isolated)], cwd=fixture, check=True)


def test_legacy_report_redacts_credentials(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-environment-secret")
    report = render_report({
        "state": "APPROVAL",
        "user outcome": "structured outcome",
        "tests": "api_key=unknown-credential",
        "independent review": "token: sk-environment-secret",
    })
    assert "unknown-credential" not in report
    assert "sk-environment-secret" not in report


def test_create_status_approve_report_and_doctor_are_operator_readable(tmp_path: Path) -> None:
    data = tmp_path / "data"
    created = runner.invoke(app, ["create", *structured_args(), "--data-root", str(data), str(tmp_path), "document the project"])
    assert created.exit_code == 0
    task_id = created.output.split("Task: ", 1)[1].splitlines()[0]
    status = runner.invoke(app, ["status", "--data-root", str(data), task_id])
    assert status.exit_code == 0 and "State: SPEC" in status.output
    denied = runner.invoke(app, ["approve", "--data-root", str(data), task_id, "visual"])
    assert denied.exit_code != 0
    report = runner.invoke(app, ["report", "--data-root", str(data), task_id])
    assert report.exit_code == 0 and "## state" in report.output
    doctor = runner.invoke(app, ["doctor", "--profile", "fake"])
    assert doctor.exit_code == 0 and "fake: ready" in doctor.output.lower()


def test_doctor_profile_lists_safe_vendor_capability_summaries(monkeypatch, tmp_path: Path) -> None:
    profile = tmp_path / "profile.toml"
    profile.write_text(
        """
[agents.codex]
command = ["custom-codex"]
[agents.cursor]
command = ["custom-cursor"]
[agents.antigravity]
command = ["custom-agy"]
[agents.deepseek]
enabled = false
model = "deepseek-v4-flash"
base_url = "https://api.deepseek.com"
""".strip(),
        encoding="utf-8",
    )
    calls: list[str] = []
    commands: dict[str, list[str]] = {}

    def adapter(name: str, *, installed: bool, authenticated: bool | None, ready: bool | None):
        class Stub:
            def __init__(self, *args, command=None, **kwargs):
                if command is not None:
                    commands[name] = list(command)
                if name == "deepseek":
                    assert kwargs["model"] == "deepseek-v4-flash"
                    assert kwargs["base_url"] == "https://api.deepseek.com"

            def capabilities(self):
                calls.append(name)
                return SimpleNamespace(
                    installed=installed,
                    authenticated=authenticated,
                    ready=ready,
                )

        return Stub

    monkeypatch.setattr(cli_module, "CodexAdapter", adapter("codex", installed=True, authenticated=True, ready=True))
    monkeypatch.setattr(cli_module, "CursorAdapter", adapter("cursor", installed=False, authenticated=False, ready=False))
    monkeypatch.setattr(cli_module, "AntigravityAdapter", adapter("antigravity", installed=True, authenticated=None, ready=None))
    monkeypatch.setattr(cli_module, "DeepSeekAdapter", adapter("deepseek", installed=None, authenticated=None, ready=False))

    result = runner.invoke(app, ["doctor", "--profile", str(profile)])

    assert result.exit_code == 0, result.output
    assert calls == ["codex", "cursor", "antigravity", "deepseek"]
    assert commands == {
        "codex": ["custom-codex"],
        "cursor": ["custom-cursor"],
        "antigravity": ["custom-agy"],
    }
    for name in calls:
        assert name in result.output.lower()
    assert "installed=yes" in result.output.lower()
    assert "installed=no" in result.output.lower()
    assert "authenticated=yes" in result.output.lower()
    assert "authenticated=unknown" in result.output.lower()
    assert "ready=unknown" in result.output.lower()
    assert "credential" not in result.output.lower()

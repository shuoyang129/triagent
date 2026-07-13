from pathlib import Path
import subprocess
from types import SimpleNamespace

from typer.testing import CliRunner

from triagent.cli import app
import triagent.cli as cli_module
from triagent.domain import RiskLevel, StageOutcome, TaskSpec, TaskState
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
    store.transition(task.id, TaskState.SPEC, TaskState.FAILED_RECOVERABLE, "test")

    result = runner.invoke(app, [
        "resume", "--profile", "fake", "--data-root", str(data), task.id
    ])

    assert result.exit_code == 0, result.output
    assert f"Task: {task.id}" in result.output
    assert "State: APPROVAL" in result.output
    assert store.runtime(task.id).repair_attempts == 1


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
[agents.opencode]
enabled = false
command = ["custom-opencode"]
""".strip(),
        encoding="utf-8",
    )
    calls: list[str] = []
    commands: dict[str, list[str]] = {}

    def adapter(name: str, *, installed: bool, authenticated: bool | None, ready: bool | None):
        class Stub:
            def __init__(self, *args, command, **kwargs):
                commands[name] = list(command)
                if name == "opencode/deepseek":
                    assert kwargs["probe_installed"] is True

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
    monkeypatch.setattr(cli_module, "DeepSeekAdapter", adapter("opencode/deepseek", installed=None, authenticated=None, ready=False))

    result = runner.invoke(app, ["doctor", "--profile", str(profile)])

    assert result.exit_code == 0, result.output
    assert calls == ["codex", "cursor", "antigravity", "opencode/deepseek"]
    assert commands == {
        "codex": ["custom-codex"],
        "cursor": ["custom-cursor"],
        "antigravity": ["custom-agy"],
        "opencode/deepseek": ["custom-opencode"],
    }
    for name in calls:
        assert name in result.output.lower()
    assert "installed=yes" in result.output.lower()
    assert "installed=no" in result.output.lower()
    assert "authenticated=yes" in result.output.lower()
    assert "authenticated=unknown" in result.output.lower()
    assert "ready=unknown" in result.output.lower()
    assert "credential" not in result.output.lower()

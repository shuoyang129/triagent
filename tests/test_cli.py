from pathlib import Path
import subprocess
from types import SimpleNamespace

from typer.testing import CliRunner

from triagent.cli import app
import triagent.cli as cli_module
from triagent.report import REPORT_FIELDS
from triagent.report import render_report


runner = CliRunner()


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
    result = runner.invoke(app, ["run", "--profile", "fake", "--data-root", str(tmp_path / "data"), str(repo), "add a health endpoint"])
    assert result.exit_code == 0, result.output
    assert "State: APPROVAL" in result.output
    reports = list((tmp_path / "data" / "runs").glob("*/final-report.md"))
    assert len(reports) == 1
    report = reports[0].read_text(encoding="utf-8")
    assert all(f"## {field}" in report for field in REPORT_FIELDS)
    assert "chain-of-thought" not in report.lower()
    assert tracked.read_text(encoding="utf-8") == "unchanged\n"


def test_exact_fixture_run_uses_persistent_git_worktree_and_preserves_source(tmp_path: Path) -> None:
    fixture = Path("tests/fixtures/sample-repo").resolve()
    before = (fixture / "README.md").read_text(encoding="utf-8")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=fixture, check=True, capture_output=True, text=True).stdout.strip()
    result = runner.invoke(app, ["run", "--profile", "fake", "--data-root", str(tmp_path / "data"), str(fixture), "add a health endpoint"])
    assert result.exit_code == 0, result.output
    task_id = result.output.split("Task: ", 1)[1].splitlines()[0]
    isolated = tmp_path / "data" / "runs" / task_id / "worktree"
    assert (isolated / ".git").exists()
    assert (isolated / "health-endpoint.txt").read_text(encoding="utf-8") == "ok\n"
    assert (fixture / "README.md").read_text(encoding="utf-8") == before
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
    result = runner.invoke(app, ["run", "--profile", "fake", "tests/fixtures/sample-repo", "add a health endpoint"])

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


def test_report_redacts_reasoning_and_credentials(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-environment-secret")
    report = render_report({
        "state": "APPROVAL",
        "user outcome": "chain-of-thought: private deliberation",
        "tests": "api_key=unknown-credential",
        "independent review": "token: sk-environment-secret",
    })
    assert "private deliberation" not in report
    assert "unknown-credential" not in report
    assert "sk-environment-secret" not in report


def test_create_status_approve_report_and_doctor_are_operator_readable(tmp_path: Path) -> None:
    data = tmp_path / "data"
    created = runner.invoke(app, ["create", "--data-root", str(data), str(tmp_path), "document the project"])
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
    profile.write_text("[agents.opencode]\nenabled = false\n", encoding="utf-8")
    calls: list[str] = []

    def adapter(name: str, *, available: bool, authenticated: bool):
        class Stub:
            def __init__(self, *args, **kwargs):
                pass

            def capabilities(self):
                calls.append(name)
                return SimpleNamespace(
                    available=available,
                    authenticated=authenticated,
                    headless=True,
                    version=f"{name}-version",
                )

        return Stub

    monkeypatch.setattr(cli_module, "CodexAdapter", adapter("codex", available=True, authenticated=True))
    monkeypatch.setattr(cli_module, "CursorAdapter", adapter("cursor", available=False, authenticated=False))
    monkeypatch.setattr(cli_module, "AntigravityAdapter", adapter("antigravity", available=True, authenticated=True))
    monkeypatch.setattr(cli_module, "DeepSeekAdapter", adapter("opencode/deepseek", available=False, authenticated=False))

    result = runner.invoke(app, ["doctor", "--profile", str(profile)])

    assert result.exit_code == 0, result.output
    assert calls == ["codex", "cursor", "antigravity", "opencode/deepseek"]
    for name in calls:
        assert name in result.output.lower()
    assert "available=yes" in result.output.lower()
    assert "available=no" in result.output.lower()
    assert "authenticated=yes" in result.output.lower()
    assert "credential" not in result.output.lower()

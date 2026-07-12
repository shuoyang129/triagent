from pathlib import Path

from typer.testing import CliRunner

from triagent.cli import app
from triagent.report import REPORT_FIELDS


runner = CliRunner()


def test_fake_run_reaches_approval_writes_safe_report_and_preserves_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "sample-repo"
    repo.mkdir()
    tracked = repo / "README.md"
    tracked.write_text("unchanged\n", encoding="utf-8")
    result = runner.invoke(app, ["run", "--profile", "fake", "--data-root", str(tmp_path / "data"), str(repo), "add a health endpoint"])
    assert result.exit_code == 0, result.output
    assert "State: APPROVAL" in result.output
    reports = list((tmp_path / "data" / "runs").glob("*/final-report.md"))
    assert len(reports) == 1
    report = reports[0].read_text(encoding="utf-8")
    assert all(f"## {field}" in report for field in REPORT_FIELDS)
    assert "chain-of-thought" not in report.lower()
    assert tracked.read_text(encoding="utf-8") == "unchanged\n"


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

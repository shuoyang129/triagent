import os
import sys
from pathlib import Path

from triagent.adapters.base import AgentRequest, AgentRole, AgentStatus
from triagent.adapters.fake import FakeAgent
from triagent.adapters.process import ProcessRunner


def test_process_runner_redacts_secret(tmp_path: Path) -> None:
    runner = ProcessRunner(redactions=["super-secret"])
    result = runner.run(
        [sys.executable, "-c", "print('super-secret')"],
        cwd=tmp_path,
        timeout=5,
        env_allowlist={},
    )
    assert result.returncode == 0
    assert "super-secret" not in result.stdout
    assert "[REDACTED]" in result.stdout


def test_process_runner_times_out(tmp_path: Path) -> None:
    runner = ProcessRunner()
    result = runner.run(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        cwd=tmp_path,
        timeout=0.05,
        env_allowlist={},
    )
    assert result.timed_out is True
    assert result.returncode is None


def test_process_runner_only_passes_allowlisted_environment(tmp_path: Path) -> None:
    os.environ["TRIAGENT_TEST_SECRET"] = "must-not-leak"
    runner = ProcessRunner()
    result = runner.run(
        [sys.executable, "-c", "import os; print(os.getenv('TRIAGENT_TEST_SECRET', 'missing'))"],
        cwd=tmp_path,
        timeout=5,
        env_allowlist={"PATH": os.environ.get("PATH", "")},
    )
    assert result.stdout.strip() == "missing"


def test_fake_agent_returns_scripted_result(tmp_path: Path) -> None:
    agent = FakeAgent.succeeding("implemented")
    request = AgentRequest(
        role=AgentRole.IMPLEMENTER,
        task_file=tmp_path / "task.yaml",
        workdir=tmp_path,
        output_schema="implementation-result-v1",
        timeout_seconds=30,
    )
    result = agent.run(request)
    assert result.status is AgentStatus.SUCCEEDED
    assert result.summary == "implemented"
    assert agent.requests == [request]

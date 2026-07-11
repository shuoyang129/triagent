from __future__ import annotations

import json
from pathlib import Path

import pytest

from triagent.adapters.antigravity import AntigravityAdapter
from triagent.adapters.base import AgentRequest, AgentRole, AgentStatus
from triagent.adapters.codex import CodexAdapter
from triagent.adapters.cursor import CursorAdapter
from triagent.adapters.deepseek import DeepSeekAdapter
from triagent.adapters.process import ProcessResult


class FakeRunner:
    def __init__(self, *results: ProcessResult | BaseException) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], Path, float, dict[str, str]]] = []

    def run(self, argv, cwd, timeout, env_allowlist):
        self.calls.append((list(argv), Path(cwd), timeout, dict(env_allowlist)))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> ProcessResult:
    return ProcessResult(returncode, stdout, stderr, False)


@pytest.fixture
def agent_request(tmp_path: Path) -> AgentRequest:
    task = tmp_path / "task.txt"
    task.write_text("fix the quoted value: '$(touch nope)'", encoding="utf-8")
    return AgentRequest(
        role=AgentRole.IMPLEMENTER,
        task_file=task,
        workdir=tmp_path,
        output_schema="result-v1",
        timeout_seconds=10,
    )


def test_codex_capabilities_use_read_only_probes(tmp_path: Path) -> None:
    runner = FakeRunner(completed("codex-cli 1.2\n"), completed("Logged in\n"))
    capabilities = CodexAdapter(runner=runner).capabilities()
    assert capabilities.available and capabilities.authenticated and capabilities.headless
    assert capabilities.version == "codex-cli 1.2"
    assert [call[0] for call in runner.calls] == [
        ["codex.exe", "--version"],
        ["codex.exe", "login", "status"],
    ]


def test_missing_binary_is_unavailable(agent_request: AgentRequest) -> None:
    runner = FakeRunner(FileNotFoundError("not installed"))
    result = CodexAdapter(runner=runner).run(agent_request)
    assert result.status is AgentStatus.UNAVAILABLE


@pytest.mark.parametrize(
    ("process", "status"),
    [
        (ProcessResult(None, "", "", True), AgentStatus.TIMED_OUT),
        (completed(returncode=2, stderr="bad invocation"), AgentStatus.FAILED),
        (completed(returncode=1, stderr="authentication required"), AgentStatus.UNAVAILABLE),
        (completed("not json"), AgentStatus.INVALID_OUTPUT),
    ],
)
def test_codex_maps_process_failures(agent_request: AgentRequest, process: ProcessResult, status: AgentStatus) -> None:
    result = CodexAdapter(runner=FakeRunner(process)).run(agent_request)
    assert result.status is status


def test_codex_runs_noninteractively_and_parses_json(agent_request: AgentRequest) -> None:
    runner = FakeRunner(completed(json.dumps({"summary": "done", "changed": True})))
    result = CodexAdapter(runner=runner).run(agent_request)
    argv = runner.calls[0][0]
    assert argv[:4] == ["codex.exe", "exec", "--sandbox", "read-only"]
    assert "--json" in argv
    assert result.status is AgentStatus.SUCCEEDED
    assert result.summary == "done"
    assert result.data["changed"] is True


def test_cursor_uses_wsl_argv_and_does_not_interpolate_prompt(agent_request: AgentRequest) -> None:
    runner = FakeRunner(completed('{"summary":"ok"}'))
    result = CursorAdapter(runner=runner).run(agent_request)
    argv = runner.calls[0][0]
    prompt = agent_request.task_file.read_text(encoding="utf-8")
    assert argv[:5] == ["wsl.exe", "--distribution", "Ubuntu-24.04", "--exec", "bash"]
    assert argv[-1] == prompt
    assert "$(touch nope)" not in argv[6]
    assert result.status is AgentStatus.SUCCEEDED


def test_cursor_deepseek_gates_are_independent_and_all_required() -> None:
    runner = FakeRunner(
        completed("cursor-agent 1"),
        completed("authenticated"),
        completed('{"models":["deepseek-v3"]}'),
        completed('{"summary":"tool smoke ok"}'),
    )
    caps = CursorAdapter(runner=runner, deepseek_billing_confirmed=False).capabilities()
    assert caps.deepseek_model_listed is True
    assert caps.deepseek_agent_smoke_test is True
    assert caps.deepseek_billing_confirmed is False
    assert caps.deepseek_byok_available is False


def test_antigravity_print_mode_never_skips_permissions(agent_request: AgentRequest) -> None:
    runner = FakeRunner(completed('{"summary":"ok"}'))
    result = AntigravityAdapter(runner=runner).run(agent_request)
    argv = runner.calls[0][0]
    assert argv[:2] == ["agy.exe", "--print"]
    assert "--dangerously-skip-permissions" not in argv
    assert result.status is AgentStatus.SUCCEEDED


def test_deepseek_defaults_disabled_without_running_probes() -> None:
    runner = FakeRunner()
    caps = DeepSeekAdapter(runner=runner).capabilities()
    assert caps.available is False
    assert caps.enabled is False
    assert runner.calls == []


def test_deepseek_requires_every_independent_gate() -> None:
    runner = FakeRunner(
        completed("opencode 1"),
        completed('{"reachable":true}'),
        completed('{"models":["deepseek/deepseek-chat"]}'),
        completed('{"summary":"agent tool passed"}'),
    )
    caps = DeepSeekAdapter(runner=runner, enabled=True, billing_confirmed=False).capabilities()
    assert caps.api_configured_reachable is True
    assert caps.model_listed is True
    assert caps.agent_tool_smoke_test is True
    assert caps.billing_confirmed is False
    assert caps.available is False


def test_deepseek_is_available_when_all_gates_pass() -> None:
    runner = FakeRunner(
        completed("opencode 1"),
        completed('{"reachable":true}'),
        completed('{"models":["deepseek/deepseek-chat"]}'),
        completed('{"summary":"agent tool passed"}'),
    )
    caps = DeepSeekAdapter(runner=runner, enabled=True, billing_confirmed=True).capabilities()
    assert caps.available is True


@pytest.mark.live_cli
def test_live_codex_capabilities_only_when_selected(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("-m") or "live_cli" not in request.config.getoption("-m"):
        pytest.skip("select explicitly with -m live_cli")
    assert isinstance(CodexAdapter().capabilities().available, bool)

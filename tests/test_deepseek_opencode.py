from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from triagent.adapters.base import AgentRequest, AgentRole, AgentStatus
from triagent.adapters.deepseek import DeepSeekAdapter
from triagent.adapters.process import ProcessResult


MODEL = "deepseek/deepseek-v4-pro"


def completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> ProcessResult:
    return ProcessResult(returncode, stdout, stderr, False)


class OpenCodeRunner:
    def __init__(
        self,
        *,
        final: dict | None = None,
        final_fragments: list[str] | None = None,
        smoke_error: ProcessResult | None = None,
        run_error: ProcessResult | None = None,
    ) -> None:
        self.final = final or {
            "status": "passed",
            "evidence": ["updated base"],
            "artifacts": [],
            "changed_paths": ["base.txt"],
        }
        self.final_fragments = final_fragments
        self.smoke_error = smoke_error
        self.run_error = run_error
        self.calls: list[tuple[list[str], Path, float, dict[str, str], str | None]] = []

    def run(self, argv, cwd, timeout, env, stdin=None):
        self.calls.append((list(argv), Path(cwd), timeout, dict(env), stdin))
        if argv[-1] == "--version":
            return completed("1.18.4\n")
        if "models" in argv:
            return completed(f"{MODEL}\ndeepseek/deepseek-v4-flash\n")
        if "triagent-opencode-probe-" in argv[-1]:
            if self.smoke_error is not None:
                return self.smoke_error
            path = json.loads(re.search(r"PATH_JSON=(\".*?\") NONCE=", argv[-1]).group(1))
            nonce = argv[-1].split(" NONCE=", 1)[1]
            Path(path).write_text(nonce, encoding="utf-8")
            return completed('{"type":"text","part":{"type":"text","text":"ok"}}\n')
        if self.run_error is not None:
            return self.run_error
        fragments = self.final_fragments or [json.dumps(self.final)]
        events = [
            {
                "type": "text",
                "part": {"type": "text", "text": fragment},
            }
            for fragment in fragments
        ]
        return completed("".join(json.dumps(event) + "\n" for event in events))


def request(tmp_path: Path) -> AgentRequest:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    task = tmp_path / "task.json"
    task.write_text('{"goal":"change base"}', encoding="utf-8")
    return AgentRequest(
        role=AgentRole.IMPLEMENTER,
        agent_identity="deepseek",
        task_file=task,
        workdir=tmp_path,
        output_schema="implementation-result-v1",
        timeout_seconds=60,
    )


def ready_adapter(monkeypatch: pytest.MonkeyPatch, runner: OpenCodeRunner, probe_dir: Path) -> DeepSeekAdapter:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-value")
    adapter = DeepSeekAdapter(
        enabled=True,
        billing_confirmed=True,
        live_confirmed=True,
        command=["custom-opencode"],
        runner=runner,
        probe_dir=probe_dir,
    )
    assert adapter.capabilities().available is True
    return adapter


def test_opencode_deepseek_uses_pro_model_and_restricted_inline_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = OpenCodeRunner()
    ready_adapter(monkeypatch, runner, tmp_path / "probe")

    run_call = runner.calls[-1]
    assert run_call[0][:2] == ["custom-opencode", "run"]
    model_index = run_call[0].index("--model")
    assert ["--model", MODEL] == run_call[0][model_index : model_index + 2]
    config = json.loads(run_call[3]["OPENCODE_CONFIG_CONTENT"])
    assert config["enabled_providers"] == ["deepseek"]
    assert config["provider"]["deepseek"]["options"]["apiKey"] == "{env:DEEPSEEK_API_KEY}"
    permissions = config["agent"]["triagent"]["permission"]
    assert permissions["bash"] == "deny"
    assert permissions["webfetch"] == "deny"
    assert permissions["task"] == "deny"
    assert permissions["external_directory"] == "deny"
    assert permissions["skill"] == "deny"
    assert permissions["read"][".env"] == "deny"
    assert permissions["edit"][".git/*"] == "deny"


def test_opencode_deepseek_smoke_timeout_is_configurable_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-value")
    runner = OpenCodeRunner()
    adapter = DeepSeekAdapter(
        enabled=True,
        billing_confirmed=True,
        live_confirmed=True,
        smoke_timeout_seconds=180,
        runner=runner,
        probe_dir=tmp_path,
    )

    assert adapter.capabilities().available is True
    smoke_call = next(
        call for call in runner.calls if "triagent-opencode-probe-" in call[0][-1]
    )
    assert smoke_call[2] == 180

    for invalid in (True, 0, 301, float("inf"), "180"):
        with pytest.raises(ValueError, match="smoke timeout"):
            DeepSeekAdapter(smoke_timeout_seconds=invalid)


def test_opencode_deepseek_parses_json_event_and_uses_private_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = OpenCodeRunner()
    adapter = ready_adapter(monkeypatch, runner, tmp_path / "probe")

    result = adapter.run(request(tmp_path))

    assert result.status is AgentStatus.SUCCEEDED
    assert result.data["changed_paths"] == ["base.txt"]
    argv = runner.calls[-1][0]
    assert "--pure" in argv and "--format" in argv and "json" in argv
    assert argv[-1].startswith("--file=")
    prompt_path = Path(
        next(part.split("=", 1)[1] for part in argv if part.startswith("--file="))
    )
    assert not prompt_path.exists()
    assert prompt_path.parent == tmp_path
    assert "secret-value" not in " ".join(argv)


def test_opencode_deepseek_removes_unclaimed_generated_output_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class OutputFileRunner(OpenCodeRunner):
        def run(self, argv, cwd, timeout, env, stdin=None):
            result = super().run(argv, cwd, timeout, env, stdin)
            if "run" in argv and "--file=" in argv[-1]:
                (Path(cwd) / ".triagent-output.json").write_text(
                    json.dumps(self.final),
                    encoding="utf-8",
                )
            return result

    runner = OutputFileRunner()
    adapter = ready_adapter(monkeypatch, runner, tmp_path / "probe")

    result = adapter.run(request(tmp_path))

    assert result.status is AgentStatus.SUCCEEDED
    assert not (tmp_path / ".triagent-output.json").exists()


def test_opencode_deepseek_parses_fragmented_final_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = {
        "status": "passed",
        "evidence": ["updated base"],
        "artifacts": [],
        "changed_paths": ["base.txt"],
    }
    encoded = json.dumps(final)
    runner = OpenCodeRunner(
        final_fragments=["intermediate text", encoded[:17], encoded[17:41], encoded[41:]]
    )
    adapter = ready_adapter(monkeypatch, runner, tmp_path / "probe")

    result = adapter.run(request(tmp_path))

    assert result.status is AgentStatus.SUCCEEDED
    assert result.data["changed_paths"] == ["base.txt"]


def test_opencode_deepseek_recovers_tracked_edits_after_invalid_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = OpenCodeRunner(final_fragments=["not canonical json"])
    adapter = ready_adapter(monkeypatch, runner, tmp_path / "probe")
    agent_request = request(tmp_path)
    subprocess.run(["git", "add", "base.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test.com",
         "commit", "-q", "-m", "base"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "base.txt").write_text("changed\n", encoding="utf-8")

    result = adapter.run(agent_request)

    assert result.status is AgentStatus.SUCCEEDED
    assert result.data["changed_paths"] == ["base.txt"]
    assert result.data["summary_code"] == "completed"


def test_opencode_deepseek_rejects_fragmented_non_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = OpenCodeRunner(final_fragments=["not ", "canonical ", "json"])
    adapter = ready_adapter(monkeypatch, runner, tmp_path / "probe")

    result = adapter.run(request(tmp_path))

    assert result.status is AgentStatus.INVALID_OUTPUT


def test_opencode_implementation_maps_only_safe_failure_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = "HTTP 429 rate limit provider-detail-must-not-persist"
    runner = OpenCodeRunner(
        run_error=completed(stderr=raw, returncode=1)
    )
    adapter = ready_adapter(monkeypatch, runner, tmp_path / "probe")

    result = adapter.run(request(tmp_path))

    assert result.status is AgentStatus.FAILED
    assert result.data == {"diagnostic_code": "deepseek-rate-limited"}
    assert raw not in result.model_dump_json()


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("HTTP 401 unauthorized", "deepseek-authentication-failed"),
        ("HTTP 402 insufficient_balance", "deepseek-insufficient-balance"),
        ("HTTP 403 forbidden", "deepseek-permission-denied"),
        ("HTTP 429 rate limit", "deepseek-rate-limited"),
        ("HTTP 503 unavailable", "deepseek-service-unavailable"),
    ],
)
def test_opencode_readiness_maps_safe_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
    expected: str,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-value")
    runner = OpenCodeRunner(smoke_error=completed(stderr=stderr, returncode=1))
    caps = DeepSeekAdapter(
        enabled=True,
        billing_confirmed=True,
        live_confirmed=True,
        runner=runner,
        probe_dir=tmp_path,
    ).capabilities()
    assert caps.available is False
    assert caps.diagnostic_code == expected
    assert stderr not in caps.model_dump_json()


def test_disabled_deepseek_only_probes_local_opencode() -> None:
    runner = OpenCodeRunner()
    caps = DeepSeekAdapter(runner=runner).capabilities()
    assert caps.available is False
    assert caps.installed is True
    assert caps.diagnostic_code == "deepseek-disabled"
    assert len(runner.calls) == 1


def test_native_deepseek_arguments_are_rejected() -> None:
    with pytest.raises(TypeError, match="legacy native"):
        DeepSeekAdapter(base_url="https://api.deepseek.com")
    with pytest.raises(TypeError, match="legacy native"):
        DeepSeekAdapter(client_factory=object())

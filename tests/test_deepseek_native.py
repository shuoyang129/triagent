from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import triagent.adapters.deepseek as deepseek_module

from triagent.adapters.base import AgentRequest, AgentRole, AgentStatus
from triagent.adapters.deepseek import DeepSeekAdapter


class FakeClient:
    def __init__(self, final: dict, *, model: str = "deepseek-v4-flash") -> None:
        self.responses = [json.dumps({"status": "ok"}), json.dumps(final)]
        self.calls = []
        self.models = SimpleNamespace(
            list=lambda: SimpleNamespace(data=[SimpleNamespace(id=model)])
        )
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create)
        )

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=self.responses.pop(0))
            )]
        )


def request(tmp_path: Path) -> AgentRequest:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "x@y"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=tmp_path, check=True)
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
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


def adapter(client: FakeClient) -> DeepSeekAdapter:
    instance = DeepSeekAdapter(
        enabled=True,
        billing_confirmed=True,
        live_confirmed=True,
        api_key="secret-value",
        estimated_usd=1.0,
        client_factory=lambda **kwargs: client,
    )
    assert instance.capabilities().available is True
    return instance


def test_native_deepseek_applies_validated_text_changes(tmp_path: Path) -> None:
    client = FakeClient({
        "status": "passed",
        "evidence": ["updated base"],
        "artifacts": [],
        "changes": [
            {"path": "base.txt", "action": "write", "content": "changed\n"},
            {"path": "new.txt", "action": "write", "content": "new\n"},
        ],
    })

    result = adapter(client).run(request(tmp_path))

    assert result.status is AgentStatus.SUCCEEDED
    assert result.data["changed_paths"] == ["base.txt", "new.txt"]
    assert (tmp_path / "base.txt").read_text() == "changed\n"
    assert (tmp_path / "new.txt").read_text() == "new\n"
    prompt = client.calls[-1]["messages"][-1]["content"]
    assert "REPOSITORY_SNAPSHOT_JSON=" in prompt
    assert "secret-value" not in prompt


@pytest.mark.parametrize("path", ["../escape.txt", ".git/config", "/tmp/escape"])
def test_native_deepseek_rejects_unsafe_paths_without_mutation(
    tmp_path: Path, path: str
) -> None:
    client = FakeClient({
        "status": "passed", "evidence": [], "artifacts": [],
        "changes": [{"path": path, "action": "write", "content": "bad"}],
    })
    original = request(tmp_path)

    result = adapter(client).run(original)

    assert result.status is AgentStatus.INVALID_OUTPUT
    assert (tmp_path / "base.txt").read_text() == "base\n"
    assert not (tmp_path.parent / "escape.txt").exists()


def test_native_deepseek_rejects_failed_result_with_changes(tmp_path: Path) -> None:
    client = FakeClient({
        "status": "failed", "evidence": [], "artifacts": [],
        "changes": [{"path": "base.txt", "action": "write", "content": "bad"}],
    })
    result = adapter(client).run(request(tmp_path))
    assert result.status is AgentStatus.INVALID_OUTPUT
    assert (tmp_path / "base.txt").read_text() == "base\n"


def test_native_deepseek_cleans_temporary_file_after_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient({
        "status": "passed", "evidence": [], "artifacts": [],
        "changes": [{"path": "base.txt", "action": "write", "content": "changed\n"}],
    })
    original_replace = deepseek_module.os.replace

    def fail_temporary_replace(source: Path, target: Path) -> None:
        if Path(source).name.startswith(".triagent-deepseek-"):
            raise OSError("replace failed")
        original_replace(source, target)

    monkeypatch.setattr(deepseek_module.os, "replace", fail_temporary_replace)

    result = adapter(client).run(request(tmp_path))

    assert result.status is AgentStatus.FAILED
    assert result.data == {"diagnostic_code": "deepseek-local-failure"}
    assert (tmp_path / "base.txt").read_text() == "base\n"
    assert list(tmp_path.glob(".triagent-deepseek-*")) == []


def test_native_deepseek_rejects_non_official_base_url() -> None:
    with pytest.raises(ValueError, match="official HTTPS"):
        DeepSeekAdapter(base_url="https://example.com", api_key="secret")

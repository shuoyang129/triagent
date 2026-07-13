from __future__ import annotations

import subprocess

from typer.testing import CliRunner

import triagent.cli as cli_module
from triagent.adapters.base import AgentCapabilities,AgentRole
from triagent.adapters.fake import FakeAgent
from triagent.domain import TaskState
from triagent.store import TaskStore


def test_disabled_deepseek_fallback_is_never_constructed_or_called(tmp_path,monkeypatch):
    repo=tmp_path/"repo";repo.mkdir();subprocess.run(["git","init","-q"],cwd=repo,check=True)
    subprocess.run(["git","config","user.email","x@y"],cwd=repo,check=True);subprocess.run(["git","config","user.name","x"],cwd=repo,check=True)
    (repo/"a").write_text("a",encoding="utf-8");subprocess.run(["git","add","a"],cwd=repo,check=True);subprocess.run(["git","commit","-qm","base"],cwd=repo,check=True)
    profile=tmp_path/"profile.toml";profile.write_text('''
[agents.cursor]
command=["cursor"]
estimated_usd=0.5
[agents.codex]
command=["codex"]
estimated_usd=0.5
[agents.antigravity]
command=["agy"]
estimated_usd=0.5
[agents.deepseek]
enabled=false
command=["must-not-run"]
estimated_usd=0.5
probe_estimated_usd=0.25
[agents.opencode]
enabled=true
command=["also-must-not-run"]
estimated_usd=0.5
probe_estimated_usd=0.25
[budget]
max_agent_calls=10
max_minutes=60
max_usd=5
''',encoding="utf-8")
    calls={"factory":0,"capabilities":0,"run":0}
    class CursorStub(FakeAgent):
        identity="cursor";allowed_roles=frozenset({AgentRole.IMPLEMENTER})
        def capabilities(self):return AgentCapabilities(available=False)
    class CodexStub(FakeAgent):identity="codex";allowed_roles=frozenset({AgentRole.VERIFIER})
    class AntigravityStub(FakeAgent):identity="antigravity";allowed_roles=frozenset({AgentRole.REVIEWER})
    class DeepStub(FakeAgent):
        identity="deepseek";allowed_roles=frozenset({AgentRole.IMPLEMENTER})
        def capabilities(self):calls["capabilities"]+=1;return AgentCapabilities(available=True)
        def run(self,request):calls["run"]+=1;return super().run(request)
    def deep_factory(*args,**kwargs):calls["factory"]+=1;return DeepStub([])
    monkeypatch.setattr(cli_module,"CursorAdapter",lambda *a,**k:CursorStub([]));monkeypatch.setattr(cli_module,"CodexAdapter",lambda *a,**k:CodexStub([]));monkeypatch.setattr(cli_module,"AntigravityAdapter",lambda *a,**k:AntigravityStub([]));monkeypatch.setattr(cli_module,"DeepSeekAdapter",deep_factory)
    data=tmp_path/"data";result=CliRunner().invoke(cli_module.app,["run","--profile",str(profile),"--live-confirmed","--billing-confirmed","--data-root",str(data),str(repo),"x"])
    assert result.exit_code!=0 and "task setup failed" in result.output
    assert calls=={"factory":0,"capabilities":0,"run":0}
    task_id=next((data/"runs").iterdir()).name
    assert TaskStore(data).load(task_id).state is TaskState.FAILED_FINAL

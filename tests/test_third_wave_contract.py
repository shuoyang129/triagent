from __future__ import annotations
import json, subprocess
from pathlib import Path
import pytest
from triagent.adapters.base import AgentRequest,AgentRole,AgentStatus
from triagent.adapters.codex import CodexAdapter
from triagent.adapters.antigravity import AntigravityAdapter
from triagent.adapters.deepseek import DeepSeekAdapter
from triagent.adapters.process import ProcessResult
from triagent.domain import Budget,StageOutcome,TaskSpec,TaskState
from triagent.store import TaskStore
from triagent.git_workspace import GitWorkspace
from triagent.adapters.fake import FakeAgent
from triagent.orchestrator import Orchestrator

class Runner:
    def __init__(self,result=None): self.calls=[]; self.inputs=[]; self.paths=[]; self.result=result
    def run(self,argv,cwd,timeout,env,stdin=None):
        self.calls.append(list(argv))
        if stdin is not None:self.inputs.append(stdin.encode())
        elif "-p" in argv:
            path=Path(argv[-1].rsplit(": ",1)[1]); self.paths.append(path); self.inputs.append(path.read_bytes())
        if isinstance(self.result,BaseException): raise self.result
        return self.result

def spec(max_usd=5): return TaskSpec(goal="x",scope=["x"],acceptance=["x"],budget=Budget(max_usd=max_usd))

def test_interrupted_paid_call_charges_reserved_estimate(tmp_path):
    store=TaskStore(tmp_path); task=store.create_task(spec()); call=store.reserve_agent_call(task.id,1.25)
    store.interrupt_agent_call(task.id,call,"crash")
    assert store.runtime(task.id).usd_spent == pytest.approx(1.25)
    with store._connect() as c:
        row=c.execute("select estimated_usd,actual_usd,charged_usd,status from agent_calls where id=?",(call,)).fetchone()
    assert tuple(row)==(1.25,None,1.25,"interrupted")

def test_deepseek_capability_without_live_confirmation_calls_no_runner(tmp_path):
    runner=Runner(AssertionError("runner must not execute"))
    caps=DeepSeekAdapter(runner=runner,enabled=True,billing_confirmed=True,live_confirmed=False).capabilities()
    assert not caps.available and runner.calls == []

@pytest.mark.parametrize("adapter_cls,output",[
    (CodexAdapter,json.dumps({"type":"item.completed","item":{"type":"agent_message","text":json.dumps({"status":"passed"})}})),
    (AntigravityAdapter,json.dumps({"status":"passed","findings":[],"actual_usd":0.1}))])
def test_real_adapter_runner_receives_exact_task_and_handoff_bytes(tmp_path,adapter_cls,output):
    task=tmp_path/"task.json"; task.write_bytes(b'{"goal":"exact"}')
    handoff=tmp_path/"handoff.json"; handoff.write_bytes(json.dumps({"final_diff":"+exact"*20000}).encode())
    runner=Runner(ProcessResult(0,output,"",False)); adapter=adapter_cls(runner=runner,**({"acl_verifier":lambda directory,file:True} if adapter_cls is AntigravityAdapter else {}))
    role=AgentRole.VERIFIER if adapter_cls is CodexAdapter else AgentRole.REVIEWER
    req=AgentRequest(role=role,agent_identity=adapter.identity,task_file=task,handoff_file=handoff,workdir=tmp_path,output_schema="x",timeout_seconds=5)
    adapter.run(req)
    expected=b'TRIAGENT_INPUT_V1\nTASK\n{"goal":"exact"}\nHANDOFF\n'+handoff.read_bytes()
    assert runner.inputs[0] == expected and all(not path.exists() for path in runner.paths)
    assert expected.decode() not in " ".join(runner.calls[0])

def test_adapter_instance_cannot_be_relabeled():
    adapter=CodexAdapter(runner=Runner())
    with pytest.raises(AttributeError): adapter.identity="antigravity"
    with pytest.raises(AttributeError): adapter.allowed_roles=frozenset({AgentRole.IMPLEMENTER})

def test_strict_outcome_is_recursively_sanitized_before_sqlite(tmp_path,monkeypatch):
    monkeypatch.setenv("THIRD_WAVE_SECRET","persist-never")
    store=TaskStore(tmp_path); task=store.create_task(spec())
    store.record_outcome(task.id,StageOutcome(stage="verify",status="passed",summary="verified",evidence=["persist-never"]))
    with store._connect() as c: raw=c.execute("select outcome_json from stage_outcomes where task_id=?",(task.id,)).fetchone()[0]
    assert "persist-never" not in raw and "[REDACTED]" in raw

def test_overrun_atomically_finalizes_and_pauses(tmp_path):
    store=TaskStore(tmp_path); task=store.create_task(spec()); store.transition(task.id,TaskState.SPEC,TaskState.IMPLEMENT,"x")
    call=store.reserve_agent_call(task.id,1)
    store.finalize_overrun_and_pause(task.id,call,2,TaskState.IMPLEMENT)
    assert store.load(task.id).state is TaskState.PAUSED_BUDGET
    with store._connect() as c: row=c.execute("select status,actual_usd from agent_calls where id=?",(call,)).fetchone()
    assert tuple(row)==("overrun",2)

def test_handoff_diff_uses_persisted_base_and_all_change_categories(tmp_path):
    repo=tmp_path/"repo"; repo.mkdir(); subprocess.run(["git","init","-q"],cwd=repo,check=True)
    subprocess.run(["git","config","user.email","x@y"],cwd=repo,check=True); subprocess.run(["git","config","user.name","x"],cwd=repo,check=True)
    for name in ("committed.txt","staged.txt","unstaged.txt"): (repo/name).write_text("base\n",encoding="utf-8")
    subprocess.run(["git","add","."],cwd=repo,check=True); subprocess.run(["git","commit","-qm","base"],cwd=repo,check=True)
    store=TaskStore(tmp_path/"data"); task=store.create_task(spec()); dest=store.runs_root/task.id/"worktree"; dest.rmdir()
    ws=GitWorkspace.create(repo,task.id,destination=dest); store.set_workspace(task.id,str(repo),ws.base_commit,f"triagent/{task.id}")
    (dest/"committed.txt").write_text("committed-change\n",encoding="utf-8"); subprocess.run(["git","add","committed.txt"],cwd=dest,check=True); subprocess.run(["git","commit","-qm","change"],cwd=dest,check=True)
    (dest/"staged.txt").write_text("staged-change\n",encoding="utf-8"); subprocess.run(["git","add","staged.txt"],cwd=dest,check=True)
    (dest/"unstaged.txt").write_text("unstaged-change\n",encoding="utf-8"); (dest/"untracked.txt").write_text("untracked-change\n",encoding="utf-8")
    path=Orchestrator(store,FakeAgent([]),FakeAgent([]),FakeAgent([]))._write_handoff(task.id)
    diff=json.loads(path.read_text(encoding="utf-8"))["final_diff"]
    for marker in ("committed-change","staged-change","unstaged-change"): assert marker in diff
    record=json.loads(diff.split("UNTRACKED_BINARY_JSON ",1)[1]); import base64
    assert base64.b64decode(record["base64"]) == (dest/"untracked.txt").read_bytes()

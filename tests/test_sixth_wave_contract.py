from __future__ import annotations
import json,math
from pathlib import Path
import pytest
from triagent.adapters.base import AgentRequest,AgentRole,AgentStatus,CostEstimate
from triagent.adapters.cursor import CursorAdapter
from triagent.adapters.codex import CodexAdapter
from triagent.adapters.antigravity import AntigravityAdapter
from triagent.adapters.process import ProcessResult
from triagent.domain import TaskSpec,TaskState
from triagent.store import TaskStore

class Runner:
    def __init__(self,out):self.out=out;self.calls=[];self.stdin=[];self.path_bytes=[]
    def run(self,argv,cwd,timeout,env,stdin=None):
        self.calls.append(list(argv));self.stdin.append(stdin)
        if "-p" in argv:self.path_bytes.append(Path(argv[-1].rsplit(": ",1)[1]).read_bytes())
        return self.out
def req(tmp_path,role,size=200000):
    task=tmp_path/"task";task.write_text("x",encoding="utf-8");hand=tmp_path/"handoff";hand.write_text(json.dumps({"final_diff":"z"*size}),encoding="utf-8")
    return AgentRequest(role=role,task_file=task,handoff_file=hand,workdir=tmp_path,output_schema="x",timeout_seconds=5)

def test_cursor_captured_help_contract_and_nested_wrapper_stdin(tmp_path):
    nested=json.dumps({"status":"passed","evidence":["ok"],"artifacts":[],"changed_paths":[]});runner=Runner(ProcessResult(0,json.dumps({"type":"result","subtype":"success","is_error":False,"result":nested,"total_cost_usd":.2}),"",False))
    result=CursorAdapter(runner=runner).run(req(tmp_path,AgentRole.IMPLEMENTER))
    argv=runner.calls[0];assert result.status is AgentStatus.SUCCEEDED and argv[-3:]==["--print","--output-format","json"]
    assert runner.stdin[0].startswith("TRIAGENT_CONTROLLER_PROMPT_V2") and "--input-file" not in argv and not list(tmp_path.glob(".triagent-input-*"))

def test_codex_documented_dash_stdin_large_payload(tmp_path):
    event=json.dumps({"type":"item.completed","item":{"type":"agent_message","text":json.dumps({"status":"passed","evidence":[],"artifacts":[]})}});runner=Runner(ProcessResult(0,event,"",False))
    assert CodexAdapter(runner=runner).run(req(tmp_path,AgentRole.VERIFIER)).status is AgentStatus.SUCCEEDED
    assert runner.calls[0][-1]=="-" and len(runner.stdin[0])>200000 and "--input-file" not in runner.calls[0]

def test_antigravity_external_acl_path_only_and_cleanup(tmp_path):
    checked=[];runner=Runner(ProcessResult(0,json.dumps({"status":"passed","evidence":[],"artifacts":[],"findings":[]}),"",False))
    adapter=AntigravityAdapter(runner=runner,acl_verifier=lambda directory,file:checked.append((directory,file)) or True)
    assert adapter.run(req(tmp_path,AgentRole.REVIEWER)).status is AgentStatus.SUCCEEDED
    directory,file=checked[1];assert checked[0]==(directory,None) and tmp_path not in file.parents and runner.stdin[0] is None and not directory.exists()
    assert "TRIAGENT_CONTROLLER_PROMPT_V2" not in " ".join(runner.calls[0]) and runner.path_bytes[0].startswith(b"TRIAGENT_CONTROLLER_PROMPT_V2")

def test_antigravity_acl_failure_never_invokes_runner(tmp_path):
    runner=Runner(ProcessResult(0,"{}","",False));result=AntigravityAdapter(runner=runner,acl_verifier=lambda d,f:False).run(req(tmp_path,AgentRole.REVIEWER))
    assert result.status is AgentStatus.FAILED and runner.calls==[]

@pytest.mark.parametrize("value",[True,float("nan"),float("inf"),-1])
def test_cost_estimate_rejects_nonfinite_bool_and_negative(value):
    with pytest.raises(ValueError):CostEstimate(value)

def test_visual_request_is_exact_and_ambiguous_versions_rejected(tmp_path):
    store=TaskStore(tmp_path);task=store.create_task(TaskSpec(goal="x",scope=["x"],acceptance=["x"]));store.transition(task.id,TaskState.SPEC,TaskState.WAITING_FOR_VISUAL_APPROVAL,"x")
    store.request_approval(task.id,"visual",{"repo":"r","version":"1"});store.request_approval(task.id,"visual",{"repo":"r","version":"2"})
    with pytest.raises(ValueError,match="ambiguous"):store.approve_and_transition(task.id,"visual",TaskState.WAITING_FOR_VISUAL_APPROVAL,TaskState.APPROVAL)

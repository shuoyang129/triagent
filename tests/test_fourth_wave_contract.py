from __future__ import annotations
import json,subprocess
from pathlib import Path
import pytest
from triagent.adapters.process import ProcessResult
from triagent.adapters.cursor import CursorAdapter
from triagent.adapters.codex import CodexAdapter
from triagent.adapters.antigravity import AntigravityAdapter
from triagent.domain import Budget,TaskSpec,TaskState
from triagent.git_workspace import GitWorkspace
from triagent.orchestrator import Orchestrator
from triagent.store import BudgetExceeded,TaskStore

class Runner:
    def __init__(self,*outputs): self.outputs=list(outputs); self.calls=[]; self.inputs=[]
    def run(self,argv,cwd,timeout,env,stdin=None):
        self.calls.append(list(argv))
        if stdin is not None:self.inputs.append(stdin.encode())
        elif "-p" in argv:self.inputs.append(Path(argv[-1].rsplit(": ",1)[1]).read_bytes())
        return self.outputs.pop(0)

def init_repo(path:Path):
    path.mkdir(); subprocess.run(["git","init","-q"],cwd=path,check=True); subprocess.run(["git","config","user.email","x@y"],cwd=path,check=True); subprocess.run(["git","config","user.name","x"],cwd=path,check=True)
    (path/"a").write_text("a",encoding="utf-8"); subprocess.run(["git","add","a"],cwd=path,check=True); subprocess.run(["git","commit","-qm","a"],cwd=path,check=True)

def test_actual_real_adapter_parsers_orchestrate_to_approval(tmp_path):
    repo=tmp_path/"repo"; init_repo(repo); store=TaskStore(tmp_path/"data")
    task=store.create_task(TaskSpec(goal="x",scope=["x"],acceptance=["x"],budget=Budget(max_usd=3)))
    dest=store.runs_root/task.id/"worktree"; dest.rmdir(); ws=GitWorkspace.create(repo,task.id,destination=dest); store.set_workspace(task.id,str(repo),ws.base_commit,f"triagent/{task.id}")
    nested=json.dumps({"status":"passed","evidence":["implemented"]}); cursor_runner=Runner(ProcessResult(0,json.dumps({"type":"result","subtype":"success","is_error":False,"result":nested,"total_cost_usd":.2}),"",False))
    codex_event=json.dumps({"type":"item.completed","item":{"type":"agent_message","text":json.dumps({"status":"passed","evidence":["tests"]})}})
    codex_runner=Runner(ProcessResult(0,codex_event,"",False))
    review={"status":"passed","evidence":["independent"],"findings":[],"actual_usd":.1}
    agy_runner=Runner(ProcessResult(0,json.dumps(review),"",False))
    orchestrator=Orchestrator(store,CursorAdapter(runner=cursor_runner,estimated_usd=.5),CodexAdapter(runner=codex_runner,estimated_usd=.5),AntigravityAdapter(runner=agy_runner,estimated_usd=.5,acl_verifier=lambda directory,file:True))
    assert orchestrator.run_until_blocked(task.id) is TaskState.APPROVAL
    assert store.runtime(task.id).usd_spent == pytest.approx(.8)  # .2 + conservative .5 unknown + .1
    assert store.outstanding_approvals(task.id)==["merge","outcome"]
    assert b"HANDOFF\n" in codex_runner.inputs[0] and b"HANDOFF\n" in agy_runner.inputs[0]

def test_pending_reservations_cannot_oversubscribe(tmp_path):
    store=TaskStore(tmp_path); task=store.create_task(TaskSpec(goal="x",scope=["x"],acceptance=["x"],budget=Budget(max_usd=1)))
    store.reserve_agent_call(task.id,.6)
    with pytest.raises(BudgetExceeded): store.reserve_agent_call(task.id,.5)

def test_approval_copies_exact_outstanding_resource(tmp_path):
    store=TaskStore(tmp_path); task=store.create_task(TaskSpec(goal="x",scope=["x"],acceptance=["x"]))
    resource={"repo":"R","task_id":task.id,"branch":"B"}; store.request_approval(task.id,"merge",resource)
    store.approve_requested(task.id,"merge")
    assert all(store.approval_resource(task.id,"merge")[key]==value for key,value in resource.items())
    with pytest.raises(ValueError): store.approve_requested(task.id,"prune-branch")

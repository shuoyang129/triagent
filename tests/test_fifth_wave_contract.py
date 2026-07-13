from __future__ import annotations
import json
from pathlib import Path
import pytest
from triagent.adapters.base import AgentRequest,AgentRole,AgentStatus
from triagent.adapters.codex import CodexAdapter
from triagent.adapters.antigravity import AntigravityAdapter
from triagent.adapters.process import ProcessResult
from triagent.domain import Budget,TaskSpec,TaskState
from triagent.store import BudgetExceeded,TaskStore

class Runner:
    def __init__(self,out): self.out=out; self.calls=[]
    def run(self,argv,cwd,timeout,env): self.calls.append(argv); return self.out
def request(tmp_path,role):
    task=tmp_path/"task"; task.write_text("x",encoding="utf-8"); hand=tmp_path/"hand"; hand.write_text('{}',encoding="utf-8")
    return AgentRequest(role=role,task_file=task,handoff_file=hand,workdir=tmp_path,output_schema="x",timeout_seconds=5)

def test_plain_text_codex_is_invalid_output(tmp_path):
    event=json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"looks good"}})
    assert CodexAdapter(runner=Runner(ProcessResult(0,event,"",False))).run(request(tmp_path,AgentRole.VERIFIER)).status is AgentStatus.INVALID_OUTPUT

@pytest.mark.parametrize("estimate",[None,0,-1])
def test_paid_probe_nonpositive_estimate_never_runs(tmp_path,estimate):
    store=TaskStore(tmp_path); task=store.create_task(TaskSpec(goal="x",scope=["x"],acceptance=["x"],budget=Budget(max_usd=5))); called=[]
    with pytest.raises(BudgetExceeded): store.execute_paid_operation(task.id,estimate,lambda:called.append(True))
    assert called==[]

def test_visual_approval_creates_outcome_and_merge_requests_atomically(tmp_path):
    store=TaskStore(tmp_path); task=store.create_task(TaskSpec(goal="x",scope=["x"],acceptance=["x"])); store.transition(task.id,TaskState.SPEC,TaskState.WAITING_FOR_VISUAL_APPROVAL,"x"); store.request_approval(task.id,"visual",{"version":"v1"})
    store.approve_and_transition(task.id,"visual",TaskState.WAITING_FOR_VISUAL_APPROVAL,TaskState.APPROVAL)
    assert store.outstanding_approvals(task.id)==["merge","outcome"] and store.approval_resource(task.id,"visual")=={"version":"v1"}

def test_resource_versions_require_separate_approvals(tmp_path):
    store=TaskStore(tmp_path); task=store.create_task(TaskSpec(goal="x",scope=["x"],acceptance=["x"])); store.request_approval(task.id,"merge",{"version":"v1"}); store.approve_requested(task.id,"merge"); store.request_approval(task.id,"merge",{"version":"v2"})
    assert store.outstanding_approvals(task.id)==["merge"]

def test_malformed_reviewer_finding_is_invalid_before_orchestration(tmp_path):
    payload={"status":"passed","findings":[{"severity":"MYSTERY","code":"x","message":"x"}]}
    result=AntigravityAdapter(runner=Runner(ProcessResult(0,json.dumps(payload),"",False))).run(request(tmp_path,AgentRole.REVIEWER))
    assert result.status is AgentStatus.INVALID_OUTPUT

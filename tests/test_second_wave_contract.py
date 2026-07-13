from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from triagent.adapters.base import AgentRequest, AgentResult, AgentRole, AgentStatus, CostEstimate
from triagent.adapters.deepseek import DeepSeekAdapter
from triagent.adapters.fake import FakeAgent
from triagent.domain import Budget, TaskSpec, TaskState
from triagent.git_workspace import GitWorkspace
from triagent.orchestrator import Orchestrator
from triagent.store import BudgetExceeded, LeaseConflict, TaskStore
from triagent.report import render_persisted_report
from typer.testing import CliRunner
import triagent.cli as cli_module


def spec(**kw):
    return TaskSpec(goal="x", scope=["x"], acceptance=["x"], **kw)


def test_unknown_cost_is_always_refused_even_with_zero_budget(tmp_path):
    store=TaskStore(tmp_path); task=store.create_task(spec(budget=Budget(max_usd=0)))
    with pytest.raises(BudgetExceeded, match="unknown"): store.reserve_agent_call(task.id, None)


def test_actual_cost_cannot_exceed_conservative_reservation(tmp_path):
    store=TaskStore(tmp_path); task=store.create_task(spec(budget=Budget(max_usd=2)))
    call=store.reserve_agent_call(task.id, 1)
    with pytest.raises(BudgetExceeded): store.complete_agent_call(task.id, call, 1.01)

def test_orchestrator_reconciles_adapter_actual_cost(tmp_path):
    class Paid(FakeAgent):
        identity="paid"; allowed_roles=frozenset({AgentRole.IMPLEMENTER})
        def estimate_cost(self, request): return CostEstimate(1.0)
    store=TaskStore(tmp_path); task=store.create_task(spec(budget=Budget(max_usd=2)))
    paid=Paid([AgentResult(status=AgentStatus.SUCCEEDED,actual_usd=.4,data={"status":"passed","summary_code":"done"})])
    orchestrator=Orchestrator(store,paid,FakeAgent.succeeding(),FakeAgent.succeeding())
    orchestrator.advance(task.id); orchestrator.advance(task.id)
    assert store.runtime(task.id).usd_spent == pytest.approx(.4)


def test_deepseek_cannot_be_activated_by_constructor_boolean(tmp_path):
    with pytest.raises(TypeError): DeepSeekAdapter(enabled=True, billing_confirmed=True, live_confirmed=True, validated_ready=True)


def test_adapter_identity_role_and_cost_are_authoritative(tmp_path):
    fake=FakeAgent.succeeding()
    assert fake.identity == "fake" and AgentRole.IMPLEMENTER in fake.allowed_roles
    assert fake.estimate_cost(None) == CostEstimate.enforced_zero()


def test_verifier_and_reviewer_receive_persisted_handoff(tmp_path):
    store=TaskStore(tmp_path); task=store.create_task(spec())
    run=store.runs_root/task.id; (run/"worktree"/"x.py").write_text("x", encoding="utf-8")
    ok=AgentResult(status=AgentStatus.SUCCEEDED, data={"status":"passed","summary_code":"completed","evidence":[]})
    verifier=FakeAgent([ok]); reviewer=FakeAgent([ok])
    Orchestrator(store, FakeAgent([ok]), verifier, reviewer).run_until_blocked(task.id)
    for agent in (verifier, reviewer):
        path=agent.requests[0].handoff_file
        payload=json.loads(path.read_text(encoding="utf-8"))
        assert set(payload) >= {"task_spec","final_diff","tests","artifacts","rollback","completed"}


def test_visual_approval_is_atomic_and_state_specific(tmp_path):
    store=TaskStore(tmp_path); task=store.create_task(spec())
    store.transition(task.id, TaskState.SPEC, TaskState.WAITING_FOR_VISUAL_APPROVAL, "test")
    store.approve_and_transition(task.id, "visual", TaskState.WAITING_FOR_VISUAL_APPROVAL, TaskState.APPROVAL)
    assert store.load(task.id).state is TaskState.APPROVAL
    assert "visual" in store.runtime(task.id).approvals

def test_budget_transaction_asserts_controller_lease(tmp_path):
    store=TaskStore(tmp_path); task=store.create_task(spec())
    store.acquire_lease(task.id,"owner",60)
    with pytest.raises(LeaseConflict): store.reserve_agent_call(task.id,0,"intruder")

def test_blocking_review_is_persisted_failed_not_passed(tmp_path):
    store=TaskStore(tmp_path); task=store.create_task(spec())
    ok=AgentResult(status=AgentStatus.SUCCEEDED,data={"status":"passed","summary_code":"done"})
    major=AgentResult(status=AgentStatus.SUCCEEDED,data={"status":"failed","summary_code":"repair","findings":[{"severity":"MAJOR","message":"x"}]})
    Orchestrator(store,FakeAgent([ok]),FakeAgent([ok]),FakeAgent([major])).run_until_blocked(task.id)
    assert store.outcomes(task.id)["review"].status == "failed"

def test_report_ignores_cached_snapshot_and_derives_pending(tmp_path):
    store=TaskStore(tmp_path); task=store.create_task(spec())
    (store.runs_root/task.id/"final-report.md").write_text("FAKE SUCCESS",encoding="utf-8")
    report=render_persisted_report(store,task.id)
    assert "FAKE SUCCESS" not in report and "unknown/missing" in report

def test_cli_partial_git_failure_is_durable_and_records_preserved_resource(tmp_path, monkeypatch):
    repo=tmp_path/"repo"; repo.mkdir()
    data=tmp_path/"data"
    def partial(repo, task_id, destination):
        destination.mkdir(parents=True)
        (destination/"partial").write_text("x",encoding="utf-8")
        raise RuntimeError("partial git failure with secret detail")
    monkeypatch.setattr(cli_module.GitWorkspace,"create",partial)
    result=CliRunner().invoke(cli_module.app,["run","--profile","fake","--data-root",str(data),str(repo),"x"])
    assert result.exit_code != 0
    task_id=next((data/"runs").iterdir()).name; store=TaskStore(data)
    assert store.load(task_id).state is TaskState.FAILED_FINAL
    setup=store.outcomes(task_id)["setup"]
    assert setup.status == "failed" and any("preserved worktree" in x for x in setup.evidence)

def test_real_profile_probes_cursor_first_and_prices_deepseek_fallback(tmp_path, monkeypatch):
    repo=tmp_path/"repo"; repo.mkdir(); subprocess.run(["git","init","-q"],cwd=repo,check=True)
    subprocess.run(["git","config","user.email","x@y"],cwd=repo,check=True); subprocess.run(["git","config","user.name","x"],cwd=repo,check=True)
    (repo/"a").write_text("a",encoding="utf-8"); subprocess.run(["git","add","a"],cwd=repo,check=True); subprocess.run(["git","commit","-qm","a"],cwd=repo,check=True)
    profile=tmp_path/"p.toml"; profile.write_text('''
[agents.cursor]
command=["cursor"]
estimated_usd=0.5
[agents.codex]
command=["codex"]
estimated_usd=0.5
[agents.antigravity]
command=["agy"]
estimated_usd=0.5
[agents.opencode]
command=["opencode"]
estimated_usd=0.5
probe_estimated_usd=0.25
[budget]
max_agent_calls=10
max_minutes=60
max_usd=5
''',encoding="utf-8")
    order=[]
    class Stub(FakeAgent):
        def __init__(self,*args,identity,available=True,**kwargs):
            super().__init__([AgentResult(status=AgentStatus.SUCCEEDED,data={"status":"passed","summary_code":"done"})]); self.identity=identity; self.available=available
        def capabilities(self): order.append(self.identity); return type("C",(),{"available":self.available})()
    monkeypatch.setattr(cli_module,"CursorAdapter",lambda *a,**k: Stub(identity="cursor",available=False))
    monkeypatch.setattr(cli_module,"DeepSeekAdapter",lambda *a,**k: Stub(identity="deepseek"))
    monkeypatch.setattr(cli_module,"CodexAdapter",lambda *a,**k: Stub(identity="codex"))
    monkeypatch.setattr(cli_module,"AntigravityAdapter",lambda *a,**k: Stub(identity="antigravity"))
    result=CliRunner().invoke(cli_module.app,["run","--profile",str(profile),"--live-confirmed","--billing-confirmed","--data-root",str(tmp_path/"data"),str(repo),"x"])
    assert result.exit_code == 0, result.output
    assert order[:2] == ["cursor","deepseek"]
    task_id=result.output.split("Task: ",1)[1].splitlines()[0]
    assert TaskStore(tmp_path/"data").runtime(task_id).usd_spent >= .25


def test_pruning_uses_durable_task_approval(tmp_path):
    repo=tmp_path/"repo"; repo.mkdir(); subprocess.run(["git","init","-q"],cwd=repo,check=True)
    subprocess.run(["git","config","user.email","x@y"],cwd=repo,check=True); subprocess.run(["git","config","user.name","x"],cwd=repo,check=True)
    (repo/"a").write_text("a",encoding="utf-8"); subprocess.run(["git","add","a"],cwd=repo,check=True); subprocess.run(["git","commit","-qm","a"],cwd=repo,check=True)
    ws=GitWorkspace.create(repo,"prune"); ws.cleanup(); store=TaskStore(tmp_path/"data"); task=store.create_task(spec())
    with pytest.raises(PermissionError): ws.prune_branch(store=store, task_id=task.id)
    store.record_approval(task.id,"prune-branch"); ws.prune_branch(store=store, task_id=task.id)

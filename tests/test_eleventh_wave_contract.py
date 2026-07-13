from __future__ import annotations

import hashlib
import json
import subprocess
import zlib
from pathlib import Path

import pytest

import triagent.store as store_module
from triagent.adapters.fake import FakeAgent
from triagent.domain import TaskSpec, TaskState
from triagent.git_workspace import GitWorkspace
from triagent.orchestrator import Orchestrator
from triagent.store import TaskStore


def init_repo(path:Path,extra:dict[str,bytes]|None=None)->str:
    path.mkdir();subprocess.run(["git","init","-q"],cwd=path,check=True)
    subprocess.run(["git","config","user.email","x@y"],cwd=path,check=True);subprocess.run(["git","config","user.name","x"],cwd=path,check=True)
    (path/"base.txt").write_text("base",encoding="utf-8")
    for name,data in (extra or {}).items():
        target=path/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    subprocess.run(["git","add","-A"],cwd=path,check=True);subprocess.run(["git","commit","-qm","base"],cwd=path,check=True)
    return subprocess.run(["git","rev-parse","HEAD"],cwd=path,check=True,capture_output=True,text=True).stdout.strip()


def setup(tmp_path:Path,extra:dict[str,bytes]|None=None):
    repo=tmp_path/"repo";base=init_repo(repo,extra);store=TaskStore(tmp_path/"data");task=store.create_task(TaskSpec(goal="x",scope=["x"],acceptance=["x"]))
    work=store.runs_root/task.id/"worktree";work.rmdir();ws=GitWorkspace.create(repo,task.id,destination=work);store.set_workspace(task.id,str(repo),base,f"triagent/{task.id}")
    return store,task,work,ws


def test_all_workspace_git_paths_disable_hooks_external_diff_and_textconv(tmp_path):
    repo=tmp_path/"repo";init_repo(repo,{".gitattributes":b"*.txt diff=evil\n"})
    hook_marker=tmp_path/"hook-ran";hook=repo/".git/hooks/post-checkout";hook.write_text(f"#!/bin/sh\nprintf ran > '{hook_marker.as_posix()}'\n",encoding="utf-8");hook.chmod(0o755)
    external_marker=tmp_path/"external-ran";textconv_marker=tmp_path/"textconv-ran"
    subprocess.run(["git","config","diff.external",f'powershell -Command "Set-Content -Path {external_marker} -Value ran"'],cwd=repo,check=True)
    subprocess.run(["git","config","diff.evil.textconv",f'powershell -Command "Set-Content -Path {textconv_marker} -Value ran"'],cwd=repo,check=True)
    ws=GitWorkspace.create(repo,"hardened",destination=tmp_path/"work")
    (ws.path/"base.txt").write_text("changed",encoding="utf-8")
    ws.diff();ws.handoff()
    assert not hook_marker.exists() and not external_marker.exists() and not textconv_marker.exists()
    (ws.path/"base.txt").write_text("base",encoding="utf-8");ws.cleanup()


@pytest.mark.parametrize("name,data",[
    (".docker/config.json",b'{"auths":{"registry.example":{"auth":"dXNlcjpwYXNzd29yZA=="}}}'),
    ("settings.yaml",b'registry:\n  basic_auth: dXNlcjpwYXNzd29yZA==\n'),
])
def test_registry_and_basic_auth_rejected_before_object_write(tmp_path,name,data):
    store,task,work,_=setup(tmp_path);target=work/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    oid=hashlib.sha1(b"blob "+str(len(data)).encode()+b"\0"+data,usedforsecurity=False).hexdigest()
    with pytest.raises(ValueError,match="candidate manifest rejected"):store.materialize_reviewed_commit(task.id,[name])
    assert subprocess.run(["git","cat-file","-e",oid],cwd=work,capture_output=True).returncode!=0


def test_approve_only_and_failed_prune_precondition_preserves_consumption(tmp_path):
    store,task,work,ws=setup(tmp_path);candidate=store.materialize_reviewed_commit(task.id,[]);resource=store.approval_manifest(task.id)
    store.request_approval(task.id,"prune-branch",resource);store.transition(task.id,TaskState.SPEC,TaskState.APPROVAL,"test-ready")
    orchestrator=Orchestrator(store,FakeAgent([]),FakeAgent([]),FakeAgent([]));orchestrator.approve(task.id,"prune-branch")
    assert store.approval_resource(task.id,"prune-branch")["reviewed_commit"]==candidate and store.consumed_actions(task.id)==[]
    with pytest.raises(RuntimeError,match="clean up the worktree"):ws.prune_branch(store=store,task_id=task.id)
    assert store.consumed_actions(task.id)==[]
    ws.cleanup();ws.prune_branch(store=store,task_id=task.id)
    assert store.consumed_actions(task.id)==["prune-branch"]


@pytest.mark.parametrize("source",["core","info"])
def test_local_excludes_cannot_hide_unexpected_paths(tmp_path,source):
    store,task,work,_=setup(tmp_path);repo=Path(store.workspace(task.id)["repo"])
    hidden=f"hidden-{source}.txt"
    if source=="core":
        excludes=tmp_path/"global-excludes";excludes.write_text(hidden+"\n",encoding="utf-8")
        subprocess.run(["git","config","core.excludesFile",str(excludes)],cwd=work,check=True)
    else:
        common=Path(subprocess.run(["git","rev-parse","--git-common-dir"],cwd=work,check=True,capture_output=True,text=True).stdout.strip())
        if not common.is_absolute():common=(work/common).resolve()
        info=common/"info/exclude";info.parent.mkdir(parents=True,exist_ok=True);info.write_text(hidden+"\n",encoding="utf-8")
    for name in ("allowed.txt",hidden):(work/name).write_text(name,encoding="utf-8")
    with pytest.raises(ValueError,match="changed path manifest mismatch"):store.materialize_reviewed_commit(task.id,["allowed.txt"])


def _chunk(kind:bytes,payload:bytes)->bytes:
    return len(payload).to_bytes(4,"big")+kind+payload+zlib.crc32(kind+payload).to_bytes(4,"big")


def test_png_rejects_nonconsecutive_idat_chunks():
    compressed=zlib.compress(b"\x00\xff\x00\x00");split=max(1,len(compressed)//2)
    png=b"\x89PNG\r\n\x1a\n"+_chunk(b"IHDR",(1).to_bytes(4,"big")*2+bytes([8,2,0,0,0]))+_chunk(b"IDAT",compressed[:split])+_chunk(b"tEXt",b"note\x00x")+_chunk(b"IDAT",compressed[split:])+_chunk(b"IEND",b"")
    assert not store_module._safe_raster(png,".png")

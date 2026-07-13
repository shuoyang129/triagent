from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import zlib
from pathlib import Path

import pytest

import triagent.store as store_module
from triagent.adapters._cli import invoke_json
from triagent.adapters.base import AgentRole, AgentStatus
from triagent.adapters.process import ProcessResult
from triagent.domain import TaskSpec
from triagent.git_workspace import GitWorkspace
from triagent.store import TaskStore


def _png_chunk(kind:bytes,payload:bytes)->bytes:
    return len(payload).to_bytes(4,"big")+kind+payload+zlib.crc32(kind+payload).to_bytes(4,"big")


VALID_PNG=(b"\x89PNG\r\n\x1a\n"+_png_chunk(b"IHDR",(1).to_bytes(4,"big")+(1).to_bytes(4,"big")+bytes([8,2,0,0,0]))+_png_chunk(b"IDAT",zlib.compress(b"\x00\xff\x00\x00"))+_png_chunk(b"IEND",b""))


def init_repo(path:Path,extra:dict[str,bytes]|None=None)->str:
    path.mkdir(); subprocess.run(["git","init","-q"],cwd=path,check=True)
    subprocess.run(["git","config","user.email","x@y"],cwd=path,check=True); subprocess.run(["git","config","user.name","x"],cwd=path,check=True)
    (path/"base.txt").write_text("base",encoding="utf-8")
    for name,data in (extra or {}).items():
        target=path/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    subprocess.run(["git","add","-A"],cwd=path,check=True);subprocess.run(["git","commit","-qm","base"],cwd=path,check=True)
    return subprocess.run(["git","rev-parse","HEAD"],cwd=path,check=True,capture_output=True,text=True).stdout.strip()


def setup(tmp_path:Path,extra:dict[str,bytes]|None=None):
    repo=tmp_path/"repo";base=init_repo(repo,extra);store=TaskStore(tmp_path/"data");task=store.create_task(TaskSpec(goal="x",scope=["x"],acceptance=["x"]))
    work=store.runs_root/task.id/"worktree";work.rmdir();ws=GitWorkspace.create(repo,task.id,destination=work);store.set_workspace(task.id,str(repo),base,f"triagent/{task.id}")
    return store,task,work


def test_strict_implementer_output_requires_changed_paths(tmp_path):
    class Runner:
        def run(self,*args,**kwargs):return ProcessResult(0,json.dumps({"status":"passed","evidence":[],"artifacts":[]}),"",False)
    assert invoke_json(Runner(),["local"],tmp_path,1,role=AgentRole.IMPLEMENTER).status is AgentStatus.INVALID_OUTPUT


def test_changed_paths_must_exactly_match_actual_changes(tmp_path):
    store,task,work=setup(tmp_path);(work/"allowed.txt").write_text("ok",encoding="utf-8");(work/"unexpected.txt").write_text("no",encoding="utf-8")
    with pytest.raises(ValueError,match="changed path manifest mismatch"):store.materialize_reviewed_commit(task.id,["allowed.txt"])
    (work/"unexpected.txt").unlink();candidate=store.materialize_reviewed_commit(task.id,["allowed.txt"])
    assert subprocess.run(["git","show",f"{candidate}:allowed.txt"],cwd=work,check=True,capture_output=True).stdout==b"ok"


@pytest.mark.parametrize("name,data",[
    ("config.json",b'{"client_secret":"abcdefghijklmnopqrstuvwxyz"}'),
    ("id_ed25519",b"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n"),
    (".npmrc",b"//registry.npmjs.org/:_authToken=npm_abcdefghijklmnopqrstuvwxyz"),
    ("settings.toml",b'token="eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signaturevalue"'),
],ids=["json-secret","ssh-key","npm-token","jwt"])
def test_suspicious_secret_changes_rejected_before_object_write(tmp_path,name,data):
    store,task,work=setup(tmp_path);target=work/name;target.write_bytes(data)
    oid=hashlib.sha1(b"blob "+str(len(data)).encode()+b"\0"+data,usedforsecurity=False).hexdigest()
    with pytest.raises(ValueError,match="candidate manifest rejected"):store.materialize_reviewed_commit(task.id,[name])
    assert subprocess.run(["git","cat-file","-e",oid],cwd=work,capture_output=True).returncode!=0


def test_ignored_cache_and_node_modules_never_enter_candidate(tmp_path):
    store,task,work=setup(tmp_path,{".gitignore":b"*.cache\nnode_modules/\n"})
    (work/"allowed.txt").write_text("ok",encoding="utf-8");(work/"junk.cache").write_text("ignored",encoding="utf-8")
    (work/"node_modules").mkdir();(work/"node_modules"/"pkg.js").write_text("ignored",encoding="utf-8")
    candidate=store.materialize_reviewed_commit(task.id,["allowed.txt"])
    names=subprocess.run(["git","ls-tree","-r","--name-only",candidate],cwd=work,check=True,capture_output=True,text=True).stdout
    assert "allowed.txt" in names and "junk.cache" not in names and "node_modules" not in names


def test_manifest_aggregate_count_bytes_depth_and_path_limits(tmp_path,monkeypatch):
    store,task,work=setup(tmp_path)
    monkeypatch.setattr(store_module,"_MAX_CHANGED_FILES",1);monkeypatch.setattr(store_module,"_MAX_CHANGED_BYTES",4);monkeypatch.setattr(store_module,"_MAX_PATH_LENGTH",12);monkeypatch.setattr(store_module,"_MAX_DIRECTORY_DEPTH",2)
    (work/"a.txt").write_text("12345",encoding="utf-8")
    with pytest.raises(ValueError,match="candidate limits exceeded"):store.materialize_reviewed_commit(task.id,["a.txt"])
    (work/"a.txt").write_text("1",encoding="utf-8");(work/"b.txt").write_text("2",encoding="utf-8")
    with pytest.raises(ValueError,match="candidate limits exceeded"):store.materialize_reviewed_commit(task.id,["a.txt","b.txt"])
    (work/"b.txt").unlink();deep=work/"a"/"b"/"c.txt";deep.parent.mkdir(parents=True);deep.write_text("x",encoding="utf-8")
    with pytest.raises(ValueError,match="candidate limits exceeded"):store.materialize_reviewed_commit(task.id,["a.txt","a/b/c.txt"])


def _rewrite_ihdr(data:bytes,*,interlace:int=0,width:int=1,height:int=1)->bytes:
    payload=width.to_bytes(4,"big")+height.to_bytes(4,"big")+bytes([8,2,0,0,interlace]);kind=b"IHDR";crc=zlib.crc32(kind+payload).to_bytes(4,"big")
    return data[:8]+len(payload).to_bytes(4,"big")+kind+payload+crc+data[33:]


def test_png_policy_rejects_jpeg_interlace_and_dimension_bomb():
    assert store_module._safe_raster(VALID_PNG,".png")
    assert not store_module._safe_raster(b"\xff\xd8fake\xff\xd9",".jpg")
    assert not store_module._safe_raster(_rewrite_ihdr(VALID_PNG,interlace=1),".png")
    assert not store_module._safe_raster(_rewrite_ihdr(VALID_PNG,width=9000),".png")


def test_approval_consumption_is_once_only_and_failure_preserves_approval(tmp_path):
    store,task,work=setup(tmp_path);(work/"a.txt").write_text("x",encoding="utf-8");candidate=store.materialize_reviewed_commit(task.id,["a.txt"]);resource=store.approval_manifest(task.id)
    store.request_approval(task.id,"merge",resource);store.approve_requested(task.id,"merge")
    subprocess.run(["git","update-ref",resource["candidate_ref"],resource["base_commit"]],cwd=work,check=True)
    with pytest.raises(PermissionError):store.consume_approval(task.id,"merge")
    assert store.approval_resource(task.id,"merge")["reviewed_commit"]==candidate and store.consumed_actions(task.id)==[]
    subprocess.run(["git","update-ref",resource["candidate_ref"],candidate],cwd=work,check=True)
    assert store.consume_approval(task.id,"merge")==candidate
    with pytest.raises(PermissionError):store.consume_approval(task.id,"merge")


def test_candidate_ref_rolls_back_when_sqlite_cas_fails(tmp_path,monkeypatch):
    store,task,work=setup(tmp_path);(work/"a.txt").write_text("x",encoding="utf-8");ref=f"refs/triagent/reviewed/{task.id}"
    monkeypatch.setattr(store,"_persist_candidate",lambda *args:False)
    with pytest.raises(ValueError,match="persistence conflict"):store.materialize_reviewed_commit(task.id,["a.txt"])
    assert subprocess.run(["git","rev-parse","--verify",ref],cwd=work,capture_output=True).returncode!=0


def test_nested_base_files_preserved_and_gitlinks_rejected(tmp_path):
    store,task,work=setup(tmp_path,{"nested/deep/base.txt":b"nested"});candidate=store.materialize_reviewed_commit(task.id,[])
    assert subprocess.run(["git","show",f"{candidate}:nested/deep/base.txt"],cwd=work,check=True,capture_output=True).stdout==b"nested"
    (work/"nested/deep/base.txt").write_text("mutated",encoding="utf-8");store.restore_candidate_worktree(task.id)
    assert (work/"nested/deep/base.txt").read_bytes()==b"nested"
    repo=tmp_path/"gitlink";base=init_repo(repo);sub=base
    subprocess.run(["git","update-index","--add","--cacheinfo",f"160000,{sub},linked"],cwd=repo,check=True);subprocess.run(["git","commit","-qm","gitlink"],cwd=repo,check=True)
    store2=TaskStore(tmp_path/"data2");task2=store2.create_task(TaskSpec(goal="x",scope=["x"],acceptance=["x"]));work2=store2.runs_root/task2.id/"worktree";work2.rmdir();ws=GitWorkspace.create(repo,task2.id,destination=work2);store2.set_workspace(task2.id,str(repo),ws.base_commit,f"triagent/{task2.id}")
    with pytest.raises(ValueError,match="gitlink"):store2.materialize_reviewed_commit(task2.id,[])


def test_malicious_diff_configuration_never_executes(tmp_path):
    store,task,work=setup(tmp_path);marker=tmp_path/"diff-ran";(work/"a.txt").write_text("x",encoding="utf-8")
    subprocess.run(["git","config","diff.external",f'powershell -Command "Set-Content -Path {marker} -Value ran"'],cwd=work,check=True)
    store.materialize_reviewed_commit(task.id,["a.txt"]);store.approval_manifest(task.id)
    assert not marker.exists()

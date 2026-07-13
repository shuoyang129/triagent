from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Mapping,Sequence


def run_git(cwd:Path,args:Sequence[str],*,stdin:bytes|None=None,check:bool=True,extra_env:Mapping[str,str]|None=None)->subprocess.CompletedProcess[bytes]:
    """Run Git through the controller's single non-interactive hardened boundary."""
    environment={name:os.environ[name] for name in ("PATH","SYSTEMROOT","WINDIR","COMSPEC","PATHEXT","TEMP","TMP","HOME","USERPROFILE") if os.environ.get(name)}
    environment.update({
        "GIT_CONFIG_NOSYSTEM":"1",
        "GIT_CONFIG_SYSTEM":os.devnull,
        "GIT_CONFIG_GLOBAL":os.devnull,
        "GIT_ATTR_NOSYSTEM":"1",
        "GIT_TERMINAL_PROMPT":"0",
    })
    environment.update(extra_env or {})
    safe_args=list(args)
    if safe_args and safe_args[0]=="diff":safe_args=["diff","--no-ext-diff","--no-textconv",*safe_args[1:]]
    command=[
        "git",
        "-c",f"core.hooksPath={os.devnull}",
        "-c","commit.gpgSign=false",
        "-c","diff.external=",
        "-c","diff.algorithm=myers",
        "-c","core.autocrlf=true",
        "-c",f"core.excludesFile={os.devnull}",
        *safe_args,
    ]
    return subprocess.run(command,cwd=Path(cwd),env=environment,input=stdin,check=check,capture_output=True,shell=False)

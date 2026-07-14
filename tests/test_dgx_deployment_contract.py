from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles" / "dgx.spark.toml"
BOOTSTRAP = ROOT / "scripts" / "bootstrap-dgx.sh"
INSTALLER = ROOT / "scripts" / "install-triagent-dgx.sh"


def test_concrete_dgx_profile_uses_verified_paths_and_budgets() -> None:
    config = tomllib.loads(PROFILE.read_text(encoding="utf-8"))
    assert config["host"] == {
        "name": "spark-5643",
        "platform": "ubuntu-24.04",
        "hardware": "nvidia-dgx-spark",
        "address": "spark",
    }
    assert config["paths"]["runs"] == "/home/ys/works/robots/triagent-runs"
    assert config["paths"]["workspace"] == "/home/ys/works/robots/projects"
    assert config["paths"]["python"] == "/home/ys/miniforge3/envs/triagent/bin/python"
    assert config["agents"]["codex"]["command"] == ["/home/ys/.local/bin/codex"]
    assert config["agents"]["cursor"]["command"] == ["/home/ys/.local/bin/cursor-agent"]
    assert config["agents"]["antigravity"]["command"] == ["/home/ys/.local/bin/agy"]
    assert config["agents"]["opencode"]["enabled"] is False
    assert config["budget"]["max_agent_calls"] == 20
    assert config["budget"]["max_minutes"] == 60
    assert config["budget"]["max_usd"] == 20.0
    assert config["budget"]["allow_paid_overage"] is False


def test_concrete_dgx_profile_never_selects_agent_alias() -> None:
    text = PROFILE.read_text(encoding="utf-8")
    assert 'command = ["/home/ys/.local/bin/agent"]' not in text

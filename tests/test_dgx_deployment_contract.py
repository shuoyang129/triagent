from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles" / "dgx.spark.toml"
SYNTHETIC_PROFILE = ROOT / "profiles" / "dgx.spark.synthetic-force.toml"
BOOTSTRAP = ROOT / "scripts" / "bootstrap-dgx.sh"
INSTALLER = ROOT / "scripts" / "install-triagent-dgx.sh"
APPARMOR_INSTALLER = ROOT / "scripts" / "install-cursor-sandbox-apparmor.sh"
CODEX_ADAPTER = ROOT / "scripts" / "codex-verify-adapter.zsh"
CURSOR_ADAPTER = ROOT / "scripts" / "cursor-agent-adapter.zsh"
FORCE_ADAPTER = ROOT / "scripts" / "cursor-synthetic-force-adapter.zsh"
AGY_ADAPTER = ROOT / "scripts" / "agy-review-adapter.zsh"
APPARMOR_PROFILE = ROOT / "deploy" / "apparmor" / "cursor-agent-sandbox"


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
    assert config["agents"]["codex"]["command"] == [
        "/home/ys/works/robots/triagent/scripts/codex-verify-adapter.zsh"
    ]
    assert config["agents"]["cursor"]["command"] == [
        "/home/ys/works/robots/triagent/scripts/cursor-agent-adapter.zsh",
        "--auto-review",
        "--sandbox",
        "enabled",
        "--model",
        "composer-2.5-fast",
    ]
    assert config["agents"]["antigravity"]["command"] == [
        "/home/ys/works/robots/triagent/scripts/agy-review-adapter.zsh"
    ]
    assert config["agents"]["deepseek"] == {
        "enabled": False,
        "model": "deepseek-v4-flash",
        "base_url": "https://api.deepseek.com",
        "estimated_usd": 1.0,
        "probe_estimated_usd": 0.25,
    }
    assert config["budget"]["max_agent_calls"] == 20
    assert config["budget"]["max_minutes"] == 60
    assert config["budget"]["max_usd"] == 20.0
    assert config["budget"]["allow_paid_overage"] is False


def test_concrete_dgx_profile_never_selects_agent_alias() -> None:
    text = PROFILE.read_text(encoding="utf-8")
    assert 'command = ["/home/ys/.local/bin/agent"]' not in text


def test_synthetic_force_profile_is_strictly_isolated() -> None:
    config = tomllib.loads(SYNTHETIC_PROFILE.read_text(encoding="utf-8"))
    assert config["paths"]["runs"] == "/home/ys/works/robots/triagent-synthetic-runs"
    assert config["paths"]["workspace"] == "/home/ys/works/robots/synthetic-projects"
    command = config["agents"]["cursor"]["command"]
    assert command[:5] == [
        "/home/ys/works/robots/triagent/scripts/cursor-synthetic-force-adapter.zsh",
        "--force",
        "--sandbox",
        "enabled",
        "--model",
    ]
    assert "--auto-review" not in command
    assert "--yolo" not in command
    wrapper = FORCE_ADAPTER.read_text(encoding="utf-8")
    assert "/triagent-synthetic-runs/runs" in wrapper
    assert "/synthetic-projects" in wrapper
    assert 'task_file="${worktree:h}/task.yaml"' in wrapper
    assert "relative_to(workspace_root)" in wrapper


def test_cursor_userns_profile_is_limited_to_versioned_helper() -> None:
    profile = APPARMOR_PROFILE.read_text(encoding="utf-8")
    assert "/home/ys/.local/share/cursor-agent/versions/*/cursorsandbox" in profile
    assert "flags=(unconfined)" in profile
    assert "userns," in profile
    assert "/home/ys/**" not in profile
    installer = APPARMOR_INSTALLER.read_text(encoding="utf-8")
    assert "apparmor_parser -r" in installer
    assert "kernel.apparmor_restrict_unprivileged_userns" in installer


def test_dgx_diagnostic_uses_fixed_vendor_and_conda_paths() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "$HOME/.local/bin/codex" in text
    assert "$HOME/.local/bin/cursor-agent" in text
    assert "$HOME/.local/bin/agy" in text
    assert "$HOME/miniforge3/bin/conda" in text
    assert "$HOME/.local/bin/agent" not in text


def test_dgx_installer_is_explicit_and_avoids_system_installation() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert "--apply" in text
    assert 'env_name="triagent"' in text
    assert "python=3.12" in text
    assert "triagent-runs" in text
    assert "projects" in text
    assert "sudo" not in text
    assert "apt-get" not in text
    assert "conda install" not in text


def test_dgx_installer_reuses_existing_node_inside_isolated_environment() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert '"$HOME"/.nvm/versions/node/*/bin/node' in text
    assert '"$HOME"/.local/opt/node-*/bin/node' in text
    assert 'ln -s "$candidate" "$env_path/bin/node"' in text
    assert "Required Node runtime missing" in text


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is unavailable")
def test_dgx_scripts_have_valid_bash_syntax() -> None:
    for script in (BOOTSTRAP, INSTALLER, APPARMOR_INSTALLER):
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is unavailable")
def test_dgx_adapter_scripts_have_valid_zsh_syntax() -> None:
    for script in (CODEX_ADAPTER, CURSOR_ADAPTER, FORCE_ADAPTER, AGY_ADAPTER):
        result = subprocess.run(["zsh", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

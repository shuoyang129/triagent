from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "triagent" / "SKILL.md"
PROFILE = ROOT / "profiles" / "dgx.example.toml"
BOOTSTRAP = ROOT / "scripts" / "bootstrap-dgx.sh"
CHECKLIST = ROOT / "docs" / "operations" / "dgx-onsite-checklist.md"
REQUIRED = (SKILL, PROFILE, BOOTSTRAP, CHECKLIST)


def read(path: Path) -> str:
    assert path.is_file(), f"missing packaging artifact: {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def test_required_distribution_files_exist() -> None:
    for path in REQUIRED:
        assert path.is_file(), f"missing packaging artifact: {path.relative_to(ROOT)}"


def test_skill_has_valid_trigger_metadata_and_operator_workflow() -> None:
    text = read(SKILL)
    frontmatter = text.split("---", 2)[1]
    keys = [line.split(":", 1)[0] for line in frontmatter.splitlines() if ":" in line]
    assert keys == ["name", "description"]
    assert "name: triagent" in frontmatter
    assert re.search(r"^description: Use when\b", frontmatter, re.MULTILINE)
    for phrase in (
        "outcome approval",
        "forbidden",
        "triagent doctor",
        "triagent create",
        "triagent run",
        "triagent status",
        "triagent approve",
        "triagent report",
    ):
        assert phrase in text.lower()


def test_skill_command_examples_use_only_triagent_operator_boundary() -> None:
    text = read(SKILL)
    blocks = re.findall(r"```(?:bash|shell|powershell)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    commands = [line.strip() for block in blocks for line in block.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert commands, "Skill must include executable operator examples"
    assert all(re.match(r"^(?:python\s+-m\s+)?triagent(?:\s|$)", line) for line in commands)
    forbidden = ("codex", "cursor", "antigravity", "agy", "gemini", "deepseek", "opencode")
    for line in commands:
        assert not any(re.search(rf"(?:^|\s){name}(?:\s|$)", line, re.IGNORECASE) for name in forbidden)


def test_dgx_profile_is_placeholder_only_and_describes_target_capabilities() -> None:
    text = read(PROFILE).lower()
    for phrase in ("ubuntu-24.04", "dgx", "nvidia", "robot", "visual"):
        assert phrase in text
    assert "example.invalid" in text or "<" in text
    assert not re.search(r"(?:api[_-]?key|token|password|secret)\s*=\s*[\"'][^<\"']{8,}", text)
    assert not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)


def test_distribution_materials_do_not_embed_secrets() -> None:
    text = "\n".join(read(path) for path in REQUIRED)
    forbidden = (
        r"sk-[a-z0-9_-]{12,}",
        r"-----begin (?:rsa |openssh )?private key-----",
        r"ghp_[a-z0-9]{20,}",
    )
    assert not any(re.search(pattern, text, re.IGNORECASE) for pattern in forbidden)


def test_dgx_bootstrap_defaults_to_diagnostics_and_guards_install() -> None:
    text = read(BOOTSTRAP)
    assert "--install" in text
    assert re.search(r"read\s+.*confirm", text, re.IGNORECASE)
    assert re.search(r"\[\[.*confirm.*(?:==|!=).*INSTALL", text, re.IGNORECASE)
    assert "apt-get install" in text
    install_pos = text.index("apt-get install")
    guard_pos = text.lower().index("confirm")
    assert guard_pos < install_pos


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is unavailable on this Windows host")
def test_dgx_bootstrap_safe_runtime_paths_do_not_install() -> None:
    syntax = subprocess.run(["bash", "-n", str(BOOTSTRAP)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr

    diagnostic = subprocess.run(["bash", str(BOOTSTRAP)], input="", capture_output=True, text=True)
    assert diagnostic.returncode == 0, diagnostic.stderr
    assert "diagnostic" in diagnostic.stdout.lower()
    assert "apt-get install" not in diagnostic.stdout.lower()

    refused = subprocess.run(["bash", str(BOOTSTRAP), "--install"], input="no\n", capture_output=True, text=True)
    assert refused.returncode != 0
    assert "cancel" in (refused.stdout + refused.stderr).lower()
    assert "apt-get install" not in refused.stdout.lower()


def test_onsite_checklist_has_separate_gates_commands_and_evidence() -> None:
    text = read(CHECKLIST).lower()
    gates = (
        "ssh reachability",
        "codex, cursor, and antigravity",
        "systemd user service",
        "nvidia gpu, driver, and container",
        "local desktop and display",
        "isaac lab, isaac sim, and webrtc",
        "tmux, background execution, and disconnect recovery",
        "chatgpt mobile and codex remote",
    )
    for gate in gates:
        assert gate in text
    assert text.count("command:") >= 8
    assert text.count("evidence:") >= 8
    assert "cannot be inferred" in text
    assert "simulated local tests" in text

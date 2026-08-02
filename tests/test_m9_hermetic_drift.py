from __future__ import annotations

import os
import shutil
from pathlib import Path

from triagent.drift_validation import (
    compare_bindings,
    documentation_drift,
    profile_binding,
    release_binding,
)


ROOT = Path(__file__).resolve().parents[1]
V2_PROFILE = ROOT / "profiles" / "dgx.spark.v2.toml"
AGY = ROOT / "scripts" / "agy-review-adapter.zsh"
LEGACY_DOCUMENTS = {
    "README": ROOT / "README.md",
    "skill": ROOT / "skills" / "triagent" / "SKILL.md",
}


def test_suite_environment_is_hermetic_from_operator_configuration(tmp_path: Path) -> None:
    assert Path(os.environ["HOME"]).is_relative_to(tmp_path)
    assert os.environ["PATH"] == os.defpath
    for name in ("PYTHONPATH", "CODEX_HOME", "TRIAGENT_HOME", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        assert name not in os.environ
    assert os.environ["GIT_CONFIG_GLOBAL"] == os.devnull
    assert Path(os.environ["XDG_CONFIG_HOME"]).is_relative_to(tmp_path)


def test_v2_profile_and_own_agy_wrapper_have_a_positive_content_binding() -> None:
    binding = profile_binding(V2_PROFILE)

    assert binding["deepseek"] == {"model": "deepseek/deepseek-v4-flash", "enabled": False}
    assert binding["antigravity_wrapper"]["path"] == str(AGY)
    assert len(str(binding["profile"]["sha256"])) == 64
    assert len(str(binding["antigravity_wrapper"]["sha256"])) == 64


def test_release_snapshot_detects_profile_agy_and_document_byte_drift(tmp_path: Path) -> None:
    profile = tmp_path / "profile.toml"
    text = V2_PROFILE.read_text(encoding="utf-8").replace(str(AGY), str(tmp_path / "agy.zsh"))
    profile.write_text(text, encoding="utf-8")
    agy = tmp_path / "agy.zsh"
    shutil.copyfile(AGY, agy)
    readme = tmp_path / "README.md"
    skill = tmp_path / "SKILL.md"
    readme.write_text("v2 document\n", encoding="utf-8")
    skill.write_text("v2 skill\n", encoding="utf-8")
    docs = {"README": readme, "skill": skill}
    recorded = release_binding(profile, docs)

    profile.write_text(profile.read_text(encoding="utf-8").replace("deepseek-v4-flash", "deepseek-v4-other"), encoding="utf-8")
    agy.write_text(agy.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    readme.write_text("changed\n", encoding="utf-8")
    changes = compare_bindings(recorded, release_binding(profile, docs))

    assert "profile.sha256" in changes
    assert "antigravity_wrapper.sha256" in changes
    assert "documents.README.sha256" in changes
    assert "deepseek.model" in changes


def test_current_legacy_docs_are_reported_as_explicit_v2_drift_not_hidden() -> None:
    drift = documentation_drift(profile_binding(V2_PROFILE), LEGACY_DOCUMENTS)

    assert "README: profile-path" in drift
    assert "README: deepseek-model" in drift
    assert "skill: profile-path" in drift
    assert "skill: deepseek-model" in drift

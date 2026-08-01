from __future__ import annotations

import os
from pathlib import Path

import pytest

from triagent.runtime import ROOT_MARKER, _write_marker
from triagent.store import TaskStore


_SECRET_ENVIRONMENT = (
    "OPENAI_API_KEY", "CURSOR_API_KEY", "AGY_API_KEY", "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GH_TOKEN",
)


@pytest.fixture(autouse=True)
def hermetic_test_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests independent of operator homes, import paths, and credentials."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", os.defpath)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("TRIAGENT_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    for name in _SECRET_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def mark_direct_test_stores_as_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep legacy unit fixtures explicit without weakening production root checks."""

    monkeypatch.delenv("CODEX_HOME", raising=False)
    original_init = TaskStore.__init__

    def initialized_init(store: TaskStore, root: Path) -> None:
        root_path = Path(root)
        root_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_path.chmod(0o700)
        if not (root_path / ROOT_MARKER).exists():
            _write_marker(root_path)
        original_init(store, root_path)

    monkeypatch.setattr(TaskStore, "__init__", initialized_init)

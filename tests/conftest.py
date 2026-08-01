from __future__ import annotations

from pathlib import Path

import pytest

from triagent.runtime import ROOT_MARKER, _write_marker
from triagent.store import TaskStore


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

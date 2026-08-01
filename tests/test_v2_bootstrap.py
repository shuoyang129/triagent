from __future__ import annotations

import sys
from pathlib import Path

import triagent_v2_bootstrap


def test_bootstrap_prioritizes_its_own_distribution_before_a_foreign_editable_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    foreign = tmp_path / "legacy-src"
    monkeypatch.setattr(sys, "path", [str(foreign), *sys.path])

    source_root = triagent_v2_bootstrap.prioritize_v2_source()

    assert sys.path[0] == str(source_root)
    assert sys.path.index(str(source_root)) < sys.path.index(str(foreign))


def test_packaging_routes_the_public_v2_command_through_the_isolation_bootstrap() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"

    assert "triagent-v2 = \"triagent_v2_bootstrap:main\"" in pyproject.read_text(encoding="utf-8")

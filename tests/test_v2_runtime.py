from __future__ import annotations

import json
from pathlib import Path
import tomllib

import pytest
from typer.testing import CliRunner

from triagent.cli import app
from triagent.runtime import (
    DataRootError,
    ROOT_MARKER,
    _ROOT_FORMAT,
    resolve_v2_data_root,
)
import triagent.runtime as runtime


def test_v2_root_initialization_writes_private_marker(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "runs-v2"
    monkeypatch.setattr(runtime, "LEGACY_DATA_ROOTS", (tmp_path / "runs",))

    assert resolve_v2_data_root(root, allow_initialize=True) == root.resolve()
    assert json.loads((root / ROOT_MARKER).read_text(encoding="utf-8")) == _ROOT_FORMAT
    assert (root / ROOT_MARKER).stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("use_symlink", [False, True])
def test_v2_refuses_original_root_before_store_initialization(
    tmp_path: Path, monkeypatch, use_symlink: bool
) -> None:
    legacy = tmp_path / "original-runs"
    legacy.mkdir()
    target = legacy
    if use_symlink:
        target = tmp_path / "legacy-alias"
        target.symlink_to(legacy, target_is_directory=True)
    monkeypatch.setattr(runtime, "LEGACY_DATA_ROOTS", (legacy,))

    result = CliRunner().invoke(app, ["status", "--data-root", str(target), "task-id"])

    assert result.exit_code != 0
    assert "refuses the original triagent data root" in result.output
    assert not (legacy / "triagent.sqlite3").exists()
    assert not (legacy / ROOT_MARKER).exists()


def test_v2_rejects_inherited_legacy_root(tmp_path: Path, monkeypatch) -> None:
    legacy = tmp_path / "original-runs"
    legacy.mkdir()
    monkeypatch.setattr(runtime, "LEGACY_DATA_ROOTS", (legacy,))

    result = CliRunner().invoke(
        app, ["status", "task-id"], env={"TRIAGENT_HOME": str(legacy)}
    )

    assert result.exit_code != 0
    assert not (legacy / "triagent.sqlite3").exists()


def test_v2_rejects_unknown_marker(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "runs-v2"
    root.mkdir()
    (root / ROOT_MARKER).write_text('{"controller":"other"}\n', encoding="utf-8")
    monkeypatch.setattr(runtime, "LEGACY_DATA_ROOTS", (tmp_path / "runs",))

    with pytest.raises(DataRootError, match="different controller"):
        resolve_v2_data_root(root, allow_initialize=False)


def test_v2_version_flag() -> None:
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output == "triagent-v2 0.2.0\n"


def test_v2_profile_binds_only_the_v2_agy_wrapper() -> None:
    root = Path(__file__).resolve().parents[1]
    profile_path = root / "profiles" / "dgx.spark.v2.toml"
    profile = tomllib.loads(profile_path.read_text(encoding="utf-8"))
    command = profile["agents"]["antigravity"]["command"]

    assert command == [str(root / "scripts" / "agy-review-adapter.zsh")]
    assert profile["paths"]["runs"] == "/home/ys/works/robots/triagent-runs-v2"
    assert profile["paths"]["python"] == "/home/ys/miniforge3/envs/triagent-v2/bin/python"
    assert 'review_evidence="${review_evidence[1,40000]}"' in (
        Path(command[0]).read_text(encoding="utf-8")
    )

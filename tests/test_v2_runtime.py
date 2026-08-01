from __future__ import annotations

import json
from pathlib import Path
import tomllib

import pytest
from typer.testing import CliRunner

from triagent.cli import _profile_stream_v2_options, _root, app
from triagent.runtime import (
    DataRootError,
    ROOT_MARKER,
    _ROOT_FORMAT,
    resolve_v2_data_root,
)
from triagent.runtime_config import load_runtime_config
import triagent.runtime as runtime
import triagent.cli as cli_module


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
    assert profile["triagent"]["profile_version"] == 2
    assert _profile_stream_v2_options(profile) == {
        "cursor": True, "deepseek": True, "codex": True, "antigravity": True,
    }
    assert 'review_evidence="${review_evidence[1,40000]}"' in (
        Path(command[0]).read_text(encoding="utf-8")
    )


def test_stream_v2_defaults_to_legacy_and_requires_explicit_v2_profile() -> None:
    legacy = {
        "agents": {
            name: {"command": [name]}
            for name in ("cursor", "deepseek", "codex", "antigravity")
        }
    }
    assert _profile_stream_v2_options(legacy) == {
        "cursor": False, "deepseek": False, "codex": False, "antigravity": False,
    }
    legacy["agents"]["codex"]["stream_v2"] = True
    with pytest.raises(ValueError, match="explicit triagent v2 profile"):
        _profile_stream_v2_options(legacy)


@pytest.mark.parametrize("invalid", ["true", 1, None])
def test_stream_v2_profile_flag_must_be_boolean(invalid: object) -> None:
    config = {
        "triagent": {"profile_version": 2},
        "agents": {
            name: {"stream_v2": invalid}
            for name in ("cursor", "deepseek", "codex", "antigravity")
        },
    }
    with pytest.raises(ValueError, match="stream_v2"):
        _profile_stream_v2_options(config)


def test_doctor_wires_explicit_v2_flags_without_calling_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[1]
    profile = tmp_path / "offline.v2.toml"
    profile.write_text(
        (root / "profiles" / "dgx.spark.v2.toml").read_text(encoding="utf-8").replace(
            "enabled = false", "enabled = true"
        ),
        encoding="utf-8",
    )
    constructed: dict[str, bool] = {}

    def adapter(name: str):
        class OfflineAdapter:
            def __init__(self, *args: object, stream_v2: bool = False, **kwargs: object) -> None:
                constructed[name] = stream_v2

            def capabilities(self):
                return type("Capabilities", (), {
                    "installed": True, "authenticated": True, "ready": True,
                    "diagnostic_code": None,
                })()
        return OfflineAdapter

    monkeypatch.setattr(cli_module, "CursorAdapter", adapter("cursor"))
    monkeypatch.setattr(cli_module, "DeepSeekAdapter", adapter("deepseek"))
    monkeypatch.setattr(cli_module, "CodexAdapter", adapter("codex"))
    monkeypatch.setattr(cli_module, "AntigravityAdapter", adapter("antigravity"))

    result = CliRunner().invoke(app, ["doctor", "--profile", str(profile)])

    assert result.exit_code == 0, result.output
    assert constructed == {
        "cursor": True, "deepseek": True, "codex": True, "antigravity": True,
    }


def test_doctor_rejects_legacy_profile_that_requests_stream_v2_before_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "legacy.toml"
    profile.write_text(
        """
[agents.codex]
command = ["codex"]
stream_v2 = true
[agents.cursor]
command = ["cursor"]
[agents.antigravity]
command = ["agy"]
[agents.deepseek]
enabled = false
command = ["opencode"]
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli_module, "CodexAdapter",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("provider constructed")),
    )

    result = CliRunner().invoke(app, ["doctor", "--profile", str(profile)])

    assert result.exit_code != 0
    assert "Cannot read selected profile" in result.output


def test_v2_runtime_config_selects_data_root_and_doctor_shows_only_bindings(
    tmp_path: Path, monkeypatch
) -> None:
    configured_root = tmp_path / "configured-runs-v2"
    config = tmp_path / "runtime-v2.toml"
    config.write_text(
        f'[paths]\ndata_root = "{configured_root}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("TRIAGENT_RUNTIME_CONFIG", str(config))

    assert load_runtime_config().data_root == configured_root
    assert _root(None, allow_initialize=True) == configured_root
    result = CliRunner().invoke(app, ["doctor", "--resolved", "--profile", "fake"])

    assert result.exit_code == 0
    assert str(configured_root) in result.output
    assert "Fake: ready (no vendor calls)" in result.output

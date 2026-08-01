from __future__ import annotations

import pytest

import triagent.runtime as runtime
from triagent.runtime_config import RuntimeConfigError, load_runtime_config


def test_runtime_config_is_absent_by_default() -> None:
    config = load_runtime_config({})

    assert config.source is None
    assert config.data_root == runtime.DEFAULT_V2_DATA_ROOT
    assert "built-in default" in "\n".join(config.doctor_lines())


def test_runtime_config_resolves_only_literal_nonsecret_bindings(tmp_path) -> None:
    root = tmp_path / "runs-v2"
    executable = tmp_path / "bin" / "codex"
    source = tmp_path / "runtime-v2.toml"
    source.write_text(
        f'''[paths]
data_root = "{root}"
python = "{tmp_path}/python"

[agents.codex]
command = ["{executable}", "exec"]
''',
        encoding="utf-8",
    )

    config = load_runtime_config({"TRIAGENT_RUNTIME_CONFIG": str(source)})

    assert config.data_root == root
    assert config.commands["codex"] == (str(executable), "exec")
    output = "\n".join(config.doctor_lines())
    assert "secret" not in output.lower()
    assert str(source) in output


@pytest.mark.parametrize(
    "body",
    [
        '[paths]\ndata_root = "relative/runs"\n',
        '[paths]\ndata_root = "${HOME}/runs"\n',
        '[environment]\nOPENAI_API_KEY = "secret"\n',
        '[agents.codex]\ncommand = ["codex", "exec"]\n',
    ],
)
def test_runtime_config_fails_closed_for_untrusted_values(tmp_path, body: str) -> None:
    source = tmp_path / "runtime-v2.toml"
    source.write_text(body, encoding="utf-8")

    with pytest.raises(RuntimeConfigError):
        load_runtime_config({"TRIAGENT_RUNTIME_CONFIG": str(source)})

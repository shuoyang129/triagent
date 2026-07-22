from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
CURSOR_ADAPTER = ROOT / "scripts" / "cursor-agent-adapter.zsh"
FORCE_ADAPTER = ROOT / "scripts" / "cursor-synthetic-force-adapter.zsh"


def _fake_cursor(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "fake-cursor"
    argv_log = tmp_path / "argv.json"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

with open(os.environ["FAKE_CURSOR_ARGV_LOG"], "w", encoding="utf-8") as stream:
    json.dump(sys.argv[1:], stream)
print(json.dumps({
    "status": "passed",
    "evidence": ["fake evidence"],
    "artifacts": [],
    "changed_paths": ["app.py"],
}))
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, argv_log


def test_cursor_adapter_rewrites_text_and_emits_cursor_envelope(tmp_path: Path) -> None:
    fake_cursor, argv_log = _fake_cursor(tmp_path)
    environment = {
        **os.environ,
        "CURSOR_AGENT_BIN": str(fake_cursor),
        "FAKE_CURSOR_ARGV_LOG": str(argv_log),
    }

    result = subprocess.run(
        [str(CURSOR_ADAPTER), "--print", "--output-format", "json"],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(argv_log.read_text(encoding="utf-8")) == [
        "--print",
        "--output-format",
        "text",
    ]
    envelope = json.loads(result.stdout)
    assert envelope["type"] == "result"
    assert envelope["subtype"] == "success"
    assert envelope["is_error"] is False
    assert json.loads(envelope["result"])["changed_paths"] == ["app.py"]


def test_synthetic_force_adapter_rejects_normal_repository() -> None:
    result = subprocess.run(
        [str(FORCE_ADAPTER), "--force", "--sandbox", "enabled"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 64
    assert "refusing non-synthetic worktree" in result.stderr

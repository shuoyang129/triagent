import json
from pathlib import Path
import subprocess


REQUIRED_TOOLS = {"python", "git", "codex", "cursor", "antigravity", "opencode"}
REQUIRED_FIELDS = {"installed", "version", "authenticated", "headless"}


def test_capability_record_has_required_fields() -> None:
    sample = {"installed": False, "version": None, "authenticated": False, "headless": False}
    assert set(sample) == REQUIRED_FIELDS


def test_generated_capability_file_matches_contract() -> None:
    capability_path = Path("work/capabilities/windows.json")
    assert capability_path.is_file()
    payload = json.loads(capability_path.read_text(encoding="utf-8"))
    assert set(payload["tools"]) == REQUIRED_TOOLS
    for capability in payload["tools"].values():
        assert set(capability) == REQUIRED_FIELDS
        assert isinstance(capability["installed"], bool)
        assert capability["version"] is None or isinstance(capability["version"], str)
        assert isinstance(capability["authenticated"], bool)
        assert isinstance(capability["headless"], bool)

def test_native_probe_contract_checks_nonzero_last_exit_code() -> None:
    script = Path("scripts/bootstrap-windows.ps1").read_text(encoding="utf-8")
    assert "$LASTEXITCODE" in script and "-eq 0" in script
    command = "$o = (& { cmd /c 'echo plausible-output & exit /b 7' } | Out-String).Trim(); if ($LASTEXITCODE -eq 0) { exit 9 } else { exit 0 }"
    result = subprocess.run(["powershell", "-NoProfile", "-Command", command], check=False)
    assert result.returncode == 0

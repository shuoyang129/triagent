import json
from pathlib import Path
import shutil
import subprocess

import pytest


REQUIRED_TOOLS = {"python", "git", "codex", "cursor", "antigravity", "opencode"}
REQUIRED_FIELDS = {"installed", "version", "authenticated", "headless"}


def test_capability_record_has_required_fields() -> None:
    sample = {"installed": False, "version": None, "authenticated": False, "headless": False}
    assert set(sample) == REQUIRED_FIELDS


@pytest.mark.onsite
def test_generated_capability_file_matches_contract(request) -> None:
    selected = request.config.getoption("-m")
    if not selected or "onsite" not in selected:
        pytest.skip("select explicitly with -m onsite")
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


def test_real_host_capability_contract_is_explicitly_onsite() -> None:
    marks = getattr(test_generated_capability_file_matches_contract, "pytestmark", [])
    assert any(mark.name == "onsite" for mark in marks)

@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell is unavailable")
def test_native_probe_contract_checks_nonzero_last_exit_code() -> None:
    script = Path("scripts/bootstrap-windows.ps1").read_text(encoding="utf-8")
    assert "$LASTEXITCODE" in script and "-eq 0" in script
    command = "$o = (& { cmd /c 'echo plausible-output & exit /b 7' } | Out-String).Trim(); if ($LASTEXITCODE -eq 0) { exit 9 } else { exit 0 }"
    result = subprocess.run(["powershell", "-NoProfile", "-Command", command], check=False)
    assert result.returncode == 0


def test_native_probe_accepts_stderr_from_successful_command() -> None:
    script = Path("scripts/bootstrap-windows.ps1").read_text(encoding="utf-8")
    start = script.index("function Invoke-Probe")
    end = script.index("function New-Capability")
    invoke_probe = script[start:end]
    command = (
        '$ErrorActionPreference = "Stop";'
        f"{invoke_probe};"
        "$result = Invoke-Probe -Executable 'cmd.exe' "
        "-Arguments @('/d', '/c', 'echo warning 1>&2 & exit /b 0');"
        "if (-not $result.ok) { exit 9 }"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-Command", command], check=False)
    assert result.returncode == 0


def test_bootstrap_accepts_absolute_output_path() -> None:
    script = Path("scripts/bootstrap-windows.ps1").read_text(encoding="utf-8")
    assert "[System.IO.Path]::GetFullPath($Output)" in script
    assert "Join-Path (Get-Location) $Output" not in script

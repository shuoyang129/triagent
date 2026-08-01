"""Offline, auditable replay runner for known historical failure classes.

The fixture names a narrow set of existing contract tests.  The runner forks
pytest in a deliberately sterile environment and records only test identity,
duration, exit status and a digest of captured output.  It never records
provider output or credentials and never constructs a vendor command.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class HistoricalReplayError(ValueError):
    """Raised for an invalid replay fixture or an unsafe invocation."""


_SECRETS = (
    "OPENAI_API_KEY", "CURSOR_API_KEY", "AGY_API_KEY", "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GH_TOKEN",
)


@dataclass(frozen=True)
class ReplayCase:
    identifier: str
    failure_class: str
    nodeids: tuple[str, ...]


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_fixture(path: Path) -> tuple[str, tuple[ReplayCase, ...], str]:
    raw = Path(path).read_bytes()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HistoricalReplayError("replay fixture is not JSON") from error
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise HistoricalReplayError("replay fixture schema_version must be 1")
    suite = value.get("suite")
    cases = value.get("cases")
    if not isinstance(suite, str) or not suite or not isinstance(cases, list) or not cases:
        raise HistoricalReplayError("replay fixture requires suite and non-empty cases")
    parsed: list[ReplayCase] = []
    seen: set[str] = set()
    for item in cases:
        if not isinstance(item, dict):
            raise HistoricalReplayError("replay case must be an object")
        identifier, failure_class, nodeids = item.get("id"), item.get("class"), item.get("nodeids")
        if (not isinstance(identifier, str) or not identifier or identifier in seen or
                not isinstance(failure_class, str) or not failure_class or
                not isinstance(nodeids, list) or not nodeids or
                not all(isinstance(nodeid, str) and nodeid.startswith("tests/") and "::test_" in nodeid for nodeid in nodeids)):
            raise HistoricalReplayError("replay case is invalid")
        seen.add(identifier)
        parsed.append(ReplayCase(identifier, failure_class, tuple(nodeids)))
    return suite, tuple(parsed), _sha256(raw)


def _sterile_environment(root: Path, home: Path) -> dict[str, str]:
    """Return an environment with deliberately injected CODEX_HOME contamination.

    Test conftest must remove this input before an adapter is reached; retaining
    it here makes the regression observable rather than merely assuming the
    operator environment is clean.
    """
    environment = {"PATH": os.defpath, "HOME": str(home), "XDG_CONFIG_HOME": str(home / "xdg"),
                   "XDG_CACHE_HOME": str(home / "cache"), "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_CONFIG_GLOBAL": os.devnull, "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONPATH": str(root / "src"), "CODEX_HOME": "/historical-contamination/codex"}
    for name in _SECRETS:
        environment.pop(name, None)
    return environment


def replay_fixture(fixture: Path, *, project_root: Path, timeout_seconds: float = 45.0) -> dict[str, Any]:
    """Run every fixture case with pytest, returning an auditable redacted report."""
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise HistoricalReplayError("timeout_seconds must be positive")
    root = Path(project_root).resolve(strict=True)
    suite, cases, fixture_digest = load_fixture(fixture)
    started = time.time()
    report_cases: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="triagent-v2-replay-") as temporary:
        home = Path(temporary) / "home"; home.mkdir()
        environment = _sterile_environment(root, home)
        for case in cases:
            command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *case.nodeids]
            one_started = time.monotonic()
            try:
                completed = subprocess.run(command, cwd=root, env=environment, text=True, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, timeout=timeout_seconds, check=False)
                exit_code: int | None = completed.returncode
                output = completed.stdout
                timed_out = False
            except subprocess.TimeoutExpired as error:
                exit_code = None
                output = (error.stdout or "") if isinstance(error.stdout, str) else ""
                timed_out = True
            report_cases.append({"id": case.identifier, "class": case.failure_class, "nodeids": list(case.nodeids),
                                 "status": "passed" if exit_code == 0 else "failed", "exit_code": exit_code,
                                 "timed_out": timed_out, "duration_ms": round((time.monotonic() - one_started) * 1000),
                                 "output_sha256": _sha256(output.encode("utf-8", "replace"))})
    finished = time.time()
    passed = all(case["status"] == "passed" for case in report_cases)
    return {"schema_version": 1, "suite": suite, "fixture_sha256": fixture_digest,
            "mode": "offline-pytest-contract-replay", "provider_calls": 0,
            "code_home_contamination_injected": True, "started_at_epoch": round(started, 3),
            "finished_at_epoch": round(finished, 3), "status": "passed" if passed else "failed", "cases": report_cases}


def write_report(report: Mapping[str, Any], path: Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical(dict(report)) + b"\n"
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, target)


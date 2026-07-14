from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool


class ProcessRunner:
    def __init__(self, redactions: Sequence[str] = ()) -> None:
        self._redactions = tuple(value for value in redactions if value)

    def _redact(self, value: str) -> str:
        for secret in self._redactions:
            value = value.replace(secret, "[REDACTED]")
        return value

    def run(
        self,
        argv: Sequence[str],
        cwd: Path,
        timeout: float,
        env_allowlist: Mapping[str, str],
        stdin: str | None = None,
    ) -> ProcessResult:
        if not argv or any(not isinstance(item, str) or not item for item in argv):
            raise ValueError("argv must contain non-empty strings")
        baseline_names = (
            "PATH", "HOME", "USERPROFILE", "TEMP", "TMP", "SYSTEMROOT",
            "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL",
        )
        environment = {name: os.environ[name] for name in baseline_names if os.environ.get(name)}
        environment.setdefault("TMP", tempfile.gettempdir())
        environment.update({key: value for key, value in env_allowlist.items() if value is not None})
        try:
            completed = subprocess.run(
                list(argv),
                cwd=Path(cwd),
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                check=False,
                input=stdin,
            )
            return ProcessResult(
                completed.returncode,
                self._redact(completed.stdout),
                self._redact(completed.stderr),
                False,
            )
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout.decode("utf-8", "replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
            stderr = error.stderr.decode("utf-8", "replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
            return ProcessResult(None, self._redact(stdout), self._redact(stderr), True)

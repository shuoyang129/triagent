from __future__ import annotations

import os
import queue
import selectors
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable, Mapping, Sequence


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool


class StreamEventKind(StrEnum):
    STARTED = "started"
    LIVENESS = "liveness"
    PROGRESS = "progress"
    TERMINAL_RESULT_SEEN = "terminal-result-seen"
    FINALIZING = "finalizing"
    COMPLETED = "completed"


@dataclass(frozen=True)
class StreamEvent:
    kind: StreamEventKind
    at_monotonic: float
    stream: str | None = None
    text: str = ""


@dataclass(frozen=True)
class StreamPolicy:
    """Bounds for an isolated v2 provider process.

    ``startup_timeout`` covers the interval before the first bytes arrive.
    ``idle_timeout`` is refreshed only by the caller's meaningful-progress
    classifier; ordinary output still produces liveness events.  A positive
    ``hard_timeout`` always wins, even when output continues.  Once a terminal
    result is seen, ``finalize_grace`` is the only remaining wait budget.
    """

    startup_timeout: float
    idle_timeout: float
    hard_timeout: float
    finalize_grace: float
    terminate_grace: float = 2.0
    max_output_bytes: int = 256 * 1024

    def __post_init__(self) -> None:
        values = (
            self.startup_timeout,
            self.idle_timeout,
            self.hard_timeout,
            self.finalize_grace,
            self.terminate_grace,
        )
        if any(not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0 for value in values):
            raise ValueError("stream policy timeouts must be positive numbers")
        if not isinstance(self.max_output_bytes, int) or isinstance(self.max_output_bytes, bool) or self.max_output_bytes < 1:
            raise ValueError("max_output_bytes must be a positive integer")


@dataclass(frozen=True)
class StreamingProcessResult(ProcessResult):
    """Sanitized, bounded evidence and controller events for a v2 process."""

    events: tuple[StreamEvent, ...]
    timeout_reason: str | None = None
    terminal_result_seen: bool = False
    output_truncated: bool = False


class _StreamingRedactor:
    """Redact across read boundaries before a chunk becomes observable."""

    def __init__(self, secrets: Sequence[str]) -> None:
        self._secrets = tuple(sorted({secret for secret in secrets if secret}, key=len, reverse=True))
        self._hold = max((len(secret) for secret in self._secrets), default=1) - 1
        self._pending = ""

    def feed(self, value: str) -> str:
        self._pending += value
        if len(self._pending) <= self._hold:
            return ""
        safe, self._pending = self._pending[:-self._hold], self._pending[-self._hold:]
        return self._redact(safe)

    def finish(self) -> str:
        result = self._redact(self._pending)
        self._pending = ""
        return result

    def _redact(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, "[REDACTED]")
        return value


class StreamingProcessRunner:
    """Streaming v2 runner.  ``ProcessRunner`` intentionally stays legacy.

    POSIX uses selectors directly.  Windows pipes are not selector-compatible,
    so the same bounded queue protocol is fed by two reader threads there.
    Every process is isolated in a fresh group; Windows additionally receives a
    kill-on-close Job Object when the platform API is available.
    """

    def __init__(self, redactions: Sequence[str] = ()) -> None:
        self._redactions = tuple(value for value in redactions if value)

    @staticmethod
    def _environment(env_allowlist: Mapping[str, str]) -> dict[str, str]:
        baseline_names = (
            "PATH", "HOME", "USERPROFILE", "TEMP", "TMP", "SYSTEMROOT",
            "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL",
        )
        environment = {name: os.environ[name] for name in baseline_names if os.environ.get(name)}
        environment.setdefault("TMP", tempfile.gettempdir())
        environment.update({key: value for key, value in env_allowlist.items() if value is not None})
        return environment

    def run(
        self,
        argv: Sequence[str],
        cwd: Path,
        policy: StreamPolicy,
        env_allowlist: Mapping[str, str],
        stdin: str | None = None,
        *,
        is_progress: Callable[[str, str], bool] | None = None,
        is_terminal_result: Callable[[str, str], bool] | None = None,
    ) -> StreamingProcessResult:
        if not argv or any(not isinstance(item, str) or not item for item in argv):
            raise ValueError("argv must contain non-empty strings")
        now = time.monotonic
        events: list[StreamEvent] = []
        started = now()
        events.append(StreamEvent(StreamEventKind.STARTED, started))
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        popen_kwargs: dict[str, object] = {
            "cwd": Path(cwd), "env": self._environment(env_allowlist), "stdin": subprocess.PIPE if stdin is not None else None,
            "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": False,
            "shell": False, "start_new_session": os.name != "nt", "creationflags": creation_flags,
        }
        try:
            process = subprocess.Popen(list(argv), **popen_kwargs)  # type: ignore[arg-type]
        except OSError:
            raise
        job = _WindowsJob.attach(process) if os.name == "nt" else None
        if stdin is not None and process.stdin is not None:
            try:
                process.stdin.write(stdin.encode("utf-8"))
                process.stdin.close()
            except OSError:
                pass

        buffers = {"stdout": [], "stderr": []}
        retained = {"stdout": 0, "stderr": 0}
        truncated = False
        redactors = {name: _StreamingRedactor(self._redactions) for name in buffers}
        first_output: float | None = None
        meaningful_at = started
        terminal_at: float | None = None
        timeout_reason: str | None = None

        def emit(kind: StreamEventKind, stream: str | None = None, text: str = "") -> None:
            events.append(StreamEvent(kind, now(), stream, text))

        def consume(stream: str, raw: bytes) -> None:
            nonlocal first_output, meaningful_at, terminal_at, truncated
            if first_output is None:
                first_output = now()
            decoded = raw.decode("utf-8", "replace")
            clean = redactors[stream].feed(decoded)
            # Classification occurs inside this trusted controller before any
            # bytes are exposed.  It must not depend on redaction carry-over:
            # a secret spanning chunks deliberately delays observable text.
            if is_progress is not None and is_progress(stream, decoded):
                meaningful_at = now()
                emit(StreamEventKind.PROGRESS, stream)
            if terminal_at is None and is_terminal_result is not None and is_terminal_result(stream, decoded):
                terminal_at = now()
                emit(StreamEventKind.TERMINAL_RESULT_SEEN, stream)
                emit(StreamEventKind.FINALIZING)
            if not clean:
                return
            emit(StreamEventKind.LIVENESS, stream, clean)
            available = policy.max_output_bytes - retained[stream]
            if available > 0:
                clipped = clean.encode("utf-8")[:available].decode("utf-8", "ignore")
                buffers[stream].append(clipped)
                retained[stream] += len(clipped.encode("utf-8"))
                truncated = truncated or clipped != clean
            else:
                truncated = True

        reader = _PipeReader(process) if os.name == "nt" else _SelectorReader(process)
        try:
            while True:
                for stream, raw in reader.read(0.05):
                    consume(stream, raw)
                current = now()
                if process.poll() is not None and reader.done:
                    break
                if current - started >= policy.hard_timeout:
                    timeout_reason = "hard-timeout"
                elif terminal_at is not None and current - terminal_at >= policy.finalize_grace:
                    timeout_reason = "finalize-timeout"
                elif first_output is None and current - started >= policy.startup_timeout:
                    timeout_reason = "startup-timeout"
                elif first_output is not None and terminal_at is None and current - meaningful_at >= policy.idle_timeout:
                    timeout_reason = "idle-timeout"
                if timeout_reason is not None:
                    self._terminate_tree(process, policy.terminate_grace, job)
                    break
        finally:
            reader.close()
            if process.poll() is None:
                self._terminate_tree(process, policy.terminate_grace, job)
            if job is not None:
                job.close()
        for stream, redactor in redactors.items():
            clean = redactor.finish()
            if clean:
                emit(StreamEventKind.LIVENESS, stream, clean)
                available = policy.max_output_bytes - retained[stream]
                if available > 0:
                    clipped = clean.encode("utf-8")[:available].decode("utf-8", "ignore")
                    buffers[stream].append(clipped)
                    retained[stream] += len(clipped.encode("utf-8"))
                    truncated = truncated or clipped != clean
                else:
                    truncated = True
        returncode = process.poll()
        timed_out = timeout_reason is not None
        emit(StreamEventKind.COMPLETED)
        return StreamingProcessResult(
            returncode, "".join(buffers["stdout"]), "".join(buffers["stderr"]), timed_out,
            tuple(events), timeout_reason, terminal_at is not None, truncated,
        )

    @staticmethod
    def _terminate_tree(process: subprocess.Popen[bytes], grace: float, job: "_WindowsJob | None") -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            if job is not None:
                job.terminate()
            else:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        try:
            process.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            pass
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
        elif job is not None:
            job.terminate()
        process.wait(timeout=grace)


class _SelectorReader:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._selector = selectors.DefaultSelector()
        assert process.stdout is not None and process.stderr is not None
        self._selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        self._selector.register(process.stderr, selectors.EVENT_READ, "stderr")

    @property
    def done(self) -> bool:
        return not self._selector.get_map()

    def read(self, timeout: float) -> list[tuple[str, bytes]]:
        result: list[tuple[str, bytes]] = []
        for key, _ in self._selector.select(timeout):
            raw = os.read(key.fileobj.fileno(), 8192)
            if raw:
                result.append((key.data, raw))
            else:
                self._selector.unregister(key.fileobj)
        return result

    def close(self) -> None:
        self._selector.close()


class _PipeReader:
    """Windows fallback: threads make blocking pipe reads selector-like."""
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._queue: queue.Queue[tuple[str, bytes | None]] = queue.Queue()
        self._remaining = 2
        assert process.stdout is not None and process.stderr is not None
        self._threads = [threading.Thread(target=self._pump, args=(name, pipe), daemon=True) for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr))]
        for thread in self._threads:
            thread.start()

    def _pump(self, name: str, pipe) -> None:
        try:
            while chunk := pipe.read(8192):
                self._queue.put((name, chunk))
        finally:
            self._queue.put((name, None))

    @property
    def done(self) -> bool:
        return self._remaining == 0

    def read(self, timeout: float) -> list[tuple[str, bytes]]:
        result: list[tuple[str, bytes]] = []
        try:
            first = self._queue.get(timeout=timeout)
            pending = [first]
            while True:
                pending.append(self._queue.get_nowait())
        except queue.Empty:
            pass
        for name, chunk in pending if 'pending' in locals() else []:
            if chunk is None:
                self._remaining -= 1
            else:
                result.append((name, chunk))
        return result

    def close(self) -> None:
        for thread in self._threads:
            thread.join(timeout=0.1)


class _WindowsJob:
    """Best-effort kill-on-close Job Object; exercised on Windows only."""
    def __init__(self, handle: int) -> None:
        self._handle = handle

    @classmethod
    def attach(cls, process: subprocess.Popen[bytes]) -> "_WindowsJob | None":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return None
            # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; basic limit information is
            # embedded at offset zero of JOBOBJECT_EXTENDED_LIMIT_INFORMATION.
            info = (ctypes.c_byte * 144)()
            ctypes.c_uint32.from_buffer(info, 16).value = 0x00002000
            if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), len(info)):
                kernel32.CloseHandle(job)
                return None
            handle = wintypes.HANDLE(process._handle)
            if not kernel32.AssignProcessToJobObject(job, handle):
                kernel32.CloseHandle(job)
                return None
            return cls(job)
        except (AttributeError, OSError):
            return None

    def terminate(self) -> None:
        try:
            import ctypes
            ctypes.WinDLL("kernel32", use_last_error=True).TerminateJobObject(self._handle, 1)
        except OSError:
            pass

    def close(self) -> None:
        try:
            import ctypes
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self._handle)
        except OSError:
            pass


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

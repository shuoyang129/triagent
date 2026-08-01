"""Controller-owned, durable terminal-result records for v2 provider calls.

This module deliberately has no provider or TaskStore dependency.  A provider
can only emit data; the trusted controller validates it and persists the final
record below the v2 run directory, never below a provider worktree.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
_IDENTITY = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ROLES = frozenset({"implementer", "verifier", "reviewer"})


class CompletionError(RuntimeError):
    """A completion record or its controller directory is not trustworthy."""


class CompletionAlreadyConsumed(CompletionError):
    """The durable result was already consumed by a controller invocation."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _validate_digest(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise CompletionError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _validate_identity(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise CompletionError(f"{name} is invalid")
    return value


def _validate_uuid(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise CompletionError(f"{name} must be a UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise CompletionError(f"{name} must be a UUID") from error
    if str(parsed) != value.lower():
        raise CompletionError(f"{name} must be a canonical UUID")
    return value.lower()


def _restricted(path: Path, mode: int) -> None:
    if os.name != "nt":
        path.chmod(mode)


def _assert_not_link(path: Path) -> None:
    # lstat, rather than resolve(), catches the link itself before it can
    # redirect controller-owned files outside the protected run root.
    if path.is_symlink():
        raise CompletionError(f"links are forbidden in completion control paths: {path}")


def _assert_existing_chain_not_links(path: Path) -> None:
    current = path
    while True:
        if current.exists() or current.is_symlink():
            _assert_not_link(current)
        if current.parent == current:
            return
        current = current.parent


def _atomic_write(path: Path, payload: bytes) -> None:
    _assert_existing_chain_not_links(path.parent)
    _assert_not_link(path)
    # Fixed ``result.json.tmp``-style names make the crash boundary auditable.
    # A stale regular temporary file can only be a previous interrupted atomic
    # write inside the private controller directory, and is never consumable.
    temporary = path.with_suffix(path.suffix + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        _assert_not_link(temporary)
        temporary.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _restricted(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any]:
    _assert_not_link(path)
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise CompletionError(f"invalid completion JSON: {path.name}") from error
    if not isinstance(value, dict):
        raise CompletionError(f"completion JSON must be an object: {path.name}")
    return value


@dataclass(frozen=True)
class CompletionBinding:
    task_id: str
    call_id: str
    provider: str
    role: str
    input_digest: str
    profile_digest: str
    runtime_manifest_digest: str

    def __post_init__(self) -> None:
        _validate_uuid(self.task_id, "task_id")
        _validate_uuid(self.call_id, "call_id")
        _validate_identity(self.provider, "provider")
        if self.role not in _ROLES:
            raise CompletionError("role is invalid")
        for name in ("input_digest", "profile_digest", "runtime_manifest_digest"):
            _validate_digest(getattr(self, name), name)

    def payload(self) -> dict[str, str]:
        return {"task_id": self.task_id, "call_id": self.call_id, "provider": self.provider, "role": self.role,
                "input_digest": self.input_digest, "profile_digest": self.profile_digest,
                "runtime_manifest_digest": self.runtime_manifest_digest}


@dataclass(frozen=True)
class DurableResult:
    binding: CompletionBinding
    candidate_commit: str
    outcome: Mapping[str, Any]
    written_at: str
    result_digest: str

    def payload_without_digest(self) -> dict[str, object]:
        return {"schema_version": 1, **self.binding.payload(), "candidate_commit": self.candidate_commit,
                "outcome": dict(self.outcome), "written_at": self.written_at}

    def payload(self) -> dict[str, object]:
        return {**self.payload_without_digest(), "result_digest": self.result_digest}


class CompletionControl:
    """Private call directory and durable result lifecycle.

    ``runs_root`` must be the v2 ``runs`` directory.  The caller supplies the
    provider worktree so we can reject every overlap in either direction.
    """

    def __init__(self, runs_root: Path, binding: CompletionBinding, provider_worktree: Path) -> None:
        self.binding = binding
        self.runs_root = Path(runs_root)
        self.provider_worktree = Path(provider_worktree)
        _assert_existing_chain_not_links(self.runs_root)
        _assert_existing_chain_not_links(self.provider_worktree)
        resolved_runs = self.runs_root.resolve(strict=True)
        resolved_worktree = self.provider_worktree.resolve(strict=True)
        self.task_root = resolved_runs / binding.task_id
        self.control_root = self.task_root / "control" / binding.call_id
        candidate = self.control_root.resolve(strict=False)
        if candidate == resolved_worktree or candidate.is_relative_to(resolved_worktree) or resolved_worktree.is_relative_to(candidate):
            raise CompletionError("completion control path must not overlap provider worktree")
        self._create_private_directory()

    @property
    def input_manifest_path(self) -> Path: return self.control_root / "input-manifest.json"
    @property
    def events_path(self) -> Path: return self.control_root / "events.jsonl"
    @property
    def heartbeat_path(self) -> Path: return self.control_root / "heartbeat.json"
    @property
    def result_path(self) -> Path: return self.control_root / "result.json"
    @property
    def receipt_path(self) -> Path: return self.control_root / "consumed.json"

    def _create_private_directory(self) -> None:
        self.task_root.mkdir(mode=0o700, parents=False, exist_ok=True)
        _assert_existing_chain_not_links(self.task_root)
        control_parent = self.task_root / "control"
        control_parent.mkdir(mode=0o700, exist_ok=True)
        _assert_not_link(control_parent)
        self.control_root.mkdir(mode=0o700, exist_ok=True)
        _assert_not_link(self.control_root)
        for directory in (self.task_root, control_parent, self.control_root):
            _restricted(directory, 0o700)

    def write_input_manifest(self, input_manifest: Mapping[str, Any]) -> str:
        """Persist the exact non-secret call input and return its digest."""
        value = dict(input_manifest)
        value_digest = _digest(value)
        if value_digest != self.binding.input_digest:
            raise CompletionError("input manifest digest conflicts with call binding")
        if self.input_manifest_path.exists():
            existing = _read_json(self.input_manifest_path)
            existing_digest = _digest(existing)
            if existing_digest != self.binding.input_digest:
                raise CompletionError("existing input manifest digest conflicts with call binding")
            return existing_digest
        _atomic_write(self.input_manifest_path, _canonical(value))
        return value_digest

    def append_event(self, event: Mapping[str, Any]) -> None:
        value = dict(event)
        value.setdefault("at", _now())
        raw = _canonical(value) + b"\n"
        _assert_existing_chain_not_links(self.control_root)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.events_path, flags, 0o600)
        try:
            os.write(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _restricted(self.events_path, 0o600)

    def heartbeat(self, details: Mapping[str, Any] | None = None) -> None:
        value: dict[str, Any] = {"schema_version": 1, "task_id": self.binding.task_id, "call_id": self.binding.call_id, "at": _now()}
        if details:
            value["details"] = dict(details)
        _atomic_write(self.heartbeat_path, _canonical(value))

    def write_result(self, *, candidate_commit: str, outcome: Mapping[str, Any], written_at: str | None = None) -> DurableResult:
        if not _COMMIT.fullmatch(candidate_commit):
            raise CompletionError("candidate_commit must be a lowercase Git SHA")
        if not isinstance(outcome, Mapping):
            raise CompletionError("outcome must be an object")
        if not self.input_manifest_path.exists():
            raise CompletionError("input manifest must be durable before a result")
        recorded_input = _digest(_read_json(self.input_manifest_path))
        if recorded_input != self.binding.input_digest:
            raise CompletionError("input manifest has changed")
        if self.result_path.exists():
            existing = self.read_result()
            candidate = DurableResult(self.binding, candidate_commit, dict(outcome), written_at or existing.written_at, "")
            if existing.payload_without_digest() != candidate.payload_without_digest():
                raise CompletionError("durable result already exists with different content")
            return existing
        provisional = DurableResult(self.binding, candidate_commit, dict(outcome), written_at or _now(), "")
        record = DurableResult(provisional.binding, provisional.candidate_commit, provisional.outcome, provisional.written_at, _digest(provisional.payload_without_digest()))
        _atomic_write(self.result_path, _canonical(record.payload()))
        return record

    def read_result(self, *, expected_candidate_commit: str | None = None) -> DurableResult:
        value = _read_json(self.result_path)
        if value.get("schema_version") != 1:
            raise CompletionError("unsupported completion schema")
        binding = CompletionBinding(**{key: value.get(key) for key in CompletionBinding.__dataclass_fields__})
        if binding != self.binding:
            raise CompletionError("completion result binding mismatch")
        candidate = value.get("candidate_commit")
        if not isinstance(candidate, str) or not _COMMIT.fullmatch(candidate):
            raise CompletionError("completion candidate is invalid")
        if expected_candidate_commit is not None and candidate != expected_candidate_commit:
            raise CompletionError("completion candidate changed")
        outcome = value.get("outcome")
        written_at = value.get("written_at")
        result_digest = value.get("result_digest")
        if not isinstance(outcome, dict) or not isinstance(written_at, str):
            raise CompletionError("completion result has invalid content")
        _validate_digest(result_digest, "result_digest")
        record = DurableResult(binding, candidate, outcome, written_at, result_digest)
        if _digest(record.payload_without_digest()) != result_digest:
            raise CompletionError("completion result digest mismatch")
        return record

    def consume_once(self, *, expected_candidate_commit: str | None = None, before_receipt: Callable[[], None] | None = None) -> DurableResult:
        """Claim a validated record exactly once without re-invoking a provider.

        The receipt is intentionally durable.  A restart after the result rename
        but before the receipt claims the existing result; a restart after the
        receipt observes ``CompletionAlreadyConsumed`` and must not make a new
        provider call.  A future TaskStore integration can make its state update
        in the same controller recovery transaction before recording the receipt.
        """
        result = self.read_result(expected_candidate_commit=expected_candidate_commit)
        _assert_not_link(self.receipt_path)
        if self.receipt_path.exists():
            raise CompletionAlreadyConsumed("completion result was already consumed")
        if before_receipt is not None:
            before_receipt()
        receipt = {"schema_version": 1, "task_id": self.binding.task_id, "call_id": self.binding.call_id,
                   "result_digest": result.result_digest, "consumed_at": _now()}
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.receipt_path, flags, 0o600)
        except FileExistsError as error:
            raise CompletionAlreadyConsumed("completion result was already consumed") from error
        try:
            raw = _canonical(receipt)
            os.write(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _restricted(self.receipt_path, 0o600)
        directory = os.open(self.control_root, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
        return result


def find_recoverable_result(
    runs_root: Path,
    *,
    task_id: str,
    provider: str,
    role: str,
    profile_digest: str,
    runtime_manifest_digest: str,
    candidate_commit: str,
    provider_worktree: Path,
) -> tuple[CompletionControl, DurableResult] | None:
    """Locate one valid durable result for an exact controller binding."""
    task_root = Path(runs_root).resolve(strict=True) / task_id
    control_parent = task_root / "control"
    if not control_parent.exists():
        return None
    _assert_existing_chain_not_links(control_parent)
    for child in sorted(control_parent.iterdir()):
        _assert_not_link(child)
        if not child.is_dir() or child.name.startswith("."):
            raise CompletionError("invalid completion control directory")
        result_path = child / "result.json"
        if not result_path.exists():
            continue
        payload = _read_json(result_path)
        try:
            binding = CompletionBinding(
                **{key: payload.get(key) for key in CompletionBinding.__dataclass_fields__}
            )
        except CompletionError:
            raise CompletionError("invalid completion result binding") from None
        if (
            binding.task_id != task_id
            or binding.call_id != child.name
            or binding.provider != provider
            or binding.role != role
            or binding.profile_digest != profile_digest
            or binding.runtime_manifest_digest != runtime_manifest_digest
        ):
            continue
        control = CompletionControl(Path(runs_root), binding, provider_worktree)
        record = control.read_result(expected_candidate_commit=candidate_commit)
        return control, record
    return None

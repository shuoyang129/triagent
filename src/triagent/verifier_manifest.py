"""Strict, provider-free executor for trusted verification manifests.

The manifest is deliberately data only: commands are argv arrays (never a
shell string), paths are relative to a supplied workspace, and the caller is
responsible for retaining an immutable copy before an implementer can edit the
workspace.  This module does not know about projects or milestones.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Protocol, Sequence

from triagent.adapters.process import StreamPolicy, StreamingProcessResult, StreamingProcessRunner


class VerificationManifestError(ValueError):
    pass


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_SOURCES = {"stdout", "stderr", "artifact"}


def _strict_keys(value: Mapping[str, Any], allowed: set[str], required: set[str], label: str) -> None:
    if set(value) - allowed or required - set(value):
        raise VerificationManifestError(f"{label} has unknown or missing fields")


def _string(value: object, label: str, *, limit: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or "\\x00" in value:
        raise VerificationManifestError(f"{label} must be a non-empty string")
    return value


def _integer(value: object, label: str, low: int, high: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise VerificationManifestError(f"{label} is out of range")
    return value


def _relative(value: object, label: str) -> str:
    text = _string(value, label)
    path = PurePosixPath(text)
    if path.is_absolute() or "\\" in text or any(part in {"", ".", ".."} for part in path.parts):
        raise VerificationManifestError(f"{label} must be a safe relative path")
    return path.as_posix()


def _inside(path: str, allowed: Sequence[str]) -> bool:
    candidate = PurePosixPath(path)
    return any(candidate == PurePosixPath(root) or PurePosixPath(root) in candidate.parents for root in allowed)


@dataclass(frozen=True)
class ArtifactRule:
    path: str
    sha256: str
    required: bool


@dataclass(frozen=True)
class EvidenceRule:
    label: str
    source: str
    contains: str
    artifact: str | None = None


@dataclass(frozen=True)
class VerificationManifest:
    identifier: str
    argv: tuple[str, ...]
    cwd: str
    timeout_seconds: int
    output_max_bytes: int
    allowed_paths: tuple[str, ...]
    exit_codes: tuple[int, ...]
    artifacts: tuple[ArtifactRule, ...]
    evidence: tuple[EvidenceRule, ...]
    digest: str

    @classmethod
    def from_bytes(cls, raw: bytes, suffix: str) -> "VerificationManifest":
        try:
            if suffix.lower() == ".toml":
                loaded = tomllib.loads(raw.decode("utf-8"))
            elif suffix.lower() == ".json":
                loaded = json.loads(raw.decode("utf-8"))
            else:
                raise VerificationManifestError("manifest must be TOML or JSON")
        except (UnicodeDecodeError, tomllib.TOMLDecodeError, json.JSONDecodeError) as error:
            raise VerificationManifestError("manifest is malformed") from error
        if not isinstance(loaded, dict):
            raise VerificationManifestError("manifest must be an object")
        return cls._parse(loaded, hashlib.sha256(raw).hexdigest())

    @classmethod
    def load(cls, path: Path, *, trusted_root: Path | None = None) -> "VerificationManifest":
        source = Path(path)
        if source.is_symlink() or not source.is_file():
            raise VerificationManifestError("manifest must be a regular file")
        resolved = source.resolve(strict=True)
        if trusted_root is not None:
            root = Path(trusted_root).resolve(strict=True)
            if root not in resolved.parents:
                raise VerificationManifestError("manifest is outside trusted root")
        return cls.from_bytes(source.read_bytes(), source.suffix)

    @classmethod
    def _parse(cls, value: Mapping[str, Any], digest: str) -> "VerificationManifest":
        _strict_keys(value, {"schema_version", "id", "execution", "allowed_paths", "exit", "artifacts", "evidence"},
                     {"schema_version", "id", "execution", "allowed_paths", "exit", "artifacts", "evidence"}, "manifest")
        if value["schema_version"] != 1 or isinstance(value["schema_version"], bool):
            raise VerificationManifestError("unsupported manifest schema_version")
        identifier = _string(value["id"], "id", limit=100)
        if not _ID.fullmatch(identifier):
            raise VerificationManifestError("id has invalid characters")
        execution = value["execution"]
        if not isinstance(execution, dict): raise VerificationManifestError("execution must be an object")
        _strict_keys(execution, {"argv", "cwd", "timeout_seconds", "output_max_bytes"},
                     {"argv", "cwd", "timeout_seconds", "output_max_bytes"}, "execution")
        argv_raw = execution["argv"]
        if not isinstance(argv_raw, list) or not 1 <= len(argv_raw) <= 64:
            raise VerificationManifestError("execution.argv must be a bounded array")
        argv = tuple(_string(part, "execution.argv") for part in argv_raw)
        if "/" in argv[0] or "\\" in argv[0] or argv[0].startswith("."):
            raise VerificationManifestError("execution argv[0] must be a PATH command name")
        cwd = _relative(execution["cwd"], "execution.cwd")
        timeout = _integer(execution["timeout_seconds"], "execution.timeout_seconds", 1, 3600)
        output_max = _integer(execution["output_max_bytes"], "execution.output_max_bytes", 1024, 1024 * 1024)
        paths_raw = value["allowed_paths"]
        if not isinstance(paths_raw, list) or not 1 <= len(paths_raw) <= 50:
            raise VerificationManifestError("allowed_paths must be a bounded array")
        allowed = tuple(_relative(item, "allowed_paths") for item in paths_raw)
        if len(set(allowed)) != len(allowed): raise VerificationManifestError("allowed_paths must be unique")
        if not _inside(cwd, allowed): raise VerificationManifestError("execution.cwd is outside allowed_paths")
        for argument in argv[1:]:
            if argument.startswith("/") or "\\" in argument:
                raise VerificationManifestError("execution argv contains an absolute path")
            if argument.startswith(".") or "/" in argument:
                candidate = argument[2:] if argument.startswith("./") else argument
                if not _inside(_relative(candidate, "execution argv path"), allowed):
                    raise VerificationManifestError("execution argv path is outside allowed_paths")
        exit_value = value["exit"]
        if not isinstance(exit_value, dict): raise VerificationManifestError("exit must be an object")
        _strict_keys(exit_value, {"codes"}, {"codes"}, "exit")
        codes_raw = exit_value["codes"]
        if not isinstance(codes_raw, list) or not 1 <= len(codes_raw) <= 32:
            raise VerificationManifestError("exit.codes must be a bounded array")
        codes = tuple(_integer(item, "exit code", 0, 255) for item in codes_raw)
        if len(set(codes)) != len(codes): raise VerificationManifestError("exit.codes must be unique")
        artifacts_raw = value["artifacts"]
        if not isinstance(artifacts_raw, list) or len(artifacts_raw) > 50: raise VerificationManifestError("artifacts must be an array")
        artifacts: list[ArtifactRule] = []
        for item in artifacts_raw:
            if not isinstance(item, dict): raise VerificationManifestError("artifact must be an object")
            _strict_keys(item, {"path", "sha256", "required"}, {"path", "sha256", "required"}, "artifact")
            path = _relative(item["path"], "artifact.path")
            digest_value = _string(item["sha256"], "artifact.sha256", limit=64)
            if not _HEX.fullmatch(digest_value): raise VerificationManifestError("artifact.sha256 must be lowercase sha256")
            if not isinstance(item["required"], bool): raise VerificationManifestError("artifact.required must be boolean")
            if not _inside(path, allowed): raise VerificationManifestError("artifact is outside allowed_paths")
            artifacts.append(ArtifactRule(path, digest_value, item["required"]))
        if len({item.path for item in artifacts}) != len(artifacts): raise VerificationManifestError("artifact paths must be unique")
        evidence_raw = value["evidence"]
        if not isinstance(evidence_raw, list) or len(evidence_raw) > 50: raise VerificationManifestError("evidence must be an array")
        evidence: list[EvidenceRule] = []
        for item in evidence_raw:
            if not isinstance(item, dict): raise VerificationManifestError("evidence must be an object")
            _strict_keys(item, {"label", "source", "contains", "artifact"}, {"label", "source", "contains"}, "evidence")
            label = _string(item["label"], "evidence.label", limit=100)
            source = _string(item["source"], "evidence.source", limit=16)
            if source not in _SOURCES: raise VerificationManifestError("evidence.source is invalid")
            artifact = item.get("artifact")
            if source == "artifact":
                artifact = _relative(artifact, "evidence.artifact")
                if artifact not in {rule.path for rule in artifacts}: raise VerificationManifestError("evidence artifact is undeclared")
            elif artifact is not None: raise VerificationManifestError("only artifact evidence may name an artifact")
            evidence.append(EvidenceRule(label, source, _string(item["contains"], "evidence.contains", limit=500), artifact))
        if len({item.label for item in evidence}) != len(evidence): raise VerificationManifestError("evidence labels must be unique")
        return cls(identifier, argv, cwd, timeout, output_max, allowed, codes, tuple(artifacts), tuple(evidence), digest)


class _Runner(Protocol):
    def run(self, argv: Sequence[str], cwd: Path, policy: StreamPolicy, env_allowlist: Mapping[str, str], stdin: str | None = None, **kwargs: object) -> StreamingProcessResult: ...


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    returncode: int | None
    timed_out: bool
    manifest_digest: str
    evidence: tuple[str, ...]
    artifacts: tuple[str, ...]
    failures: tuple[str, ...]


class ManifestExecutor:
    """Run a parsed trusted manifest in one workspace with no shell or env input."""
    def __init__(self, workspace: Path, runner: _Runner | None = None) -> None:
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir(): raise VerificationManifestError("workspace must be a directory")
        self.runner = runner or StreamingProcessRunner()

    def _path(self, relative: str) -> Path:
        path = (self.workspace / relative).resolve(strict=False)
        if self.workspace != path and self.workspace not in path.parents:
            raise VerificationManifestError("resolved path escapes workspace")
        return path

    def execute(self, manifest: VerificationManifest) -> VerificationResult:
        cwd = self._path(manifest.cwd)
        if not cwd.is_dir() or cwd.is_symlink(): raise VerificationManifestError("execution.cwd is not a regular directory")
        policy = StreamPolicy(min(30, manifest.timeout_seconds), manifest.timeout_seconds, manifest.timeout_seconds, min(10, manifest.timeout_seconds), max_output_bytes=manifest.output_max_bytes)
        result = self.runner.run(manifest.argv, cwd, policy, {})
        failures: list[str] = []
        if result.timed_out: failures.append("command timed out")
        if result.returncode not in manifest.exit_codes: failures.append("unexpected exit code")
        artifact_data: dict[str, str] = {}
        artifacts: list[str] = []
        for rule in manifest.artifacts:
            path = self._path(rule.path)
            if path.is_symlink() or not path.is_file():
                if rule.required: failures.append(f"missing artifact: {rule.path}")
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != rule.sha256: failures.append(f"artifact hash mismatch: {rule.path}")
            else: artifacts.append(rule.path)
            artifact_data[rule.path] = path.read_text(encoding="utf-8", errors="replace")
        extracted: list[str] = []
        for rule in manifest.evidence:
            source = result.stdout if rule.source == "stdout" else result.stderr if rule.source == "stderr" else artifact_data.get(rule.artifact or "", "")
            if rule.contains not in source: failures.append(f"missing evidence: {rule.label}")
            else: extracted.append(rule.label)
        return VerificationResult(not failures, result.returncode, result.timed_out, manifest.digest, tuple(extracted), tuple(artifacts), tuple(failures))

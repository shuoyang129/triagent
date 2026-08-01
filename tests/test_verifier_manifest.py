from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from triagent.verifier_manifest import ManifestExecutor, VerificationManifest, VerificationManifestError


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _value() -> dict[str, object]:
    return {
        "schema_version": 1, "id": "offline-smoke",
        "execution": {"argv": [Path(sys.executable).name, "-c", "print('VERIFIED')"], "cwd": "work", "timeout_seconds": 10, "output_max_bytes": 4096},
        "allowed_paths": ["work", "artifacts"], "exit": {"codes": [0]},
        "artifacts": [{"path": "artifacts/result.txt", "sha256": _hash("artifact-ok"), "required": True}],
        "evidence": [{"label": "stdout-marker", "source": "stdout", "contains": "VERIFIED"}, {"label": "artifact-marker", "source": "artifact", "artifact": "artifacts/result.txt", "contains": "artifact-ok"}],
    }


def test_toml_and_json_have_the_same_strict_schema(tmp_path: Path) -> None:
    value = _value()
    json_manifest = VerificationManifest.from_bytes(json.dumps(value).encode(), ".json")
    toml = """schema_version = 1\nid = \"offline-smoke\"\nallowed_paths = [\"work\", \"artifacts\"]\n[[artifacts]]\npath = \"artifacts/result.txt\"\nsha256 = \"%s\"\nrequired = true\n[[evidence]]\nlabel = \"stdout-marker\"\nsource = \"stdout\"\ncontains = \"VERIFIED\"\n[[evidence]]\nlabel = \"artifact-marker\"\nsource = \"artifact\"\nartifact = \"artifacts/result.txt\"\ncontains = \"artifact-ok\"\n[execution]\nargv = [\"%s\", \"-c\", \"print('VERIFIED')\"]\ncwd = \"work\"\ntimeout_seconds = 10\noutput_max_bytes = 4096\n[exit]\ncodes = [0]\n""" % (_hash("artifact-ok"), Path(sys.executable).name)
    toml_manifest = VerificationManifest.from_bytes(toml.encode(), ".toml")
    assert json_manifest.identifier == toml_manifest.identifier
    assert json_manifest.argv == toml_manifest.argv


@pytest.mark.parametrize("mutate", [
    lambda value: value.update({"unexpected": True}),
    lambda value: value["execution"].update({"shell": True}),  # type: ignore[index]
    lambda value: value["execution"].update({"argv": ["/bin/sh", "-c", "true"]}),  # type: ignore[index]
    lambda value: value.update({"allowed_paths": ["work", "work"]}),
    lambda value: value["artifacts"][0].update({"path": "../outside"}),  # type: ignore[index]
    lambda value: value["evidence"][0].update({"source": "artifact"}),  # type: ignore[index]
])
def test_schema_rejects_unsafe_or_ambiguous_input(mutate) -> None:
    value = _value(); mutate(value)
    with pytest.raises(VerificationManifestError): VerificationManifest.from_bytes(json.dumps(value).encode(), ".json")


def test_executor_runs_only_argv_and_verifies_artifact_hash_and_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"; (root / "work").mkdir(parents=True); (root / "artifacts").mkdir(); (root / "artifacts" / "result.txt").write_text("artifact-ok")
    # PATH command is intentionally a simple name.  This makes the test fully local and provider-free.
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent))
    manifest = VerificationManifest.from_bytes(json.dumps(_value()).encode(), ".json")
    result = ManifestExecutor(root).execute(manifest)
    assert result.passed and result.evidence == ("stdout-marker", "artifact-marker") and result.artifacts == ("artifacts/result.txt",)


def test_executor_reports_timeout_exit_artifact_and_evidence_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"; (root / "work").mkdir(parents=True); (root / "artifacts").mkdir(); (root / "artifacts" / "result.txt").write_text("wrong")
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent))
    value = _value(); value["execution"]["argv"] = [Path(sys.executable).name, "-c", "import sys; sys.exit(3)"]  # type: ignore[index]
    result = ManifestExecutor(root).execute(VerificationManifest.from_bytes(json.dumps(value).encode(), ".json"))
    assert not result.passed
    assert "unexpected exit code" in result.failures and "artifact hash mismatch: artifacts/result.txt" in result.failures and "missing evidence: stdout-marker" in result.failures


def test_loader_requires_regular_file_under_trusted_root(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"; trusted.mkdir(); outside = tmp_path / "outside.json"; outside.write_text(json.dumps(_value()))
    with pytest.raises(VerificationManifestError, match="outside trusted root"): VerificationManifest.load(outside, trusted_root=trusted)
    link = trusted / "link.json"; link.symlink_to(outside)
    with pytest.raises(VerificationManifestError, match="regular file"): VerificationManifest.load(link, trusted_root=trusted)

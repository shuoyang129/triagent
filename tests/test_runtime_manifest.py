from __future__ import annotations

import hashlib

from triagent.runtime_manifest import build_runtime_manifest, compare_manifests


def _manifest(environ: dict[str, str] | None = None) -> dict[str, object]:
    return build_runtime_manifest(
        profile_digest=hashlib.sha256(b"fake-v1").hexdigest(),
        providers={
            "implementer": ("fake", ("fake",), None, "fake-1"),
            "verifier": ("fake", ("fake",), None, "fake-1"),
            "reviewer": ("fake", ("fake",), None, "fake-1"),
        },
        environ=environ,
    )


def test_manifest_is_deterministic_and_never_contains_unrelated_secret() -> None:
    secret = "test-secret-value-must-not-persist"
    manifest = _manifest({"OPENAI_API_KEY": secret})

    assert manifest == _manifest({"OPENAI_API_KEY": secret})
    assert secret not in str(manifest)
    assert manifest["secret_presence"] == {}
    assert manifest["timeout_policy"]["seconds"] == 900
    assert len(manifest["timeout_policy"]["digest"]) == 64


def test_compare_returns_only_deterministic_field_paths() -> None:
    before = _manifest({"TRIAGENT_AGENT_TIMEOUT_SECONDS": "900"})
    after = _manifest({"TRIAGENT_AGENT_TIMEOUT_SECONDS": "901"})

    assert compare_manifests(before, after) == [
        "timeout_policy.digest", "timeout_policy.seconds"
    ]

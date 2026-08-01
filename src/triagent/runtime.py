from __future__ import annotations

import json
import os
import stat
from pathlib import Path


DEFAULT_V2_DATA_ROOT = Path("/home/ys/works/robots/triagent-runs-v2")
LEGACY_DATA_ROOTS = (Path("/home/ys/works/robots/triagent-runs"),)
ROOT_MARKER = ".triagent-v2-root.json"
_ROOT_FORMAT = {
    "format": "triagent-data-root",
    "controller": "triagent-v2",
    "schema_version": 1,
}


class DataRootError(ValueError):
    """Raised before a v2 command can open an incompatible data root."""


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_legacy_root(root: Path) -> bool:
    return any(root == _resolved(legacy) for legacy in LEGACY_DATA_ROOTS)


def _write_marker(root: Path) -> None:
    marker = root / ROOT_MARKER
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(_ROOT_FORMAT, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        marker.unlink(missing_ok=True)
        raise


def _validate_marker(root: Path, *, allow_initialize: bool) -> None:
    marker = root / ROOT_MARKER
    if not marker.exists():
        if not allow_initialize:
            raise DataRootError("v2 data root is uninitialized")
        if any(root.iterdir()):
            raise DataRootError("refusing to initialize a non-empty unmarked data root")
        _write_marker(root)
        return
    if marker.is_symlink() or not marker.is_file():
        raise DataRootError("v2 data-root marker must be a regular file")
    if stat.S_IMODE(marker.stat().st_mode) & 0o077:
        raise DataRootError("v2 data-root marker permissions are too broad")
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DataRootError("v2 data-root marker is unreadable") from error
    if value != _ROOT_FORMAT:
        raise DataRootError("data root belongs to a different controller or schema")


def resolve_v2_data_root(value: Path | None, *, allow_initialize: bool) -> Path:
    """Return a marked v2 root without ever opening the original task database."""

    if value is None and "TRIAGENT_HOME" in os.environ:
        raise DataRootError("TRIAGENT_HOME is unsupported by triagent-v2; pass --data-root")
    root = _resolved(value or DEFAULT_V2_DATA_ROOT)
    if _is_legacy_root(root):
        raise DataRootError("triagent-v2 refuses the original triagent data root")
    if root.exists() and not root.is_dir():
        raise DataRootError("v2 data root must be a directory")
    if not root.exists():
        if not allow_initialize:
            raise DataRootError("v2 data root does not exist")
        root.mkdir(mode=0o700, parents=True)
    if stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise DataRootError("v2 data-root permissions are too broad")
    _validate_marker(root, allow_initialize=allow_initialize)
    return root

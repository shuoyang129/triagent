"""Isolated public entry point for the v2 controller.

The legacy controller and v2 intentionally use the same internal ``triagent``
package name while they coexist. This bootstrap runs before importing that
package and gives the installed v2 source root precedence, so a stray legacy
editable-install path cannot silently make ``triagent-v2`` execute v1 code.
"""

from __future__ import annotations

import sys
from pathlib import Path


def prioritize_v2_source() -> Path:
    """Put this installed distribution source root first on ``sys.path``."""
    source_root = Path(__file__).resolve().parent.parent
    root = str(source_root)
    sys.path[:] = [root, *(entry for entry in sys.path if entry != root)]
    return source_root


def main() -> None:
    prioritize_v2_source()
    from triagent.cli import app

    app()

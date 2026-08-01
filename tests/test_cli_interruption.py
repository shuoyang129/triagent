from __future__ import annotations

import signal

import pytest

from triagent.cli import _recover_interruption


def test_sigterm_guard_converts_supervisor_stop_to_recoverable_exception() -> None:
    previous = signal.getsignal(signal.SIGTERM)
    with pytest.raises(InterruptedError, match="controller interrupted"):
        with _recover_interruption():
            signal.raise_signal(signal.SIGTERM)
    assert signal.getsignal(signal.SIGTERM) == previous

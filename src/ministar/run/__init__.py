"""One run at a time, process-wide.

There is exactly one camera and one loop, so a module-level singleton is the honest
model rather than a limitation: two runs would be two writers to one shutter. The screen
asks :func:`active` every tick and starts or stops through :func:`start` / :func:`stop`,
which means a page refresh cannot orphan a running loop — the runner outlives the DOM.

Tests use the same API and call :func:`stop` in a fixture, because a leaked daemon
thread holding the fake camera is exactly the kind of cross-test coupling that produces
a mysterious failure in an unrelated file.
"""

from __future__ import annotations

import threading

from ministar.run.runner import RunConfig, Runner, RunSnapshot

_ACTIVE: Runner | None = None
_LOCK = threading.Lock()

__all__ = ["RunConfig", "RunSnapshot", "Runner", "active", "start", "stop"]


def active() -> Runner | None:
    """The runner in this process, or ``None``."""
    return _ACTIVE


def start(**kwargs) -> Runner:
    """Stop any previous run and start a new one. Returns the live runner."""
    global _ACTIVE
    with _LOCK:
        if _ACTIVE is not None:
            _ACTIVE.stop()
        runner = Runner(**kwargs)
        _ACTIVE = runner
    runner.start()
    return runner


def stop() -> None:
    """Stop the loop and forget it, including any temporary frame directory."""
    global _ACTIVE
    with _LOCK:
        runner, _ACTIVE = _ACTIVE, None
    if runner is not None:
        runner.stop()

"""The reframe sub-cycle's timing rules.

Brief 12 §6 makes the reframe a *sub-cycle* inside RUN rather than a re-entry into the
wizard: pause, re-solve, instruct, confirm, resume. The geometry lives in
:mod:`ministar.pointing` (there is already a `best_start` and a `frame_advice`); what is
left is the one piece of state that has no natural home elsewhere -- how long the aim has
been good enough.

**Why a dwell instead of an instant.** A hand on the mount passes *through* the target on
its way somewhere else: an instantaneous check would resume the run mid-nudge, with the
frame smeared. Requiring the aim to stay inside tolerance for a few seconds turns "it was
close once" into "it is there", and costs nothing but patience. Fifteen seconds is long
enough to be deliberate and short enough not to feel broken.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: How long the aim must stay inside tolerance before the run resumes by itself.
REFRAME_CONFIRM_S = 15.0


@dataclass
class Dwell:
    """Accumulates *continuous* time spent inside tolerance.

    Deliberately not "time since the first good sample": the check must reset the moment
    the aim leaves tolerance, or a slow sweep across the marker would accumulate its way
    to a resume while never actually stopping on it.
    """

    window_s: float = REFRAME_CONFIRM_S
    _since: float | None = field(default=None, repr=False)
    _held_s: float = field(default=0.0, repr=False)

    def update(self, now: float, satisfied: bool) -> bool:
        """Feed one observation. ``True`` when the window has been held long enough."""
        if not satisfied:
            self.reset()
            return False
        if self._since is None:
            self._since = now
        self._held_s = max(0.0, now - self._since)
        return self._held_s >= self.window_s

    def reset(self) -> None:
        self._since = None
        self._held_s = 0.0

    @property
    def held_s(self) -> float:
        """Seconds held so far, for the readout that says why it has not resumed yet."""
        return self._held_s


__all__ = ["REFRAME_CONFIRM_S", "Dwell"]

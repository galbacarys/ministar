"""The dwell that decides when a reframe is finished.

The only rule here is "continuous, not accumulated": a hand sweeping across the marker must
not add up to a resume. The runner owns the state, the page feeds observations, and this is
the arithmetic both rely on.
"""

from __future__ import annotations

from ministar.run.reframe import REFRAME_CONFIRM_S, Dwell


def test_the_window_is_fifteen_seconds() -> None:
    assert REFRAME_CONFIRM_S == 15.0


def test_the_dwell_needs_the_full_window() -> None:
    dwell = Dwell()
    assert dwell.update(0.0, True) is False
    assert dwell.update(14.9, True) is False
    assert dwell.held_s == 14.9
    assert dwell.update(15.0, True) is True


def test_leaving_tolerance_resets_the_clock() -> None:
    dwell = Dwell()
    for now in (0.0, 5.0, 10.0):
        assert dwell.update(now, True) is False
    assert dwell.update(11.0, False) is False
    assert dwell.held_s == 0.0
    assert dwell.update(20.0, True) is False
    assert dwell.held_s == 0.0


def test_a_sweep_through_the_marker_does_not_confirm() -> None:
    """The reason this is a dwell at all: passing *through* the target is not stopping on it."""
    dwell = Dwell()
    assert dwell.update(0.0, True) is False
    assert dwell.update(3.0, False) is False  # kept moving
    assert dwell.update(5.0, True) is False
    assert dwell.update(19.0, True) is False  # 14 s since it settled, not 19
    assert dwell.update(20.0, True) is True


def test_a_shorter_window_can_be_asked_for() -> None:
    dwell = Dwell(window_s=2.0)
    assert dwell.update(0.0, True) is False
    assert dwell.update(2.0, True) is True

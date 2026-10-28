"""Is this frame worth solving? The policy, and the stillness tracker behind it."""

from __future__ import annotations

import math

import numpy as np
import pytest

from ministar import quality


# -- HFD on a comparable scale ---------------------------------------------
def test_hfd_in_arcseconds_survives_a_binned_live_view() -> None:
    """The whole reason the conversion exists: 4.93x binning must not change the answer."""
    from ministar.rig import RigProfile

    optics = RigProfile.fake().optics()
    full_res = quality.hfd_arcsec(2.6, width_px=optics.width_px, width_arcsec=optics.width_arcsec)
    # The same star seen through a 4.93x binned live view: fewer, bigger pixels, so the same
    # star is 4.93x *smaller* in pixels.
    live = quality.hfd_arcsec(
        2.6 / 4.93, width_px=optics.width_px / 4.93, width_arcsec=optics.width_arcsec
    )

    assert live == pytest.approx(full_res, rel=1e-6)
    assert full_res == pytest.approx(2.6 * 6.7747, rel=1e-4)


def test_hfd_is_nan_rather_than_a_lie_without_a_frame_size() -> None:
    assert math.isnan(quality.hfd_arcsec(2.6, width_px=0, width_arcsec=35200.0))
    assert math.isnan(quality.hfd_arcsec(float("nan"), width_px=5184, width_arcsec=35200.0))


# -- the focus verdict ------------------------------------------------------
def test_a_sharp_frame_against_its_baseline_says_nothing() -> None:
    assert quality.focus_verdict(2.6, 2.6) is None
    assert quality.focus_verdict(4.9, 2.6) is None, "under 2x is not worth a sentence"


def test_a_soft_frame_says_how_soft_and_what_it_was() -> None:
    verdict = quality.focus_verdict(6.1, 2.6)

    assert verdict is not None
    assert '6.1"' in verdict and '2.6"' in verdict
    assert "focus has drifted" in verdict


def test_a_saturated_frame_is_diagnosed_not_rounded_off() -> None:
    """The measured real case: every bright core clipped, so HFD is nan."""
    verdict = quality.focus_verdict(float("nan"), 2.6)

    assert verdict is not None and "cannot judge focus" in verdict


def test_no_baseline_means_no_verdict() -> None:
    assert quality.focus_verdict(9.9, None) is None
    assert quality.focus_verdict(9.9, float("nan")) is None


# -- the motion verdict -----------------------------------------------------
def test_silence_is_not_stillness_but_it_is_also_not_an_accusation() -> None:
    assert quality.motion_verdict(-1.0) is None


def test_a_moving_mount_is_reported_in_terms_of_the_picture() -> None:
    assert quality.motion_verdict(2.5) is None, "drift and noise live here"
    verdict = quality.motion_verdict(9.0)

    assert verdict is not None and "mount moved" in verdict


# -- where it measures ------------------------------------------------------
def test_the_gate_looks_at_the_middle_of_the_frame() -> None:
    image = np.arange(100 * 200, dtype=np.uint16).reshape(100, 200)
    crop = quality.centre_crop(image, size=20)

    assert crop.shape == (20, 20)
    assert crop[0, 0] == image[40, 90]


def test_a_frame_smaller_than_the_crop_is_its_own_crop() -> None:
    image = np.zeros((50, 60), dtype=np.uint16)
    assert quality.centre_crop(image, size=1200).shape == (50, 60)


# -- the stillness tracker --------------------------------------------------
class _Snap:
    def __init__(self, motion: float, hfd: float) -> None:
        self.motion = motion
        self.hfd = hfd


def _feed(tracker: quality.Stillness, samples, *, step: float = 0.5) -> None:
    for index, (motion, hfd) in enumerate(samples):
        tracker.observe(_Snap(motion, hfd), index * step)


def test_a_drifting_sharp_sky_is_still() -> None:
    tracker = quality.Stillness()
    _feed(tracker, [(-1.0, 2.6), (2.4, 2.6), (2.5, 2.7), (2.4, 2.6)])

    assert not tracker.moving
    assert tracker.describe() == "mount still"


def test_a_defocused_frame_is_not_mistaken_for_a_moving_mount() -> None:
    """Stably soft means focus; erratically soft means the mount. This is the distinction."""
    tracker = quality.Stillness()
    _feed(tracker, [(2.4, 6.1), (2.5, 6.0), (2.4, 6.2), (2.3, 5.9)])

    assert not tracker.moving
    assert tracker.hfd_spread < quality.HFD_UNSTABLE_FRACTION


def test_a_hand_on_the_knob_shows_up_in_the_picture() -> None:
    tracker = quality.Stillness()
    _feed(tracker, [(9.0, 3.1), (2.4, 3.0), (7.0, 2.9), (2.4, 3.2)])

    assert tracker.moving
    assert "levels/s" in tracker.describe()


def test_sharpness_swinging_is_enough_on_its_own() -> None:
    """The signal that survives when the thumbnail rate is too deaf to see a small bump."""
    tracker = quality.Stillness()
    _feed(tracker, [(2.4, 3.1), (2.4, 6.8), (2.4, 2.9), (2.4, 5.4)])

    assert tracker.moving
    assert "sharpness swinging" in tracker.describe()


def test_samples_older_than_the_window_are_forgotten() -> None:
    tracker = quality.Stillness(window_s=2.0)
    _feed(tracker, [(9.0, 3.0), (2.4, 3.0), (2.4, 3.0), (2.4, 3.0), (2.4, 3.0), (2.4, 3.0)])

    assert not tracker.moving, "the bump was five samples ago"


def test_before_any_live_view_it_admits_it_knows_nothing() -> None:
    assert quality.Stillness().describe() == "no live view yet"

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
    assert quality.motion_verdict(0.9) is None, "drift and noise live here"
    verdict = quality.motion_verdict(6.0)

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
    _feed(tracker, [(-1.0, 2.6), (0.9, 2.6), (1.0, 2.7), (0.9, 2.6)])

    assert not tracker.moving
    assert tracker.describe().startswith("mount still")


def test_a_noisy_field_raises_its_own_threshold() -> None:
    """The reported bug: a *static* scene read as moving.

    Measured on an EOS 60D pointed at a fixed star field: 0.9 grey levels between frames, whole
    spread 0.8-1.2 over 59 frames. The limit has to come from the field, because a noisier body
    (higher ISO, darker lens) has a higher floor and a single constant cannot serve both.
    """
    tracker = quality.Stillness()
    # A quiet field: this is the measured static case, and it must not fire.
    _feed(tracker, [(0.9, 2.6), (1.0, 2.6), (0.9, 2.7), (1.1, 2.6), (0.9, 2.6), (1.0, 2.7)])
    assert not tracker.moving, f"a static field fired: {tracker.describe()}"

    # A noisier body: a floor of ~4 levels raises the limit with it, so 5.0 is still quiet.
    noisy = quality.Stillness()
    _feed(noisy, [(4.0, 2.6), (4.2, 2.6), (3.9, 2.7), (4.1, 2.6), (4.0, 2.6), (4.1, 2.7)])
    noisy.observe(_Snap(5.0, 2.6), 2.6)
    assert not noisy.moving, "the limit did not follow the field's own noise"
    assert noisy.threshold >= 3 * 3.9
    assert "of" in noisy.describe(), "the readout should say what it is comparing against"


def test_a_solve_exposure_change_can_be_forgotten() -> None:
    """A solve changes ISO/Tv and restores them; that transition is not a moving mount."""
    tracker = quality.Stillness()
    _feed(tracker, [(0.9, 2.6)] * 6)
    tracker.observe(_Snap(40.0, 2.6), 3.0)  # the solve exposure's much noisier frames
    assert tracker.moving

    tracker.reset()

    assert not tracker.moving, "the transition was still being judged after the reset"
    assert tracker.motion == -1.0
    assert tracker.describe() == "no live view yet"


def test_a_one_second_settle_judges_only_the_last_second() -> None:
    """The window is 5 s; "the hand has stopped" must not take 5 s to notice."""
    tracker = quality.Stillness()
    for index in range(8):
        tracker.observe(_Snap(0.9, 2.6), index * 0.25)

    tracker.observe(_Snap(40.0, 2.6), 2.0)  # a hand on the head, inside the window
    assert not tracker.steady_for(1.0, now=2.6)
    assert not tracker.steady_for(1.0, now=2.9)

    tracker.observe(_Snap(0.9, 2.6), 3.0)
    tracker.observe(_Snap(0.9, 2.6), 3.3)
    assert tracker.steady_for(1.0, now=3.3), "a full second of quiet was not enough"

    # One sample is not a settle: it can be the gap between a pulse and the frame that shows it.
    assert not quality.Stillness().steady_for(1.0, now=10.0)


def test_a_real_bump_still_fires_above_a_noisy_floor() -> None:
    """Raising the limit must not make the gate deaf: a hand moves whole cells, not 10%."""
    tracker = quality.Stillness()
    _feed(
        tracker,
        [(4.0, 2.6), (3.9, 2.6), (4.1, 2.6), (4.0, 2.6), (3.9, 2.6), (4.0, 2.6), (4.1, 2.6)],
    )
    assert not tracker.moving

    tracker.observe(_Snap(40.0, 2.6), 3.0)
    assert tracker.moving
    assert "40.0 levels between frames" in tracker.describe()


def test_without_a_floor_the_old_behaviour_stands() -> None:
    """A synthetic field has no noise to measure; the absolute floor is what it gets."""
    assert quality.motion_threshold(-1.0) == quality.MOVING_LEVELS_PER_FRAME
    assert quality.motion_verdict(1.0) is None
    assert quality.motion_verdict(5.0) is not None


def test_a_defocused_frame_is_not_mistaken_for_a_moving_mount() -> None:
    """Stably soft means focus; erratically soft means the mount. This is the distinction."""
    tracker = quality.Stillness()
    _feed(tracker, [(0.9, 6.1), (1.0, 6.0), (0.9, 6.2), (0.8, 5.9)])

    assert not tracker.moving
    assert tracker.hfd_spread < quality.HFD_UNSTABLE_FRACTION


def test_a_hand_on_the_knob_shows_up_in_the_picture() -> None:
    tracker = quality.Stillness()
    _feed(tracker, [(9.0, 3.1), (0.9, 3.0), (7.0, 2.9), (0.9, 3.2)])

    assert tracker.moving
    assert "between frames" in tracker.describe()


def test_sharpness_swinging_is_enough_on_its_own() -> None:
    """The signal that survives when the thumbnail rate is too deaf to see a small bump."""
    tracker = quality.Stillness()
    # A sustained swing: the sharpness is not settling, which is the mount (or the focus)
    # rather than frame noise. Six samples, because the spread needs a quartile to exist.
    _feed(tracker, [(0.9, 3.1), (0.9, 6.8), (0.9, 2.9), (0.9, 5.4), (0.9, 3.3), (0.9, 6.1)])

    assert tracker.moving
    assert "sharpness swinging" in tracker.describe()


def test_samples_older_than_the_window_are_forgotten() -> None:
    tracker = quality.Stillness(window_s=2.0)
    # One bump at t=0, then quiet: the window is 5 s, so the bump must be forgotten.
    _feed(tracker, [(9.0, 3.0), (0.9, 3.0), (0.9, 3.0), (0.9, 3.0), (0.9, 3.0), (0.9, 3.0)])

    assert not tracker.moving, "the bump was five samples ago"


def test_before_any_live_view_it_admits_it_knows_nothing() -> None:
    assert quality.Stillness().describe() == "no live view yet"


# -- when to stop spending solves -------------------------------------------
def test_one_failure_is_bad_luck_and_two_are_a_pattern() -> None:
    """Each attempt already contains a hinted solve and a blind retry, so two is four tries."""
    assert quality.GIVE_UP_AFTER == 2
    assert not quality.should_hold_off(0)
    assert not quality.should_hold_off(1)
    assert quality.should_hold_off(2)
    assert quality.should_hold_off(3)


def test_hfd_jitter_on_a_static_field_is_not_movement() -> None:
    """Measured: 72 frames on a motionless 60D with a monitor starfield ran 4.81-7.10 px.

    That is 38% as max-minus-min and 73% over some windows -- enough to call a motionless mount
    moving. As an interquartile spread the same data is 12%, so the gate has to use the robust
    statistic. Frame-to-frame jitter is the *measurement*, not the mount.
    """
    tracker = quality.Stillness()
    jitter = [5.58, 5.08, 6.04, 5.68, 6.15, 6.07, 5.56, 6.73, 6.05, 6.86, 5.58, 4.89]
    _feed(tracker, [(0.9, hfd) for hfd in jitter])

    assert tracker.hfd_spread < quality.HFD_UNSTABLE_FRACTION
    assert not tracker.moving, tracker.describe()


# -- the operator's override -------------------------------------------------
def test_the_override_never_holds_the_shutter() -> None:
    """The gate is a guess about the picture; the operator can see the mount.

    This exists because the gate was wrong on a *static* scene: a starfield displayed on a
    monitor beats against the rolling shutter, and no amount of tuning makes a heuristic
    reliable against every artefact of a test bench. "Inform, never block" applies to the gate
    itself, so the decision has to be the operator's.
    """
    reading = 40.0  # far above any threshold
    assert quality.motion_blocked(reading, ignore=True) is False
    assert quality.motion_blocked(reading, ignore=False) is True


def test_the_override_does_not_hide_what_was_seen() -> None:
    """Turning the check off changes the decision, not the evidence: it can still be quoted."""
    verdict = quality.motion_verdict(40.0)
    assert verdict is not None and "40.0" in verdict
    assert quality.motion_blocked(40.0, ignore=True) is False

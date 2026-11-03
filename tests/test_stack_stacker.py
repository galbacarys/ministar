"""The live stack against a sky that actually moves.

These are integration tests: real frames from the fake camera, the real transform, the
real accumulator. The claims they defend are the ones a user would notice as a *picture*
rather than an error -- the stars line up, a bump is caught, an empty frame is named, and
the coverage readout shrinks as the field rotates. Brief 15 is the specification.
"""

from __future__ import annotations

import math

import cv2
import numpy as np
import pytest

from ministar.stack import frames
from ministar.stack.stacker import RESIDUAL_TOL_PX, LiveStack
from ministar.stack.transform import FieldModel


def add_frame(stack: LiveStack, sky, *, seconds: float = 0.0, exposure_s: float = 2.0):
    if seconds:
        sky.advance(seconds)
    return stack.add(sky.frame(), sky.when, exposure_s=exposure_s)


def test_a_drifting_field_stacks_without_smearing(sky) -> None:
    stack = LiveStack(sky.model, bin_factor=2, display_width=0)
    first = sky.frame()
    results = [stack.add(first, sky.when, exposure_s=2.0)]
    for _ in range(4):
        results.append(add_frame(stack, sky, seconds=20.0))

    assert all(result.accepted for result in results), [r.reason for r in results]
    # Adjacent predictions are within a fraction of a pixel of the measurement.
    assert all(result.residual_px < 1.0 for result in results[1:])

    # And the *picture* is aligned: the stack matches the first frame, not a smeared average.
    assert stack._acc is not None
    stacked = stack._acc.mean().astype(np.float32).mean(axis=2)
    reference = frames.prepare(first, bin_factor=2, bayer=None).cube.mean(axis=2)
    window = cv2.createHanningWindow((reference.shape[1], reference.shape[0]), cv2.CV_32F)
    (dx, dy), response = cv2.phaseCorrelate(reference, stacked, window)
    assert math.hypot(dx, dy) < 0.5
    assert response > 0.5


def test_the_coverage_readout_shrinks_as_the_field_rotates(sky) -> None:
    stack = LiveStack(sky.model, bin_factor=2, display_width=0)
    for index in range(5):
        add_frame(stack, sky, seconds=20.0 if index else 0.0)

    coverage = stack.stats().coverage
    assert coverage.fraction == pytest.approx(1.0)  # every pixel saw something
    assert coverage.fully < 0.95  # not every pixel saw every frame
    assert coverage.rect is not None
    _x, _y, width, _height = coverage.rect
    assert width < stack.stats().width


def test_an_empty_frame_is_rejected_with_a_reason(sky) -> None:
    stack = LiveStack(sky.model, bin_factor=2, display_width=0)
    add_frame(stack, sky)
    dark = np.zeros((320, 480), dtype=np.uint8)
    result = stack.add(dark, sky.when, exposure_s=2.0)

    assert not result.accepted
    assert "no signal" in result.reason
    assert stack.stats().rejected == 1
    assert stack.stats().reasons[0][0] == result.reason


def test_a_bumped_mount_is_rejected_rather_than_aligned(sky) -> None:
    """The one thing the pole model cannot predict, and the reason the residual exists."""
    stack = LiveStack(sky.model, bin_factor=2, display_width=0)
    add_frame(stack, sky, seconds=0.0)
    add_frame(stack, sky, seconds=20.0)

    ra, dec = sky.camera.pointing
    # A tenth of a degree is ~53 native pixels here: unmistakable to the correlation, and
    # still leaving most of the frame overlapping so the residual is measurable at all.
    sky.camera.set_pointing(ra + 0.1, dec)
    result = add_frame(stack, sky, seconds=20.0)

    assert not result.accepted
    assert "mount moved" in result.reason
    assert result.residual_px > RESIDUAL_TOL_PX
    assert stack.stats().accepted == 2


def test_a_re_solve_keeps_the_canvas_and_the_bumped_frame(sky) -> None:
    """Brief 15 §4: a bump is recovered by solving the frame, not by throwing the stack away."""
    stack = LiveStack(sky.model, bin_factor=2, display_width=0)
    first = sky.frame()
    stack.add(first, sky.when, exposure_s=2.0)
    add_frame(stack, sky, seconds=20.0)

    ra, dec = sky.camera.pointing
    sky.camera.set_pointing(ra + 0.1, dec)
    bumped = add_frame(stack, sky, seconds=20.0)
    assert not bumped.accepted and "mount moved" in bumped.reason

    # What ASTAP would report on that frame: where the camera is *actually* pointing now.
    solved = FieldModel(
        ra_deg=sky.camera.pointing[0],
        dec_deg=sky.camera.pointing[1],
        pa_y_deg=sky.camera.roll_deg,
        plate_scale_arcsec=sky.optics.plate_scale_arcsec,
        width_px=480,
        height_px=320,
        epoch=sky.when,
    )
    assert stack.rebase(solved) > 0.5

    after = stack.add(sky.frame(), sky.when, exposure_s=2.0)
    assert after.accepted
    assert "mount moved" not in after.reason

    # The canvas did not move with the model: the stack still matches its first frame.
    reference = frames.prepare(first, bin_factor=2, bayer=None).cube.mean(axis=2)
    stacked = stack._acc.mean().astype(np.float32).mean(axis=2)
    window = cv2.createHanningWindow((reference.shape[1], reference.shape[0]), cv2.CV_32F)
    (dx, dy), response = cv2.phaseCorrelate(reference, stacked, window)
    assert math.hypot(dx, dy) < 1.0
    assert response > 0.5


def test_a_far_move_is_reported_as_a_mostly_new_field(sky) -> None:
    stack = LiveStack(sky.model, bin_factor=2, display_width=0)
    add_frame(stack, sky)
    ra, dec = sky.camera.pointing
    sky.camera.set_pointing(ra + 3.0, dec)  # degrees: far outside this 54' x 36' frame
    solved = FieldModel(
        ra_deg=sky.camera.pointing[0],
        dec_deg=sky.camera.pointing[1],
        pa_y_deg=sky.camera.roll_deg,
        plate_scale_arcsec=sky.optics.plate_scale_arcsec,
        width_px=480,
        height_px=320,
        epoch=sky.when,
    )
    assert stack.rebase(solved) < 0.1


def test_every_captured_frame_is_either_stacked_or_explained(sky) -> None:
    stack = LiveStack(sky.model, bin_factor=2, display_width=0)
    add_frame(stack, sky)
    stack.add(np.zeros((320, 480), dtype=np.uint8), sky.when, exposure_s=2.0)
    add_frame(stack, sky, seconds=20.0)

    stats = stack.stats()
    assert stats.frames == 3
    assert stats.accepted + stats.rejected == stats.frames
    assert stats.accepted == 2
    assert sum(count for _reason, count in stats.reasons) == stats.rejected


def test_the_preview_is_a_frozen_jpeg(sky) -> None:
    stack = LiveStack(sky.model, bin_factor=2, display_width=0, stretch_after=2)
    add_frame(stack, sky)
    first = stack.stats().jpeg
    assert first.startswith(b"\xff\xd8")  # JPEG SOI
    add_frame(stack, sky, seconds=20.0)
    second = stack.stats().jpeg
    assert second.startswith(b"\xff\xd8")
    # Changing the stretch is explicit; asking twice otherwise returns the same bytes.
    assert stack.stats().jpeg is second


def test_the_half_size_stack_uses_a_coarser_grid_without_changing_the_sky(sky) -> None:
    full = LiveStack(sky.model, bin_factor=2, display_width=0)
    half = LiveStack(sky.model, bin_factor=4, display_width=0)
    first = sky.frame()
    full.add(first, sky.when, exposure_s=2.0)
    half.add(first, sky.when, exposure_s=2.0)
    assert full.stats().width == half.stats().width * 2
    assert full.stats().height == half.stats().height * 2
    # Both accepted the same frame: the model was re-scaled with the grid.
    assert full.stats().accepted == half.stats().accepted == 1


def test_a_mono_frame_keeps_its_single_channel(sky) -> None:
    stack = LiveStack(sky.model, bin_factor=2, display_width=0)
    add_frame(stack, sky)
    assert stack._acc is not None
    assert stack._acc.channels == 1
    assert stack.stats().jpeg


def test_the_stretch_freezes_and_restretch_re_measures_it(sky) -> None:
    stack = LiveStack(sky.model, bin_factor=2, display_width=0, stretch_after=1)
    add_frame(stack, sky)
    frozen = stack._stf
    assert frozen is not None
    add_frame(stack, sky, seconds=20.0)
    assert stack._stf is frozen  # held, so the picture cannot pulse between frames
    stack.restretch()
    assert stack._stf is not frozen


def test_a_bin_factor_below_two_is_refused(sky) -> None:
    with pytest.raises(ValueError):
        LiveStack(sky.model, bin_factor=1)


def test_the_preview_is_cropped_to_the_fully_covered_rectangle(sky) -> None:
    """15 §3.4: the edges get fewer frames; showing them makes the picture look lopsided."""
    stack = LiveStack(sky.model, bin_factor=2, display_width=0)
    for index in range(6):
        add_frame(stack, sky, seconds=20.0 if index else 0.0)

    coverage = stack.stats().coverage
    assert coverage.rect is not None
    _x, _y, width, height = coverage.rect
    preview = cv2.imdecode(np.frombuffer(stack.stats().jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert preview.shape[:2] == (height, width)
    assert width < stack.stats().width  # the crop really is smaller than the canvas


def test_the_preview_centres_on_a_target_that_was_aimed_at(sky) -> None:
    """The complaint this fixes: a centred aim plus a usable-area crop puts the target
    toward a corner, because the fully-covered rectangle shrinks away from one side."""
    target = sky.camera.pointing
    stack = LiveStack(
        sky.model,
        bin_factor=2,
        display_width=0,
        target_ra_deg=target[0],
        target_dec_deg=target[1],
    )
    add_frame(stack, sky)

    stats = stack.stats()
    assert stats.crop_rule == "target"
    assert stats.crop is not None
    _x, _y, width, height = stats.crop
    assert width >= stats.width - 1
    assert height >= stats.height - 1


def test_the_preview_will_not_zoom_to_a_postage_stamp(sky) -> None:
    """A target aimed at the entry corner is 2% from an edge: centring would crop 4%."""
    from dataclasses import replace

    from ministar.pointing import best_start

    target = sky.camera.pointing
    entry_ra, entry_dec = best_start(
        target_ra_deg=target[0],
        target_dec_deg=target[1],
        optics=sky.optics,
        pa_y_deg=sky.camera.roll_deg,
    )
    model = replace(sky.model, ra_deg=entry_ra, dec_deg=entry_dec)
    stack = LiveStack(
        model,
        bin_factor=2,
        display_width=0,
        target_ra_deg=target[0],
        target_dec_deg=target[1],
    )
    add_frame(stack, sky)
    assert stack.stats().crop_rule == "usable"


def test_without_a_target_the_preview_shows_the_usable_rectangle(sky) -> None:
    stack = LiveStack(sky.model, bin_factor=2, display_width=0)
    add_frame(stack, sky)
    assert stack.stats().crop_rule == "usable"


def test_the_preview_keeps_the_sky_dark(sky) -> None:
    """The preview target is 0.06, not the 0.25 that reads as a grey wash in the dark."""
    stack = LiveStack(sky.model, bin_factor=2, display_width=0)
    for index in range(6):
        add_frame(stack, sky, seconds=20.0 if index else 0.0)
    preview = cv2.imdecode(np.frombuffer(stack.stats().jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
    background = float(np.median(preview))
    assert 5.0 < background < 40.0  # dark, but not crushed to black


def test_a_material_sky_change_restretches_the_display(sky) -> None:
    stack = LiveStack(sky.model, bin_factor=2, display_width=0, stretch_after=1)
    add_frame(stack, sky)
    frozen = stack._stf
    assert frozen is not None

    # Pretend the stretch was measured under a much darker sky: the current sky is now
    # more than 1.5x that baseline, which is the one thing the freeze cannot survive.
    stack._stretch_sky = float(stack._stretch_sky) / 4.0
    add_frame(stack, sky, seconds=20.0)
    assert stack._stf is not frozen


def test_a_stable_sky_leaves_the_frozen_stretch_alone(sky) -> None:
    stack = LiveStack(sky.model, bin_factor=2, display_width=0, stretch_after=1)
    add_frame(stack, sky)
    frozen = stack._stf
    for _ in range(4):
        add_frame(stack, sky, seconds=20.0)
    assert stack._stf is frozen

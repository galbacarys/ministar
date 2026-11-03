"""Sensor-frame preparation: binning, sky subtraction, and the flat guard.

Brief 15 §2 and §7.3 put a hard boundary between sensor-frame work and reference-frame
work. These tests are about the sensor side of it, where the two failures that matter
are silent: a demosaic that offsets the colour planes, and a flat divide that turns a
dead pixel into an infinity.
"""

from __future__ import annotations

import numpy as np
import pytest

from ministar.stack.frames import (
    apply_flat,
    block_mean,
    normalise_bayer,
    prepare,
    sky_level,
    subtract_sky,
    superpixel,
    to_cube,
)

R, G, B = 10.0, 20.0, 40.0


def cfa(pattern: str = "RGGB", shape=(4, 4)) -> np.ndarray:
    """A CFA frame with known values: R at the R sites, G at both greens, B at blue."""
    image = np.zeros(shape, dtype=np.uint16)
    offsets = {
        "RGGB": {"r": (0, 0), "g": ((0, 1), (1, 0)), "b": (1, 1)},
        "BGGR": {"r": (1, 1), "g": ((0, 1), (1, 0)), "b": (0, 0)},
        "GRBG": {"r": (0, 1), "g": ((0, 0), (1, 1)), "b": (1, 0)},
        "GBRG": {"r": (1, 0), "g": ((0, 0), (1, 1)), "b": (0, 1)},
    }[pattern]
    image[offsets["r"][0] :: 2, offsets["r"][1] :: 2] = R
    for y, x in offsets["g"]:
        image[y::2, x::2] = G
    image[offsets["b"][0] :: 2, offsets["b"][1] :: 2] = B
    return image


def test_superpixel_turns_one_cfa_cell_into_one_rgb_pixel() -> None:
    cube = superpixel(cfa("RGGB"))
    assert cube.shape == (2, 2, 3)
    assert np.allclose(cube[..., 0], R)
    assert np.allclose(cube[..., 1], G)
    assert np.allclose(cube[..., 2], B)


@pytest.mark.parametrize("pattern", ["RGGB", "BGGR", "GRBG", "GBRG"])
def test_every_bayer_pattern_is_interpreted_as_named(pattern: str) -> None:
    cube = superpixel(cfa(pattern), pattern)
    assert np.allclose(cube[0, 0], (R, G, B))


def test_reading_an_rggb_layout_as_bggr_swaps_red_and_blue() -> None:
    """A wrong BAYERPAT must be visible as a colour swap, not as a plausible gradient."""
    cube = superpixel(cfa("RGGB"), "BGGR")
    assert cube[0, 0, 0] == pytest.approx(B)
    assert cube[0, 0, 2] == pytest.approx(R)


def test_a_mono_frame_is_a_plain_block_mean() -> None:
    image = np.arange(16, dtype=np.float32).reshape(4, 4)
    cube = to_cube(image, bin_factor=2, bayer=None)
    assert cube.shape == (2, 2, 1)
    assert cube[0, 0, 0] == pytest.approx(np.mean([0, 1, 4, 5]))


def test_bin_four_averages_whole_superpixels() -> None:
    big = cfa("RGGB", shape=(8, 8))
    cube = to_cube(big, bin_factor=4, bayer="RGGB")
    assert cube.shape == (2, 2, 3)
    assert np.allclose(cube, (R, G, B))


def test_a_three_plane_frame_is_block_averaged_not_demosaiced() -> None:
    image = np.ones((4, 4, 3), dtype=np.float32)
    image[..., 1] *= 2.0
    cube = to_cube(image, bin_factor=2)
    assert cube.shape == (2, 2, 3)
    assert np.allclose(cube[..., 0], 1.0)
    assert np.allclose(cube[..., 1], 2.0)


def test_odd_dimensions_are_trimmed_rather_than_crashing() -> None:
    image = np.ones((5, 5), dtype=np.float32)
    assert to_cube(image, bin_factor=2, bayer=None).shape == (2, 2, 1)


def test_sky_subtraction_removes_each_channels_own_level() -> None:
    cube = np.zeros((4, 4, 3), dtype=np.float32) + np.array([10.0, 20.0, 30.0])
    cube[1, 1] += 500.0
    assert np.allclose(sky_level(cube, stride=1), (10.0, 20.0, 30.0), atol=5.0)
    cube = subtract_sky(cube, stride=1)
    assert abs(float(np.median(cube[..., 0]))) < 1e-3
    assert abs(float(np.median(cube[..., 2]))) < 1e-3
    assert cube[1, 1, 0] > 400.0  # the star survives


def test_prepare_subtracts_the_sky_and_reports_it() -> None:
    image = np.full((4, 4), 500, dtype=np.uint16)
    image[2, 2] = 5000
    prepared = prepare(image, bin_factor=2, bayer=None)
    assert float(np.median(prepared.cube)) == pytest.approx(0.0, abs=2.0)
    assert prepared.sky[0] > 400.0


def test_the_flat_guard_never_divides_by_zero() -> None:
    cube = np.ones((2, 2, 3), dtype=np.float32)
    flat = np.ones((2, 2, 3), dtype=np.float32)
    flat[0, 0] = 0.0  # a dead pixel in the flat
    corrected = apply_flat(cube, flat)
    assert np.isfinite(corrected).all()
    # The floor is 0.2, so the worst gain is 5x -- a guard, not a correction.
    assert corrected[0, 0].max() == pytest.approx(5.0)


def test_a_flat_is_normalised_before_it_is_applied() -> None:
    cube = np.full((2, 2, 3), 2.0, dtype=np.float32)
    flat = np.full((2, 2, 3), 4.0, dtype=np.float32)  # mean 1.0 after normalisation
    assert np.allclose(apply_flat(cube, flat), 2.0)


def test_a_flat_of_the_wrong_shape_is_refused() -> None:
    with pytest.raises(ValueError):
        apply_flat(np.ones((2, 2, 3), np.float32), np.ones((3, 3, 3), np.float32))


def test_a_flat_with_no_signal_is_refused() -> None:
    with pytest.raises(ValueError):
        apply_flat(np.ones((2, 2, 3), np.float32), np.zeros((2, 2, 3), np.float32))


def test_bayer_names_are_normalised_or_rejected() -> None:
    assert normalise_bayer(None) is None
    assert normalise_bayer("") is None
    assert normalise_bayer("rggb") == "RGGB"
    assert normalise_bayer("MONO") is None
    with pytest.raises(ValueError):
        normalise_bayer("XYZV")


def test_block_mean_rejects_a_zero_factor() -> None:
    with pytest.raises(ValueError):
        block_mean(np.ones((4, 4)), 0)

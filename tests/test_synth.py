"""The synthetic star field, checked against the real focus metrics.

The fake image train is only useful if the *measurement* code responds to it the way it
responds to sky: HFD must grow as focus is lost, flux must be conserved so the curve is
made by blur rather than by dimming, and a heavily defocused frame must lose stars.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from ministar.focus.metrics import measure_hfd
from ministar.focus.synth import Star, StarField


def field(**kwargs: object) -> StarField:
    values: dict[str, object] = {
        # Both well inside the frame: a star near the edge loses flux when its patch is
        # truncated at large sigma, which would look like the model losing light.
        "stars": (Star(x=400.5, y=150.25, flux=4000.0), Star(x=200.5, y=250.75, flux=1500.0)),
        "shape": (300, 800),
        "read_noise": 0.0,
        "sky": 0.0,
    }
    values.update(kwargs)
    return StarField(**values)  # type: ignore[arg-type]


# -- the blur model ---------------------------------------------------------
def test_sigma_is_smallest_at_best_focus() -> None:
    scene = field(best_position=24)

    assert scene.sigma_px(24) == pytest.approx(scene.sigma_best_px)
    assert scene.sigma_px(324) > scene.sigma_px(24)


def test_sigma_is_symmetric_and_monotonic_away_from_focus() -> None:
    scene = field(best_position=100)

    near = [scene.sigma_px(100 + step) for step in (10, 50, 200)]
    far = [scene.sigma_px(100 - step) for step in (10, 50, 200)]

    assert near == pytest.approx(far), "blur must not care which way you missed focus"
    assert near == sorted(near), "and must grow with distance from focus"


def test_sigma_combines_in_quadrature_not_linearly() -> None:
    """The in-focus spot convolved with a defocus disc: sigma never drops below it."""
    scene = field(best_position=0, sigma_best_px=1.0, defocus_px_per_unit=0.1)

    # hypot(1.0, 0.1*30) = 3.16, not 1.0 + 3.0 = 4.0
    assert scene.sigma_px(30) == pytest.approx(math.hypot(1.0, 3.0))


# -- what the focus metric sees --------------------------------------------
def test_defocus_spreads_light_without_losing_it() -> None:
    """A fake that dimmed stars instead of spreading them would pass a broken HFD."""
    scene = field()

    for position in (24, 74, 174):
        image = scene.render(position, noise=False)
        assert image.sum() == pytest.approx(scene.total_flux, rel=0.01)
        peak = image.max()
        if position == 24:
            sharp_peak = peak
        else:
            assert peak < sharp_peak, "the peak must fall as the light spreads"


def test_hfd_grows_as_focus_is_lost() -> None:
    # Fluxes stay below the live-view ceiling: a star that clips at best focus is
    # rejected by the saturation gate exactly when we are most interested in it.
    scene = StarField.random(
        count=25,
        seed=7,
        shape=(400, 600),
        read_noise=0.0,
        best_position=24,
        flux_min=250.0,
        flux_max=600.0,
    )

    measured = []
    for position in (24, 44, 64, 84):
        hfd, stars = measure_hfd(scene.render_uint8(position, noise=False))
        assert stars > 0, f"no stars detected at position {position}"
        measured.append(hfd)

    assert measured == sorted(measured), f"HFD must grow with defocus, got {measured}"


def test_heavy_defocus_costs_stars_as_well_as_resolution() -> None:
    """Realistic: at some point a defocused star no longer clears the detector."""
    scene = StarField.random(count=40, seed=3, shape=(400, 600), read_noise=2.0)

    _, at_focus = measure_hfd(scene.render_uint8(24))
    _, defocused = measure_hfd(scene.render_uint8(24 + 400))

    assert at_focus > defocused


# -- determinism ------------------------------------------------------------
def test_the_same_position_and_frame_give_identical_pixels() -> None:
    scene = StarField.random(count=20, seed=5)

    first = scene.render(50, frame=3)
    second = scene.render(50, frame=3)

    assert np.array_equal(first, second)


def test_noise_varies_frame_to_frame_so_a_preview_is_alive() -> None:
    scene = StarField.random(count=20, seed=5)

    assert not np.array_equal(scene.render(50, frame=1), scene.render(50, frame=2))


def test_noise_can_be_switched_off_for_measurements() -> None:
    scene = StarField.random(count=20, seed=5)

    assert np.array_equal(scene.render(50, noise=False), scene.render(50, frame=9, noise=False))


# -- scene generation -------------------------------------------------------
def test_random_fields_respect_shape_flux_and_margin() -> None:
    scene = StarField.random(count=50, seed=11, shape=(300, 400), margin=20, flux_max=5000.0)

    assert len(scene.stars) == 50
    for star in scene.stars:
        assert 20 <= star.x <= 380
        assert 20 <= star.y <= 280
        assert 0 < star.flux <= 5000.0


def test_brightness_is_heavy_tailed_like_a_real_frame() -> None:
    scene = StarField.random(count=400, seed=2)

    fluxes = sorted(star.flux for star in scene.stars)

    assert fluxes[-1] > 20 * fluxes[len(fluxes) // 2], "a few stars should dominate"


def test_uint8_frames_clip_bright_stars_like_a_camera_does() -> None:
    scene = field(stars=(Star(x=100.0, y=100.0, flux=200_000.0),), shape=(200, 200), sky=30.0)

    frame = scene.render_uint8(24, noise=False)

    assert frame.dtype == np.uint8
    assert frame.max() == 255

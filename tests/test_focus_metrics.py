"""HFD measurement on synthetic star fields."""

from __future__ import annotations

import math

import numpy as np

from ministar.focus.metrics import measure_hfd

SIZE = 96


def star_field(
    sigmas: tuple[float, ...] = (1.5,),
    *,
    peak: float = 4000.0,
    background: float = 200.0,
    noise: float = 0.0,
    seed: int = 7,
) -> np.ndarray:
    """Place a grid of Gaussian stars, well clear of the borders."""
    rng = np.random.default_rng(seed)
    img = np.full((SIZE, SIZE), background, dtype=np.float32)
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    centres = [(24, 24), (24, 48), (24, 72), (48, 24), (48, 48), (48, 72), (72, 24), (72, 48)]
    for i, (cy, cx) in enumerate(centres):
        s = sigmas[i % len(sigmas)]
        img += peak * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * s * s)))
    if noise:
        img += rng.normal(0.0, noise, img.shape)
    return np.clip(img, 0, 65535).astype(np.uint16)


def test_hfd_tracks_star_size() -> None:
    """The only property focus actually needs: bigger blur -> bigger HFD."""
    small, _ = measure_hfd(star_field((0.8,)))
    medium, _ = measure_hfd(star_field((1.5,)))
    large, _ = measure_hfd(star_field((2.5,)))
    assert small < medium < large


def test_hfd_matches_gaussian_theory() -> None:
    # For a 2D Gaussian of sigma s, HFD = 2 * E[r] = s * sqrt(2*pi) ~= 2.5066 s.
    for sigma in (1.0, 1.5):
        hfd, count = measure_hfd(star_field((sigma,)))
        assert count >= 5
        assert hfd == pytest_approx(sigma * math.sqrt(2 * math.pi), rel=0.25)


def test_noise_tolerated() -> None:
    clean, _ = measure_hfd(star_field((1.5,), noise=0.0))
    noisy, count = measure_hfd(star_field((1.5,), noise=60.0))
    assert count >= 5
    assert abs(noisy - clean) / clean < 0.20


def test_blank_frame_reports_nothing() -> None:
    blank = np.full((SIZE, SIZE), 200, dtype=np.uint16)
    hfd, count = measure_hfd(blank)
    assert math.isnan(hfd)
    assert count == 0


def test_saturated_stars_are_excluded() -> None:
    """A clipped star has an artificially low HFD and must not pollute the median."""
    saturated = star_field((1.5,), peak=200_000.0)  # clips to 65535
    hfd, count = measure_hfd(saturated)
    assert count == 0
    assert math.isnan(hfd)


def test_too_few_stars_is_not_trusted() -> None:
    img = np.full((SIZE, SIZE), 200, dtype=np.uint16)
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    img = img + (4000 * np.exp(-(((yy - 48) ** 2 + (xx - 48) ** 2) / 18.0))).astype(np.uint16)
    hfd, count = measure_hfd(img)
    assert count < 5
    assert math.isnan(hfd)


def test_colour_frame_is_accepted() -> None:
    mono = star_field((1.5,))
    rgb = np.stack([mono, mono, mono], axis=2)
    hfd_mono, _ = measure_hfd(mono)
    hfd_rgb, _ = measure_hfd(rgb)
    assert hfd_rgb == pytest_approx(hfd_mono, rel=1e-6)


# Local shim so the file does not need pytest just for approx.
def pytest_approx(value: float, rel: float = 1e-6):
    class _Approx:
        def __eq__(self, other: object) -> bool:
            return isinstance(other, (int, float)) and abs(other - value) <= abs(value) * rel

    return _Approx()

"""The coarse focus metric used while the user hunts for focus by eye."""

from __future__ import annotations

import numpy as np

from ministar.focus.sharpness import frame_sharpness


def star_field(size: int = 128, peak: float = 4000.0, background: float = 200.0) -> np.ndarray:
    img = np.full((size, size), background, dtype=np.float32)
    yy, xx = np.mgrid[0:size, 0:size]
    for cy in range(24, size, 32):
        for cx in range(24, size, 32):
            img += peak * np.exp(-(((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * 1.5**2)))
    return img


def blur(image: np.ndarray, passes: int) -> np.ndarray:
    """Cheap wide blur: repeated 4-neighbour averaging."""
    out = image.astype(np.float32)
    for _ in range(passes):
        out = (
            out
            + np.roll(out, 1, 0)
            + np.roll(out, -1, 0)
            + np.roll(out, 1, 1)
            + np.roll(out, -1, 1)
        ) / 5.0
    return out


def test_sharpness_decreases_with_blur() -> None:
    sharp = star_field()
    values = [frame_sharpness(blur(sharp, n)) for n in (0, 2, 6, 12, 24)]
    assert values == sorted(values, reverse=True), values


def test_sharpness_is_scale_invariant() -> None:
    """Exposure and sky brightness change; the number must not."""
    base = star_field()
    brighter = base * 3.0
    np.testing.assert_allclose(frame_sharpness(brighter), frame_sharpness(base), rtol=0.05)


def test_flat_frame_has_zero_sharpness() -> None:
    assert frame_sharpness(np.full((32, 32), 500.0)) == 0.0


def test_tiny_frame_is_safe() -> None:
    assert frame_sharpness(np.zeros((2, 2))) == 0.0


def test_works_on_colour_frames() -> None:
    mono = star_field()
    rgb = np.stack([mono, mono, mono], axis=2)
    np.testing.assert_allclose(frame_sharpness(rgb), frame_sharpness(mono), rtol=1e-6)

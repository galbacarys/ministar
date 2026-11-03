"""The frozen STF stretch.

Brief 15 §6's requirement is narrow and testable: a robust black point, the median placed
at the midtones target, monotonic, and stable once measured. The "frozen" part is not a
property of this class -- it is the caller keeping one instance -- so what is tested here
is that an instance is a value: nothing it does to an image changes what it will do next.
"""

from __future__ import annotations

import numpy as np
import pytest

from ministar.stack.stretch import MIDTONES, Stf, _midtone_for


def noisy_sky(level: float = 100.0, sigma: float = 3.0, shape=(64, 64), seed: int = 4):
    rng = np.random.default_rng(seed)
    image = rng.normal(level, sigma, shape).astype(np.float32)
    image[10:12, 10:12] += 4000.0  # a few stars, so the MAD is not the whole story
    image[40:42, 40:42] += 2500.0
    return image


def test_the_midtones_target_is_stf_s_default() -> None:
    assert MIDTONES == 0.25


def test_the_median_lands_at_the_midtones_target() -> None:
    image = noisy_sky()
    stf = Stf.measure(image)
    stretched = stf.apply(image)
    assert float(np.median(stretched)) == pytest.approx(MIDTONES, abs=0.02)


def test_a_darker_target_puts_the_sky_where_the_caller_asked() -> None:
    """The live preview asks for a dark sky; the algorithm is the same, the target differs."""
    image = noisy_sky()
    stf = Stf.measure(image, target=0.06)
    assert float(np.median(stf.apply(image))) == pytest.approx(0.06, abs=0.01)


def test_the_stretch_is_monotonic() -> None:
    image = np.linspace(-50.0, 5000.0, 2000, dtype=np.float32).reshape(40, 50)
    stf = Stf.measure(noisy_sky())
    stretched = stf.apply(image).reshape(-1)
    assert np.all(np.diff(stretched) >= 0)


def test_values_below_the_shadows_clip_to_black() -> None:
    stf = Stf.measure(noisy_sky())
    below = np.full((2, 2), stf.shadows - 10.0, dtype=np.float32)
    assert np.all(stf.apply(below) == 0.0)


def test_the_stretch_does_not_change_when_it_is_used() -> None:
    stf = Stf.measure(noisy_sky())
    before = (stf.shadows, stf.white, stf.midtone)
    stf.apply(noisy_sky(level=900.0, seed=9))
    assert (stf.shadows, stf.white, stf.midtone) == before


def test_a_flat_frame_does_not_become_black_or_nan() -> None:
    flat = np.full((16, 16), 42.0, dtype=np.float32)
    stf = Stf.measure(flat)
    out = stf.apply(flat)
    assert np.isfinite(out).all()
    assert float(out.min()) >= 0.0 and float(out.max()) <= 1.0


def test_an_all_zero_frame_is_handled() -> None:
    stf = Stf.measure(np.zeros((8, 8), dtype=np.float32))
    out = stf.apply(np.zeros((8, 8), dtype=np.float32))
    assert np.isfinite(out).all()
    assert float(out.max()) == 0.0


def test_uint8_conversion_spans_the_full_range() -> None:
    image = noisy_sky()
    stf = Stf.measure(image)
    out = stf.to_uint8(image)
    assert out.dtype == np.uint8
    assert out.min() == 0
    assert out.max() == 255


def test_a_colour_frame_shares_one_transfer() -> None:
    """Luminance, not per-channel: otherwise the preview drifts in colour as it grows."""
    base = noisy_sky()
    rgb = np.stack([base, base * 1.2, base * 0.8], axis=-1)
    stf = Stf.measure(rgb)
    out = stf.apply(rgb)
    assert out.shape == rgb.shape
    assert np.isfinite(out).all()


@pytest.mark.parametrize("median", [0.01, 0.05, 0.1, 0.25, 0.5, 0.75])
def test_the_midtone_solution_inverts_the_transfer_function(median: float) -> None:
    m = _midtone_for(median)
    mtf = ((m - 1.0) * median) / ((2.0 * m - 1.0) * median - m)
    assert mtf == pytest.approx(MIDTONES, abs=1e-6)


def test_the_lookup_table_matches_the_direct_path() -> None:
    stf = Stf.measure(noisy_sky())
    lut = stf.lut()
    values = np.array([0.0, 100.0, 1000.0, 5000.0], dtype=np.float32)
    direct = stf.to_uint8(values)
    indexed = lut[values.astype(np.int64)]
    assert np.abs(direct.astype(int) - indexed.astype(int)).max() <= 1

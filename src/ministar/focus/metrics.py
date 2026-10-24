"""Star sharpness measurement: HFD.

HFD (half-flux diameter) is the flux-weighted mean radius of a star times two.
Chosen over FWHM because it survives saturation, works on the ~1.75 px PSF this
rig produces at every focal length (11 §3), and is cheap.

Implementation notes that matter:

* **Local maxima via array shifts**, not connected-component labelling. Stars are
  a couple of pixels across, so a 3x3 local maximum plus a small cutout is both
  sufficient and dependency-free (numpy only).
* **Background and noise are LOCAL, per star.** A global MAD is not usable on real
  frames: on the user's 100 mm North America light it is inflated by the nebula
  filling the field, pushing a 5-sigma threshold above the frame's maximum and
  finding zero stars on a frame that plainly has them. Each candidate therefore
  gets an annulus of its own to measure against.
* **Single-pixel spikes are rejected.** A bias master has no stars, yet a plain
  SNR gate happily reported 23 of them: hot pixels are very significant and only
  one pixel wide. Requiring several pixels above the local threshold kills them.
* **Integration is confined to pixels above the local threshold.** Noise in the
  cutout wings otherwise adds weight at large radius and inflates HFD badly (7.0 px
  measured against a true 3.7 px). This truncates the star's faint wings, so HFD is
  slightly low in absolute terms — acceptable, because focus only compares samples
  taken at a similar noise level.
* **Saturation is excluded, not clipped.** A saturated star has an artificially
  low HFD and drags the median the wrong way.
* **Border peaks are dropped.** A cutout that runs off the frame cannot have its
  flux integrated.

⚠️ **Known limitation: a starless frame can still yield "stars".** On the user's
M81 master bias — which contains no stars at all — this reports 15 at HFD 6.5 px.
Fixed-pattern noise in a very clean frame is locally significant and can span a
couple of pixels, so neither the SNR gate nor the ``MIN_SIGNIFICANT_PIXELS`` gate
rejects it. A real star field yields tens of *isolated* maxima; structured noise
yields a few clusters. Callers that need certainty (the focus monitor on a cloudy
night) should treat a low accepted/candidate ratio as suspicious rather than
relying on the count alone. Not worth more machinery for v1 — you do not focus on
a master bias.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: Radius of the cutout used to integrate each star.
#:
#: This is a real trade-off, not a detail. A small window rejects any star whose
#: HFD exceeds it (the wings get clipped and the measurement is a lie) -- which is
#: *exactly* the data the focus sweep needs, since stars are biggest far from
#: focus. With a radius of 5 the synthetic sigma=2.5 star was discarded outright.
#: Large enough to bracket the sweep's shoulders, small enough that neighbours
#: rarely intrude.
_WINDOW = 12
_PATCH = 2 * _WINDOW + 1

#: A real star spans more than one pixel. Hot pixels and cosmic rays do not.
MIN_SIGNIFICANT_PIXELS = 2


def to_gray(image: np.ndarray) -> np.ndarray:
    """Collapse a colour frame to float32 luminance."""
    arr = np.asarray(image)
    if arr.ndim == 3:
        return arr.astype(np.float32).mean(axis=2)
    return arr.astype(np.float32)


def saturation_level(image: np.ndarray) -> float | None:
    """Full-scale value for an integer image, else ``None``."""
    dtype = np.asarray(image).dtype
    if np.issubdtype(dtype, np.integer):
        return float(np.iinfo(dtype).max)
    return None


def background_and_noise(gray: np.ndarray) -> tuple[float, float]:
    """Coarse global background and noise, used only to *seed* peak candidates.

    Median and MAD rather than mean and stddev, because stars are outliers. This is
    deliberately permissive: the real decision is made per star in :func:`hfd_at`.
    """
    median = float(np.median(gray))
    mad = float(np.median(np.abs(gray - median)))
    noise = 1.4826 * mad
    if noise <= 0:
        noise = max(float(gray.std()), 1e-6)
    return median, noise


def find_peaks(
    gray: np.ndarray,
    background: float,
    noise: float,
    threshold_sigma: float = 3.0,
) -> np.ndarray:
    """3x3 local maxima above ``background + threshold_sigma * noise``.

    Returns peak coordinates, brightest first.
    """
    threshold = background + threshold_sigma * noise
    is_peak = gray > threshold
    h, w = gray.shape

    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            shifted = np.full_like(gray, -np.inf)
            ys = slice(max(0, dy), h + min(0, dy))
            yd = slice(max(0, -dy), h + min(0, -dy))
            xs = slice(max(0, dx), w + min(0, dx))
            xd = slice(max(0, -dx), w + min(0, -dx))
            shifted[yd, xd] = gray[ys, xs]
            is_peak &= gray >= shifted

    is_peak[:_WINDOW, :] = False
    is_peak[-_WINDOW:, :] = False
    is_peak[:, :_WINDOW] = False
    is_peak[:, -_WINDOW:] = False

    ys, xs = np.nonzero(is_peak)
    if len(ys) == 0:
        return np.empty((0, 2), dtype=int)
    order = np.argsort(gray[ys, xs])[::-1]
    return np.column_stack((ys[order], xs[order]))


def _annulus(patch: np.ndarray) -> np.ndarray:
    """Perimeter of the cutout: the local sky, uncontaminated by the star core."""
    ring = np.concatenate([patch[0, :], patch[-1, :], patch[1:-1, 0], patch[1:-1, -1]])
    return ring


@dataclass(frozen=True)
class StarMeasurement:
    hfd: float
    peak_snr: float
    significant_pixels: int


def measure_star(
    gray: np.ndarray, y: int, x: int, *, min_snr: float = 5.0
) -> StarMeasurement | None:
    """Local HFD for the star at ``(y, x)``, or ``None`` if it is not a star."""
    patch = gray[y - _WINDOW : y + _WINDOW + 1, x - _WINDOW : x + _WINDOW + 1]
    if patch.shape != (_PATCH, _PATCH):
        return None

    ring = _annulus(patch)
    local_bg = float(np.median(ring))
    local_noise = 1.4826 * float(np.median(np.abs(ring - local_bg)))
    if local_noise <= 0:
        local_noise = max(float(ring.std()), 1e-6)

    threshold = min_snr * local_noise
    peak_snr = (float(patch[_WINDOW, _WINDOW]) - local_bg) / local_noise
    if peak_snr < min_snr:
        return None

    # Integrate only over pixels that clear the local threshold. Both alternatives
    # fail: clipping at zero lets noise excursions in the wings add weight at large
    # radius (7.02 px measured against a true 3.7 px), and subtracting a 1-sigma
    # floor only halves the error (4.62 px) because the positive tail of the noise
    # still votes. Restricting to significant pixels removes it at the source -- the
    # wings of a real star are indistinguishable from noise out there, so they must
    # not be counted at all.
    core = patch > (local_bg + threshold)
    significant = int(core.sum())
    if significant < MIN_SIGNIFICANT_PIXELS:
        return None  # hot pixel / cosmic ray, not a star

    weights = np.where(core, patch - local_bg, 0.0)

    flux = float(weights.sum())
    if flux <= 0:
        return None

    yy, xx = np.mgrid[0:_PATCH, 0:_PATCH]
    cy = float((weights * yy).sum() / flux)
    cx = float((weights * xx).sum() / flux)
    radius = np.hypot(yy - cy, xx - cx)
    hfd = 2.0 * float((weights * radius).sum() / flux)

    # Pinned to the cutout edge means the wings were clipped, so the HFD is a lie.
    if hfd <= 0.0 or hfd > _WINDOW:
        return None
    return StarMeasurement(hfd=hfd, peak_snr=peak_snr, significant_pixels=significant)


def measure_hfd(
    image: np.ndarray,
    *,
    max_stars: int = 40,
    min_stars: int = 5,
    threshold_sigma: float = 3.0,
    min_star_snr: float = 5.0,
) -> tuple[float, int]:
    """Median HFD and accepted star count for a frame.

    ``hfd`` is ``nan`` when fewer than ``min_stars`` usable stars were found, which
    is the caller's signal that this frame cannot inform focus (11 §4).
    """
    gray = to_gray(image)
    background, noise = background_and_noise(gray)

    peaks = find_peaks(gray, background, noise, threshold_sigma=threshold_sigma)
    if len(peaks) == 0:
        return float("nan"), 0

    sat = saturation_level(image)
    saturated = gray >= (sat * 0.98) if sat is not None else np.zeros_like(gray, bool)

    values: list[float] = []
    for y, x in peaks[: max_stars * 4]:
        if saturated[y, x]:
            continue
        star = measure_star(gray, int(y), int(x), min_snr=min_star_snr)
        if star is not None:
            values.append(star.hfd)
        if len(values) >= max_stars:
            break

    if len(values) < min_stars:
        return float("nan"), len(values)

    return float(np.median(values)), len(values)

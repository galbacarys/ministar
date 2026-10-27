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

⚠️ **Starless frames: mostly fixed, a small residue remains.** An earlier version
reported 15 "stars" at HFD 6.5 px on a starless M81 master bias, and 40 on a
lens-capped live view — 40 being the ``max_stars`` cap, so the true figure was
"at least 40".

The dominant cause was **the noise estimator, not the gates**. On a near-black
frame roughly half the pixels sit at the dtype floor, so the MAD is exactly zero;
the old fallback to ``std`` then underestimated sigma by ~1.7x on a one-sided
clipped distribution (1.66 against a true 3.0). That dropped the candidate
threshold from 9.0 to 5.0 and admitted ~40 000 pixels — 40 562 measured on a
704x1056 frame.

``p84 - p50`` (:func:`ministar.focus.noise.robust_sigma`) fixes that, because it
needs only the upper tail. What can still happen is residual *structured* noise —
chiefly JPEG block artefacts in a live view — producing a few locally-significant
multi-pixel blobs. So ``max_stars`` remains a cap and a saturated count means "at
least this many". When a sweep needs certainty, prefer a real capture over a live
view (11 §4.2), and treat ``count == max_stars`` as untrustworthy.

Two traps found by measuring, both of which produced *confident* wrong answers:

1. **The seed threshold must be 5 sigma, not 3.** A k-sigma gate admits a fixed
   *fraction* of pixels, so the absolute number of candidates scales with frame
   area, not with star content. On a 704x1056 live view a 3-sigma seed admitted
   ~1300 pixels and produced 27 acceptances on a frame containing exactly 14
   stars. At 5 sigma the same frame yields exactly 14, and every existing test
   still passes because real stars are tens of sigma.
2. **Pass the integer image, not a float copy.** ``saturation_level`` returns
   ``None`` for float dtypes, which silently disables saturated-star rejection.
   Measured on the same frame: the uint8 original reports 0 stars (all clipped,
   correctly excluded) while a float copy reports 40 at HFD 8.05 -- a fabricated
   result, with no error. Converting to float before measuring is a silent loss of
   a safety check, so do not do it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ministar.focus.noise import robust_sigma

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
    """Full-scale value for an integer image, else ``None``.

    ⚠️ Returns ``None`` for float input, which **disables saturated-star
    rejection**. Callers wanting that check must pass the integer image, not a
    ``to_gray`` conversion (see the module docstring).
    """
    dtype = np.asarray(image).dtype
    if np.issubdtype(dtype, np.integer):
        return float(np.iinfo(dtype).max)
    return None


def tenengrad(gray: np.ndarray) -> float:
    """Mean squared gradient magnitude: a *contrast* measure that needs no stars.

    This exists because HFD cannot work without a star field, and there are real
    situations with none: on a bench, through a lens cap, or on a bright moonlit
    night.

    ⚠️ **It is not a focus metric, and it must not be used as one.** Tenengrad
    peaks only while the PSF spans several pixels; below that it *falls* as focus
    improves, because the gradient energy gets concentrated into fewer and fewer
    samples. Measured on synthetic stars of equal peak flux:

    | PSF sigma | 0.8 px | 1.5 px | 2.5 px | 3.0 px | 5.0 px |
    |---|---|---|---|---|---|
    | tenengrad | 22 238 | 35 221 | 40 322 | **41 292** | 41 047 |

    This rig is diffraction-limited at ~1.75 px HFD, i.e. sigma ~0.7 px (03 §3),
    which sits on the *rising* side of that curve. So near best focus this number
    gets **worse as focus improves**. Use HFD for focus (11 §3); use this only to
    answer "is there any contrast in this frame at all".

    Measured contrast between a capped and an open lens was 0.27 vs 20.0 in
    arbitrary units (brief 16 §8) -- two orders of magnitude -- so it is a good
    frame-sanity check even though it is a bad focus metric.
    """
    gy, gx = np.gradient(gray)
    return float(np.mean(gx * gx + gy * gy))


def background_and_noise(gray: np.ndarray) -> tuple[float, float]:
    """Coarse global background and noise, used only to *seed* peak candidates.

    Median for the background because stars are outliers, and ``p84 - p50`` for the
    noise because a black frame has its lower tail clipped away (see
    :func:`ministar.focus.noise.robust_sigma`). Deliberately permissive: the real
    decision is made per star in :func:`measure_star`.
    """
    background = float(np.median(gray))
    return background, robust_sigma(gray)


def find_peaks(
    gray: np.ndarray,
    background: float,
    noise: float,
    threshold_sigma: float = 5.0,
) -> np.ndarray:
    """3x3 local maxima above ``background + threshold_sigma * noise``.

    Returns peak coordinates, brightest first.

    ⚠️ **5 sigma, not 3.** A k-sigma gate admits a roughly fixed fraction of
    pixels, so on a large frame the candidate count scales with area rather than
    with star content: 3 sigma on a 704x1056 frame gave ~1300 candidates and 27
    acceptances for a 14-star field. 5 sigma gave exactly 14.
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
    # Same clipping-robust estimator as the global seed: an annulus around a star
    # in a dark sky is just as prone to a clipped floor.
    local_noise = robust_sigma(ring)

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
    threshold_sigma: float = 5.0,
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

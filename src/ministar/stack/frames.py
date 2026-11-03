"""One sensor frame, prepared for the accumulator.

Everything here runs in the **sensor frame** (brief 15 §2): black level, binning and the
flat divide all happen before any warp, because vignetting and dust are bolted to the
sensor. Warping first would rotate the flat into the sky and then divide by it — a bug
that looks like a gradient and cannot be undone later.

The ordering is the brief's, and only the steps we have data for are taken:

    decode (elsewhere) -> super-pixel bin -> flat divide -> sky subtract

Black-level subtraction is deliberately absent: we have no measured black level for
either camera path, and inventing one (the frame minimum, say) would subtract the sky
level twice. It belongs here when a body reports one.

**Super-pixel binning does the bin and the demosaic in one reshape.** It also removes the
half-pixel colour offsets that a `cvtColor` demosaic would rotate against each other,
frame to frame. For a mono frame (the fake camera's FITS) there is nothing to demosaic
and the same operation is a plain 2x2 block average.

The bin factor is the *final* reduction of the sensor grid: 2 is the 4.5 MP super-pixel
frame, 4 is the 1.1 MP half-size frame of brief 15 §2.1. A factor above 2 averages whole
super-pixels, which keeps the CFA arithmetic exact.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: Offsets of (R, G1, G2, B) inside the 2x2 CFA cell, per pattern. Bayer patterns are
#: named for the top-left 2x2 in reading order, so RGGB is the identity layout.
_BAYER_OFFSETS: dict[str, tuple[tuple[int, int], ...]] = {
    "RGGB": ((0, 0), (0, 1), (1, 0), (1, 1)),
    "BGGR": ((1, 1), (0, 1), (1, 0), (0, 0)),
    "GRBG": ((0, 1), (0, 0), (1, 1), (1, 0)),
    "GBRG": ((1, 0), (0, 0), (1, 1), (0, 1)),
}

#: Used when a converted RAW does not name its pattern. RGGB is what the 60D writes and
#: what dcraw reports for it (16 §11); a wrong guess shows up as a colour cast, not as an
#: alignment failure, so it is safe to default.
DEFAULT_BAYER = "RGGB"


def normalise_bayer(pattern: str | None) -> str | None:
    """A FITS ``BAYERPAT`` card to one of the four patterns, or ``None`` for mono."""
    if not pattern:
        return None
    cleaned = str(pattern).strip().upper()
    if not cleaned or cleaned in {"NONE", "MONO"}:
        return None
    if cleaned not in _BAYER_OFFSETS:
        raise ValueError(f"unknown Bayer pattern {pattern!r}")
    return cleaned


def _even(image: np.ndarray, factor: int) -> np.ndarray:
    """Trim the last row/column so ``factor`` divides both dimensions."""
    height, width = image.shape[:2]
    h = height - height % factor
    w = width - width % factor
    return image[:h, :w]


def superpixel(image: np.ndarray, pattern: str = DEFAULT_BAYER) -> np.ndarray:
    """2x2 CFA cell to one RGB pixel, float32 ``(h/2, w/2, 3)``.

    Channels are R, G, B with the two greens averaged: they are two measurements of the
    same colour, and averaging them halves their noise for free.
    """
    if image.ndim != 2:
        raise ValueError("super-pixel binning expects a single-plane CFA frame")
    offsets = _BAYER_OFFSETS[normalise_bayer(pattern) or DEFAULT_BAYER]
    trimmed = _even(image.astype(np.float32, copy=False), 2)
    (ry, rx), (g1y, g1x), (g2y, g2x), (by, bx) = offsets
    red = trimmed[ry::2, rx::2]
    green = 0.5 * (trimmed[g1y::2, g1x::2] + trimmed[g2y::2, g2x::2])
    blue = trimmed[by::2, bx::2]
    return np.stack([red, green, blue], axis=-1)


def block_mean(image: np.ndarray, factor: int) -> np.ndarray:
    """Average ``factor x factor`` blocks. Works on 2-D and 3-D arrays."""
    if factor < 1:
        raise ValueError("block factor must be at least 1")
    if factor == 1:
        return image.astype(np.float32, copy=False)
    trimmed = _even(image, factor)
    height, width = trimmed.shape[:2]
    if trimmed.ndim == 2:
        return trimmed.reshape(height // factor, factor, width // factor, factor).mean(axis=(1, 3))
    channels = trimmed.shape[2]
    blocks = trimmed.reshape(height // factor, factor, width // factor, factor, channels).mean(
        axis=(1, 3)
    )
    return blocks.astype(np.float32, copy=False)


def to_cube(image: np.ndarray, *, bin_factor: int = 2, bayer: str | None = None) -> np.ndarray:
    """Any frame the two cameras produce, as float32 ``(h, w, 3)`` at the stack resolution.

    ``bin_factor`` counts sensor pixels per stacked pixel: 2 is the full super-pixel
    frame, 4 the half-size one. A 3-plane frame (already demosaiced) is block-averaged
    instead, so the function is total rather than assuming a CFA.
    """
    if bin_factor < 1:
        raise ValueError("bin factor must be at least 1")
    source = np.asarray(image)
    pattern = normalise_bayer(bayer)
    if source.ndim == 3:
        if source.shape[2] < 3:
            raise ValueError("a 3-plane frame needs at least three channels")
        cube = source[..., :3].astype(np.float32, copy=False)
        return block_mean(cube, bin_factor)
    if source.ndim != 2:
        raise ValueError(f"expected a 2-D or 3-D frame, got shape {source.shape}")

    if pattern is None:
        return block_mean(source.astype(np.float32, copy=False), bin_factor)[..., None]

    cube = superpixel(source, pattern)
    # A super-pixel is 2 sensor pixels; anything beyond that is a plain reduction.
    return block_mean(cube, max(1, bin_factor // 2))


def sky_level(cube: np.ndarray, *, stride: int = 16) -> np.ndarray:
    """Per-channel median of a subsample — the sky background, in ``(3,)``.

    Subsampled because the median is a single scalar per channel and a true median of
    4.5 MP costs ~200 ms for no benefit (brief 15 §2.2). Float frames that carry NaNs
    are tolerated: ``nanmedian`` is the honest answer, and the caller guards holes later.
    """
    sample = cube[::stride, ::stride]
    if sample.size == 0:
        return np.zeros(cube.shape[-1] if cube.ndim == 3 else 1, dtype=np.float32)
    flat = sample.reshape(-1, sample.shape[-1]) if sample.ndim == 3 else sample.reshape(-1, 1)
    return np.nanmedian(flat, axis=0).astype(np.float32)


def subtract_sky(cube: np.ndarray, *, stride: int = 16) -> np.ndarray:
    """Remove each channel's own sky level, in place on a copy.

    Per channel rather than one number for all three: the CFA's three planes do not share
    a zero (no white balance is baked in, 05 §"raw"), so a single scalar would leave a
    colour cast that the STF then stretches.
    """
    level = sky_level(cube, stride=stride)
    return (cube - level.reshape(1, 1, -1)).astype(np.float32, copy=False)


def apply_flat(cube: np.ndarray, flat: np.ndarray, *, floor: float = 0.2) -> np.ndarray:
    """Divide by a normalised flat, with the brief's guard (15 §7.4).

    A dead pixel is near zero in a flat; unguarded division turns it into an infinity
    (or a NaN), and one NaN is enough to poison a whole accumulator column. The floor is
    a *guard*, not a correction: it caps the gain at 5x, which is where the flat stops
    carrying information anyway.
    """
    if cube.shape != flat.shape:
        raise ValueError(f"flat shape {flat.shape} does not match frame {cube.shape}")
    normalised = flat.astype(np.float32, copy=False)
    median = float(np.median(normalised))
    if not np.isfinite(median) or median <= 0:
        raise ValueError("flat has no usable median")
    normalised = normalised / median
    return cube / np.maximum(normalised, floor)


@dataclass(frozen=True)
class Prepared:
    """A frame ready to warp: the cube, and what was done to it.

    Carrying the sky level rather than only the subtracted frame lets the gate compare a
    frame's background against the running one — the cloud test of brief 15 §5 — without
    re-measuring the original.
    """

    cube: np.ndarray
    sky: np.ndarray


def prepare(
    image: np.ndarray,
    *,
    bin_factor: int = 2,
    bayer: str | None = None,
    flat: np.ndarray | None = None,
    subtract_background: bool = True,
) -> Prepared:
    """The sensor-frame half of the pipeline, in the one order that is correct."""
    cube = to_cube(image, bin_factor=bin_factor, bayer=bayer)
    if flat is not None:
        cube = apply_flat(cube, flat)
    sky = sky_level(cube)
    if subtract_background:
        cube = (cube - sky.reshape(1, 1, -1)).astype(np.float32, copy=False)
    return Prepared(cube=cube, sky=sky)


__all__ = [
    "DEFAULT_BAYER",
    "Prepared",
    "apply_flat",
    "block_mean",
    "normalise_bayer",
    "prepare",
    "sky_level",
    "subtract_sky",
    "superpixel",
    "to_cube",
]

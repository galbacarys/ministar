"""Sum, count, M2 — and the three things that must not be simplified away.

Brief 15 §3 is the specification; this is it in numpy. The rules that are easy to get
wrong, stated once here so the code can be read against them:

* **Never divide by N.** Under rotation and translation the edges get fewer frames than
  the centre, so ``sum / frames`` paints a growing dark border into the image. Every
  readout divides by the *per-pixel* count.
* **The sigma gate rejects without incrementing the weight.** The mean stays the mean of
  accepted values, so a persistent hot pixel never pulls the mean toward itself — which
  is also why the count map *is* the defect map (brief 15 §8.2).
* **Rejection is per pixel, not per frame.** A satellite crosses a part of the frame;
  the rest of that frame is still good.

Memory: at 4.5 MP this is ~54 MB for sum + M2 (float32, three channels) and ~36 MB for
count + seen. That is why there is no ring buffer: Welford needs O(1) frames.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: Welford's gate, in sigmas. Four is brief 15 §3.2's number: at 4sigma a Gaussian rejects
#: ~6e-5 of good samples per pixel, which at 4.5 MP and a few hundred frames is a
#: handful of pixels — the ones worth rejecting.
DEFAULT_SIGMA = 4.0

#: The box used to fill holes. Small on purpose: the fill is for isolated dead pixels,
#: and a large box would smear a genuinely uncovered edge inward.
FILL_KERNEL = 5


@dataclass(frozen=True)
class Coverage:
    """How much of the canvas the run has covered, in the two senses that differ.

    ``fraction`` is the union — pixels that any frame reached. ``fully`` is the
    intersection — pixels *every* accepted frame reached, which is what shrinks as the
    field rotates (brief 15 §3.4). The gap between them is the border the drift has
    eaten, and showing only the union would hide exactly the cost the HUD exists to make
    visible.

    Both are measured on ``seen`` (frames that *touched* the pixel), not on ``count``
    (frames that were accepted there). The sigma gate rejects a scattered pixel here and
    there, so a count-based intersection would be a lace curtain with no solid rectangle
    in it — and coverage is a geometric question, while the count map is the photometric
    one (15 §3.2, §8.2).
    """

    fraction: float
    fully: float
    #: ``(x, y, width, height)`` of the largest fully covered rectangle, or ``None``.
    rect: tuple[int, int, int, int] | None


def largest_rectangle(mask: np.ndarray, *, stride: int = 16) -> tuple[int, int, int, int] | None:
    """Largest all-true rectangle in ``mask``, as ``(x, y, w, h)``, coarse by ``stride``.

    The exact problem is the classic maximal rectangle in a binary matrix, which is
    O(pixels) in Python and therefore far too slow to run every frame at 4.5 MP. The
    rectangle is a HUD element and guidance, not photometry, so the mask is min-pooled
    to a coarse grid first (all-true blocks only) and the answer is scaled back up. That
    makes it a *conservative* estimate: it can under-report the valid area, never
    over-report it.
    """
    if mask.ndim != 2 or mask.size == 0:
        return None
    height, width = mask.shape
    sh = max(1, min(stride, height))
    sw = max(1, min(stride, width))
    rows = height // sh
    cols = width // sw
    if rows == 0 or cols == 0:
        return None
    blocks = mask[: rows * sh, : cols * sw].reshape(rows, sh, cols, sw).all(axis=(1, 3))
    if not blocks.any():
        return None

    heights = np.zeros(cols, dtype=np.int32)
    best_area = 0
    best: tuple[int, int, int, int] | None = None
    for row in range(rows):
        heights = np.where(blocks[row], heights + 1, 0)
        stack: list[tuple[int, int]] = []
        for col in range(cols + 1):
            current = int(heights[col]) if col < cols else 0
            start = col
            while stack and stack[-1][1] > current:
                left, bar = stack.pop()
                area = bar * (col - left)
                if area > best_area:
                    best_area = area
                    best = (left * sw, (row - bar + 1) * sh, (col - left) * sw, bar * sh)
                start = left
            stack.append((start, current))
    return best


class Accumulator:
    """Running per-pixel mean of accepted values, with variance for the gate."""

    def __init__(
        self,
        height: int,
        width: int,
        channels: int = 3,
        *,
        sigma: float = DEFAULT_SIGMA,
    ) -> None:
        if height <= 0 or width <= 0 or channels <= 0:
            raise ValueError("accumulator needs a positive shape")
        self.height = int(height)
        self.width = int(width)
        self.channels = int(channels)
        self.sigma = float(sigma)
        shape = (self.height, self.width)
        self.sum = np.zeros((*shape, channels), dtype=np.float32)
        self.m2 = np.zeros((*shape, channels), dtype=np.float32)
        self.count = np.zeros(shape, dtype=np.float32)
        #: Frames that *touched* the pixel, accepted or not. Coverage is this; defects are
        #: the difference between this and ``count`` (brief 15 §8.2).
        self.seen = np.zeros(shape, dtype=np.float32)
        self.accepted_pixels = 0
        self.rejected_pixels = 0
        self.frames = 0

    def add(self, frame: np.ndarray, mask: np.ndarray) -> tuple[int, int]:
        """Accumulate one warped frame. Returns ``(accepted, rejected)`` pixel counts.

        ``frame`` is ``(h, w, c)`` float32 in the reference frame; ``mask`` is ``(h, w)``
        boolean, true where the warp had real data (no border fill).
        """
        if frame.shape != (self.height, self.width, self.channels):
            raise ValueError(f"frame shape {frame.shape} does not fit the accumulator")
        if mask.shape != (self.height, self.width):
            raise ValueError(f"mask shape {mask.shape} does not fit the accumulator")

        data = np.asarray(frame, dtype=np.float32)
        valid = np.asarray(mask, dtype=bool) & np.isfinite(data).all(axis=2)
        self.seen += valid

        count = self.count
        mean = self.sum / np.maximum(count, 1.0)[..., None]
        # Variance needs two samples; before that there is nothing to be an outlier from.
        variance = self.m2 / np.maximum(count - 1.0, 1.0)[..., None]
        deviation = np.abs(data - mean)
        spread = self.sigma * np.sqrt(np.maximum(variance, 0.0))
        rejects = (count[..., None] >= 2) & (deviation > spread)
        accept = valid & ~rejects.any(axis=2)

        self.sum += np.where(accept[..., None], data, 0.0)
        new_count = count + accept
        new_mean = self.sum / np.maximum(new_count, 1.0)[..., None]
        delta = np.where(accept[..., None], data - mean, 0.0)
        self.m2 += delta * (np.where(accept[..., None], data, mean) - new_mean)
        self.count = new_count

        accepted = int(accept.sum())
        rejected = int((valid & ~accept).sum())
        self.accepted_pixels += accepted
        self.rejected_pixels += rejected
        self.frames += 1
        return accepted, rejected

    # -- readouts -----------------------------------------------------------
    def mean(self, *, fill_holes: bool = True) -> np.ndarray:
        """The accumulated image. Holes are filled from valid neighbours, not left black."""
        count = self.count
        mean = self.sum / np.maximum(count, 1.0)[..., None]
        holes = count <= 0
        if fill_holes and holes.any():
            # Normalised convolution by hand: a box filter of the sums over a box filter of
            # the counts is the mean of the *valid* neighbours, and three lines of numpy.
            import cv2

            kernel = (FILL_KERNEL, FILL_KERNEL)
            box_sum = cv2.boxFilter(self.sum, -1, kernel, normalize=False)
            box_count = cv2.boxFilter(count, -1, kernel, normalize=False)
            # cv2 drops a trailing axis of length one, so a mono accumulator comes back
            # (h, w) where an RGB one comes back (h, w, 3). Put it back rather than
            # keeping two code paths for the same sum.
            if box_sum.ndim == 2:
                box_sum = box_sum[..., None]
            filled = box_sum / np.maximum(box_count, 1.0)[..., None]
            mean = np.where(holes[..., None], filled, mean)
        return mean

    def coverage(self, *, stride: int = 16) -> Coverage:
        covered = self.seen > 0
        fraction = float(covered.mean()) if covered.size else 0.0
        # ``frames`` only counts frames that were accumulated, so ``seen == frames`` is the
        # intersection. Half a frame of slack absorbs float32 accumulation.
        fully = (self.seen >= self.frames - 0.5) if self.frames else np.zeros_like(covered)
        return Coverage(
            fraction=fraction,
            fully=float(fully.mean()) if fully.size else 0.0,
            rect=largest_rectangle(fully, stride=stride),
        )

    def defect_fraction(self, *, threshold: float = 0.5) -> float:
        """Share of touched pixels that were rejected in most of the frames.

        The count map doubles as a defect map (15 §8.2): a hot pixel is rejected every
        frame because it never moves, while stars move. This is the one-line readout that
        makes a bad sensor self-report instead of requiring darks to discover it.
        """
        touched = self.seen > 0
        if not touched.any():
            return 0.0
        return float(((self.count < threshold * self.seen) & touched).sum() / touched.sum())


__all__ = ["DEFAULT_SIGMA", "FILL_KERNEL", "Accumulator", "Coverage", "largest_rectangle"]

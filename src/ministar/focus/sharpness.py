"""A coarse focus metric that works when nothing is in focus.

HFD is the right metric near focus and the wrong one far from it: a star blurred
across 30 px exceeds the measurement window, so the metric simply returns nothing.
That is exactly when a human is hunting for focus by eye.

This module provides the complement — normalised Laplacian variance, which rises
monotonically toward focus across the whole range. It is the same signal a human
eye uses when watching stars shrink from donuts to points.

Two metrics, two jobs (11 §4, 12 §3):

    far out of focus ....... frame_sharpness  (works anywhere, inaccurate near focus)
    near focus ............. HFD              (precise, but only within its window)

The crossover is where HFD starts returning values at all, i.e. when the median
star fits inside the ~12 px cutout.
"""

from __future__ import annotations

import numpy as np

from ministar.focus.metrics import to_gray


def frame_sharpness(image: np.ndarray) -> float:
    """Normalised high-frequency energy. Higher is sharper.

    Normalising by the frame's own variance makes the number comparable between
    exposures and sky brightnesses, which matters because the user will be watching
    this on a live stream while the scene is otherwise unchanged.
    """
    gray = to_gray(image)
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0

    centre = gray[1:-1, 1:-1]
    laplacian = 4.0 * centre - gray[:-2, 1:-1] - gray[2:, 1:-1] - gray[1:-1, :-2] - gray[1:-1, 2:]

    signal = float(np.var(gray))
    if signal <= 0:
        return 0.0
    return float(np.var(laplacian) / signal)

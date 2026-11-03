"""Sum, count, M2 -- and the three claims brief 15 §3 makes about them.

Each test here is one of the mistakes that produces a plausible picture instead of an
error: dividing by the frame count, moving the mean when a pixel is rejected, or leaving
a hole black. The last one also pins the count map's second job, which is being the
defect map (15 §8.2).
"""

from __future__ import annotations

import numpy as np
import pytest

from ministar.stack.accumulator import Accumulator, largest_rectangle


def ones(shape=(4, 4)) -> np.ndarray:
    return np.ones(shape, dtype=bool)


def test_a_corner_is_not_divided_by_the_frame_count() -> None:
    """The failure brief 15 §3.1 calls out: sum/N paints a dark border that grows."""
    acc = Accumulator(2, 2, channels=1)
    left = np.full((2, 2, 1), 10.0, dtype=np.float32)
    right = np.full((2, 2, 1), 20.0, dtype=np.float32)
    left_mask = np.array([[True, True], [False, False]])
    acc.add(left, left_mask)
    acc.add(right, ~left_mask)

    mean = acc.mean()
    assert mean[0, 0, 0] == pytest.approx(10.0)  # not 5.0
    assert mean[1, 1, 0] == pytest.approx(20.0)  # not 10.0
    assert acc.count[0, 0] == 1
    assert acc.count[1, 1] == 1


def test_the_sigma_gate_rejects_without_incrementing_the_weight() -> None:
    acc = Accumulator(3, 3, channels=1, sigma=4.0)
    flat = np.full((3, 3, 1), 10.0, dtype=np.float32)
    for _ in range(5):
        acc.add(flat, ones((3, 3)))

    hot = flat.copy()
    hot[1, 1] = 1000.0
    accepted, rejected = acc.add(hot, ones((3, 3)))

    assert accepted == 8
    assert rejected == 1
    assert acc.count[1, 1] == 5
    assert acc.mean()[1, 1, 0] == pytest.approx(10.0)
    assert acc.frames == 6


def test_rejection_is_per_pixel_not_per_frame() -> None:
    acc = Accumulator(3, 3, channels=1)
    flat = np.full((3, 3, 1), 5.0, dtype=np.float32)
    for _ in range(4):
        acc.add(flat, ones((3, 3)))

    satellite = flat.copy()
    satellite[0, :] = 900.0  # a trail across one row
    acc.add(satellite, ones((3, 3)))
    assert acc.count[1, 1] == 5  # the rest of the frame was still good
    assert acc.count[0, 1] == 4


def test_holes_are_filled_from_valid_neighbours() -> None:
    acc = Accumulator(4, 4, channels=1)
    frame = np.full((4, 4, 1), 7.0, dtype=np.float32)
    mask = ones((4, 4))
    mask[2, 2] = False
    acc.add(frame, mask)

    mean = acc.mean()
    assert acc.count[2, 2] == 0
    assert mean[2, 2, 0] == pytest.approx(7.0)  # not black


def test_coverage_separates_seen_from_covered_by_every_frame() -> None:
    acc = Accumulator(4, 4, channels=1)
    frame = np.ones((4, 4, 1), dtype=np.float32)
    acc.add(frame, ones((4, 4)))
    partial = ones((4, 4))
    partial[0, :] = False
    acc.add(frame, partial)

    coverage = acc.coverage(stride=1)
    assert coverage.fraction == pytest.approx(1.0)
    assert coverage.fully == pytest.approx(12 / 16)
    assert coverage.rect == (0, 1, 4, 3)


def test_the_largest_rectangle_prefers_the_full_frame_when_it_is_covered() -> None:
    assert largest_rectangle(ones((8, 8)), stride=1) == (0, 0, 8, 8)


def test_the_largest_rectangle_never_claims_a_hole() -> None:
    mask = ones((8, 8))
    mask[3:5, 0:3] = False
    rect = largest_rectangle(mask, stride=1)
    assert rect is not None
    _x, _y, width, height = rect
    assert width * height < 64


def test_an_empty_mask_has_no_rectangle() -> None:
    assert largest_rectangle(np.zeros((8, 8), dtype=bool), stride=1) is None


def test_a_pixel_rejected_in_every_frame_is_reported_as_a_defect() -> None:
    acc = Accumulator(2, 2, channels=1)
    base = np.zeros((2, 2, 1), dtype=np.float32)
    # Two clean frames, because the gate needs a variance before it can reject anything --
    # a rejection on the very first repeat would be fitting noise to noise (15 §3.2).
    acc.add(base, ones((2, 2)))
    acc.add(base, ones((2, 2)))
    for _ in range(4):
        spike = base.copy()
        spike[0, 0] = 500.0
        acc.add(spike, ones((2, 2)))

    assert acc.count[0, 0] == 2
    assert acc.seen[0, 0] == 6
    assert acc.defect_fraction() == pytest.approx(0.25)


def test_frames_counts_only_accumulation_attempts() -> None:
    acc = Accumulator(2, 2, channels=1)
    acc.add(np.zeros((2, 2, 1), np.float32), ones((2, 2)))
    acc.add(np.zeros((2, 2, 1), np.float32), ones((2, 2)))
    assert acc.frames == 2
    assert acc.count.max() == 2


def test_a_frame_that_does_not_fit_is_refused() -> None:
    acc = Accumulator(2, 2, channels=1)
    with pytest.raises(ValueError):
        acc.add(np.zeros((3, 3, 1), np.float32), ones((2, 2)))
    with pytest.raises(ValueError):
        acc.add(np.zeros((2, 2, 1), np.float32), ones((3, 3)))


def test_nan_pixels_are_never_accumulated() -> None:
    acc = Accumulator(2, 2, channels=1)
    frame = np.ones((2, 2, 1), dtype=np.float32)
    frame[0, 0] = np.nan
    acc.add(frame, ones((2, 2)))
    assert acc.count[0, 0] == 0
    assert np.isfinite(acc.mean()).all()

"""Reading a solve back: the facts, and the crop that changes their meaning.

The fixture is brief 07's real solved frame (``CD`` matrix, 1958 px tall, 2.297"/px, PA_y
178.05 deg), so these numbers are not invented.
"""

from __future__ import annotations

import pytest

from ministar.pointing import MIN_USEFUL_FRACTION, crop_factor, describe, solved_width_deg
from ministar.solver.astap import Solution

#: Brief 07 §5, a real solve.
FRAME_A = Solution(
    crpix=(1469.0, 979.5),
    crval=(80.0, 69.23),
    cdelt=(0.0, 0.0),
    crota=(-178.051, -178.036),
    cd=(
        6.3766968388110333e-4,
        2.1696682773295344e-5,
        2.1874558883785833e-5,
        -6.3784928262788855e-4,
    ),
)


def test_the_solved_width_comes_from_the_cd_matrix() -> None:
    """Brief 07 records 1958 px at 2.297"/px = 1.249 deg; the same number from CD."""
    assert solved_width_deg(FRAME_A, width_px=1958) * 60 == pytest.approx(74.95, abs=0.2)


def test_a_full_frame_solve_produces_no_warnings() -> None:
    report = describe(FRAME_A, width_px=1958, expected_width_deg=1.249)

    assert not report.cropped
    assert report.warnings == ()
    assert any("position angle" in fact for fact in report.facts)


def test_a_mild_crop_is_a_note_not_a_warning() -> None:
    """A centred crop keeps the centre and the roll exact; only the edges go unverified."""
    report = describe(FRAME_A, width_px=1958, expected_width_deg=1.249 / 0.95)

    assert report.cropped
    assert report.usable
    assert report.warnings == ()
    assert "centre and roll are good" in report.notes[0]


def test_the_middle_quarter_is_enough_to_work_from() -> None:
    """A 100 mm frame solved at 34% is still worth having: the centre and roll are exact."""
    report = describe(FRAME_A, width_px=5184, expected_width_deg=9.76)

    assert report.cropped
    assert report.usable, "a quarter of the frame is the bar, and 34% clears it"
    assert report.warnings == ()
    assert "edges unverified" in report.notes[0]


def test_below_a_quarter_solved_the_warning_names_the_remedy() -> None:
    """The 50 mm case: an ultra-wide whose field the database can barely sample."""
    report = describe(FRAME_A, width_px=5184, expected_width_deg=26.34)

    assert report.cropped
    assert not report.usable
    warning = report.warnings[0]
    assert "less than a quarter" in warning
    assert "wider star database" in warning
    assert "W08" in warning


def test_the_usable_limit_is_a_quarter_of_the_frame() -> None:
    """25% solved is the bar: 26% is accepted, 24% is not."""
    accepted = 1.249 / (MIN_USEFUL_FRACTION + 0.01)
    rejected = 1.249 / (MIN_USEFUL_FRACTION - 0.01)
    inside = describe(FRAME_A, width_px=1958, expected_width_deg=accepted)
    outside = describe(FRAME_A, width_px=1958, expected_width_deg=rejected)

    assert MIN_USEFUL_FRACTION == 0.25
    assert inside.usable and not inside.warnings
    assert not outside.usable and outside.warnings


def test_without_an_expected_field_there_is_nothing_to_compare() -> None:
    assert crop_factor(FRAME_A, width_px=1958, expected_width_deg=None) is None
    report = describe(FRAME_A, width_px=1958)
    assert not report.cropped
    assert report.usable
    assert report.solved_fraction is None


def test_a_mirrored_solution_warns_that_instructions_flip() -> None:
    mirrored = Solution(
        crpix=FRAME_A.crpix,
        crval=FRAME_A.crval,
        cdelt=FRAME_A.cdelt,
        crota=FRAME_A.crota,
        cd=(FRAME_A.cd[0], FRAME_A.cd[1], FRAME_A.cd[2], -FRAME_A.cd[3]),
    )

    report = describe(mirrored, width_px=1958, expected_width_deg=1.249)

    assert any("mirrored" in warning for warning in report.warnings)


def test_the_position_angle_uses_cd_not_crota() -> None:
    """CROTA2 = -178.036 and PA_y = 178.051 on this frame; both are 'close', one is right."""
    report = describe(FRAME_A, width_px=1958)

    assert "178.05" in report.facts[1]

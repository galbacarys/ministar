"""Reading a solve back: the facts, and the crop that changes their meaning.

The fixture is brief 07's real solved frame (``CD`` matrix, 1958 px tall, 2.297"/px, PA_y
178.05 deg), so these numbers are not invented.
"""

from __future__ import annotations

import pytest

from ministar.pointing import CROP_TOLERANCE, crop_factor, describe, solved_width_deg
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


def test_a_cropped_solve_is_flagged_with_both_numbers() -> None:
    """The 100 mm case: an 8.5 deg frame solved against D80 comes back as a sub-image."""
    report = describe(FRAME_A, width_px=5184, expected_width_deg=9.76)

    assert report.cropped
    warning = report.warnings[0]
    assert "crop" in warning
    assert "34%" in warning or "33%" in warning, warning


def test_the_crop_threshold_is_the_documented_one() -> None:
    inside = 1.249 / (1 - CROP_TOLERANCE / 2)
    outside = 1.249 / (1 - CROP_TOLERANCE * 1.5)
    just_inside = describe(FRAME_A, width_px=1958, expected_width_deg=inside)
    just_outside = describe(FRAME_A, width_px=1958, expected_width_deg=outside)

    assert not just_inside.cropped
    assert just_outside.cropped


def test_without_an_expected_field_there_is_nothing_to_compare() -> None:
    assert crop_factor(FRAME_A, width_px=1958, expected_width_deg=None) is None
    assert not describe(FRAME_A, width_px=1958).cropped


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

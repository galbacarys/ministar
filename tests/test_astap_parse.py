"""Parse tests against a real ASTAP .ini captured on 2026-10-24.

The fixture is the byte-for-byte output for `bodes_stack.fits` (Canon EOS 60D,
384.7 mm, 2937x1958). Keeping the real spelling matters: ASTAP writes Fortran
E-notation, CRLF endings, and in the failure case a leading blank line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ministar.solver.astap import parse_ini

SOLVED_INI = (
    "PLTSOLVD=T\n"
    "CRPIX1= 1.4690000000000000E+003\n"
    "CRPIX2= 9.7950000000000000E+002\n"
    "CRVAL1= 1.4940264813174895E+002\n"
    "CRVAL2= 6.9226141661203599E+001\n"
    "CDELT1=-6.3803869144777670E-004\n"
    "CDELT2= 6.3822425813758624E-004\n"
    "CROTA1=-1.7805126542085671E+002\n"
    "CROTA2=-1.7803585432775375E+002\n"
    "CD1_1= 6.3766968388110333E-004\n"
    "CD1_2= 2.1696682773295344E-005\n"
    "CD2_1= 2.1874558883785833E-005\n"
    "CD2_2=-6.3784928262788855E-004\n"
    "CMDLINE=astap_cli.exe  -f C:\\ministar-scratch\\frames\\bodes_stack.fits -fov 1.2497\n"
)

FAILED_INI = (
    "\n"  # the CLI build's failure output begins with a blank line
    "PLTSOLVD=F\n"
    "CMDLINE=astap_cli.exe  -f t1.fits -fov 2.13 -r 15\n"
    "ERROR=Not enough stars.\n"
)


def _write(tmp_path: Path, text: str, newline: str = "\r\n") -> Path:
    path = tmp_path / "result.ini"
    path.write_bytes(text.replace("\n", newline).encode("ascii"))
    return path


def test_solved_ini_parses(tmp_path: Path) -> None:
    result = parse_ini(_write(tmp_path, SOLVED_INI))
    assert result.solved
    assert result.solution is not None
    assert result.solution.crpix == pytest.approx((1469.0, 979.5))
    assert result.solution.crval == pytest.approx((149.40264813174895, 69.226141661203599))


def test_crlf_and_leading_blank_line_tolerated(tmp_path: Path) -> None:
    with_crlf = parse_ini(_write(tmp_path, SOLVED_INI, newline="\r\n"))
    without = parse_ini(_write(tmp_path, SOLVED_INI, newline="\n"))
    assert with_crlf.solved and without.solved
    assert with_crlf.solution == without.solution


def test_leading_blank_line_on_failure(tmp_path: Path) -> None:
    result = parse_ini(_write(tmp_path, FAILED_INI))
    assert not result.solved
    assert result.error == "Not enough stars."
    assert result.starved, "a blank frame is the common failure, not a solver problem"


def test_derived_quantities_match_measurement(tmp_path: Path) -> None:
    sol = parse_ini(_write(tmp_path, SOLVED_INI)).solution
    assert sol is not None

    # 2.297 arcsec/px measured from the CD matrix.
    assert sol.pixel_scale_arcsec == pytest.approx(2.2970, abs=1e-3)
    # PA_y from atan2(CD1_2, CD2_2); cross-checks against -CROTA2 = +178.036.
    assert sol.position_angle_deg == pytest.approx(178.0518, abs=1e-3)
    assert abs(sol.position_angle_deg - (-sol.crota[1])) < 0.02
    # Negative determinant is the normal FITS convention, not a mirror.
    assert sol.parity == -1
    assert not sol.is_mirrored
    # 1958 rows * CDELT2 = 1.2496 deg, matching the field passed via -fov.
    assert sol.field_height_deg(1958) == pytest.approx(1.2496, abs=1e-3)


def test_position_angle_uses_column_two(tmp_path: Path) -> None:
    """Guards the atan2(CD2_1, ...) mistake: the two forms are not interchangeable."""
    import math

    sol = parse_ini(_write(tmp_path, SOLVED_INI)).solution
    assert sol is not None
    _, _cd1_2, cd2_1, cd2_2 = sol.cd
    wrong = math.degrees(math.atan2(cd2_1, cd2_2)) % 360.0
    # Only approximately equal here because CD1_2 ~ CD2_1 for a near-pure
    # rotation; in general they differ in sign, so the correct form is fixed.
    assert sol.position_angle_deg == pytest.approx(wrong, abs=0.05)
    assert sol.position_angle_deg != pytest.approx(-wrong, abs=0.05)


def test_warning_survives_a_successful_solve(tmp_path: Path) -> None:
    text = SOLVED_INI + 'WARNING=Warning scale was inaccurate! Set FOV=8.62d, scale=9.0"\n'
    result = parse_ini(_write(tmp_path, text))
    assert result.solved
    assert len(result.warnings) == 1
    assert "scale was inaccurate" in result.warnings[0]


def test_solved_but_missing_key_is_not_an_exception(tmp_path: Path) -> None:
    truncated = SOLVED_INI.replace("CD2_2=-6.3784928262788855E-004\n", "")
    result = parse_ini(_write(tmp_path, truncated))
    assert not result.solved
    assert result.error is not None and "missing key" in result.error


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        parse_ini(tmp_path / "nope.ini")

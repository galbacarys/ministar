"""The OpenNGC loader, checked against the real vendored file."""

from __future__ import annotations

import pytest

from ministar.catalog import (
    COMMON_TYPES,
    DEFAULT_TARGET_TYPES,
    EXCLUDED_TYPES,
    Target,
    catalog,
    load_catalog,
    parse_dec,
    parse_ra,
)


@pytest.fixture(scope="module")
def targets() -> tuple[Target, ...]:
    return catalog()


def test_parses_sexagesimal_ra_and_dec() -> None:
    assert parse_ra("00:42:44.30") == pytest.approx(10.6846, abs=1e-3)
    assert parse_dec("+41:16:09.0") == pytest.approx(41.2692, abs=1e-3)
    assert parse_dec("-05:23:28.0") == pytest.approx(-5.3911, abs=1e-3)


def test_loads_the_whole_survey(targets: tuple[Target, ...]) -> None:
    # 14 034 rows minus artefacts; the exact figure guards against a silent
    # truncation (a wrong path, a missing addendum, a bad delimiter).
    assert len(targets) > 13_000


def test_data_artefacts_are_dropped(targets: tuple[Target, ...]) -> None:
    present = {target.type for target in targets}
    assert not (present & EXCLUDED_TYPES)


def test_every_coordinate_is_plausible(targets: tuple[Target, ...]) -> None:
    for target in targets:
        assert 0.0 <= target.ra_deg < 360.0, target.name
        assert -90.0 <= target.dec_deg <= 90.0, target.name


def test_andromeda_has_the_expected_position_and_size(targets: tuple[Target, ...]) -> None:
    m31 = next(t for t in targets if t.messier == 31)
    assert m31.name == "NGC0224"
    assert m31.ra_deg == pytest.approx(10.68, abs=0.05)
    assert m31.dec_deg == pytest.approx(41.27, abs=0.05)
    assert m31.size_arcmin == pytest.approx(177.8, abs=1.0)


def test_messier_list_is_complete_except_the_disputed_m102(
    targets: tuple[Target, ...],
) -> None:
    """109 distinct numbers, not 110 -- and that is correct.

    OpenNGC carries the famous M102 ambiguity as a `Dup` row named "M102" whose `M`
    field is **101**, i.e. it declares M102 a duplicate of M101. We drop duplicates,
    so M102 is absent by construction rather than by accident.
    """
    messier = {t.messier for t in targets if t.messier is not None}
    assert messier == set(range(1, 111)) - {102}
    assert 45 in messier, "M45 lives in the addendum and must be loaded"
    assert 40 in messier, "M40 lives in the addendum and must be loaded"


def test_magnitude_falls_back_from_v_to_b_and_says_so(
    targets: tuple[Target, ...],
) -> None:
    """V covers only 30% of rows, so a filter must not silently mix bands."""
    v_missing = [t for t in targets if t.v_mag is None and t.b_mag is not None]
    assert v_missing, "expected plenty of B-only objects"

    sample = v_missing[0]
    assert sample.magnitude == sample.b_mag
    assert sample.magnitude_band == "B"

    with_v = next(t for t in targets if t.v_mag is not None)
    assert with_v.magnitude == with_v.v_mag
    assert with_v.magnitude_band == "V"


def test_an_object_with_no_magnitude_at_all_is_none() -> None:
    bare = Target(name="X", type="G", ra_deg=0.0, dec_deg=0.0)
    assert bare.magnitude is None
    assert bare.magnitude_band == ""
    assert bare.size_arcmin is None


def test_size_falls_back_to_the_minor_axis() -> None:
    only_minor = Target(name="X", type="G", ra_deg=0.0, dec_deg=0.0, minor_arcmin=3.5)
    assert only_minor.size_arcmin == pytest.approx(3.5)


def test_label_prefers_the_messier_number(targets: tuple[Target, ...]) -> None:
    m31 = next(t for t in targets if t.messier == 31)
    assert m31.label.startswith("M31")
    assert "NGC0224" in m31.label


def test_missing_directory_yields_an_empty_catalogue(tmp_path) -> None:
    assert load_catalog(tmp_path) == []


def test_default_target_types_drop_bare_stars_but_keep_asterisms() -> None:
    """The picker must not open with Sirius.

    The catalogue has 547 individual stars; a magnitude-sorted list of them is not a
    target list. Asterisms stay because they are legitimate wide-field subjects.
    """
    assert "*" not in DEFAULT_TARGET_TYPES
    assert "*Ass" in DEFAULT_TARGET_TYPES
    assert set(DEFAULT_TARGET_TYPES) <= set(COMMON_TYPES)


def test_every_selectable_type_has_a_human_label() -> None:
    from ministar.catalog import TYPE_LABELS

    missing = [code for code in COMMON_TYPES if code not in TYPE_LABELS]
    assert not missing, f"unlabelled types would render as raw codes: {missing}"
    assert TYPE_LABELS["G"] == "Galaxy"
    assert TYPE_LABELS["Neb"] == "Nebula"

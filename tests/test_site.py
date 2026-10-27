"""The observing site: persistence and the refusal to invent coordinates."""

from __future__ import annotations

import json

from ministar.site import Site, load, save, site_path, validate


def test_an_empty_site_is_incomplete() -> None:
    site = Site()
    assert not site.complete
    assert "latitude" in site.describe()
    assert "longitude" in site.describe()


def test_a_half_set_site_is_incomplete() -> None:
    assert not Site(latitude_deg=45.0).complete
    assert not Site(longitude_deg=-77.0).complete
    assert Site(latitude_deg=45.0, longitude_deg=-77.0).complete


def test_describe_shows_the_coordinates_when_set() -> None:
    text = Site(latitude_deg=45.0, longitude_deg=-77.5, name="garden").describe()
    assert "garden" in text
    assert "+45.000" in text
    assert "-77.500" in text


def test_validate_accepts_a_plausible_pair() -> None:
    assert validate("45.0", "-77.5") is None
    assert validate(45.0, -77.5) is None
    assert validate("", "") is None  # both blank is "not set", not an error


def test_validate_rejects_out_of_range_and_junk() -> None:
    assert validate("91", "0") is not None
    assert validate("0", "181") is not None
    assert validate("north", "0") is not None


def test_save_and_load_round_trip(tmp_path) -> None:
    path = tmp_path / "site.json"
    save(path, Site(latitude_deg=45.123, longitude_deg=-77.456, name="garden"))

    restored = load(path)

    assert restored.complete
    assert restored.latitude_deg == 45.123
    assert restored.longitude_deg == -77.456
    assert restored.name == "garden"


def test_load_of_a_missing_file_is_an_empty_site(tmp_path) -> None:
    assert not load(tmp_path / "nope.json").complete


def test_load_of_corrupt_json_is_an_empty_site(tmp_path) -> None:
    path = tmp_path / "site.json"
    path.write_text("{not json", encoding="utf-8")
    assert not load(path).complete


def test_load_of_an_out_of_range_file_is_an_empty_site(tmp_path) -> None:
    """A hand-edited file must not surface an impossible latitude."""
    path = tmp_path / "site.json"
    path.write_text(json.dumps({"latitude_deg": 500, "longitude_deg": 0}), encoding="utf-8")
    assert not load(path).complete


def test_save_is_atomic_and_leaves_no_temporary_file(tmp_path) -> None:
    path = tmp_path / "site.json"
    save(path, Site(latitude_deg=1.0, longitude_deg=2.0))
    assert path.is_file()
    assert not (tmp_path / "site.json.tmp").exists()


def test_site_path_uses_the_filename_constant(tmp_path) -> None:
    assert site_path(tmp_path) == tmp_path / "site.json"

"""The rig profile: persistence, provenance, and the zoom-lens invalidation rule."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ministar.io.tiff import RESOLUTION_UNIT_INCH, FrameExif
from ministar.optics import Optics
from ministar.rig import (
    FOCAL_TOLERANCE,
    PROVENANCE_ORDER,
    RigProfile,
    better_than,
    load,
    reconcile,
    rig_path,
    save,
)


def eos60d_exif(focal_mm: float = 24.0) -> FrameExif:
    """The real numbers read out of a 60D CR2 (16 §11.1)."""
    return FrameExif(
        make="Canon",
        model="Canon EOS 60D",
        lens_model="EF-S24mm f/2.8 STM",
        focal_length_mm=focal_mm,
        focal_plane_x_resolution=5184000 / 905,
        focal_plane_y_resolution=3456000 / 595,
        resolution_unit=RESOLUTION_UNIT_INCH,
        pixel_x_dimension=5184,
        pixel_y_dimension=3456,
        orientation=1,
    )


def a_profile(**overrides: object) -> RigProfile:
    values: dict[str, object] = {
        "width_px": 5184,
        "height_px": 3456,
        "plate_scale_arcsec": 38.11,
        "focal_length_mm": 24.0,
        "source": "exif",
    }
    values.update(overrides)
    return RigProfile(**values)  # type: ignore[arg-type]


# -- what it is for ---------------------------------------------------------
def test_from_exif_uses_the_real_focal_plane_tags() -> None:
    profile = RigProfile.from_frame_exif(eos60d_exif())

    assert profile.source == "exif"
    assert profile.camera == "Canon EOS 60D"
    assert profile.plate_scale_arcsec == pytest.approx(38.11, abs=0.02)
    assert profile.width_px == 5184


def test_a_frame_with_a_bogus_pitch_is_refused_with_a_reason() -> None:
    broken = eos60d_exif()
    broken = FrameExif(**{**broken.__dict__, "focal_plane_x_resolution": 72.0})

    with pytest.raises(ValueError, match="plate solve"):
        RigProfile.from_frame_exif(broken)


def test_optics_round_trips_through_the_profile() -> None:
    optics = Optics.from_plate_scale(
        width_px=5202, height_px=3465, plate_scale_arcsec=2.217, focal_length_mm=400.0
    )

    profile = RigProfile.from_optics(optics, source="solved")
    rebuilt = profile.optics()

    assert rebuilt.width_px == 5202
    assert rebuilt.plate_scale_arcsec == pytest.approx(2.217)
    assert profile.describe().endswith("[solved]")
    assert "400 mm" in profile.describe()


# -- persistence ------------------------------------------------------------
def test_save_and_load_round_trip(tmp_path: Path) -> None:
    profile = a_profile(camera="Canon EOS 60D", lens="EF-S24mm", recorded="2026-10-27")
    path = rig_path(tmp_path)

    save(path, profile)

    assert load(path) == profile


def test_no_profile_is_not_an_error(tmp_path: Path) -> None:
    assert load(rig_path(tmp_path)) is None


def test_a_corrupt_profile_reads_as_no_profile(tmp_path: Path) -> None:
    """A half-written or hand-edited file must not take the picker down."""
    path = rig_path(tmp_path)
    path.write_text("{ not json", encoding="utf-8")
    assert load(path) is None

    path.write_text(json.dumps({"width_px": 100}), encoding="utf-8")
    assert load(path) is None

    path.write_text(json.dumps({**a_profile().as_dict(), "source": "vibes"}), encoding="utf-8")
    assert load(path) is None


def test_save_is_atomic(tmp_path: Path) -> None:
    path = rig_path(tmp_path)
    save(path, a_profile())

    assert not list(tmp_path.glob("*.tmp")), "the temporary file must not survive"


# -- provenance -------------------------------------------------------------
def test_provenance_order_is_explicit() -> None:
    assert PROVENANCE_ORDER["typed"] < PROVENANCE_ORDER["exif"] < PROVENANCE_ORDER["solved"]


def test_a_better_source_replaces_a_worse_one() -> None:
    assert better_than(a_profile(source="exif"), None)
    assert better_than(a_profile(source="exif"), a_profile(source="typed"))
    assert better_than(a_profile(source="solved"), a_profile(source="exif"))


def test_a_worse_source_does_not_replace_a_better_one() -> None:
    """An EXIF read must never undo a solve: 3% error versus measured."""
    assert not better_than(a_profile(source="exif"), a_profile(source="solved"))
    assert not better_than(a_profile(source="solved"), a_profile(source="solved"))


def test_two_exif_reads_of_the_same_lens_do_not_churn() -> None:
    """Equal provenance is not "better", so re-reading an unchanged rig keeps the file."""
    assert not better_than(a_profile(source="exif"), a_profile(source="exif"))


def test_a_lens_change_replaces_the_profile_whatever_its_source() -> None:
    """Swapping 28 mm for 100 mm must replace the profile, even a solved one.

    Provenance answers "which measurement of this setup is better". It does not answer
    "is this still the same setup" -- ``reconcile`` is where that second question lives.
    A 100 mm frame against a solved 28 mm profile is a worse source and also the only
    correct answer for the lens now on the camera, so provenance alone kept the 28 mm
    numbers. That is the reported bug.
    """
    wide = a_profile(source="solved", focal_length_mm=28.0)
    long = a_profile(source="exif", focal_length_mm=100.0)

    assert reconcile(wide, long) is long, "a different lens must replace a solved profile"
    # And back again: the frame describes whichever lens is mounted, not the best ever.
    assert reconcile(long, wide) is wide


def test_a_lens_change_beats_a_typed_profile() -> None:
    """The common first-run case: a typed 28 mm guess, then a real 100 mm frame."""
    merged = reconcile(
        a_profile(source="typed", focal_length_mm=28.0),
        a_profile(source="exif", focal_length_mm=100.0),
    )
    assert merged.source == "exif"
    assert merged.focal_length_mm == 100.0


def test_reconcile_takes_the_camera_label_and_keeps_the_better_geometry() -> None:
    """Same focal length means one configuration: relabel it, do not demote a solve.

    Two consumers, two rules -- the camera knows which lens it is looking through, and
    the solve knows the plate scale better than an EXIF derivation does.
    """
    solved = a_profile(source="solved", focal_length_mm=100.0, lens="old label")
    fresh = a_profile(source="exif", focal_length_mm=100.0, lens="EF100mm f/2.8L Macro")

    merged = reconcile(solved, fresh)

    assert merged.source == "solved", "the measured plate scale must survive"
    assert merged.plate_scale_arcsec == solved.plate_scale_arcsec
    assert merged.lens == "EF100mm f/2.8L Macro", "the camera's label is ground truth"


def test_reconcile_keeps_an_equal_reading_unchanged() -> None:
    """Re-reading the same lens at the same source is a no-op, so the UI can say so."""
    existing = a_profile(source="exif", focal_length_mm=100.0, lens="EF100mm")
    again = a_profile(source="exif", focal_length_mm=100.0, lens="EF100mm")
    assert reconcile(existing, again) == existing


def test_reconcile_with_no_stored_profile_keeps_the_candidate() -> None:
    candidate = a_profile(source="exif", focal_length_mm=100.0)
    assert reconcile(None, candidate) is candidate


# -- the zoom caveat, as arithmetic ----------------------------------------
def test_the_same_focal_length_matches() -> None:
    assert a_profile(focal_length_mm=200.0).focal_matches(200.0)
    assert a_profile(focal_length_mm=200.0).focal_matches(204.0)  # inside 5%


def test_turning_a_zoom_ring_invalidates_the_profile() -> None:
    profile = a_profile(focal_length_mm=200.0)

    assert not profile.focal_matches(135.0)
    assert not profile.focal_matches(300.0)


def test_the_tolerance_is_stated() -> None:
    profile = a_profile(focal_length_mm=200.0)
    edge = 200.0 * (1 + FOCAL_TOLERANCE)

    assert profile.focal_matches(edge)
    assert not profile.focal_matches(edge * 1.01)


def test_an_unknown_focal_length_cannot_contradict_anything() -> None:
    assert a_profile(focal_length_mm=200.0).focal_matches(None)
    assert a_profile(focal_length_mm=None).focal_matches(50.0)

"""The rig profile: what one frame covers, and where each number came from.

The frame-fit work needs three facts -- pixel dimensions, plate scale, and (for the
header) the focal length. They can arrive three ways, and they are not equally good:

``solved``
    A plate solve measured arcsec per pixel directly. Needs no EXIF, no sensor table,
    and no trust in the zoom ring. This is the authority.
``exif``
    The frame's ``FocalPlaneXResolution`` and ``FocalLength``. Correct on Canon and
    Fujifilm, absent on the Nikon and Sony files we checked (16 §11.1), and about 3%
    off the nominal pitch on the 60D.
``typed``
    The user entered it. Always available, always stale the moment a lens changes.

The point of recording the source is that a *worse* answer must never overwrite a
better one, and that a stale one can be noticed rather than silently reused: a zoom
lens keeps its profile only while the reported focal length matches.

The profile deliberately does **not** hold the camera's JPEG frame size
(5184x3456 on the 60D) when a converted frame says otherwise -- dcraw writes the raw
sensor area, 5202x3465, which is what every FITS we produce will contain.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

from ministar.optics import Optics

#: File name inside ``Paths.data``.
RIG_FILENAME = "rig.json"

#: Higher wins. A solve is measured, EXIF is reported, typing is a claim, and a fake
#: profile (fake camera mode) is worth less than all of them -- it is never saved.
PROVENANCE_ORDER = {"fake": -1, "typed": 0, "exif": 1, "solved": 2}

#: How far the reported focal length may drift before a profile is considered stale.
#: Zooms report 1/3-stop steps, so 5% separates "same setting" from "turned the ring".
FOCAL_TOLERANCE = 0.05


@dataclass(frozen=True)
class RigProfile:
    """One frame's geometry, plus where it came from."""

    width_px: int
    height_px: int
    plate_scale_arcsec: float
    focal_length_mm: float | None = None
    source: str = "typed"
    camera: str = ""
    lens: str = ""
    #: ISO 8601 date the profile was established, for "why is this wrong now?".
    recorded: str = ""

    def __post_init__(self) -> None:
        if self.source not in PROVENANCE_ORDER:
            raise ValueError(f"unknown source {self.source!r}")
        if self.width_px <= 0 or self.height_px <= 0:
            raise ValueError("frame dimensions must be positive")
        if not self.plate_scale_arcsec > 0:
            raise ValueError("plate scale must be positive")

    # -- construction -------------------------------------------------------
    @classmethod
    def from_optics(
        cls,
        optics: Optics,
        *,
        source: str = "typed",
        camera: str = "",
        lens: str = "",
        recorded: str = "",
    ) -> RigProfile:
        return cls(
            width_px=optics.width_px,
            height_px=optics.height_px,
            plate_scale_arcsec=optics.plate_scale_arcsec,
            focal_length_mm=optics.focal_length_mm,
            source=source,
            camera=camera,
            lens=lens,
            recorded=recorded,
        )

    @classmethod
    def from_frame_exif(cls, exif, *, recorded: str = "") -> RigProfile:
        """From a frame's EXIF, or ``ValueError`` explaining why it cannot be used."""
        optics = Optics.from_exif(exif, source="EXIF")
        return cls.from_optics(
            optics,
            source="exif",
            camera=exif.model,
            lens=exif.lens_model,
            recorded=recorded,
        )

    @classmethod
    def fake(cls, focal_length_mm: float = 135.0, *, pixel_um: float = 4.434) -> RigProfile:
        """A profile for fake-camera mode, so screens can be tried at a real focal length.

        Default 135 mm: a popular untracked deep-sky length, and one where the frame fit
        is interesting -- wide enough to hold most of the catalogue, long enough that the
        reframe window bites. Never written to disk: the measured profile is the user's,
        and a demo should not overwrite it.
        """
        optics = Optics.from_pitch(
            width_px=5184,
            height_px=3456,
            pixel_um=pixel_um,
            focal_length_mm=focal_length_mm,
            source="fake",
        )
        return cls.from_optics(
            optics,
            source="fake",
            camera="Fake EOS",
            lens=f"Fake {focal_length_mm:g}mm",
        )

    # -- use ----------------------------------------------------------------
    def optics(self) -> Optics:
        return Optics.from_plate_scale(
            width_px=self.width_px,
            height_px=self.height_px,
            plate_scale_arcsec=self.plate_scale_arcsec,
            focal_length_mm=self.focal_length_mm,
            source=self.source,
        )

    def describe(self) -> str:
        """One line for the UI: the field, the scale, and who said so."""
        optics = self.optics()
        focal = f"{self.focal_length_mm:g} mm " if self.focal_length_mm else ""
        return (
            f"{focal}{optics.width_arcmin / 60:.2f} x {optics.height_arcmin / 60:.2f} deg"
            f'  {self.plate_scale_arcsec:.2f}"/px  [{self.source}]'
        )

    def focal_matches(self, focal_mm: float | None, tolerance: float = FOCAL_TOLERANCE) -> bool:
        """True when a freshly read focal length is the one this profile was built at.

        An unknown focal length on either side cannot contradict anything, so it
        matches: the caller still has to decide what to do with the uncertainty.
        """
        if focal_mm is None or self.focal_length_mm is None:
            return True
        if self.focal_length_mm == 0:
            return focal_mm == 0
        return abs(focal_mm - self.focal_length_mm) / abs(self.focal_length_mm) <= tolerance

    # -- persistence --------------------------------------------------------
    def as_dict(self) -> dict[str, object]:
        return {
            "width_px": self.width_px,
            "height_px": self.height_px,
            "plate_scale_arcsec": self.plate_scale_arcsec,
            "focal_length_mm": self.focal_length_mm,
            "source": self.source,
            "camera": self.camera,
            "lens": self.lens,
            "recorded": self.recorded,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> RigProfile:
        def number(key: str) -> float | None:
            value = data.get(key)
            return None if value is None else float(value)  # type: ignore[arg-type]

        width = number("width_px")
        height = number("height_px")
        scale = number("plate_scale_arcsec")
        if width is None or height is None or scale is None:
            raise ValueError("rig profile is missing width_px, height_px or plate_scale_arcsec")
        return cls(
            width_px=int(width),
            height_px=int(height),
            plate_scale_arcsec=scale,
            focal_length_mm=number("focal_length_mm"),
            source=str(data.get("source") or "typed"),
            camera=str(data.get("camera") or ""),
            lens=str(data.get("lens") or ""),
            recorded=str(data.get("recorded") or ""),
        )


def better_than(candidate: RigProfile, existing: RigProfile | None) -> bool:
    """Is ``candidate`` the better *measurement of the same configuration*?

    Only asks about geometry. Provenance compares two readings of one optical setup --
    a solve measures arcsec per pixel, EXIF derives it from the reported focal length
    and is about 3% off, typing is a guess. That ranking is only meaningful while the
    setup is unchanged, so this does **not** decide whether a lens is still the one on
    the camera. Use :func:`reconcile` for a fresh read; it applies this to the geometry
    and takes the identity fields from the camera.
    """
    if existing is None:
        return True
    return PROVENANCE_ORDER[candidate.source] > PROVENANCE_ORDER[existing.source]


def reconcile(existing: RigProfile | None, candidate: RigProfile) -> RigProfile:
    """Merge a fresh camera read into the stored profile. Two consumers, two rules.

    *Session parameters* -- focal length, lens, camera -- describe the rig as it is
    **right now**, and the camera is the only thing that can know that. The candidate's
    values are taken unconditionally. A profile that keeps reporting 24 mm with a 100 mm
    lens mounted is not being conservative, it is simply wrong.

    *Stacking geometry* -- ``plate_scale_arcsec`` and where it came from -- feeds the
    warp and the frame-fit math. There provenance still decides: a measured solve beats
    a 3%-off EXIF derivation of the same configuration.

    A changed focal length is not a disagreement about one configuration, it is a
    different configuration, so the candidate's geometry replaces the old one instead of
    being merged into it. Otherwise the camera's labels are ground truth and the geometry
    keeps the stronger source.
    """
    if existing is None:
        return candidate
    if not existing.focal_matches(candidate.focal_length_mm):
        return candidate
    geometry = candidate if better_than(candidate, existing) else existing
    # ``recorded`` belongs to the measurement, not to the label, so a pure identity
    # refresh must not make the geometry look newly measured.
    return replace(
        geometry,
        camera=candidate.camera or geometry.camera,
        lens=candidate.lens or geometry.lens,
    )


def rig_path(data_directory: Path) -> Path:
    return Path(data_directory) / RIG_FILENAME


def load(path: Path) -> RigProfile | None:
    """Read a rig profile. Missing, unreadable or invalid all mean "no profile".

    ``None`` is a legitimate state, not an error: the picker then shows sizes in
    arcminutes and says it does not know the frame, which is better than a guess.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return RigProfile.from_dict(data)
    except (TypeError, ValueError):
        return None


def save(path: Path, profile: RigProfile) -> None:
    """Write the profile atomically, so a crash cannot leave a truncated file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(profile.as_dict(), indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)


__all__ = [
    "FOCAL_TOLERANCE",
    "PROVENANCE_ORDER",
    "RIG_FILENAME",
    "RigProfile",
    "better_than",
    "load",
    "reconcile",
    "rig_path",
    "save",
]

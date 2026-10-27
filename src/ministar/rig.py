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
from dataclasses import dataclass
from pathlib import Path

from ministar.optics import Optics

#: File name inside ``Paths.data``.
RIG_FILENAME = "rig.json"

#: Higher wins. A solve is measured, EXIF is reported, typing is a claim.
PROVENANCE_ORDER = {"typed": 0, "exif": 1, "solved": 2}

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
    """Should ``candidate`` replace ``existing``? Only if its provenance is better."""
    if existing is None:
        return True
    return PROVENANCE_ORDER[candidate.source] > PROVENANCE_ORDER[existing.source]


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
    "rig_path",
    "save",
]

"""What one frame covers, and what that means for a target.

Pure geometry, no camera and no UI. Everything here is derived from two facts: the
frame's pixel dimensions and the plate scale in arcseconds per pixel. Nothing is
Canon-specific, because the numbers come from a profile that any body can fill --
see :meth:`Optics.from_exif`, :meth:`Optics.from_pitch` and
:meth:`Optics.from_plate_scale`.

**Plate scale is the root quantity, not pixel pitch.** Mainly because the pitch is
frequently absent: measured on real files, Canon and Fujifilm write
``FocalPlaneXResolution`` (5728 ppi on an EOS 60D, 1879 per cm on an X-E5), while a
Nikon NEF and a Sony ARW carry no focal-plane tags at all and offer only the print
resolution, ``XResolution`` -- 300 and 350 dpi -- which has nothing to do with the
sensor. A plate solve measures arcsec per pixel directly and needs no pitch, so pitch
is a *convenience input*, validated, never the only route.

The one number that decides whether a target is comfortable is not a percentage of
the frame but *how often the mount must be re-aimed*. An untracked rig drifts at
``15.0411 * cos(dec)`` arcsec per second of time, so an object that fills a fraction
``f`` of the long axis forces a reframe every

    window = (1 - f) * width_arcsec / (15.0411 * cos(dec))  seconds

which is brief 10 §2.1's retained-area formula solved for time. As a percentage, "50%
is comfortable" is either wildly strict or nearly free depending on focal length and
declination -- 6.4 minutes at 400 mm on the equator, 4 hours at 100 mm near the pole.
Minutes are the honest unit, so minutes (well, seconds) is what the table shows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ministar.camera.base import pixel_scale_arcsec

#: Sidereal rate at the celestial equator, arcsec per second of time. The drift rate
#: of any fixed object is this times ``cos(dec)`` (brief 10 §2.1).
SIDEREAL_RATE_ARCSEC_PER_S = 15.0411

#: Below this, reframing costs more attention than the run is worth. Brief 04 measures
#: a re-aim cadence of 4-6 min as normal, and brief 10 calls 2.5 min at 400 mm "barely
#: worth automating", so 5 min sits between the two. Not a law: it is the default that
#: the TARGET slider exposes, and the user is expected to move it.
DEFAULT_MIN_WINDOW_S = 300.0

#: An object smaller than this is a handful of pixels: a dot, whatever the focal length.
#: The old fixed 2-arcminute threshold was 7 px at 50 mm and 54 px at 400 mm, i.e. wrong
#: at both ends of the same lens.
TINY_PX = 20.0

#: Declination used for the one-number hint beside the slider: a representative
#: mid-latitude target, since a global control cannot know every row's declination.
HINT_DEC_DEG = 45.0

#: Accepted pixel pitches, micrometres. The floor is below any production sensor; the
#: ceiling is above medium format. A print resolution misread as a pitch lands outside
#: it easily: 72 dpi is 352 um.
PLAUSIBLE_PITCH_UM = (1.0, 20.0)


@dataclass(frozen=True)
class Optics:
    """One frame: its pixel dimensions and what a pixel spans on the sky."""

    width_px: int
    height_px: int
    #: Arcseconds per pixel. The measured/derived root quantity -- see the module note.
    plate_scale_arcsec: float
    focal_length_mm: float | None = None
    #: How this was established, for the UI to repeat honestly.
    source: str = "unspecified"

    def __post_init__(self) -> None:
        if self.width_px <= 0 or self.height_px <= 0:
            raise ValueError("frame dimensions must be positive")
        if not self.plate_scale_arcsec > 0:
            raise ValueError("plate scale must be positive")

    # -- construction -------------------------------------------------------
    @classmethod
    def from_plate_scale(
        cls,
        *,
        width_px: int,
        height_px: int,
        plate_scale_arcsec: float,
        focal_length_mm: float | None = None,
        source: str = "measured",
    ) -> Optics:
        """The authoritative route: a plate solve already measured the scale."""
        return cls(
            width_px=width_px,
            height_px=height_px,
            plate_scale_arcsec=plate_scale_arcsec,
            focal_length_mm=focal_length_mm,
            source=source,
        )

    @classmethod
    def from_pitch(
        cls,
        *,
        width_px: int,
        height_px: int,
        pixel_um: float,
        focal_length_mm: float,
        source: str = "pixel pitch",
    ) -> Optics:
        """Derive the scale from pitch and focal length, refusing an implausible pitch."""
        low, high = PLAUSIBLE_PITCH_UM
        if not low <= pixel_um <= high:
            raise ValueError(
                f"{pixel_um:.1f} um pixel pitch is not physical "
                f"(expected {low:g}-{high:g} um); solve a frame instead"
            )
        if focal_length_mm <= 0:
            raise ValueError("focal length must be positive")
        return cls(
            width_px=width_px,
            height_px=height_px,
            plate_scale_arcsec=pixel_scale_arcsec(pixel_um, focal_length_mm),
            focal_length_mm=focal_length_mm,
            source=source,
        )

    @classmethod
    def from_exif(cls, exif, source: str = "EXIF") -> Optics:
        """Best effort from a frame's EXIF; ``ValueError`` says what is wrong.

        Deliberately strict: a Nikon NEF or a Sony ARW has no focal-plane resolution to
        give, so this raises rather than inventing one. A caller that wants to fall back
        should catch, say so, and use a solve instead.
        """
        problem = exif.geometry_problem()
        if problem:
            raise ValueError(problem)
        pitch = exif.pixel_size_um
        if pitch is None or not exif.pixel_x_dimension or not exif.pixel_y_dimension:
            raise ValueError("EXIF has no usable geometry")
        if exif.focal_length_mm is None:
            raise ValueError("EXIF has no focal length")
        return cls.from_pitch(
            width_px=int(exif.pixel_x_dimension),
            height_px=int(exif.pixel_y_dimension),
            pixel_um=pitch,
            focal_length_mm=exif.focal_length_mm,
            source=source,
        )

    # -- the frame ----------------------------------------------------------
    @property
    def width_arcsec(self) -> float:
        return self.width_px * self.plate_scale_arcsec

    @property
    def height_arcsec(self) -> float:
        return self.height_px * self.plate_scale_arcsec

    @property
    def width_arcmin(self) -> float:
        return self.width_arcsec / 60.0

    @property
    def height_arcmin(self) -> float:
        return self.height_arcsec / 60.0

    @property
    def description(self) -> str:
        """One line for the UI, e.g. ``12.8 deg x 8.5 deg (4.43"/px)``."""
        focus = f" at {self.focal_length_mm:g} mm" if self.focal_length_mm else ""
        return (
            f"{self.width_arcmin / 60:.2f} deg x {self.height_arcmin / 60:.2f} deg"
            f' ({self.plate_scale_arcsec:.2f}"/px{focus}, {self.source})'
        )

    # -- targets against it ------------------------------------------------
    def fill(self, major_arcmin: float, minor_arcmin: float | None = None) -> float:
        """Fraction of the frame the object takes, in its better orientation.

        The camera can be rolled, so the object's major axis is put on whichever frame
        axis suits it -- we take the orientation that needs less room. A missing minor
        axis is treated as equal to the major one, which is conservative: the extent is
        then assumed circular rather than guessed in the flattering direction.
        """
        major = abs(major_arcmin) * 60.0
        minor = abs(minor_arcmin if minor_arcmin is not None else major_arcmin) * 60.0
        along_width = max(major / self.width_arcsec, minor / self.height_arcsec)
        across_width = max(minor / self.width_arcsec, major / self.height_arcsec)
        return min(along_width, across_width)

    def object_size_px(self, major_arcmin: float) -> float:
        """How many pixels across its long axis the object spans."""
        return abs(major_arcmin) * 60.0 / self.plate_scale_arcsec

    def drift_arcsec_per_s(self, dec_deg: float) -> float:
        """Sky drift rate at this declination. Negative declination is symmetric."""
        return SIDEREAL_RATE_ARCSEC_PER_S * math.cos(math.radians(dec_deg))

    def window_seconds(self, *, fill: float, dec_deg: float) -> float:
        """Seconds between reframes before an object of ``fill`` leaves the frame.

        Assumes the drift runs along the long axis, which is the case after following
        the rotate-the-camera instruction in brief 10 §2.1c (a 5-second action worth
        1.5x the run time). If the clamp cannot achieve that angle, the honest number is
        ``height_arcsec`` in place of ``width_arcsec`` -- i.e. 0.67x this one.
        """
        rate = self.drift_arcsec_per_s(dec_deg)
        if rate <= 1e-9:
            return float("inf")  # at the pole the object does not drift
        remaining = max(0.0, 1.0 - fill)
        return remaining * self.width_arcsec / rate

    def window_seconds_for(
        self, *, major_arcmin: float, minor_arcmin: float | None, dec_deg: float
    ) -> float:
        return self.window_seconds(fill=self.fill(major_arcmin, minor_arcmin), dec_deg=dec_deg)

    def max_fill(self, *, dec_deg: float, min_window_s: float) -> float:
        """The largest fill that still leaves ``min_window_s`` between reframes."""
        rate = self.drift_arcsec_per_s(dec_deg)
        if rate <= 1e-9:
            return 1.0
        return max(0.0, min(1.0, 1.0 - min_window_s * rate / self.width_arcsec))

    def max_size_arcmin(self, *, dec_deg: float, min_window_s: float) -> float:
        """The largest object (long axis) that fits with that window, in arcminutes."""
        return self.max_fill(dec_deg=dec_deg, min_window_s=min_window_s) * self.width_arcmin

    def hint_size_arcmin(self, *, min_window_s: float, dec_deg: float = HINT_DEC_DEG) -> float:
        """One representative size for a global window control (dec 45 by default).

        A global control cannot know each row's declination, so it states the size it
        implies at a stated, typical declination -- and the table carries the real
        per-row number regardless.
        """
        return self.max_size_arcmin(dec_deg=dec_deg, min_window_s=min_window_s)


__all__ = [
    "DEFAULT_MIN_WINDOW_S",
    "HINT_DEC_DEG",
    "PLAUSIBLE_PITCH_UM",
    "SIDEREAL_RATE_ARCSEC_PER_S",
    "TINY_PX",
    "Optics",
]

"""Which way to turn the camera, and by how much.

An untracked rig drags the sky along the celestial parallel. The frame is 3:2, so how much
of it survives a run depends entirely on whether that drift runs along the long axis or
across the short one -- worth up to `W/H` = 1.5x the time between reframes for a
five-second action (brief 10 §2.1c, measured).

The angles come from brief 07 §2.5::

    q  = atan2(sin H, tan(lat) cos(dec) - sin(dec) cos H)      # parallactic angle, Meeus
    psi = PA_y - q                                             # camera roll vs the ground

``psi`` is the roll *relative to the local horizontal frame*, which is the useful quantity
here because it is known **before** a solve: the user can read it off the clamp, or the fake
telescope can simply be told. ``PA_y`` -- the position angle of the image's +y axis -- then
follows as ``psi + q``, and the drift's angle away from the frame's long axis is

    180 - PA_y      (mod 180)

A north-up camera (``PA_y = 0``) has the drift along its long axis, which is the sanity
check this module is built around.

Sign convention, because a sign error here turns the knob the wrong way: ``psi`` increases
when the camera body is rotated **counter-clockwise as seen from behind it**, which is also
the direction a photographer calls "turn it to the left". Advice is therefore expressed as
a numbered turn of the body, not as an image rotation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Below this the drift is close enough to the long axis (brief 10 §2.1c: "if |theta| > ~10
#: deg: instruct 'rotate the camera by -theta'").
ALIGNED_TOLERANCE_DEG = 10.0


def parallactic_angle(latitude_deg: float, dec_deg: float, hour_angle_deg: float) -> float:
    """Meeus' parallactic angle in degrees, in (-180, 180].

    The angle at the object between the hour circle (toward the pole) and the vertical,
    which is what rotates the sky's motion into the ground frame the camera is bolted to.
    """
    hour = math.radians(hour_angle_deg)
    latitude = math.radians(latitude_deg)
    dec = math.radians(dec_deg)
    q = math.atan2(
        math.sin(hour),
        math.tan(latitude) * math.cos(dec) - math.sin(dec) * math.cos(hour),
    )
    return math.degrees(q)


def drift_angle_from_long_axis(*, psi_deg: float, parallactic_deg: float) -> float:
    """Angle between the sky's drift and the frame's long axis, in [0, 180).

    0 means aligned (drift along the long side, the good case); 90 means across the short
    side, which is the worst case for a 3:2 frame at large retention.
    """
    pa_y = psi_deg + parallactic_deg
    return (180.0 - pa_y) % 180.0


def roll_inputs(
    *,
    solved_pa_deg: float | None,
    camera_roll_deg: float | None,
    camera_reports_pa: bool,
    parallactic_deg: float,
) -> tuple[float, float] | None:
    """The two angles ``roll_advice`` needs, or ``None`` when it cannot know.

    The distinction this exists to keep: a *position angle* already includes the
    parallactic angle, while a *clamp angle* read off the head does not. Adding q to a PA
    double-counts it, and on the fake camera -- whose reported roll **is** its PA -- that
    error is the whole parallactic angle. Measured for M45 at a 39 deg parallactic angle:
    the advice told the user to rotate 39 deg, which rotated the drift *into* the diagonal
    it was trying to avoid, costing 8% of the frame over a 5-minute interval.

    Returns ``(q_used_in_roll_advice, psi)``; q is zero when the PA is known.
    """
    if solved_pa_deg is not None:
        return 0.0, float(solved_pa_deg)
    if camera_reports_pa and camera_roll_deg is not None:
        return 0.0, float(camera_roll_deg)
    if camera_roll_deg is None:
        return None
    return float(parallactic_deg), float(camera_roll_deg)


@dataclass(frozen=True)
class RollAdvice:
    """What to do with the camera, in the units of the thing being turned."""

    #: Degrees to turn, always positive; ``direction`` says which way.
    degrees: float
    #: ``"ccw"``, ``"cw"`` or ``"none"``.
    direction: str
    #: The drift's angle from the long axis before the turn, for the readout.
    angle_from_long_axis: float

    @property
    def aligned(self) -> bool:
        return self.direction == "none"

    def describe(self) -> str:
        if self.aligned:
            return "aligned with the long side"
        return f"turn the camera {self.degrees:.0f} deg {self.direction.upper()}"


def roll_advice(
    *, psi_deg: float, parallactic_deg: float, tolerance_deg: float = ALIGNED_TOLERANCE_DEG
) -> RollAdvice:
    """The shortest turn that puts the drift along the long axis.

    The residual is folded into (-90, 90] first: the long axis has 180 deg of symmetry, so
    a 170 deg error is the same problem as -10 deg and deserves the small correction.
    """
    angle = drift_angle_from_long_axis(psi_deg=psi_deg, parallactic_deg=parallactic_deg)
    # Signed shortest rotation, applied to psi, that zeroes the residual.
    residual = (angle + 90.0) % 180.0 - 90.0
    if abs(residual) <= tolerance_deg:
        return RollAdvice(degrees=abs(residual), direction="none", angle_from_long_axis=angle)
    # psi increases counter-clockwise, and the residual is measured the same way.
    direction = "ccw" if residual > 0 else "cw"
    return RollAdvice(degrees=abs(residual), direction=direction, angle_from_long_axis=angle)


__all__ = [
    "ALIGNED_TOLERANCE_DEG",
    "RollAdvice",
    "drift_angle_from_long_axis",
    "parallactic_angle",
    "roll_advice",
    "roll_inputs",
]

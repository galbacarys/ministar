"""The POINT page as a picture: where the target is, where it should start, and where it goes.

The page already says all of this in words -- "target 3.9 deg east", "move the mount 50' east",
"drift ahead 34 min". Words are precise and slow to read at night. This draws the same facts onto
the live view:

- a **circle** at the target's own position, sized to the object,
- a **dashed line** from where the target enters the frame to where it leaves: the whole run,
- a **reticle** where the target should sit to begin that run,
- an **arrow** from where it is to where it should be (so the correction is a direction, not a
  sentence),
- **chevrons** for which way to turn the camera, matching the roll advice.

Everything is drawn in *live view pixels*: the same frame the preview shows, whatever it is
binned to -- which is why the plate scale is derived from the live view's own width rather than
assumed to be the still's.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ministar.pointing import best_start, drift_direction_px, edge_travel_px
from ministar.projection import TangentPlane

#: Theme colours: dim for the frame's own furniture, bright for the goal.
DIM = "#a33226"
SOFT = "#e06a5c"
BRIGHT = "#ff5a4d"

#: Fraction of the short side used for the margin, matching pointing.EDGE_MARGIN_FRACTION's job.
_MIN_VISIBLE = 8.0


@dataclass(frozen=True)
class Overlay:
    """A frame's worth of pointing geometry, in the frame's own pixels."""

    width: int
    height: int
    #: Where the camera is pointed -- always the frame's centre, by construction.
    here: tuple[float, float]
    target: tuple[float, float]
    goal: tuple[float, float]
    target_radius_px: float
    #: The target's path across the frame: entry and exit points on the boundary.
    entry: tuple[float, float]
    exit: tuple[float, float]
    inside: bool
    roll_direction: str | None = None

    @property
    def move_px(self) -> tuple[float, float]:
        """Where the target must *appear* to move: goal minus where it is."""
        return (self.goal[0] - self.target[0], self.goal[1] - self.target[1])

    @property
    def aligned(self) -> bool:
        """Close enough that the reticle and the circle are the same mark on the screen."""
        mx, my = self.move_px
        return self.inside and math.hypot(mx, my) <= max(_MIN_VISIBLE, self.target_radius_px * 0.3)

    def svg(self, image_uri: str = "") -> str:
        """The overlay markup, in a viewBox of the frame's own pixels.

        The live view is *inside* the SVG rather than behind it: one element, one coordinate
        space, so the marks cannot drift from the stars however the browser sizes things.
        """
        if self.width <= 0 or self.height <= 0:
            return ""
        w, h = self.width, self.height
        stroke = max(1.0, w / 420.0)
        head = stroke * 9.0
        r_target = max(self.target_radius_px, stroke * 6.0)
        r_goal = max(self.target_radius_px, stroke * 8.0)
        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'viewBox="0 0 {w} {h}" width="100%" height="100%" '
            f'preserveAspectRatio="none" style="display:block;pointer-events:none">'
        ]
        if image_uri:
            parts.append(
                f'<image href="{image_uri}" xlink:href="{image_uri}" x="0" y="0"'
                f' width="{w}" height="{h}" preserveAspectRatio="none"/>'
            )

        # The run: the target's path, entry to exit, so the line *is* the interval.
        if self.inside:
            (ex, ey), (xx, yy) = self.entry, self.exit
            parts.append(
                f'<line x1="{ex:.1f}" y1="{ey:.1f}" x2="{xx:.1f}" y2="{yy:.1f}" '
                f'stroke="{DIM}" stroke-width="{stroke:.1f}" '
                f'stroke-dasharray="{stroke * 7:.1f} {stroke * 7:.1f}" opacity="0.7"/>'
            )
            ux, uy = _unit((xx - ex, yy - ey))
            parts.append(_arrowhead(xx, yy, ux, uy, head, DIM, 0.85))

        # Where the camera is pointed: the middle of what it is looking at.
        cx, cy = self.here
        for sx, sy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            parts.append(
                f'<line x1="{cx + sx * stroke * 3:.1f}" y1="{cy + sy * stroke * 3:.1f}" '
                f'x2="{cx + sx * stroke * 9:.1f}" y2="{cy + sy * stroke * 9:.1f}" '
                f'stroke="{DIM}" stroke-width="{stroke:.1f}" opacity="0.9"/>'
            )

        # The target now: a circle at its size, and a dot at its centre.
        tx, ty = self.target
        if self.inside:
            parts.append(
                f'<circle cx="{tx:.1f}" cy="{ty:.1f}" r="{r_target:.1f}" fill="none" '
                f'stroke="{SOFT}" stroke-width="{stroke * 1.6:.1f}" opacity="0.9"/>'
            )
            parts.append(
                f'<circle cx="{tx:.1f}" cy="{ty:.1f}" r="{stroke * 2.4:.1f}" fill="{SOFT}"/>'
            )
        else:
            parts.append(_off_frame_marker(tx, ty, w, h, stroke, head))

        # Where it should start: a reticle, so the eye has one thing to match.
        gx, gy = self.goal
        parts.append(
            f'<circle cx="{gx:.1f}" cy="{gy:.1f}" r="{r_goal:.1f}" fill="none" '
            f'stroke="{BRIGHT}" stroke-width="{stroke * 1.8:.1f}"/>'
        )
        for sx, sy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            parts.append(
                f'<line x1="{gx + sx * r_goal * 1.15:.1f}" y1="{gy + sy * r_goal * 1.15:.1f}" '
                f'x2="{gx + sx * r_goal * 1.7:.1f}" y2="{gy + sy * r_goal * 1.7:.1f}" '
                f'stroke="{BRIGHT}" stroke-width="{stroke * 2.0:.1f}"/>'
            )

        # The correction: from the circle to the reticle, when they are apart.
        mx, my = self.move_px
        reach = math.hypot(mx, my)
        if self.inside and reach > max(r_goal * 0.5, stroke * 6):
            ux, uy = _unit((mx, my))
            start = (tx + ux * (r_target + stroke * 3), ty + uy * (r_target + stroke * 3))
            finish = (gx - ux * (r_goal + stroke * 3), gy - uy * (r_goal + stroke * 3))
            if (finish[0] - start[0]) * ux + (finish[1] - start[1]) * uy > 0:
                parts.append(
                    f'<line x1="{start[0]:.1f}" y1="{start[1]:.1f}" x2="{finish[0]:.1f}" '
                    f'y2="{finish[1]:.1f}" stroke="{SOFT}" stroke-width="{stroke * 1.4:.1f}" '
                    f'opacity="0.9"/>'
                )
                parts.append(_arrowhead(finish[0], finish[1], ux, uy, head * 0.9, SOFT, 0.95))

        # Roll: the chevrons from the advice, dimmed when they are not the instruction.
        if self.roll_direction is not None:
            parts.append(self._roll_chevrons(stroke, head))
        parts.append("</svg>")
        return "".join(parts)

    def _roll_chevrons(self, stroke: float, head: float) -> str:
        y = self.height * 0.93
        out = []
        for side, x_frac, sign in (("ccw", 0.07, -1.0), ("cw", 0.93, 1.0)):
            active = self.roll_direction == side
            colour = BRIGHT if active else DIM
            opacity = "1" if active else "0.3"
            for step in range(2):
                x = self.width * x_frac + sign * step * head * 1.7
                out.append(
                    f'<polyline points="{x:.1f},{y - head:.1f} {x + sign * head:.1f},{y:.1f} '
                    f'{x:.1f},{y + head:.1f}" fill="none" stroke="{colour}" '
                    f'stroke-width="{stroke * 1.5:.1f}" opacity="{opacity}"/>'
                )
        return "".join(out)


def _unit(vector: tuple[float, float]) -> tuple[float, float]:
    length = math.hypot(*vector) or 1.0
    return vector[0] / length, vector[1] / length


def _arrowhead(
    x: float, y: float, ux: float, uy: float, head: float, colour: str, opacity: float
) -> str:
    back_x, back_y = x - ux * head, y - uy * head
    px, py = -uy * head * 0.55, ux * head * 0.55
    return (
        f'<polygon points="{x:.1f},{y:.1f} {back_x + px:.1f},{back_y + py:.1f} '
        f'{back_x - px:.1f},{back_y - py:.1f}" fill="{colour}" opacity="{opacity}"/>'
    )


def _off_frame_marker(x: float, y: float, w: int, h: int, stroke: float, head: float) -> str:
    """When the target is not in the frame, point at it from the nearest edge."""
    cx, cy = w / 2.0, h / 2.0
    ux, uy = _unit((x - cx, y - cy))
    edge = min(
        (w * 0.5 - stroke * 14) / abs(ux) if abs(ux) > 1e-9 else float("inf"),
        (h * 0.5 - stroke * 14) / abs(uy) if abs(uy) > 1e-9 else float("inf"),
    )
    mx, my = cx + ux * edge, cy + uy * edge
    return (
        f'<line x1="{cx + ux * (edge - head * 3):.1f}" y1="{cy + uy * (edge - head * 3):.1f}" '
        f'x2="{mx:.1f}" y2="{my:.1f}" stroke="{SOFT}" stroke-width="{stroke * 1.6:.1f}"/>'
        + _arrowhead(mx, my, ux, uy, head, SOFT, 0.95)
    )


def frame_svg(width: int, height: int, image_uri: str = "") -> str:
    """Just the frame, for when the geometry is not known yet but the picture is."""
    if width <= 0 or height <= 0:
        return ""
    image = (
        f'<image href="{image_uri}" xlink:href="{image_uri}" x="0" y="0" width="{width}" '
        f'height="{height}" preserveAspectRatio="none"/>'
        if image_uri
        else ""
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'viewBox="0 0 {width} {height}" width="100%" height="100%" '
        f'preserveAspectRatio="none" style="display:block;pointer-events:none">{image}</svg>'
    )


def build(
    *,
    width: int,
    height: int,
    pointing: tuple[float, float],
    roll_deg: float,
    optics,
    target_ra_deg: float,
    target_dec_deg: float,
    target_radius_arcmin: float = 0.0,
    roll_direction: str | None = None,
    interval_s: float | None = None,
) -> Overlay:
    """The picture for one frame of live view. Pure geometry: no UI, no camera."""
    if width <= 0 or height <= 0:
        return Overlay(
            width=0,
            height=0,
            here=(0.0, 0.0),
            target=(0.0, 0.0),
            goal=(0.0, 0.0),
            target_radius_px=0.0,
            entry=(0.0, 0.0),
            exit=(0.0, 0.0),
            inside=False,
            roll_direction=roll_direction,
        )

    # The live view's own scale: it may be binned relative to the sensor, and the still's plate
    # scale would then put every mark in the wrong place.
    scale = optics.width_arcsec / float(width)

    def plane_at(ra_deg: float, dec_deg: float) -> TangentPlane:
        return TangentPlane(
            ra_deg=ra_deg,
            dec_deg=dec_deg,
            pa_y_deg=roll_deg,
            plate_scale_arcsec=scale,
            width_px=int(width),
            height_px=int(height),
        )

    here = plane_at(*pointing)
    target = _project(here, target_ra_deg, target_dec_deg)
    goal_ra_dec = best_start(
        target_ra_deg=target_ra_deg,
        target_dec_deg=target_dec_deg,
        optics=optics,
        pa_y_deg=roll_deg,
        target_radius_arcmin=target_radius_arcmin,
        interval_s=interval_s,
    )
    goal = _project(plane_at(*goal_ra_dec), target_ra_deg, target_dec_deg)

    ux, uy = drift_direction_px(here, target_ra_deg, target_dec_deg)
    inside = 0.0 <= target[0] <= width - 1 and 0.0 <= target[1] <= height - 1
    entry = exit_ = target
    if inside:
        ahead = edge_travel_px(target[0], target[1], ux, uy, width, height)
        behind = edge_travel_px(target[0], target[1], -ux, -uy, width, height)
        exit_ = (target[0] + ux * ahead, target[1] + uy * ahead)
        entry = (target[0] - ux * behind, target[1] - uy * behind)

    return Overlay(
        width=int(width),
        height=int(height),
        here=here.centre_px,
        target=target,
        goal=goal,
        target_radius_px=target_radius_arcmin * 60.0 / scale,
        entry=entry,
        exit=exit_,
        inside=inside,
        roll_direction=roll_direction,
    )


def _project(plane: TangentPlane, ra_deg: float, dec_deg: float) -> tuple[float, float]:
    x, y = plane.project(np.array([ra_deg]), np.array([dec_deg]))
    return float(x[0]), float(y[0])


__all__ = ["BRIGHT", "DIM", "SOFT", "Overlay", "build", "frame_svg"]

"""The POINT picture: the same geometry as the advice, in the frame's own pixels."""

from __future__ import annotations

import re

import pytest

from ministar.pointing import best_start, frame_advice
from ministar.rig import RigProfile
from ministar.ui import overlay

RA, DEC, RADIUS = 56.87, 24.12, 50.0  # the Pleiades, 100' across
W, H = 5184, 3456


def _optics():
    return RigProfile.fake().optics()


def _paint(pointing, *, roll=0.0, width=W, height=H, radius=RADIUS, direction="cw"):
    return overlay.build(
        width=width,
        height=height,
        pointing=pointing,
        roll_deg=roll,
        optics=_optics(),
        target_ra_deg=RA,
        target_dec_deg=DEC,
        target_radius_arcmin=radius,
        roll_direction=direction,
    )


def test_aimed_at_the_target_the_circle_is_central_and_the_reticle_is_near_the_edge() -> None:
    optics = _optics()
    painting = _paint((RA, DEC))

    assert painting.target == pytest.approx(painting.here, abs=1.0), "aimed at it: it is central"
    assert painting.inside
    assert not painting.aligned
    # The reticle sits where the run should begin: the object's edge against the entry margin.
    clearance = min(painting.goal[0], painting.goal[1], W - painting.goal[0], H - painting.goal[1])
    assert clearance >= RADIUS * 60 / optics.plate_scale_arcsec, "room for the whole object"


def test_aimed_for_the_run_the_circle_sits_on_the_reticle() -> None:
    start = best_start(
        target_ra_deg=RA,
        target_dec_deg=DEC,
        optics=_optics(),
        pa_y_deg=0.0,
        target_radius_arcmin=RADIUS,
    )
    painting = _paint(start)

    assert painting.aligned, "one mark on the screen: that is the whole point of the picture"
    assert painting.target == pytest.approx(painting.goal, abs=2.0)


def test_the_dashed_line_spans_the_run_from_the_edge_it_enters() -> None:
    """The line is not decoration: its ends are the boundary crossings."""
    painting = _paint((RA, DEC))

    assert painting.entry[0] == pytest.approx(0.0, abs=2.0) or painting.entry[0] == pytest.approx(
        W - 1, abs=2.0
    )
    assert painting.exit[0] == pytest.approx(W - 1, abs=2.0) or painting.exit[0] == pytest.approx(
        0.0, abs=2.0
    )
    length_deg = abs(painting.exit[0] - painting.entry[0]) * _optics().plate_scale_arcsec / 3600
    assert length_deg == pytest.approx(_optics().width_arcmin / 60, rel=0.01)


def test_a_target_outside_the_frame_is_pointed_at_from_the_edge() -> None:
    painting = _paint((RA + 12.0, DEC))

    assert not painting.inside
    markup = painting.svg()
    assert 'stroke="#e06a5c"' in markup, "an edge arrow, not an invisible circle"
    assert not painting.aligned


def test_a_binned_live_view_draws_the_right_size_circle() -> None:
    """Half the width in pixels is double the pixels per arcsecond: the circle must shrink."""
    full = _paint((RA, DEC))
    binned = _paint((RA, DEC), width=W // 4, height=H // 4)

    assert binned.target_radius_px == pytest.approx(full.target_radius_px / 4, rel=0.01), (
        "the same angular size in fewer pixels"
    )
    assert binned.here == pytest.approx((W / 8 - 0.5, H / 8 - 0.5), abs=1.0)


def test_the_arrow_is_the_targets_motion_while_the_text_is_the_mounts() -> None:
    """The two are opposites and both correct: the picture says where to put the circle.

    Moving the mount east makes the sky appear to move west, so the on-screen arrow points at
    where the *target* has to end up. The text names the knob. Drawing the knob's direction on
    the screen instead would make the circle move against the arrow, which is how users end up
    turning the wrong way.
    """
    optics = _optics()
    painting = _paint((RA, DEC))
    advice = frame_advice(
        ra_deg=RA,
        dec_deg=DEC,
        target_ra_deg=RA,
        target_dec_deg=DEC,
        optics=optics,
        pa_y_deg=0.0,
        target_radius_arcmin=RADIUS,
    )

    # The projection has east to the left, so the target moving east means x decreasing.
    assert painting.goal[0] < painting.target[0], "the circle has to go east on the screen"
    east_on_screen = (painting.target[0] - painting.goal[0]) * optics.plate_scale_arcsec / 3600.0
    assert east_on_screen == pytest.approx(-advice.move_east_arcmin / 60.0, rel=0.05)
    # ...and the mount move the words ask for is west, the opposite way.
    assert advice.move_east_arcmin < 0, "so the words say: move the mount west"


def test_the_markup_is_an_svg_the_size_of_the_frame() -> None:
    markup = _paint((RA, DEC)).svg()

    assert markup.startswith("<svg ") and markup.endswith("</svg>")
    assert f'viewBox="0 0 {W} {H}"' in markup
    assert markup.count("<") == markup.count(">")
    # No NaN can reach the DOM: one bad coordinate blanks the whole picture in silence.
    assert not re.search(r"nan|inf", markup, re.IGNORECASE)


def test_an_unusable_frame_draws_nothing_at_all() -> None:
    assert _paint((RA, DEC), width=0, height=0).svg() == ""


def test_the_roll_chevrons_dim_the_direction_that_is_not_the_instruction() -> None:
    cw = _paint((RA, DEC), direction="cw").svg()
    ccw = _paint((RA, DEC), direction="ccw").svg()

    assert cw != ccw
    assert overlay.BRIGHT in cw and overlay.BRIGHT in ccw


def test_the_live_view_is_inside_the_same_svg_as_the_marks() -> None:
    """One element, one coordinate space: alignment cannot depend on how a box is sized."""
    markup = _paint((RA, DEC)).svg("data:image/jpeg;base64,AAAA")

    assert '<image href="data:image/jpeg;base64,AAAA"' in markup
    assert f'width="{W}" height="{H}"' in markup
    assert markup.index("<image") < markup.index("<line"), "the picture goes underneath"


def test_the_frame_can_be_shown_before_any_geometry_is_known() -> None:
    from ministar.ui.overlay import frame_svg

    markup = frame_svg(W, H, "data:image/jpeg;base64,AAAA")
    assert markup.startswith("<svg ") and "<image" in markup

    # No picture yet, no geometry yet: nothing at all rather than a broken mark.
    assert frame_svg(0, 0, "data:image/jpeg;base64,AAAA") == ""

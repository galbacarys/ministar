"""The live-view image must not animate, and no screen may build its own.

Found on the real body: the FOCUS preview sat permanently half-faded and flickering. Nothing
was wrong with the frames -- the pump was delivering ~11/s -- but Quasar's ``q-img`` fades in
every new ``src`` over ~0.3 s and overlays a spinner, so the fade was always still running
when the next frame arrived. Focus is judged by looking at star sharpness, so this was not a
cosmetic problem: the screen could not do its job.

The failure is invisible in Python (``ui.image(...)`` looks correct) and obvious in a browser,
which is why the guard is on the element's props and on the fact that both screens go through
the one helper.
"""

from __future__ import annotations

from pathlib import Path

from ministar.ui import focus as focus_screen
from ministar.ui import screens
from ministar.ui.preview import LIVE_IMAGE_PROPS, live_image


def test_the_live_image_disables_the_fade_and_the_spinner() -> None:
    image = live_image()

    assert image.props["no-transition"] is True, (
        "a fade per frame never completes at ~11 fps and leaves the preview half-faded"
    )
    assert image.props["no-spinner"] is True, "a loading overlay at frame rate is a strobe"


def test_the_live_image_keeps_its_styling_hooks() -> None:
    # The red-on-black theme sizes the box; a preview that loses the class is a 0-width image.
    image = live_image(style="max-width: 880px; width: 100%")

    assert "mn-preview" in image._classes
    assert "880px" in image._style.get("max-width", "")


def test_the_props_are_the_quasar_ones_that_disable_the_transition() -> None:
    """Pinned because the names are Quasar's, not ours: q-img builds the fade from
    ``noTransition ? '' : '-transition'``, so the prop is spelled ``no-transition``."""
    assert LIVE_IMAGE_PROPS.split() == ["no-transition", "no-spinner"]


def test_no_screen_builds_its_own_live_image() -> None:
    """``ui.image`` in a screen is how this bug got in twice (FOCUS and RUN)."""
    for module in (screens, focus_screen):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "ui.image(" not in source, (
            f"{module.__name__} builds an image directly; use preview.live_image() so the "
            "fade and spinner stay off"
        )

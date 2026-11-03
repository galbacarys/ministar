"""The live-view image element, and the two props that make it watchable.

A live view is a *stream*, but Quasar's ``q-img`` treats every new ``src`` as a new picture:
it fades the image in (~0.3 s) and shows a spinner over it while loading. The pump delivers
about 11 frames per second (measured: 114 frames in 10 s on the EOS 60D), so the fade is
still running when the next frame replaces it -- the picture sits permanently half-faded and
flickers, and the spinner blinks on top of that. On a red-on-black screen in the dark it is
not merely ugly, it makes focus impossible, because the thing being judged is star sharpness.

``no-transition`` is the prop that removes it (Quasar builds the fade from
``noTransition ? '' : '-transition'`` on ``q-img__image``), and ``no-spinner`` removes the
loading overlay. Both are Quasar props passed through NiceGUI's ``q-img`` wrapper.

Kept in one function rather than written out at each use, because the failure is invisible in
the Python and obvious in the browser: a screen that forgets these props looks correct in
code review and unusable at the telescope.
"""

from __future__ import annotations

from nicegui import ui

#: Quasar props that make a repeatedly-replaced image behave like a video frame.
#:
#: ``no-transition``: drop the fade-in that restarts with every ``src`` change.
#: ``no-spinner``: drop the loading overlay that would otherwise blink at frame rate.
LIVE_IMAGE_PROPS = "no-transition no-spinner"


def live_image(classes: str = "mn-preview", style: str | None = None):
    """A live-view image: no fade, no spinner, natural aspect ratio.

    Returns the element. Callers set ``source`` and call ``update()`` as usual.
    """
    image = ui.image("").classes(classes).props(LIVE_IMAGE_PROPS)
    if style is not None:
        image = image.style(style)
    return image

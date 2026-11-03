"""The live view's display stretch.

The camera's own JPEG is the wrong thing to show on a dark field: Canon's tone curve leaves a
black rectangle with a few dots in it, and the screen exists so a human can judge whether those
dots are round. These tests pin the three properties that make the stretch usable -- the sky
lands where it was asked to, faint stars become visible, and the parameters do not move unless
the *sky* does.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from ministar.stack.stretch import Stf
from ministar.ui.livestretch import SKY_TARGET, LiveStretch, sky_level


def a_sky(
    *, sky: float = 12.0, noise: float = 3.0, width: int = 160, height: int = 120, seed: int = 0
) -> np.ndarray:
    """Noisy dark background, BGR uint8, as the camera delivers it."""
    rng = np.random.default_rng(seed)
    frame = rng.normal(sky, noise, (height, width, 3))
    return frame.clip(0, 255).astype(np.uint8)


def with_stars(frame: np.ndarray, *, above: float = 12.0) -> np.ndarray:
    """Put a few small stars ``above`` the background, in BGR."""
    out = frame.astype(np.float32).copy()
    for i, (y, x) in enumerate([(20, 30), (40, 70), (60, 110), (80, 20), (100, 90)]):
        out[y, x] += above + 4 * i
        out[y, x + 1] += (above + 4 * i) / 2
    return out.clip(0, 255).astype(np.uint8)


def decode(jpeg: bytes) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    return image


# -- where the sky lands -----------------------------------------------------
def test_the_sky_lands_at_the_requested_level() -> None:
    stretch = LiveStretch()
    shown = decode(stretch.encode(with_stars(a_sky())))

    # The median is the sky, and SKY_TARGET is where it was asked to go. A few levels of
    # slack for JPEG and for the stretch landing the *subsampled* median exactly.
    sky = float(np.median(shown))
    assert abs(sky - SKY_TARGET * 255) <= 8, f"sky landed at {sky:.0f}/255, wanted ~38"


def test_the_camera_frame_is_blacker_than_what_is_shown() -> None:
    """The whole point: the raw frame is unusable and the stretched one is not."""
    raw_frame = with_stars(a_sky())
    shown = decode(LiveStretch().encode(raw_frame))

    assert float(np.median(raw_frame)) < 20.0
    assert float(np.median(shown)) > 2.5 * float(np.median(raw_frame))


# -- stars become visible ----------------------------------------------------
def test_faint_stars_are_lifted_out_of_the_background() -> None:
    """A star only ~4 sigma above the sky is invisible raw and clear after the stretch.

    This is the judgement the screen exists for: at focus time the operator is looking for
    *any* star in the field, not measuring a bright one.
    """
    frame = with_stars(a_sky(noise=3.0), above=12.0)
    shown = decode(LiveStretch().encode(frame))

    def visible(image: np.ndarray, threshold: float) -> int:
        sky = float(np.median(image))
        return int(np.count_nonzero(image.mean(axis=2) > sky + threshold))

    assert visible(shown, 40.0) >= 5, "the stars did not survive the stretch"
    assert visible(frame, 40.0) < visible(shown, 40.0), (
        "the stretched frame should separate more stars than the raw one"
    )


def test_colour_survives_the_stretch() -> None:
    """One transfer across the channels: a red star stays red (BGR order preserved).

    The frame needs a real bright feature for this. ``Stf`` puts white at the brightest
    *sampled* pixel -- brief 15's "the brightest real thing is white" -- so on a sparse
    synthetic field the white point lands in the noise and everything above it clips, which
    tells you nothing about channel order.
    """
    frame = with_stars(a_sky())
    frame[5:15, 5:15] = 200  # the bright thing that sets the white point
    # A patch, not a pixel: the preview is JPEG at q80, and 4:2:0 chroma subsampling averages
    # colour over 2x2 blocks, so one red pixel would come back grey whatever we did.
    frame[50:58, 50:58] = (30, 60, 140)  # distinctly red (BGR), below the white point
    shown = decode(LiveStretch().encode(frame))

    b, g, r = shown[52, 52]
    assert r > g > b, f"channel order was lost: BGR={b},{g},{r}"


# -- the freeze --------------------------------------------------------------
def test_the_stretch_is_not_remeasured_for_noise_wobble() -> None:
    """Per-frame re-measuring is what makes a live view pulse; the parameters must hold."""
    stretch = LiveStretch()
    stretch.encode(a_sky(sky=12.0, seed=1))
    first = stretch.stf
    assert isinstance(first, Stf)

    for seed in range(2, 6):
        stretch.encode(a_sky(sky=12.5, seed=seed))
    assert stretch.stf == first, "the stretch moved with the noise, so the preview will pulse"


def test_a_material_sky_change_does_remeasure() -> None:
    """Twilight, a cap on, a cloud: the stretch has to follow the scene."""
    stretch = LiveStretch()
    stretch.encode(a_sky(sky=12.0))
    first = stretch.stf

    stretch.encode(a_sky(sky=40.0))  # > 1.5x brighter
    assert stretch.stf != first, "a 3x sky change should re-measure the stretch"


def test_restretch_forces_a_fresh_measurement() -> None:
    stretch = LiveStretch()
    stretch.encode(a_sky(sky=12.0))
    first = stretch.stf

    stretch.restretch()
    assert stretch.stf is None
    stretch.encode(a_sky(sky=12.0, seed=9))
    assert stretch.stf != first


# -- the encoding ------------------------------------------------------------
def test_encode_returns_a_jpeg_of_the_same_size() -> None:
    frame = with_stars(a_sky(width=200, height=150))
    jpeg = LiveStretch().encode(frame)

    assert jpeg[:2] == b"\xff\xd8", "not a JPEG"
    shown = decode(jpeg)
    assert shown.shape == frame.shape
    assert len(jpeg) > 0


def test_a_uniform_black_frame_stays_black_rather_than_blowing_up() -> None:
    """A lens cap, or a frame with no scene, has zero MAD: the stretch must not divide by it."""
    shown = decode(LiveStretch().encode(np.zeros((32, 32, 3), np.uint8)))
    assert float(np.max(shown)) == 0.0


@pytest.mark.parametrize("sky", [0.0, 1.0, 255.0])
def test_extreme_backgrounds_do_not_raise(sky: float) -> None:
    frame = np.full((24, 24, 3), sky, np.uint8)
    assert LiveStretch().encode(frame)[:2] == b"\xff\xd8"


def test_sky_level_ignores_the_stars() -> None:
    """The level is used to decide when the *scene* changed, so a star must not move it."""
    frame = a_sky(sky=12.0, seed=3)
    starred = with_stars(frame, above=200.0)

    assert sky_level(starred) == pytest.approx(sky_level(frame), abs=1.0)

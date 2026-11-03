"""The live stack: one frame in, a better picture and an honest sentence out.

This is the orchestrator brief 15 describes. It owns, in order:

1. the **frame gate** (§5) — the reasons a whole frame is not worth accumulating;
2. the **predicted transform** (§1) — the model in ``transform.py``, not star matching;
3. the **residual check** (§4) — one ``phaseCorrelate``, which catches the one thing the
   model cannot predict: a hand or a gust on the tripod;
4. the **warp and accumulate** (§2-§3);
5. the **frozen stretch and JPEG** (§6).

Two decisions worth stating because they are visible in the numbers on screen:

* **Rejection is counted, never silent.** Every rejected frame carries a reason, and the
  UI shows the tally. A stacker that quietly drops half the frames is a stacker whose
  integration time is a lie.
* **The residual is a gate, not an aligner.** The warp is always the prediction. If the
  measured shift disagrees with it, the frame is rejected rather than aligned to the
  measurement — because a frame that needed a different transform is evidence the mount
  moved, and aligning it would hide that from the user (12 §7).
"""

from __future__ import annotations

import math
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime

import cv2
import numpy as np

from ministar import quality
from ministar.focus.metrics import measure_hfd
from ministar.focus.noise import robust_sigma
from ministar.stack import frames as frame_ops
from ministar.stack.accumulator import DEFAULT_SIGMA, Accumulator, Coverage
from ministar.stack.stretch import Stf
from ministar.stack.transform import (
    FieldModel,
    apply,
    compose,
    fit_homography,
    invert,
    rotation_deg,
)

#: HFD this many times the running median means focus moved, not that the seeing is bad
#: (brief 15 §5). Tighter than the focus screen's 2.0: this is a *drift* alarm on a field
#: that was in focus minutes ago, not a "should I refocus?" question.
HFD_DRIFT_FACTOR = 1.3

#: How far above the background the brightest pixel must be before the frame is called
#: non-empty. Deliberately a *signal* test, not a star count: measuring stars needs
#: unsaturated ones, and a bright field (or a bright star in the crop) clips them all, which
#: makes ``measure_hfd`` return zero on a frame that is plainly full of stars. The gate must
#: not confuse "I cannot measure this" with "there is nothing here".
NO_SIGNAL_SIGMA = 8.0

#: Stride for the background statistics. A frame-wide median costs 60 ms at 4.5 MP for a
#: single scalar, and the sky is flat at the scale that matters here.
NOISE_STRIDE = 4

#: A background this many times brighter or dimmer than the running median is a cloud, dew
#: or twilight — a change of *transparency*, not a change of noise. Deliberately blunt: the
#: gate exists to catch a deck of cloud, not to police the sky's slow dimming.
SKY_CHANGE_FACTOR = 2.0

#: How far the measured frame-to-frame shift may disagree with the prediction before the
#: frame is called a bump. Binned pixels: at 4.5 MP this is ~4 native pixels, i.e. ~25" at
#: 135 mm. A hand on the tripod moves the field by degrees; a gust by arcminutes.
RESIDUAL_TOL_PX = 4.0

#: Below this phase-correlation response the residual is not evidence of anything (a
#: star-poor frame, or a nearly featureless one). Silence is not stillness.
MIN_RESIDUAL_RESPONSE = 0.15

#: The stretch is measured on the fifth accepted frame and then held (brief 15 §6). Before
#: then it is recomputed each frame, so the first recognisable picture appears immediately.
STRETCH_AFTER_FRAMES = 5

#: Width of the preview JPEG. The browser shows it at ~880 CSS px; 1200 covers a retina
#: laptop and keeps each frame under ~200 KB.
DISPLAY_WIDTH = 1200

#: Where the *preview* puts the sky, as a fraction of full scale. `Stf`'s 0.25 default is
#: PixInsight's desktop convention; this screen is looked at in the dark (14 §6), and a sky
#: at 25% grey reads as a lit screen with a haze over it -- measured: a 60-frame stack at
#: 0.25 had 99% of its pixels between 0.25 and 0.35, i.e. a flat grey wash. At 0.06 the
#: background sits at 15/255 while the *displayed* noise still falls with N (measured
#: 5.19 -> 2.68 -> 1.24 over 5/15/60 frames), which is the feedback the freeze exists for.
SKY_TARGET = 0.06

#: How far a target must be from every canvas edge before the preview will centre on it.
#: Below this, a centred crop is a postage stamp (aiming at the entry corner puts the target
#: 2% from an edge, i.e. a 4% crop), so the preview falls back to the fully-covered rectangle
#: and the HUD says so. The preview should not silently zoom 25x because of where the mount
#: was aimed.
MIN_TARGET_MARGIN = 0.12

#: A restretch is triggered by a *material* change in sky brightness, never by a timer.
#: Re-measuring the stretch periodically re-normalises by the current noise, so the
#: displayed noise stops falling and the picture looks frozen even as the stack deepens
#: (measured: a fresh stretch at frame 60 was noisier than the frame-5 one). What the
#: freeze genuinely cannot survive is the sky itself changing -- moonrise, twilight, a
#: bank of cloud -- so that is the trigger.
RESTRETCH_SKY_DRIFT = 1.5


@dataclass(frozen=True)
class FrameResult:
    """What happened to one frame, in the words the HUD will repeat."""

    accepted: bool
    reason: str
    hfd_px: float
    stars: int
    residual_px: float
    sky_luma: float


def empty_stats(width: int, height: int, bin_factor: int) -> StackStats:
    """The zero state, for a runner that has not read its first frame yet."""
    return StackStats(
        width=width,
        height=height,
        bin_factor=bin_factor,
        frames=0,
        accepted=0,
        rejected=0,
        reasons=(),
        integration_s=0.0,
        hfd_px=float("nan"),
        stars=0,
        coverage=Coverage(0.0, 0.0, None),
        defect_fraction=0.0,
        rotation_deg=0.0,
        jpeg=b"",
        crop=None,
        crop_rule="usable",
    )


@dataclass(frozen=True)
class StackStats:
    """A snapshot of the stack, safe to hand across threads."""

    width: int
    height: int
    bin_factor: int
    frames: int
    accepted: int
    rejected: int
    reasons: tuple[tuple[str, int], ...]
    integration_s: float
    hfd_px: float
    stars: int
    coverage: Coverage
    defect_fraction: float
    rotation_deg: float
    jpeg: bytes
    #: The rectangle the preview shows, and why (``target`` or ``usable``).
    crop: tuple[int, int, int, int] | None = None
    crop_rule: str = "usable"


@dataclass
class _History:
    """The running baselines a gate needs, kept together so they age together."""

    hfd: deque[float] = field(default_factory=lambda: deque(maxlen=15))
    sky: deque[float] = field(default_factory=lambda: deque(maxlen=15))

    def hfd_median(self) -> float:
        values = [v for v in self.hfd if math.isfinite(v)]
        return float(np.median(values)) if values else float("nan")

    def sky_median(self) -> float:
        return float(np.median(self.sky)) if self.sky else float("nan")


class LiveStack:
    """One accumulator, one reference frame, one frozen stretch."""

    def __init__(
        self,
        model: FieldModel,
        *,
        bin_factor: int = 2,
        bayer: str | None = None,
        sigma: float = DEFAULT_SIGMA,
        stretch_after: int = STRETCH_AFTER_FRAMES,
        flat: np.ndarray | None = None,
        display_width: int = DISPLAY_WIDTH,
        sky_target: float = SKY_TARGET,
        restretch_sky_drift: float = RESTRETCH_SKY_DRIFT,
        target_ra_deg: float | None = None,
        target_dec_deg: float | None = None,
        crop: str = "target",
    ) -> None:
        if bin_factor < 2:
            raise ValueError("bin factor must be at least 2 (the super-pixel grid)")
        self.model = FieldModel.for_binning(model, bin_factor)
        self.bin_factor = int(bin_factor)
        self.bayer = frame_ops.normalise_bayer(bayer)
        self.flat = flat
        self.display_width = int(display_width)
        self.sky_target = float(sky_target)
        self.restretch_sky_drift = float(restretch_sky_drift)
        if crop not in {"target", "usable"}:
            raise ValueError(f"unknown crop mode {crop!r}")
        self.crop_mode = crop
        self._target = (
            (float(target_ra_deg), float(target_dec_deg))
            if target_ra_deg is not None and target_dec_deg is not None
            else None
        )
        self._crop: tuple[int, int, int, int] | None = None
        self.stretch_after = int(stretch_after)

        self._sigma = float(sigma)
        self._acc: Accumulator | None = None
        self._reference: datetime | None = None
        #: Canvas pixels -> the model's epoch plane. Normally the inverse of the model's
        #: own map at the reference time; a re-solve replaces it so the accumulator's canvas
        #: can stay put while the model changes underneath it (see ``rebase``).
        self._epoch_to_reference: np.ndarray | None = None
        #: The last frame that was *accumulated*, with its transform. The residual is
        #: measured against this anchor, not against the previous frame: comparing with a
        #: rejected frame would let a bumped mount agree with itself from the second frame
        #: onwards, and the stack would carry on misaligned while claiming to be fine.
        self._anchor_gray: np.ndarray | None = None
        self._anchor_matrix: np.ndarray | None = None
        self._history = _History()
        self._reasons: Counter[str] = Counter()
        self._integration_s = 0.0
        self._accepted = 0
        self._rejected = 0
        self._last_stars = 0
        self._last_rotation = 0.0
        self._stf: Stf | None = None
        #: Sky level when the stretch was measured, so a real change in the sky can trigger
        #: a re-measure while a *stable* sky leaves the frozen display alone.
        self._stretch_sky: float | None = None
        self._jpeg = b""
        self._defect_fraction = 0.0
        self._dirty = False

    # -- the one entry point -------------------------------------------------
    def add(self, image: np.ndarray, when: datetime, *, exposure_s: float) -> FrameResult:
        """Gate, warp, accumulate. Returns the reason, including ``""`` for accepted."""
        data = np.asarray(image)
        hfd, stars = self._measure_focus(data)
        prepared = frame_ops.prepare(
            data,
            bin_factor=self.bin_factor,
            bayer=self.bayer,
            flat=self.flat,
        )
        cube = prepared.cube
        gray = cube.mean(axis=2)
        sky_luma = float(np.mean(prepared.sky))

        if self._acc is None:
            self._start(cube.shape)
        assert self._acc is not None

        if self._reference is None:
            self._reference = when
            self._epoch_to_reference = invert(self.model.homography(when))
            matrix = np.eye(3)
            residual, residual_ok = 0.0, True
        else:
            matrix = self._matrix(when)
            self._last_rotation = rotation_deg(
                matrix,
                (self._acc.width / 2.0, self._acc.height / 2.0),  # type: ignore[union-attr]
            )
            residual, residual_ok = self._residual(gray, matrix)

        reason = self._gate(
            gray=gray,
            hfd=hfd,
            sky_luma=sky_luma,
            residual=residual,
            residual_ok=residual_ok,
        )
        if reason:
            self._rejected += 1
            self._reasons[reason] += 1
            self._last_stars = stars
            return FrameResult(False, reason, hfd, stars, residual, sky_luma)

        warped, mask = self._warp(cube, matrix)
        self._acc.add(warped, mask)
        # Only now does this frame become the anchor the next residual is measured from.
        self._anchor_gray = gray
        self._anchor_matrix = matrix
        self._accepted += 1
        self._integration_s += max(0.0, float(exposure_s))
        self._last_stars = stars
        if math.isfinite(hfd):
            self._history.hfd.append(hfd)
        self._history.sky.append(sky_luma)

        if self._stf is None or self._accepted < self.stretch_after or self._sky_changed(sky_luma):
            self._measure_stretch(sky_luma)
        self._dirty = True
        return FrameResult(True, "", hfd, stars, residual, sky_luma)

    def _matrix(self, when: datetime) -> np.ndarray:
        """Canvas pixels -> ``when`` pixels, through the current model and baseline."""
        assert self._epoch_to_reference is not None
        return compose(self.model.homography(when), self._epoch_to_reference)

    def rebase(self, model: FieldModel) -> float:
        """Adopt a fresh solve without moving the accumulator's canvas.

        This is the recovery for the one physical event the pole model cannot predict: a
        hand, a cable, a gust. The residual already detects it; the question is what to do
        next. Astroalign could re-register the frame (brief 15 §4's fallback), but we have
        something better — ASTAP on the frame that failed, which measures the *actual*
        pointing, its roll, and even a focal-length change from focus breathing.

        The canvas stays where it is: the new model is composed with a homography from
        canvas pixels to the new solve's plane, so every earlier frame remains valid and the
        frame that triggered the re-solve can be accumulated. Returns the fraction of the
        canvas that still falls inside the new frame, because a re-solve after a 5-degree
        move "works" while adding almost nothing, and that deserves a warning rather than a
        silent half-empty stack.
        """
        if self._acc is None or self._reference is None or self._epoch_to_reference is None:
            raise RuntimeError("nothing to rebase: no frame has been stacked yet")
        fresh = FieldModel.for_binning(model, self.bin_factor)
        if (fresh.width_px, fresh.height_px) != (self.model.width_px, self.model.height_px):
            raise ValueError(
                f"fresh solve describes {fresh.width_px}x{fresh.height_px}, "
                f"but the canvas is {self.model.width_px}x{self.model.height_px}"
            )

        sample = self.model.sample_pixels()
        epoch_x, epoch_y = apply(self._epoch_to_reference, sample[:, 0], sample[:, 1])
        ra, dec = self.model.plane.deproject_pixels(epoch_x, epoch_y)
        target_x, target_y = fresh.plane.project(ra, dec)
        baseline = fit_homography(sample, np.column_stack([target_x, target_y]))

        self.model = fresh
        self._epoch_to_reference = baseline
        return self._overlap(fresh)

    def _overlap(self, model: FieldModel) -> float:
        """Fraction of the canvas that the frame at ``model.epoch`` still covers."""
        width, height = self.model.width_px, self.model.height_px
        xs = np.linspace(0, width - 1, 9)
        ys = np.linspace(0, height - 1, 9)
        grid_x, grid_y = np.meshgrid(xs, ys)
        to_frame = self._matrix(model.epoch)
        x, y = apply(to_frame, grid_x.ravel(), grid_y.ravel())
        inside = (x >= 0) & (x <= width - 1) & (y >= 0) & (y <= height - 1)
        return float(inside.mean())

    def restretch(self) -> None:
        """Re-measure the stretch. The one thing the frozen display allows on demand."""
        if self._acc is not None:
            self._measure_stretch(self._stretch_sky)

    def _measure_stretch(self, sky_luma: float | None) -> None:
        assert self._acc is not None
        self._stf = Stf.measure(self._acc.mean(), target=self.sky_target)
        self._stretch_sky = None if sky_luma is None else float(sky_luma)
        self._dirty = True

    def _sky_changed(self, sky_luma: float) -> bool:
        """Has the sky brightness moved enough that the frozen black point is wrong?"""
        baseline = self._stretch_sky
        if not baseline or baseline <= 1e-3 or sky_luma <= 1e-3:
            return False
        return (
            sky_luma > self.restretch_sky_drift * baseline
            or sky_luma < baseline / self.restretch_sky_drift
        )

    # -- internals -----------------------------------------------------------
    def _start(self, shape: tuple[int, ...]) -> None:
        height, width, channels = shape
        # The model speaks the same binned grid the accumulator does; a mismatch here would
        # warp every frame to the wrong scale and look like terrible tracking.
        if (width, height) != (self.model.width_px, self.model.height_px):
            raise ValueError(
                f"stacked frame is {width}x{height} but the model expects "
                f"{self.model.width_px}x{self.model.height_px}"
            )
        self._acc = Accumulator(height, width, channels, sigma=self._sigma)

    def _measure_focus(self, image: np.ndarray) -> tuple[float, int]:
        """HFD and star count on the centre crop, before any binning.

        Cheap (a 1200 px crop, 0.06 s, quality.CROP_PX) and measured on the *raw* frame so
        saturation rejection works: ``measure_hfd`` refuses to judge saturated stars, and
        passes over them only for integer arrays.
        """
        try:
            return measure_hfd(quality.centre_crop(image))
        except Exception:  # a diagnostic must never take the loop down
            return float("nan"), 0

    def _residual(self, gray: np.ndarray, matrix: np.ndarray) -> tuple[float, bool]:
        """How far the measured shift is from the predicted one, and whether it is usable.

        One phase correlation between the last *accepted* frame and this one. The prediction
        is the same map the warp uses, evaluated at the frame centre. Over a growing gap the
        real motion includes rotation, which translation-only correlation cannot describe --
        so this is a bump detector with a slowly loosening grip, not an aligner.

        The response threshold is what separates "the shift disagrees" from "there was
        nothing to correlate": a star-poor or clouded frame has no peak, and silence must
        not be read as stillness.
        """
        if (
            self._anchor_gray is None
            or self._anchor_matrix is None
            or self._anchor_gray.shape != gray.shape
        ):
            return 0.0, False
        window = cv2.createHanningWindow((gray.shape[1], gray.shape[0]), cv2.CV_32F)
        try:
            (dx, dy), response = cv2.phaseCorrelate(
                self._anchor_gray.astype(np.float32, copy=False),
                gray.astype(np.float32, copy=False),
                window,
            )
        except cv2.error:
            return 0.0, False
        centre_x = (gray.shape[1] - 1) / 2.0
        centre_y = (gray.shape[0] - 1) / 2.0
        # Anchor-frame prediction: last accepted frame's pixels -> this frame's pixels.
        step = compose(matrix, invert(self._anchor_matrix))
        predicted_x, predicted_y = apply(step, centre_x, centre_y)
        predicted = (float(predicted_x - centre_x), float(predicted_y - centre_y))
        residual = math.hypot(dx - predicted[0], dy - predicted[1])
        return residual, response >= MIN_RESIDUAL_RESPONSE

    def _gate(
        self,
        *,
        gray: np.ndarray,
        hfd: float,
        sky_luma: float,
        residual: float,
        residual_ok: bool,
    ) -> str:
        """The frame-level rejection policy of brief 15 §5, cheapest test first.

        The HFD and sky tests only act when they have a *measured* baseline: a frame whose
        stars are all clipped reports ``nan`` rather than a wrong HFD, and "cannot measure"
        must never be treated as "bad frame". The one unconditional test is that the frame
        is not empty.
        """
        if not self._has_signal(gray):
            return "no signal (cap on the lens, cloud, or too dark)"
        if residual_ok and residual > RESIDUAL_TOL_PX:
            return "the mount moved (measured shift disagreed with the model)"
        baseline = self._history.hfd_median()
        if math.isfinite(hfd) and math.isfinite(baseline) and hfd > HFD_DRIFT_FACTOR * baseline:
            return "focus drifted (HFD rose)"
        sky_median = self._history.sky_median()
        if (
            len(self._history.sky) >= 3
            and abs(sky_median) > 1e-3
            and (
                sky_luma > SKY_CHANGE_FACTOR * sky_median
                or sky_luma < sky_median / SKY_CHANGE_FACTOR
            )
        ):
            return "background changed (cloud or twilight)"
        return ""

    def _has_signal(self, gray: np.ndarray) -> bool:
        """Is there anything in this frame above the noise?"""
        sample = gray[::NOISE_STRIDE, ::NOISE_STRIDE]
        background = float(np.median(sample))
        noise = float(robust_sigma(sample))
        return float(np.max(gray)) - background >= NO_SIGNAL_SIGMA * max(noise, 1e-6)

    def _warp(self, cube: np.ndarray, matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Frame -> reference canvas, plus the mask of pixels that came from real data.

        ``matrix`` maps reference pixels to *this frame's* pixels, and
        ``cv2.warpPerspective`` samples ``dst(p) = src(M⁻¹(p))`` by default — so the
        inverse is what goes in. The mask is eroded by one pixel because a bilinear tap on
        the boundary can mix in the constant border, and brief 15 §1 says the border may
        align but never photometrise.
        """
        height, width = self._acc.height, self._acc.width  # type: ignore[union-attr]
        inverse = invert(matrix)
        warped = cv2.warpPerspective(
            cube,
            inverse,
            (width, height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        ones = np.ones(cube.shape[:2], dtype=np.float32)
        mask = cv2.warpPerspective(
            ones,
            inverse,
            (width, height),
            flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        valid = cv2.erode((mask > 0.99).astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
        if warped.ndim == 2 and self._acc.channels == 1:  # type: ignore[union-attr]
            # cv2 drops a trailing axis of length one, exactly as boxFilter does.
            warped = warped[..., None]
        return warped.astype(np.float32, copy=False), valid

    def target_pixel(self) -> tuple[float, float] | None:
        """Where the target sits on the accumulator's canvas.

        The canvas is the first frame's grid, and the target is fixed on the sky, so this is
        stable for the whole run -- which is what makes a crop centred on it stable too.
        After a re-solve the model and baseline change, but the answer does not: same sky,
        same canvas.
        """
        if self._target is None or self._acc is None or self._epoch_to_reference is None:
            return None
        ra, dec = self._target
        epoch_x, epoch_y = self.model.plane.project(np.array([ra]), np.array([dec]))
        # Scalars, not 1-element arrays: numpy refuses float() on the latter now, and the
        # failure would surface as a preview that silently stopped cropping.
        x, y = apply(invert(self._epoch_to_reference), epoch_x[0], epoch_y[0])
        return float(x), float(y)

    def _crop_rect(self) -> tuple[tuple[int, int, int, int] | None, str]:
        """The rectangle the preview shows, and which rule chose it.

        ``target``: the largest rectangle centred on the target that fits in the canvas. For a
        target aimed at the middle this is the whole frame -- the crop restores the picture
        the shrinking usable rectangle was pushing into a corner. For a target within
        ``MIN_TARGET_MARGIN`` of an edge the centred rectangle would be a postage stamp, so
        the preview falls back to the usable rectangle rather than zooming absurdly.
        """
        if self._acc is None:
            return None, "usable"
        usable = self._acc.coverage().rect
        if self.crop_mode != "target" or self._target is None:
            return usable, "usable"
        point = self.target_pixel()
        if point is None:
            return usable, "usable"
        x, y = point
        width, height = self._acc.width, self._acc.height
        if not (0.0 <= x <= width - 1 and 0.0 <= y <= height - 1):
            return usable, "usable"
        half_w = min(x, width - 1 - x)
        half_h = min(y, height - 1 - y)
        margin = MIN_TARGET_MARGIN * min(width, height)
        if half_w < margin or half_h < margin:
            return usable, "usable"
        left = round(x - half_w)
        top = round(y - half_h)
        return (left, top, max(1, round(2 * half_w)), max(1, round(2 * half_h))), "target"

    def _preview(self) -> bytes:
        """The stretch, crop, downscale and JPEG encode of brief 15 §6.3-§6.4.

        Cropped to the *usable* rectangle: as the field rotates, the edges of the canvas
        receive fewer frames (brief 15 §3.4), and showing them makes the picture look
        progressively softer and lopsided even though the centre is getting better. The
        crop is the largest fully-covered rectangle, so every pixel shown has the depth
        the integration time claims.
        """
        assert self._acc is not None
        mean = self._acc.mean()
        if self._stf is None:
            self._measure_stretch(None)
        assert self._stf is not None
        crop, rule = self._crop_rect()
        self._crop = crop
        self._crop_rule = rule
        if crop is not None:
            x, y, width, height = crop
            mean = mean[y : y + height, x : x + width]
        rgb = self._stf.to_uint8(mean)
        if rgb.ndim == 2:
            rgb = np.repeat(rgb[..., None], 3, axis=2)
        if self.display_width and rgb.shape[1] > self.display_width:
            scale = self.display_width / float(rgb.shape[1])
            size = (self.display_width, max(1, round(rgb.shape[0] * scale)))
            rgb = cv2.resize(rgb, size, interpolation=cv2.INTER_AREA)
        bgr = np.ascontiguousarray(rgb[..., ::-1])
        ok, buffer = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:  # pragma: no cover - cv2 only fails on an impossible array
            raise RuntimeError("cv2 failed to encode the stack preview")
        return buffer.tobytes()

    # -- readout -------------------------------------------------------------
    def stats(self) -> StackStats:
        if self._acc is None:
            return empty_stats(self.model.width_px, self.model.height_px, self.bin_factor)
        if self._dirty:
            self._jpeg = self._preview()
            self._defect_fraction = self._acc.defect_fraction()
            self._dirty = False
        coverage = self._acc.coverage()
        return StackStats(
            width=self._acc.width,
            height=self._acc.height,
            bin_factor=self.bin_factor,
            frames=self._accepted + self._rejected,
            accepted=self._accepted,
            rejected=self._rejected,
            reasons=tuple(self._reasons.most_common()),
            integration_s=self._integration_s,
            hfd_px=self._history.hfd_median(),
            stars=self._last_stars,
            coverage=coverage,
            defect_fraction=self._defect_fraction,
            rotation_deg=self._last_rotation,
            jpeg=self._jpeg,
            crop=self._crop,
            crop_rule=self._crop_rule,
        )


__all__ = [
    "DISPLAY_WIDTH",
    "HFD_DRIFT_FACTOR",
    "MIN_RESIDUAL_RESPONSE",
    "MIN_TARGET_MARGIN",
    "RESIDUAL_TOL_PX",
    "SKY_CHANGE_FACTOR",
    "STRETCH_AFTER_FRAMES",
    "FrameResult",
    "LiveStack",
    "StackStats",
    "empty_stats",
]

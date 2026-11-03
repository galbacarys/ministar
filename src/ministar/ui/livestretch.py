"""The live view's display stretch: what the camera saw, made visible.

The camera hands back a JPEG that has already been through Canon's tone curve, and on a dark
field that is a nearly black rectangle with a handful of faint dots in it. Focus is judged by
eye, so the screen has to show the *sky* without showing the *noise* -- the STF from
``stack/stretch.py``, which the stack preview already uses, does exactly that.

Three decisions worth knowing:

* **Frozen, not per-frame.** The same trap the stack documents (15 §6): re-measuring every
  frame re-normalises by the current noise, so the picture pulses and the thing being watched
  -- stars getting tighter -- is masked by the whole image breathing. Measured here on the real
  body: the sky level jitters frame to frame, and a per-frame stretch turned a steady preview
  into a flickering one. The parameters are measured once and re-measured only when the sky
  itself changes materially (a cap on, a cloud, twilight, or a big slew).

* **A brighter target than the stack's 0.06.** The stack preview is watched for *noise
  falling* over minutes, so its sky sits at 15/255. The live view is watched for *stars
  existing at all*: at 0.06 the field is black with four dots in it, which is not enough to
  focus on. 0.15 puts the sky at 38/255 (still comfortably dark-adapted) while lifting the
  faint stars that the camera's own curve buries.

* **Colour is kept.** One transfer across the channels (``Stf.measure`` takes luminance), so
  the image cannot drift in colour as the sky changes, but a red star still reads as red --
  useful when confirming a field against a chart. A mono preview would detect stars just as
  well; it is a separate choice, not this one.
"""

from __future__ import annotations

import cv2
import numpy as np

from ministar.stack.stretch import SAMPLE_STRIDE, Stf

#: Where the sky lands after the stretch, as a fraction of full scale. See the module note:
#: the stack's 0.06 is too dark to focus on, and 0.25 (STF's desktop default) is a grey wash.
SKY_TARGET = 0.15

#: A sky this many times brighter or dimmer than the one the stretch was measured on is a
#: change of *scene* -- cloud, a cap, twilight, a slew -- and is the only thing that justifies
#: re-measuring. Same reasoning (and same factor) as the stack preview's restretch trigger.
SKY_DRIFT_FACTOR = 1.5

#: JPEG quality for the preview. The frame is 1056x704; at q80 it lands near 70 KB, against
#: ~274 KB for the camera's own JPEG -- the stretch also pays for itself in bandwidth.
JPEG_QUALITY = 80


def sky_level(image: np.ndarray, *, stride: int = SAMPLE_STRIDE) -> float:
    """The background level, robustly: median luminance on a subsampled grid.

    A median rather than a mean because a frame full of stars is exactly the case where the
    mean lies: at 135 mm a dense field moves the mean by several ADU, which would look like a
    change of sky and re-trigger the stretch on every frame.
    """
    data = np.asarray(image, dtype=np.float32)[::stride, ::stride]
    flat = data.reshape(-1, data.shape[-1]).mean(axis=1) if data.ndim == 3 else data.reshape(-1)
    flat = flat[np.isfinite(flat)]
    return float(np.median(flat)) if flat.size else 0.0


class LiveStretch:
    """A frozen STF for the preview, plus the two reasons to replace it.

    Stateful on purpose: the freeze is the feature. ``encode`` is the only method the pump
    needs, and it returns display-ready JPEG bytes.
    """

    def __init__(
        self,
        *,
        target: float = SKY_TARGET,
        drift_factor: float = SKY_DRIFT_FACTOR,
        quality: int = JPEG_QUALITY,
    ) -> None:
        self.target = float(target)
        self.drift_factor = float(drift_factor)
        self.quality = int(quality)
        self._stf: Stf | None = None
        self._sky: float | None = None

    @property
    def stf(self) -> Stf | None:
        """The stretch currently in force, or ``None`` before the first frame."""
        return self._stf

    def restretch(self) -> None:
        """Forget the measured stretch: the next frame measures a fresh one."""
        self._stf = None
        self._sky = None

    def encode(self, image: np.ndarray) -> bytes:
        """Stretch one decoded frame and return it as JPEG bytes."""
        sky = sky_level(image)
        if self._stf is None or self._drifted(sky):
            self._stf = Stf.measure(np.asarray(image, dtype=np.float32), target=self.target)
            self._sky = sky
        stretched = self._stf.to_uint8(image)
        if stretched.ndim == 2:
            stretched = np.repeat(stretched[..., None], 3, axis=2)
        # `image` is BGR (cv2 decode order), and so is what imencode wants; to_uint8 preserves
        # the channel order, so no swap is needed -- doing one would invert the colour.
        ok, buffer = cv2.imencode(
            ".jpg", np.ascontiguousarray(stretched), [cv2.IMWRITE_JPEG_QUALITY, self.quality]
        )
        if not ok:  # pragma: no cover - cv2 only refuses an impossible array
            raise RuntimeError("cv2 failed to encode the live-view preview")
        return buffer.tobytes()

    def _drifted(self, sky: float) -> bool:
        if self._sky is None:
            return True
        if self._sky <= 0.0:
            # A completely dark frame has no ratio to compare; any light at all is a change.
            return sky > 0.0
        return sky > self.drift_factor * self._sky or sky < self._sky / self.drift_factor


__all__ = ["JPEG_QUALITY", "SKY_DRIFT_FACTOR", "SKY_TARGET", "LiveStretch", "sky_level"]

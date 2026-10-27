"""A synthetic star field: the fake image train.

Every screen in this project needs to be developed, screenshotted and tested without a
camera, and a focus screen specifically needs a scene where **blur depends on something
you control**. So the fake is a real model, not a picture:

* Stars are point sources at **sub-pixel** positions with a power-law flux
  distribution -- a few bright ones and many faint ones, which is what makes the star
  detector's threshold behave anything like it does on sky. Integer pixel centres would
  make the HFD curve artificially smooth, because the PSF would always land in the same
  phase relative to the grid.
* The PSF is Gaussian with ``sigma = hypot(sigma_best, k * |position - best|)``: blur
  radii combine in quadrature (the in-focus spot convolved with the defocus disc), so
  sigma is never smaller than the at-focus value, whatever the direction of travel.
* **Total flux is conserved.** Defocus spreads a star, it does not dim it, so the peak
  falls as the area grows. That is exactly what makes a focus curve work, and a fake
  that simply dims stars would let a broken HFD pass.
* Noise is Gaussian read noise, optionally varying frame to frame so a live preview
  is not frozen (same ``frame`` index = identical pixels, which the tests rely on).

What this deliberately is not: real star *geometry*. Pulling actual fields from the
ASTAP catalogue is the next step (so the star patterns match the sky you are pointing
at); random placement is a stand-in, and it is why the field is a value object that can
be built from any list of stars.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

#: Live view on the 60D is 1056x704 (brief 16), so the fake matches it.
DEFAULT_SHAPE = (704, 1056)


@dataclass(frozen=True)
class Star:
    """One point source. Position is in pixels and deliberately not an integer."""

    x: float
    y: float
    #: Total flux in ADU summed over the PSF, so the peak scales as 1/(2 pi sigma^2).
    flux: float


@dataclass(frozen=True)
class StarField:
    """A scene plus the optics model that blurs it."""

    stars: tuple[Star, ...]
    shape: tuple[int, int] = DEFAULT_SHAPE
    #: Focus position at which the PSF is smallest.
    best_position: int = 24
    #: At-focus blur, pixels. 0.7 px is HFD ~1.75, matching this rig (brief 03 §3).
    sigma_best_px: float = 0.7
    #: How fast blur grows per focus unit. 0.06 gives a usefully curved sweep over a
    #: few hundred units, which is the range the routine actually scans.
    defocus_px_per_unit: float = 0.06
    #: Sky background in ADU, and read noise sigma in ADU.
    sky: float = 30.0
    read_noise: float = 4.0
    seed: int = 1

    # -- building scenes ----------------------------------------------------
    @classmethod
    def random(
        cls,
        *,
        count: int = 60,
        seed: int = 1,
        shape: tuple[int, int] = DEFAULT_SHAPE,
        margin: int = 32,
        flux_min: float = 120.0,
        flux_max: float = 6000.0,
        **kwargs: object,
    ) -> StarField:
        """Stars scattered uniformly, with a heavy-tailed brightness distribution.

        Uniform placement is a stand-in for a real field: it exercises the detector
        without pretending to be one specific patch of sky.
        """
        rng = np.random.default_rng(seed)
        height, width = shape
        stars: list[Star] = []
        for _ in range(int(count)):
            x = float(rng.uniform(margin, width - margin))
            y = float(rng.uniform(margin, height - margin))
            # Pareto-ish: mostly faint, occasionally very bright, like a real frame.
            flux = flux_min * (1.0 - rng.random()) ** (-1.0 / 1.3)
            stars.append(Star(x=x, y=y, flux=float(min(flux, flux_max))))
        return cls(stars=tuple(stars), shape=shape, seed=seed, **kwargs)  # type: ignore[arg-type]

    # -- the optics model ---------------------------------------------------
    def sigma_px(self, position: float) -> float:
        """PSF width at a focus position. Never below the at-focus value."""
        offset = self.defocus_px_per_unit * abs(float(position) - self.best_position)
        return math.hypot(self.sigma_best_px, offset)

    def render(self, position: float, *, frame: int = 0, noise: bool = True) -> np.ndarray:
        """The scene as a float image at one focus position.

        ``frame`` only changes the noise, so the same position and frame always yield
        identical pixels; a live preview passes an incrementing counter.
        """
        height, width = self.shape
        sigma = self.sigma_px(position)
        image = np.full((height, width), float(self.sky), dtype=np.float64)
        if sigma > 0 and self.stars:
            # Normalised so that sum(PSF) == flux: defocus spreads, never dims.
            peak_per_flux = 1.0 / (2.0 * math.pi * sigma * sigma)
            reach = max(3, math.ceil(5.0 * sigma))
            denom = 2.0 * sigma * sigma
            for star in self.stars:
                y0 = max(0, math.floor(star.y) - reach)
                y1 = min(height, math.floor(star.y) + reach + 1)
                x0 = max(0, math.floor(star.x) - reach)
                x1 = min(width, math.floor(star.x) + reach + 1)
                if y0 >= y1 or x0 >= x1:
                    continue
                dy = np.arange(y0, y1, dtype=np.float64)[:, None] - star.y
                dx = np.arange(x0, x1, dtype=np.float64)[None, :] - star.x
                image[y0:y1, x0:x1] += (
                    star.flux * peak_per_flux * np.exp(-(dy * dy + dx * dx) / denom)
                )
        if noise and self.read_noise > 0:
            rng = np.random.default_rng((self.seed, round(position), int(frame)))
            image += rng.normal(0.0, self.read_noise, image.shape)
        return np.maximum(image, 0.0)

    def render_uint8(self, position: float, *, frame: int = 0, noise: bool = True) -> np.ndarray:
        """8-bit frame, as a live-view JPEG would arrive. Bright stars clip, as on sky."""
        return np.clip(self.render(position, frame=frame, noise=noise), 0.0, 255.0).astype(np.uint8)

    @property
    def total_flux(self) -> float:
        return float(sum(star.flux for star in self.stars))


__all__ = ["DEFAULT_SHAPE", "Star", "StarField"]

"""A small fake sky that a test can move in controlled steps.

The live stack needs three things that only a camera can supply: frames of the *same*
stars at different times, a geometry to predict their motion, and a frame size small
enough that a test runs in seconds. :class:`MovingSky` is all three, on a 480x320 grid
(so a rendered frame costs ~50 ms rather than the 0.6 s of the full 18 MP sensor).

The catalogue is deliberately stubby -- uniform stars in a 2x2 degree box -- because
these tests are about registration and accumulation, not about matching ASTAP's idea of
a plausible sky. Density is kept near the real fake camera's so the star detector sees
the same kind of field a demo does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from ministar.optics import Optics
from ministar.site import Site
from ministar.stack.transform import FieldModel
from ministar.ui.station import FakeCamera

RA_DEG = 100.0
DEC_DEG = 45.0
ROLL_DEG = 20.0


@dataclass
class StubCatalog:
    """The slice of the catalogue API ``FakeCamera`` actually calls."""

    ra: np.ndarray
    dec: np.ndarray
    mag: np.ndarray

    @property
    def ra_deg(self) -> np.ndarray:
        return self.ra

    @property
    def dec_deg(self) -> np.ndarray:
        return self.dec

    def stars_near(self, ra_deg, dec_deg, radius_deg, mag_limit: float = 99.0):
        return self


class MovingSky:
    """A fake camera standing still while the sky turns underneath it."""

    def __init__(self) -> None:
        self.when = datetime(2026, 1, 1, 22, 0, tzinfo=UTC)
        self.optics = Optics.from_pitch(
            width_px=480, height_px=320, pixel_um=4.434, focal_length_mm=135.0
        )
        self.site = Site(latitude_deg=52.0, longitude_deg=13.0, name="test")
        rng = np.random.default_rng(7)
        catalog = StubCatalog(
            ra=rng.uniform(98.9, 101.1, 400),
            dec=rng.uniform(44.0, 46.0, 400),
            mag=rng.uniform(4.0, 11.0, 400),
        )
        self.camera = FakeCamera(
            shape=(320, 480),
            optics=self.optics,
            catalog=catalog,
            site=self.site,
            clock=lambda: self.when,
            pointing=(RA_DEG, DEC_DEG),
            roll_deg=ROLL_DEG,
            mag_limit=11.0,
        )
        self.epoch = self.when
        self.model = FieldModel(
            ra_deg=self.camera.pointing[0],
            dec_deg=self.camera.pointing[1],
            pa_y_deg=ROLL_DEG,
            plate_scale_arcsec=self.optics.plate_scale_arcsec,
            width_px=480,
            height_px=320,
            epoch=self.epoch,
        )

    def advance(self, seconds: float) -> None:
        self.when += timedelta(seconds=seconds)

    def frame(self) -> np.ndarray:
        return self.camera.render_frame()


@pytest.fixture
def sky() -> MovingSky:
    return MovingSky()

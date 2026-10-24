"""Per-lens, per-focal-length focus calibration.

Because EDSDK has no absolute focus position, the position we report is always
relative to the **far focus stop**, which is a physically repeatable reference:
Canon puts the stop slightly past infinity so the lens can still reach infinity
as it expands with temperature (11 §1).

The stored number is a *bootstrap* hint, not a promise. Measured on this machine
(brief 11 §2.2), the user's EF 100-400L needs offset 0 at 100 mm and 5030 at
400 mm — the 100 mm case needs no step back at all, because a short lens has an
enormous depth of focus. That asymmetry is why the table is per focal length.

Same shape as the N.I.N.A. plugin's ``lenses.json``, plus the step ratios it does
not record.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from ministar.focus.driver import Direction

#: Focal lengths are matched to the nearest stored value within this many mm.
FOCAL_TOLERANCE_MM = 2.0


@dataclass
class FocalEntry:
    mm: float
    stop_offset: int
    last_used: str | None = None


@dataclass
class LensCalibration:
    lens: str
    vendor: str = "Canon"
    focal_lengths: list[FocalEntry] = field(default_factory=list)
    #: Near1-units moved per pulse, per tier. Missing tiers are simply not used.
    step_ratios: dict[str, float] = field(default_factory=lambda: {Direction.NEAR_1.name: 1.0})

    def offset_for(self, mm: float) -> int | None:
        best = _nearest(self.focal_lengths, mm)
        return best.stop_offset if best else None

    def record(self, mm: float, offset: int, when: str | None = None) -> None:
        existing = _nearest(self.focal_lengths, mm)
        if existing is not None:
            existing.stop_offset = int(offset)
            existing.last_used = when
        else:
            self.focal_lengths.append(
                FocalEntry(mm=float(mm), stop_offset=int(offset), last_used=when)
            )

    def ratios(self) -> dict[Direction, float]:
        out: dict[Direction, float] = {}
        for name, value in self.step_ratios.items():
            try:
                out[Direction[name]] = float(value)
            except KeyError:
                continue
        # Near1 is the unit of the coordinate system; it is never optional.
        out.setdefault(Direction.NEAR_1, 1.0)
        return out


def _nearest(entries: list[FocalEntry], mm: float) -> FocalEntry | None:
    if not entries:
        return None
    best = min(entries, key=lambda e: abs(e.mm - mm))
    return best if abs(best.mm - mm) <= FOCAL_TOLERANCE_MM else None


class CalibrationStore:
    """JSON-backed store. Writes atomically so a crash cannot leave it corrupt."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lenses: dict[str, LensCalibration] = {}

    def load(self) -> CalibrationStore:
        self._lenses = {}
        if not self.path.is_file():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt calibration file must never block a session; start fresh.
            return self

        for item in raw.get("lenses", []):
            try:
                lens = LensCalibration(
                    lens=str(item["lens"]),
                    vendor=str(item.get("vendor", "Canon")),
                    focal_lengths=[
                        FocalEntry(
                            mm=float(f["mm"]),
                            stop_offset=int(f.get("stop_offset", 0)),
                            last_used=f.get("last_used"),
                        )
                        for f in item.get("focal_lengths", [])
                    ],
                    step_ratios=dict(item.get("step_ratios", {Direction.NEAR_1.name: 1.0})),
                )
            except (KeyError, TypeError, ValueError):
                continue
            self._lenses[lens.lens] = lens
        return self

    def save(self) -> None:
        payload = {
            "version": 1,
            "lenses": [
                {
                    "lens": lens.lens,
                    "vendor": lens.vendor,
                    "focal_lengths": [
                        {"mm": f.mm, "stop_offset": f.stop_offset, "last_used": f.last_used}
                        for f in lens.focal_lengths
                    ],
                    "step_ratios": lens.step_ratios,
                }
                for lens in self._lenses.values()
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, lens: str) -> LensCalibration:
        if lens not in self._lenses:
            self._lenses[lens] = LensCalibration(lens=lens)
        return self._lenses[lens]

    def __len__(self) -> int:
        return len(self._lenses)


def plausible_offset(offset: int | None, max_reasonable: int = 100_000) -> bool:
    """Guard against a corrupt or absurd stored offset launching a huge move."""
    return offset is not None and 0 <= offset <= max_reasonable and math.isfinite(offset)

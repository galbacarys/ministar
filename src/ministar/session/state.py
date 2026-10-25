"""The session wizard as a state machine.

The UI does not exist yet, but the *flow* is a real design decision and the
ordering has hard dependencies worth encoding rather than discovering later:

* **Focus before solve**, because ASTAP needs stars. A badly defocused frame will
  not plate solve, so asking the user to solve first is asking them to fail.
* **Solve before the run**, because the envelope's entry corner depends on where
  the camera is actually pointing and how it is rolled (10 §5).
* **Parameters before focus**, because the reframe interval determines the usable
  field, which the user should see before committing the evening to it.

The state machine is deliberately permissive: :meth:`SessionState.advance` raises
only on a genuine dependency, and :meth:`blockers` explains what is missing. Per the
README's design stance, nothing here should be a gate for its own sake.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

#: Written to disk so a crash or a restart does not lose the session setup.
STATE_FILENAME = "session.json"


class Phase(Enum):
    PREFLIGHT = "preflight"
    TARGET = "target"
    PARAMETERS = "parameters"
    FOCUS = "focus"
    POINT = "point"
    RUN = "run"
    DONE = "done"


ORDER: tuple[Phase, ...] = (
    Phase.PREFLIGHT,
    Phase.TARGET,
    Phase.PARAMETERS,
    Phase.FOCUS,
    Phase.POINT,
    Phase.RUN,
    Phase.DONE,
)


@dataclass
class SessionState:
    """Everything the wizard collects, and the phase it is in."""

    phase: Phase = Phase.PREFLIGHT

    # Target selection (10 §1)
    target_name: str | None = None
    target_ra_deg: float | None = None
    target_dec_deg: float | None = None

    # Session parameters (10 §4)
    focal_length_mm: float | None = None
    exposure_s: float | None = None
    iso: int | None = None
    reframe_interval_s: float | None = None
    usable_mp: float | None = None

    # Focus (11, 12 §3)
    focus_position: int | None = None
    focus_hfd: float | None = None

    # Pointing (10 §5)
    solved: bool = False
    entry_corner: str | None = None
    pointing_ra_deg: float | None = None
    pointing_dec_deg: float | None = None

    #: Free-form notes surfaced to the user, e.g. "rotate the camera 32 degrees".
    advice: list[str] = field(default_factory=list)

    # -- flow ---------------------------------------------------------------

    def next_phase(self) -> Phase | None:
        index = ORDER.index(self.phase)
        return ORDER[index + 1] if index + 1 < len(ORDER) else None

    def blockers(self) -> list[str]:
        """Reasons the next phase cannot start yet. Empty means it can."""
        upcoming = self.next_phase()
        if upcoming is None:
            return []

        if upcoming is Phase.TARGET:
            return []
        if upcoming is Phase.PARAMETERS:
            return [] if self.target_name else ["no target selected"]
        if upcoming is Phase.FOCUS:
            missing = []
            if self.reframe_interval_s is None:
                missing.append("reframe interval not chosen")
            return missing
        if upcoming is Phase.POINT:
            return [] if self.focus_position is not None else ["camera not focused yet"]
        if upcoming is Phase.RUN:
            return [] if self.solved else ["not plate solved yet"]
        return []

    def can_advance(self) -> bool:
        return not self.blockers()

    def advance(self) -> Phase:
        """Move to the next phase, or raise if a dependency is unmet."""
        upcoming = self.next_phase()
        if upcoming is None:
            return self.phase
        if blocked := self.blockers():
            raise ValueError(f"cannot enter {upcoming.value}: {'; '.join(blocked)}")
        self.phase = upcoming
        return self.phase

    def restart(self) -> Phase:
        """Back to target selection, keeping the equipment-level settings."""
        self.phase = Phase.TARGET
        self.solved = False
        self.entry_corner = None
        return self.phase

    # -- persistence --------------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["phase"] = self.phase.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SessionState:
        payload = dict(data)
        phase = payload.pop("phase", Phase.PREFLIGHT.value)
        try:
            restored = Phase(str(phase))
        except ValueError:
            restored = Phase.PREFLIGHT
        state = cls(**{k: v for k, v in payload.items() if k in cls.__dataclass_fields__})
        state.phase = restored
        return state

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> SessionState:
        if not path.is_file():
            return cls()
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError):
            return cls()

    # -- display ------------------------------------------------------------

    def summary(self) -> str:
        """One-line status, for a window title or a log."""
        bits = [self.phase.value]
        if self.target_name:
            bits.append(self.target_name)
        if self.reframe_interval_s:
            bits.append(f"reframe {self.reframe_interval_s / 60:.0f} min")
        if self.usable_mp:
            bits.append(f"{self.usable_mp:.1f} MP")
        return "  |  ".join(bits)

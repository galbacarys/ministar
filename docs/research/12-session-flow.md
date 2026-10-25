# 12 — Session flow and the setup wizard

**Status:** design doc, 2026-10-24. The UI does not exist yet; the *flow* is a design
decision and the state machine behind it is implemented
(`src/ministar/session/state.py`, 75 tests).

Target flow, per the user:

> pick a target → wizard for parameters (reframe interval / usable MP) → initial focus →
> plate solve and find the starting point → **then** the main loop starts.

---

## 0. The flow

```
  ┌───────────┐   ┌────────┐   ┌────────────┐   ┌───────┐   ┌───────┐   ┌─────┐
  │ PREFLIGHT │ → │ TARGET │ → │ PARAMETERS │ → │ FOCUS │ → │ POINT │ → │ RUN │
  └───────────┘   └────────┘   └────────────┘   └───────┘   └───────┘   └─────┘
   tools, camera   catalog,     interval ↔        aim by       entry       capture,
   lens, site       tonight     usable MP,        hand, then   corner,     stack,
                                camera roll       autofocus    solve       monitor
                                                                 ↓
                                                          reframe (cheap
                                                          re-entry to POINT)
```

**The ordering is a dependency chain, not a preference:**

| Step | Must precede | Because |
|---|---|---|
| FOCUS | POINT | ASTAP needs ~30 stars; a defocused frame will not solve. Asking the user to solve first is asking them to fail. |
| POINT | RUN | The envelope's entry corner depends on where the camera actually points and how it is rolled (10 §5). |
| PARAMETERS | FOCUS | The interval sets the usable field, and the user should see what they are committing the evening to before spending it. |

---

## 1. PREFLIGHT

Already implemented as `ministar preflight`. Its job is to fail *before* the user
drives somewhere dark, not after. Reports: ASTAP + its star database, `EDSDK.dll`,
camera on the bus, lens model and focal length, site latitude/longitude, disk space.

The camera checklist from `preflight.CAMERA_CHECKS` belongs here as a real check
where possible: mirror lockup, LENR, mode dial. Per 08 §7.2b these are *the* cause of
past failures, and N.I.N.A. only ever surfaced them as a message box.

Non-blocking, per the README design stance, except for things that make the app
physically unable to work (no ASDK, no database).

---

## 2. TARGET

OpenNGC, sorted by expected quality rather than filtered (10 §1.2). Each row shows:
name, common name, type, magnitude, **surface brightness**, size vs the current
frame, altitude now and at its peak tonight, and a plain-language verdict — "good
for this rig tonight", "will be small", "too faint for this sky", "bigger than your
frame".

Nothing is hidden. A beginner learns more from a labelled bad choice than from an
empty list.

---

## 3. PARAMETERS

One slider: **reframe interval**, with **usable megapixels** as the headline
readout, plus the whole curve rendered behind it (10 §2.1b/§4). The curve collapses
to zero at a focal-length- and declination-dependent point, and seeing that is worth
more than any single number.

Also surfaced here:

- **Camera roll advice.** If `ψ` puts the drift more than ~30° from the frame's long
  axis, tell the user to rotate the camera: worth up to `W/H` = 1.5× more run time for
  a five-second action (10 §2.1c).
- **Exposure and ISO**, snapped to the legal Tv table, with the duty-cycle
  consequence shown (10 §2.1e).
- **Session length estimate**, so nobody plans four hours at 400 mm without knowing.

---

## 4. FOCUS — the interesting step

### 4.1 The problem with doing it automatically

Driving the lens from fully out-of-focus to focus means traversing the **stop
offset**. At 400 mm that is ~5030 Near1-units (11 §2.2), and each pulse needs a
~100 ms settle:

| strategy | pulses | time |
|---|---|---|
| `Near1` only (what the reference plugin does) | 5030 | **8.4 min** |
| `Near2` at 4:1 | 1258 | 2.1 min |
| `Near3` at 12:1 | 419 | **42 s** |

Plus a measurement frame at every stop along the way, and the measurement is the
unreliable part: a defocused star overflows the HFD window, so the routine is
partially blind exactly where it must search.

### 4.2 The user's proposal: let the human do the coarse search

Stream live view. The user watches stars go from donuts to points and hits a button
when they look sharp. Then autofocus refines from there.

This is the right call, for reasons beyond speed:

- **A human is a better star detector than a threshold** when the PSF is a 200 px
  donut. No tuned `k_min` to get wrong.
- **It cannot fail silently.** A blind walk that mis-thresholds reports "no stars"
  on a perfectly good sky.
- **It keeps the user in the loop** on the one step where their judgement is
  genuinely better than the software's.
- **It is bounded.** 42 s of holding a `Near3` button crosses the entire focus range.

The cost is that the wizard has a step requiring attention. That is acceptable at
setup and unacceptable mid-session — which is why the manual step happens **once per
lens per focal length**, not once per session (§4.4).

### 4.3 Two metrics, because no single one works across the range

| regime | metric | why |
|---|---|---|
| Far out of focus | **normalised Laplacian variance** (`focus/sharpness.py`) | rises monotonically toward focus from any defocus; it is the same signal the human eye is using |
| Near focus | **HFD** (`focus/metrics.py`) | precise, but only valid while the star fits the ~12 px cutout |

Show both live during the hunt: the human judges by eye, the number confirms.

⚠️ **Live view is for hunting, stills are for measuring.** Live-view frames are
8-bit JPEG, often downscaled, and compression-artifacted, so HFD measured on them is
biased and not comparable with HFD from a still. Use live view for the coarse phase
and real captures for the sweep. This also means the fine phase pays the
stop-LV/capture/download/restart-LV cost (02) per sample — ~1-2 s each, so keep the
sweep to ~10 samples.

### 4.4 The bonus: the manual step *is* the calibration

When the user stops and hits "good enough", `driver.position` **is** the stop offset
for this lens at this focal length. Write it to the calibration store.

So there is no separate calibration wizard, and no user-facing "calibrate your lens"
task. The table populates itself through normal use (11 §6):

```
first use of a lens at a focal length ..... manual hunt (~1 min) -> offset recorded
every session after that ................. home, jump to offset, verify (seconds)
```

This is what makes the manual step affordable: it is a one-time cost per
lens/focal-length combination, and the 100-400 L is four combinations at most
(100/200/300/400 mm).

At short focal lengths it is trivial anyway — at 100 mm the calibrated offset is
**0**, so the far stop is already in focus and the step completes immediately.

### 4.5 The MF/AF question

`DriveLensEvf` drives the focus motor; whether the lens must be switched to AF is
unresolved (11 §11). The manual hunt has two possible implementations:

- **(a) The app's own coarse buttons**, pulsing `Near3`/`Far3` while held. Keeps
  everything motor-driven, no switch fiddling, and the position counter stays valid.
- **(b) The user turns the physical ring.** Faster to start, but requires MF, and
  switching back to AF afterwards may be needed — and in MF the position counter
  stops tracking, losing the calibration measurement that is the whole bonus.

**Recommend (a).** It costs 42 s of holding a button to cross the range, and it
preserves the position counter. (b) throws away §4.4.

---

## 5. POINT — getting the target in the right corner

The subtle step. It is not "centre the target" (10 §5): the drift direction is fixed
by physics, so the target must start at the **upstream** corner of the envelope and
be allowed to cross.

```
1. capture + solve
   - no solution & few stars  -> go back to FOCUS
   - no solution & many stars -> "wrong sky, re-aim roughly"
2. compute psi, the drift direction, the envelope, the entry corner
3. show a marker in the live view at the entry corner
4. user nudges; re-solve (or dead-reckon) to confirm
5. inside tolerance (= max(5', 5% of frame height), 07 §4) -> RUN
```

Two things this step must get right:

- **Name the corner in image terms** — "top-left of the frame" — never "east". The
  user is looking at a screen.
- **Handle "target not in frame at all."** If the offset exceeds half a frame, do not
  draw an arrow; give a coarse cardinal instruction and re-solve blind-ish until it
  lands. A 30° arrow on a 4° frame is a lie (07 §3).

---

## 6. RUN, and the cheap re-entry

The main loop is 10 §5–6. The one flow decision worth stating here: **the period reframe
does *not* re-enter the wizard.** It is a lightweight sub-cycle inside RUN:

```
RUN → (target reaches the downstream corner) → pause capture
    → re-solve → instruct "put the target at [the upstream corner]"
    → confirm → resume RUN
```

No parameter re-selection, no re-focus by default (a *check* only, 11 §7), no target
re-selection. The wizard is for setup; the loop owns its own re-pointing.

---

## 7. Interruption and recovery

| Event | Behaviour |
|---|---|
| Clouds | Star count falls → pause, keep the stack, resume when stars return. Focus metric is *not* trusted during this (11 §4). |
| USB disconnect | Reconnect and re-open the session (08 §7.3). The position counter may survive; home if not. |
| Camera sleep | `WillSoonShutDown` → extend the timer (08 §7.4). |
| User pauses | Save state; `SessionState.save()` already persists the whole setup. |
| New target | `SessionState.restart()` → back to TARGET, keeping lens and site settings. |
| Focal length changes | Force a full re-focus: the stop offset is focal-length dependent (11 §2.2). |

---

## 8. Timing budget

The wizard must reach imaging in a couple of minutes, or nobody uses it.

| Step | Expected | Notes |
|---|---|---|
| PREFLIGHT | 2-5 s | local file checks + camera enumerate |
| TARGET | user's choice | no app cost |
| PARAMETERS | 10-30 s | includes a camera-roll adjustment if advised |
| FOCUS, first time on a lens | 60-90 s | manual hunt + stills sweep |
| FOCUS, subsequent | 5-10 s | home, jump to calibrated offset, verify |
| POINT | 30-60 s | solve + a human nudge + confirm |
| **Total to first frame** | **~2 min subsequent, ~3 min first time** | |

---

## 9. What this deliberately does *not* do

- **No equipment inventory / profile editor.** One camera, one lens, one site.
- **No dark-frame library or flat wizard.** Real, valuable, and not the point (see the
  README design stance).
- **No sequence builder.** There is one loop, with one shape.
- **No mosaics.** Out of scope until the single-field flow works.
- **No blocking.** Every step can be walked past with a warning, and the app will do
  its best. The one thing it will not do is start the loop with no focus and no
  solve, because that produces nothing but wasted frames.

---

## 10. Implemented so far

`src/ministar/session/state.py` — `Phase`, `SessionState`, dependency-checked
`advance()`, `blockers()`, JSON persistence that survives a corrupt or unknown-phase
file. The linear wizard is modelled; the in-loop reframe is deliberately *not* a
phase (§6).

Deliberately unimplemented: anything that needs a UI to be meaningful, and the
wizard's ordering of *prompts* rather than its ordering of *dependencies*.

---

## 11. Open questions

- Does `DriveLensEvf` work with the lens switch in MF? Decides §4.5 (a) vs (b) and
  therefore whether the manual step can double as calibration.
- Can live view keep streaming while `DriveLensEvf` pulses are being sent? The whole
  §4.2 design assumes yes. **Unverified** and worth a five-minute test.
- How many live-view frames per second can we actually get *and* decode *and* compute
  sharpness on, on this laptop? Sets the responsiveness of the manual hunt.
- Does the user want the wizard to be resumable across nights, or is a fresh session
  each time the better default?
- Should PARAMETERS default to the previous session's values for the same target?
  Probably yes, with the interval re-validated against tonight's declination.

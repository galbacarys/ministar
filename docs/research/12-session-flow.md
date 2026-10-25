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

### 4.1 Why we do not drive it there ourselves

If the software did the coarse search it would have to traverse the whole focus range,
one ~100 ms pulse at a time:

| strategy | pulses to cross ~5030 units | time |
|---|---|---|
| `Near1` only (what the reference plugin does) | 5030 | **8.4 min** |
| `Near2` at 4:1 | 1258 | 2.1 min |
| `Near3` at 12:1 | 419 | 42 s |

Plus a measurement frame at every stop, and the measurement is the unreliable part: a
defocused star overflows the HFD window, so the routine is partly blind exactly where it
must search.

### 4.2 The human turns the focus ring

Stream live view with a sharpness readout. The user turns the **physical focus ring**
until the stars look sharp, then presses "good enough". Autofocus refines from there.

This is not a workaround, it is the better instrument:

- **A hand crosses the entire range in a second or two.** No pulses, no settle delay,
  no traversal budget. It beats every row of the table above by two orders of magnitude.
- **A human is a better star detector than a threshold** when the PSF is a 200 px donut.
  There is no `k_min` to get wrong.
- **It cannot fail silently.** A blind walk that mis-thresholds reports "no stars" on a
  perfectly good sky; the user just looks and turns the ring.
- **It needs no motor, no absolute position and no calibration**, so it works even if
  `DriveLensEvf` turns out to be unusable in MF (§4.5).

The cost is one step requiring attention, once, at setup. That is acceptable where a
mid-session prompt would not be.

⚠️ Use the **live-view stream** for this, not stills. Live view at 10-30 fps is what makes
the feedback feel immediate; a 1-2 s still per sample would make hunting miserable.

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

### 4.4 What this removes from the design

Because the human moves the lens by hand, **the motor's position counter stops reflecting
where focus actually is.** That invalidates several things an earlier draft of this brief
proposed, and dropping them is the point:

| Dropped | Why it is no longer needed |
|---|---|
| Homing to the far stop | Nothing needs an absolute origin |
| Per-lens / per-focal-length calibration table | A stored offset is fiction once the ring is turned by hand |
| `Start.COLD` on the critical path | There is no search to bound |
| Step-ratio calibration (`Near2`/`Near3` ratios) | Only matters for large moves; we now only make small ones |

The sweep and refine phases work on **relative** moves, with sample positions in whatever
arbitrary units the counter happens to use. Nothing in them needs to know where the stop is.

⚠️ **This retires the "manual focus is the calibration" idea from an earlier draft.** It
rested on the position counter staying valid through the manual step, which it does not. And
the companion claim — that 100 mm needs no calibration because its offset is 0 — was a
misreading: `0` in the plugin's `lenses.json` is an auto-created placeholder meaning *never
calibrated*, not a measurement (see the correction in 11 §2.2).

The homing / calibration / step-ratio code in `focus/` is kept but **demoted**: not used by
the default flow, present only for a possible future "recover focus without a human" mode.
If that never materialises, it should be deleted.

### 4.5 Resolved: the drive works in MF, which is the mode we want

Closed — confirmed by the user from experience with N.I.N.A.'s lens focusing, and consistent
with the only documentation that exists, the naming: the C# method is `DriveManualFocus` and
Nikon's equivalent is `eNkMAIDMFDrive`. These are **manual-focus** drive commands.

This is the good outcome, and it collapses a whole class of complexity:

- **The hand and the motor work in the same mode.** The user turns the ring for the coarse
  pass, the app pulses for the fine pass — no switch to flip between them.
- **No mode dance around capture either.** MF removes the "lens may refocus when a picture is
  taken" hazard instead of working around it.
- **Tier 2 is real**, so hands-free refine and drift correction are available.

The one thing to add: **verify it, do not assume it.** `EdsSendCommand` returns `EDS_ERR_OK`
when nothing moves, so a lens left on AF is indistinguishable from a lens at the end of its
travel. Pulse and observe before trusting any sweep (11 §9.1), and phrase the failure as a
user action: "set the lens switch to MF."

Build order is unchanged in spirit — hand focus first because it depends on nothing, motor
sweep second because it depends on this:

| Tier | Capability | Depends on |
|---|---|---|
| **1** | Hand focus guided by the live readout; app says when focus has drifted | nothing |
| **2** | Hands-free local sweep to refine and correct drift | `DriveLensEvf` + MF switch verified |

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

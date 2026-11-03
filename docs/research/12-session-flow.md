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
  ┌───────────┐   ┌────────┐   ┌────────────┐   ┌───────┐   ┌───────────┐   ┌───────┐   ┌─────┐
  │ PREFLIGHT │ → │ TARGET │ → │ PARAMETERS │ → │ FOCUS │ → │ CALIBRATE │ → │ POINT │ → │ RUN │
  └───────────┘   └────────┘   └────────────┘   └───────┘   └───────────┘   └───────┘   └─────┘
   tools, camera   catalog,     interval ↔        aim by       head: degrees    entry       capture,
   lens, site       tonight     usable MP,        hand, then   per turn, once   corner,     stack,
                                camera roll       autofocus    per head         solve       monitor
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
| FOCUS | CALIBRATE | The calibration solves frames, and a defocused frame will not solve (same reason as above). |

**CALIBRATE is the one step with no dependents.** It converts "move 2.1° left" into "turn the
azimuth knob about a third of a turn" (13), which is the difference between an instruction a
beginner can act on and one they cannot. But POINT works without it — it just falls back to
stating degrees. So it is offered, not required, and it is skipped automatically on later sessions
once a head profile exists. Per the README design stance: inform, never block.

---

## 1. PREFLIGHT

Already implemented as `ministar preflight`. Its job is to fail *before* the user
drives somewhere dark, not after. Reports: ASTAP + its star database, `EDSDK.dll`,
camera on the bus, lens model and focal length, disk space.

**PREFLIGHT is software readiness, and it can be run at home.** It answers "will this
machine work at all". The rig itself — where we are, and whether the camera is
configured — is SETUP (§1a). That split came from the operator's own description:
PREFLIGHT is *"I booted my computer to verify my software is ready"*, SETUP is
*"I am ready to start my shooting session"*.

**The head profile is selected in SETUP** (§1a, 13 §8) — a dropdown of named heads, defaulting to the
last one used. It belongs here because it is an equipment property like the camera and
lens, not a per-target setting. Users commonly own more than one head (a good geared one
at home, a small travel head in a rucksack), and those need separate profiles: using the
wrong one makes the instructions wrong by the ratio of the two knob rates, which could be
5×. Some heads — anything with a single lock — are recorded as `kind: "ball"` and
honestly get arrow-only guidance rather than a fabricated calibration.

The camera checklist from `preflight.CAMERA_CHECKS` belongs here as a real check
where possible: mirror lockup, LENR, mode dial. Per 08 §7.2b these are *the* cause of
past failures, and N.I.N.A. only ever surfaced them as a message box.

Non-blocking, per the README design stance, except for things that make the app
physically unable to work (no ASDK, no database).

---

## 1a. SETUP

Rig readiness, performed **at the site** at the start of a session. Where PREFLIGHT
checks the installation, SETUP checks that this evening can proceed:

* **Observing site** — latitude and longitude, entered manually or read from the
  browser's geolocation, persisted to `data/site.json`. Nothing is defaulted: a
  plausible invented latitude is indistinguishable from a correct one, and both the
  visibility columns and the reframe geometry depend on it. TARGET cannot rank
  anything until this is set, so it is a real blocker rather than advice.
* **Camera configuration** — the entries in `preflight.CAMERA_CHECKS`, confirmed on
  the body because most cannot be read over USB (08 §7.2b): mode dial on M, lens on
  MF, mirror lockup as expected, LENR off, auto power-off disabled.
* **Head profile** — optional. A dropdown of named heads (13 §8), with the option to
  calibrate a new one. POINT works without it and falls back to stating degrees, so
  this is offered, never required.

Browsers only expose geolocation in a secure context, so the *find me* button works on
`127.0.0.1` but not over a LAN address on plain http. Manual entry is therefore always
available and is the only route on the SBC until we serve over HTTPS.

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

### 4.2c The movement check is advisory (added 2026-10-28)

POINT holds the shutter while the picture is changing faster than its own noise (`Stillness`), and
vetoes a solve whose capture happened during movement. Both are heuristics about *pixels*, and a
picture changes for reasons that have nothing to do with the mount -- a starfield on a monitor
beating against the rolling shutter is the case that exposed it, at 0.9 grey levels per frame being
read as movement until the unit and threshold were fixed.

So the page carries an explicit **ignore the movement check** switch. With it on, the settle wait
and the veto are skipped; the reading is still quoted in the result, because turning a check off
should change the decision and not the evidence. The alternative -- a gate that cannot be
overridden -- makes a heuristic the authority on something the operator can see and the software
cannot.

### 4.2b When the routine fails, the hand still wins (added 2026-10-28)

The automatic stage moves the lens, so a failed run does not leave the operator where they were:
it leaves them at the routine's best guess, with their own hand-set position gone. That is the
worst possible outcome for the step whose entire premise is "a human is the better instrument".

So the end of the stage always offers three things, never one:

* **use this focus** -- record the routine's result (offered only when it actually converged);
* **use this manual focus** -- drive the lens back to the position the hand had it at before the
  run, measure *there* (averaged, same as the routine), and record that;
* **discard** -- throw the attempt away and hand the manual controls back, recording nothing.

The hand position is captured when "looks good, start focus" is pressed, and the restore happens
before the measurement, so the number stored describes where the lens actually is. Without the
first two, a failure at 3 a.m. means restarting the wizard to get back to a screen that was
working a minute ago.

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

**Updated 2026-10-28.** The marker is the centre of the *retained* rectangle, not the frame
corner: with the chosen interval the target starts at ``centre - d/2`` along the drift, so it is
centred in the usable field and ends the mirror-image distance downstream (10 §5). The roll
advice now takes the image's position angle directly when it is known (a solve, or the fake's
roll) and only adds the parallactic angle to a *clamp* angle read off the head — the old code
added it to the fake's PA and told the user to rotate 39 deg into the diagonal.

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
| CALIBRATE | 45-70 s | 4 solves + 2 knob moves, **once per head**; skipped afterwards |
| POINT | 30-60 s | solve + a human nudge + confirm |
| **Total to first frame** | **~2 min subsequent, ~4 min first time (incl. head calibration)** | |

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

## 4a. Head calibration (brief 13)

Sits between FOCUS and POINT. It exists so the reframe instruction can be expressed in the units
the user actually operates: **turns of a knob**, not degrees on the sky. The measured object is a
2×2 matrix mapping knob turns to (Δalt, Δaz) — not two scalar rates — because an unlevel tripod
couples the axes, and at 3° of tilt the coupling is ~6′, the size of the whole aiming tolerance.

See [13-head-calibration.md](13-head-calibration.md). The wizard is: solve, turn azimuth 5 turns,
solve, turn back, solve, tilt 3 turns, solve. Under a minute, once per head — and normally
**skipped entirely**, because a selected profile is confirmed by a single-move check (~15 s)
rather than re-measured. That cheap check earns its place by catching the error that actually
happens: the wrong profile is selected, because the user picked up the travel head and the
dropdown still said Manfrotto. Without it that surfaces as confusing instructions instead of a
clear message, and the residual check in 13 §7 would eventually catch it but only after the user
has been misled once.

⚠️ **Pending:** `session/state.py` has no `CALIBRATE` phase yet. Adding it is a small change
(enum member, ordering, one guard) plus tests; it is deliberately not done here so the doc can be
reviewed first.

---

## 10. Implemented so far

`src/ministar/session/state.py` — `Phase`, `SessionState`, dependency-checked
`advance()`, `blockers()`, JSON persistence that survives a corrupt or unknown-phase
file. The linear wizard is modelled; the in-loop reframe is deliberately *not* a
phase (§6).

Deliberately unimplemented: anything that needs a UI to be meaningful, and the
wizard's ordering of *prompts* rather than its ordering of *dependencies*.

**RUN (2026-10-28).** The loop is `src/ministar/run/`, and the page only starts, stops and
draws it: the runner is a process-wide singleton, so a refresh or a phone reconnecting sees
the run that is actually happening rather than a new one. Four choices worth keeping:

- **The station is suspended, not shared.** The camera pump stops while a run owns the
  camera, and captures go through the station's lock — EDSDK is never entered from two
  threads, and live view is not running through a shutter release (§02).
- **Pacing is measured from the start of each frame**, so the cadence does not drift by the
  capture's own duration; a slow frame makes the next one immediate rather than banking
  time.
- **Fake frames go to a temporary directory** and are deleted on stop. Real frames are the
  session's originals and stay in `data/frames`.
- **A bump is recovered in place.** The frame whose measured shift disagrees with the model
  is solved with ASTAP and the model rebased onto the same canvas (15), so a nudge costs one
  rejection and a fraction of a second instead of the night. The loop only asks the user to
  re-point when that solve fails or the new field barely overlaps the old one.
- **The preview is cropped to the usable rectangle and the sky is dark.** The display does
  not show the partially-covered edges (they are noisier and make the picture look soft), and
  the stretch targets a dark background so the falling noise stays visible instead of being
  re-normalised away (15).
- **The reframe is a sub-cycle inside RUN, and it reuses POINT rather than a copy.** The
  countdown is a progress bar over the interval; when it fills, the loop stops the shutter
  and the RUN page embeds the POINT renderer — live view, entry marker, solve, and the
  fake-mount controls — with the instruction naming the corner in image terms ("put the
  target on the top-left marker", 10 §5). No parameter or target re-selection.
- **It resumes by itself once the aim has *held*.** Fifteen seconds inside the tolerance of
  10 §5 (5', or 5% of the frame height), continuous rather than accumulated: a hand sweeping
  through the marker must not resume the run mid-nudge. A camera that reports where it looks
  (the fake) is judged live; a real body needs a solve taken after the reframe began, because
  one taken before describes the old aim. "continue now" overrides the wait.
- **Resuming rebases the stack onto the resume solve.** The mount has moved by most of a
  frame, so the loop is not restarted and the old frames are not discarded: the solve that
  confirmed the aim is also the one that reprojects the canvas (15). The interval, and the
  bar, start again from zero.

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

## POINT: start at the entry edge, not on the target

Aiming *at* the target spends half the frame before the session begins: the object then has
only the half-width ahead of it before it drifts out. Aiming so it starts against the edge it
entered from gives it the whole run -- for a 135 mm frame on the 60D and a target at
RA 160.27 Dec +41.27, **25.2 min aimed at the target against 50.6 min from the entry point**,
same lens, same exposure.

`pointing.best_start()` computes that pointing, and `pointing.frame_advice()` describes the
difference in mount terms ("move the mount 4.74 deg west and 10' north"). Two traps, both
found by testing at other rolls rather than at roll 0:

- **A great circle is not a parallel.** Parking the centre a fixed *RA* west of the target
  leaves the target ~10 arcmin off the middle row at 5 deg, so the entry point is solved in
  *pixels* by Newton, using the projection itself for the Jacobian.
- **The margin is a box, not a distance.** Subtracting it from the chord length looks right
  and is 2 px short on the axis at any diagonal roll -- which reads as "target NOT in frame"
  at exactly the pointing the page told the user to take.

The run available is the **chord** of the frame along the drift, `min(W/|ux|, H/|uy|)` -- not
the projection `|ux|W + |uy|H`. The two agree at roll 0 and 90 deg and disagree everywhere
else by hundreds of pixels, which is why the bug was invisible in the axis-aligned case.

**Settled: the long axis is right, and it is provably area-optimal.** The chord is longest
along the frame *diagonal* (6228 px at the corner angle against 5183 px along the long axis,
~20% more run), which briefly looked like an argument for rolling diagonally. It is not, once
the cost is counted in pixels rather than seconds: the kept fraction of a run is
`(W - D|ux|)(H - D|uy|)`, so the area lost per unit drift is `D(W|uy| + H|ux|)`, minimised at
`|uy| = 0` -- drift along the long side. For a 5184x3456 frame that is 3455 units of loss per
unit of drift against 5084 on the diagonal, so the diagonal pays **47% more pixels** for its
20% longer run. At 135 mm a 20-minute run is already short of the 60-minute ceiling, so the
extra run is worth nothing and the pixels are worth a great deal: **align with the long axis**.

---

# Field rotation: why the usable-frame number is optimistic near the pole

`optics.retained_fraction` models the run as a straight translation, which is exact for
translation and says nothing about *rotation*. The camera's footprint does not slide across
the sky -- it rotates about the celestial pole, so the sky content turns inside the frame while
it drifts (`theta = 15.0411 deg/h * t`, independent of declination).

Measured by clipping the frame against its own displaced-and-rotated copies over the interval
(`/tmp/rot.py`: build the footprint in sky coordinates, rotate about the pole, project into the
target-centred tangent plane, intersect):

| frame | dec | interval | straight-line model | measured | |
|---|---|---|---|---|---|
| 135 mm | 0 | 20 min | 48.6% | 51.1% | model pessimistic |
| 135 mm | 41 | 20 min | 61.4% | 62.2% | within a point |
| 400 mm | 0 | 10 min | 23.9% | 24.5% | within a point |
| 135 mm | 70 | 60 min | 47.3% | 45.7% | model slightly optimistic |
| 135 mm | 85 | 60 min | 86.6% | 80.4% | **6 points optimistic** |
| 135 mm | 89 | 60 min | 97.3% | 88.2% | **9 points optimistic** |

The control that proves rotation is in the measurement: at dec 89 the drift is 0.2% of a frame
per 20 min, so the straight-line model says "nothing happens" (99.1%) while the measured figure
is 95.5% -- rotation alone.

So: at mid declinations and quarter-hour intervals -- where this rig actually works -- the
closed form is right to within a percentage point, and the rectangle it draws *is* the kept
region. Near the pole it is wrong twice over: the number is optimistic, and the kept region is
an octagon (frame intersected with its own rotation), not a rectangle. The written rule:

- `|dec| < 75 deg` or interval < 30 min: straight-line model, rectangle, no caveat.
- Otherwise: say so on PARAMETERS, because "the object barely drifts" is not "the frame is
  intact" -- near the pole it turns instead.

**Implemented as `ministar/envelope.py`.** The closed form stays for intervals that turn less
than 2 deg (it is exact for translation and says what it means); beyond that the *measured*
intersection is used, so the megapixel count and the slider ceiling are right anywhere on the
sky. The drawing stays a rectangle on purpose: stacking rejects the outer edges anyway, so the
octagon is a shape the user never has to think about -- the number is what had to be honest.
The PARAMETERS caveat is gated on both conditions (|dec| >= 75 **and** interval >= 15 min),
because a five-minute run at the pole turns only 1.2 deg and needs no explanation.

## The fake sky moves, and the solver measures it

The fake mount holds an altitude and azimuth; the sky it points at is re-derived from the
clock on every access, so the field crawls westward at `15.04 cos(dec) arcsec/s` exactly as a
real tripod does. A scene rebuild costs **4 ms** against the **527 ms** render that happens
each pump tick anyway, so the drift is exact -- no interpolation, no quantising the sky into
steps. (Measured before designing around a limit that did not exist.)

Confirmed end to end, two captures ten *simulated* minutes apart, blind-solved but for the
field of view and the second hinted from the first (`data/drift_solve.py`):

```
t0   rendered RA 10.6847 h   solved RA 10.6847 h
t10  rendered RA 10.8518 h   solved RA 10.8519 h
measured dRA +2.5069 deg    expected +2.5068 deg  (= 10 min of LST)
swept 6783"                 expected 6783"       (= 15.04 cos 41.27 arcsec/s)
scale 6.7743 -> 6.7749 arcsec/px
```

Three things fall out of that, and all three are wanted:

- The clock is **injectable**, so drift can be exercised in seconds instead of waited for --
  the difference between a test and a demo.
- The second solve is hinted from the first at radius 3 deg while the field moved 2.5 deg, so
  the re-solve path gets real use rather than the easy case.
- A fixed altitude is not a fixed declination: the mount's sky position traces a curve, which
  is the drift *direction* changing slowly. That is the same geometry the entry-point advice
  and the frame advice already account for.

The switch on the page ("let the sky move") freezes the sky where it is, for reading numbers
off a screenshot; freezing never rewinds to when the mount was set.

## Hints that follow the sky, with a blind net under them

A tight hint is worth seconds (0.9 s against 4-8 s blind, 01 §8), and once the fake sky really
drifts the previous solution is no longer where the camera is looking. So the hint is
predicted, not remembered:

    centre = previous + half the drift already elapsed      (east!)
    radius = 1 deg + half the drift                         (> 20 deg: go blind instead)

Halfway, because from one solve we cannot tell whether the mount is tracking (pointing
unchanged) or not (pointing moved): the circle centred at the midpoint, of radius the half-drift
plus a margin, contains both hypotheses and is half the size of a circle that would cover them
from one end.

**The direction is a trap worth writing down.** The *stars* march west across the frame, while
the frame's own sky position advances **east** at the same rate -- `RA = LST - HA`, and a fixed
mount holds the HA. Aiming the hint at the stars' motion puts it wrong by twice the drift: at
135 mm, dec 41, half an hour apart, that is 7.5 deg instead of the right answer. Caught only by
testing the hint against what the fake mount measurably does, which is why
`test_the_hint_direction_matches_what_the_fake_sky_actually_does` exists.

Underneath sits one blind retry (`solver.solve_with_fallback`), because a bumped mount moves
unpredictably and a missed hint is indistinguishable from a bad image. Measured on ASTAP:

| step | gap | hint | result |
|---|---|---|---|
| first solve | -- | blind, FOV only | solved, 3.0" from truth |
| next solve | 30 min | RA 164.03 deg, r 3.83 | solved **without** the retry, 3.4" |
| mount bumped 6 deg | -- | RA 167.79 deg, r 1.00 | hinted **fails**, blind retry solves |
| bumped 15 / 30 deg | -- | same | hinted fails, blind retry solves |

The bump row matters twice over: it proves the retry works, and it proves the hint is a real
constraint on this build (radius 1 deg fails at 6 deg of separation) rather than a formality --
so the scaled radius is load-bearing, not decoration.

## Two failures stop the page offering a third

Each solve attempt already spends a hinted solve *and* a blind retry, so two failed presses are
four failed searches and a minute of the night. After the second, the page stops offering
"capture and solve" and offers a pulsing **retry** instead: the solver is not silent about its
limits, and continuing to press a button that has failed twice mostly teaches the user to press
it a third time.

Three deliberate details:

- **Only real solve failures count.** A refusal ("you moved during the exposure", "wait for it to
  settle", "no star database") is not the solver's fault and does not push the user towards a
  retry prompt -- otherwise fumbling in the dark would be scored against them.
- **The count survives a restart**, because a session resumed at midnight should not pretend the
  solver has not already given up twice. It is cleared by a success or by pressing retry.
- **The pulse is slow and dim** (2.4 s, low alpha, and disabled under
  `prefers-reduced-motion`): this screen is read in the dark, so a retry must be findable
  without being an event (14 §6).

The rule itself is one line in `quality.should_hold_off`, next to the other "should we spend a
solve on this?" answers, so the threshold is argued with in a test rather than tuned in a click
handler.

## POINT as a picture (`ui/overlay.py`)

The page already states everything in words -- "target 3.9 deg east", "move the mount 50' east",
"drift ahead 34 min" -- and words are precise but slow to read in the dark. The overlay draws the
same geometry onto the live view, in the frame's own pixel space:

| mark | meaning |
|---|---|
| dashed line, edge to edge | the run available: entry boundary to exit boundary, along the drift |
| soft circle + dot | the target where it is now, sized to the object |
| bright reticle | where the target must sit to start that run (the best start) |
| arrow between them | the correction, as a direction rather than a sentence |
| chevrons bottom-left/right | roll: the instructed direction bright, the other dimmed |

**The one thing that can seriously mislead is the arrow's frame of reference.** Moving the mount
east makes the sky appear to move west, so an on-screen arrow drawn in *mount* terms points
opposite to how the target circle actually travels -- the user turns the knob the arrow suggests
and the circle moves away. The arrow therefore shows where the **target must appear**, the legend
says "align the circle with the reticle", and the *text* keeps saying which way to move the mount.
Both are correct; they are just different frames, and the picture's frame is the one the user is
looking at. (In the fake rig the projection puts east to the left, so "goal x < target x" means the
circle must move east -- worth remembering when reading the tests.)

Scale is derived from the **live view's own width** (`width_arcsec / width_px`), never the still's
plate scale: a binned live view would otherwise draw every circle at the wrong size, and the
radius in pixels would be 4.93x out. Pinned by a test that halves the frame width and expects the
circle to shrink by the same factor.

Verified numerically before shipping (135 mm, Pleiades, 5184x3456): aimed at the target the
reticle lands at x=514 px, i.e. the entry margin plus the object's 443 px radius; aimed for the run
the circle and reticle are the same mark (0 px apart), which is the whole point of the picture.

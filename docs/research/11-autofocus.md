# 11 — Autofocus

**Status:** design spec, 2026-10-24. Grounded in the EDSDK primitive, the actual
`Camera-Lens-AF` plugin source, the user's own lens calibration data, and computed optics.

**Requirement (user):** as simple as possible. Slam focus to infinity, step back until
anything resembling stars appears, then hands-free autofocus. A quick focus check on every
reframe, plus monitoring of sampled frames for focus drift.

---

## 0. The short version

The design the user described is **exactly what the existing N.I.N.A. plugin does**, and it
works. We should copy the *approach*, improve the *motion planner*, and reuse the frame-quality
metric we already need for stacking.

The one thing that is genuinely hard is not the algorithm — it is that **ESDK gives no absolute
focus position**, only relative "nudge" pulses. Everything follows from that.

---

## 1. What the hardware actually gives us

**The primitive is one command:**

```
EdsSendCommand(cam, kEdsCameraCommand_DriveLensEvf, direction)
    direction ∈ { EvfDriveLens_Near1, Near2, Near3, Far1, Far2, Far3 }
```

Three magnitudes in each direction, each a **single relative pulse**. Confirmed in
`CanonFocuser.cs` (`DriveManualFocus`) and in SharpAstro's FC.SDK:

```csharp
uint error = EDSDK.EdsSendCommand(camera, EDSDK.CameraCommand_DriveLensEvf, direction);
if (error != EDSDK.EDS_ERR_OK) return false;
await Task.Delay(100);          // motor settle, then return
```

**What we do NOT get:**

- ❌ No absolute focus position read. There is no `kEdsPropID_FocusPosition`.
- ❌ No "go to position N". Only pulses.
- ❌ No documented step size — Near1/2/3 ratios are unspecified and **lens-dependent**.
- ❌ No "go to infinity". There is only "pulse Far until it stops moving".
- ⚠️ Canon added "Focus position Memory" in EDSDK 13.18.30, but that is a camera-side recall
  feature for R-series bodies, not a readable position register.

**Constraints that matter:**

| Constraint | Consequence |
|---|---|
| `DriveLensEvf` is **live-view** AF drive | Live view must be on to focus. It must be stopped for capture (and re-armed after — 02). |
| Requires an **AF-capable lens** | Manual lenses are out of scope. |
| **Works with the lens switched to MF, and that is the mode we want** ✅ | Confirmed by the user from experience with N.I.N.A.'s focusing. Consistent with the naming, which is the only documentation there is: the C# method is `DriveManualFocus`, and Nikon's underlying call is `eNkMAIDMFDrive` — *MF Drive*. These are **manual-focus** commands, not AF commands. |
| The plugin warns: *"Some lenses may try to refocus, when taking images if the lens is set to AF"* | Another reason MF is the right mode: it removes the risk entirely rather than working around it. |
| ~100 ms settle per pulse | Motion planning is the real performance problem (§8). |

**Why homing to the far stop is the right anchor.** Canon's far stop is deliberately **past
infinity**, so the lens can still reach infinity as it expands with temperature. The stop is
therefore a physically repeatable reference that is *close to* correct focus — which is why the
"hammer it to the stop and step back a little" bootstrap works at all (§3).

---

## 2. Prior art: it already exists, and it is calibrated for this exact lens

`christian-photo/Camera-Lens-AF` — the source of the `Lens AF` plugin already installed in
N.I.N.A. on this machine. `LensConfig.cs` carries an **MPL-2.0** header (the repo has no root
LICENSE, so the GitHub API reports null — check per-file headers, not the repo).

It presents the lens as a **N.I.N.A. `IFocuser`**, so N.I.N.A.'s existing autofocus and
sequencer drive it unchanged. That is the key architectural idea: *do not write an autofocus
algorithm to make a lens focus — expose the lens as a focuser.*

### 2.1 Its calibration routine, verbatim

```csharp
StartLiveView();
for (int i = 0; i < CalibrationLargeSteps; i++)
    await DriveManualFocus(EvfDriveLens_Far3, ct);   // hammer to the focus stop
await DriveManualFocus(EvfDriveLens_Near2, ct);      // one medium step back
Position = FocusStopPosition;                        // anchor the coordinate system at 0
int p = GetFocusPosition(Name);                      // per-lens, per-focal-length table
if (p > 0) await Move(p, ct);                        // jump to approximate focus
StopLiveView();
```

That is the user's description, already implemented: *throw it to infinity, step back, and if
we know roughly where focus is, go there.*

### 2.2 The user's own calibration data

`lenses.json` on this machine:

```json
{"LensName": "Canon Lens Driver (EF100-400mm f/4.5-5.6L IS II USM)",
 "FocusPosition": [{"FocalLength": 100.0, "FocusPosition": 0},
                   {"FocalLength": 400.0, "FocusPosition": 5030}]}
```

⚠️ **CORRECTION (2026-10-24).** This section originally read those numbers as *"at 100 mm the far
stop is already in focus, so zero steps back; at 400 mm it is 5030"*, and invented a depth-of-focus
explanation. **That was wrong** — the user spotted it. `LensAF.cs` settles it:

```csharp
instance.KnownLenses.Add(new LensConfig(name,
    [new FocalLengthConfig(ProfileService.ActiveProfile.TelescopeSettings.FocalLength, 0)]));
```

When a lens is first seen the plugin **auto-creates an entry with `FocusPosition = 0`**, and a new
focal length gets the same placeholder. So **`0` means "never calibrated", not "at the stop"** —
and `CalibrateCamera` only moves `if (focusPosition > 0)`, i.e. an uncalibrated lens is left
sitting at the far stop, out of focus, for N.I.N.A.'s autofocus to rescue.

**Exactly one number in that file is real: 5030 at 400 mm.** Everything else is a placeholder. The
proof is the EF100mm f/2.8 Macro entry, which carries a *400 mm* row — nonsense for a prime.

The physics never supported the claim either: the far stop is **past infinity by design**, so it is
out of focus for stars at *every* focal length. Image-space depth of focus is `2λN²`, independent
of focal length, so the same past-infinity offset produces the same blur at 100 mm and 400 mm.
What varies with focal length is only how many motor steps the mechanism needs to get there.

This also proves the primitive is fine enough: an implementation demonstrably autofocuses a
400 mm lens using nothing but 1-unit pulses. We are not fighting the hardware.

Two consequences:

1. **Zoom lenses must re-focus when the focal length changes.** Focus position is a function of
   focal length (0 vs 5030 is not a small correction). Any zoom adjustment invalidates focus.
2. **The table is per lens *and* per focal length**, so it belongs in a small user-editable
   JSON exactly like `lenses.json`, keyed by lens name and focal length.

---

## 3. What precision is actually needed (computed)

Image-space depth of focus is `±λ·N²` — **independent of focal length**:

| f/# | Airy disc | depth of focus | near limit @50 mm | near limit @400 mm |
|---|---|---|---|---|
| 2.8 | 0.88 px | 8.6 µm | 580 m | 37 km |
| 4.0 | 1.25 px | 17.6 µm | 284 m | 18 km |
| **5.6** | **1.75 px** | **34.5 µm** | 145 m | 9.3 km |
| 8.0 | 2.50 px | 70.4 µm | 71 m | 4.5 km |

Three useful conclusions:

- **The star PSF is ~1.75-1.97 px at every focal length** (diffraction-dominated; seeing
  contributes 0.11 px at 50 mm and 0.90 px at 400 mm). ⚠️ This corrects brief 03's claim that
  "at 14-85 mm the PSF is 0.1-0.5 px" — that was the *seeing* term only, and diffraction sets a
  1.75 px floor. It makes 03's conclusion (nothing for lucky imaging to sharpen) *stronger*, not
  weaker, and it means the PSF is measurable at all focal lengths.
- **Focus is not optional at any focal length.** The far stop is past infinity, so it is out of
  focus for stars everywhere. Image-space depth of focus is `±λN²`, independent of focal length
  (±34.5 µm at f/5.6), so the required *precision* is the same at every focal length; only the
  step count needed to reach focus varies, and that is a property of the lens mechanism.
- **Where precision is required, HFD is also most sensitive** — the easy case and the demanding
  case coincide, which is why a single metric is enough.

Derived estimate of step granularity at 400 mm: if the stop sits ~0.5-2 mm past infinity, then
5030 pulses map to 0.1-0.4 µm per pulse, making the in-focus window **~100-350 pulses wide**.
⚠️ The past-infinity margin is assumed, not measured — but the window is clearly wide enough for
a 1-unit step sweep, which is what the reference does.

---

## 4. The focus metric: HFD, and its two traps

Use **HFD** (half-flux diameter) — the flux-weighted mean radius × 2, median over the N brightest
*unsaturated* stars. Chosen over FWHM because it is robust to saturation, works on a ~2 px PSF,
and is cheap.

**Trap 1 — absolute HFD is not comparable across star fields.** HFD depends on which stars you
measure, so a baseline established on M81 means nothing on the Jellyfish. Never compare an
absolute HFD to a constant; compare *within a target*, or re-baseline after every reframe.

**Trap 2 — HFD must not be contaminated by the stacker's quality gate.** Roundness and star
count reject frames for *other* reasons (cloud, trail, dew). A frame rejected by the gate has an
untrustworthy HFD. Compute HFD once, use it for three purposes (quality gate, focus measure,
focus monitor), but gate the uses in the right order:

```
frame → star detect → (count, HFD, roundness, background)
      → quality gate (reject cloud/trail/dew)  ── if rejected, do NOT use HFD for focus
      → focus monitor (compare to per-target baseline)
      → stacking
```

Also: **hold the focus exposure regime constant.** HFD shifts with exposure length, ISO and sky
background, so changing them mid-run invalidates the baseline.

---

## 5. The algorithm

Five phases. Each has an explicit exit condition, because the dominant failure mode in this
feature is an unbounded loop on a cloudy night.

### P1 — Home to the far stop

```
live_view(on)
n = 0
repeat:
    pulse Far3 ; wait settle
    n += 1
until (n >= max_home_pulses) or (image stopped changing)
position := 0        # "0" = the focus stop, calibrated whenever this runs
```

⚠️ Do **not** rely on `DriveLensEvf` returning an error at the stop as the loop terminator — the
reference does, but error-at-stop behaviour is body/lens specific. Use a bounded count, and treat
"image stopped changing" as the real signal.

### P2 — Acquire stars ("anything that resembles stars")

```
if calibration exists for (lens, focal_length):
    move_to(calibrated_position)       # coarse, see §8
else:
    repeat:
        pulse Near2 ; wait
        measure star count
    until star_count >= k_min  or steps > budget
```

`k_min` small (~5-10 stars) — this is a "is there a sky at all" gate, not a quality gate. On a
cloudy night this phase *must* time out cleanly and say so, rather than grinding through the
whole focus range.

### P3 — Coarse sweep (the V-curve)

⚠️ **Amended 2026-10-28: the sweep finds the trough; it does not assume which way focus is.**
The pseudocode below starts from "stars appeared, therefore the minimum is ahead", which is only
true after P1/P2 (home to the stop, walk inward). The wizard runs **`AT_CURRENT`**, where no homing
has happened: the counter's zero is arbitrary, and whoever just turned the focus ring by hand is as
likely to have overshot as to be short. Sweeping one way from the wrong side walks *away* from
focus, and a V's far arm is monotonic, so "worsened twice" sees nothing wrong.

Reported from the field as *"it just keeps going up and never says try the other way"*. Reproduced
on a synthetic curve starting 200 units past the minimum: three samples, all further out, then
**"no reliable minimum (flat curve)"** -- and if the field dies off as it defocuses (a defocused
star is not a star to any detector) the same start produced **762 pulses** walking 8000 units in
one direction before giving up.

**What the sweep does now — a trough search, not a gradient bet.**

* **Direction comes from a shape, not a step.** A solved trough is *a run of improving samples
  followed by a rise*. If the first samples only worsen, that arm is the wrong one and the sweep
  re-anchors at the start and walks the other way. One sample of HFD is noisy (seeing, JPEG), and
  the difference across one 24-unit step is smaller than that noise at both ends of the lens range
  -- an in-focus window is ~100-350 units at 400 mm and thousands at 14 mm, so "one step" means
  something different on every lens.
* **The stride is derived from the descent.** A linear model through the last two samples predicts
  how far HFD would still fall; the next step takes a third of that, bounded below by
  `sweep_step_units` (24) and above by `sweep_step_max_units` (96, below the narrowest window we
  expect). A shoulder predicts a long run-out and gets long steps, so a wide curve is crossed in
  tens of samples rather than hundreds; near the bottom the prediction shortens and the steps
  tighten, so the trough is not jumped.
* **A floor guards the last approach.** `sweep_floor_hfd` (2.5 px) is the one quantity that needs
  no per-rig calibration: HFD is in pixels, and plate scale and diffraction trade off, so an
  in-focus star lands at ~1.5-4 px on any lens. Within 5% of it the stride drops to the base
  regardless of what the extrapolation says -- measured without it, a 96-unit step on a shallow
  descent landed 69 units past focus.
* **"Out of samples" while still falling is not an answer.** The walk extends up to
  `sweep_extend_samples` (96) and only reports a trough once it has seen the rise. A field that
  stops yielding usable samples ends the walk immediately instead (`max_rejected_run`).
* **The turn is announced**: `on_progress("turned round at N")`, and a `turned-round-at-N` phase,
  because an operator watching the position counter climb needs to know whether that is the sweep
  working or the sweep lost.

Measured on the same `AT_CURRENT` configuration, all lenses:

| case | landed | error | pulses | samples | widest step |
|---|---|---|---|---|---|
| 400 mm-like window (50 u), from 300 out | 998 of 1000 | -2 | 46 | 12 | 94 |
| 24 mm-like window (1500 u), from 3000 out | 3000 of 3000 | 0 | 258 | 76 | 96 |
| asymmetric V, started 200 past focus | 527 of 500 | +27 | 40 | 12 | 72 |
| asymmetric V, started 200 short of focus | 518 of 500 | +18 | 31 | 11 | 59 |

The wide case is the one a fixed 24-unit stride cannot do at all -- 125 samples just to arrive, and
the rise that proves a trough would not fit in the budget. On the real 60D with no stars in view,
`AT_CURRENT` now stops after **44 pulses and 4 units of net travel in 15.5 s** instead of walking
the range.

`AT_CURRENT` acquisition is likewise bounded to **+/- `acquire_local_units` (200)** in both
directions rather than one 8000-unit walk: with no anchor, distance travelled is not evidence of
anything.

```
samples = []
repeat:
    hfd = measure_hfd()                # reject if star count too low
    samples.append((position, hfd))
    pulse Near2 (or Near3, see §8)
until hfd has worsened for m consecutive samples past the best   # m = 2
```

Bracketed minimum. `m = 2` rather than 1 because a single bad sample on a marginal frame is
common.

### P3.5 — One measurement is ten frames (added 2026-10-28)

Every sample in P2-P5 is now the **median HFD of ~10 consecutive live-view frames**
(`StationDriver.frames_per_measure`), not one frame. The median rather than the mean because it is
the same cost and one bad frame -- a gust, a cloud edge, a dropped JPEG -- cannot drag the sample.

Measured on the EOS 60D at a fixed lens position, ten measurements each (a monitor starfield in
view, HFD ~4.5 px, 40 stars):

| frames averaged | mean HFD | sd | range | spread |
|---|---|---|---|---|
| 1 | 4.472 | 0.028 | 4.43-4.51 | 0.6 % |
| 3 | 4.479 | 0.011 | 4.46-4.49 | 0.2 % |
| 10 | 4.483 | 0.010 | 4.46-4.50 | 0.2 % |

The 2.8x reduction is what sqrt(10) predicts, and the cost is 1.47 s per measurement against
0.12 s for a single frame at ~8.6 fps. A 12-sample sweep becomes ~18 s rather than ~2 s. That is
the trade the operator asked for and it is worth it: a 0.6 % reading is fine near a steep shoulder
but is comparable to the *entire* depth of the curve on a short lens, where the fit has nothing to
work with. Where the noise is worse than this table -- a dim field, a long exposure-less preview --
averaging is the difference between a curve and static.

⚠️ If the frame-to-frame spread at one position is ever comparable to the curve's depth, the fit is
measuring noise. Averaging reduces the spread by sqrt(N) and no more; it does not rescue a field
whose HFD genuinely varies by 2x per frame.

### P4 — Fit and land

Fit a **hyperbola** to `(position, HFD)` — the V-curve is asymmetric, so a parabola is a poor
model on the shoulders. Rather than pure hyperbola fitting, the robust trick is to fit in
`1/HFD` space, where the curve is close to a parabola:

```
fit 1/HFD = a·p² + b·p + c
land at p* = -b / 2a
```

Then move there and re-measure.

### P5 — Refine and verify

2-3 iterations: sweep a narrow window around `p*` with 1-unit steps, refit, land. Stop when the
measured HFD at the landing position is within a few percent of the fitted minimum. **Store the
final position in the calibration table** — the next session starts from a good guess.

Exit condition for the whole routine: *either* converged, *or* a hard cap on total pulses. If it
fails, keep the best measured position rather than leaving the lens wherever it stopped.

### Why not just hill-climb

A gradient hill-climb is simpler to write but has two failure modes that matter here: it stalls
on a plateau in noise, and it cannot tell a local dip from the true minimum. The sweep+fit costs
maybe 15 extra pulses and is dramatically more robust on marginal frames. Given the settle delay
is 100 ms, those 15 pulses are 1.5 s — worth it.

---

## 6. Calibration model

Mirror `lenses.json`:

```json
{ "lens": "Canon Lens Driver (EF100-400mm f/4.5-5.6L IS II USM)",
  "vendor": "Canon",
  "focal_lengths": [ {"mm": 100.0, "stop_offset": 0},
                     {"mm": 400.0, "stop_offset": 5030} ],
  "step_ratios": {"near2_per_near1": null, "near3_per_near1": null},
  "last_used": "2026-10-24T21:00:00" }
```

`stop_offset` is in Near1 pulse units, measured from the far stop. `step_ratios` are measured at
calibration (§8) and are the thing the reference implementation does not have.

**Bootstrap UX:** the first time a lens/focal-length is seen, run the full routine and save the
result. Every subsequent session is P1 → jump → verify. That turns an 8-minute first run into a
few-second start-up.

---

## 7. Reframe check and continuous monitoring

Two distinct jobs, deliberately different costs:

**On each reframe — a check, not a hunt.** After re-aiming and re-solving:

```
measure one frame → HFD_here
if |HFD_here - baseline_for_this_target| < tol:  continue   # ~1 frame, free
else:                                            run P5 only (fine, local, ±small window)
```

Reframing does not change focus — the *lens* didn't move. What a reframe check actually catches
is temperature drift since the last check, and a *different star field* (Trap 1). So re-baseline
on each new target, and use the reframe moment as the natural place to do it.

**During capture — monitor, don't act immediately.** Every sampled frame already gets a star
detection pass for the quality gate (03/06). Reuse it:

```
if HFD > baseline * (1 + tol) for k consecutive accepted frames:   # k = 3-5
    flag focus drift → pause and run P5
```

Requiring `k` consecutive frames prevents a single marginal frame from interrupting a run.

**Also re-focus, unconditionally, when:**

- the user changes focal length (zoom) — §2.2, this is the big one for a 100-400
- the user changes aperture (some lenses shift focus on stop-down)
- altitude changes a lot (airmass affects best focus slightly)

---

## 8. Performance: the real problem is the 100 ms pulse

The reference implementation's `Move` uses **only `Near1`/`Far1`**, one pulse at a time:

```csharp
while (diff > 0 && ok) {
    ok = await DriveManualFocus(direction, ct);   // 100 ms each
    diff -= StepSize;
}
```

At 5030 units from the stop to 400 mm focus, that is **~8.4 minutes** to travel. Fine for a
once-per-session N.I.N.A. autofocus; useless for our "quick check on every reframe".

**Our improvement: mixed-radix motion.** Measure how far one Near1, Near2 and Near3 pulse actually
moves the lens, then decompose a move into the fewest pulses:

```
travel to position p (p = number of Near1-equivalents, from the stop):
    use Near3 for the bulk, Near2 for the remainder, Near1 for the last few
```

If Near3 ≈ 10× Near1 (typical for a 3-tier API, and worth measuring), 5030 units becomes
~500 Near3 pulses ≈ 50 s instead of 8.4 minutes — and the *typical* startup move is not 5030,
it is a handful of units from the previous session's stored position.

**Measure `step_ratios` at calibration** by pulsing each size N times and measuring the HFD shift,
or more cheaply by counting until the star pattern visibly changes. This is the highest-value
number the reference does not record.

⚠️ Also worth measuring: whether the `100 ms` settle is actually needed on this body, and whether
it can be shortened for small pulses. It is the single dominant cost.

---

## 9. Failure modes

| Failure | Detection | Response |
|---|---|---|
| Cloudy / no stars | `k_min` never reached in P2 | Time out, report "no stars — check sky/dust cap", do **not** leave the lens dithered at a random position |
| Frosted / dewed lens | Star count falling, HFD rising across all positions | Report dew, stop early — autofocus cannot fix this and will happily land in nonsense |
| Lens left in **AF** instead of MF | Every `DriveLensEvf` returns OK but nothing moves | **Detect by measuring**: pulse N times, confirm HFD changed; if not, report "is the lens switch on MF?" rather than a generic failure. The motion check doubles as the mode check — see §9.1 |
| Reached end of travel | Position stops changing while pulsing | Terminate, clamp position |
| Zoom changed mid-session | Focal-length property changed | Force full re-focus, invalidate baseline |
| AF re-focus on shutter | Star pattern changes unexpectedly at capture | Moot — the drive requires MF anyway, so the lens is never in AF during a run (§9.1) |
| Live view blocks capture | Frame downloads stall | Re-arm `Evf_OutputDevice` after each focus operation (02) |
| Saturated stars at high ISO | HFD artificially low | Exclude saturated stars from the median; cap focus exposure |

The "lens not moving" case is the most insidious: EDSDK returns OK, nothing happens, and the
algorithm confidently converges on noise. **Always verify motion** before trusting a sweep.

### 9.1 The motion check is also the mode check

Because the drive requires MF, "the lens did not move" has three candidate causes, and they
are **indistinguishable from the return code** — `EdsSendCommand` reports `EDS_ERR_OK` in all
of them when nothing happens:

1. the lens switch is on **AF**,
2. the lens has no motor, or the body does not support `DriveLensEvf`,
3. we are already at the end of travel.

The only reliable discriminator is **observation**: pulse N times and confirm the image
actually changed. Do this once per session, before the first sweep, and turn a failure into a
user-actionable message ("set the lens switch to MF") rather than a software error.

The dividend is that **hand and motor both work in MF**, so the coarse (by hand) and fine (by
motor) passes happen in the same mode. Nothing to flip between them, and nothing to remember
before capture — an arrangement that only works because this constraint turned out this way.

---

## 10. Validation

1. **Motion verification** — pulsing N times must change HFD measurably. This is the gate for
   every other test.
2. **Synthetic V-curve** — feed the fitter a known hyperbola plus noise; the fit must recover the
   minimum to within the step size, and must not be fooled by 20% of samples being outliers.
3. **Monotonic approach** — from either side of focus, P3 must bracket the minimum within the
   configured sample count.
4. **Stop repeatability** — home to the stop three times; the resulting "0" must be consistent to
   within the in-focus window (the whole design rests on this).
5. **Idempotence** — running the routine when already focused must make no material change.
6. **Timeout discipline** — with the lens cap on, the routine must return a clear failure within
   the pulse budget, not hang.
7. **Cross-field baseline** — two different star fields must not be compared by absolute HFD.
8. **Step-ratio calibration** — measured Near2/Near1 and Near3/Near1 must be stable across runs.

Items 1, 2, 4 and 6 need no hardware; 3, 5, 7, 8 need the camera and a clear night.

---

## 11. Open questions

- **Does `DriveLensEvf` move the lens when the lens switch is set to MF?** Determines whether we
  must toggle AF mode around focusing. The plugin's warning implies AF is required for drive and
  ~~hazardous for capture — so the workflow is probably focus in AF, run in MF.~~
  **Resolved 2026-10-24: the drive works in MF**, which is the mode the run needs anyway.
  No mode switching around focus, and no mode switching around capture. See §9.1.
- ~~Does the 60D support `DriveLensEvf` at all?~~ **Resolved 2026-10-24: yes.** User-confirmed
  from experience with this body. The highest-risk unknown in this brief is closed; the design
  stands as written. Still worth an automated motion-verification pulse on first connect (§9),
  because "the body supports it" and "this lens moves when I pulse it" are different claims and
  the second one is the one the algorithm actually depends on.
- Whether `Near3` is a single large pulse or a burst of small ones (affects the motion planner).
- Whether the settle delay can be adaptive (e.g. proportional to pulse size).
- Whether best focus drifts enough over a typical 2-3 hour session to matter. Physics says yes
  (thermal), but the magnitude on this lens is unmeasured.
- Should we expose the lens as an `IFocuser`-shaped abstraction internally, even though we have no
  external host to serve? Probably yes — it keeps the focus logic honest and reusable, and it is
  what made the reference implementation simple.

## A quality gate in front of the solver: measured constraints

Before spending a solve on a frame, ask whether the frame can be solved and, if not, *why*.
HFD is the obvious quantity and the machinery is already there -- ``io.fits.read_fits`` was
written for exactly this ("so the focus metric can be exercised against real frames") and
``focus.metrics.measure_hfd`` is the same code the focus screen trusts. Two traps, both found
by running it rather than reasoning about it:

**Cost.** ``measure_hfd`` on a full 5184x3456 frame takes **0.80 s** -- the same as a hinted
solve. On a 1200x1200 centre crop it is **0.06 s**, and 600x600 is 0.02 s. A gate that costs
what the solve costs is not a gate, so measure the middle of the frame.

**Saturation, and it bites immediately.** On fake frames exposed for the *solver* (bright star
cores clipped at 255, which ASTAP is perfectly happy with) ``measure_hfd`` returned
``(nan, 0)``: it rejects saturated stars, correctly, and every bright star in the frame was
clipped. Exposed for HFD instead (a lower flux scale), the same scene gave a median HFD of
3.12 px across 40 stars. So "cannot measure" is a legitimate outcome and must be said out loud
("the frame is saturated"), because it is itself a diagnosis -- the exposure is too long to
judge focus on, which is also why real focus routines use short exposures for HFD.

**Same pipeline or no comparison.** The same scene at the same focus gave 3.12 px on the full
frame, 2.64 px on a 1200^2 crop and 2.35 px on 600^2 -- the sample of "brightest 40 stars"
changes with the crop. HFD is also threshold/SNR-dependent. So the baseline has to be measured
by the same code on the same crop as the check, and normalised to **arcseconds** (x frame width
in arcsec / width in pixels), or a binned live view (4.93x on the 60D) gets compared to a
full-resolution still and the ratio is meaningless.

**What the 2x gate would actually catch**, given the exposure is already capped at 2 px of
trailing by design (``TRAIL_BUDGET_PX``): focus creep from temperature, mirror sag, and the
no-stars-at-all case (cloud, lens cap -- where ``measure_hfd`` returning nan with 0 stars is
already more informative than ASTAP's generic "no solution found").

**Squiggles, and why the shape test was wrong.** The obvious next thought -- compare second
moments along and across the drift, so an elongated PSF reads as motion and a round fat one as
defocus -- does not survive contact with a three-way head. Nobody turns a knob smoothly: the
mount gets bumped, so a star's path during the exposure is a *squiggle* in no particular
direction, and its second moments point nowhere useful. Motion while hand-adjusting is also not
stationary frame to frame, so there is no consistent direction to compare against.

What does separate them is **time, not shape**:

- *Moving*: the frame changes between frames faster and less predictably than drift explains.
- *Defocused*: the frame is stable and round, just fat.

So the gate belongs in two parts, answering different questions:

1. **Is the mount still?** Cheap and temporal, from what the station already publishes: the
   live view's 32x32 thumbnail delta (``Snapshot.changed``, currently keyed to "since the last
   focus pulse") or just the HFD/star count bouncing between ticks. Drift moves the field
   predictably and slowly; a hand on a knob does not. This is the gate that matters while
   pointing, and it can also say *when the user has stopped*, which is what a settle pause
   needs (11 §1: settle before capture).
2. **Is the frame sharp?** HFD at arcsecond scale, on a centre crop, against a baseline
   measured the same way. This catches focus creep, and it is worth nothing if the mount is
   still moving.

A frame taken *while* the gate says "moving" should not be submitted to the solver at all --
"you moved during the exposure" is a true and actionable statement, where "no solution found"
is not.

**Simulation status**: focus shift is exerciseable today (the fake's ``_position`` drives blur:
1.51 px at position 24, 1.73 at 32, 2.49 at 44 -- approaching a 2x gate). The motion half
needs no squiggle model at all, because the gate is temporal: jitter the fake's altitude and
azimuth between frames ("a hand on the knob") and the stillness gate has something to detect.
A true within-exposure squiggle would need a smear model, and is not needed to test the thing
we would actually ship.

### Built: `ministar/quality.py`, and what calibrating it taught

Shipped as: `hfd_arcsec()`, `focus_verdict()`, `motion_verdict()`, `centre_crop()`, and a
`Stillness` tracker fed one snapshot per tick. The POINT page shows its verdict live, waits up
to 4 s for it to say "still" before capturing, and refuses to spend a capture or a solve while
it says "moving".

⚠️ **Corrected 2026-10-28: the unit was wrong, not just the number.** The threshold was 5.0
"levels/s", calibrated on the fake. On a real 60D pointed at a *fixed* star field it reported
"mount moving" at 9.5. Measured over 59 frames: the per-frame change on the 32x32 thumbnail is
**0.9 grey levels** (whole spread 0.8-1.2) -- a quiet field -- and the preview arrives every
**102 ms**. Dividing by that interval turned 0.9 into 8.8 "levels/s". So the metric was measuring
the *preview rate* as much as the sky: noise is a property of the sensor and the JPEG, and it does
not accumulate with time. No constant could have been right, because the fake's pump and a real
body deliver frames at different intervals.

`Snapshot.motion` is now **levels between frames** (no time division), and the limit is
**relative to the field's own floor**: `max(1.5, 3 x p10 of the recent window)`. On the measured
static field that is `max(1.5, 2.7)`, so 0.9 reads "mount still (0.9 of 2.7 levels between
frames)"; a hand on the head moves whole cells, an order of magnitude beyond it. The *p10* rather
than the median matters: measured with p25, a reading that sat at 18.9 for six seconds lifted its
own limit to 18.3 and the gate went quiet while the scene was still changing.

This is the same class of error as the focus sweep's fixed stride, and the same fix: derive the
threshold from the measurement in front of you instead of carrying a number from another
environment.

**The HFD half of the gate had the same flaw.** Measured on the same static monitor starfield (72
frames, 40 stars): HFD ran 4.81-7.10 px around a median of 5.99, which is **38%** as
`(max-min)/median` and reached 73% over some 5 s windows -- enough to call a motionless mount
moving. As an interquartile spread the same data is **12%**. So `Stillness.hfd_spread` is now
IQR/median, and the 0.5 threshold has a 4x margin on a quiet field instead of a negative one.

**And a gate about the picture must not overrule the operator.** A DSLR pointed at a monitor is a
test bench with a million artefacts -- LCD refresh beating against the rolling shutter, moiré,
gamma -- and no threshold survives all of them. The POINT page now has an explicit *ignore the
movement check* switch: with it on, the settle wait and the post-capture motion veto are both
skipped, and the reading is still reported ("kept despite a movement reading: ..."). That is the
README's "inform, never block" applied to the gate itself: the operator can see the mount, and the
gate cannot.

**The motion threshold was guessed wrong by 250x, then measured.** The estimate was ~0.01 grey
levels/s (a drifting sky moves one 32x32 thumbnail cell per 96 s). Measured on the fake: noise
floor **2.6**, drifting **2.4**, a 2' nudge **2.4** -- drift and noise are *indistinguishable*,
because frame-to-frame noise dominates the thumbnail delta. A hand moving the head 5' between
frames gives **4.0-8.5**, and 20' gives 4.3-8.4. So `MOVING_LEVELS_PER_S = 5.0`, which sits
above the noise and below a real hand -- and means **a bump under a few arcminutes is
invisible**, which is acceptable because such a bump usually still solves.

**Phase correlation was the obvious better metric and it is useless here.** It would ignore noise
by looking only for *coherent* shift, but at 32x32 one cell is 162 px = **18 arcmin**, so 2', 5'
and even 20' nudges all reported a shift of exactly 0.00 cells. A finer thumbnail would be
needed, and that is a different measurement, not a free improvement.

**What made the gate work is HFD stability, not the picture delta.** A defocused frame is
*stably* soft; a frame taken while the mount moves is *erratically* soft. Measured: four samples
of a defocused frame swing 5% around their median, while a hand on the knob swings 100%+. So
`Stillness` needs both signals: the delta catches the loud cases, the swing catches the quiet
ones, and a stably-soft frame is correctly attributed to focus.

**Verified on ASTAP, fake frames, 135 mm** (flux as shipped, real star database):

| focus position | HFD (centre crop) | verdict |
|---|---|---|
| 24 (best focus) | 1.45 px = 9.9" , 24 stars | none -- and the frame **solved** |
| 40 | 2.27 px = 15.4" | none, under 2x |
| 60 | 3.15 px = 21.3" | **fires**: "focus has drifted, or the frame is smeared" |

A tempting change was to lower the fake's capture flux so its stills stop clipping (clipping is
what makes `measure_hfd` return nan on some frames). Measured first: at flux 1200 the best-focus
frame has 10 measurable stars but **ASTAP stops solving it**. So the flux stays at 6200 -- the
fake must be solvable first and measurable second, which is also the order of importance on real
hardware.

**Still needs real hardware**: the 5.0 levels/s threshold is calibrated against the fake's
synthetic noise. A real live view has JPEG artefacts, sensor noise and auto-exposure changes, so
the number will move; it is a named constant so it can be re-measured in one place.

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
| The plugin warns: *"Some lenses may try to refocus, when taking images if the lens is set to AF"* | **AF mode fights capture.** Focus in AF, then get out of the way for the run. Verify on the 60D. |
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
| Lens not in AF / MF switch on | Every `DriveLensEvf` returns OK but nothing moves | **Detect by measuring**: pulse N times, confirm HFD changed; if not, error out clearly |
| Reached end of travel | Position stops changing while pulsing | Terminate, clamp position |
| Zoom changed mid-session | Focal-length property changed | Force full re-focus, invalidate baseline |
| AF re-focus on shutter | Star pattern changes unexpectedly at capture | Keep lens out of AF during the run (§1) |
| Live view blocks capture | Frame downloads stall | Re-arm `Evf_OutputDevice` after each focus operation (02) |
| Saturated stars at high ISO | HFD artificially low | Exclude saturated stars from the median; cap focus exposure |

The "lens not moving" case is the most insidious: EDSDK returns OK, nothing happens, and the
algorithm confidently converges on noise. **Always verify motion** before trusting a sweep.

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
  hazardous for capture — so the workflow is probably *focus in AF, run in MF*.
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

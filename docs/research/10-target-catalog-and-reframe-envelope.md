# 10 — Target selection and the reframe envelope

**Status:** design spec. Geometry numbers computed on 2026-10-24 for the actual rig
(Canon 60D, 5202×3464, 4.29 µm pixels). Builds on 04 (drift model), 07 (alt/az reframing)
and 03 (trailing limits).

---

## 0. The idea

Two user-facing things that turn out to be one design problem:

1. **A catalog** so the user picks a target by name instead of typing coordinates.
2. **A "reframe envelope"** — the region of the frame the target is allowed to drift through.
   The user places the target at the *upstream* corner, capture runs while the target crosses
   the frame, and when it reaches the *downstream* corner we stop and help them put it back.

The user-facing knob is **"how often do you want to reframe?"**. The honest inverse is
**"how much of your field are you willing to give up?"**, and the app should show both,
because they are the same number.

The good news from the numbers below: the model works, translation dominates, and field
rotation — which looks like it should ruin the plan — costs only a few percent.

---

## 1. The catalog

### 1.1 Data source: OpenNGC

| Property | Value |
|---|---|
| Content | NGC + IC, **13,970 objects**, plus a 64-row addendum |
| Format | semicolon-delimited CSV, **3.8 MB** for `NGC.csv` |
| Licence | **CC-BY-SA-4.0** — README states it; the licence text lives at `LICENSES/CC-BY-SA-4.0.txt` (the repo follows the REUSE spec, so there is no root `LICENSE` file). A `LICENSES/MIT.txt` covers the scripts |
| Fields we need | `Name;Type;RA;Dec;Const;MajAx;MinAx;PosAng;B-Mag;V-Mag;SurfBr;M;Common names` |

RA/Dec are sexagesimal J2000 strings; `M` holds the Messier number where one exists, so the
Messier list needs no separate file — except **M40 and M45**, which have no NGC/IC designation
and live in `addendum.csv`.

**Licence consequences.** CC-BY-SA-4.0 is share-alike *for the data file*, not for our code.
Vendoring it means: ship the CSV unmodified (or mark modifications), include the attribution
and a copy of the licence, and do not claim it as ours. That is cheap and worth doing rather
than depending on a network API at a dark site with no signal.

### 1.2 What "visible" has to mean

A raw dump of 13,970 objects is unusable as a picker — most of the NGC is magnitude 13-15
galaxies that this rig cannot see. But per the README's design stance these are **advisory
filters, never gates**: they rank and annotate, and the user can always show everything and pick
something hopeless. What is not acceptable is *silently* excluding an object, or silently
including one that has no chance:

- **Altitude window** — above `h_min` (default ~25°) at some point during the planned session.
  This is a function of site latitude/longitude and time, i.e. the same machinery as 07.
- **Magnitude** — but *V-mag is not enough*. For extended objects, **surface brightness**
  (`SurfBr`) is the honest cut: a mag 8 galaxy with 22 mag/arcsec² surface brightness is
  invisible from a suburb while a mag 10 planetary nebula is easy. OpenNGC carries `SurfBr`.
- **Apparent size vs field** — `MajAx`/`MinAx` should be compared with the frame. An object
  10× the frame height is not a target, it is a mosaic; an object under ~20 px across is a
  dot. Both ends deserve a warning rather than silent exclusion.
- **Type** — filter by `Type` (G, GPair, GTrpl, GGroup, PN, HII, SNR, Cl+N, OCl, GCl, *).

Same filters must drive both the picker and the envelope, because a target's size determines
whether a reframe envelope even makes sense (§5).

The picker should carry a plain-language verdict rather than a pass/fail badge — "good for this
rig tonight", "will be small in frame", "too faint for this sky", "bigger than your frame, will
not fit". A beginner learns nothing from a hidden object and a lot from one labelled *this will
be a smudge*.

---

## 2. The envelope: three constraints, only one of which really binds

### 2.1 Translation — this is the one that matters

On a fixed tripod the target moves across the frame at

```
rate = Ω · cos(δ)          Ω = 15.0411 °/hr of *time* → 15.0411 arcsec/s
```

along the celestial parallel (i.e. toward celestial west). In image coordinates that direction
is set by `ψ` (07 §2.5), so it can be any screen direction depending on camera roll — but the
magnitude depends only on declination and the plate scale.

If a run accumulates a drift vector `(dx, dy)` in pixels, the region covered by **every** frame
in that run is `(W − |dx|)(H − |dy|)`. So the retained area fraction is

```
f = (1 − |dx|/W) · (1 − |dy|/H)
```

Time to reach `f = 0.80` with drift along the long axis:

| focal | ″/px | dec 0° | dec 45° | dec 80° |
|---|---|---|---|---|
| 50 mm | 17.70 | 20.4 min | 28.9 min | 117.5 min |
| 135 mm | 6.55 | 7.6 min | 10.7 min | 43.5 min |
| 200 mm | 4.42 | 5.1 min | 7.2 min | 29.4 min |
| 400 mm | 2.21 | 2.6 min | 3.6 min | 14.7 min |

Two things fall out:

- The knob is genuinely worth having: the useful range spans **2.6 minutes to 2 hours**.
- **Drift dies near the pole.** At dec 80 a 50 mm lens can run for two hours without the target
  leaving a 20% margin. Conversely, near the celestial equator at 400 mm you reframe every
  ~2.5 minutes, which is barely worth automating.

### 2.1b Usable megapixels — the metric to actually show

Percent-of-field is abstract. The user loses **megapixels**, and that is what the UI should
report. The frame is 5202×3464 = **18.02 MP**, and because the intersection of two rectangles
offset by a drift vector is itself a rectangle, the retained area is exact — not an estimate.

`usable_MP = (W − |dx|) · (H − |dy|) / 10⁶`, and it reaches **zero** once the drift exceeds the
frame in either axis. Drift along the long axis, best orientation:

| | 2 min | 5 min | 10 min | 15 min | 30 min | 60 min |
|---|---|---|---|---|---|---|
| 50 mm, dec 45° | 17.8 | 17.4 | 16.8 | 16.1 | 14.3 | 10.5 |
| 135 mm, dec 45° | 17.3 | 16.3 | 14.6 | 13.0 | 7.9 | **0.0** |
| 200 mm, dec 45° | 17.0 | 15.5 | 13.0 | 10.5 | 3.0 | **0.0** |
| 200 mm, dec 0° | 16.6 | 14.5 | 11.0 | 7.4 | **0.0** | **0.0** |
| 400 mm, dec 0° | 15.2 | 11.0 | 3.9 | **0.0** | **0.0** | **0.0** |

This is the argument for showing megapixels rather than a percentage: the curve **collapses to
zero**, and the collapse point moves by more than an order of magnitude across plausible
sessions. A user who drags "reframe every 30 min" at 200 mm on a dec-0 target sees 18.02 → 0.0 MP
and immediately understands something a "80% retained" readout would have phrased as a mild
trade-off.

**Consequence for the UI: the slider must be clamped at the zero point**, and the zero point
depends on focal length, declination *and* camera roll.

### 2.1c Camera roll is worth an instruction (measured)

The drift direction is set by `ψ`, i.e. by how the camera happens to sit in its clamp. But the
*retained area* depends on the drift direction relative to the frame's aspect ratio. Maximum
drift achievable for 80% retention, by drift angle from the frame's long axis:

| drift angle | 0° | 15° | 30° | 45° | 60° | 90° |
|---|---|---|---|---|---|---|
| max drift (px) | **1040** | 802 | 679 | 619 | 603 | 693 |

**Aligning the drift with the long axis yields ~50-70% more run time at the same retained area**
(1040 px versus 603-693 px). That is a physical action the user can take in five seconds by
rotating the camera in its clamp, and it is worth telling them:

> "Rotate the camera so the target's drift runs along the long side of the frame — you gain
> about 50% more time between adjustments at the same field."

The curve is non-monotonic (60° is worst, 90° is better than 60°) because the frame is 3:2 rather
than square. Do not assume "aligned is better" in some simple sense — use the measured table, or
just evaluate `(W − |dx|)(H − |dy|)` directly for the actual `ψ`.

#### The gain is exactly the aspect ratio, and it is a one-time action

Run-time available at a *fixed* retained fraction, relative to the long-axis optimum:

| drift angle | 0° | 15° | 30° | 45° | 60° | 75° | 90° |
|---|---|---|---|---|---|---|---|
| f = 0.90 | 1.00× | 0.75× | 0.63× | 0.58× | 0.57× | 0.59× | 0.67× |
| f = 0.80 | 1.00× | 0.77× | 0.65× | 0.60× | 0.58× | 0.60× | 0.67× |
| f = 0.50 | 1.00× | 0.83× | 0.72× | 0.66× | 0.63× | 0.63× | 0.67× |

Two exact results fall out, and they are cleaner than the tables suggest:

- **The long-axis gain is exactly `W/H`** — 1.502× for a 3:2 frame — and this holds at every
  retention fraction. Aligning with the short axis gives exactly `1/(W/H)`. The whole business is
  just the aspect ratio.
- **The penalty is worst around 60°**, at 0.57-0.63×, not at 45°.

> ⚠️ **Correction to an earlier draft of this brief.** It claimed a "7× usable field" benefit at
> 400 mm / 10 min. That figure is real but misleading: it is a *cliff-proximity artifact* — at a
> fixed 10-minute interval near the collapse point, a small change in effective drift produces a
> huge relative change in megapixels. The honest, interval-independent headline is **1.5× more
> time** (= the aspect ratio). Near the collapse point the MP difference can look much larger,
> but do not plan around that number.

**Why this is a one-time setup action, not a per-run chore.** `ψ = PA_y − q` is constant for a
camera that is not moving, so the drift direction in image coordinates is fixed for a run — and
rotating the camera changes `ψ`, hence the drift angle, **1:1**. So the whole feature is:

```
1. solve
2. compute the drift direction in image coords from psi
3. theta = angle between that direction and the frame's long axis
4. if |theta| > ~10 deg: instruct "rotate the camera by -theta"
5. let the user rotate, re-solve, confirm theta ~ 0 -> done for this target
```

Three caveats, all real:

1. **`ψ` changes when you re-aim at a different target.** Pan/tilt changes the camera's
   orientation relative to the local horizontal frame (this is the same alt-az field rotation that
   makes per-axis alt/az readout degenerate near the zenith, 07 §3), so re-check the rotation
   advisor per target rather than once per night. Between small reframes it is effectively
   constant.
2. **Rotating disturbs the pointing.** On a ball or 3-way head you loosen, rotate, retighten. Do
   the rotation adjustment *before* fine aiming, never after — otherwise the user re-centres the
   target and then wrecks it.
3. **It competes with object framing.** OpenNGC carries `MajAx`, `MinAx` and `PosAng`, so for an
   elongated target the user may want the frame's long axis *along the object's* long axis. Both
   constraints are computable; show the conflict rather than silently picking one. Round targets
   (most planetaries, clusters) have no conflict at all.

Rotation does **not** change the drift *magnitude* — only its direction in the frame. There is no
cleverer use of the roll axis than aligning with the long side; the optimiser is that simple.

### 2.1d Worked example: this rig at 400 mm

Canon 60D + 400 mm: 2.212 ″/px, field **3.20° × 2.13°**, 18.02 MP, drift along the long axis.

| interval | dec 0 | dec +22 (Jellyfish) | dec +45 | dec +69 (M81) |
|---|---|---|---|---|
| 1 min | 16.6 | 16.7 | 17.0 | 17.5 |
| 2 min | 15.2 | 15.4 | 16.0 | 17.0 |
| 3 min | 13.8 | 14.1 | 15.0 | 16.5 |
| 5 min | 11.0 | 11.5 | 13.0 | 15.5 |
| 8 min | 6.7 | 7.6 | 10.0 | 14.0 |
| 10 min | 3.9 | 5.0 | 8.0 | 13.0 |
| 12 min | 1.1 | 2.4 | 6.0 | 12.0 |
| 15 min | **0.0** | **0.0** | 3.0 | 10.5 |
| 30 min | 0.0 | 0.0 | **0.0** | 3.0 |

**The field is exhausted at 12.8 min (dec 0), 13.8 (dec +22), 18.0 (dec +45), 35.9 (dec +69).**
At 400 mm there is no such thing as a relaxed cadence below dec +60.

Orientation is worth much more here than at short focal lengths, because the drift is a larger
fraction of the frame:

| interval | dec+22, 0° | 45° | 60° |
|---|---|---|---|
| 5 min | 11.5 | 8.2 | 7.8 |
| 8 min | 7.6 | 4.1 | 3.2 |
| 10 min | 5.0 | 2.0 | 0.7 |

At 10 minutes a bad camera roll costs **7× the usable field**. Telling the user to rotate the
camera in its clamp stops being a nicety and becomes the difference between a usable sub-3°
field and a postage stamp.

**Reality check that matters more than the curve.** 400 mm on a fixed tripod allows, at 2 px of
trail:

| dec | 0 | +22 | +45 | +69 |
|---|---|---|---|---|
| max sub | 0.29 s | 0.32 s | 0.42 s | 0.83 s |

Sub-second subs mean read-noise-dominated stacking and thousands of frames per target (03:
250 × 5 s ≈ 1469 × 2 s for equal SNR, and the penalty keeps growing below 2 s). So 400 mm
untracked is an **acquisition** instrument, not an integration one: it is fine for confirming a
pointing and taking a look, and it is the wrong place to spend a night. The app should say so
rather than silently letting someone queue 4000 frames at 0.3 s.

This is the case for the catalog filters in §1.2, too: at 400 mm the honest target list is small
bright planetaries and the cores of large galaxies, not the 13,970-object catalog.

### 2.1e Inverting it: "I want N usable megapixels"

This is the form the UI should actually compute, because it is how people think. With drift
along the long axis the retained area is `(W − D)·H`, so the drift budget follows in closed form:

```
D_budget = W − MP_target · 10⁶ / H          # pixels, independent of focal length and declination
t_interval = D_budget · scale(") / (Ω · cos δ)      seconds
```

For 8 MP on the 60D: `D_budget = 5202 − 8e6/3464 = 2892.5 px` — **1.78° of sky at 400 mm**.
Everything else is just the clock:

| focal | ″/px | dec 0° | dec +22° (Jellyfish) | dec +45° | dec +69° (M81) |
|---|---|---|---|---|---|
| 50 mm | 17.70 | 56.7 min | 61.4 min | 80.2 min | 159.7 min |
| 100 mm | 8.85 | 28.4 min | 30.7 min | 40.1 min | 79.9 min |
| 200 mm | 4.42 | 14.2 min | 15.3 min | 20.1 min | 39.9 min |
| 400 mm | 2.21 | **7.1 min** | **7.7 min** | **10.0 min** | **20.0 min** |

Because the budget is a fixed number of *pixels*, the interval is linear in focal length and
inverse in `cos δ`. That is the whole dependency.

#### ⚠️ Cadence, not trailing, is the binding constraint

An earlier draft of this section claimed an invariant — `frames_per_run = D_budget / trail_px`,
**~1450 frames per adjustment for any lens at any declination** — and used it to warn that
400 mm would burn ~14,500 shutter actuations a night.

**Both were wrong**, because the invariant silently assumes the camera can be triggered at the
trailing-limited rate. It cannot. Real cadence on this rig is **one frame every ~2-3 s** once
exposure, USB download, live stacking and solving are accounted for. At 400 mm the trailing limit
is 0.29-0.42 s, so the pipeline cannot come close to feeding the camera as fast as the geometry
permits.

The governing constraint is therefore **duty cycle**:

```
t_sub  = min(trailing_limit, cadence)      # never expose longer than you can deliver
duty   = t_sub / cadence                   # fraction of wall time actually integrating
```

At a 3 s cadence with a 2 px trail allowance:

| focal | dec 0° | dec +45° | dec +69° |
|---|---|---|---|
| 50 mm | 78% | **100%** | 100% |
| 100 mm | 39% | 55% | 100% |
| 200 mm | 20% | 28% | 55% |
| 400 mm | **10%** | **14%** | 27% |

At 400 mm you collect photons for **one seventh of the session**. That, not shutter wear, is the
real cost — and it compounds with the read-noise penalty 03 already quantified for sub-second
subs.

**Sub-length rule (supersedes the framing in 03):** the optimum is not the trailing limit, it is
`min(trailing_limit, cadence)`. Where trailing permits, expose for the *full* cadence — dead time
between frames is pure loss. Shorten only when trailing forces it.

#### Pipeline cadence sets the maximum usable focal length

Equating the trailing limit to the cadence gives a hard ceiling:

```
fl_max = trail_px · 206.265 · pixel_um / (cadence · Ω · cos δ)
```

| cadence | dec 0° | dec +45° | dec +69° |
|---|---|---|---|
| 5.0 s | 24 mm | 33 mm | 66 mm |
| 3.0 s | 39 mm | 55 mm | 109 mm |
| 2.0 s | 59 mm | 83 mm | 164 mm |
| 1.5 s | 78 mm | 111 mm | 219 mm |
| 1.0 s | 118 mm | 166 mm | 328 mm |

**Halving the pipeline latency roughly doubles the usable focal length.** That makes cadence a
*product* parameter rather than an implementation detail — it decides which targets the tool can
serve at all. Worth engineering against explicitly: resident daemon rather than per-frame
subprocess (00 §8), dead-reckoning instead of solving every frame (§3), download overlapped with
stacking.

**On shutter life — retracted.** The earlier ~14,500/night figure was ~7× too high because it
rested on the broken invariant. At a 3 s cadence a 10-minute run is ~200 frames, not 1450. A
1250 s integration at 400 mm costs ~3000 actuations over 2.5 h, which is unremarkable for a 60D —
a cheap, replaceable body. The correct objection to 400 mm untracked is the 14% duty cycle, not
mechanical wear.

**Cost of a 1250 s integration at a 3 s cadence, dec +45°:**

| focal | t_sub | duty | frames | wall time | reframes |
|---|---|---|---|---|---|
| 50 mm | 3.00 s | 100% | 417 | 0.3 h | 0 |
| 100 mm | 1.66 s | 55% | 751 | 0.6 h | 1 |
| 200 mm | 0.83 s | 28% | 1502 | 1.3 h | 4 |
| 400 mm | 0.42 s | 14% | 3005 | 2.5 h | 15 |

So 400 mm is not *impossible* — it is two and a half hours and fifteen reframes for what 50 mm
delivers in twenty minutes. Per the README's design stance, that belongs on screen as a
plainly-stated cost, not as a disabled button.

### 2.2 Rotation — real, but second-order for coverage

The field rotates at `Ω · sin(δ)` about the frame centre (04 §3.1). Since registration is
similarity-based (translation + rotation), rotation between frames is *handled* — the question
is only how much coverage it costs. Measured by rasterising the actual frame and intersecting
it with its own rotation:

| rotation | retained | reached at dec 45° | at dec 80° |
|---|---|---|---|
| 1° | 99.1% | 5.6 min | 4.1 min |
| 2° | 98.2% | 11.3 min | 8.1 min |
| 5° | 95.6% | 28.2 min | 20.3 min |
| 10° | 91.9% | 56.4 min | 40.5 min |
| 20° | 86.1% | 112.8 min | 81.0 min |

So rotation costs **4-8%** over a session — comparable to a small increase in the translation
margin, and nothing like the disaster it would be if we could not derotate.

Checked further out, because a long session rotates a lot: at ±60° the frame still retains
**74.9% (13.5 MP)**. Even a full hour of rotation at dec 45° (60°) therefore costs less than one
bad slider position on the translation curve. Rotation is a footnote, not a second mechanism.

⚠️ This assumes the stack reference is chosen near the middle of the session rather than at the
start, so the rotation is bounded by ±(half-session) instead of the full session. That is free
to arrange — 03 already re-anchors the reference every 2-5 min.

⚠️ This assumes the stack reference is periodically re-anchored (which 03 already requires, for
projection reasons, every 2-5 min). Anchoring forever to the *first* frame would accumulate the
full session rotation; re-anchoring bounds it.

### 2.3 Trailing within a single sub — already covered

Corner smear from rotation *within one exposure*:

```
dec 45°:  1s 0.16px   2s 0.32px   5s 0.81px  15s 2.42px  30s 4.83px
dec 80°:  1s 0.22px   2s 0.45px   5s 1.12px  15s 3.37px  30s 6.73px
```

At our 0.4-3 s subs this is **sub-pixel** — a non-issue. It becomes visible (2-7 px at the
corners) at 15-30 s, which is another argument for the short-sub approach 03 already settled on.

### 2.4 Synthesis

**The user's box model is correct** in the regime where it is used. Translation governs; rotation
adds a small, quantifiable crop. Implement the box, and fold rotation in as an extra margin
rather than as a second mechanism — **but the fold is mandatory, not optional**, because there is
a declination band where it is the only term that matters (§2.5).

---

### 2.5 Where the model breaks — measured

Built the geometry from first principles (fixed sky→pixel map in the local horizontal frame,
§3.2) and measured the whole thing rather than reasoning about it. Three results.

#### (a) The meridian is not special

The drift direction on screen rotates at **`ω·sin δ`**, uniformly, and **independent of hour
angle**:

| δ | 0° | 30° | 45° | 60° | 75° | 85° | 89° |
|---|---|---|---|---|---|---|---|
| measured | 0.00 | 7.46 | 10.58 | 12.99 | 14.52 | 14.98 | 15.04 °/hr |
| `ω·sin δ` | 0.00 | 7.52 | 10.64 | 13.03 | 14.53 | 14.98 | 15.04 |

Measured at the meridian and at 6 h hour angle: **identical to three decimals**. So there is no
direction flip and no cusp at transit.

⚠️ **Correction.** This brief previously warned that the drift direction "reverses after transit
(the parallactic angle flips through 0)" and told the UI to re-derive the corner instruction
specially. **That was wrong** — it is the behaviour of a *tracked* mount, where `q` genuinely
changes. A bolted-down camera has no such feature. The §7 row has been corrected.

#### (b) The pole is where it actually breaks, and quietly

Near the pole translation collapses while rotation stays at its maximum — they are exactly
complementary (`cos δ` versus `sin δ`):

| δ = 89.9° | 5 min | 10 min | 20 min | 40 min | 60 min | 120 min |
|---|---|---|---|---|---|---|
| drift | 4 px | 7 px | 14 px | 28 px | 43 px | 84 px |
| predicted MP (translation only) | 18.01 | 17.99 | 17.97 | 17.91 | 17.84 | 17.62 |
| actual MP | 17.80 | 17.59 | 17.21 | 16.54 | 15.96 | 14.61 |
| **error** | −1.2% | −2.2% | −4.2% | −7.7% | **−10.6%** | **−17.1%** |

The failure is specific and bad: the target barely moves, so the envelope computes a huge
interval and the UI says *"reframe in hours, 18 MP retained"* — while the field is eroding at
about 10% per hour and nothing alerts the user. That is exactly the "silently worse than
promised" failure the README's design stance forbids.

**Fix:** `usable_MP = translation_factor × rotation_factor`, and the interval is
`min(translation_limited, rotation_limited)`. Rotation-limited interval =
`θ_max / (ω·sin δ)`, which for a 90%-retention `θ_max ≈ 9.5°` is **~54 min at δ=45°** and
**~40 min at δ=70°** — flat and short, because `sin δ` saturates.

#### (c) The binding constraint switches with declination

Table for 8 MP retained and `θ_max = 9.5°`:

| focal | crossover δ | at δ=70°: translation vs rotation |
|---|---|---|
| 50 mm | 33.7° | 166 min vs 40 min |
| 100 mm | 53.2° | 83 min vs 40 min |
| 200 mm | 69.5° | 41 min vs 40 min |
| 400 mm | 79.4° | 21 min vs 40 min |

**Above those declinations rotation binds and translation is irrelevant.** At 200 mm that is
δ > 69.5° — which includes M81, M82, NGC 2403 and a good part of the popular northern sky. So
this is not an edge case; it is a routine band of targets, and it needs the rotation term to be
implemented rather than noted.

Mid-latitude, where translation binds, the translation-only model is accurate to **better than
1.5% for runs up to 20 minutes** (measured at 200 mm, δ=45°). So no correction is needed there.

#### (d) Practical consequence for the box

The direction rotates at up to 15°/hr, so a box drawn at `t₀` is misaligned by `ω·sin δ·t`.
At δ=60° that is 4.3° over a 20-minute run — small against a box several degrees wide, but it is
another reason to recompute the box from a fresh solve rather than caching it.

---

## 3. The path is exactly computable — do not simulate it

The target's track across the frame needs no integrator. But **the obvious formulation is
wrong**, and it fails silently, so this section is written to be hard to get wrong.

### 3.1 The wrong way (and why it looks right)

An earlier draft said: `ψ = PA_y − q` is constant for a fixed camera, therefore
`PA_y(t) = ψ + q_c(t)`, therefore build a WCS at `t` from the camera's current RA/Dec and that
`PA_y`. Rebuilt numerically, **that recipe produces a camera with zero field rotation** — a
sky-tracking camera, not a bolted-down one — while still showing the correct translation. It is
wrong in a way that looks plausible, because the translation terms are right and only the
rotation is missing.

The trap: `q` evaluated at a *fixed local direction* is constant (the pole, the zenith and the
camera axis are all fixed in the local frame, so the angle between the hour circle and the
vertical there does not change). It is easy to conclude "constant `PA_y`, no rotation". The
rotation is real anyway — it is a **differential** property of the projection as the sky moves
through the tangent point, not a change in the centre's orientation.

### 3.2 The right way: build the map in the local frame

The camera is bolted to the ground, so the sky→pixel map is **fixed in the local horizontal
frame**. That is the whole model:

```
1. camera axis alt/az (A_c, h_c) — constant.  Roll φ between the image axes and the
   local (right, up) tangent basis — also constant.
2. build ONCE:  u_c = unit(A_c, h_c)
                up = normalize(zenith − (zenith·u_c)·u_c)
                right = cross(up, u_c)
                e_x, e_y = rotate (right, up) by φ          # image axes
3. for any sky point at time t:
       (A, h) = radec→altaz(α, δ, LST(t))
       u = unit(A, h);  den = u·u_c
       w = u − den·u_c
       pixel = (W/2 + (w·e_x)/den · 180/π / scale,
                H/2 + (w·e_y)/den · 180/π / scale)
```

No `ψ`, no parallactic angle, no per-epoch WCS. Verified against the physics: it reproduces
field rotation at **ω·sin δ** to within 0.5% (10.58°/hr measured at δ=45° against 10.64°, and
15.04°/hr at the pole), and translation at **ω·cos δ ±** as expected.

⚠️ **Units.** `(w·e)/den` is in **radians**. Dividing it by a plate scale in degrees/pixel was
worth a 57.3× error in the first version of this experiment — the frames were fine and every
distance was wrong. Convert explicitly and write a test that checks a known separation.

Sample every ~30 s for the drawn path.

**Optimisation worth having:** dead-reckon between solves. Solve once a minute (0.8 s each) and
use this model to compute the target's pixel position on every frame. The stop-capture trigger
then costs nothing.

---

## 4. The user-facing knob

One slider, three readouts — leading with the thing the user is actually spending:

```
  reframe every  [ 8 min ]   ->  16.4 usable MP  of 18.0
                                 target travels 1.6° of 4.3°
                                 drift runs along the long axis  [good]
```

- **Usable MP vs total MP is the headline**, per §2.1b. It is concrete, it is the real currency,
  and it makes the collapse-to-zero visible instead of abstract.
- Show the *distance* as well as the time, because "1.6° of 4.3°" is intuitive in a way a
  percentage is not.
- Show the orientation verdict, and offer the rotate-the-camera advice from §2.1c when the
  measured `ψ` puts the drift in a bad sector (roughly 30-75° from the long axis).
- **Render the whole curve, not just the current point.** A small `usable MP` vs `interval` plot
  behind the slider says more than any single number: the user can see the knee before they
  drag past it. Default position: the largest interval that still retains 85% of peak, clamped
  to `[3 min, 45 min]` — faster than 3 min is not worth a human's attention, and past ~45 min
  the translation curve has usually already collapsed.
- If the interval the user wants is past the zero point, clamp and say why: "30 min is not
  available at 200 mm on this target — the field is empty by 22 min."

**Envelope sizing.** Two modes, and only two:

1. **Auto** — the app picks the box from the interval and the target's declination. This is the
   default and the one most users should keep.
2. **Manual** — the user drags box edges. Needed because some people want a specific crop, and
   because a target may be part of a mosaic.

**The box is a rotated rectangle, aligned to the drift vector.** Decided 2026-10-24.

The long axis of the box lies along the drift direction, so the corridor points exactly where the
target will go; its short axis is the transverse margin. This makes the entry and exit corners
unambiguous, makes the drawn path run corner-to-corner through the box, and removes the awkward
case where a diagonal drift clips the corners of an axis-aligned box in a way the user cannot
read off the screen.

Two implementation notes:

- The **retained-area arithmetic in §2.1b does not depend on the box orientation** — it depends
  only on the total drift vector relative to the *frame* axes, because the fully-covered region
  is the intersection of the frame with itself offset. The rotation is a UX device, not an
  optimisation. Worth stating plainly so nobody later "optimises" the box angle expecting more
  megapixels.
- The box must still be **clipped to the frame**, which for a rotated rectangle means clipping a
  convex quad against a rectangle — four line intersections, not a bounding-box clamp. A convex
  clip is ~20 lines and avoids a whole class of drawing bugs.

---

## 5. Entry and exit corners

The subtlety that must not be fudged: **the drift direction is fixed by physics** (celestial
west). So "put it in the opposite corner" only works if the *opposite* corner is the **upstream**
one. Which physical corner is upstream depends on `ψ`, i.e. on how the user happened to bolt the
camera on — and it can change during the night as `q_c(t)` evolves.

So:

- The app names the corner in **image terms** — "top-left of the frame" — never in sky terms
  ("east"), because the user is looking at a screen and the mapping is not obvious.
- At the end of a run the target is at the downstream corner. The instruction is
  "re-aim so the target sits at the **top-left** marker", and the live view shows the marker.
- After the nudge, **re-solve before resuming**. `ψ` changes slightly (the pan changed the
  pointing, hence `q_c`), so the path and the proposed box must be recomputed. Do not carry the
  pre-reframe geometry across the reframe.
- If the target is large relative to the frame (an HII region wider than the envelope), a corner-
  to-corner run is pointless: the object is bigger than the retained region. Fall back to
  "keep it centred, reframe often" and say so.

---

## 6. The visual guide

What the user sees over the live image:

```
┌────────────────────────────────────────────┐  frame rectangle (current solve)
│   ╔══════════════════════════════════╗     │  envelope box
│   ║ ●──────────────────────────────→  ║     │  path: solid = elapsed, dashed = predicted
│   ║ │  t0                        ●     ║     │  ● = now
│   ║ │                                │ ║     │  ◦ = predicted exit, with time label
│   ║ └─────────── 4m12s left ─────────┘ ║     │
│   ╚══════════════════════════════════╝     │
│                              ░░▒▒▓▓        │  coverage: fewer frames toward the edge
└────────────────────────────────────────────┘
```

Elements, in priority order:

1. **The path** — solid behind the current position, dashed ahead, with time ticks every minute.
   This is the feature's whole value: the user can *see* where the target is going.
2. **The "now" marker** and **predicted time to exit the box**, because that is the countdown
   that decides when they get to go back inside.
3. **The entry marker** — where to put the target after the next reframe.
4. **A coverage heatmap** near the edges, so the cost of a long run is visible before they run it,
   not after. This can be a coarse 64×64 grid; it is guidance, not photometry.
5. Rotation-degraded corners, if the session is long enough for it to matter (§2.2).

The path should be drawn in the **current frame's pixel coordinates**, recomputed on each solve,
so it stays correct as the user nudges.

---

## 7. Edge cases

| Case | Behaviour |
|---|---|
| Target near the meridian, dec < latitude | Drift is fastest and nearly horizontal on screen. Shortest runs; expect reframes every few minutes. |
| Target transits | **Nothing special — corrected 2026-10-24.** The drift direction rotates at a uniform `ω·sin δ`, verified identical at the meridian and 6 h away. There is no flip and no cusp for a bolted-down camera; that folklore belongs to a *tracked* mount, where the parallactic angle changes. See §2.6. |
| Target near the pole (δ ≳ 80°) | **This is the real breakage.** Translation collapses (43 px/hr at δ=89.9°) while rotation stays at its maximum 15°/hr, so the envelope says "reframe in hours" while the retained field quietly erodes at ~10%/hr. See §2.6. |
| Pointing near the zenith | Per-axis alt/az readout is suppressed (07 §3). The envelope still works — it is pure pixel geometry, which is an argument for keeping it separate from the alt/az layer. |
| Target's drift exits the frame before the interval expires | Contradiction between interval and box. Clamp: shrink the interval and tell the user. |
| Target sets mid-session | Compute the altitude curve; warn at planning time and again when `h < h_min`. |
| Long focal length, dec 0 | 2.6-minute runs mean the reframe overhead dominates. Say so plainly — "expect a rough image: ~1000 frames and ~15 reframes for what 100 mm delivers in 20 minutes" — and then get out of the way. Choosing a bad night is allowed (README design stance). |

---

## 8. Validation

Testable with no hardware, like 07:

1. **Path closure** — the modelled path at `t = 0` equals the solved pixel position of the
   target. Any mismatch means `ψ` or the WCS-at-time-t construction is wrong.
2. **Straight-line limit** — at `δ ≈ 0` and short durations the path must be straight to within
   a pixel (rotation is ~0 there), and must have slope matching `ψ`.
3. **Rate check** — `|d(path)/dt|` must equal `Ω·cos(δ)/scale` in px/s, independent of latitude.
   This is the same invariant 04 verified; reuse its synthetic-WCS harness.
4. **Rotation check** — the frame's `PA_y` must advance at `Ω·sin(δ)`, matching 04 §3.1.
5. **Coverage** — the retained fraction from the actual stacking pipeline (brief 06's coverage
   map) must match `(1−dx/W)(1−dy/H)` for a synthetic drift.
6. **Envelope arithmetic** — for a given interval, the box must contain the whole path from
   entry to exit, and the exit time must match the drawn "time left".
7. **Megapixel curve** — `usable_MP(t)` must equal `(W − |dx|)(H − |dy|)/10⁶` for the drift
   vector at `t`, must reach exactly zero at the documented crossing, and must be monotonic
   decreasing in `t` for fixed `ψ`. The non-monotonicity in §2.1c is in the *orientation* axis,
   not the time axis — if the time axis is ever non-monotonic, something is wrong.
8. **Rotated box clip** — a box at 45° must produce a 4-vertex quad clipped correctly against the
   frame, with no pixels of the box drawn outside the frame and no frame pixels wrongly masked.
7. **Transit flip** — a synthetic target crossing the meridian must produce the corner
   instruction flipping, not the target being chased off-screen.

---

## 9. Open questions

- Rotated box: **decided, aligned to the drift vector** (§4). Remaining question is only whether
  the box should ever be *tilted relative to the drift* to dodge a bright star or a frame
  artefact — probably not, and offering it invites the user to break the arithmetic.
- Whether "usable megapixels" should be shown as **fully-covered area** (the §2.1b formula) or
  **area weighted by frame count** from the coverage map. The former is exact and simple; the
  latter is more honest for the partially-covered edges and closer to what the final stack will
  actually deliver. Lean toward showing fully-covered MP on the slider and the weighted number
  after the session.
- Whether the picker should default to **sorted-by-expected-quality** rather than
  filtered. Same information as a curated subset, but nothing is hidden: the honest answer for a
  beginner is "you have a 100-400 mm lens on a tripod; here is everything visible tonight, and
  the top thirty are the ones that will actually look like something." Sorting respects the
design stance; excluding does not.
- Whether the coverage heatmap should drive an **automatic crop** for the final stack, or just
  inform the user. Auto-crop risks throwing away good data at the edges.
- Attribution mechanics: the repo is REUSE-compliant, with a `.reuse/dep5` and per-file
  licence headers. We should mirror that structure when vendoring rather than dropping in a
  bare CSV, so the provenance stays machine-checkable.
- Does anything here need to handle a **tracked** session (the user's own `north-america` frames
  were 180 s at 100 mm, so tracking exists in their workflow)? The envelope model assumes a
  fixed camera; a tracked session makes drift ≈ 0 and the whole feature collapses to "pick a
  target". Keep the model untracked-only and treat tracking as out of scope.

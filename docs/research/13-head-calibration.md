# 13 — Head calibration: turning degrees into turns

**Status:** design, 2026-10-24. The 2×2 arithmetic is verified numerically (§2, §6). Not
implemented; the session state machine needs a `CALIBRATE` phase (see 12 §4a).

**The idea (user):** every reframe currently says *"move 2.1° up, 0.8° left"*, which is
meaningless to someone holding a 3-way head. Calibrate the head once — solve, ask the user to turn
azimuth by 5 full turns, solve again — and from then on the instruction is *"turn the altitude knob
about a quarter turn, the azimuth knob a third"*. This is plausibly the feature that makes the
whole tool usable by someone who has never done astrophotography.

---

## 1. Why this matters more than it sounds

Brief 07 establishes that the corrections we ask for are **arcminutes to a few degrees**, and that
human repeatability on a 3-way head is 2-5′ (geared) to 20-60′ (ball). So the user is being asked
to make sub-degree adjustments by eye, with no feedback, on a mechanism whose scale they don't
know. That is the single most frustrating part of the whole workflow, and it is pure UI friction —
the information to fix it is already there.

A calibrated knob turns a judgement call into an arithmetic one. That is the difference between a
tool a beginner fights and one they trust.

---

## 2. Measure a 2×2, not two rates

The natural reading of the idea is to measure two scalars: degrees-per-turn for azimuth and for
altitude. **Measure a 2×2 matrix instead** — it costs the user nothing extra (the same two moves
yield a richer measurement) and it is required if the tripod is not perfectly level.

Computed: with the pan axis tilted **3°** off vertical (an ordinary eyeballed setup), panning 36°
changes the pointing's altitude by **−1.77°**:

| pan-axis tilt | pan 36° → Δalt | → Δaz | tilt 36° → Δalt | → Δaz |
|---|---|---|---|---|
| 0° (level) | 0.00° | −36.00° | −36.00° | 0.00° |
| 1° | −0.59° | −35.84° | −35.99° | −0.57° |
| **3°** | **−1.77°** | −35.49° | −35.95° | **−1.72°** |
| 5° | −2.94° | −35.13° | −35.85° | −2.86° |

For a 2° azimuth correction on a 3°-tilted tripod, the diagonal-only model leaves **~6′ of
unwanted altitude error** — the same size as the whole aiming tolerance from 07 §4. So the
cross-terms are not pedantry; they are the dominant error at exactly the scale we care about.

And with a level tripod the cross-terms go to exactly zero, so the 2×2 degrades gracefully to the
diagonal case. There is no cost to using the general form.

```
[ Δalt ]   [ j_h,az   j_h,alt ]   [ turns_az  ]
[ Δaz  ] = [ j_A,az   j_A,alt ] · [ turns_alt ]

turns = J⁻¹ · (Δalt, Δaz)
```

Verified: for a 3°-tilted setup, wanting Δalt=+0.8°, Δaz=−2.1° gives
`J = [[−0.353, −7.189], [−7.099, −0.344]]` alt-az-per-turn, `det = −50.91`, and back-substituting
returns the requested offsets exactly.

⚠️ **`J` is in *azimuth degrees*, not on-sky degrees.** One azimuth turn moves the pointing `j_A,az`
degrees of azimuth regardless of altitude, but only `j_A,az·cos(h)` of *sky*. That `cos h` is
already inside the matrix because the calibration measures the actual pointing change — do not
apply it twice. This is the same trap as 07 §2.4, in a new place.

---

## 3. The protocol

Two moves, four solves, at a pointing with room to move in both axes:

```
0. focus first (a defocused frame will not solve -- 12 §4), and level the tripod as well as
   you can be bothered to. Point at roughly alt 45-50 deg.
1. SOLVE A                         -> (alt, az) baseline
2. turn the AZIMUTH knob +5 turns  -> SOLVE B        -> column 1 of J
3. turn the AZIMUTH knob -5 turns  -> SOLVE C        -> backlash estimate (§6)
4. turn the ALTITUDE knob  3 turns -> SOLVE D        -> column 2 of J
```

### Why 5 turns for azimuth, and only 3 for altitude

**Five turns is right, and for a better reason than averaging hand error.** Backlash is an additive
offset; measuring the rate over `N` turns divides its influence by `N`. With a typical 0.1-turn of
slack, a 1-turn baseline gives a 10% rate error while a 5-turn baseline gives **2%**. That is the
real justification, and the user's instinct to use many turns was correct.

Altitude gets fewer because of geometry, not accuracy:

- the head has ~90° of travel, so 5 turns ≈ 36° leaves little room;
- tilting *up* 36° from alt 45 lands near the zenith, where the alt/az readout is ill-conditioned
  (07 §3);
- tilting *down* 36° lands at alt 9°, in heavy refraction and possible obstruction.

**3 turns ≈ 21°** keeps both ends comfortable. The rate error rises to ~3%, still 10× better than
the tolerance needs.

### Why the solve is the expensive part

At 7.2°/turn (a geared 3-way head — 04 §2.1 cites this for a Manfrotto 410), 5 turns moves the
pointing **36°**. The second solve cannot use a tight hint: allow `-r 45`, or fall back to blind.
Budget a few seconds per solve; the whole routine is under a minute, once per head.

⚠️ Check the head has room *before* starting — some heads have a limited pan range, and hitting a
stop mid-move silently corrupts the calibration.

---

## 4. What the instruction looks like

At 7.2°/turn:

| correction | turns | knob rotation |
|---|---|---|
| 0.5° | 0.07 | 25° |
| 1.0° | 0.14 | 50° |
| 2.0° | 0.28 | 100° |
| 5.0° | 0.69 | ¼ turn short of full |
| 15° | 2.08 | two full turns |
| 40° | 5.56 | five and a half |

So the *useful* range for fine reframing is **0.1-0.3 turns** — a few tens of degrees of knob
rotation. §6 is about why that is a problem, and what to do about it.

**Precision is not the issue.** A 36° calibration baseline with ~3″ of solve noise gives a rate
error of 0.002%, which over a 2° correction is **0.17″**. Three orders of magnitude below the
tolerance. The measurement is easy; the *mechanism* is the hard part.

### Wording

- **Never say "clockwise" without saying which knob.** On a 3-way head the two knobs' clockwise
  directions move the camera in opposite senses, and which is which depends on the head.
  The calibration determines the signs; print "azimuth knob, clockwise" /
  "altitude knob, anticlockwise" using the measured sign.
- Prefer the knob's *name* as moulded on the head, and an icon. "Left/right" is worse than
  "azimuth".
- Round to something a human can do: "about a quarter turn" beats "0.28 turns". Give the number
  too, for people who count.

---

## 5. When *not* to give a turn instruction

| Condition | Instead |
|---|---|
| correction > ~2 turns (≈15°) | "Loosen and move by hand — roughly 20° north-east, then fine-tune." Counting turns for a coarse move is slower and less accurate than hand-aiming. |
| near the zenith (`h > 80°`) | A 1° on-sky move needs `1/cos h` = 5.8° of azimuth = 0.8 turns, rising without bound as `h → 90°`. Say "use the arrow, the knob will overshoot here" (07 §3 already suppresses the per-axis readout above 87°). |
| correction < backlash | "Smaller than the head's slack — nudge it and re-solve, or leave it if you're inside tolerance." |
| no calibration yet | Fall back to degrees. The feature is an enhancement, not a prerequisite (README: inform, never block). |

---

## 6. Backlash is the real accuracy limit

This is the part the idea does not solve on its own, and it matters because the useful corrections
are tiny.

If the head has ~0.1 turn of slack (0.72° at 7.2°/turn) and the aiming tolerance is ~5′ (0.08°),
then **a single knob move can land anywhere within 0.1 turn of where you meant** — 9× the
tolerance. A naive implementation would therefore *feel broken*: the user turns as instructed and
the error barely moves or overshoots randomly.

Three mitigations, in order of value:

1. **Re-solve and iterate.** After the move, solve again and issue a corrected instruction. Two or
   three iterations converge regardless of slack, and we are re-solving anyway to check the
   envelope. This alone makes the feature work; it is also the honest answer — do not pretend one
   move is exact.
2. **Approach unidirectionally.** Slack is taken up once. If consecutive moves are in the same
   direction, motion is immediate and accurate; only a *reversal* costs the slack. Track the last
   direction and add the backlash only when reversing.
3. **Measure the backlash** (step 3 of the protocol, one extra solve): turn +5 then −5; any
   residual displacement along the pan direction is the slack. Report it, use it in (2), and warn
   the user if it exceeds the tolerance — some heads genuinely cannot be aimed precisely, and
   saying so is more useful than failing quietly.

---

## 7. Validation and self-correction

The calibration validates itself in normal use, with no extra work:

```
predicted = J · turns_user_actually_reported
actual    = solve_after - solve_before
if |predicted - actual| > 20%:      # not noise -- a wrong axis count, a slipped knob, a bump
    flag "head calibration may be off; re-run it?"
```

That catches an unlevel tripod, a mis-set knob, a user who turned four turns instead of five, or a
head that has been swapped. It also means a bad calibration is discovered on the first reframe
rather than silently degrading a whole session — the same principle as 07 §7's assertion that the
two offset routes must agree.

⚠️ Do not silently *re-fit* `J` from use, tempting as it is: the residuals are dominated by
backlash and hand error rather than any real change in the head, so online fitting would drift
away from truth.

---

## 8. Storage

One profile per head, in the same JSON style as the lens calibration:

```json
{ "head": "Manfrotto 410 geared",
  "j": [[-0.353, -7.189], [-7.099, -0.344]],
  "backlash_turns": 0.08,
  "calibrated": "2026-10-24T21:14:00",
  "tripod_level_deg": 3.0 }
```

`tripod_level_deg` is not needed for the arithmetic — it is inferred from the cross-terms — but it
is worth surfacing: **"your tripod is 3° off level"** is actionable, it explains the cross-terms,
and levelling would remove them.

---

## 9. Edge cases

| Case | Behaviour |
|---|---|
| Head has a stop within 5 turns | Detect: the second solve's displacement is far off the expected magnitude → abort and suggest fewer turns |
| Ball head with no separate axes | The 2×2 still works (two independent knobs), but the axes are not pan/tilt; if the user has only one lock, calibration is meaningless → skip and fall back to the arrow |
| User swaps heads | Keyed by profile name; `tripod_level_deg` and `J` both change |
| Zoom/focus changed since calibration | Irrelevant — `J` is about the head, not the optics. Only the *pixel* scale depends on focal length, and that comes from the solve |
| Target changes declination drastically | `J` unaffected (it is a head property). The `cos h` effect on needed turns is handled inside the solve at use time |
| Very fine correction, < 0.05 turn | Below what a hand can do. Say "within tolerance" if it is, else iterate |

---

## 10. Open questions

- **What is a real 3-way head's degrees-per-turn?** We are using 7.2°/turn from 04, cited for a
  Manfrotto 410 but not measured on the user's head. **Measure it** — the whole instruction
  vocabulary depends on the value, and a head with 1°/turn changes the UI thresholds completely.
- Is 0.1 turn a realistic backlash figure for a geared head? Unknown and worth measuring.
- Should the wizard offer a *practice* reframe after calibration, so the user sees the loop work
  once before it matters? Probably yes — it is the cheapest possible confidence builder.
- Whether to calibrate at two altitudes to detect head non-linearity (geared heads can be
  non-linear near their extremes). Probably unnecessary for v1.
- The `cos h` blow-up near the zenith might argue for calibrating in *sky* coordinates as well and
  interpolating — but that reintroduces the `ψ` problem, and the matrix form is cleaner.

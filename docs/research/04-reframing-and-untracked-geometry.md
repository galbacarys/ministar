# 04 — Reframe assistance and untracked-capture geometry

**Scope.** A tool with one camera on a 3-way (pan / front-tilt / lateral-tilt) tripod head and a
laptop. ASTAP plate-solves each frame and returns a WCS (`CRVAL1/2`, `CRPIX1/2`, `CD1_1..CD2_2`,
optionally SIP). The tool must tell a human which way to nudge the head, (a) to acquire a target
and (b) to re-center after drift.

**Status of this document.** Formulas, derivations and numbers below were verified numerically in a
throwaway scratch harness (vector spherical trig + gnomonic projection + synthetic CD matrices);
the verification results are quoted inline. Claims that could not be checked are marked
**UNVERIFIED**. Section 5 contains the headline design opinion.

---

## 0. Executive answers

| Question | Answer |
|---|---|
| Is alt/az math needed to make the arrows correct? | Only for the *words* ("tilt up", "pan left") and for *cold-start* direction. The on-screen arrow lives entirely in the WCS's pixel frame. |
| The one angle that matters | `ψ = PA_y − q` — camera roll relative to the horizon. Verified: it is the exact rotation between the local alt/az tangent basis and the image axes. |
| Can a human hit the target? | No, not to better than ~2–5 arcmin on a geared head, ~20–60 arcmin on a ball head. Design for "in frame, then crop". |
| What ends a sub-exposure series first, drift or rotation? | Translation. Rotation is ~14× smaller in linear terms and only costs you crop area. |
| Re-aim cadence at 200 mm / APS-C | Every **4–6 min** at dec 0–45°, every ~25 min at dec 80°. |
| Is the analytic alt/az path or the empirical Jacobian more robust? | Closed loop beats both. Empirical Jacobian is the *fallback*; analytic is the *bootstrap*; neither should ever be the correctness-critical path. |

---

## 1. The math of "which way do I turn the knob"

### 1.1 Step 1 — where is the target in this frame?

The WCS is a pixel→world map. We need world→pixel, which is three composed pieces.

**pixel → world (ASTAP / standard FITS TAN):**

```
u  = x − CRPIX1                    v  = y − CRPIX2
(forward SIP, if A_i_j/B_i_j present)
u' = u + Σ_{i,j} A_ij u^i v^j       v' = v + Σ_{i,j} B_ij u^i v^j
ξ  = CD1_1 u' + CD1_2 v'            η  = CD2_1 u' + CD2_2 v'          [degrees]
```

then de-project `(ξ, η)` (gnomonic, in radians of arc) about `(CRVAL1, CRVAL2) = (α0, δ0)`.  With
`x = ξ`, `y = η` converted to radians:

```
den2 = cosδ0 − y·sinδ0                        // intermediate, always > 0 for |offset| < 90°
α    = α0 + atan2( x , den2 )
δ    = atan2( sinδ0 + y·cosδ0 ,  sqrt(x² + den2²) )
```

The matching **sky → intermediate** direction (this is the pair used for verification; `D` is the
cosine of the angular offset):

```
D  = sinδ sinδ0 + cosδ cosδ0 cos(α−α0)
ξ  = cosδ sin(α−α0) / D
η  = (cosδ0 sinδ − sinδ0 cosδ cos(α−α0)) / D
```

Note `den2 = D · cosδ0 − η · sinδ0`; the two forms are the same expression, which is why you can
deproject using only `(x, y, δ0)` and never need `δ` explicitly.

**world → pixel** is the inverse chain:

1. project `(α_t, δ_t)` to `(ξ, η)` about the frame's own `CRVAL` — the *same* gnomonic
   `(α0, δ0) = CRVAL`, which is why no separate tangent-point bookkeeping is needed;
2. `(u', v') = CD⁻¹ (ξ, η)`;
3. invert SIP. There is no closed form; Newton on the 2-D polynomial converges quadratically. In
   practice 3–5 iterations, and for our purposes (target within a few degrees of the reference
   point) the **linear term alone is a good enough seed**: `u₀ = u'`, then
   `u_{k+1} = u' − Σ A_ij u_k^i v_k^j`. Guard with `|SIP residual| < 0.01 px` and fall back to the
   SIP-free result if it does not converge.
4. `x = u + CRPIX1`, `y = v + CRPIX2`.

**ASTAP-specific freebies** (docs: <https://www.hnsky.org/astap.htm>):

* *"The reference pixel is always the image centre."* So `CRPIX = ((nx+1)/2, (ny+1)/2)` and
  `CRVAL` **is** the current pointing — no need to reconstruct the boresight from `CRPIX`.
* ASTAP writes both the modern `CD` matrix and the deprecated `CDELT1/2`, `CROTA1/2`. **Use `CD`
  only.** `CROTA` is ambiguous for flipped images; Han K. states this explicitly in the docs.
* ASTAP can emit 3rd-order **SIP** coefficients (`astap -sip`, or the alignment tab). So the
  world→pixel path must handle SIP; it is not optional.
* The Mount tab reports the altitude and azimuth of the image centre (with aberration, nutation and
  refraction applied). Whether these are *written as FITS keywords* (plausible names
  `OBJCTALT`/`OBJCTAZ`) is **UNVERIFIED**; treat them as display-only until checked.

### 1.2 The three frames

Everything below rests on distinguishing three 2-D tangent bases at the frame centre:

| Frame | Basis vectors | Where it comes from |
|---|---|---|
| **Sky (equatorial)** | `+E` = increasing RA (east), `+N` = increasing Dec | implied by the WCS |
| **Ground (horizontal)** | `+Az` = increasing azimuth, `+Alt` = toward the zenith | ASTAP pointing + latitude + LST |
| **Image (pixel)** | `+x`, `+y` as the array indexes them | the `CD` matrix |

Position angle (PA) is the sky-frame angle, measured **from north, through east**:
PA 0° = north, 90° = east, 180° = south, 270° = west.

**PA of the image +y axis** — straight out of the CD matrix:

```
PA_y = atan2(CD1_2, CD2_2)                         [degrees, mod 360]
PA_x = PA_y − 90°
```

Checked against ASTAP's own worked example header (`CD1_1 = −7.4782e−4`, `CD1_2 = 1.5241e−5`,
`CD2_1 = 1.5534e−5`, `CD2_2 = 7.4830e−4`, `CROTA2 = −1.190`): `PA_y = 1.17°`,
`PA_x = −88.8°` (image +x points ~west — the canonical "north up, east left" orientation), and
`PA_y ≈ −CROTA2`. Han K.'s own rotation formula for programmers is equivalent:

```
sign = +1 if (CD1_1·CD2_2 − CD1_2·CD2_1) ≥ 0 else −1
rotation = −atan2(CD2_1, sign·CD1_1) · 180/π        // == −PA_y, up to the aspect-ratio term
```

**PA of the image +y axis, astronomically:** the direction that appears "up" in the image has sky
position angle `PA_y`. That is all the WCS tells us. What it does *not* tell us is which sky
direction is "up" for the human — i.e. toward the zenith. That is the next formula.

### 1.3 The parallactic angle, and the master rotation ψ

Hour angle `H = LST − α`. The parallactic angle (angle at the object between the hour circle and
the vertical circle) is

```
q = atan2( sin H ,  tanφ · cosδ − sinδ · cos H )                  [Meeus; mod 360]
```

**Verified result:** *the sky position angle of the local "+altitude / toward zenith" direction is
exactly `q`.* Checked over **198 geometries** — φ ∈ {−80…+75°}, H ∈ {−150…+150°}, δ ∈ {−80…+80°},
all pointings above alt −5° — as `max |PA_up − q| = 6 × 10⁻¹⁰ deg` (the finite-difference floor of the
check, i.e. exact). Note: `sin q = sin A · cosφ / cosδ` is the commonly quoted scalar identity, but
the `atan2` form is what you implement (it is quadrant-correct and does not blow up at the pole).

Now define the single number the whole feature hinges on:

```
ψ = PA_y − q                                            "camera roll relative to the horizon"
```

`ψ = 0` ⇔ the image is "upright": image +y points at the zenith. `ψ = 90°` ⇔ portrait/rolled, image
+y points east or west.

**Mapping ground corrections into the image** (derived by composing the two rotations; verified):

```
x = ( −Δaz·cos h )·cos ψ + ( Δalt )·sin ψ
y = (  Δaz·cos h )·sin ψ + ( Δalt )·cos ψ
```

where `(Δaz·cos h, Δalt)` is the target-minus-pointing offset in arcsec/degrees on the local
horizontal tangent plane, and `(x, y)` is the same offset in **pixels-capable units of the image
frame** (divide by the pixel scale to get pixels). Time to be explicit about signs: this convention
treats `+x` as "to the right" and `+y` as "up on screen" in a north-up/east-left rendering, so a
target that is east of centre comes out with `x < 0` (left) — correct for that rendering.

**Verification.** Against a synthetic WCS (7 geometries: φ ∈ {+45, −33, +70, −20, −80},
δ ∈ {−60…85}, arbitrary `PA_y` ∈ {0, 15, 30, 45, 137, 200, −70}, scales 2.7–9.5 ″/px), the ψ
formula reproduces the full WCS world→pixel offset to **0.05 px worst case** at 0.02° offsets (the
residual is pure second-order projection, ~0.3 %). So: ψ is exact to numerical precision in the
linear regime, and the second-order term is irrelevant for anything under ~10 % of the frame.

Why this matters, concretely: with an *upright* camera (`PA_y = 0`) at φ = +45°,

| pointing | alt | az | q | ψ |
|---|---|---|---|---|
| RA 0°, Dec 0° (meridian) | 45.0 | 0.0 | 0.0 | **0°** |
| RA 45°, Dec 0° | 30.0 | 54.7 | −35.3 | **35°** |
| RA 90°, Dec 0° (rising, east) | 0.0 | 90.0 | −45.0 | **45°** |
| RA 45°, Dec 45° | 58.6 | 106.3 | −73.7 | **74°** |
| RA 45°, Dec 75° | 54.3 | 161.7 | −121.0 | **121°** |

A naive implementation that assumes "image up = sky up" is wrong by more than 45° over most of the
sky, and by more than 90° for anything within ~20° of the pole. That is the whole argument for
carrying ψ.

### 1.4 Equivalent formulation you may prefer: rotate the *sky* offset, not the ground offset

If you already have the target's pixel position (§1.1) you do **not** need ψ at all for an on-screen
arrow. But when you do need the ground frame (labels, "turn the pan knob", drift alignment), here is
the compact pair, both verified:

```
// sky-frame offset (arcsec), PA θ, magnitude m
E = m·sin θ ,  N = m·cos θ

// ground frame  (right-handed rotation by +q)
Δaz·cos h =  E·cos q − N·sin q
Δalt      =  E·sin q + N·cos q

// image frame   (this one is a REFLECTION — det = −1)
x = N·sin PA_y − E·cos PA_y
y = N·cos PA_y + E·sin PA_y
```

The reflection is not a bug: `(x, y)` with `+x` 90° *clockwise* from `+y` in sky-PA terms is a
left-handed sky frame, which is exactly why flipped-parity WCS solutions are harmless once you stay
in the CD frame (see §6).

### 1.5 Reductions worth keeping in your head

* **Azimuth and altitude corrections are cheap.** Once you have `(α, δ)` for the pointing and the
  target, `(Δaz·cos h, Δalt)` falls out of the standard spherical identities. Use the `atan2` forms,
  never `asin`/`acos`:

```
sinδ = sinφ sin h + cosφ cos h cos A
H    = atan2( −sin A · cos h , sin h·cosφ − cos h·cos A·sinφ )
α    = LST − H

cos A = (sinδ − sinφ·sin h)/(cosφ·cos h)      → use atan2(−cosδ sinH, sinδ cosφ − cosδ cosH sinφ)
h     = asin( sinφ sinδ + cosφ cosδ cos H )
```

* **The correction magnitude** is `m = great-circle distance(pointing, target)`; for small offsets
  `m ≈ sqrt((Δα cosδ)² + Δδ²)`, and `m ≈ sqrt(Δaz²cos²h + Δalt²)` — the two agree to O(m³).

* **Knob → sky response is *not* 1:1.** Panning rotates the head about the vertical: a pan of `ΔA`
  moves the *pointing* by only `ΔA·cos h`. Front tilt moves altitude 1:1 (the tilt axis is
  horizontal and perpendicular to the sight line). So the pan knob becomes **useless near the
  zenith** and the azimuth correction you must ask for blows up:

| alt | pan effectiveness | az error amplification 1/cos h |
|---|---|---|
| 30° | 0.87× | 1.15× |
| 60° | 0.50× | 2.0× |
| 75° | 0.26× | 3.9× |
| 85° | 0.087× | 11.5× |
| 88° | 0.035× | 28.7× |

  At alt 88° the user can turn the pan knob through 10° and see the field move 0.35° — they will
  conclude the tool is broken unless you say so first.

* **Caveat for unleveled tripods.** All of the above assumes the pan axis is vertical and the tilt
  axis horizontal. An unleveled tripod cross-couples them: panning also changes altitude. Handle
  empirically (§5).

### 1.6 Degenerate cases — flag them in the UI, do not let the arrow lie

| Condition | What breaks | What to do |
|---|---|---|
| `alt > 85°` (near zenith) | Azimuth undefined at the pole of the horizontal frame; `Δaz` → ∞; pan knob ineffective; a 0.1° change in pointing can demand 180° of pan | Suppress az/alt labels. Give the **image-space arrow only**, plus "the target is close — tilt and pan in small steps". Never print "pan 47°". |
| `|δ| > 85°` (near celestial pole) | `q` and `PA_y` change fast with time (the field *spins* at ~15°/hr), so `ψ` drifts minute-by-minute; the field rotates a full frame in ~18 min at 200 mm | Recompute ψ from a fresh solve each iteration; never cache it across minutes. Warn "fast field rotation". |
| `δ ≈ 0` | Rotation ≈ 0 but translation is maximal; frame exits fastest | It's the *easiest* case for arrows and the *worst* for cadence (see §3). |
| target within ~0.05° of centre | Arrow direction is noise (solve centroid jitter dominates) | Replace arrow with "centred ✓"; snap the arrow to 8 compass directions below some threshold. |
| target within a few arcmin of the *pole* | RA is degenerate; PA_y from the WCS is still fine but `atan2` on a near-zero CD column gets noisy | Use vector math (`(CD1_2, CD2_2)` has a well-defined direction) — it is only `atan2` of a *small* pair, which is fine; the degeneracy is in *declination*, not in the CD. |
| target below the horizon / wrong sky region | Correction is huge but "valid" | If `m > 30°`: stop nudging, declare "the camera isn't pointing where you think" and check date/time/target. |
| low altitude (`alt < 15°`) | Differential atmospheric refraction adds a real translation+rotation gradient across the field (≈2.5′ at 20° alt; ≈0.4′ across a 4.5° field ⇒ ~6 px at 200 mm) **UNVERIFIED** magnitude, standard refraction models | Prefer solving higher in the sky; note it in the docs; ASTAP's own alt/az already applies refraction. |

---

## 2. How much error a human can absorb

### 2.1 What the hardware can actually do

* **Manfrotto 410 Junior geared head:** *"Degrees of Rotation for Each Full Turn of Handle: 7.2°"*
  (vendor spec sheet, <https://www.eurooptic.com/manfrotto-gear-head-410>) → 50 turns end-to-end,
  micrometric knobs, ~25 mm knob ⇒ ~1 mm of finger travel ≈ 0.09° ≈ 5.5′.
* **Ball heads:** the panning base is often laser-engraved in **2.5°** increments
  (e.g. Leofoto LH-30). A ball head's fine control is a friction knob — you are pushing a ball by
  hand, not gearing it.
* **Working estimate of human repeatability** (**UNVERIFIED**, engineering judgement):
  * geared head, deliberate nudge: **2–5 arcmin**
  * geared head, "just push it": 10–20 arcmin
  * ball head, friction drag: **20–60 arcmin** (0.3–1°)
  * after releasing and re-aiming grossly: 0.5–2°

For scale, one arcminute in pixels:

| lens (APS-C, 3.9 µm) | scale ″/px | 0.5′ | 2′ | 5′ | 15′ | 1° |
|---|---|---|---|---|---|---|
| 50 mm | 16.09 | 1.9 px | 7 px | 19 px | 56 px | 224 px |
| 135 mm | 5.96 | 5 px | 20 px | 50 px | 151 px | 604 px |
| 200 mm | 4.02 | 7.5 px | 30 px | 75 px | 224 px | 896 px |
| 300 mm | 2.68 | 11 px | 45 px | 112 px | 336 px | 1343 px |

**Conclusion:** even a "coarse" 5′ hand adjustment at 200 mm is 1.2 % of the frame width. Perfect
centring is *not achievable and not needed*.

### 2.2 Therefore: design for "in frame, then crop"

The stack is registered and cropped anyway. A residual pointing error `e` (as a fraction of frame
height) costs you roughly `e` in linear field, i.e. `2e` in area. A 10′ error at 200 mm = 3.7 % of
the 4.5° height ⇒ 7 % area loss. Irrelevant. Therefore:

```
accept when  m < max( 5′, 0.05 × frame_height )
```

and never ask for better. Publish the residual, not a "lock" indicator.

### 2.3 When to nudge vs. re-aim from scratch

Define `D = frame diagonal`. Rules of thumb (opinionated, but grounded in §2.1):

| error `m` | Recommendation |
|---|---|
| `m < 0.05·H` | **Done.** "centred (3′) ✓", stop prompting. |
| `0.05·H < m < 0.25·H` | Fine nudge with the knobs. This is where the arrow lives. |
| `0.25·H < m < 0.75·H` | One firm knob nudge; if the head has a lock, it is cheaper to release and re-aim. Say so. |
| `0.75·H < m < 30°` | "Release the head, point roughly at it, re-solve." Give a **coarse** arrow (32-wind compass rose, 10° quantisation) and the distance in "frames" ("about 2 frame-widths left and 1 up"). |
| `m > 30°` | Do not nudge. Wrong target / wrong time / wrong date / solved a stale frame. |

Frame-relative phrasing is the important part: a human can act on "half a frame left" instantly, and
cannot act on "1.4°" without a mental scale conversion. Show both.

---

## 3. Drift, rotation, and re-centering cadence

### 3.1 The exact model for a fixed camera (verified)

Set `ω = 15.041067 ″/s` (360° per sidereal day 86164.0905 s). For a camera that does **not** move, at
a field centre of declination δ:

* **Translation:** speed `ω·cos δ` (arcsec/s), direction = **due west** = PA 270°.
* **Rotation:** `ω·sin δ` (arcsec/s of field rotation *about the frame centre*).

The two together are a rigid rotation about the celestial pole; the decomposition above is exact to
first order in field radius. Note what is *absent*: latitude and azimuth. **The fixed-camera field
rotation rate depends only on δ.** Verified numerically over a 5 × 4 grid of (δ, H) at φ = 45°
(fixed-camera mode), matching `ω·sin δ` to 0.001 ″/s in every case, including δ = 0 (zero rotation,
pure translation) and δ = 85° (≈ω).

**Caution — folklore formula.** You will find the field-rotation rate quoted as
`ω·cosφ·cosA/cos h` (e.g. Cloudy Nights, many blog posts). That is a quantity for a *tracking* alt-az
mount, where the tube's own roll about the sight line (`−Ȧ·sin h`) must be added to the sky's
rotation; it is **not** the fixed-camera rate. Our numerical check confirms it fails badly for a
fixed camera (at φ=45°, δ=0°, meridian: 15.04 vs 0.00). **Do not use it.** The tracking-mount
formula in general is **UNVERIFIED** here and out of scope — we have no tracking mount.

### 3.2 The drift forecaster is three lines

Because the camera is fixed and the WCS is a snapshot, "predict where the target will be at time t"
does not need any of §1.3's machinery:

```
α_t(t) = α_t + ω·(t − t₀)        (δ_t unchanged)
pixel(t) = world→pixel( α_t(t), δ_t )    using the PINNED WCS from the last solve
```

The gnomonic projection through the pinned CD automatically produces the *correct combination* of
translation and rotation. No latitude, no LST, no clock beyond a monotonic timer. Error sources over
minutes: refraction (dominant, only near the horizon), polar motion/aberration (negligible).

This is the strongest available argument that *re-centring does not need alt/az at all*.

### 3.3 Numbers for an APS-C 23.6 × 15.7 mm sensor at 3.9 µm (6000 × 4000)

Field of view and scale:

| lens | FOV (W×H) | ″/px |
|---|---|---|
| 24 mm | 52.36° × 36.22° | 33.5 |
| 50 mm | 26.56° × 17.85° | 16.1 |
| 85 mm | 15.81° × 10.55° | 9.5 |
| 135 mm | 9.99° × 6.66° | 6.0 |
| **200 mm** | **6.75° × 4.50°** | **4.02** |
| 300 mm | 4.50° × 3.00° | 2.68 |

Time for a centred target to reach the frame edge (no re-aim), and the rotation-induced smear at the
corner during one **5 s** sub:

| lens | dec | drift ″/s | t to short edge | t to long edge | subs @5 s | corner rot smear / 5 s | rot °/min |
|---|---|---|---|---|---|---|---|
| 50 | 0 | 15.04 | 35.6 min | 53.0 min | 427 | 0.00 px | 0.000 |
| 50 | 45 | 10.64 | 50.3 min | 74.9 min | 604 | 0.92 px | 0.177 |
| 50 | 80 | 2.61 | 205 min | 305 min | 2460 | 1.29 px | 0.247 |
| 200 | 0 | 15.04 | **9.0 min** | 13.5 min | 108 | 0.00 px | 0.000 |
| 200 | 30 | 13.03 | 10.4 min | 15.5 min | 124 | 0.66 px | 0.125 |
| 200 | 45 | 10.64 | **12.7 min** | 19.0 min | 152 | 0.94 px | 0.177 |
| 200 | 60 | 7.52 | 18.0 min | 26.9 min | 215 | 1.15 px | 0.217 |
| 200 | 80 | 2.61 | 51.7 min | 77.5 min | 620 | 1.30 px | 0.247 |
| 300 | 0 | 15.04 | 6.0 min | 9.0 min | 72 | 0.00 px | 0.000 |
| 300 | 45 | 10.64 | 8.5 min | 12.7 min | 102 | 0.94 px | 0.177 |

Note the striking invariance: **the corner rotation smear per sub is independent of focal length**
(both the lever arm and the pixel scale scale with f). For an N-megapixel sensor it is

```
smear_px ≈ (rot_rate · t) · (half-diagonal in px) / 206265  ≈  0.0175 · rot_rate[″/s] · t[s]   @ 24 MP
```

so at dec 45° a 5 s sub always smears the corners by ~0.9 px whatever the lens.

### 3.4 Answering the question directly

For **200 mm, 4.5° tall frame, 5 s subs**:

* **Translation is the binding constraint, by a factor of ~14.** At dec 0°, one 5 s sub trails the
  stars by `15.04 × 5 / 4.02 ≈ 19 px` at the frame centre, while rotation moves the *corners* by
  0.0 px. At dec 45°: 13 px trailing vs 0.9 px of corner smear.
* **Subs before you must re-aim (target exits frame): 108–152**, i.e. **9–13 min** at dec 0–45°.
  That is the theoretical maximum. Practically you re-aim far sooner (below).
* **Recommended re-aim cadence:** keep the target within ±25 % of the frame height from centre, so
  re-aim every **≈ 4–6 min** at dec 0–45°, **≈ 25 min** at dec 80°. Print a countdown ("re-centre
  in 3:40") rather than making the user notice.
* **Does rotation become binding first?** Not for *re-aiming* — translation always wins. Rotation
  becomes the binding constraint only for **total integration time on one pointing**, because each
  sub must be rotated into a common frame and the common inscribed rectangle shrinks. For a
  rectangle `W×H` rotated by θ, the largest inscribed same-aspect rectangle is scaled by

```
k(θ) = min( W / (W cosθ + H sinθ) ,  H / (W sinθ + H cosθ) )
```

| θ | k | area kept | time @dec 20° | @dec 45° | @dec 70° |
|---|---|---|---|---|---|
| 2° | 0.951 | 90 % | 23 min | 11 min | 8.5 min |
| 5° | 0.887 | 79 % | 58 min | 28 min | 21 min |
| 10° | 0.803 | 65 % | 117 min | 56 min | 43 min |
| 20° | 0.688 | 47 % | 233 min | 113 min | 85 min |

(The factor is almost focal-length independent — it depends on aspect ratio only.) So: **rotation
costs you ≈5 % of crop area per 5 minutes at dec 45° (≈4 % at dec 20°, ≈7 % at dec 70°), and that is
the whole story.** Do not build
elaborate de-rotation. Do surface it as "field rotated 3.2° — final crop will be 92 %" after long
runs. Also note the corollary: at dec 80° the frame rotates 0.25°/min — a *single* 5 s sub's corners
smear ~1.3 px, so long-lens imaging near the pole is rotation-limited per sub even before tracking
enters the picture.

**Per-sub trailing is the real limit, not the re-aim cadence.** At 200 mm/f4 dec 0, staying under
2 px of trailing caps the sub at `2 × 4.02 / 15.04 = 0.53 s`. Untracked 5 s subs at 200 mm are
already 19 px trailed — the tool should say this out loud. The honest product position: **untracked
imaging works at 24–85 mm with 2–10 s subs; at 135 mm+ the tool's job is acquisition and framing,
not deep-sky integration.**

---

## 4. Polar-alignment analogies worth stealing

### 4.1 Drift alignment (the classical method)

Drift alignment observes declination drift of a star near the meridian (for azimuth error) and near
the east/west horizon (for altitude error), and tells the user to turn the *azimuth* knob or the
*altitude* knob by a small amount. Two structural lessons:

* **Decouple the axes.** One knob is adjusted using one observation. This is what makes it learnable
  even though the underlying geometry is 3-D. Cross-coupling is what makes novices fail.
* **The gold standard is slow precisely because each iteration is one observation.** A solved-frame
  tool does the same measurement in one exposure.

Sources: <https://canburytech.net/DriftAlign/>, <https://skyandtelescope.org/astronomy-resources/accurate-polar-alignment/>.
Note that the Canbury analysis emphasises refraction being a first-class term — worth copying: past
~10′ of accuracy, refraction (and its gradient) is the error floor.

### 4.2 SharpCap Polar Alignment — the closest analogue to what we need

Method: two solves of the pole region separated by ~90° of RA rotation; from the two, the RA axis
and hence the alignment error are recovered. Plate solving is built in, offline
(<https://docs.sharpcap.co.uk/4.1/9_PolarAlignment.htm>).

**UI affordance — copy this one:**

* The brightest star on screen is highlighted **with an arrow pointing from it to a target marker**.
  Direct manipulation: *move the indicated star into the target.* This is far better than an abstract
  dial, because it is self-calibrating for the user — they see the star move under their hands.
* Direction guidance ("left and down") is repeated **in large text overlaid on the camera image**, so
  it is readable from arm's length while both hands are on the mount.
* The direction guidance is the *only* part that needs latitude/longitude: the manual says so
  explicitly ("Determining which direction is up/down/left/right"). **This is independent
  corroboration of §5's architecture.**
* The reference convention is spelled out in words: *"Stand looking past your telescope and mount
  towards the celestial pole… move the mount so as to shift the far end of the telescope."* They had
  to define "left" because it is genuinely ambiguous otherwise.
* **Audio feedback:** *"SharpCap will play a beep… as the polar alignment error is reduced, the pitch
  of the beep will increase."* For a laptop tool where the user's hands and eyes are on the tripod
  and the screen is behind them, **steal this immediately.** Rising pitch = shrinking error is a
  1-dimension channel that needs zero attention.
* Error is reported as a single scalar; there is an explicit warning that the guidance is only
  approximate if location is not entered.

### 4.3 N.I.N.A. Three Point Polar Alignment (TPPA) — steal the tolerancing UI

Method: three solves while the user hand-rotates the RA axis (or the plugin moves it). Three boresight
points define a circle; the circle's axis is the RA axis; the misalignment is the angle between that
axis and the pole. Manual mode explicitly *keeps solving while the user moves*, and transitions when
the movement is "far enough"
(<https://github.com/isbeorn/nina.plugin.polaralignment/blob/master/PolarAlignment/FAQ.md>).

**UI affordances worth copying:**

* **Concentric target circles rendered from the image scale** — TPPA draws 30″, 1′, and 5′ circles.
  This is exactly the "tolerancing" language we need: the user is not chasing zero, they are entering
  a circle. Our equivalent: draw circles at `0.02·H`, `0.05·H`, `0.15·H` of the frame.
* Separate **altitude error, azimuth error, and total error** readouts, each colour-coded, with an
  "Alignment Tolerance" that auto-finishes when total error drops below a configured threshold. Copy
  this: *declare success automatically.* The single worst beginner experience is a tool that keeps
  asking for more precision than the hardware can hold.
* Overlay colours are user-configurable (target circle, success, per-axis). Minor, but it signals
  that overlays should be glanceable, not pretty.
* It works "almost anywhere above the horizon" and states which fields are more forgiving — i.e.
  *the docs teach the geometry*. Do the same: one sentence on why re-centring is hard near the zenith.
* Refraction is an optional, explicitly "under test" correction. Precedent for shipping it as an
  option with a caveat rather than silently.

### 4.4 Ekos / KStars Polar Alignment Assistant

Three RA rotations + solves, then an explicit **azimuth error / altitude error** readout plus a
*correction graphic* — the code has `setupCorrectionGraphics(pixel)` and
`updateRefreshDisplay(azError, altError)`, i.e. **an arrow drawn at a pixel position plus a numeric
per-axis error**. Same decomposition as TPPA.
(<https://api.kde.org/legacy/kstars/html/polaralignmentassistant_8h_source.html>,
<https://kstars-docs.kde.org/en/user_manual/ekos-align.html> — the docs page rendered as binary for
me; treat the UI specifics as **partially UNVERIFIED**.)

### 4.5 PoleMaster

Camera on the RA axis; the user identifies Polaris; the software draws a **green circle** on which
Polaris must lie, and the user adjusts alt/az until the star sits on the circle. Verification step:
*"the accuracy of the RA axis is verified when one can see the star moving along the green circle."*
(<https://www.qhyccd.com/uploadfile/2018/1225/20181225042450636.pdf>)

Lessons: (a) a *geometric locus* (a circle) is easier to satisfy than a numeric target; (b) **use the
prediction itself as the verification** — if the star doesn't follow the predicted path, the model is
wrong and you should say so instead of drawing a confident arrow.

### 4.6 PS Align Pro / Polar Scope Align

Phone-gyro app: draws the *correct current* position of Polaris for the epoch/date/time/location on
top of a chosen reticle, calibrated against a known star. Side benefit: it also does "push-to / manual
GoTo" with an arrow. Lessons: (a) **calibrate the aiming device against something known** — the same
trick as the empirical knob Jacobian in §5; (b) supporting many reticles is really "adapt the overlay
to the user's hardware", which for us means adapt to the head/knobs.
(<https://astro.ecuadors.net/polar-scope-align/>, <https://apps.apple.com/us/app/polar-scope-align-pro/id970161373>)

### 4.7 SkySafari / push-to (Dobsonians)

Arrow buttons on screen + a rate slider; a **bullseye indicator** that the user drives to centre;
"Push-To" draws an arrow showing which way to move. Physical convention matters: docs for
encoder-based push-to tell you to configure for "flipped or inverted" mounting
(<https://support.simulationcurriculum.com/hc/en-us/articles/115015867607-Scope-Control-View>,
<https://www.pushtoair.com/pta-usermanual/>).

Lesson: **the bullseye-with-a-dot is the most transferable widget in amateur astronomy.** It encodes
both direction and magnitude in one glyph, it degrades gracefully when the dot is off-screen (clamp
to the rim + a distance label), and it needs no legend.

### 4.8 Non-astro precedents

* **Spirit-level apps** (bubble + numeric degrees): an *absolute* gauge that shows "how far from
  good" without demanding direction reasoning. Good for the "you are 1.4° off" panel.
* **Car reversing cameras** (static + dynamic guide lines that bend with steering): the "predicted
  path" idiom. Our analogue: draw the *predicted* target position after the nudge the user is about
  to make — we can't, we don't sense the nudge, so use the drawn arrow instead.
* **Camera gimbal apps** (Ronin/DJI) showing a horizon line + roll readout: **the horizon line is a
  free ψ display.** Drawing a synthetic horizon/level line on the live frame tells the user their
  camera's roll instantly and makes "tilt up" unambiguous. Strongly recommended, costs nothing: the
  horizon's image angle is just `ψ`.
* **Audio sonification** (Geiger counters, parking sensors): SharpCap's pitch feedback is the
  astronomy instance; the parking-sensor model (rate increases as you approach) is the same idea and
  is well understood by non-experts.

---

## 5. Do we need alt/az? — the design question, with an opinion

### 5.1 The three candidate architectures

**(A) Analytic, ground-referenced.** Solve → `(α,δ,PA_y)` → alt/az both ends → `(Δaz·cos h, Δalt)` →
ψ → image arrow. Needs *latitude* and *LST* (hence a correct clock, timezone, DST, hemisphere).

**(B) Analytic in image space.** Solve → project target through the WCS → pixel offset from centre →
arrow. Needs nothing but the WCS. (For *drift forecasting*, also ω and δ — still just the WCS.)

**(C) Empirical Jacobian.** Probe: solve, nudge one knob, solve. The observed image displacement is
one column of a 2×2 matrix `J: (Δpan, Δtilt) → (Δx, Δy)`. Two non-parallel probes give the whole
matrix; then `δknob = J⁻¹ (target_px − current_px)`.

### 5.2 What each is actually good for

**(B) is strictly necessary and sufficient for the on-screen loop.** Everything the arrow needs is in
the WCS. The reflection in §1.4 means parity and roll are handled for free. Verified to 0.05 px.

**(B) also gives you the drift model for free**, because a fixed camera means the WCS can be pinned
and the target can be walked forward in RA (§3.2). Crucially, **drift forecasting needs δ, not
latitude or azimuth** — so even the "when do I need to re-aim" feature needs no geography.

**(A)'s irreducible value is exactly three things:**

1. **Words.** "tilt up" / "pan left" is what a human does with their hands. (B) can only say "move
   the star left-and-down", which is fine when the star is visible and useless when it is not.
2. **Cold start.** Before any target is visible, the arrow direction has to come from somewhere.
   (C) has no information yet; (B) can produce the pixel position of the target but *not* which
   physical direction that is until you know ψ.
3. **Explanations and refusal.** "Don't bother, the target is 1° from the zenith — panning won't
   help" requires knowing altitude. So does a refraction caveat.

**(C)'s value** is that it makes the *hardware* part of the model empirical instead of assumed. It
absorbs: an unleveled tripod, a non-vertical pan axis, knobs with different rates, head backlash,
and — importantly — **any sign/parity error in our own code**, because it is measured in the same
pixel frame we draw in.

**(C)'s fatal flaw as a *primary* architecture:** it cannot produce the first arrow. A 2×2 Jacobian
needs two probes; before that you have one column and no scale. And the knob *scale* is unobservable
— the user cannot tell you "I turned it 3.2°".

### 5.3 Recommendation

> **Build the loop in image space (B). Use alt/az (A) only as a labelling/explanation layer. Use the
> empirical Jacobian (C) as a silent self-check, not as the controller.**

Concretely, in priority order:

1. **Controller = closed loop in pixels.** Never invert anything analytically to produce a motor
   command. Produce a *direction* and let the human close the loop. The control law:

   ```
   while |e_px| > tol:
       solve → e_px = target_px − centre_px
       draw arrow from current target position toward centre, length ∝ |e_px|
       label with |e| in arcmin AND as a fraction of the frame
       speak a pitch proportional to 1/|e|
   ```

   Because the loop re-measures after every nudge, the *gain* never has to be known. Convergence is
   geometric; from 2° to 3′ takes ~4 iterations on a geared head. This is the single most robust
   thing you can build and it is why "which way do I turn" should never be answered by an open-loop
   calculation.

2. **ψ is the only angle you must compute, and only for words and overlays.** Level-line overlay,
   "tilt up / pan left", the compass rose for gross re-aims. ψ needs `PA_y` (free from the CD) and
   `q` (needs latitude and LST) — **one scalar, one place to get it wrong, easy to unit-test.**

3. **Ship a "level the camera" shortcut that removes the dependency.** If the user levels the head
   with its bubble level (3-way heads all have one — the Manfrotto 410 has a levelling bubble), then
   image-up *is* zenith *is* +alt, i.e. `ψ = 0` by construction, and (A) degenerates to (B) with a
   known basis. That single instruction deletes latitude, time, timezone, DST and hemisphere from the
   critical path. Make "is the camera level?" a checkbox that defaults to *yes*, and fall back to
   computing ψ when it is unticked.

4. **Learn the knob silently.** Log every `(Δknob-implied-displacement)` pair from the natural
   nudging the user does anyway; once you have two non-parallel samples, compare the measured 2×2
   against the predicted `ψ`-rotated diagonal. Two actions:
   * if `|off-diagonal| > 0.2` → tell the user "your tripod is not level; the pan knob is changing
     altitude too" (a genuinely useful diagnostic nobody else gives),
   * otherwise use the measured matrix for the *magnitude* suggestion ("about a quarter turn") while
     keeping the measured *direction* as the arrow.

5. **Optional "calibrate my head" wizard** (one minute, once per head): turn each knob through one
   full turn while solving continuously. Yields `deg/turn` per axis, so you can then say "0.4 turns"
   instead of "1.2°". Nice-to-have, not core.

6. **Do not build an open-loop "nudge by exactly X°" as the primary UX.** It assumes head
   linearity, backlash, and a level tripod, and it fails exactly when the user is already struggling
   (near the zenith, unleveled, cold fingers). Show the number as *context* next to the arrow.

**Answer to "which is more robust for a beginner-facing tool":** (B) plus a closed loop. (A) alone is
more precise on paper and fails catastrophically when the laptop's timezone is wrong by an hour or
the user types 54 instead of 45 for latitude — at which point the arrow points 20° the wrong way and
the beginner concludes the software is broken. (C) alone cannot start. (B) is *self-evidently* correct
because the target's predicted pixel position is directly checkable against the frame the moment the
target appears in it.

---

## 6. Camera-orientation bookkeeping

**Does the CD matrix give roll? Yes, and that is all you need.**

```
PA_y  = atan2(CD1_2, CD2_2)              // PA of image +y  (astronomical, N through E)
PA_x  = PA_y − 90°
scale = sqrt(|det CD|)                    // deg/px, geometric mean of the axes
det   = CD1_1·CD2_2 − CD1_2·CD2_1
```

Sign conventions to write down once and never re-derive:

* PA is measured **from north, through east**, i.e. counterclockwise on the sky when rendered
  north-up/east-left.
* `+x` in the array is **clockwise** 90° from `+y` in PA terms — hence the reflection in §1.4.
  Standard "north up, east left" imagery gives `PA_y ≈ 0`, `PA_x ≈ 270°` (image +x points west).
* ASTAP reports the complementary number: `CROTA2 = −PA_y` to within the aspect term, and defines the
  reported angle as *"between the north arrow and the Y axis… the angle reported between the Y-axis
  and north arrow in the direction north to east"*, and *"the reported angle is the rotation from sky
  to image orientation counterclockwise; the camera … was rotated with the same angle clockwise."*
* **Do not trust mnemonics; unit-test.** The test that catches every sign error: solve a real frame,
  pick the pixel on the celestial equator due east of the centre one degree away, and assert it
  renders on the side of the image where you expect east. Then flip the camera 90° and re-run.

**Parity / mirrored solutions.** ASTAP (like astrometry.net with `--parity`) can return either parity.
`det CD < 0` ⇔ mirrored (astrometry.net's `--parity pos/neg` selects the same binary switch). ASTAP states it standardises on reporting *"flipped horizontal"* and that
the reported angle/CROTA2 is *as if the image is "unflipped horizontal"*.

**Pitfalls, in order of likelihood:**

1. **Mixing frames.** If you flip the array for display (many people mirror left-right because the
   camera's preview is mirrored, or because the sensor is read out that way) but compute pixel
   positions from the unflipped WCS, every arrow is mirrored. **Rule: derive everything in the native
   array frame; if you transform for display, compose the display transform with the WCS and keep one
   matrix.** Verification: the mirrored case shows up immediately as arrows pointing *away* from the
   target — make that a self-test ("if the user's error increases twice in a row, the sign is wrong —
   flip and retry"), which is a legitimate cheap defence.
2. **EXIF orientation / raw decoding conventions.** A JPEG from the camera carries an orientation tag;
   a de-mosaiced raw does not. The array that ASTAP saw is the array you must draw on. Hash the frame
   and never re-transform between solve and render.
3. **`CROTA`/`CDELT` vs `CD`.** `CROTA1/CROTA2` are deprecated and ambiguous when `CDELTn` differ;
   ASTAP writes them for compatibility only. Read `CD`, or read `PC`+`CDELT` and multiply.
4. **`PC` matrix instead of `CD`.** Some pipelines emit `PC1_1..` with `CDELT1/2`; `CD = PC · diag(CDELT)`.
   If both are present, `CD` wins. (**UNVERIFIED** whether any ASTAP version writes `PC`.)
5. **Non-orthogonal CD.** A CD matrix can encode a slight shear (residual distortion); `atan2` on a
   column still gives a valid axis direction, but `PA_y − PA_x ≠ 90°` exactly. Compute both
   independently and use the appropriate one per axis rather than assuming orthogonality, or
   orthogonalise before use if you need a single ψ.
6. **Roll changes between frames** because the user bumped the camera. Never cache `PA_y` longer than
   one solve; recompute ψ from the current solution every iteration.
7. **SIP + roll.** SIP is defined in the *undistorted focal plane*, before the CD matrix; it must be
   applied between pixel and CD, in the order given in §1.1. Applying SIP after CD is a classic bug
   that produces a few-pixel error growing toward the corners — which at 200 mm is exactly where you
   are trying to place a target.

---

## 7. UI precedents, distilled

**Astro software that shows a "nudge this way" affordance**

| Tool | Affordance | Copy? |
|---|---|---|
| SharpCap PA | arrow from a highlighted **star to its target marker**; large overlaid text direction hints; ascending **beep pitch** as error falls; error scalar | **Yes — all four.** Closest analogue to our problem. |
| N.I.N.A. TPPA | concentric **target circles at 30″/1′/5′** rendered from image scale; separate az/alt/total error, colour-coded; auto-finish below tolerance | **Yes.** Circles at frame-relative radii, auto-finish. |
| Ekos PA | arrow graphic at a pixel + numeric az/alt error readout | Yes (same as TPPA) |
| PoleMaster | **green locus circle** the star must sit on; prediction doubles as verification | Yes — "verify by predicting". |
| PS Align Pro | overlay adapted to the user's reticle; calibrated against a known star; also push-to arrows | Yes — adapt to hardware, calibrate against known. |
| SkySafari / push-to | **bullseye + dot**, rate slider, arrow | Yes — best single glyph. |
| ASTAP viewer | north-east arrow + numeric rotation angle in the status bar | Yes — free credibility; user learns to trust the solve. |
| Stellarium / planetarium apps | FOV rectangle overlay showing where the camera is pointed on the sky | Yes, later: "you are here" context is the best cure for pointing grossly wrong. |

**Non-astro**

* **Spirit level / bubble level apps**: absolute gauge, numeric degrees, zero learning curve. Use for
  the "you are 1.4° off" panel.
* **Camera gimbal apps**: horizon line + roll readout → **draw the synthetic horizon**; its image
  angle is literally ψ, and it makes "tilt up"/"pan left" self-evident.
* **Parking sensors / Geiger counters**: rate-coded audio. Combined with SharpCap's precedent, audio
  is the right channel for a hands-busy, screen-behind-you workflow.
* **Car reversing guides / drone return-to-home**: clamped off-screen arrow at the frame rim with a
  distance label. Use when the target is outside the frame — which is the *acquisition* case and
  therefore the common case.

**Suggested minimum overlay set**

1. Frame centre reticle + the tolerance circles (2 %, 5 %, 15 % of frame height).
2. Target marker at the predicted pixel (clamped to the rim if outside, with "3.1° ↗" label).
3. Arrow from current→target, in **pixel** space.
4. Text: `TILT UP 0.42° · PAN LEFT 1.1°` (needs ψ), plus `= 1/6 turn` if calibrated.
5. Horizon line (angle = ψ) — cheap, and it teaches the user why the arrows rotate.
6. `error 1.4° (31 % of frame)` and, once solved repeatedly, `re-centre in 3:40`.
7. Rising-pitch beep on error decrease; a distinct "good enough" chime at tolerance.

---

## 8. Implementation notes and residual risks

* **Control-loop bandwidth = solve rate.** Everything in §5.3 assumes the loop closes in ~1 s. Hinted
  ASTAP solves (`-f -fov -r -ra -spd`; note `-ra` is in **hours** and `-spd = δ + 90`) on a
  downsampled 2000 px frame are sub-second on a laptop (**UNVERIFIED** for ASTAP specifically; a
  comparable Rust solver advertises 0.2–0.4 s hinted solves). Budget: downsample to 25–50 % for
  *guidance* solves, full-res only for the final confirmation.
* **Solve failures dominate UX, not math.** Few-star fields, dew, clouds, and a target outside the
  frame all produce "no solution". Design the arrow to be *sticky*: keep drawing the last known
  direction with a "stale" styling and a "re-solving…" indicator. A blank screen mid-nudge destroys
  trust.
* **Refraction** is the accuracy floor below ~10′ and near the horizon; it also biases the *reported*
  altitude. ASTAP applies refraction to its displayed alt/az; if you compute your own, either both
  apply it or neither does.
* **Do not implement de-rotation.** Rotation costs area, not resolution (§3.4), and re-solving
  handles it.
* **Kill the "perfectly centred" fantasy.** Auto-finish (TPPA's Alignment Tolerance) is the single
  highest-value UI decision available.

**UNVERIFIED list**

1. Manfrotto 410 "7.2° per full turn" comes from one vendor spec sheet; confirm on a real head.
2. Hand-adjustment repeatability (2–5′, 20–60′) is an engineering estimate, not measured.
3. Whether ASTAP writes altitude/azimuth (e.g. `OBJCTALT`/`OBJCTAZ`) into the FITS header, vs. only
   displaying them in the Mount tab.
4. Whether ASTAP computes SIP by default or only on request (`-sip`); the docs describe the option,
   not the default.
5. Whether any ASTAP version emits `PC` rather than `CD`.
6. The magnitude of the differential-refraction gradient across a 4.5° field at low altitude.
7. The tracking-alt-az field-rotation formula in the folklore (`ω·cosφ·cosA/cos h`) — deliberately
   *not* used here; our fixed-camera result `ω·sinδ` was verified numerically instead.
8. Ekos PA UI specifics (docs page did not render as text).

**Primary sources**

* ASTAP manual, incl. "Viewer, reported angle", CD/CROTA notes, SIP section: <https://www.hnsky.org/astap.htm>
* Han K. on parity/CROTA in headers: <https://www.cloudynights.com/forums/topic/972648-fits-headers-and-field-rotation/>
* SIP convention: <https://irsawebops2.ipac.caltech.edu/data/SPITZER/docs/files/spitzer/shupeADASS.pdf>; astropy note: <https://docs.astropy.org/en/stable/wcs/note_sip.html>
* Parallactic angle: <https://en.wikipedia.org/wiki/Parallactic_angle>; GBT memo on parallactic-angle rates near the zenith: <https://library.nrao.edu/public/memos/gbt/GBT_052.pdf>; MMA positional-astronomy page (alt/az + refraction pseudocode): <http://kofa.mmto.arizona.edu/astronomy/altaz.html>
* SharpCap PA (method + UI + audio): <https://docs.sharpcap.co.uk/4.1/9_PolarAlignment.htm>
* N.I.N.A. TPPA FAQ (target circles, tolerances, manual mode): <https://github.com/isbeorn/nina.plugin.polaralignment/blob/master/PolarAlignment/FAQ.md>
* Ekos PA source (correction graphics, az/alt errors): <https://api.kde.org/legacy/kstars/html/polaralignmentassistant_8h_source.html>
* PoleMaster manual: <https://www.qhyccd.com/uploadfile/2018/1225/20181225042450636.pdf>
* PS Align Pro: <https://astro.ecuadors.net/polar-scope-align/>
* Drift alignment, incl. refraction caveat: <https://canburytech.net/DriftAlign/>
* SkySafari scope control (push-to arrows/bullseye): <https://support.simulationcurriculum.com/hc/en-us/articles/115015867607-Scope-Control-View>

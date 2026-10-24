# 07 — Alt/az reframing: implementation spec

**Status:** spec, derived from 04 (geometry, verified numerically there) and grounded in real
ASTAP output measured on 2026-10-24 (see §9). No code yet.

**Decisions this spec implements** (user, 2026-10-24):
- **Full alt/az math with per-axis error readout.** Not just an arrow — show Δalt and Δaz.
- Frame selection is a **quality gate only** (not best-N%); irrelevant here but recorded in 03.
- Python/Windows, PySide6, vendored runtime (05, 09). This spec is UI-framework agnostic.

---

## 0. The one-paragraph version

We solve a frame, so we know exactly where the camera is pointing and how it is rolled. The user
wants a target in the middle. The correction has two representations: in **sky/image pixels** and
in **alt/az degrees**. Both are the same vector seen from different angles, and the rotation
between them is `ψ = PA_y − q` — the camera's roll (`PA_y`, from the WCS) minus the parallactic
angle (`q`, from geometry). Compute the error in alt/az for the human, rotate it into image axes
for the arrow, and **assert the two agree** — because that assertion is a free unit test that
catches every sign error you will ever make in this file.

---

## 1. Inputs

| Input | Source | Notes |
|---|---|---|
| `CRVAL1`, `CRVAL2` | ASTAP `.ini` | RA/Dec of the frame centre, **degrees**, J2000. |
| `CRPIX1`, `CRPIX2` | ASTAP `.ini` | Reference pixel = image centre, 1-based. |
| `CD1_1 … CD2_2` | ASTAP `.ini` | deg/px. **The only orientation source you should use.** |
| Image `width`, `height` | frame | px. |
| Target `α_t`, `δ_t` | user/catalogue | J2000, degrees. |
| Site `φ`, `λ` | config | latitude + **longitude, east positive**. |
| `t` | UTC now | **UTC only.** Never local time. |

⚠️ **Trap found in the wild (2026-10-24).** When ASTAP crops a frame to satisfy a star-database
field limit, `CRVAL` describes the **cropped sub-image**, not the frame the user shot. A real
8.53° field solved against D80 (5.14° limit) came back with a solution for a cropped centre. If
you feed that into reframing, the arrows point at the wrong patch of sky. Detect it: compare
`sqrt(CD1_1² + CD2_1²) × width` against the expected field from the header, and treat a
mismatch >10% as "solution describes a crop — reframing unreliable".

⚠️ **Never use `CROTA1`/`CROTA2`.** They are present, they are close to `−PA_y`, and they are a
trap. On a real solved frame `CROTA2 = −178.036°` while `PA_y = 178.051°`. Derive orientation
from the CD matrix only (§2.1).

---

## 2. The forward chain

Every step below must be implemented with the stated sign convention. Sign errors here produce
arrows that look plausible and are 180° wrong, which is worse than no arrow.

### 2.1 Camera roll from the WCS

```
PA_y = atan2(CD1_2, CD2_2)        # degrees, mod 360
```

Note it is **`CD1_2` and `CD2_2`** (column 2), not `CD2_1`. In FITS terms `PA_y` is the position
angle of the image's `+y` axis, measured East of North. Cross-check: `PA_y ≈ −CROTA2` for
well-behaved solutions.

```
pixel_scale_rad = sqrt(CD1_1² + CD2_1²)          # deg/px, uses column 1 for +x
parity          = sign(CD1_1·CD2_2 − CD1_2·CD2_1)
```

`parity` is **−1 for a normal FITS solution** (RA increases leftward). It is `+1` only for a
mirrored solution. All arrow logic below assumes the normal case; if `parity == +1`, flip the
sign of both `x` and `y` offsets and log a warning. Recovering a mirror flip by guessing is not
worth it — solve a different frame.

### 2.2 Where the camera is pointing, in alt/az

```
sin h_c = sin φ · sin δ_c + cos φ · cos δ_c · cos H_c
H_c     = atan2( −sin A_c · cos h_c ,  sin h_c·cos φ − cos h_c·cos A_c·sin φ )   # inverse form; prefer:
H_c     = LST − α_c                                # direct, and what you should actually use
A_c     = atan2( −cos δ_c · sin H_c ,  sin δ_c·cos φ − cos δ_c·cos H_c·sin φ )
h_c     = asin( sin φ·sin δ_c + cos φ·cos δ_c·cos H_c )
```

- `A` is azimuth **from North, increasing toward East** (i.e. N=0°, E=90°, S=180°, W=270°).
- `h` is altitude above the horizon, degrees.
- `LST = GMST(UTC) + λ` (east-positive longitude), then `H = LST − α`.
- Compute GMST from UTC with the standard IAU 1982/Meeus expression. This is the whole reason
  UTC and east-positive longitude are mandatory inputs: timezone/DST handling is where every
  astro app gets this wrong, and it is silently wrong for half the year.

### 2.3 Where the target is, in alt/az

Same transforms with `(α_t, δ_t)` → `(A_t, h_t)`.

### 2.4 Per-axis error — and the `cos h` question

```
Δalt = h_t − h_c
Δaz  = wrap180( A_t − A_c )
```

Now the important part. These are **coordinate differences, not angular distances.** Azimuth is
an angle in the horizontal plane; a change of `Δaz` in azimuth displaces the pointing on the sky
by only `Δaz · cos h`. Near the horizon those are the same; near the zenith they are wildly
different.

**Show the user `Δaz_sky = Δaz · cos h_c`, labelled as an on-sky angle.** Reasoning:

- The user's goal is "put the target in the middle of the frame". What matters is how far the
  target must move *across the field*, which is the `cos h`-weighted quantity.
- The pan knob, on a level tripod, rotates the pointing by exactly `Δaz` in azimuth. If you show
  raw `Δaz`, then at `h = 85°` you are telling the user to turn the knob 11× further than the
  effect they will actually see, and they will overshoot and oscillate.
- So: **`Δaz_sky` is the error to display; `Δaz` is only interesting as a "how far to turn the
  knob" aside**, and only worth showing when `cos h` is not near zero.

Keep both internally. Display `Δaz_sky`. If you ever add a "turn the knob N°" hint, use raw
`Δaz` and say so explicitly.

### 2.5 The rotation into image axes

```
q   = atan2( sin H_c ,  tan φ · cos δ_c − sin δ_c · cos H_c )    # Meeus; mod 360
ψ   = PA_y − q
```

`q` is the parallactic angle: the angle at the object between the hour circle and the vertical.
`ψ` is therefore the camera roll **relative to the local horizontal frame** — the exact rotation
between the alt/az tangent basis and the image axes.

Then the target's offset in image pixels' *directions*:

```
x_sky = (−Δaz_sky) · cos ψ + ( Δalt) · sin ψ
y_sky = ( Δaz_sky) · sin ψ + ( Δalt) · cos ψ
```

Conventions: `x` increases to the image **right**, `y` increases **up** (image plane, not array
row order — flip for display). Signs follow 04 §1.3.

Why `ψ` is not optional: with an upright camera (`PA_y = 0`) at `φ = +45°`, `ψ` ranges from
roughly 0° to +121° over the sky. "Image up = sky up" is wrong by more than 45° across most of
the sky.

---

## 3. Degeneracy and conditioning — quantify it, don't hand-wave

The alt/az → image-plane Jacobian, in the alt/az tangent frame, is essentially
`diag(cos h, 1)` before the rotation by `ψ`. So:

```
condition_number(h) ≈ 1 / cos h
```

| `h` | `cos h` | condition | what it means |
|---|---|---|---|
| 0° | 1.000 | 1.0 | healthy |
| 45° | 0.707 | 1.4 | fine |
| 70° | 0.342 | 2.9 | fine |
| 80° | 0.174 | 5.8 | getting touchy |
| 85° | 0.087 | 11.5 | pan knob 11× less effective than it looks |
| 88° | 0.035 | 28.7 | unusable as a per-axis instruction |
| 90° | 0.000 | ∞ | azimuth undefined |

**Policy:**

| Condition | UI behaviour |
|---|---|
| `h_c < 80°` | full per-axis readout |
| `80° ≤ h_c < 87°` | show readout **plus** "near zenith — pan moves more than the numbers suggest"; grey the Δaz row |
| `h_c ≥ 87°` | **suppress per-axis azimuth entirely.** Show the pixel-space arrow only, with "pointing near zenith, use the arrow". |
| `\|δ_c\| > 85°` | warn "fast field rotation": `q` and `PA_y` drift at ~15°/hr, and the field rotates. **Recompute `ψ` from a fresh solve every iteration; never cache it across minutes.** |
| `\|δ_c\| < 5°` | note "fastest sky drift" — translation dominates re-aim cadence (04 §3). |
| error > 30° | not a nudge: "target is not near this pointing — re-aim from scratch or check the target". Do not draw an arrow; a 30°+ arrow is meaningless on a ~4° frame. |

The last row matters: an arrow longer than the frame is a *wrong* arrow. Beyond ~half a frame,
switch messaging from "nudge" to "re-aim".

---

## 4. Tolerance and success criteria

Human repeatability on a 3-way head (04 §2.1): **geared head ≈ 2–5′**, ball head ≈ 20–60′.
So demanding sub-arcminute centring is asking for the impossible and will make the app feel
broken.

**Tolerance policy** (TPPA-style):

```
tol = max(5 arcmin, 0.05 × frame_height_deg)
```

Worked: 200 mm on APS-C (4.3 µm px), `PS = 206.265·4.3/200 = 4.43″/px`, frame height
3464 px → `4.43·3464/3600 = 4.26°`. `0.05·4.26° = 0.213° = 12.8′`. So `tol = 12.8′` — spend
the accuracy on the wide end where it is achievable.

Pixel equivalents at `tol = 5′`:

| FL (APS-C, 4.3 µm) | ″/px | 5′ in px | frame height |
|---|---|---|---|
| 50 mm | 17.7 | 17 px | 17.0° |
| 135 mm | 6.6 | 45 px | 6.3° |
| 200 mm | 4.4 | 68 px | 4.3° |
| 400 mm | 2.2 | 136 px | 2.1° |

**Declare success** when the error is inside `tol`; **auto-advance** the UI to "aligned, start
capturing" without requiring a click. Show a green ring. This is the single biggest UX win in
the whole reframing feature — the user stops fiddling and starts imaging.

---

## 5. Refraction: quantify, then skip it

Bennett/Saemundsson apparent correction:

```
R_arcmin = 1.02 / tan( h + 10.3/(h + 5.11) )        # h in degrees, valid h > 0
```

| `h` | R |
|---|---|
| 90° | 0.0′ |
| 45° | 1.0′ |
| 30° | 1.7′ |
| 20° | 2.6′ |
| 10° | 5.3′ |
| 0° | 34′ |

Both the camera pointing and the target are refracted, so what matters is the **differential**,
which over a ~4° separation is well under 0.2′ — negligible against a 5–13′ tolerance.

**Decision: do not apply refraction to the per-axis error.** Do apply it if you ever display an
*absolute* altitude, and warn below `h = 20°` where `R > 2.6′` starts to eat the tolerance on its
own ("low altitude — atmospheric refraction and extinction both significant").

Nutation and aberration: ≤20″ and ≈20″ respectively, i.e. ≤0.3′. **Ignore.** (Aberration is
already absorbed because the catalogue and the solve are both J2000.)

---

## 6. Readout spec (the UI)

Four things on screen, in this priority order:

**1 — The arrow** (primary; pixel space, authoritative). Offset vector scaled to the preview,
with a tolerance ring drawn at `tol` in the same units. Hide it when the error is under
tolerance, when `> 30°`, or when the target is off-frame.

**2 — Per-axis numbers** (the thing the user asked for):

```
        Δalt   +0.42°   ( 25′ up   )
        Δaz    −1.07°   ( 64′ left )    on-sky, cos(alt) corrected
        total   1.15°   ( 69′ )         1.3 × tolerance
```

Rules:
- **Arcminutes in the primary display, degrees in parentheses** (or vice versa) — arcminutes are
  the natural unit at these tolerances and degrees read better for the big corrections. Pick one
  convention and keep it.
- Always show the **sign** plus a **word** ("up"/"left"). Signs alone are ambiguous to a human
  because "which direction is +az" is not obvious.
- Show **total** and **ratio-to-tolerance**. The ratio is what makes the progress feel real.
- Wording must be relative to **the image / the view**, not the sky: "move camera left" means
  the *target* moves right. State it once in the UI ("arrows show where the target will move")
  and keep it consistent. This is the #1 source of user confusion in every push-to app.

**3 — A bullseye / crosshair panel** for fine work: target dot in a tolerance ring, TPPA-style.
Draw it in the **image plane** (post-`ψ` rotation), not the sky plane, because the user is
looking at the image and the ring must match the arrow.

**4 — Status text**: solution age ("solved 1.2 s ago"), field rotation warning, degenerate-condition
warning, refraction warning at low altitude.

Handling "target not in the current frame": don't draw an answer you don't have. Say
**"target is 8.4° away — outside this frame. Point roughly 8° N and 3° E, then re-solve."** Give
a coarse instruction plus a cardial direction, and go back to blind-ish solving (`-r 30`) until
it lands in frame.

---

## 7. Reconciling with the pixel-space arrow

Brief 04 recommended a pixel-space closed loop as primary; the user chose full alt/az readout.
Both, with a clear hierarchy:

| Role | Authority |
|---|---|
| **Arrow, tolerance ring, bullseye** | **Pixel space** — direct WCS world→pixel of the target. Always correct, no `ψ`, no parallactic angle, no degeneracy. |
| **Δalt / Δaz numbers, warnings, explanations** | **Alt/az** — this is the *labelling* layer. It tells the user what they are doing in physical terms. |
| **Which wins on disagreement** | **Pixel space.** If they disagree by more than a pixel, there is a bug, and the arrow is the one to trust. |

Compute both, **assert agreement**, and log loudly on mismatch:

```
# independent route: world → pixel using the WCS directly
(px_t, py_t) = wcs_world_to_pixel(α_t, δ_t)
dx_px = px_t − CRPIX1
dy_px = CRPIX2 − py_t                    # image y-up
# alt/az route rotated through ψ, converted to px with the pixel scale
dx_px2 = x_sky / pixel_scale_deg
dy_px2 = y_sky / pixel_scale_deg
assert |dx_px − dx_px2| < 1 and |dy_px − dy_px2| < 1
```

This assertion is the highest-value line in the feature. It catches: `ψ` sign errors, `q` sign
errors, `PA_y` using `CD2_1` instead of `CD1_2`, degrees-vs-radians, azimuth from South instead
of North, `cos h` applied twice, and parity flips. Every one of those bugs produces a
plausible-looking arrow, and all of them are caught here.

For a small field the two routes agree to well under a pixel. On a wide field (tens of degrees)
the tangent-plane approximation itself starts to matter, so relax the tolerance to
`1 px + 0.001 × width` or similar, and note that the pixel route is the correct one.

---

## 8. Validation plan (no hardware needed)

Synthetic WCS + known geometry. Build a test harness that, for each case, constructs a CD matrix
at a chosen `PA_y` and scale, then checks the invariants.

**Invariants**

1. **Round-trip alt/az**: `(α,δ) → (A,h) → (α,δ)` reproduces the input to <0.1″.
2. **Agreement**: pixel route == alt/az route (§7), < 1 px.
3. **Zero error**: `target == CRVAL` ⇒ `Δalt = Δaz = 0` and zero-length arrow, for all `ψ`.
4. **Rotation only**: rotate `PA_y` by 90° ⇒ the arrow rotates by 90° and its magnitude is
   unchanged.
5. **`ψ` identity**: at `φ = δ_c` and `H = 0` (on the meridian), `q = 0` so `ψ = PA_y`.
6. **Parity**: flipping the CD matrix sign pattern flips both arrow components.
7. **Degeneracy guard**: at `h = 89°`, the azimuth row is suppressed, not printed as a huge
   number.

**Test matrix** — the cross product is large, so fix a representative grid:

| Axis | Values |
|---|---|
| latitude `φ` | 0°, 45°, 70° |
| azimuth `A` | 0°, 90°, 180°, 270° |
| altitude `h` | 10°, 45°, 85° |
| declination `δ` | −80°, 0°, +45°, +89° |
| `PA_y` | 0°, 45°, 137°, −70° |
| scale | 2.2″, 4.4″, 17.7″ /px |

Plus the **regression case from real data** (§9) — assert the known `PA_y` and a known target
offset.

---

## 9. Ground truth measured on this machine (2026-10-24)

Real ASTAP output, so the spec is anchored rather than theoretical.

**Frame A** — `bodes_stack.fits`, Canon 60D at 384.7 mm, 2937×1958, Siril stack of 153×15 s:

```
CRPIX1=1469   CRPIX2=979.5
CRVAL1=149.402648  CRVAL2=69.226142
CDELT1=-6.3803869144777670E-004   CDELT2= 6.3822425813758624E-004
CD1_1= 6.3766968388110333E-004   CD1_2= 2.1696682773295344E-005
CD2_1= 2.1874558883785833E-005   CD2_2=-6.3784928262788855E-004
CROTA1=-178.051   CROTA2=-178.036
```

Derived per §2.1: `pixel_scale = 6.3804e-4 °/px = 2.297″/px`; `PA_y = atan2(2.1697e-5, −6.3785e-4)
= 178.05°`; `parity = −1` (normal). Cross-check passes: `PA_y ≈ −CROTA2`.

Field: `1958 × 2.297″ = 1.249°` tall. Tolerance `max(5′, 0.05·1.249°=3.75′) = 5′` = 131 px.

Use this frame as the regression fixture: at `φ = +39.16°`, `δ_c = 69.23°`, the camera is rolled
essentially 180° from north-up, so `ψ` will be large and any sign error in `q` will be obvious.

**Frame B** — `na_light.fit`, 100 mm, 180 s, 5202×3464, 8.53° tall. ASTAP solved it only after
cropping to 6.33° (D80 ceiling), and emitted
`WARNING=Warning scale was inaccurate! Set FOV=8.62d, scale=9.0", FL=99mm`.

**Lesson for this feature:** that frame's `CRVAL` refers to a **crop**, so reframing off it would
be wrong by the crop offset. This is exactly the guard in §1 — and it was found by accident on
real data, which is why the guard exists.

Also relevant: the user's `north-america` frames were shot at 100 mm, 180 s, i.e. **tracked**. The
drift model in 04 §3.1 (`translation ω·cos δ`, rotation `ω·sin δ`, latitude-independent) applies
to the untracked case this app targets; do not carry those rates over to tracked sessions.

---

## 10. Open questions

- Exact head geometry: is the pan axis truly vertical on the user's tripod, and does the head
  have a separate tilt axis that couples to azimuth once tilted? A geared 3-axis head does not
  decompose into clean pan/tilt when tilted. If it doesn't, the per-axis readout should degrade
  to the arrow only once the head is off-level — which argues for a **"level the tripod"**
  pre-flight check and for measuring the tripod tilt (phone sensor or a level) and folding it
  into the alt/az basis.
- Whether to display knob-rotation amounts (raw `Δaz`) or only on-sky angles. Currently: on-sky
  only, with raw shown as an aside near the zenith.
- Empirically verify the `ψ` drift rate at high declination to size the "recompute every N
  seconds" policy (04 predicts ~15°/hr of field rotation).
- SIP: ASTAP writes SIP into `.wcs` by default (measured), but the `.ini` carries no SIP terms
  and the CD matrix alone is linear. For narrow fields the linear solution is sufficient; for
  wide/uncorrected lenses the pixel route in §7 will drift at the frame edges. Decide whether we
  ever need to consume the `.wcs` SIP terms — likely yes once fields exceed ~10°.

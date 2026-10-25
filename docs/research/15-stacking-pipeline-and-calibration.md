# 15 — The live stacking pipeline, and where calibration goes

**Status:** design, 2026-10-25. Supersedes the stacking *implementation* choice in brief 06 §5 (the
hybrid/Siril-as-closer debate) for the live path; Siril remains the end-of-session closer.
Consumes 03 (§3, §6), 06 (§5), 10 (§3.2), 14 (§4).

**Decisions taken here:**

1. The per-frame estimator is deliberately dumb — sum + count + a σ gate. Justified in §1.
2. Calibration is **flats at the end**, **no per-session darks**, **no bias** (§7).
3. Calibration is applied **per-frame in sensor coordinates, before any warping** (§7.3). This is
   specific to untracked data and is not an optimisation detail — getting it wrong is a bug.
4. The accumulator is `(sum, count, M2)` at display resolution, never `acc/N` (§3).

---

## 0. Summary

| Question | Answer |
|---|---|
| Is "pixel averaging plus stretching" enough? | **Yes** — because the transform is known analytically, not estimated (§1). |
| What is *not* optional? | per-pixel count map, per-pixel σ gate, per-frame sky subtraction (§2). |
| Where does the stretch live? | Computed once at frame 5, then **frozen** (§6). |
| Darks per session? | **No** — the payload is a defect map, which the count map already provides (§8). |
| Flats? | **Yes, at the end**, terminal and abortable (§7.1). |
| Bias? | Never; darks would include it, and we are not taking darks (§8.4). |

---

## 1. Why the estimator is allowed to be simple

A conventional stacker needs star detection because it has to *estimate* the transform, *verify* it,
and recover when estimation fails. We are not in that position: brief 10 §3.2 gives the transform
analytically from the frame timestamps — rotation by `Ω·Δt` about the projected pole, plus
translation. Both come from the ASTAP solve at `POINT` and the fixed-camera model.

```
no estimation  ⇒  no per-frame failure mode  ⇒  no need for a robust-but-expensive estimator
```

**Pay for the geometry once; the statistics can stay trivial.** Consequence for dependencies: the
live path needs **no SEP and no astroalign** — they survive only as the fallback for the one case
that breaks the model, which is the user nudging the tripod (§4).

---

## 2. The per-frame pipeline

Order matters, and the ordering is set by §7.3.

```
CR2 bytes (from the camera, in memory)
  └─ decode                          libraw
  └─ subtract black level            once, constant          [sensor frame]
  └─ super-pixel bin 2x2             bin AND demosaic in one  [sensor frame]
  └─ divide by binned master flat    guarded, normalised     [sensor frame]
  └─ subtract per-frame sky           subsampled median      [sensor frame]
  └─ warp by predicted similarity    cv2.warpAffine + ones-mask warp
  └─ σ gate                          Welford, k≈4
  └─ accumulate                      sum, count, M2
     ↓ every frame (or every Nth)
  └─ mean = sum/count → fill holes → STF → downscale → JPEG → UI
```

Everything above the warp operates in the **sensor frame**. Everything below it operates in the
**reference frame**. That boundary is load-bearing.

### 2.1 Super-pixel binning does two jobs

2×2 super-pixel binning (average the 2×2 of each Bayer colour into one RGB pixel) performs the
**downsample and the demosaic in a single reshape-and-mean**. No `cvtColor`, no interpolation
artefacts, no half-pixel colour offsets rotating against each other frame to frame. It is also the
cheapest correct path.

Full frame 5202×3464 → **2601×1732 (4.5 MP)**.

Two decode levers combine with it:

| path | libraw | after super-pixel | cost (SBC) |
|---|---|---|---|
| full decode | 5202×3464 | 2601×1732 · **4.5 MP** | ~1.0 s |
| **half-size decode** | 2601×1732 | 1300×866 · **1.1 MP** | **~0.3 s** |

⚠️ **Accumulator resolution should follow the client's display, not be fixed at 2K** (14 §9). A
phone is ~1.2 K wide, so the 1.1 MP path is both the cheapest *and* the display-matched one. Default
half-decode on the box, full-decode on a laptop.

### 2.2 Per-frame cost

| step | @1.1 MP (SBC) | @4.5 MP (laptop) |
|---|---|---|
| decode | 0.15 s | 0.20 s |
| black level + bin | 0.05 s | 0.04 s |
| flat divide | 0.01 s | 0.01 s |
| subsampled median | 0.01 s | 0.01 s |
| warp + ones-mask warp | 0.01 s | 0.02 s |
| σ gate + accumulate | 0.02 s | 0.03 s |
| STF stretch | 0.03 s | 0.06 s |
| JPEG encode | 0.01 s | 0.02 s |
| **total** | **~0.30 s** | **~0.40 s** |

Both sit far inside the 1.5–4.5 s of camera-wait per frame that 14 §4.1 identified, so the stacker
is invisible in the cadence on either path. [D] from 03 §7 and 06 §4 measurements, scaled.

⚠️ **The median must be subsampled** (e.g. every 16th pixel). A true median of 4.5 MP is ~200 ms for
no benefit — the sky level is a single scalar.

---

## 3. The accumulator

Three planes, float32, at accumulator resolution:

```python
sum_px  += warped           # only where mask is valid
count   += mask             # 0 or 1
m2      += ...              # Welford M2 for the per-pixel variance
```

### 3.1 Never divide by N

`mean = sum_px / N` is **wrong**, and wrong in a way that grows with time. Under rotation and
translation, edge pixels receive fewer contributions than central ones, so dividing by the frame
count paints structured darkening into the corners that *accumulates as the field rotates*.

`mean = sum_px / count` (guarding `count == 0`) is the whole fix, and the count map is also the
dispatch for §8.2.

### 3.2 The σ gate

Welford gives the per-pixel variance for free at O(1) memory — no ring buffer, no frame storage
(03 §6.1–6.2). Reject at `|x − mean| > 4σ` **without incrementing the weight**, so the mean stays the
mean of *accepted* values. Track "seen" separately if a coverage readout is wanted.

A persistent artifact is rejected forever (it never enters, so it never shifts the mean toward
itself). A transient event admitted before σ has converged is diluted by large N. Both are fine.

### 3.3 Hole filling

`count == 0` means a pixel was rejected in every frame — which is precisely the definition of a
defect (§8.2). Fill from valid neighbours with a normalised convolution, three lines and no library:

```
fill = box_filter(sum_px) / box_filter(count)   # over valid pixels
out  = where(count > 0, sum_px/count, fill)
```

### 3.4 Re-anchoring

Rotation accumulates, so the valid region shrinks and eventually the canvas is wrong.

- **v1:** show the shrinking inscribed rectangle as a HUD. It is honest, simple, and it visualises
  *why* the session has a length — which is a feature, not a workaround.
- **Later:** re-anchor every K frames by warping the accumulator *and* count map into the current
  frame's frame (~5 ms).

---

## 4. One cheap operation, three jobs

`cv2.phaseCorrelate` on consecutive previews returns:

1. **sub-pixel refinement** of the predicted translation (~10 ms at 1 MP);
2. **the frame-quality gate** — 06 §5's point: *"did the residual disagree with the prediction" is a
   far sharper gate than star count*, because cloud, a bump and a trail all break the prediction
   while star count may not;
3. **drift-model validation** — a small residual across 50 frames confirms the pole model; a step
   means something physical happened.

So frame-quality logic is one function, not a cascade. And a residual too large to be refinement is
the trigger for the astroalign fallback — the *only* place it is still needed.

---

## 5. Frame rejection policy

Reject (do not accumulate), with the reason recorded for the UI:

| symptom | test |
|---|---|
| cloud / transparency drop | background RMS deviates from running median beyond k |
| trail, satellite, aircraft | elongation, or residual disagreement |
| tripod bump | residual step exceeding the model's tolerance |
| badly defocused | HFD beyond 1.3× running median (03 §4.2) |

Weighting by `1/σ_f²` is nearly free once the background RMS is in hand, but it complicates the
Welford update. **Defer to v2**; the gate plus plain accepted-frame mean is the v1 answer. Consistent
with the standing "quality gate only, no best-N%" decision.

---

## 6. Display

1. **STF autostretch** (03 §6.4): `shadows = median − 2.8·1.4826·MAD`, then the MTF solve. ~63 ms at
   1.5 MP, asinh ~26 ms as a fallback.
2. **Freeze it.** Compute on frame 5, then hold. Re-autostretching every frame makes the image pulse
   between frames and destroys the only feedback the stacking offers — watching noise fall. Provide
   an explicit "restretch" action instead.
3. Downscale to the client's width, JPEG at ~85. 1 fps × ~150 KB ≈ 1.2 Mbit/s, which 14 §5 already
   established is a non-issue over the hotspot.
4. HUD: frames accepted / rejected / why, integration time, HFD, coverage, and the shrinking valid
   rectangle from §3.4.

---

## 7. Calibration

### 7.1 Flats go at the end

The wizard order is `PREFLIGHT → TARGET → PARAMETERS → FOCUS → CALIBRATE(head) → POINT → RUN`. Three
reasons for a trailing flat step, one of which decides it:

- **Optical state is correct.** A leading step would run *before* `FOCUS` — at the wrong focus, and
  before the zoom ring is where it will stay.
- **Dust shadows are this session's.** A reused flat library goes stale the moment a mote lands.
- **Decisive: startup latency costs far more than teardown latency here.** Brief 12 budgets ~2 min
  to first frame and the tool's identity is "see something immediately"; 60–90 s of flats in front
  taxes every session. Trailing, the step is **optional and abortable at zero cost** — aborting it
  costs only flats, whereas aborting a leading step costs the session.

### 7.2 The live stack does not need them

The objection "we can't use them during live stacking" mostly dissolves:

- The live stack's job is framing, focus confirmation and watching noise fall — not final quality.
  The STF stretch is robust to a smooth gradient, so vignetting is nearly invisible in a preview.
- **Untracked rotation partially self-flattens the vignette for free.** The sensor-fixed vignette
  rotates *under* a sky-fixed field, so the accumulator holds a rotationally-averaged vignette —
  flatter than any single frame. This is the "free dithering" of 03 §3.5.

  Exception: dust donuts smear into **arcs** rather than averaging away, so the final image still
  needs real flats.

The final full-res stack already happens offline from saved originals (14 §4.4), and that is where
the flats are applied.

### 7.3 The constraint that is ours alone

⚠️ **Calibration must be applied per-frame, in sensor coordinates, before any warping.**

Vignetting and hot pixels are bolted to the sensor. After a frame is warped into the accumulator its
sensor pattern has rotated with it. Therefore:

- flat-correcting the **accumulator** is wrong twice over — it applies a sensor-fixed pattern to a
  sky-fixed image, *and* it re-applies vignetting the stack already averaged down;
- **a tracked imager never hits this**, because their field does not rotate.

Consequence: **bin the master flat exactly the way frames are binned** (2×2 super-pixel → the same
2601×1732), then divide after binning. Binning is linear so the order is free. This is why §2 puts a
hard boundary between sensor-frame and reference-frame work.

### 7.4 Flat practicalities

| Item | Value |
|---|---|
| Count | 20–30, ISO matched to the lights, histogram 50–70% |
| Cost | ~30 s total at 0.5–2 s each — trivially fits teardown |
| Normalisation | divide by its own median → mean 1.0 |
| **Guard** | **floor at ~0.2 after normalisation** — a dead pixel is near-zero in a flat and an unguarded divide produces infinities/NaNs |
| Source ≤100 mm | entrance pupil is 22 mm — a phone screen works |
| Source at 400 mm | pupil is **71 mm** — a phone will not cover it; t-shirt + torch, or a laptop screen |
| Freshness | invalidated by any change to focus, zoom, aperture, or a new dust mote |

Preflight should state which source the chosen focal length requires. All of these cost nothing,
which keeps the "no purchase may be required" rule intact. Lock the lens's tension ring.

⚠️ **Flats must be stored with metadata** (ISO, aperture, focal length, focus position from EXIF) so
the offline restack can verify it is matching the right ones. A flat that silently does not match is
worse than no flat.

---

## 8. Why there is no dark library

### 8.1 The decomposition

At 0.4–3 s subs, a 60D at ~20 °C carries roughly 0.1–0.5 e⁻/s dark current [D], so a 3 s sub holds
~0.3–1.5 e⁻ against ~3.3 e⁻ of read noise and *hundreds* of e⁻ of suburban sky background. Dark
current's shot noise works out to ~1 e⁻ versus 15–50 e⁻ of sky shot noise.

| Contribution | At our sub lengths | Verdict |
|---|---|---|
| Dark current mean offset | negligible against sky | irrelevant |
| Bias / pedestal | a constant; the STF already normalises it | irrelevant |
| Column / row fixed-pattern noise | clean sensor; rotation smears the rest | negligible |
| Amp glow | minimal on a 60D | irrelevant |
| **Stuck / dead pixels** | permanent, temperature-independent | **the only payload** |

### 8.2 The payload is a defect map, and we already have one

A dark's real value is identifying permanently bad pixels — not subtracting anything. But the
rotating field is a perfect defect discriminator: **a hot pixel is bright at a fixed *sensor*
coordinate while stars move.**

So a pixel that is persistently rejected by the σ gate, or that ends with `count ≈ 0`, *is* a
defect. It is a statistic we compute anyway (§3.1, §3.3), it costs nothing extra, and it is
**better than a dark map** because it is specific to this session's sensor temperature and state
rather than to whatever night the darks were shot.

It also self-reports: a sensor bad enough that many pixels fail the gate says so through the count
map, instead of requiring the user to shoot darks to discover it.

### 8.3 Where it comes back

**Trigger: if we add the polar-target mode with 10–30 s subs.** Dark current then reaches 3–30 e⁻ and
genuinely matters; darks become worth shooting, **at the end**, temperature-matched, same argument as
§7.1. So this is deferred, not refuted.

### 8.4 Bias

Never needed. It is included in any dark, and we are not taking darks. If darks return (§8.3), bias
still does not — dark subtraction subsumes it at these exposure lengths.

### 8.5 Presentation

⚠️ "You do not need darks" contradicts astro convention and will read as an omission rather than a
decision. Per the README's *inform, never block*, the UI should say why — briefly, in one sentence
with the reasoning above — rather than silently omitting the feature.

---

## 9. Session state

```
PREFLIGHT → TARGET → PARAMETERS → FOCUS → CALIBRATE(head) → POINT → RUN → FLATS?
```

- `FLATS` is **terminal, optional, abortable**, and requires that nothing be touched — naturally true
  at the end of `RUN`.
- Darks and bias do not appear at all in v1.
- `CALIBRATE` in brief 12 is **head calibration** (13), not calibration frames — a naming collision
  worth resolving, e.g. `HEAD_CALIBRATE` and `FLATS`.
- Per 12 §10 the `CALIBRATE` phase is still absent from `session/state.py`; this adds a second
  reason to touch that file.

---

## 10. Dependencies

Live path: **numpy, OpenCV, libraw**. No SEP, no astroalign, no Siril.

SEP/astroalign appear only for the fallback in §4, and Siril only as the offline closer (06 §5). That
is a small enough set to package for an SBC.

---

## 11. What to measure

1. **Real libraw decode time** for a 60D CR2 at full and half size, on both the laptop and the box.
   §2.2's estimate is the largest single uncertainty in the budget.
2. **Super-pixel bin cost** — expected to be memory-bound; confirm it is not the wall.
3. **Astroalign fallback accuracy on real 60D frames** — 03 §9 flags the synthetic benchmark as
   optimistic (JPEG artefacts, hot pixels, distortion).
4. **How far the pole-model prediction drifts over 45 min in the field** — tripod creep, mirror slap,
   focus and temperature drift. 03 §9 lists this as unknown and it is the assumption §1 rests on.
5. **Whether the frozen stretch still looks good after 30 minutes of sky-brightness change** — if
   not, the freeze needs a slow tracking update rather than a hard freeze.
6. **Flat source adequacy at 400 mm** with a t-shirt and a phone torch.

---

## 12. Open questions

- Should the stacker take the accumulator resolution from the *client* (which could reconnect with a
  different width mid-session), or fix it at `RUN` start? Fixing at start is simpler and the field
  rotation model is already anchored to it.
- Is a single scalar sky level enough, or does a strong gradient (moon near the field) need a
  low-order plane fit per frame? Plane fit is ~3× the cost of a median and may be warranted.
- Does the count-map hole filler ever visibly differ from a proper defect map? Worth an A/B on a
  known-bad sensor if one turns up.
- Do we expose the rejected frames as thumbnails (03 §8.6 argues yes) or only as counts?

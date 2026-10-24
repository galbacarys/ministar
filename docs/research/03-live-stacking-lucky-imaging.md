# 03 — Live stacking & "lucky imaging" of many short exposures, untracked DSLR deep-sky

**Scope.** No tracker (3-way head): the field rotates *and* drifts between frames. Subs 1–15 s, hundreds to thousands per session, deep-sky targets, laptop-side process that must show a progressively improving image in real time, frames arriving from a Windows process (live-view JPEG or downloaded stills).

**Labelling convention used throughout**

| Tag | Meaning |
|---|---|
| **[M]** | Measured by me for this brief (this laptop: AMD Ryzen AI 7 350, 16 threads, 15 GB RAM, Linux; numpy 2.5.4, OpenCV 5.0.0, skimage 0.26, astroalign 2.6.2, SEP 1.4.1; synthetic 6000×4000 uint16 star fields, 400 stars, FWHM 2 px, sky 900 ADU, σ=8 ADU). Benchmarks are *not* optimised production code. |
| **[D]** | Derived by me, numerically verified with an independent 3-D spherical-astronomy simulation (see §1.2, method note). |
| **[C]** | Cited (full references in §10). |
| **[U]** | **UNVERIFIED** — plausible but not confirmed in this pass. |

---

## 0. Executive summary (the load-bearing conclusions)

1. **Translation-only stacking is not "slightly wrong", it is structurally wrong.** On a fixed tripod the frame-to-frame transform is a *rotation about the projected celestial pole*. The roll rate about the frame centre is **[D]**

   **ω = Ω · sin δ<sub>b</sub>**,  Ω = 15.0411 ″/s = 7.2921 × 10⁻⁵ rad/s, δ<sub>b</sub> = declination of the boresight.

   This is *not* the widely-quoted alt-az formula `15.04·cos(lat)·cos(az)/cos(alt)` — that is the *derotator rate of a tracking alt-az mount* (= parallactic-angle rate) and it is the wrong number for a fixed tripod (§1.2 verifies both numerically). Consequence: rotation is **maximal near the pole (0.25°/min) and zero on the celestial equator**, independent of latitude; the "zenith singularity" does **not** apply to a fixed tripod.
2. **Rotation is a pixel-domain problem, not an arcsec-domain one.** For any 6000×4000 sensor the *corner* moves ~8 px/min at δ=30°, ~11 px/min at δ=45°, ~16 px/min at δ=85° — regardless of focal length. Meanwhile the *drift* is 902″/min × cos δ (= 55 px/min at 50 mm, 328 px/min at 300 mm). Over a 10-minute session at δ=45° you get ~1.8° of roll and hundreds-to-thousands of pixels of drift. Total (similarity) registration per frame is mandatory.
3. **The sub-length ceiling is set by trailing, not by skyglow.** For ≤2 px of intra-exposure trail: 50 mm → 2–3 s, 135 mm → ~0.8–1.6 s, 300 mm → 0.4–0.7 s (δ dependent). This — not seeing — is what forces "hundreds/thousands of frames", and it is why read noise (not seeing) is the enemy (§3.3).
4. **Registration cost is dominated by star detection and matching, not by resampling.** [M] `cv2.warpAffine` on a 24 MP uint16 frame: **15 ms**. SEP detection at 1.5 MP: **32–40 ms**; at 24 MP: 0.36 s typ., 3.3 s in a bad case. astroalign: **0.20 s @1.5 MP, 0.36 s @6 MP**, and it *crashes outright* on full-res 24 MP (SEP pixstack overflow, measured). ⇒ **Do all registration at ≤2 MP and warp the full-res frame with the up-scaled transform** (verified accurate to ~0.1 px at the frame corner).
5. **Recommended architecture** (§6–§8): (a) register on a 1500×1000 luminance downsample; (b) fit a **similarity** transform against a *re-anchored* reference (re-anchor every ~2–5 min, §1.4); (c) optionally exploit the *predicted-transform* trick — the transform is a rotation by Ω·Δt about the **pole's image position**, which can be calibrated from the first few star-matched frames and then verified with a cheap phase-correlation residual (§1.4) [D]; (d) accumulate with a **Welford running mean + M2 variance** (O(1) memory, per-pixel ±kσ rejection) — [M] 91 ms/frame at 24 MP float32, or 19 ms with `cv2.accumulateWeighted` for the plain mean; (e) keep a **per-pixel weight/coverage map** (mandatory once you rotate — otherwise the corners darken, exactly the artefact SharpCap documents [C]); (f) display via **STF-style MTF autostretch** on a ≤1.7 MP preview (63 ms) or asinh (26 ms).
6. **"Lucky imaging" for deep sky is mostly a category error — with one honest exception.** Deep-sky frames at amateur pixel scales are *sky-background limited*, so discarding 80–95 % of frames throws away SNR you cannot get back (§4.3). Lucky selection only pays where the pixel scale is ≲ seeing/2 (≳300–400 mm focal length) **and** the target is bright enough that shot noise is not the limit. At 14–85 mm — the only focal lengths where untracked 1–15 s subs are geometrically viable — the PSF is **diffraction-limited at ~1.75 px**, so there is nothing for lucky imaging to sharpen: the blur is optical, not atmospheric (computed in 11 §3). What *is* worth borrowing from lucky imaging is the **quality-gate** (drop cloud-affected, trailed, out-of-focus frames) and the **per-alignment-point local stacking** idea (PSS/AutoStakkert).
7. **There is exactly one category of permissively-licensed, full-featured reference stacker: DeepSkyStacker (BSD-3-Clause, C++, open since 2018).** The closest *architectural* match to "watch a folder, align, stack incrementally, display" is **Astro Live Stacker (ALS, Python/PyQt, GPL-3.0)**, which literally is `astroalign` + running mean + Qt — and whose limitations (fixed reference frame, mean-only, no weights, full-res alignment) are exactly the traps to avoid (§5).

---

## 1. The alignment problem without a tracker

### 1.1 What the sky actually does in a fixed, level camera

The sky is a rigid sphere rotating at Ω about the celestial pole; the camera is bolted to the ground, so in the *camera* frame the pattern both **translates** (rate Ω·sin θ) and **rotates/strains** (roll rate Ω·cos θ), where θ is the angular distance from the boresight to the pole, i.e. θ = 90° − δ<sub>b</sub>. Hence:

- **translation rate** |v| = Ω·cos δ<sub>b</sub> = 15.04″/s × cos δ<sub>b</sub>  [C,D]
- **roll rate about the frame centre** ω = Ω·sin δ<sub>b</sub>  **[D]**
- the composite is, to first order over a small field, a **similarity transform whose fixed point is the projected pole** [D] (§1.4).
- **differential rotation** — the second-order remainder of that map. Because the rotation axis does not pass through the projection centre, the gnomonic image of a rigid rotation is *not* exactly a similarity; the departure is a strain that grows with (rotation angle) × (field radius) and is the reason a single transform fitted over a long baseline or a wide field leaves residuals (§1.4 table). It is what breaks *translation-only* stacking even at δ≈0 where the roll is zero.

Neither term depends on latitude; latitude only enters through *which* (δ, hour angle) you can see and how fast δ<sub>b</sub> changes. The instantaneous direction of drift is perpendicular to the great circle through the pole, i.e. position angle = parallactic angle + 90°.

### 1.2 The rotation formula — and why the famous alt-az formula is the wrong tool here

**The formula you will find everywhere** (RASC Calgary, californiaskys, JDSO, and many forum posts) is [C]:

```
field rotation rate (deg/hour) = 15.0410686 × cos(latitude) × cos(azimuth) / cos(altitude)
```

its properties: zero for due east/west, "infinite" at the zenith (cos alt → 0), zero at the poles. This is the **parallactic-angle rate**, i.e. the rate at which a *tracking* alt-az telescope's focal plane must be derotated. I verified it independently [D]: numerically differentiating the parallactic angle for a tracked alt-az mount reproduces this expression to 4 decimals (sign differs by convention) at every (alt, az, lat) tested.

**It is not the rotation of the field in a fixed camera.** I then ran a full 3-D simulation: build a grid of stars on the sky (uniform in (dec, H)), project them through a fixed gnomonic camera held at constant (alt, az), advance sidereal time by Δt, re-project, and least-squares fit a similarity transform. Results for Δt = 60 s, field ±1.5°: [D]

| lat | alt | az | fitted roll (deg/min) | alt-az formula (deg/min) | **Ω·sin δ<sub>b</sub>** | similarity residual |
|---|---|---|---|---|---|---|
| 51 | 45 | 0 | −0.2493 | +0.2231 | +0.2493 (δ<sub>b</sub>=84.0°) | 0.08″ |
| 51 | 45 | 90 | −0.1378 | 0 (singular-free) | +0.1378 (δ<sub>b</sub>=33.3°) | 0.86″ |
| 51 | 45 | 180 | +0.0262 | −0.2231 | +0.0262 (δ<sub>b</sub>=6.0°) | 0.73″ |
| 51 | 70 | 45 | −0.2212 | +0.3262 | +0.2212 (δ<sub>b</sub>=61.9°) | 0.43″ |
| 51 | 85 | 180 | −0.1803 | **−1.8101** | +0.1803 (δ<sub>b</sub>=46.0°) | 0.51″ |
| 35 | 20 | 0 | −0.2421 | +0.2185 | +0.2421 (δ<sub>b</sub>=75°) | 0.19″ |
| 35 | 45 | 135 | +0.0010 | −0.2053 | +0.0010 (δ<sub>b</sub>=−0.2°) | 1.02″ |

The `Ω·sin δ<sub>b</sub>` column matches the simulation to 4 decimals in *every* case, including the near-zenith case where the textbook formula is off by a factor of 10. **The correct expression for a fixed, level tripod is therefore:**

```
ω_roll = Ω · sin δ_b                       (rad/s),  Ω = 7.292115e-5 rad/s
       = 15.0411 ″/s · sin δ_b
sin δ_b = sin φ · sin h + cos φ · cos h · cos A        (φ=lat, h=alt, A=az of the boresight)
```

**Special cases.**
- **Near the pole** (δ<sub>b</sub> → 90°): roll is maximal, 15.04″/s = 0.25°/min. Nothing pathological, but *every* star traces a circle about the pole, and the pole is usually in-frame → the classic "Polaris star-trail" picture.
- **Near the celestial equator** (δ<sub>b</sub> → 0): roll → 0 — trails are almost straight. This is the *most forgiving* part of the sky for a fixed tripod, and the reason Orion/M42 is the canonical untracked target.
- **Near the zenith:** *nothing special happens for a fixed tripod.* The "zenith singularity" is an artefact of the tracking alt-az formula. This is a genuine correction to most of the online literature, and it matters: an untracked imager pointed at the zenith is limited by drift (Ω·cos δ<sub>b</sub>) plus a large roll only if δ<sub>b</sub> happens to be large.
- **Latitude:** |ω| is latitude-independent; latitude only changes how fast δ<sub>b</sub> itself changes (at high latitude a circumpolar target has roughly constant δ<sub>b</sub>, so the roll rate is nearly constant all night).

### 1.3 Numbers you can design against

Rotation about the frame centre (identical for all focal lengths — it is a sky angle; expressed as corner displacement it depends on *pixel count*, not focal length): [D]

| δ<sub>b</sub> | roll rate | per minute | per 10 min | per 30 min | corner motion (6000×4000, r=3606 px)/min |
|---|---|---|---|---|---|
| 0° | 0.0 ″/s | 0.000° | 0.00° | 0.00° | 0 px |
| 30° | 7.5 ″/s | 0.125° | 1.25° | 3.76° | 7.9 px |
| 45° | 10.6 ″/s | 0.177° | 1.77° | 5.32° | 11.2 px |
| 60° | 13.0 ″/s | 0.217° | 2.17° | 6.51° | 13.7 px |
| 75° | 14.5 ″/s | 0.242° | 2.42° | 7.26° | 15.2 px |
| 85° | 15.0 ″/s | 0.250° | 2.50° | 7.49° | 15.7 px |

Drift and the resulting **maximum sub length** (4.0 µm pixels; trail budget 2 px): [D]

| focal length | scale | δ=0: drift / t<sub>max</sub> | δ=45° | δ=80° |
|---|---|---|---|---|
| 24 mm | 34.4″/px | 902″/min → **4.6 s** | 6.5 s | 26 s |
| 50 mm | 16.5″/px | 902″/min → **2.2 s** | 3.1 s | 12.6 s |
| 135 mm | 6.1″/px | 902″/min → **0.8 s** | 1.2 s | 4.7 s |
| 300 mm | 2.8″/px | 902″/min → **0.37 s** | 0.5 s | 2.1 s |
| 600 mm | 1.4″/px | 902″/min → **0.18 s** | 0.26 s | 1.05 s |

Five-second trail length in px (δ=45°): 24 mm → 1.6, 50 mm → 3.2, 135 mm → 8.7, 300 mm → 19, 600 mm → 39. **This is the hard constraint the whole project lives inside.** Note the tension: a 15 s sub at 50 mm is a **10–14 px** streak (δ=45°→0°; stars become dashes), and at 135 mm it is **26–37 px** — unusable. Realistically:
- **14–85 mm**: untracked 2–15 s subs are geometrically sound; the target is wide-field (Milky Way, big nebulae, constellations). This is the sweet spot.
- **135 mm**: 1–2 s subs; ~1000–3000 frames/session; read noise starts to bite (§3.3).
- **≥300 mm**: sub-second subs; you are doing planetary-style high-frame-rate work on a deep-sky target, and 24 MP of sub-second frames is a data-rate problem before it is an algorithm problem.

Drift over a 10-minute session (px, 6000-wide frame): 50 mm → 547 px (δ=0) / 387 px (δ=45°); 135 mm → 1477 / 1044 px; 300 mm → 3281 / 2320 px. **The target leaves the frame within a single session** unless the accumulator follows the sky (§6.3).

### 1.4 Which transform, and how good is "good enough"?

Fit **similarity** (4 dof: tx, ty, θ, s) per frame. Full **affine** (6 dof) buys almost nothing; **homography** is unnecessary. Measured failure of simpler/nastier models, max residual over the field after the stated elapsed time: [D]

| target | elapsed | field | translation-only residual | similarity residual | affine residual |
|---|---|---|---|---|---|
| δ≈6° | 1 min | ±1.5° | 3.7″ | 0.7″ | 0.7″ |
| δ≈31° | 1 min | ±1.5° | 17.4″ | 0.6″ | 0.6″ |
| δ≈62° | 1 min | ±1.5° | 29.6″ | 0.4″ | 0.4″ |
| δ≈80° | 30 min | ±1.5° | 988″ | 4.2″ | 4.0″ |
| δ≈62° | 30 min | ±5° | 2985″ | 163″ | 147″ |
| δ≈6° | 30 min | ±10° | 705″ (±5°) | 1063″ | 1019″ |

Three conclusions:
1. **Per-frame similarity is excellent over short baselines** (≲1″ for a 3° field at 1 min) — the per-frame registration problem is easy.
2. **Translation-only fails immediately** at high declination and slowly at low declination — the failure mode is not "slightly soft stars", it is 30–1000″ long streaks.
3. **The error grows with the *time baseline* and the *field size*, and affine does not fix it** — the residual is gnomonic-projection curvature (the exact map is a rotation about an off-plane point), not shear. Practical rules:
   - **Re-anchor** the reference frame every 2–5 min (warp the accumulator into the new frame's frame; one warp of the accumulator, 15 ms).
   - For FOV ≳ 10° and sessions ≳ 15 min, consider 3×3 **tile-wise local transforms** (the PSS "alignment point" trick [C]) or a full SIP/projective model. [U] whether the extra complexity is needed in practice for a 50 mm lens at 5 min re-anchor intervals.

**Predicted-transform ("pole model") registration [D] — worth prototyping.** Since the fixed-tripod transform is a rotation by Ω·Δt about the projected pole, and Ω is known to 1e-6, the entire transform sequence is described by ~4 numbers (pole x, y, scale≈1, phase). Verified [D]: the fixed point of the fitted similarity coincides with the projected pole to ≲0.2 % of its coordinate when the pole is in front of the camera (e.g. fixed point (1.6521, 0.5692) vs pole (1.6491, 0.5774) in tan units at lat 35/alt 60/az 270). Consequences:
- Calibrate from the first 2–3 star-matched frames by least squares, then **predict** each subsequent transform from its timestamp — zero star detection per frame.
- Verify each frame with a *tiny* phase-correlation residual on a centre crop (or on the brightest N stars only) → a few ms.
- A sudden residual jump = tripod kicked/refocused → re-solve automatically.
- Caveat [U]: lens distortion/BAYER/rolling-shutter effects and physical tripod creep break the model slowly; a re-fit every ~60 frames is cheap insurance. Also this assumes the tripod is *stationary*, which is the whole premise.

---

## 2. Registration algorithms that actually get used

### 2.1 Star detection (the cheap, robust part)

| Detector | Where | Algorithm | Notes |
|---|---|---|---|
| `sep.extract` / `sep.Background` | SEP (LGPL-3.0), SExtractor core [C] | mesh background + thresholded connected components + deblending | Fastest practical option; astroalign uses it. **[M] 32–40 ms @1.5 MP, 0.12 s @6 MP, 0.18 s @13.5 MP, 0.36 s (typical) – 3.3 s (worst) @24 MP.** Needs `set_extract_pixstack()` on big/noisy frames. |
| `photutils.DAOStarFinder` | photutils (BSD-3) [C] | DAOFIND: LoG/convolution + local maxima + Gaussian moments | Pure Python/numpy; slower than SEP [U by factor 3–10], but no C dependency beyond numpy. Returns sharpness/roundness → free quality metrics. |
| `photutils.detect_sources` | photutils (BSD-3) | segmentation (SExtractor-like) | Good for faint/extended fields. |
| DoG / LoG + peak finding | OpenCV / skimage | `cv2.SimpleBlobDetector`, `skimage.feature.blob_log` | Fine, but you must implement sub-pixel centroid; no advantage over SEP. |
| DSS's approach | DSS (BSD-3) [C] | "round object whose luminance decreases monotonically, r ≤ 50 px"; Gaussian centroid; **rejects elongated (trailed) stars** | Nice trick: the roundness filter is a free trail/cloud detector. |
| Centroiding | all of the above | intensity-weighted centroid or 2-D Gaussian fit | Sub-pixel accuracy 0.05–0.2 px at SNR≳20 [U, order-of-magnitude]. |

Practical settings that matter more than the choice of detector: **detect on a background-subtracted luminance image with a sigma threshold ~4–5**, cap the catalogue at the **50–200 brightest** sources (astroalign caps at 50 [C]), and **mask saturated stars** (they have biased centroids and hot-pixel neighbours).

### 2.2 Matching: triangles, quads, hashes, then RANSAC

- **Triangle/asterism matching with geometric hashing** (Groth 1986 → Pál & Bakos 2006 → Tabur 2007 → astroalign) [C]. astroalign's recipe: 50 brightest sources → k-d tree → for each star the 4 nearest neighbours → 10 triangles → invariant tuple (L₂/L₁, L₁/L₀) → k-d tree of invariants → nearest-neighbour matching in invariant space → hypothesise similarity → validate by reprojection error (Sampson distance < 3) → RANSAC re-fit on inliers; accept when 80 % (or 10) triangle matches agree. **This is the single most reusable piece of code in the field for our problem.**
- **Quad matching / pattern hashing**: ASTAP (Object Pascal, MPL-2.0) [C], astrometry.net (Lang et al. 2010) [C]. These are *blind* solvers (they must search the whole sky); we do **not** need blind solving — we need "align to the previous/accumulated image", where the search space is tiny. Blind solving is only interesting for the *first* frame (to label the target, get FOV/pixel scale, correct lens distortion via SIP).
- **tetra3** (ESA, Apache-2.0, Python) [C]: 4-star "tetra" pattern hashing designed *for star trackers* (i.e. for exactly our failure mode: fast, wide-field, unknown pointing, ms-scale). A credible "first-frame absolute solve" component. [U] its accuracy from a 50 mm DSLR frame with 5 % lens distortion.
- **RANSAC** [C] is the standard robust fit and is what makes any of the above survive hot pixels, satellites and mismatched asterisms. With correspondences already established, `cv2.estimateAffinePartial2D(src→dst, method=RANSAC, ransacReprojThreshold≈2 px)` does the job: **[M] 40 µs for 50 correspondences.** Note the OpenCV name: `estimateAffinePartial2D` = 4-dof similarity; `estimateAffine2D` = 6-dof affine; `estimateAffinePartial2DWithRefinement` if you want LM polish.
- **Nearest-neighbour + RANSAC only ("small drift" shortcut)**: if the transform between *consecutive* frames is < ~10 % of the frame, a simple KD-tree nearest-neighbour match of the two star lists + RANSAC works and is ~10× cheaper than triangle hashing, because the invariant search is unnecessary. **[D]** Between adjacent 5 s frames at 50 mm the shift is ~0.5 % of the frame; over a 30 min session though, the *cumulative* transform is large — so the shortcut requires chaining (error accumulates as √N × per-frame centroid error, plus systematic drift from the similarity-vs-projection mismatch, §1.4). [U] quantify: expect a few px of slow drift over 500 frames; mitigate by re-anchoring.

### 2.3 The phase-correlation / FFT shortcut, and exactly when it dies

`skimage.registration.phase_cross_correlation` (Kuglin–Hines with Guizar-Sicairos upsampled-DFT sub-pixel refinement) [C] and `cv2.phaseCorrelate` estimate **translation only**.

Measured **[M]** (6000×4000 equivalent, downsampled inputs, pure 7.37 px translation in a 1500×1000 crop):

| variant | time | recovered shift error |
|---|---|---|
| `phase_cross_correlation(upsample_factor=1)` @1.5 MP | 0.031 s | 0.5 px (quantised) |
| `upsample_factor=10` | 0.050 s | 0.03 px |
| `upsample_factor=100` | 0.082 s | 0.008 px |
| @24 MP, `upsample_factor=10` | **0.86 s** | — (`cv2.phaseCorrelate`: 0.48 s) |

And with a *real* 0.3° rotation + 0.1 % scale in the pair, phase correlation reports a translation that is **wrong by ~17 px** (it splits the rotation into a bogus translation): recovered (−132.0, +72.4) vs true (−137, +88). [M]

⇒ **Phase correlation is a good *residual/verification* tool and a bad *registration* tool** for untracked data. Use it for: (i) sub-pixel refinement after a star-based similarity fit; (ii) the "did the predicted-transform model hold?" check (§1.4); (iii) drift between *adjacent* frames only when the rotation between them is ≪ 0.01° (5 s at 50 mm: 0.015°/5 s at δ=60° — marginal). Fourier–Mellin / log-polar registration (rotation+scale from the magnitude spectrum) exists as an alternative [U — notoriously fragile on sparse star fields; not recommended].

`cv2.findTransformECC` (Evangelidis) — measured, and it **fails** here: on a 1.5 MP pair with a 137 px shift and 0.3° rotation, EUCLIDEAN mode returned rotation −0.0074° (true −0.3°) and cc = 0.009 in 0.79 s; AFFINE mode 1.39 s, same failure. Without a pyramid/initial guess, ECC's basin of convergence is far too small. [M] **Do not use `findTransformECC` as the primary matcher**; it is only usable as a final polish given a good initial guess (and even then, phase correlation is faster).

### 2.4 Library/approach comparison for 1–5 s/frame on a laptop

| Approach | Lang / licence | Time @1.5 MP | Time @6 MP | Time @24 MP | Verdict |
|---|---|---|---|---|---|
| astroalign `find_transform` (triangles+RANSAC) | Python, **MIT** [C] | **0.20–0.24 s** [M] | 0.36 s [M] | **fails** (SEP pixstack) [M] | Best default; use a downsample. |
| SEP + own triangle hash | Python/C, LGPL-3 [C] | ~0.1 s [U] | — | — | If you need to shave latency/control thresholds. |
| SEP + KD-tree NN + `cv2.estimateAffinePartial2D` | Python, Apache-2.0 | ~0.06 s [M extrapolated: 0.04 s SEP + 0.04 ms fit] | ~0.15 s | — | Best for the *chained/adjacent-frame* mode. |
| skimage phase correlation | BSD-3 | 0.05 s | 0.16 s | 0.86 s | Verification only (§2.3). |
| `cv2.findTransformECC` | Apache-2.0 | 0.79 s (+ fails) | — | — | No. |
| tetra3 (blind solve) | Python, Apache-2.0 [C] | [U] ~0.1–1 s | — | — | First frame / labelling only. |
| `ccdproc` | BSD-3 | n/a (calibration, cosmic rays) | | | Calibration pipeline, not registration. |
| `starfinder` (PyPI v1.1.0) | — | — | — | — | **[U] Not the tool you think it is** — the PyPI package is a *star-tracker image simulator*. The intended "STARFINDER" is almost certainly `photutils.DAOStarFinder` or SExtractor. |

### 2.5 Recommended cascade (per frame)

1. Downsample to **1500×1000** luminance (`cv2.resize(..., INTER_AREA)`; **[M] 3 ms**).
2. Predicted transform from the pole model (if calibrated) → else previous frame's transform → warp the *reference crop*, not the new frame, and measure a phase-correlation residual.
3. If residual > threshold: full `astroalign.find_transform(new_small, ref_small)`; if that fails, retry on the centre 30 % crop; if that fails, mark the frame "unaligned" (stack it in translation-only with a penalty, or drop it) — *never* silently drop frames without telling the user.
4. Convert the small-image transform to full-res (scale tx,ty by 4, rotation/scale unchanged) → `cv2.warpAffine` on the full-res 16-bit frame.
5. Accumulate + update coverage map (§6).

---

## 3. Stacking math

### 3.1 Estimators

For N frames x<sub>i</sub> (same pixel after alignment), with weights w<sub>i</sub> = 1/σ<sub>i</sub>²:

| Estimator | Formula | Robustness | Efficiency | When |
|---|---|---|---|---|
| mean | (1/N)Σx<sub>i</sub> | none (one satellite/cosmic ray/hot pixel ruins the pixel) | 100 % | never, for a live tool fed by an uncooled DSLR |
| median | median(x<sub>i</sub>) | excellent (breakdown 50 %) | ~64 % vs mean for Gaussian (π/2 variance penalty) [C, standard] | N ≲ 20, or for masters |
| kappa-sigma clipped mean | reject \|x−μ\|>kσ, k≈3, 1–3 iterations, then mean | good (needs N ≳ 8–10 for a sane σ) | ~95 %+ | **default for N ≳ 10** — DSS's recommendation is "median for small stacks, kappa-sigma for large" [C] |
| median-kappa-sigma | rejected values *replaced by the median*, then mean | as above, no N-dependence of the denominator | high | DSS's variant; converges better when many pixels are rejected [C] |
| auto-adaptive weighted mean | Stetson (1989) iterative weighting by deviation | very good | high | DSS default-ish; needs a full stack in memory |
| entropy-weighted / "noise-weighted" | weight ∝ 1/σ<sub>frame</sub>² | good for variable transparency | — | **yes — weight by frame quality** (§3.4) |

### 3.2 The SNR law, and the read-noise penalty (the crux for short subs)

Per pixel, N frames of exposure t, source rate S, sky+dark rate B (e⁻/s), read noise RN (e⁻ rms):

```
SNR = S·t·√N / √(S·t + B·t + RN²)          [C: Howell, standard CCD SNR equation]
```

√N scaling holds **only** while `B·t ≫ RN²` (background-limited). The general statement is that SNR depends on **total exposure T = N·t** and on the read-noise overhead:

```
SNR ≈ S·√T / √(S + B + RN²/t)   per-pixel-unit...   ∝ √T · [ t / (t + t_RN) ]^0.5 ,  t_RN = RN²/B
read-noise efficiency  ε = t / (t + RN²/B)     → total-time penalty = 1/ε
```

Sky background rate per pixel [D — computed from the V-band photon zero point 8.9×10⁹ ph/s/m² for m<sub>V</sub>=0, QE·throughput 0.4, 4.0 µm pixels]:

| optics | scale | sky 18 mag/arcsec² (suburb) | sky 20 | sky 21.5 (dark) |
|---|---|---|---|---|
| 50 mm f/1.8 | 16.5″/px | 42.8 e⁻/s/px | 6.8 | 1.7 |
| 50 mm f/2.8 | 16.5″/px | 17.7 | 2.8 | 0.71 |
| 135 mm f/2.8 | 6.1″/px | 17.7 | 2.8 | 0.71 |
| 300 mm f/5.6 | 2.8″/px | 4.4 | 0.70 | 0.18 |
| 600 mm f/6.3 | 1.4″/px | 3.5 | 0.55 | 0.14 |

With RN = 3 e⁻ (RN² = 9 e⁻²), the "break-even" exposure t<sub>RN</sub> = 9/B where sky noise = read noise: **1–5 s for a fast lens under suburban skies, but 13–65 s for f/5.6–f/6.3 optics at a dark site.** So the popular claim "short subs are read-noise dominated" is only *half* true, and the exception is important for this project:

- **50 mm f/1.8, 20 mag sky, 2 s subs**: ε = 2/(2+9/6.8) = 0.60 → only 1.7× total-time penalty vs. infinitely long subs. Short subs are *cheap*.
- **300 mm f/5.6, 21.5 mag sky, 2 s subs**: ε = 2/(2+51) = 0.038 → **26× penalty (3.5 mag of depth lost)**. Short subs are *ruinous*.

Frames needed for SNR = 5 on a faint extended feature (S = 0.2 e⁻/s/px, RN = 3 e⁻) [D]:

| sub length | frames needed | total integration | vs. noiseless readout | vs. 30 s subs |
|---|---|---|---|---|
| 2 s | 1469 | 2938 s | 23× | 8.9× |
| 5 s | 250 | 1250 s | 10× | 3.8× |
| 10 s | 69 | 690 s | 5.5× | 2.1× |
| 30 s | 11 | 330 s | 2.6× | 1.0× |

**Design consequence:** optimize sub length against the *trailing* budget (§1.3) and then accept the read-noise cost — do not choose sub length for noise reasons. And use a **fast lens** (f/1.8–f/2.8) for untracked work: it moves you from the 26×-penalty regime into the ~2× regime, which is worth more than any algorithm.

### 3.3 Limiting-magnitude math

Stacking gains `2.5·log₁₀√N = 1.25·log₁₀N` magnitudes (background-limited). 100 frames → +2.5 mag; 500 → +3.4 mag; 1000 → +3.75 mag. With read noise: **Δm = 1.25·log₁₀(ε·N)**. At ε = 0.04, 500 frames behave like 20 frames → +1.6 mag instead of +3.4. This is the honest ceiling for untracked work at long focal lengths.

### 3.4 Weighting by frame quality

Two levels, both cheap:
- **Global frame weight** w<sub>f</sub> ∝ 1/σ<sub>f</sub>² where σ<sub>f</sub> is the measured background RMS (or ∝ 1/FWHM² if you trust the FWHM estimate): compensates thin cloud and transparency changes. Also *rejects* frames whose background is > ~1.5× the running median (cloud) — SharpCap ships exactly this as its "Brightness Filter" [C].
- **Per-pixel rejection** (§6.2) for cosmic rays, satellites, hot pixels, aircraft.

An intermediate trick that works well in practice [U, but well-founded]: **σ-clip in the *log* or *ratio* domain near saturation**, or simply exclude saturated pixels per-frame using a per-frame saturation mask. Uncooled DSLRs also have amp glow and dark-current drift across a session — a single master dark taken at the end of the session is often a poor match to the beginning [U; DSLR-specific, worth measuring].

### 3.5 Drizzle or not?

Drizzle (Fruchter & Hook 2002 [C]) recovers resolution from **undersampled** data by shrinking each input pixel's footprint (pixfrac < 1) onto a finer output grid, using the natural dithering. Applicability here:

| case | sampling | drizzle verdict |
|---|---|---|
| 14–85 mm | 10–60″/px — PSF ~1.75 px, near-Nyquist and **diffraction-limited** (11 §3) | **No.** Nothing to sharpen. Downsample for *display* only — binning the archived frame discards real resolution. |
| 135–200 mm | 4–6″/px; seeing 2–3″ → still oversampled | No; 2×2 binning is the right move. |
| 300 mm+ | ~2.8″/px ≈ seeing — right at Nyquist | Marginal yes *if* you had tracking; with sub-second untracked subs you also have a *known* motion trail, which is far more damaging than under-sampling. |

Also note: with a rotating field you already have strong, free "dithering", so a drizzle implementation is *feasible* (that's exactly how DSS's Bayer-drizzle exploits drift [C]). But it costs 4–16× the accumulation work and is unnecessary at the focal lengths this project can actually use. **Verdict: no drizzle; bin for display.** Mention "Bayer drizzle"/super-pixel debayering only as a note: if you stack raw CFA data *before* demosaicing, you avoid interpolation artefacts and gain ~√2 effective resolution — DSS and Siril both do this [C]; it is more valuable than drizzle here [U quantitatively].

---

## 4. Lucky imaging proper

### 4.1 What "lucky" means quantitatively

- **r₀ (Fried parameter)** [C, Fried 1966]: the aperture diameter over which the wavefront phase variance is ~1 rad². Seeing FWHM ≈ 0.98 λ/r₀ (at 500 nm: FWHM″ ≈ 0.21/r₀[m]... equivalently r₀ ≈ 100 mm at 1″ seeing, ~50 mm at 2″). r₀ ∝ λ^(6/5), and it *fluctuates*: over seconds, r₀ can swing ±30–50 % [U order of magnitude].
- **Why selection works**: at any instant the turbulence is a frozen-ish phase screen advected by the wind (Taylor hypothesis), so short exposures (≲10–20 ms) "freeze" a single realisation. The distribution of instantaneous image sharpness is wide; the best few percent approach the diffraction limit. For a large aperture (D ≫ r₀) the instantaneous PSF is a **speckle** pattern of ~(D/r₀)² speckles, and the "lucky" frame is the one where one speckle happens to carry most of the energy.
- **Numbers from the literature** [C, Law et al. 2006, 2.56 m NOT, I-band]: selecting the best **10 %** of frames at 10–30 Hz gave I-band **Strehl 0.15–0.2** (vs ≈0.01–0.02 for shift-and-add of all frames); typical selection percentages used in practice are 5–20 %; the gain falls off fast with selection fraction and with aperture size relative to r₀.
- **Isoplanatic angle** θ₀ ≈ 0.314·r₀/h̄ (single-layer approximation): with r₀ = 15 cm and an effective turbulence height ≈5 km, θ₀ ≈ 1.9″; good sites show θ₀ ≈ 2–3″ at 500 nm [U]. In practice amateur lucky imaging is valid over ~5–20″ patches, i.e. a few APs across a planet. **This is why AutoStakkert and PSS stack per-alignment-point patches rather than globally** — different parts of the frame are lucky at different instants [C].
- Exposure: ≤ ~10–20 ms for real lucky imaging; 100 ms is already "short-exposure" rather than "lucky" [C, Wikipedia/standard].

### 4.2 Frame-quality metrics (cheap, all O(N×px))

| Metric | Definition | Cost | What it catches |
|---|---|---|---|
| FWHM / HFD of detected stars | fit each star; take the median | one SEP pass | focus, seeing, trailing |
| Star count above threshold | `len(sep.extract(...))` | free with detection | cloud, transparency |
| Peak brightness / star flux sum | max or Σflux of the brightest 20 | free | cloud, focus |
| Global contrast / gradient energy | Σ(∇I)² or Σ|Laplacian| over the frame | 1 convolution | sharpness — this is AutoStakkert's "Edge"/"Gradient" estimator [C] |
| Local contrast per AP | same, in patches | PSS/AS!3 style [C] | local lucky selection |
| Roundness/elongation | from DAOStarFinder or SEP moments | free | **trailing** — directly measures the §1.3 problem |

For this project the useful composite is: *reject if* (median FWHM > 1.3× running median) OR (star count < 0.6× running median) OR (elongation > 1.4×) OR (background > 1.5× running median). Then *weight the survivors* by 1/FWHM². Cheap, explainable, and it doubles as a user-facing "quality" readout.

### 4.3 Deep sky vs planets — the honest accounting

Lucky imaging buys **angular resolution**, and pays with **integration time**. For a planet, resolution *is* the signal: the target is bright, you are read-noise limited at 10 ms, and the atmosphere destroys detail. For a deep-sky target:

1. **You are background-limited, not seeing-limited.** At 50 mm, seeing of 2.5″ is 0.15 px. There is no detail that selection could recover. At 300 mm (2.8″/px) seeing is ~1 px — still at/below Nyquist. Only at ≥400–600 mm does the PSF start to span multiple pixels, and there the untracked sub-length limit is 0.2–0.4 s (§1.3), which lands you in a read-noise catastrophe (§3.2: ε ≈ 0.004 → 250× penalty).
2. **Selection multiplies the read-noise penalty.** Keeping 10 % of frames multiplies the required total exposure by 10 for the same SNR. Combined with the short-sub penalty, untracked + lucky + deep sky at long focal length is off the table by an order of magnitude, not a factor of two.
3. **Where it *does* help for deep sky** (be precise about this):
   - **Bright, high-surface-brightness objects needing resolution**: planetary nebulae cores, globular cluster cores, M42's Trapezium, M31's nucleus — *if* you have the focal length and a bright enough guide star for the quality metric.
   - **Discarding genuinely bad frames** (cloud, trailed, focus drift, aircraft) — this is not lucky imaging, it is hygiene, and it is worth 10–30 % of frames.
   - **Weighted stacking by FWHM** — a "soft" version of selection that never throws away photons; strictly better than hard selection when read noise matters [U but standard practice].
   - **Per-AP local alignment** (PSS/AS!3) which corrects *differential* motion within the frame — relevant if the tripod is bumped or (rarely) for very wide fields with strong lens distortion.
4. **The "many short subs" regime this project lives in is *not* lucky imaging** — it is plain short-exposure stacking, whose justification is *geometric* (no tracker ⇒ you cannot expose longer) and *dynamic-range* (no saturation), not atmospheric.

### 4.4 How many frames, actually?

Take the honest path: 
- choose t from the trailing budget (§1.3): 50 mm/δ=45° → 3 s;
- you need SNR ≈ 5 on your faintest interesting feature;
- from §3.2's table, ~250 frames (12 min) for a 0.2 e⁻/s/px feature at 0.7 e⁻/s/px sky (20 mag) — and 4× more at a dark site with slow optics;
- add a 20 % overhead for rejected/unaligned frames;
- **budget 300–1500 frames for a 15–45 min untracked session at 24–85 mm**, i.e. exactly the "hundreds to thousands" the project spec assumes. At 135 mm+ you will be in the thousands, and the binding constraint becomes disk I/O and registration throughput (≈0.25 s/frame ⇒ 4 fps ceiling for astroalign-based registration on one core).

---

## 5. Existing implementations: what to learn, what to reuse

| Tool | Lang | Licence | Registration | Estimator | Reusable? |
|---|---|---|---|---|---|
| **DeepSkyStacker** 4.2.5/5.x | C++ (Qt) | **BSD-3-Clause** (open-sourced 2018) [C] | star detection (round, ≤50 px, Gaussian centroid) + **largest-triangle pattern matching**, min **8 stars common to all frames**, offsets+rotation vs. best-scoring reference; sub-pixel; optional drizzle/Bayer-drizzle | average, median, max, **kappa-sigma**, **median kappa-sigma**, **auto-adaptive weighted (Stetson)**, entropy-weighted; per-channel background calibration | **YES — the only permissive full stacker.** Excellent source for the estimator maths and star-detection heuristics. Batch-oriented (no streaming). |
| **Siril** ≥1.2 | C | GPL-3.0 | Livestack: watches a folder, per-frame register; **shift-only by default (faster), rotation optional (`-rotate`, recommended for alt-az/heavy drift)**; 16-bit default; optional dark/flat masters; gradient removal | Siril's stacking norms (mean/median/winsorised σ-clip etc., batch side) | Yes (GPL). **Direct evidence that "translation-only" is the fast path and rotation must be opt-in** [C]. |
| **SharpCap** 4.x | .NET/C# [U] | proprietary (free tier + Pro) | star-based alignment **with de-rotation** ("rotating each new image to match the orientation of the stacked image"), min 3 stars, configurable star detection; FWHM filter; brightness filter | default (mean-like) and **sigma-clipped (Pro)**; darks/flats live | No. But the **feature set is a checklist**: FWHM/brightness filters, coverage-aware display ("areas that stop getting new data become darker"), 16-bit stack, "save stack at any time" [C]. |
| **Astro Live Stacker (ALS)** | Python 3 + PyQt5 | **GPL-3.0** [C] | **`astroalign.find_transform`** on the green channel, against a *fixed first-frame reference*, with 3 fallbacks (centre 10 %, 30 %, 100 % of the frame) and a minimum-match threshold; skimage `SimilarityTransform` + warp | running **sum** or running **mean** only (`(n·acc + x)/(n+1)`); no weights, no rejection | **Yes — closest architectural match.** Copy the queue/worker structure and the folder-watch UX; do *not* copy the fixed reference, the mean-only estimator, or full-res alignment. |
| **PlanetarySystemStacker (PSS)** | Python 3 + OpenCV + Qt | GPL-3.0 [C] | global rank → **alignment-point mesh** (automatically dropping dim/low-contrast APs) → per-AP local contrast ranking → per-AP local shifts → per-patch stacking → blending with the background | per-AP local lucky selection + averaging | Yes (GPL). **Best reference for local/AP stacking and for a documented algorithm** (`Documentation/algorithm_summary.pdf`). Claims parity with AS!3 [C]. |
| **AutoStakkert! 3/4** | Delphi/Pascal [U] | freeware, closed | quality ranking (Edge / noise-robust **Gradient** estimator) → reference frame → 1..many APs → per-AP alignment → stack best X % | selection % + mean; 16-bit output; multiple stack sizes | No. Steal: quality-estimator ideas, AP alignment, "stack N % of frames" UX. |
| **Registax 6** | closed | freeware, unmaintained | manual/auto alignment points | wavelet sharpening (its real value) | No. |
| **FireCapture** | Java [U] | proprietary, "private use" licence [C] | capture only (has real-time drift/alignment for planets [U]) | — | No. |
| **ASTAP** | Object Pascal (FPC/Lazarus) | **MPL-2.0** [C] | **quad-based star-pattern hashing** + blind plate solve; stacking via star alignment | stacking modes incl. sigma clip | Partly (MPL-2.0): its quad matcher is a great design reference for a *blind* first-frame solve. |
| **tetra3** | Python | **Apache-2.0** [C] | 4-star pattern "tetra" hashing, lost-in-space, FOV-agnostic | — | **Yes** — permissive, fast, ideal for the first frame. |
| **astroalign** | Python | **MIT** [C] | triangles + RANSAC + skimage warp | — | **Yes** — use as-is (§2). |
| **INDI / KStars+Ekos** | C++ | LGPL-2.1 (INDI) / GPL-2.0+ (KStars) [C] | Ekos internal guider does guide-frame stacking [U] | — | Yes (GPL/LGPL). Reference for device I/O + the "separate capture and processing processes" architecture. |
| **N.I.N.A.** | C#/.NET | **MPL-2.0** [C] | no core live-stack [U] | — | Only if the Windows acquisition side needs an ASOM/Alpaca client. |
| **ToupTek ToupSky / AltairCapture** | closed | proprietary | EAA live stacking for their cameras [U] | — | No. |
| `sep`, `photutils`, `ccdproc`, `astroscrappy`, `scikit-image`, `OpenCV` | C/C++/Python | LGPL-3.0 / BSD-3 / BSD-3 / BSD-3 / BSD-3 / Apache-2.0 | building blocks | — | **Yes** — a fully permissive core is achievable with *only* `sep` (LGPL, dynamically linked ok) or `photutils` (BSD) + OpenCV + skimage + astroalign. |

**Answer to "is there a good permissively-licensed reference implementation of align+stack incrementally+display?"**
- Incremental + display: **ALS** (GPL-3.0) — good, but GPL and architecturally naive.
- Permissive: **DeepSkyStacker** (BSD-3) for the stacking *maths/heuristics* and **astroalign** (MIT) for registration, but **nobody has published a permissive, streaming, rotation-aware live stacker.** That is a genuine gap and a differentiator — build the accumulator ourselves on top of MIT/BSD pieces.

---

## 6. Incremental / streaming stacking

### 6.1 Accumulator maths (no frame storage)

**Weighted running mean** (exact, O(1) memory, order-independent if weights are fixed):

```
W ← W + w                                   (per-pixel weight sum, float32)
acc ← acc + (w / W)·(x − acc)               (acc = current weighted mean)
```

**Welford mean + variance** (gives you a *free* per-pixel σ for rejection):

```
n ← n + 1
d  = x − mean
mean ← mean + d/n
M2   ← M2 + d·(x − mean)        ;  σ² = M2/n
reject if |d| > k·√σ²_prev      (k≈4; skip the update, still increment a "seen" counter)
```

- Cost at 24 MP float32: **Welford 91 ms/frame** [M]; plain running mean **38 ms**; `cv2.accumulateWeighted` **19 ms** [M]. A rejection mask costs a further ~84 ms [M] → so **do Welford+reject at preview resolution only** and use it to (i) flag whole frames and (ii) protect the display; keep the full-res accumulator as a simple weighted sum.
- **Exact integer accumulation**: a uint32 sum is exact up to 65 535 frames of uint16 data (4.29e9/65535). 24 MP uint32 = 96 MB, `np.add(acc32, frame16, out=acc32)` ≈ 10–20 ms [U by analogy with `cv2.accumulate`'s 10 ms for uint16→float32]. Divide by the count map at display time. **This is the cheapest correct option for the "many frames, no rejection" tier.**
- **float32 vs uint16 accumulator**: float32 (96 MB/frame) is required for weighted/robust accumulators; uint32 (96 MB) suffices for plain sums. uint16 accumulation overflows at 1–2 frames — never use it for the accumulator (only for storage). Watch out: **stacking in 16-bit for a live session is what Siril does by default** [C] — fine for the *input* representation, but the accumulator needs headroom.

**Memory budget** (6000×4000 = 24 MP): one frame is 48 MB as uint16, 96 MB as float32/uint32.

| quantity | per item | footprint | comment |
|---|---|---|---|
| all 500 frames, uint16 (RAW-ish) | 48 MB | **24.0 GB** | cannot keep in RAM; disk only |
| all 500 frames, float32 | 96 MB | **48.0 GB** | never |
| all 500 frames, 8-bit (lossy) | 24 MB | 12 GB | still no |
| accumulator + weight map (float32 ×2) | 96 + 96 MB | 192 MB | fine |
| accumulator (uint32) + count (uint16) | 96 + 48 MB | 144 MB | fine — **exact sum to 65 535 frames** |
| Welford (mean + M2, float32 ×2) | 192 MB | 192 MB | fine |
| 20-frame uint16 ring buffer (for true σ-clip) | 48 MB | 960 MB | only at ≤6 MP |
| 20-frame float32 ring buffer @1.5 MP preview | 6 MB | 120 MB | fine |

| accumulator dtype | headroom | exactness | cost |
|---|---|---|---|
| uint16 | none (overflows after 1–2 frames) | — | never |
| uint32 | 65 535 × 65 535 → 4.29e9 | **exact** for ≤65 535 frames | ~10–20 ms/frame [U] |
| float32 (24-bit significand) | large | integers exact only while Σx ≤ 2²⁴, i.e. ≈256 frames at full scale (rounding beyond that is ~1e-7 relative and averages out) | 19–38 ms/frame [M] |

### 6.2 Sigma clipping without keeping all frames

True kappa-sigma needs the *distribution*, so:
- **Ring buffer of the last M frames** (M = 15–31), per-pixel `np.partition` / median, reject, then fold the surviving pixels into the running accumulator; recompute the *final* image from the buffer window only every display update. Cost is brutal at full res: **median of 10×24 MP float32 = 1.32 s, σ of 10 = 0.38 s** [M] ⇒ only viable at ≤2 MP or tile-parallel (a 1.5 MP float32 ring buffer of 20 frames = 120 MB, median ~40–60 ms [M extrapolated]).
- **Preferred for a live tool: Welford + k·σ gate** (§6.1) — O(1) memory, no ring buffer, catches cosmic rays/hot pixels/satellites as long as they do not repeat at the same pixel (they don't). Weakness: a *biased* early mean (e.g. the first two frames both hit by the same satellite) can't be undone; mitigate with a "burn-in" of ~5 frames and by capping n at, say, 200 with exponential forgetting (`1/n → 1/max(n,200)`).
- **Tile-based accumulation** (e.g. 512×512 tiles, `numpy` or `cv2` parallel): bounds peak memory, improves cache behaviour, and lets you keep tiles in RAM only while being updated. Given the measured 19–91 ms/frame for whole-frame numpy ops, tiling is mainly a *memory* strategy, not a speed one [U: expect neutral-to-slightly-better throughput].

### 6.3 Coverage/weight map, re-anchoring, canvas

- **A per-pixel weight/count map is mandatory** the moment you rotate or translate: pixels near the edge receive fewer contributions (and warped frames may have zero). Divide by the weight map at display time, and render "no data" as black rather than as a dark gradient. SharpCap documents exactly this artefact ("some areas of the stack stop getting new data, so they become darker compared to the core") [C]. Cost: one extra float32 24 MP buffer + one pass per frame (~20 ms).
- **Re-anchoring:** every K frames (or when the cumulative transform exceeds ~20 px / ~1°), warp the accumulator *and* the weight map into the current frame's coordinate system, then continue. One extra warp per K frames = negligible. This keeps the display centred on the target (necessary — the target drifts thousands of px per session, §1.3), keeps the numerical baseline short (§1.4), and keeps the working canvas at sensor size instead of a giant sky canvas.
- **Framing/rotation loss:** after total rotation θ about a point, the largest inscribed axis-aligned rectangle of the accumulator shrinks; show the "valid region" mask, and warn the user at θ > ~5°. [U] compute exact numbers: for a 3:2 frame rotated by 5°, inscribed area ≈ 94 %; 10° ≈ 87 %; 20° ≈ 74 % [U geometry, easy to verify numerically].

### 6.4 Display pipeline (the "beginner sees something immediately" part)

1. **Debayer once** (OpenCV `cvtColor(..., COLOR_BayerRG2BGR)`) or use super-pixel/bilinear [U which is fastest]; for a *live JPEG* stream the data is already demosaiced, 8-bit, gamma-encoded — **you must linearise sRGB→linear before summing** (otherwise you are averaging in a compressed domain and everything saturates oddly). [D — standard colour science; the exact sRGB EOTF being applied by the camera's JPEG pipeline is [U].]
2. **Background subtraction / gradient removal**: subtract the median (or a low-order plane fit) so the display is stable while the sky brightness changes.
3. **Autostretch.** Two good options:
   - **STF/midtones-transfer-function** (PixInsight-style [C]): 
     `shadows = median − 2.8·1.4826·MAD`, `x = clip((v − shadows)/(1 − shadows))`, `MTF(m,x) = ((m−1)x)/((2m−1)x − m)`, with `m` solved so the background lands at ~0.25: `m = 0.75·x_bg/(0.5·x_bg + 0.25)`. **[M] 63 ms at 1.7 MP** (dominated by the two medians — cache them and recompute every ~10 frames).
   - **asinh** (Lupton et al. 2004 [C]) `y = asinh(a·v/v_bg)/asinh(a)`, a≈5–10; **[M] 26 ms at 1.7 MP**; more "video-like", less controlled.
   - Do **not** use histogram equalisation or CLAHE: it destroys the perception of progressive improvement (noise gets amplified as much as signal, so the image "improves" less and less visibly).
4. **Encode**: `cv2.imencode('.jpg', ..., quality=85)` = **2.8 ms at 1.7 MP** [M]; PNG at 24 MP = 153 ms [M]. For a web UI, JPEG/WebP at preview resolution, always.
5. **Progressive-looking feedback**: display the stack with a stable stretch (so improvements are visible as noise reduction), plus a small "N frames / total time / FWHM / SNR estimate" HUD. The single biggest UX win is *not* the algorithm, it is a stretch that doesn't jump around between frames.

---

## 7. Latency budget (measured)

Per-frame costs, 6000×4000 uint16, on the benchmark machine: **[M]**

| Stage | Full res 24 MP | 1.5 MP (1500×1000) |
|---|---|---|
| resize (INTER_AREA) | 3 ms (produces the 1.5 MP) | — |
| star detection (SEP, background + extract) | 0.36 s typ / 3.3 s worst | 32–40 ms |
| transform fit (`estimateAffinePartial2D`, 50 pts) | — | 0.04 ms |
| astroalign `find_transform` | **fails** | 0.20 s |
| phase correlation verification (upsample 10) | 0.86 s | 0.05 s |
| `warpAffine` (INTER_LINEAR) | **15 ms** (uint16) / 20 ms (f32) | 0.8 ms |
| running mean | 19–38 ms | ~1 ms |
| Welford + reject | 91 + 84 ms | ~6 ms |
| median of 10-frame ring buffer | 1.32 s | ~50–80 ms |
| STF autostretch | — | 63 ms (asinh: 26 ms) |
| JPEG encode q85 | — | 2.8 ms |
| PNG encode | 153 ms | — |

**Realistic per-frame latency:**

| tier | composition | per frame | feasible rate |
|---|---|---|---|
| **A. "Just show me something"** (translation-only, phase-correlated, preview-res stack — legitimate only for the first few seconds or as a fallback; smears within ~30 s at high δ) | 3 + 40 + 50 + 1 + 63 + 3 | **~160 ms** | 6 fps |
| **B. "Proper, recommended"** (SEP on preview + triangle match + full-res warp + Welford + STF) | 3 + 40 + 200 + 15 + 100 + 63 + 3 | **~420 ms** | 2.4 fps |
| **C. Same, with the predicted-transform model** (no matching, only verification) | 3 + 40 + 5 + 15 + 100 + 63 + 3 | **~230 ms** | 4 fps |
| **D. Full-res everything** | 0.36 s + 0.86 s + 15 ms + 90 ms + … | **~1.5 s** | 0.7 fps |

**GPU?** [U, but well-founded] A GPU (CuPy/torch) would accelerate exactly the parts that are already cheap — `warpAffine` (15 ms), accumulation (19–40 ms), stretch (26–63 ms) — and would *not* accelerate the dominant parts: JPEG/RAW decode, SEP star detection and the triangle/RANSAC matching are all CPU/host-side and largely serial. Realistic expectation: a GPU implementation of the accumulator+display chain cuts tier B from ~420 ms to ~300 ms (~1.4×) at best, and introduces PCIe copy costs for 96 MB buffers (~10 ms each way). **Do not build a GPU pipeline first**; optimise the registration (downsample, predicted transform) instead.

Caveats: [U] a Windows build of the same code is typically 1.3–2× slower for numpy-heavy work, and a real DSLR pipeline adds **RAW decode (0.2–1.5 s/frame with libraw/dcraw, or ~0.1–0.3 s with a fast vendor SDK) + PTP/SDK download latency**, which likely dominates everything above. Live-view JPEG frames (8-bit, ~1–2 MP, ~30 fps) are the fast path; full-res stills are the quality path. **Design for two input tiers** and never block the accumulator on the slow one.

**Downsampling/binning strategy**: register and display at 1500×1000 (bin ≈4×); accumulate at full res in uint32/float32 only if RAM allows 2–3 full-res buffers (96 MB each + weight map) — 24 MP × (acc + weight + temp) ≈ 300 MB, which is fine. If RAM-bound, accumulate at half res (6 MP) and keep the full-res frames on disk for a final restack. Binning 2×2 also *improves SNR per display pixel* by 4×, which makes the "progressive improvement" visible sooner — a genuinely useful trick for the beginner UX.

---

## 8. Recommended design for this project (research conclusions)

1. **Be honest about the operating envelope in the UI.** Show, live: target δ, predicted roll rate (Ω sin δ), drift rate, and the "max sub for 2 px trail" (§1.3). If the user points at 300 mm, tell them what it costs (§3.2). This single feature would put the tool ahead of every consumer package.
2. **Registration:** astroalign (MIT) on a 1500×1000 luminance crop as the default, with SEP+NN+RANSAC chaining as the fast path and a **pole-model predictor** (§1.4) as the optimisation. Re-anchor every 2–5 min.
3. **Accumulator:** uint32 exact sum + count map at full res (simple tier); Welford mean/σ² + kσ gate at preview res (robust tier); a small ring buffer only if the user asks for true σ-clipping.
4. **Coverage map from day one** — rotating means edge pixels get fewer samples; without it the result looks broken.
5. **Display:** STF autostretch, asinh fallback, JPEG/WebP preview at 1500 px, HUD with frames/seconds/FWHM.
6. **Quality gate, not lucky imaging:** FWHM/star-count/roundness/background → reject + weight (§4.2). Expose the rejected-frame thumbnails so the user can see why.
7. **Do not implement drizzle. Do implement 2×2 display binning.**
8. **Two input tiers:** live-view JPEG (8-bit) for the live experience + full-res stills for the final restack; linearise sRGB before stacking 8-bit data.

---

## 9. Open questions / things worth measuring next

- [U] Real astroalign accuracy on 24 MP *real* DSLR frames (JPEG artefacts, hot pixels, lens distortion) — the synthetic benchmark is optimistic.
- [U] How much the pole-model predictor drifts over a 45 min session in the field (tripod creep, mirror slap, focus drift, temperature).
- [U] Whether Bayer-domain (super-pixel) stacking measurably beats debayer-then-stack for untracked wide-field data.
- [U] Whether the ~1.3–3.3 s SEP worst case at 24 MP can be avoided reliably by masking saturated regions and setting `extract_pixstack`.
- [U] Windows-specific throughput for the same numpy/OpenCV operations; and RAW decode cost for the actual camera model.
- [U] The "inscribed rectangle vs total rotation" numbers (§6.3) and a good UX for the shrinking valid region.

---

## 10. References

Theory
1. Fried, D. L. 1966, "Optical Resolution Through a Randomly Inhomogeneous Medium for Very Long and Very Short Exposures", *JOSA* 56, 1372. doi:10.1364/JOSA.56.001372
2. Law, N. M., Mackay, C. D., Baldwin, J. E. 2006, "Lucky imaging: high angular resolution imaging in the visible from the ground", *A&A* 446, 739. doi:10.1051/0004-6361:20053695
3. Law, N. M. 2006/2010, *Lucky Exposures: diffraction-limited astronomical imaging through the atmosphere* (thesis). doi:10.17863/cam.15991
4. "High-efficiency lucky imaging", *MNRAS* (2013). doi:10.1093/mnras/stt507 — [U] author list not re-checked here.
5. Howell, S. B. 2006, *Handbook of CCD Astronomy*, 2nd ed., CUP — CCD SNR equation, read noise, stacking.
6. Fruchter, A. S. & Hook, R. N. 2002, "Drizzle: A Method for the Linear Reconstruction of Undersampled Images", *PASP* 114, 144. doi:10.1086/338393
7. Lupton, R. et al. 2004, "Preparing Red-Green-Blue Images from CCD Data", *PASP* 116, 133. doi:10.1086/382245
8. Fischler, M. A. & Bolles, R. C. 1981, "Random Sample Consensus", *CACM* 24, 381 (RANSAC).
9. Kuglin, C. D. & Hines, D. C. 1975, "The phase correlation image alignment method" (phase correlation).
10. Guizar-Sicairos, M., Thurman, S. T., Fienup, J. R. 2008, "Efficient subpixel image registration algorithms", *Opt. Lett.* 33, 156 (upsampled-DFT refinement used by `skimage`).
11. Bertin, E. & Arnouts, S. 1996, "SExtractor", *A&AS* 117, 393.
12. Barbary, K. 2016, "SEP: Source Extractor as a library", *JOSS* 1(6), 58.
13. Lang, D. et al. 2010, "Astrometry.net", *AJ* 139, 1782.
14. Groth, E. J. 1986, "A pattern-matching algorithm for two-dimensional coordinate lists", *AJ* 91, 1244; Pál & Bakos 2006; Tabur 2007 — asterism-matching lineage.
15. Stetson, P. B. 1989, *Techniques of Least Squares and Stellar Photometry with CCDs* — auto-adaptive weighted averaging (cited by DSS).
16. Evangelidis, G. D. & Psarakis, E. Z. 2008, "Parametric Image Alignment Using Enhanced Correlation Coefficient Maximization", *IEEE TPAMI* 30, 1858 (OpenCV `findTransformECC`).
17. Field rotation: RASC Calgary Centre, "Field Rotation" — <https://calgary.rasc.ca/field_rotation.htm>; R. P. Patera 2022, "Deriving Field Rotation Rate for an Alt-Az Mounted Telescope", viXra:2205.0085 / doi:10.13140/RG.2.2.13335.04007.
18. Read noise: photonstophotos.net "Input-referred Read Noise vs ISO" <https://www.photonstophotos.net/Charts/RN_e.htm>; Clarkvision Canon 6D sensor analysis <https://clarkvision.com/reviews/evaluation-canon-6d/>.
19. PixInsight ScreenTransferFunction (midtones transfer function) documentation.

Software / docs
20. astroalign — Cabral, Sanchez et al., "Astroalign: A Python Module for Astronomical Image Registration", arXiv:1909.02946; repo <https://github.com/quatrope/astroalign> (MIT).
21. SEP — <https://sep.readthedocs.io> (LGPL-3.0+).
22. photutils — <https://photutils.readthedocs.io> (BSD-3).
23. scikit-image `phase_cross_correlation` / `SimilarityTransform` — BSD-3.
24. OpenCV `estimateAffinePartial2D`, `warpAffine`, `phaseCorrelate` — Apache-2.0.
25. DeepSkyStacker — <http://deepskystacker.free.fr/english/technical.htm> (registration + stacking methods), repo <https://github.com/deepskystacker/DSS> (**BSD-3-Clause**).
26. Siril livestacking — <https://siril.readthedocs.io/en/stable/Livestack.html> (GPL-3.0).
27. SharpCap live stacking — <https://docs.sharpcap.co.uk/4.0/16_LiveStacking.htm>; feature tiers <https://www.sharpcap.co.uk/feature-comparison>; forum thread on de-rotation <https://forums.sharpcap.co.uk/viewtopic.php?t=7390> (proprietary).
28. Astro Live Stacker — <https://github.com/deufrai/als>, <https://als-app.org> (GPL-3.0); `src/als/stack.py` uses astroalign + running mean.
29. PlanetarySystemStacker — <https://github.com/Rolf-Hempel/PlanetarySystemStacker> (GPL-3.0), `Documentation/algorithm_summary.pdf`.
30. AutoStakkert! — <https://www.autostakkert.com/wp/guides/>, manual <https://www.astrokraai.nl/software/manual/as2_planet.html> (freeware, closed).
31. ASTAP — <https://www.hnsky.org/astap_astrometric_solving.htm>, repo <https://github.com/han-k59/astap> (**MPL-2.0**).
32. tetra3 — <https://github.com/esa/tetra3> (**Apache-2.0**).
33. FireCapture licence — <https://www.firecapture.de/license.html> (proprietary, private use).
34. INDI (LGPL-2.1) / KStars-Ekos (GPL-2.0+) / N.I.N.A. (MPL-2.0) — as noted in the table.

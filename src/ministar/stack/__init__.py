"""Live stacking: align + accumulate + stretch.

Implemented (brief 15, measured on the fake rig; see 15 §"RUN, built"):

* ``frames``      — one sensor frame prepared in the **sensor** frame: black level,
                    super-pixel bin, sky subtract, optional flat.
* ``transform``   — the known camera model, and the jump from canvas pixels to the
                    current pointing when the mount is bumped.
* ``accumulator`` — sum / count / M2 / seen, with a coverage mask that is geometric
                    and a count map that is photometric.
* ``stretch``     — STF, computed once and frozen, so the preview does not pulse.
* ``stacker``     — the orchestrator: warp, sigma gate, accumulate, crop, encode.

Design decisions recorded in ``docs/research/03``, ``06`` and ``15``:

* Register on a ~1.5 MP downsample, warp at full resolution.
* The inter-frame transform on a fixed tripod is a rotation ``Ω·Δt`` about the
  projected pole plus a translation residual, so most of it is *predictable*
  from the previous solution. Phase-correlate the residual instead of
  re-detecting stars every frame (measured 150-250 ms/frame, or 30-50 ms with
  the predicted-rotation prior).
* **The transform is a homography, not an affine.** The composition of two tangent-plane
  projections is projective; an affine fits to 480 px and is 6.5 px out at the corners after
  15 minutes (15 §1).
* Quality **gate**, not best-N% selection: at 14-85 mm the PSF is a fraction of
  a pixel, so frame selection has nothing to sharpen. Use FWHM/roundness/star
  count to reject clouds, dew, trails and focus drift.
* Siril is for the end-of-session restack only — its headless live loop rejects
  over a 2-sample window, which is a no-op (06 §7).
"""

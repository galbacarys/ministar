"""Live stacking: align + accumulate + stretch.

Not implemented. Design decisions recorded in ``docs/research/03`` and ``06``:

* Register on a ~1.5 MP downsample, warp at full resolution.
* The inter-frame transform on a fixed tripod is a rotation ``Ω·Δt`` about the
  projected pole plus a translation residual, so most of it is *predictable*
  from the previous solution. Phase-correlate the residual instead of
  re-detecting stars every frame (measured 150-250 ms/frame, or 30-50 ms with
  the predicted-rotation prior).
* Quality **gate**, not best-N% selection: at 14-85 mm the PSF is a fraction of
  a pixel, so frame selection has nothing to sharpen. Use FWHM/roundness/star
  count to reject clouds, dew, trails and focus drift.
* Siril is for the end-of-session restack only — its headless live loop rejects
  over a 2-sample window, which is a no-op (06 §7).
"""

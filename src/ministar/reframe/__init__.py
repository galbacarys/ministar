"""Reframing: tell the user which way to turn the tripod head.

Not implemented. The specification is ``docs/research/07-altaz-reframing-spec.md``.

Summary of the contract, so the shape is visible:

* ``PA_y = atan2(CD1_2, CD2_2)`` — column 2, not ``CD2_1``.
* ``q`` is the parallactic angle; ``psi = PA_y - q`` is the camera roll relative
  to the horizon. With an upright camera at 45 deg latitude, ``psi`` spans 0-121
  degrees across the sky, so "image up = sky up" is badly wrong.
* Display ``Δaz·cos(h)`` (the on-sky error), not raw ``Δaz``; the two differ by
  11x at 85 degrees altitude.
* Suppress the per-axis azimuth readout above 87 degrees altitude: the Jacobian
  is ``diag(cos h, 1)``, so the condition number is ``1/cos h``.
* Compute the offset **twice** — via the WCS to pixels, and via alt/az and
  ``psi`` — and assert they agree within a pixel. That assertion catches every
  sign error this feature is prone to.
"""

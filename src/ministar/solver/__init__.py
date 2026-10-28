"""Plate solving. See ``docs/research/01-astap-cli.md``."""

from ministar.solver.astap import (
    BLIND_RADIUS_DEG,
    EXIT_MEANINGS,
    AstapResult,
    Solution,
    SolveHints,
    parse_ini,
    solve,
)


def solve_with_fallback(image, astap, *, out_base=None, hints=None):
    """Solve with the hint, and again blind if the hint finds nothing.

    A hinted solve is ~0.9 s against 4-8 s blind (01 §8) and is right almost always. "Almost"
    is the problem: a bumped mount, or a mount that turns out to be tracking, puts the frame
    outside the search circle -- and a failed hinted solve is indistinguishable from a bad
    image, so the user would go looking for a hardware fault that is not there.

    One blind retry costs a few seconds in the rare case and removes a whole class of
    confusing failures. Returns ``(result, used_blind)``.
    """
    from dataclasses import replace

    result = solve(image, astap, out_base=out_base, hints=hints)
    if result.solved or hints is None:
        return result, False

    blind = replace(hints, ra_hours=None, south_pole_distance_deg=None, radius_deg=BLIND_RADIUS_DEG)
    return solve(image, astap, out_base=out_base, hints=blind), True


__all__ = [
    "BLIND_RADIUS_DEG",
    "EXIT_MEANINGS",
    "AstapResult",
    "Solution",
    "SolveHints",
    "parse_ini",
    "solve",
    "solve_with_fallback",
]

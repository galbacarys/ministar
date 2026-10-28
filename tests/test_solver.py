def test_a_missed_hint_falls_back_to_a_blind_solve(monkeypatch) -> None:
    """A bumped mount must not look like a broken camera."""
    from ministar.solver import AstapResult, SolveHints, astap, solve_with_fallback

    calls: list[SolveHints | None] = []

    def fake_solve(image, astap_cli, *, out_base=None, hints=None):
        calls.append(hints)
        # Hunted around a stale position: nothing there. Blind: found it.
        return AstapResult(solved=hints.ra_hours is None)

    monkeypatch.setattr("ministar.solver.solve", fake_solve)
    assert astap  # the constant this test was seeded from still exists

    hinted = SolveHints(fov_height_deg=6.5, ra_hours=10.0, south_pole_distance_deg=131.0)
    result, used_blind = solve_with_fallback("frame.fit", "astap", hints=hinted)

    assert used_blind
    assert result.solved
    assert len(calls) == 2
    assert calls[0] is hinted
    assert calls[1].ra_hours is None and calls[1].radius_deg == 180.0
    assert calls[1].fov_height_deg == 6.5, "the field of view is still known, so still used"


def test_a_good_hint_is_never_retried(monkeypatch) -> None:
    from ministar.solver import AstapResult, SolveHints, solve_with_fallback

    calls: list[SolveHints | None] = []

    def fake_solve(image, astap_cli, *, out_base=None, hints=None):
        calls.append(hints)
        return AstapResult(solved=True)

    monkeypatch.setattr("ministar.solver.solve", fake_solve)

    result, used_blind = solve_with_fallback(
        "frame.fit", "astap", hints=SolveHints(fov_height_deg=6.5, ra_hours=10.0)
    )

    assert result.solved and not used_blind
    assert len(calls) == 1, "a hit costs one solve, not two"

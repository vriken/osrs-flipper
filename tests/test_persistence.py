"""Spread persistence must separate a real spread (Jug) from integer churn (Earth rune)."""

from osrs_flipper.persistence import spread_stats


def test_stable_spread_is_capturable():
    # constant 2gp spread, flat mid -> fully realizable
    highs = [14] * 60
    lows = [12] * 60
    s = spread_stats(highs, lows)
    assert s["realizable_spread"] == 2
    assert s["persist"] == 1.0
    assert s["persist_factor"] == 1.0


def test_integer_churn_is_not_capturable():
    # mid whipsaws as much as the spread is wide -> realizable collapses to ~0
    highs = [6 if i % 2 else 5 for i in range(60)]
    lows = [5 if i % 2 else 4 for i in range(60)]
    s = spread_stats(highs, lows)
    assert s["realizable_spread"] <= 0
    assert s["persist_factor"] == 0.0


def test_partial_noise_gives_intermediate_factor():
    # 2gp spread, but mid drifts ~0.5/bar -> ~75% of the spread survives
    highs = [14 if i % 2 else 15 for i in range(60)]
    lows = [12 if i % 2 else 13 for i in range(60)]
    s = spread_stats(highs, lows)
    assert 0.0 < s["persist_factor"] < 1.0


def test_insufficient_history_returns_none():
    assert spread_stats([14] * 10, [12] * 10) is None


def test_nulls_are_dropped_before_stats():
    highs = ([14] * 50) + [None] * 10
    lows = ([12] * 50) + [None] * 10
    s = spread_stats(highs, lows)
    assert s["n_bars"] == 50

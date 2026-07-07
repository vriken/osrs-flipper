"""Spread persistence must separate a real spread (Jug) from integer churn (Earth rune)."""

import pytest

from osrs_flipper.persistence import reliability_stats, spread_stats


def _bars(specs):
    """specs: list of (avgHighPrice, avgLowPrice) → /timeseries-shaped bars."""
    return [{"avgHighPrice": h, "avgLowPrice": low} for h, low in specs]


# --- fast margin-decay (5m reliability): catch a spread that collapses within minutes --------------

def test_stable_margin_is_not_penalised():
    # every bar's post-tax net (98−90=8) clears half the quote (4) → uptime 1.0, no penalty
    s = reliability_stats(_bars([(100, 90)] * 12), item_id=2, quoted_net=8)
    assert s["uptime"] == 1.0 and s["gone_frac"] == 0.0 and s["reliab_mult"] == 1.0


def test_fleeting_margin_is_downweighted():
    # half the bars have the spread (net 8), half collapsed (91−90 → net 0) — the Ultracompost case
    s = reliability_stats(_bars([(100, 90)] * 6 + [(91, 90)] * 6), item_id=2, quoted_net=8)
    assert s["uptime"] == pytest.approx(0.5) and s["gone_frac"] == pytest.approx(0.5)
    assert s["reliab_mult"] == pytest.approx(0.4 + 0.6 * 0.5)   # floor + slope·uptime = 0.7


def test_threshold_is_relative_to_the_quote():
    # net 8 is real, but if we're quoting 100 then 8 is <half the quote → not "healthy"
    s = reliability_stats(_bars([(100, 90)] * 12), item_id=2, quoted_net=100)
    assert s["uptime"] == 0.0 and s["reliab_mult"] == 0.4       # floored


def test_thin_history_is_neutral_not_penalised():
    s = reliability_stats(_bars([(100, 90)] * 4), item_id=2, quoted_net=8)   # < RELIAB_MIN_BARS
    assert s["thin"] is True and s["reliab_mult"] == 1.0


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

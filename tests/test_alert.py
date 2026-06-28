"""Portfolio summary greedily splits cash across slots — verify the allocation math."""

import pandas as pd

from osrs_flipper.alert import format_portfolio_summary, format_sell_plan


def test_format_sell_plan():
    rows = [{"name": "Anchovy pizza", "qty": 51, "avg_cost": 450.0, "sell_px": 470,
             "profit": 561.0, "eta_h": 0.1}]
    s = format_sell_plan(rows)
    assert "Anchovy pizza" in s and "470" in s and "+561" in s
    assert format_sell_plan([]) == ""  # nothing to sell → no section


def _df(rows):
    return pd.DataFrame(rows)


def test_fully_deployed_no_warning():
    df = _df([
        {"capital_deployed": 30_000, "exp_gp_cycle_adj": 3_000},
        {"capital_deployed": 80_000, "exp_gp_cycle_adj": 8_000},
    ])
    s = format_portfolio_summary(df, bankroll=100_000, slots=3)
    # 30k fully + 70k of the 80k (pro-rated gp 8000*0.875=7000) → 100k deployed, 10k gp/cycle
    assert "100,000 of 100,000" in s
    assert "10,000 gp/cycle" in s
    assert "idle" in s and "⚠" not in s  # 0 idle, no warning


def test_idle_cash_triggers_warning():
    df = _df([{"capital_deployed": 12_000, "exp_gp_cycle_adj": 441}])
    s = format_portfolio_summary(df, bankroll=100_000, slots=3)
    assert "88,000 idle" in s
    assert "⚠" in s


def test_empty_or_no_bankroll_returns_blank():
    assert format_portfolio_summary(pd.DataFrame(), 100_000) == ""
    assert format_portfolio_summary(_df([{"capital_deployed": 1, "exp_gp_cycle_adj": 1}]), 0) == ""

"""Progress series + growth-rate fit + ETA math (pure; rendering is smoke-tested separately)."""

from osrs_flipper.progress import build_history, eta_days, fit_daily_rate


def test_build_history_is_initial_plus_cumulative_realized():
    # two buys (cash out, no pnl), then a sell booking +100 — net worth moves only by the spread
    rows = [(100, -1000, 0.0), (200, -500, 0.0), (300, 1600, 100.0)]
    initial, times, nw = build_history(rows, cash_now=1000)
    assert initial == 900           # 1000 - sum(cash_delta=100)
    assert times == [100, 200, 300]
    assert nw == [900, 900, 1000]   # flat through buys, +100 on the profitable sell


def test_fit_daily_rate_basic():
    # +200 realized on a 1000 base over exactly one day → 20%/day
    rate, span = fit_daily_rate([(0, -1000, 0.0), (86400, 1200, 200.0)], base=1000)
    assert span == 1.0
    assert abs(rate - 0.20) < 1e-9


def test_fit_daily_rate_too_little_history_is_none():
    rate, _ = fit_daily_rate([(0, -1000, 0.0), (60, 1200, 200.0)], base=1000)  # 60s span
    assert rate is None


def test_eta_days_compounds_and_guards():
    assert abs(eta_days(1.0, 2.0, 0.0718) - 10.0) < 0.2   # ~doubling at 7.18%/day ≈ 10 days
    assert eta_days(2.0, 1.0, 0.05) is None               # already past target
    assert eta_days(1.0, 2.0, 0.0) is None                # no growth → unreachable

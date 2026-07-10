"""Progress series + active-time growth fit + Monte-Carlo projection (pure; rendering is smoke-tested)."""

import numpy as np

from osrs_flipper import progress
from osrs_flipper.progress import build_history, crossing_stats, eta_days, fit_growth, simulate_paths


def test_build_history_is_initial_plus_cumulative_realized():
    # two buys (cash out, no pnl), then a sell booking +100 — net worth moves only by the spread
    rows = [(100, -1000, 0.0), (200, -500, 0.0), (300, 1600, 100.0)]
    initial, times, nw = build_history(rows, cash_now=1000)
    assert initial == 900           # 1000 - sum(cash_delta=100)
    assert times == [100, 200, 300]
    assert nw == [900, 900, 1000]   # flat through buys, +100 on the profitable sell


def test_fit_growth_basic():
    # +200 realized on a 1000 base over exactly one active day → 20%/day
    rate, vol, active = fit_growth([(0, -1000, 0.0), (86400, 1200, 200.0)], base=1000)
    assert abs(active - 1.0) < 1e-9
    assert abs(rate - 0.20) < 1e-9
    assert vol >= 0.0


def test_fit_growth_too_little_history_is_none():
    rate, _, _ = fit_growth([(0, -1000, 0.0), (60, 1200, 200.0)], base=1000)  # 60s active span
    assert rate is None


def test_fit_growth_excludes_idle_gaps():
    # +200 in the first hour, a 3-DAY idle gap (nothing growing), then +200 more an hour later.
    rows = [(0, -1000, 0.0), (3600, 1200, 200.0), (259200, -1000, 0.0), (262800, 1200, 200.0)]
    rate, _vol, active = fit_growth(rows, base=1000, gap_max_h=24)
    wall = (rows[-1][0] - rows[0][0]) / 86400
    assert wall > 3.0                                    # ~3 wall-clock days elapsed
    assert active < 1.2                                  # but only ~1.08 ACTIVE days (idle gap clamped)
    assert abs(rate - (400 / 1000) / active) < 1e-9      # rate is per ACTIVE day, not diluted by idle
    # excluding the idle gap yields a HIGHER (undiluted) rate than counting wall-clock would
    assert rate > (400 / 1000) / wall


def test_eta_days_compounds_and_guards():
    assert abs(eta_days(1.0, 2.0, 0.0718) - 10.0) < 0.2   # ~doubling at 7.18%/day ≈ 10 days
    assert eta_days(2.0, 1.0, 0.05) is None               # already past target
    assert eta_days(1.0, 2.0, 0.0) is None                # no growth → unreachable


def test_simulate_paths_deterministic_shape_and_growth():
    p = simulate_paths(1_000_000, 0.05, 0.02, 30, n_paths=500, seed=1)
    assert p.shape == (31, 500)
    assert (p[0] == 1_000_000).all()
    assert np.median(p[-1]) > 1_000_000                   # positive drift → median grows
    assert (p == simulate_paths(1_000_000, 0.05, 0.02, 30, n_paths=500, seed=1)).all()  # reproducible


def test_simulate_paths_drift_decays_at_scale():
    # vol 0 so it's deterministic: a stack far ABOVE the pivot barely drifts; one far below compounds
    small = simulate_paths(1_000, 0.10, 0.0, 5, n_paths=1, seed=3, decay_pivot=20_000_000)
    big = simulate_paths(1_000_000_000, 0.10, 0.0, 5, n_paths=1, seed=3, decay_pivot=20_000_000)
    assert small[-1, 0] / small[0, 0] > 1.4               # ~1.1**5 ≈ 1.61
    assert big[-1, 0] / big[0, 0] < 1.02                  # drift throttled toward 0 at 50× the pivot


def test_crossing_stats_reports_reach_prob_and_day_distribution():
    p = simulate_paths(1_000_000, 0.10, 0.01, 30, n_paths=500, seed=2)
    cs = crossing_stats(p, 2_000_000)
    assert 0.0 < cs["prob"] <= 1.0
    assert cs["median"] is not None and 0 < cs["median"] <= 30
    assert cs["p10"] <= cs["median"] <= cs["p90"]
    unreachable = crossing_stats(p, 10 ** 15)             # far beyond any path in the horizon
    assert unreachable["prob"] == 0.0 and unreachable["median"] is None


def test_percentile_fan_is_ordered_and_full_length():
    p = simulate_paths(1_000_000, 0.05, 0.03, 20, n_paths=800, seed=4)
    fan = progress.percentile_fan(p)
    assert (fan[10] <= fan[50]).all() and (fan[50] <= fan[90]).all()
    assert len(fan[50]) == 21

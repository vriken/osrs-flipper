"""Adaptive objective: gp/hour by default, ramping to variance-penalised as Sharpe RISES above its
trailing baseline (a competition regime CHANGE, not an absolute level)."""

import pytest

from osrs_flipper import objective


def test_realized_sharpe_guards():
    assert objective.realized_sharpe(0.05, 0.10) == 0.5
    assert objective.realized_sharpe(None, 0.1) is None
    assert objective.realized_sharpe(0.05, 0) is None      # zero vol → undefined
    assert objective.realized_sharpe(0.0, 0.1) is None     # no drift → treat as no signal


def test_baseline_seeds_then_tracks_slowly():
    assert objective.update_baseline(3.4, None) == 3.4          # first reading seeds the baseline
    assert objective.update_baseline(None, 3.4) == 3.4          # no new signal → unchanged
    b = objective.update_baseline(4.4, 3.4, alpha=0.10)         # slow EWMA toward the new value
    assert b == pytest.approx(3.5)                              # 0.1*4.4 + 0.9*3.4


def test_lambda_zero_at_or_below_baseline():
    kw = dict(base=0.0, rise_full=1.0, lam_max=1.0)
    # a HIGH absolute Sharpe that equals its baseline → NO tilt (the old absolute bug: this fired λ=1)
    assert objective.variance_aversion(3.4, 3.4, **kw) == 0.0
    assert objective.variance_aversion(2.0, 3.4, **kw) == 0.0   # below baseline → floor
    assert objective.variance_aversion(3.4, None, **kw) == 0.0  # no baseline yet → floor
    assert objective.variance_aversion(None, 3.4, **kw) == 0.0  # no signal → floor


def test_lambda_ramps_with_the_rise():
    kw = dict(base=0.0, rise_full=1.0, lam_max=1.0)
    assert objective.variance_aversion(3.9, 3.4, **kw) == pytest.approx(0.5)  # +0.5 rise → half tilt
    assert objective.variance_aversion(4.4, 3.4, **kw) == 1.0                 # +1.0 rise → full
    assert objective.variance_aversion(9.0, 3.4, **kw) == 1.0                 # bigger rise → clamped


def test_floor_honoured_and_disable():
    assert objective.variance_aversion(2.0, 3.4, base=0.3, rise_full=1.0, lam_max=1.0) == 0.3  # floor
    assert objective.variance_aversion(9.0, 3.4, base=0.0, rise_full=1.0, lam_max=0.0) == 0.0  # disabled

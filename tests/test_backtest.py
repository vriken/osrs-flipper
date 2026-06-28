"""Engine mechanics: capacity caps, partial-fill MTM bail-out, and no look-ahead."""

import pandas as pd

from osrs_flipper.backtest.engine import _simulate_item
from osrs_flipper.backtest.strategies.margin_flip import MarginFlip


def _series(high_vol_at_exit=1000):
    return pd.DataFrame({
        "ts": [0, 3600, 7200, 10800],
        "avg_high": [100, 100, 100, 100],
        "avg_low": [90, 90, 90, 90],
        "high_vol": [1000, high_vol_at_exit, high_vol_at_exit, high_vol_at_exit],
        "low_vol": [1000, 1000, 1000, 1000],
    })


def test_profitable_flip_completes_with_zero_haircut():
    trades = _simulate_item(MarginFlip(), _series(), item_id=1, limit=10_000,
                            capital=10_000_000, beta=0.0)
    assert trades
    t = trades[0]
    assert t["completed"]
    assert t["pnl"] > 0  # buy 90, sell 100, tax 2 -> positive
    assert t["units"] > 0


def test_buy_limit_caps_units():
    trades = _simulate_item(MarginFlip(), _series(), item_id=1, limit=5,
                            capital=10_000_000, beta=0.0)
    assert all(t["units"] <= 5 for t in trades)


def test_capital_caps_units():
    # bankroll only affords a few units at buy_px ~90
    trades = _simulate_item(MarginFlip(), _series(), item_id=1, limit=10_000,
                            capital=450, beta=0.0)
    assert trades and all(t["units"] <= 450 // 90 + 1 for t in trades)


def test_unsold_inventory_is_marked_to_market_as_loss():
    # no instant-buy volume at exit -> nothing sells -> remainder dumped at a loss
    trades = _simulate_item(MarginFlip(), _series(high_vol_at_exit=0), item_id=1, limit=10_000,
                            capital=10_000_000, beta=0.0)
    assert trades
    t = trades[0]
    assert not t["completed"]
    assert t["sold"] == 0
    assert t["pnl"] < 0  # bailed out below cost (tax on the dump)


def test_haircut_reduces_pnl_vs_zero_beta():
    base = _simulate_item(MarginFlip(), _series(), item_id=1, limit=10_000, capital=10_000_000, beta=0.0)
    cut = _simulate_item(MarginFlip(), _series(), item_id=1, limit=10_000, capital=10_000_000, beta=0.5)
    assert sum(t["pnl"] for t in cut) < sum(t["pnl"] for t in base)

"""Feature gating must keep stale/ghost items out and compute margins after tax."""

import time

from osrs_flipper.features import build_features

NOW = 1_700_000_000


def _mapping(item_id, name, *, members=False, limit=1000):
    return {"id": item_id, "name": name, "members": members, "limit": limit,
            "value": 100, "highalch": 60}


def _latest(high, low, age_s):
    t = NOW - age_s
    return {"high": high, "highTime": t, "low": low, "lowTime": t}


def _hourly(ah, al, hv, lv):
    return {"avgHighPrice": ah, "avgLowPrice": al, "highPriceVolume": hv, "lowPriceVolume": lv}


def test_fresh_liquid_item_is_tradeable():
    m = [_mapping(1, "Oak logs")]
    df = build_features({1: _latest(33, 29, 60)}, {1: _hourly(33, 29, 5000, 5000)}, m, now_ts=NOW)
    assert df.loc[0, "tradeable"]
    assert df.loc[0, "margin_abs"] > 0


def test_stale_item_is_not_tradeable():
    m = [_mapping(1, "Ghost")]
    df = build_features({1: _latest(33, 29, 999_999)}, {1: _hourly(33, 29, 5000, 5000)}, m, now_ts=NOW)
    assert not df.loc[0, "tradeable"]


def test_thin_volume_is_not_tradeable():
    m = [_mapping(1, "Thin")]
    df = build_features({1: _latest(33, 29, 60)}, {1: _hourly(33, 29, 5, 5)}, m, now_ts=NOW)
    assert not df.loc[0, "tradeable"]


def test_null_price_ghost_is_dropped():
    m = [_mapping(1, "Nulled")]
    # no avg prices and no latest low -> cannot price, must be excluded entirely
    df = build_features({1: {"high": 100, "highTime": NOW, "low": None, "lowTime": None}},
                        {1: {"avgHighPrice": None, "avgLowPrice": None,
                             "highPriceVolume": 0, "lowPriceVolume": 0}}, m, now_ts=NOW)
    assert df.empty


def test_capital_binds_quantity_for_small_bankroll():
    m = [_mapping(1, "Pricey", limit=10_000)]
    df = build_features({1: _latest(2000, 1900, 60)}, {1: _hourly(2000, 1900, 100_000, 100_000)},
                        m, bankroll=200_000, now_ts=NOW)
    assert df.loc[0, "bound_by"] == "capital"
    assert df.loc[0, "capacity"] == 200_000 // df.loc[0, "buy_px"]


def test_fast_margin_collapses_on_thin_spread():
    # a 2gp spread (bid 12 / ask 14) profits if you wait, but queue-jumping (13/13) doesn't
    m = [_mapping(1, "Jug")]
    df = build_features({1: _latest(14, 12, 60)}, {1: _hourly(14, 12, 5000, 5000)}, m, now_ts=NOW)
    assert df.loc[0, "margin_abs"] > 0       # patient flip profits
    assert df.loc[0, "margin_fast"] <= 0     # fast-fill (queue-jump) does not


def test_crossed_live_book_is_dropped():
    # live bid 407 > ask 258 (inverted, illiquid) → item excluded even if 1h avg looks fat
    m = [_mapping(1, "Mithril warhammer")]
    df = build_features({1: _latest(258, 407, 60)}, {1: _hourly(963, 694, 900, 200)}, m, now_ts=NOW)
    assert df.empty


def test_buy_limit_used_reduces_capacity():
    m = [_mapping(1, "Pricey", limit=1000)]
    lat, h1 = {1: _latest(2000, 1900, 60)}, {1: _hourly(2000, 1900, 100_000, 100_000)}
    # fully used 4h limit → no room → capacity 0 (item drops out of scan/port)
    maxed = build_features(lat, h1, m, bankroll=10**9, now_ts=NOW, limit_used={1: 1000})
    assert maxed.loc[0, "buy_limit_eff"] == 0
    assert maxed.loc[0, "capacity"] == 0
    # partially used → remaining room
    partial = build_features(lat, h1, m, bankroll=10**9, now_ts=NOW, limit_used={1: 600})
    assert partial.loc[0, "buy_limit_eff"] == 400


def test_now_default_runs():
    m = [_mapping(1, "X")]
    df = build_features({1: _latest(33, 29, 60)}, {1: _hourly(33, 29, 5000, 5000)}, m,
                        now_ts=int(time.time()))
    assert len(df) == 1

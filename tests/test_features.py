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


def test_thin_cheap_volume_is_not_tradeable():
    # cheap + thin: 5/h at ~30gp = ~155 gp/h turnover → fails BOTH the units and the turnover branch
    m = [_mapping(1, "Thin")]
    df = build_features({1: _latest(33, 29, 60)}, {1: _hourly(33, 29, 5, 5)}, m, now_ts=NOW)
    assert not df.loc[0, "tradeable"]


def test_expensive_low_volume_item_tradeable_via_turnover():
    # Karil's-like: ~522k each, ~16 two-sided trades/h → fails the 500-unit floor, but ~8.4M gp/h
    # turnover clears the turnover branch, and it sizes to ≥1 unit despite α·vol flooring to 0.
    m = [_mapping(1, "Karil's leatherskirt", limit=15)]
    df = build_features({1: _latest(530_000, 515_000, 60)}, {1: _hourly(530_000, 515_000, 13, 3)},
                        m, bankroll=10**9, now_ts=NOW)
    assert df.loc[0, "turnover_1h"] >= 1_000_000
    assert df.loc[0, "tradeable"]      # admitted by gp turnover, not unit count
    assert df.loc[0, "capacity"] >= 1  # not zeroed by α×vol; buy-limit/bankroll still bind

    # but unaffordable on a small bankroll → capacity 0, so it never reaches the scan
    broke = build_features({1: _latest(530_000, 515_000, 60)}, {1: _hourly(530_000, 515_000, 13, 3)},
                           m, bankroll=200_000, now_ts=NOW)
    assert broke.loc[0, "capacity"] == 0


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


def test_diverged_live_vs_average_dropped():
    # live ~410 mid but 1h-avg ~925 mid (deflating pump) → too divergent to trust → dropped
    m = [_mapping(1, "Pump")]
    df = build_features({1: _latest(420, 400, 60)}, {1: _hourly(950, 900, 500, 500)}, m, now_ts=NOW)
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


def test_adverse_downward_move_relative_to_margin_drops():
    # 1h-avg mid 1000 (spread 950/1050 → ~3% modeled margin), but live has fallen to mid 970
    # (960/980): a 3% adverse drop > 50% of the ~3% margin → falling knife, dropped.
    m = [_mapping(1, "Falling")]
    df = build_features({1: _latest(980, 960, 60)}, {1: _hourly(1050, 950, 5000, 5000)}, m, now_ts=NOW)
    assert df.empty


def test_small_adverse_move_within_margin_survives():
    # fat spread (~7% margin): a small live dip to mid 1080 (~1.8% drop) stays under the
    # margin-relative threshold (~3.7%) → kept.
    m = [_mapping(1, "Fat")]
    df = build_features({1: _latest(1085, 1075, 60)}, {1: _hourly(1200, 1000, 5000, 5000)}, m, now_ts=NOW)
    assert not df.empty
    assert df.loc[0, "tradeable"]


def test_wide_spread_low_volume_is_suspect():
    # a fat % spread on thin volume (Curry-leaf-like: 40→59 @ ~900/h) is an illiquidity artifact,
    # not a capturable flip → flagged suspect (and dropped from the default scan)
    m = [_mapping(1, "Trap")]
    df = build_features({1: _latest(59, 40, 60)}, {1: _hourly(59, 40, 900, 900)}, m, now_ts=NOW)
    assert df.loc[0, "suspect"]


def test_wide_spread_high_volume_not_suspect():
    # a penny staple (fat % spread but huge volume — Air rune 4→5) is real, must NOT be flagged
    m = [_mapping(1, "Air rune")]
    df = build_features({1: _latest(5, 4, 60)}, {1: _hourly(5, 4, 159_000, 159_000)}, m, now_ts=NOW)
    assert not df.loc[0, "suspect"]


def test_wide_spread_stale_leg_is_suspect():
    # wide spread measured with one trade leg 40 min stale = phantom margin across two regimes
    m = [_mapping(1, "Stale")]
    lat = {"high": 100, "highTime": NOW - 60, "low": 70, "lowTime": NOW - 2400}  # low leg 40m old
    # volume (50k) is high enough to clear the spread-vs-volume gate, so suspect here is purely
    # the stale-leg clause firing
    df = build_features({1: lat}, {1: _hourly(100, 70, 50_000, 50_000)}, m, now_ts=NOW)
    assert df.loc[0, "suspect"]


def test_hold_units_capped_by_realizable_volume():
    # illiquid item: hold qty capped by ALPHA(0.1)×vol(900)×HOLD_WINDOW_H(8)=720, not the buy limit
    m = [_mapping(1, "Thinhold", limit=10_000)]
    df = build_features({1: _latest(33, 30, 60)}, {1: _hourly(33, 30, 900, 900)}, m, now_ts=NOW)
    assert df.loc[0, "hold_units"] == 720
    # liquid item: the buy limit binds instead of volume
    big = build_features({1: _latest(33, 30, 60)}, {1: _hourly(33, 30, 500_000, 500_000)}, m, now_ts=NOW)
    assert big.loc[0, "hold_units"] == 10_000


def test_patient_beta_captures_more_of_the_spread():
    # at beta 0 you post AT the bid/ask → margin is the whole spread minus tax, not the inside-haircut
    m = [_mapping(1, "Karil's leatherskirt", limit=15)]
    lat, h1 = {1: _latest(530_000, 506_000, 60)}, {1: _hourly(530_000, 506_000, 13, 3)}
    default = build_features(lat, h1, m, bankroll=10**9, now_ts=NOW)
    patient = build_features(lat, h1, m, bankroll=10**9, now_ts=NOW, beta=0.0)
    assert patient.loc[0, "margin_abs"] > default.loc[0, "margin_abs"]   # full spread > inside-haircut


def test_one_sided_volume_still_tradeable_via_total_turnover():
    # a low-frequency item that traded only the high side this hour (low_vol 0) → binding min is 0,
    # but ~13 × ~518k of value moved, so the both-side turnover gate still admits it and sizes ≥1
    m = [_mapping(1, "Karil's leatherskirt", limit=15)]
    df = build_features({1: _latest(530_000, 506_000, 60)}, {1: _hourly(530_000, 506_000, 13, 0)},
                        m, bankroll=10**9, now_ts=NOW)
    assert df.loc[0, "vol_1h_binding"] == 0   # thin/min leg empty this hour
    assert df.loc[0, "tradeable"] and df.loc[0, "hold_units"] >= 1


def test_relaxed_staleness_keeps_low_frequency_item():
    # a 90-min-old trade is dropped by the default 1h ghost gate, kept when staleness is relaxed
    m = [_mapping(1, "Karil's leatherskirt", limit=15)]
    lat, h1 = {1: _latest(530_000, 506_000, 5400)}, {1: _hourly(530_000, 506_000, 13, 3)}
    assert not build_features(lat, h1, m, bankroll=10**9, now_ts=NOW).loc[0, "tradeable"]
    assert build_features(lat, h1, m, bankroll=10**9, now_ts=NOW, staleness_max=21_600).loc[0, "tradeable"]


def test_now_default_runs():
    m = [_mapping(1, "X")]
    df = build_features({1: _latest(33, 29, 60)}, {1: _hourly(33, 29, 5000, 5000)}, m,
                        now_ts=int(time.time()))
    assert len(df) == 1

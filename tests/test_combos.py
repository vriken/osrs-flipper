"""Combination pricing must model the GE tax-per-leg asymmetry correctly, pick the profitable direction,
size against capital ∩ buy-limit ∩ liquidity, and gate out any leg that can't be priced or is illiquid.

The crux: tax is paid only on the SOLD leg — ASSEMBLE taxes the set once, BREAK taxes each piece — and
the 5M/item cap can flip which direction wins. These are pure functions over a synthetic feature map, so
no network is needed.
"""

import time

from osrs_flipper.combinations import Combo
from osrs_flipper.combos import price_combination, scan_combinations
from osrs_flipper.tax import ge_tax


def leg(iid, name, buy, sell, *, limit=1000, eff=1000, hold=1000, tradeable=True,
        suspect=False, members=True, eta=1.0):
    """One build_features-shaped row (dict form) for a single item."""
    return {"item_id": iid, "name": name, "members": members, "buy_px": buy, "sell_px": sell,
            "buy_limit": limit, "buy_limit_eff": eff, "hold_units": hold, "tradeable": tradeable,
            "suspect": suspect, "fill_eta_h": eta}


def two_piece(output_id=900, a=1, b=2, **kw):
    return Combo(id="t", name="Test set", output_id=output_id, inputs=((a, 1), (b, 1)), **kw)


# --- the tax-per-leg asymmetry ---------------------------------------------------------------------

def test_assemble_taxes_output_once():
    combo = two_piece()
    feat = {1: leg(1, "p1", 100, 90), 2: leg(2, "p2", 100, 90), 900: leg(900, "set", 320, 300)}
    r = price_combination(combo, feat, cash=10_000, members=True)
    assert r["direction"] == "ASSEMBLE"
    assert r["proceeds_per_conv"] == 300 - ge_tax(300)          # taxed once, on the set
    assert r["tax_per_conv"] == ge_tax(300) == 6
    assert r["profit_per_conv"] == (300 - 6) - 200


def test_break_taxes_each_piece():
    combo = two_piece()
    # set sells barely above its buy; pieces sell well above → BREAK wins, and tax is charged TWICE
    feat = {1: leg(1, "p1", 100, 150), 2: leg(2, "p2", 100, 150), 900: leg(900, "set", 200, 210)}
    r = price_combination(combo, feat, cash=10_000, members=True)
    assert r["direction"] == "BREAK"
    assert r["proceeds_per_conv"] == (150 - ge_tax(150)) * 2    # per-piece tax, not one lump
    assert r["tax_per_conv"] == ge_tax(150) * 2 == 6            # tax deducted TWICE, once per sold piece


def test_direction_selection_picks_higher_profit():
    combo = two_piece()
    assemble_wins = {1: leg(1, "p1", 100, 90), 2: leg(2, "p2", 100, 90), 900: leg(900, "set", 320, 300)}
    break_wins = {1: leg(1, "p1", 100, 150), 2: leg(2, "p2", 100, 150), 900: leg(900, "set", 200, 210)}
    assert price_combination(combo, assemble_wins, cash=10_000, members=True)["direction"] == "ASSEMBLE"
    assert price_combination(combo, break_wins, cash=10_000, members=True)["direction"] == "BREAK"


def test_break_skipped_when_irreversible():
    # Pieces sell high (BREAK would net far more), but the recipe is one-way — it must never pick BREAK.
    combo = two_piece(reversible=False, kind="recipe")
    feat = {1: leg(1, "p1", 100, 150), 2: leg(2, "p2", 100, 150), 900: leg(900, "set", 200, 205)}
    r = price_combination(combo, feat, cash=10_000, members=True)
    assert r["direction"] == "ASSEMBLE"          # the only legal direction
    assert r["bought_ids"] == (1, 2)


def test_5m_tax_cap_flips_direction_on_high_value_set():
    # Very expensive set: ASSEMBLE pays ONE capped 5M tax on the set; BREAK pays 4 sub-cap taxes on the
    # pieces. The cap on the single large sale is what tips the winner to ASSEMBLE.
    combo = Combo(id="big", name="Ancestral", output_id=900, inputs=((1, 1), (2, 1), (3, 1), (4, 1)))
    pieces = {i: leg(i, f"p{i}", 73_000_000, 75_000_000) for i in (1, 2, 3, 4)}
    feat = {**pieces, 900: leg(900, "set", 291_600_000, 300_000_000)}
    r = price_combination(combo, feat, cash=10 ** 12, members=True)
    assert ge_tax(300_000_000) == 5_000_000            # capped (2% would be 6M)
    assert ge_tax(75_000_000) == 1_500_000             # per-piece, under the cap
    assert r["direction"] == "ASSEMBLE"
    assert r["tax_per_conv"] == 5_000_000
    assert r["profit_per_conv"] == 3_000_000           # (300M-5M) - 292M ; BREAK would net only 2.4M


# --- sizing ----------------------------------------------------------------------------------------

def test_buy_limit_binds_across_legs():
    combo = two_piece()
    feat = {1: leg(1, "p1", 100, 90, eff=2),                 # only 2 of piece 1 left this window
            2: leg(2, "p2", 100, 90, eff=1000),
            900: leg(900, "set", 320, 300)}
    r = price_combination(combo, feat, cash=10_000, members=True)
    assert r["conversions"] == 2
    assert r["bound_by"].startswith("limit:")


def test_no_buy_limit_is_treated_as_unlimited():
    combo = two_piece()
    feat = {1: leg(1, "p1", 100, 90, limit=0, eff=0),        # limit==0 → untracked, NOT zero conversions
            2: leg(2, "p2", 100, 90),
            900: leg(900, "set", 320, 300)}
    r = price_combination(combo, feat, cash=10_000, members=True)
    assert r["conversions"] >= 1


def test_liquidity_and_cash_caps():
    combo = two_piece()
    # cash affords 5 (cost 200/conv), limits & hold generous → capital binds
    feat = {1: leg(1, "p1", 100, 90), 2: leg(2, "p2", 100, 90), 900: leg(900, "set", 320, 300)}
    r = price_combination(combo, feat, cash=1_000, members=True)
    assert r["conversions"] == 5 and r["bound_by"] == "capital"
    # now a thin sell-side on the set caps volume-realizable size
    feat[900] = leg(900, "set", 320, 300, hold=3)
    r = price_combination(combo, feat, cash=1_000_000, members=True)
    assert r["conversions"] == 3 and r["bound_by"].startswith("liquidity:")


# --- gating ----------------------------------------------------------------------------------------

def test_missing_leg_gates_combo():
    combo = two_piece()
    feat = {1: leg(1, "p1", 100, 90), 900: leg(900, "set", 320, 300)}   # piece 2 didn't price
    assert price_combination(combo, feat, cash=10_000, members=True) is None


def test_suspect_or_untradeable_leg_gates_combo():
    combo = two_piece()
    base = {1: leg(1, "p1", 100, 90), 2: leg(2, "p2", 100, 90), 900: leg(900, "set", 320, 300)}
    sus = {**base, 2: leg(2, "p2", 100, 90, suspect=True)}
    unt = {**base, 900: leg(900, "set", 320, 300, tradeable=False)}
    assert price_combination(combo, sus, cash=10_000, members=True) is None
    assert price_combination(combo, unt, cash=10_000, members=True) is None


def test_members_combo_hidden_in_f2p():
    combo = two_piece()
    feat = {1: leg(1, "p1", 100, 90, members=True), 2: leg(2, "p2", 100, 90, members=False),
            900: leg(900, "set", 320, 300, members=False)}
    assert price_combination(combo, feat, cash=10_000, members=False) is None    # a members leg present
    assert price_combination(combo, feat, cash=10_000, members=True) is not None


def test_unprofitable_combo_dropped_unless_kept():
    combo = two_piece()
    feat = {1: leg(1, "p1", 100, 100), 2: leg(2, "p2", 100, 100), 900: leg(900, "set", 300, 190)}
    assert price_combination(combo, feat, cash=10_000, members=True) is None
    kept = price_combination(combo, feat, cash=10_000, members=True, keep_unprofitable=True)
    assert kept is not None and kept["profit_per_conv"] <= 0


def test_bought_ids_target_the_buy_side_for_the_anomaly_gate():
    combo = two_piece()
    assemble = {1: leg(1, "p1", 100, 90), 2: leg(2, "p2", 100, 90), 900: leg(900, "set", 320, 300)}
    brk = {1: leg(1, "p1", 100, 150), 2: leg(2, "p2", 100, 150), 900: leg(900, "set", 200, 210)}
    assert price_combination(combo, assemble, cash=10_000, members=True)["bought_ids"] == (1, 2)
    assert price_combination(combo, brk, cash=10_000, members=True)["bought_ids"] == (900,)


# --- end-to-end through build_features --------------------------------------------------------------

NOW = 1_700_000_000


def _map(iid, name, *, members=False, limit=1000):
    return {"id": iid, "name": name, "members": members, "limit": limit, "value": 100, "highalch": 60}


def _lat(high, low):
    return {"high": high, "highTime": NOW - 60, "low": low, "lowTime": NOW - 60}


def _hr(ah, al):
    return {"avgHighPrice": ah, "avgLowPrice": al, "highPriceVolume": 100_000, "lowPriceVolume": 100_000}


def test_scan_combinations_prices_through_build_features(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW)  # freshness clock
    combo = two_piece(output_id=900, a=1, b=2)
    mapping = [_map(1, "p1"), _map(2, "p2"), _map(900, "Test set")]
    latest = {1: _lat(110, 100), 2: _lat(110, 100), 900: _lat(260, 250)}
    hourly = {1: _hr(110, 100), 2: _hr(110, 100), 900: _hr(260, 250)}
    rows = scan_combinations([combo], latest, hourly, mapping, cash=10_000, limit_used=None,
                             beta=0.0, staleness_max=21_600, members=True)
    assert len(rows) == 1
    r = rows[0]
    # β=0 → buy at avg_low, sell at avg_high: assemble cost 200, proceeds post_tax(260)=255 → +55
    assert r["direction"] == "ASSEMBLE"
    assert r["profit_per_conv"] == (260 - ge_tax(260)) - 200
    assert r["conversions"] > 0

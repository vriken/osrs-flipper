"""Journal P&L math: weighted-average cost, taxed sells, realised P&L, equity."""

import pytest

from osrs_flipper.journal import Journal

GOLD_BAR = 2357  # >50gp, taxed
FEATHER = 314  # placeholder cheap id for an exempt-by-price item (<50gp)


@pytest.fixture
def j(tmp_path):
    jr = Journal(path=str(tmp_path / "j.duckdb"))
    jr.set_cash(200_000)
    yield jr
    jr.con.close()


def test_buy_reduces_cash_and_sets_avg_cost(j):
    j.record_buy(GOLD_BAR, "Gold bar", 2000, 97)
    assert j.cash() == 200_000 - 2000 * 97
    pos = j.position(GOLD_BAR)
    assert pos.qty == 2000 and pos.avg_cost == 97


def test_weighted_average_cost(j):
    j.record_buy(GOLD_BAR, "Gold bar", 1000, 97)
    j.record_buy(GOLD_BAR, "Gold bar", 1000, 99)
    assert j.position(GOLD_BAR).avg_cost == 98  # (97+99)/2


def test_sell_applies_tax_and_realises_pnl(j):
    j.record_buy(GOLD_BAR, "Gold bar", 2000, 97)
    proceeds, realized = j.record_sell(GOLD_BAR, "Gold bar", 2000, 101)
    # tax(101)=2 -> net 99/unit; pnl = (99-97)*2000 = 4000
    assert proceeds == 2000 * 99
    assert realized == 4000
    assert j.realized_pnl() == 4000
    assert j.position(GOLD_BAR).qty == 0


def test_cannot_oversell(j):
    j.record_buy(GOLD_BAR, "Gold bar", 100, 97)
    proceeds, realized = j.record_sell(GOLD_BAR, "Gold bar", 999, 101)
    assert j.position(GOLD_BAR).qty == 0  # only the 100 held were sold


def test_equity_marks_inventory_at_bid(j):
    j.record_buy(GOLD_BAR, "Gold bar", 1000, 97)
    # cash now 200k - 97k = 103k; inventory marked at post-tax bid 100 -> 98 each
    eq = j.equity({GOLD_BAR: 100})
    assert eq == 103_000 + 1000 * 98


def test_import_offer_is_idempotent(j):
    assert j.import_offer("u1", GOLD_BAR, "Gold bar", True, 100, 97) is True
    assert j.import_offer("u1", GOLD_BAR, "Gold bar", True, 100, 97) is False  # same uuid → skipped
    assert j.position(GOLD_BAR).qty == 100  # recorded exactly once
    assert j.cash() == 200_000 - 100 * 97


def test_units_bought_since(j):
    j.record_buy(GOLD_BAR, "Gold bar", 100, 97)
    j.record_buy(GOLD_BAR, "Gold bar", 50, 98)
    assert j.units_bought_since(0)[GOLD_BAR] == 150  # sums buys in window
    assert j.units_bought_since(10**12) == {}  # far-future cutoff → nothing counts


def test_set_cash_persists(j):
    j.set_cash(204_000)
    assert j.cash() == 204_000


def test_predictions_logged_and_read_back(j):
    j.log_prediction(GOLD_BAR, "Gold bar", 2000, 97, 101, 0.9, 0.8, 0.72, 5760)
    j.log_prediction(GOLD_BAR, "Gold bar", 100, 97, 101, 0.9, 0.8, 0.72, 200, source="buy")
    preds = j.recent_predictions(5)
    assert len(preds) == 2
    # don't assume tie order (same-second ts) — find by source
    assert {p["source"] for p in preds} == {"buy", "quote"}
    buy = next(p for p in preds if p["source"] == "buy")
    quote = next(p for p in preds if p["source"] == "quote")
    assert buy["qty"] == 100 and quote["qty"] == 2000
    assert quote["buy_px"] == 97 and quote["sell_px"] == 101


def test_attempt_reconciles_with_a_matching_fill(j):
    aid = j.record_attempt(GOLD_BAR, "Gold bar", "BUY", 2000, 97, horizon_h=2.0,
                           avg_low=96, avg_high=101, vol_1h_binding=5000, pred_p_fill=0.8)
    # a later buy fill for the same item closes the open attempt
    matched = j.reconcile_fill(GOLD_BAR, is_buy=True, qty=2000, price=97, fill_ts=10**12)
    assert matched == aid
    row = j.calibration_rows()[0]
    assert row["status"] == "filled" and row["filled_qty"] == 2000 and row["fill_px"] == 97


def test_partial_fill_then_completion_vwaps_price(j):
    j.record_attempt(GOLD_BAR, "Gold bar", "BUY", 1000, 100, horizon_h=2.0,
                     avg_low=96, avg_high=104, vol_1h_binding=5000)
    j.reconcile_fill(GOLD_BAR, is_buy=True, qty=400, price=100, fill_ts=10**12)
    j.reconcile_fill(GOLD_BAR, is_buy=True, qty=600, price=105, fill_ts=10**12 + 1)
    row = j.calibration_rows()[0]
    assert row["status"] == "filled" and row["filled_qty"] == 1000
    assert row["fill_px"] == (400 * 100 + 600 * 105) / 1000  # VWAP = 103


def test_reconcile_ignores_fill_placed_before_the_attempt(j):
    j.record_attempt(GOLD_BAR, "Gold bar", "BUY", 100, 97, horizon_h=2.0,
                     avg_low=96, avg_high=101, vol_1h_binding=5000)
    # fill timestamped before the attempt was placed → not a match
    assert j.reconcile_fill(GOLD_BAR, is_buy=True, qty=100, price=97, fill_ts=1) is None


def test_stale_attempt_expires_and_enters_calibration_set(j):
    j.record_attempt(GOLD_BAR, "Gold bar", "BUY", 100, 97, horizon_h=1.0,
                     avg_low=96, avg_high=101, vol_1h_binding=5000, pred_p_fill=0.8)
    assert j.open_attempts()  # open until it ages out
    expired = j.expire_stale_attempts(10**12)  # far future → past its 1h horizon
    assert expired == 1
    assert not j.open_attempts()
    row = j.calibration_rows()[0]
    assert row["status"] == "expired" and row["filled_qty"] == 0  # a counted miss

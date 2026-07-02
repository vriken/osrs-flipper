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


def test_sell_without_cost_basis_books_zero_realised_not_full_proceeds(j):
    # selling stock the journal never saw bought (a pre-plugin holding) must NOT count the whole
    # sale as profit — that inflated realised P&L. Cash still receives the proceeds.
    proceeds, realized = j.record_sell(GOLD_BAR, "Gold bar", 100, 101)
    assert realized == 0                       # cost unknown → no phantom profit
    assert proceeds == 100 * 99                # full proceeds still credited
    assert j.cash() == 200_000 + 100 * 99
    assert j.realized_pnl() == 0


def test_repair_zeroes_only_phantom_realised_and_is_idempotent(tmp_path):
    jr = Journal(path=str(tmp_path / "r.duckdb"))
    jr.con.execute("DELETE FROM meta WHERE key='realized_repair_v1'")  # let the one-time repair run
    # a zero-cost (phantom) sell: realised == proceeds; and a properly-costed sell that must survive
    jr.con.execute("INSERT INTO ledger VALUES (1,111,'Phantom','SELL',100,500,1000,49000,49000)")
    jr.con.execute("INSERT INTO ledger VALUES (2,222,'Costed','SELL',100,500,1000,49000,4000)")
    removed = jr._repair_phantom_realized()
    assert removed == 49000
    got = dict(jr.con.execute("SELECT name, realized_pnl FROM ledger").fetchall())
    assert got["Phantom"] == 0 and got["Costed"] == 4000  # only the phantom is zeroed
    assert jr._repair_phantom_realized() == 0             # idempotent — flag set, no re-run
    jr.con.close()


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


def test_record_sell_records_full_qty_no_silent_cap(j):
    j.record_buy(GOLD_BAR, "Gold bar", 100, 50)            # hold 100
    j.record_sell(GOLD_BAR, "Gold bar", 250, 60)           # sell 250 (matching buy imported later)
    sold = j.con.execute("SELECT qty FROM ledger WHERE side='SELL' AND item_id=?", [GOLD_BAR]).fetchone()
    assert sold[0] == 250                                  # full sale recorded, not capped to 100
    assert all(p.item_id != GOLD_BAR for p in j.positions())  # position floored at 0, not negative


def test_reconcile_positions_clears_phantom(j):
    from osrs_flipper.runelite import Fill
    j.con.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?)", [9143, "Adamant bolts", 1126, 142.0])
    fills = [  # RuneLite's authoritative history balances → net 0 held
        Fill(uuid="a", item_id=9143, name="Adamant bolts", is_buy=True, qty=2864, price=141, state="BOUGHT", t_ms=0),
        Fill(uuid="b", item_id=9143, name="Adamant bolts", is_buy=False, qty=2864, price=148, state="SOLD", t_ms=0),
    ]
    drift = j.reconcile_positions(fills)
    assert ("Adamant bolts", 1126, 0) in drift
    assert all(p.item_id != 9143 for p in j.positions())   # phantom cleared


def test_reconcile_positions_sets_correct_remaining(j):
    from osrs_flipper.runelite import Fill
    j.reconcile_positions([
        Fill(uuid="a", item_id=1, name="X", is_buy=True, qty=1000, price=100, state="BOUGHT", t_ms=0),
        Fill(uuid="b", item_id=1, name="X", is_buy=False, qty=600, price=110, state="SOLD", t_ms=0),
    ])
    p = j.position(1)
    assert p.qty == 400 and p.avg_cost == 100              # 1000 bought − 600 sold; avg from buys


def test_reconcile_to_holdings_drops_phantom_and_trims(j):
    j.con.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?)", [1, "Lava rune", 5000, 30.0])
    j.con.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?)", [2, "Unicorn horn dust", 528, 498.0])
    j.con.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?)", [3, "Real", 100, 10.0])
    drift = j.reconcile_to_holdings({2: 327, 3: 100})  # 1 absent → drop ; 2 → trim 327 ; 3 matches
    assert ("Lava rune", 5000, 0) in drift and ("Unicorn horn dust", 528, 327) in drift
    assert all(d[0] != "Real" for d in drift)
    assert j.position(1) is None and j.position(2).qty == 327 and j.position(3).qty == 100


def test_sync_positions_to_bag_sets_qty_and_cost_and_clears_junk(j):
    from osrs_flipper.runelite import Fill
    # an erroneous old manual SELL + a leftover phantom; the bag is the truth
    j.con.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?)", [9, "Phantom", 5000, 10.0])
    j.record_manual_fill(2114, "Pineapple", is_buy=False, qty=1500)  # bad drop
    fills = [Fill(uuid="b", item_id=2114, name="Pineapple", is_buy=True, qty=1630, price=202, state="BOUGHT", t_ms=0)]
    changes = j.sync_positions_to_bag({2114: 1500, 229: 1}, fills)   # bag holds Pineapple 1500 + a junk Vial
    p = j.position(2114)
    assert p.qty == 1500 and round(p.avg_cost) == 202        # qty from bag, cost from buy history
    assert j.position(9) is None                              # phantom not in bag → dropped
    assert j.position(229) is None                            # in bag but never bought here → skipped as junk
    assert j.con.execute("SELECT COUNT(*) FROM manual_fills").fetchone()[0] == 0  # corruption cleared
    assert ("Pineapple", 0, 1500) in changes


def test_reconcile_to_holdings_is_reduce_only(j):
    # bag shows MORE than the journal tracks (e.g. bought off-device) → never inflate, unknown cost
    j.con.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?)", [1, "X", 50, 10.0])
    assert j.reconcile_to_holdings({1: 500}) == []
    assert j.position(1).qty == 50


def test_reconcile_skips_items_with_no_buy_in_history(j):
    # a position whose buy predates RuneLite's window (only a sell shows up) must NOT be cleared
    from osrs_flipper.runelite import Fill
    j.con.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?)", [1, "Held", 500, 100.0])
    drift = j.reconcile_positions(
        [Fill(uuid="s", item_id=1, name="Held", is_buy=False, qty=200, price=110, state="SOLD", t_ms=0)])
    assert drift == []                      # no buy in history → left alone
    assert j.position(1).qty == 500


def test_forget_is_reconcile_proof(j):
    # bought on this device (RuneLite has the buy), sold on another device (RuneLite lacks the sale)
    from osrs_flipper.runelite import Fill
    j.con.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?)", [5, "Black knife", 814, 200.0])
    j.forget_position(5, "Black knife", 814)
    assert all(p.item_id != 5 for p in j.positions())          # gone immediately
    j.reconcile_positions(  # RuneLite still shows the buy, but the forget offsets it
        [Fill(uuid="b", item_id=5, name="Black knife", is_buy=True, qty=814, price=200, state="BOUGHT", t_ms=0)])
    assert all(p.item_id != 5 for p in j.positions())          # NOT re-added by reconcile


def test_hold_position_adds_without_cash_and_survives_reconcile(j):
    cash0 = j.cash()
    j.hold_position(7, "Mud rune", 5000, 98.0)            # acquired on another device
    p = j.position(7)
    assert p.qty == 5000 and p.avg_cost == 98.0
    assert j.cash() == cash0                               # cash untouched
    j.reconcile_positions([])                              # no RuneLite buys, but the manual buy keeps it
    assert j.position(7).qty == 5000                       # not wiped by reconcile


def test_account_fill_delta_credits_partial_sells_incrementally(j):
    from osrs_flipper.tax import post_tax_received
    j.record_buy(GOLD_BAR, "Gold bar", 1000, 100)        # hold 1000
    cash1 = j.cash()
    assert j.account_fill_delta("u1", GOLD_BAR, "Gold bar", False, 400, 110) == 400  # 400 sold so far
    assert j.account_fill_delta("u1", GOLD_BAR, "Gold bar", False, 700, 110) == 300  # +300 new
    assert j.account_fill_delta("u1", GOLD_BAR, "Gold bar", False, 700, 110) == 0    # re-seen → nothing
    assert j.cash() == cash1 + 700 * post_tax_received(110, item_id=GOLD_BAR)        # 700 proceeds credited
    assert j.position(GOLD_BAR).qty == 300               # 1000 − 700 sold


def test_account_fill_delta_skips_legacy_imported(j):
    j.record_buy(GOLD_BAR, "Gold bar", 500, 100)
    j.con.execute("INSERT INTO imported_offers VALUES ('legacy')")
    cash0 = j.cash()
    assert j.account_fill_delta("legacy", GOLD_BAR, "Gold bar", False, 500, 110) == 0  # already accounted
    assert j.cash() == cash0


def test_migrate_baselines_then_credits_only_new(j):
    from osrs_flipper.runelite import Fill
    j.record_buy(GOLD_BAR, "Gold bar", 1000, 100)
    cash0 = j.cash()
    assert j.migrate_fill_accounting_if_needed(
        [Fill(uuid="u", item_id=GOLD_BAR, name="Gold bar", is_buy=False, qty=400, price=110, state="SELLING", t_ms=0)])
    assert j.account_fill_delta("u", GOLD_BAR, "Gold bar", False, 400, 110) == 0   # existing 400 NOT re-credited
    assert j.cash() == cash0
    assert j.account_fill_delta("u", GOLD_BAR, "Gold bar", False, 600, 110) == 200  # only new units credit
    assert j.migrate_fill_accounting_if_needed([]) is False                         # one-time


# --- durable offer age across a restart ---------------------------------------
_HOUR_MS = 3_600_000


def _offer(started_ms: int, observed: bool, *, slot: int = 0, item: int = GOLD_BAR,
           is_buy: bool = True, qty: int = 100, price: int = 4, filled: int = 0):
    from osrs_flipper.runelite import Offer
    return Offer(slot=slot, item_id=item, is_buy=is_buy, state="BUYING", qty=qty, price=price,
                 started_ms=started_ms, filled=filled, placement_observed=observed)


def test_freshly_witnessed_unfilled_placement_is_trusted(j):
    now = 100 * _HOUR_MS
    o = _offer(started_ms=now - 10 * _HOUR_MS, observed=True)     # caught at 0 fill → real age known
    j.remember_offer_ages([o], now)
    assert o.started_ms == now - 10 * _HOUR_MS and o.placement_observed is True


def test_age_survives_restart_that_resets_placement(j):
    """The real bug: after a client restart the plugin re-emits the SAME order (same terms) with a
    fresh placedAt — and even re-asserts placementObserved=true. The remembered original age must
    win, and the reload must not upgrade our confidence."""
    now = 100 * _HOUR_MS
    # session 1: caught the placement 10h ago at 0 fill
    j.remember_offer_ages([_offer(started_ms=now - 10 * _HOUR_MS, observed=True)], now)
    # session 2 (after restart): identical terms, but plugin reset placedAt to ~now
    later = now + _HOUR_MS
    o2 = _offer(started_ms=later, observed=False)
    j.remember_offer_ages([o2], later)
    assert o2.started_ms == now - 10 * _HOUR_MS   # original placement preserved, not reset to `later`
    assert o2.placement_observed is True          # locked from session 1


def test_fresh_order_that_partfilled_before_first_sight_is_observed(j):
    """Regression: a just-placed BUY can start filling before the first `go`. Its placement was still
    witnessed, so it must read as a known age — not "true age unconfirmed" — despite filled>0."""
    now = 100 * _HOUR_MS
    o = _offer(started_ms=now, observed=True, is_buy=True, qty=4000, price=273, filled=661)
    j.remember_offer_ages([o], now)
    assert o.placement_observed is True and o.started_ms == now


def test_reload_preserves_age_even_if_it_restamps_placedat(j):
    """The real guarantee: whatever the plugin claims after a reload, the earliest placement we ever
    recorded wins, so age keeps growing and stale/slow keep firing."""
    t0 = 40 * _HOUR_MS
    j.remember_offer_ages([_offer(started_ms=t0, observed=False, filled=30)], t0)  # first sight, mid-fill
    later = t0 + 3 * _HOUR_MS
    o2 = _offer(started_ms=later, observed=True, filled=30)                         # reload re-stamps placedAt
    j.remember_offer_ages([o2], later)
    assert o2.started_ms == t0                     # earliest kept — age is 3h, not reset to 0


def test_never_witnessed_keeps_earliest_first_seen_as_lower_bound(j):
    """An offer open before we ever tracked it stays unobserved, but its first-seen time is
    remembered so age GROWS across restarts (a ≥ lower bound) instead of resetting each time."""
    t0 = 50 * _HOUR_MS
    j.remember_offer_ages([_offer(started_ms=0, observed=False)], t0)     # first ever sight, no stamp
    o2 = _offer(started_ms=0, observed=False)
    j.remember_offer_ages([o2], t0 + 5 * _HOUR_MS)                        # a later restart
    assert o2.started_ms == t0 and o2.placement_observed is False         # earliest kept, still unconfirmed


def test_new_listing_in_slot_resets_age(j):
    """A genuinely new order (different terms) reusing the slot must not inherit the old age."""
    now = 100 * _HOUR_MS
    j.remember_offer_ages([_offer(started_ms=now - 20 * _HOUR_MS, observed=True, price=4)], now)
    later = now + _HOUR_MS
    fresh = _offer(started_ms=later, observed=True, price=7)              # re-listed at a new price
    j.remember_offer_ages([fresh], later)
    assert fresh.started_ms == later


def test_slot_turnover_detected_by_fill_reset(j):
    """Same terms but filled dropped back to 0 ⇒ the prior order completed and a new one took the
    slot; age must restart rather than inherit the finished order's clock."""
    now = 100 * _HOUR_MS
    j.remember_offer_ages([_offer(started_ms=now - 15 * _HOUR_MS, observed=True, filled=90)], now)
    later = now + _HOUR_MS
    new = _offer(started_ms=later, observed=True, filled=0)               # fresh order, same item/price/qty
    j.remember_offer_ages([new], later)
    assert new.started_ms == later

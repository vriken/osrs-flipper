"""The unified `go` screen's NEXT-action synthesis prioritises the right thing to do."""

import types

import pytest

from osrs_flipper import terminal as term_mod
from osrs_flipper.journal import Journal, Position
from osrs_flipper.runelite import Offer
from osrs_flipper.terminal import Terminal

na = Terminal._next_action

# A full Super strength (1)-(4) family + the empty vial — enough for decant_recipes to build inputs.
_SS_MAPPING = [
    {"id": 161, "name": "Super strength(1)"}, {"id": 159, "name": "Super strength(2)"},
    {"id": 157, "name": "Super strength(3)"}, {"id": 2440, "name": "Super strength(4)"},
    {"id": 229, "name": "Vial"},
]


def _mock_potion_api(monkeypatch, *, members=True, high4=3235):
    monkeypatch.setattr(term_mod.config, "MEMBERS", members)
    monkeypatch.setattr(term_mod.api, "mapping", lambda: _SS_MAPPING)
    monkeypatch.setattr(term_mod.api, "one_hour",
                        lambda: {2440: {"avgHighPrice": high4, "highPriceVolume": 5000}})
    monkeypatch.setattr(term_mod.api, "latest", lambda: {})


def test_split_sells_holds_back_bounce_items():
    # an underwater holding the recovery read says will bounce is held (not listed, no slot); a
    # re-rating (recover=False) and a non-underwater item stay in the list-now sells.
    rows = [{"item_id": 1, "name": "NotUnderwater"},
            {"item_id": 2, "name": "Bounces"},
            {"item_id": 3, "name": "ReRating"}]
    rec = {2: {"recover": True}, 3: {"recover": False}}  # item 1 not underwater → no recovery read
    to_list, holds = Terminal._split_sells(rows, rec)
    assert [r["item_id"] for r in to_list] == [1, 3]   # bounce (2) pulled out of the sell list
    assert [r["item_id"] for r in holds] == [2]


# --- decant-aware sell review: a held low dose exits via decant, not a flip -------------------------

def test_decant_input_ids_lists_low_doses_members_only(monkeypatch):
    _mock_potion_api(monkeypatch, members=True)
    ids = Terminal._decant_input_ids(None)
    assert {161, 159, 157} <= ids and 2440 not in ids   # the (1),(2),(3) are inputs; the (4) is the output
    _mock_potion_api(monkeypatch, members=False)
    assert Terminal._decant_input_ids(None) == set()     # no Bob Barter in F2P


def test_decant_exit_ready_recommends_decant_and_prices_the_4(monkeypatch):
    from osrs_flipper.tax import ge_tax
    _mock_potion_api(monkeypatch)
    held = [Position(item_id=157, name="Super strength(3)", qty=4, avg_cost=2204)]  # 4×(3) = 12 doses = 3×(4)
    rows = Terminal._decant_exits(None, held, busy_ids=set())
    assert len(rows) == 1
    d = rows[0]
    assert d["ready"] and d["out_qty"] == 3 and d["out_name"] == "Super strength(4)"
    assert d["net"] == 3 * (3235 - ge_tax(3235)) - 4 * 2204    # sell 3 (4)s post-tax, less the (3) basis
    assert d["leftover"] == 0 and not d["underwater"]


def test_decant_exit_partial_says_accumulate_not_flip(monkeypatch):
    _mock_potion_api(monkeypatch)
    held = [Position(item_id=157, name="Super strength(3)", qty=1, avg_cost=2204)]  # 3 doses — can't make a (4)
    rows = Terminal._decant_exits(None, held, busy_ids=set())
    assert rows[0]["ready"] is False and rows[0]["need"] == 2   # need ≥2 (3)s for one (4)


def test_decant_exit_skips_items_with_a_live_offer(monkeypatch):
    _mock_potion_api(monkeypatch)
    held = [Position(item_id=157, name="Super strength(3)", qty=4, avg_cost=2204)]
    assert Terminal._decant_exits(None, held, busy_ids={157}) == []   # still buying / already listed → skip


def test_decant_exit_none_in_f2p(monkeypatch):
    _mock_potion_api(monkeypatch, members=False)
    held = [Position(item_id=157, name="Super strength(3)", qty=4, avg_cost=2204)]
    assert Terminal._decant_exits(None, held, busy_ids=set()) == []


def test_next_action_surfaces_a_ready_decant(monkeypatch):
    ready = [{"ready": True, "name": "Super strength(3)"}]
    msg = na([], [], 0, [], ready)
    assert "decant" in msg.lower() and "Bob Barter" in msg


# --- auto-detect in-game decants: move basis before the bag-sync drops the low dose -----------------

def _term_stub(j, offers=()):
    return types.SimpleNamespace(j=j, _active_offers=lambda: list(offers))


def test_autodecant_moves_basis_when_low_dose_leaves_bag(tmp_path, monkeypatch):
    _mock_potion_api(monkeypatch)
    j = Journal(path=str(tmp_path / "ad.duckdb"))
    j.set_cash(1_000_000)
    j.record_buy(157, "Super strength(3)", 4, 2204)          # hold 4 (3)s @2,204
    # bag now: (3) gone, three (4)s present → decanted 4×(3) into 3×(4)
    Terminal._autodecant(_term_stub(j), {2440: 3}, bought_since={}, sold_since={})
    assert j.position(157) is None                            # (3) basis not lost as junk
    p4 = j.position(2440)
    assert p4.qty == 3 and p4.avg_cost == pytest.approx(4 * 2204 / 3)
    j.con.close()


def test_autodecant_ignores_a_ge_sale_of_the_low_dose(tmp_path, monkeypatch):
    # the (3) left the bag because you SOLD it on GE (not decanted) — conservation nets to 0, no transfer,
    # even though (4)s happen to be present. Prevents double-counting the sale as a decant.
    _mock_potion_api(monkeypatch)
    j = Journal(path=str(tmp_path / "ad2.duckdb"))
    j.set_cash(1_000_000)
    j.record_buy(157, "Super strength(3)", 4, 2204)
    Terminal._autodecant(_term_stub(j), {2440: 5}, bought_since={}, sold_since={157: 4})
    assert j.position(157).qty == 4 and j.position(2440) is None   # untouched
    j.con.close()


def test_autodecant_skips_when_no_4_dose_evidence(tmp_path, monkeypatch):
    # (3) vanished but no (4) in bag / offers / sells → don't attribute a mystery drop to a decant
    _mock_potion_api(monkeypatch)
    j = Journal(path=str(tmp_path / "ad3.duckdb"))
    j.set_cash(1_000_000)
    j.record_buy(157, "Super strength(3)", 4, 2204)
    Terminal._autodecant(_term_stub(j), {}, bought_since={}, sold_since={})
    assert j.position(157).qty == 4                            # left alone (basis preserved for now)
    j.con.close()


def test_autodecant_partial_decant_keeps_remainder(tmp_path, monkeypatch):
    _mock_potion_api(monkeypatch)
    j = Journal(path=str(tmp_path / "ad4.duckdb"))
    j.set_cash(1_000_000)
    j.record_buy(157, "Super strength(3)", 8, 2204)           # hold 8; decant 4, keep 4 in bag
    Terminal._autodecant(_term_stub(j), {157: 4, 2440: 3}, bought_since={}, sold_since={})
    assert j.position(157).qty == 4                            # remainder stays tracked
    assert j.position(2440).qty == 3
    j.con.close()


def test_autodecant_noop_in_f2p(tmp_path, monkeypatch):
    _mock_potion_api(monkeypatch, members=False)
    j = Journal(path=str(tmp_path / "ad5.duckdb"))
    j.set_cash(1_000_000)
    j.record_buy(157, "Super strength(3)", 4, 2204)
    Terminal._autodecant(_term_stub(j), {2440: 3}, bought_since={}, sold_since={})
    assert j.position(157).qty == 4 and j.position(2440) is None
    j.con.close()


def _stub(j, offers=()):
    return types.SimpleNamespace(
        j=j, _snapshot=lambda iid: {"avg_low": 100, "avg_high": 110, "vol_1h_binding": 5000},
        _active_offers=lambda: list(offers))


def test_autodetect_logs_pending_offer_once(tmp_path, monkeypatch):
    j = Journal(path=str(tmp_path / "j.duckdb"))
    monkeypatch.setattr(term_mod.api, "mapping", lambda: [{"id": 561, "name": "Air rune"}])
    stub = _stub(j, [Offer(slot=0, item_id=561, is_buy=True, state="BUYING", qty=1000, price=4)])
    assert Terminal._autodetect_placements(stub) == 1   # first sync logs it
    assert Terminal._autodetect_placements(stub) == 0   # idempotent — already an open attempt
    assert (561, "BUY") in {(a["item_id"], a["side"]) for a in j.open_attempts()}
    j.con.close()


def test_autodetect_skips_unknown_price(tmp_path, monkeypatch):
    # a fallback FU offer with price 0 (unfilled) — don't log a price-0 attempt (poisons β); the
    # exporter normally carries the real price, so this only guards the FU-only fallback path.
    j = Journal(path=str(tmp_path / "j3.duckdb"))
    monkeypatch.setattr(term_mod.api, "mapping", lambda: [{"id": 561, "name": "Air rune"}])
    stub = _stub(j, [Offer(slot=0, item_id=561, is_buy=True, state="BUYING", qty=1000, price=0)])
    assert Terminal._autodetect_placements(stub) == 0
    assert not j.open_attempts()
    j.con.close()


def test_autodetect_skips_completed_offers(tmp_path, monkeypatch):
    # a BOUGHT offer is a completed fill (imported via completed_offers) — re-logging it would
    # double-count, so detection only records still-pending BUYING/SELLING offers.
    j = Journal(path=str(tmp_path / "j2.duckdb"))
    monkeypatch.setattr(term_mod.api, "mapping", lambda: [{"id": 561, "name": "Air rune"}])
    stub = _stub(j, [Offer(slot=0, item_id=561, is_buy=True, state="BOUGHT", qty=1000, price=4)])
    assert Terminal._autodetect_placements(stub) == 0
    assert not j.open_attempts()
    j.con.close()


def test_refine_sell_priced_to_market_downgrades_to_ontrack(monkeypatch):
    # a stale SELL still at/under the 5m-avg market is priced to sell, just slow → don't re-list
    monkeypatch.setattr(term_mod.api, "five_min", lambda: {561: {"avgHighPrice": 6}})
    monkeypatch.setattr(term_mod.api, "latest", lambda: {})  # no tick → ref is the 5m avg
    o = Offer(slot=0, item_id=561, is_buy=False, state="SELLING", qty=100, price=6)
    v, hint = Terminal._refine_verdict(o, "stale")
    assert v == "ontrack" and "just slow" in hint


def test_refine_sell_within_deadband_is_not_flagged(monkeypatch):
    # 1gp above the 5m-avg on a 100gp item (1% < 2% deadband) is tick noise → hold, don't chase
    monkeypatch.setattr(term_mod.api, "five_min", lambda: {561: {"avgHighPrice": 100}})
    monkeypatch.setattr(term_mod.api, "latest", lambda: {})
    o = Offer(slot=0, item_id=561, is_buy=False, state="SELLING", qty=100, price=101)
    v, hint = Terminal._refine_verdict(o, "stale")
    assert v == "ontrack" and "just slow" in hint


def test_refine_sell_above_market_says_relist_lower(monkeypatch):
    # 50% above the 5m-avg is a genuine mispricing (past the deadband) → re-list
    monkeypatch.setattr(term_mod.api, "five_min", lambda: {561: {"avgHighPrice": 6}})
    monkeypatch.setattr(term_mod.api, "latest", lambda: {})
    o = Offer(slot=0, item_id=561, is_buy=False, state="SELLING", qty=100, price=9)
    v, hint = Terminal._refine_verdict(o, "stale")
    assert v == "stale" and "re-list" in hint


def _uw_sell(monkeypatch, ask=50):
    # a SELL listed well above a market that has collapsed to `ask` (5m avg), so it's flagged AND
    # re-listing at market would be below a high cost basis → the below-break-even path.
    monkeypatch.setattr(term_mod.api, "five_min", lambda: {561: {"avgHighPrice": ask}})
    monkeypatch.setattr(term_mod.api, "latest", lambda: {})
    return Offer(slot=0, item_id=561, is_buy=False, state="SELLING", qty=100, price=200)


def test_refine_sell_below_cost_holds_at_breakeven_by_default(monkeypatch):
    # market below your break-even, no cut signals → hold at break-even, never chase down into a loss
    o = _uw_sell(monkeypatch)
    v, hint = Terminal._refine_verdict(o, "stale", avg_cost=100)
    assert v == "ontrack" and "hold at break-even" in hint and "re-list nearer" not in hint


def test_refine_sell_below_cost_cuts_when_no_bounce_and_better_flip(monkeypatch):
    # the only case a below-cost sell is advised: no near-term bounce AND a better flip is ready
    o = _uw_sell(monkeypatch)
    v, hint = Terminal._refine_verdict(o, "stale", avg_cost=100, bounce_likely=False, better_flip=True)
    assert "cut & redeploy" in hint


def test_refine_sell_below_cost_holds_when_bounce_likely(monkeypatch):
    o = _uw_sell(monkeypatch)
    v, hint = Terminal._refine_verdict(o, "stale", avg_cost=100, bounce_likely=True, better_flip=True)
    assert v == "ontrack" and "hold at break-even" in hint


def test_refine_sell_below_cost_holds_when_no_better_flip(monkeypatch):
    o = _uw_sell(monkeypatch)
    v, hint = Terminal._refine_verdict(o, "stale", avg_cost=100, bounce_likely=False, better_flip=False)
    assert v == "ontrack" and "hold at break-even" in hint


def test_refine_sell_above_cost_still_relists_normally(monkeypatch):
    # market above your (low) break-even → re-listing there is no loss, so the normal advice stands
    o = _uw_sell(monkeypatch)
    v, hint = Terminal._refine_verdict(o, "stale", avg_cost=5, bounce_likely=False, better_flip=True)
    assert v == "stale" and "re-list nearer" in hint


def test_refine_sell_reacts_to_a_sharp_drop_via_the_tick_blend(monkeypatch):
    # 5m avg still 100 but the last tick has crashed to 60 (40% > 8% big-move) → the blend trusts
    # the tick, so a sell listed at 100 is now well above market → re-list (not smoothed away).
    monkeypatch.setattr(term_mod.api, "five_min", lambda: {561: {"avgHighPrice": 100}})
    monkeypatch.setattr(term_mod.api, "latest", lambda: {561: {"high": 60}})
    o = Offer(slot=0, item_id=561, is_buy=False, state="SELLING", qty=100, price=100)
    v, hint = Terminal._refine_verdict(o, "stale")
    assert v == "stale" and "re-list" in hint


def test_refine_buy_same_price_downgrades_to_ontrack(monkeypatch):
    from osrs_flipper import quote as quote_mod
    monkeypatch.setattr(quote_mod, "optimal_quote",
                        lambda *a, **k: types.SimpleNamespace(buy_px=4, sell_px=6, net_unit=1))
    o = Offer(slot=0, item_id=561, is_buy=True, state="BUYING", qty=100, price=4)
    v, hint = Terminal._refine_verdict(o, "margin")
    assert v == "ontrack" and "just slow" in hint


def test_refine_buy_above_quote_but_profitable_downgrades(monkeypatch):
    # the Redwood case: bid a hair above the quote's buy, still profitable → not mispriced
    from osrs_flipper import quote as quote_mod
    monkeypatch.setattr(quote_mod, "optimal_quote",
                        lambda *a, **k: types.SimpleNamespace(buy_px=4215, sell_px=4447, net_unit=144))
    o = Offer(slot=0, item_id=1234, is_buy=True, state="BUYING", qty=10, price=4216)
    v, hint = Terminal._refine_verdict(o, "margin")
    assert v == "ontrack" and "just slow" in hint


def test_refine_buy_underbid_keeps_flag(monkeypatch):
    # known price, but bidding below the competitive buy floor → genuinely needs to re-quote up
    from osrs_flipper import quote as quote_mod
    monkeypatch.setattr(quote_mod, "optimal_quote",
                        lambda *a, **k: types.SimpleNamespace(buy_px=5, sell_px=7, net_unit=2))
    o = Offer(slot=0, item_id=561, is_buy=True, state="BUYING", qty=100, price=4)
    v, hint = Terminal._refine_verdict(o, "margin")
    assert v == "margin" and "re-quote" in hint


def test_refine_buy_unknown_price_shows_market_not_mispriced(monkeypatch):
    # 0%-filled offer (price 0): we can't judge the bid, so don't keep "margin"/"mispriced" —
    # downgrade to slow and show the live market to compare against.
    from osrs_flipper import quote as quote_mod
    monkeypatch.setattr(quote_mod, "optimal_quote",
                        lambda *a, **k: types.SimpleNamespace(buy_px=4215, sell_px=4447, net_unit=144))
    o = Offer(slot=0, item_id=19672, is_buy=True, state="BUYING", qty=31, price=0)
    v, hint = Terminal._refine_verdict(o, "margin")
    assert v == "slow" and "market now" in hint and "4,215" in hint


def test_refine_sell_unknown_price_shows_market_ask(monkeypatch):
    monkeypatch.setattr(term_mod.api, "five_min", lambda: {})
    monkeypatch.setattr(term_mod.api, "latest", lambda: {554: {"high": 6}})  # only a tick → ref = 6
    o = Offer(slot=0, item_id=554, is_buy=False, state="SELLING", qty=50000, price=0)
    v, hint = Terminal._refine_verdict(o, "stale")
    assert v == "slow" and "market ask ~6" in hint


def _row(verdict, eta_h=1.0):
    return (None, verdict, 0.5, eta_h, 0.0)  # (offer, verdict, elapsed, eta, progress)


def test_collect_takes_priority():
    rows = [_row("collect"), _row("ontrack")]
    assert "collect" in na(rows, sell_rows=[], free=0, picks=[])


def test_margin_or_stale_prompts_reprice():
    assert "re-price" in na([_row("margin")], [], 0, [])
    assert "re-price" in na([_row("stale")], [], 0, [])


def test_free_slots_with_picks_says_place():
    picks = [{"place_at_h": 0}, {"place_at_h": 0}, {"place_at_h": 4.0}]
    msg = na([], sell_rows=[], free=3, picks=picks)
    assert "place buy #1–#2" in msg  # the two placeable now


def test_holdings_only_says_list_to_sell():
    assert "list your holdings" in na([], sell_rows=[{"name": "x"}], free=0, picks=[])


def test_all_slots_working_reports_wait_time():
    msg = na([_row("ontrack", eta_h=2.0)], sell_rows=[], free=0, picks=[])
    assert "check back" in msg and "120m" in msg


def test_free_slot_but_nothing_passed_is_not_all_slots_working():
    # a free slot with no pick must NOT claim "all slots working"
    msg = na([_row("ontrack")], sell_rows=[], free=1, picks=[])
    assert "nothing cleared" in msg and "all slots working" not in msg


def test_idle_when_nothing_to_do():
    assert "idle" in na([], sell_rows=[], free=0, picks=[])

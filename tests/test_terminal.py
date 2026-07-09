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
    # the (3) count dropped because you SOLD some on the GE — the fill loop already applied that sale to
    # BOTH the tracked position AND the bag before _autodecant runs, so tracked == bag and conservation
    # nets to 0: no decant is fabricated even with (4)s present. (Regression guard for the double-count
    # bug: the old formula re-subtracted sold_since on top of an already-reduced position.)
    _mock_potion_api(monkeypatch)
    j = Journal(path=str(tmp_path / "ad2.duckdb"))
    j.set_cash(1_000_000)
    j.record_buy(157, "Super strength(3)", 8, 2204)               # held 8
    j.record_sell(157, "Super strength(3)", 4, 2500)              # GE-sold 4 → tracked now 4 (fill loop)
    Terminal._autodecant(_term_stub(j), {157: 4, 2440: 5}, bought_since={}, sold_since={157: 4})
    assert j.position(157).qty == 4 and j.position(2440) is None  # tracked==bag → untouched, no decant
    j.con.close()


def test_autodecant_no_fabrication_on_a_plain_buy(tmp_path, monkeypatch):
    # the CRITICAL double-count regression: a plain GE buy of the low dose (already applied to the
    # position by the fill loop) with the (3)s still sitting in the bag must NOT be read as a decant just
    # because unrelated (4)s exist. tracked(4) - bag(4) == 0 → no transfer, basis intact.
    _mock_potion_api(monkeypatch)
    j = Journal(path=str(tmp_path / "ad6.duckdb"))
    j.set_cash(1_000_000)
    j.record_buy(157, "Super strength(3)", 4, 2204)              # bought 4 this sync (position now 4)
    Terminal._autodecant(_term_stub(j), {157: 4, 2440: 5}, bought_since={157: 4}, sold_since={})
    assert j.position(157).qty == 4                              # the 4 bought are still in the bag
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


# --- rebalance names the deploy plan's OWN pick (unified pool), not a flips-only alt ----------------

def _rebal_stub(cands, latest, monkeypatch):
    monkeypatch.setattr(term_mod.api, "one_hour", lambda: {100: {"lowPriceVolume": 1, "highPriceVolume": 1}})
    monkeypatch.setattr(term_mod.api, "mapping", lambda: [{"id": 100, "name": "Raw halibut"}])
    return types.SimpleNamespace(latest=lambda: latest,
                                 _deploy_candidates=lambda *a, **k: (cands, lambda c: True, {}))


def _stuck_buy():  # early-fill, old (started_ms truthy but ancient) → eligible for a swap
    return Offer(slot=7, item_id=100, is_buy=True, state="BUYING", qty=1000, price=100,
                 started_ms=1, filled=0)


def test_rebalance_names_the_unified_top_pick(monkeypatch):
    from osrs_flipper import planner
    # a strong gear candidate — the OLD flips-only rebalance could never have named it
    cand = planner.Candidate(kind="gear", key="Elder chaos top", slots=1, window_gp=60_000,
                             patient=True, item_ids=(200,))
    stub = _rebal_stub([cand], {100: {"high": 105}}, monkeypatch)
    out = Terminal._rebalance(stub, [_stuck_buy()], cash=400_000, held=[], net_worth=2_000_000,
                              daytime=True, hours=5.0)
    assert len(out) == 1
    assert "cancel Raw halibut" in out[0] and "Elder chaos top" in out[0] and "go` would deploy" in out[0]


def test_rebalance_stays_silent_when_nothing_clearly_beats_the_stuck_buy(monkeypatch):
    from osrs_flipper import planner
    weak = planner.Candidate(kind="flip", key="Meh flip", slots=1, window_gp=1.0, item_ids=(200,))
    stub = _rebal_stub([weak], {100: {"high": 105}}, monkeypatch)
    assert Terminal._rebalance(stub, [_stuck_buy()], cash=400_000, held=[], net_worth=2_000_000,
                               daytime=True, hours=5.0) == []


def test_rebalance_ignores_a_nearly_filled_buy(monkeypatch):
    from osrs_flipper import planner
    cand = planner.Candidate(kind="flip", key="Fast flip", slots=1, window_gp=60_000, item_ids=(200,))
    stub = _rebal_stub([cand], {100: {"high": 105}}, monkeypatch)
    o = Offer(slot=7, item_id=100, is_buy=True, state="BUYING", qty=1000, price=100, started_ms=1, filled=900)
    assert Terminal._rebalance(stub, [o], cash=400_000, held=[], net_worth=2_000_000,
                               daytime=True, hours=5.0) == []   # 90% filled → don't churn it


# --- cancel detection: vanished offer + a cancel in history → terminal-state it ---------------------

def _mk_attempt(j, item_id=100):
    return j.record_attempt(item_id, "Raw halibut", "BUY", 1000, 50, horizon_h=1.0, avg_low=48,
                            avg_high=52, vol_1h_binding=5000)


def test_detect_cancels_marks_a_vanished_cancelled_offer(tmp_path, monkeypatch):
    from osrs_flipper.runelite import Fill
    j = Journal(path=str(tmp_path / "c.duckdb"))
    aid = _mk_attempt(j)
    cancel = Fill(uuid="", item_id=100, name="Raw halibut", is_buy=True, qty=0, price=50,
                  state="CANCELLED_BUY", t_ms=0)
    monkeypatch.setattr(term_mod.datasource, "active",
                        lambda: types.SimpleNamespace(completed_offers=lambda: [cancel]))
    Terminal._detect_cancels(types.SimpleNamespace(j=j), offers=[])   # no live offer → vanished
    status, resolved = j.con.execute("SELECT status, resolved_ts FROM attempts WHERE attempt_id=?",
                                     [aid]).fetchone()
    assert status == "cancelled" and resolved is not None
    assert "cancelled" in [e["event"] for e in j.offer_timeline(aid)]
    j.con.close()


def test_detect_cancels_spares_a_still_live_offer(tmp_path, monkeypatch):
    from osrs_flipper.runelite import Fill
    j = Journal(path=str(tmp_path / "c2.duckdb"))
    aid = _mk_attempt(j)
    cancel = Fill(uuid="", item_id=100, name="Raw halibut", is_buy=True, qty=0, price=50,
                  state="CANCELLED_BUY", t_ms=0)
    monkeypatch.setattr(term_mod.datasource, "active",
                        lambda: types.SimpleNamespace(completed_offers=lambda: [cancel]))
    live = [Offer(slot=0, item_id=100, is_buy=True, state="BUYING", qty=1000, price=50)]
    Terminal._detect_cancels(types.SimpleNamespace(j=j), offers=live)  # still live → leave open
    assert j.con.execute("SELECT status FROM attempts WHERE attempt_id=?", [aid]).fetchone()[0] == "open"
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


# --- Discord auto-push: dashboard capture + change-gated status edit ---------------------------------

def test_render_go_captures_text_and_echo_controls_console(capsys):
    stub = types.SimpleNamespace(cmd_go=lambda a: print("DASHBOARD LINE"))
    silent = Terminal._render_go(stub, echo=False)
    assert silent.strip() == "DASHBOARD LINE"
    assert capsys.readouterr().out == ""                      # echo=False → nothing on the console
    echoed = Terminal._render_go(stub, echo=True)
    assert "DASHBOARD LINE" in echoed
    assert "DASHBOARD LINE" in capsys.readouterr().out         # echo=True → also printed locally


def _autopush_stub(dash_ref):
    return types.SimpleNamespace(_auto_push=True, _render_go=lambda echo: dash_ref[0],
                                 _push_transition_pings=lambda: None, _status_msg_id=None, _last_dash="")


def test_auto_tick_reposts_status_only_when_dashboard_changes(monkeypatch):
    monkeypatch.setattr(term_mod.alert, "bot_enabled", lambda: True)
    monkeypatch.setattr(term_mod.config, "DISCORD_BOT_TOKEN", "x")
    monkeypatch.setattr(term_mod.config, "DISCORD_CHANNEL_ID", "y")
    posted = []
    monkeypatch.setattr(term_mod.alert, "repost_status", lambda t, m: (posted.append(t), "mid")[1])
    dash = ["D1"]
    stub = _autopush_stub(dash)
    Terminal._auto_tick(stub)                                  # first render → repost at bottom
    assert posted == ["D1"] and stub._status_msg_id == "mid" and stub._last_dash == "D1"
    Terminal._auto_tick(stub)                                  # unchanged → no repost (no churn/spam)
    assert posted == ["D1"]
    dash[0] = "D2"
    Terminal._auto_tick(stub)                                  # changed → repost fresh (old one deleted)
    assert posted == ["D1", "D2"] and stub._last_dash == "D2"


def test_auto_tick_is_a_noop_when_off_or_no_channel(monkeypatch):
    monkeypatch.setattr(term_mod.config, "DISCORD_BOT_TOKEN", None)
    monkeypatch.setattr(term_mod.config, "DISCORD_CHANNEL_ID", None)
    monkeypatch.setattr(term_mod.config, "DISCORD_WEBHOOK_URL", None)
    monkeypatch.setattr(term_mod.alert, "bot_enabled", lambda: False)
    posted = []
    monkeypatch.setattr(term_mod.alert, "repost_status", lambda t, m: posted.append(t))
    off = _autopush_stub(["D"])
    off._auto_push = False
    Terminal._auto_tick(off)                                   # push disabled
    nochan = _autopush_stub(["D"])                             # enabled but no channel
    Terminal._auto_tick(nochan)
    assert posted == []


def test_compact_status_keeps_actions_drops_noise():
    from osrs_flipper.terminal import _compact_status
    dash = "\n".join([
        "  === 12:00 · cash 1,274,300 · 2/8 slots free · ☀️ day ===",
        "  holdings: 0 in bank · 3 listed in GE  (`inv` for detail)",
        "  ACTIVE OFFERS:",
        "    0  Granite boots    SELL  50%  0.4h  \033[32m🟢 on track\033[0m",
        "    2  White lily seed  BUY   0%   0.4h  \033[31m🟠 MARGIN GONE — cancel\033[0m",
        "\033[1m         → no spread to buy into now — cancel the buy\033[0m",   # ANSI-wrapped like the real tty
        "  BEST FOR YOUR 2 FREE SLOT(S) · ☀️ day — flips cycle  (using 2)",
        "    # type   trade   ~gp  slots  detail",
        "    1 ⚡flip  Adamant dart tip  145,771  1  buy 188 → sell 195 × 3,389",
        "    * best-case (β=0, fill AT the bid/ask); EV haircut ×0.55",
        "    (flips auto-calibrated from 18 attempts: β 0.53)",
        "  NEXT: place buy #1-#2 now",
    ])
    out = _compact_status(dash)
    assert "1,274,300" in out and "2/8 free" in out                   # terse header (no "cash ", no "===")
    assert "⚠ needs you:" in out and "White lily seed (margin gone)" in out   # one compact attention line
    assert "Adamant dart tip" in out and "NEXT: place buy" in out     # pick + next kept
    for noise in ("on track", "Granite boots", "holdings:", "auto-calibrated", "→ no spread",
                  "# type", "best-case", "MARGIN GONE — cancel"):     # verbose verdict tail gone too
        assert noise not in out
    assert len(out.splitlines()) <= 4                                 # header + needs-you + pick + NEXT


# --- recommendation-ledger glue (previously untested: Terminal was never instantiated for these) -----

def _rec_stub(j):
    return types.SimpleNamespace(j=j, _evaluate_pulls=lambda hr: None)


def test_log_recommendations_stores_all_set_legs(tmp_path):
    from osrs_flipper.planner import Candidate
    j = Journal(path=str(tmp_path / "rl.duckdb"))
    c = Candidate(kind="set", key="Ahrim's · ASSEMBLE", slots=2, window_gp=5000, patient=True,
                  item_ids=(10, 11), payload={"cost": 100, "proceeds": 120, "conv": 5, "gp": 5000})
    Terminal._log_recommendations(_rec_stub(j), [c], net_worth=1_000_000, free_slots=3,
                                  daytime=True, lat={}, hr={})
    assert j.con.execute("SELECT leg_ids FROM recommendations WHERE kind='set'").fetchone()[0] == "10,11"
    assert j.mark_rec_acted(11, "BUY", 1, "aid") is True     # placing the 2nd leg links the set rec
    j.con.close()


def test_log_recommendations_never_calls_a_set_pull_margin_gone(tmp_path):
    # a set's stored item_id is only the FIRST leg you buy; its own spread must not decide margin_gone.
    from osrs_flipper.planner import Candidate
    j = Journal(path=str(tmp_path / "rl2.duckdb"))
    stub = _rec_stub(j)
    c = Candidate(kind="set", key="S", slots=2, window_gp=5000, patient=True, item_ids=(10, 11),
                  payload={"cost": 100, "proceeds": 120, "conv": 5, "gp": 5000})
    Terminal._log_recommendations(stub, [c], net_worth=1_000_000, free_slots=3, daytime=True, lat={}, hr={})
    j.con.execute("UPDATE recommendations SET last_ts=last_ts-100000")   # age past the pull grace
    # leg 10's naive spread is deeply negative — a flip would read margin_gone; a set must read outranked
    Terminal._log_recommendations(stub, [], net_worth=1_000_000, free_slots=3, daytime=True,
                                  lat={}, hr={10: {"avgHighPrice": 50, "avgLowPrice": 100}})
    assert j.con.execute("SELECT pull_reason FROM recommendations").fetchone()[0] == "outranked"
    j.con.close()


def test_evaluate_pulls_marks_set_decant_unrated(tmp_path):
    from osrs_flipper.planner import Candidate
    j = Journal(path=str(tmp_path / "rl3.duckdb"))
    stub = _rec_stub(j)
    c = Candidate(kind="decant", key="Prayer", slots=1, window_gp=3000, patient=True, item_ids=(139,),
                  payload={"in_qty": 1, "in_px": 100, "out_qty": 1, "out_px": 200, "in_dose": 3,
                           "out_dose": 4, "conv": 10, "gp": 3000})
    Terminal._log_recommendations(stub, [c], net_worth=1_000_000, free_slots=3, daytime=True, lat={}, hr={})
    j.con.execute("UPDATE recommendations SET pulled_ts=1, acted=FALSE")   # force a matured pull
    Terminal._evaluate_pulls(stub, hr={139: {"avgHighPrice": 300, "avgLowPrice": 90}})
    assert j.con.execute("SELECT eval FROM recommendations").fetchone()[0] == "unrated"
    j.con.close()


def test_transition_ping_carries_target_and_drops_false_alarms(monkeypatch):
    # a flagged offer is re-checked against fresh prices: a real mispricing pings WITH the concrete
    # re-quote target; a priced-right-but-slow offer downgrades to on-track and is NOT pinged.
    o = Offer(slot=6, item_id=20997, is_buy=True, state="BUYING", qty=100, price=190, started_ms=1)
    monkeypatch.setattr(term_mod.api, "mapping", lambda: [{"id": 20997, "name": "Twisted bow"}])
    monkeypatch.setattr(term_mod.api, "latest", lambda: {})
    monkeypatch.setattr(term_mod.api, "one_hour", lambda: {})
    monkeypatch.setattr(term_mod.monitor, "review_offers", lambda *a, **k: [(o, "stale", 5.0, 0.1, 0.0)])
    sent = []
    monkeypatch.setattr(term_mod.alert, "notify", lambda c: sent.append(c) or True)

    def stub(refine):
        return types.SimpleNamespace(
            _active_offers=lambda: [o], j=types.SimpleNamespace(cash=lambda: 1_000_000),
            _seen_offers={(6, 20997, "BUY")}, _alerted={},
            _sell_cut_context=lambda off, cash: {}, _refine_verdict=refine)

    s1 = stub(lambda off, v, **k: ("stale", "         → re-quote: buy 900 / sell 950  (net 40/ea)"))
    term_mod.Terminal._push_transition_pings(s1)
    assert sent and "slot 6" in sent[0] and "re-quote: buy 900" in sent[0]   # actionable target in the ping

    sent.clear()
    s2 = stub(lambda off, v, **k: ("ontrack", ""))
    term_mod.Terminal._push_transition_pings(s2)
    assert sent == []                                                        # slow-but-fine → no false ping


def test_print_plan_labels_multi_window_overnight(capsys):
    from osrs_flipper.planner import Candidate
    c = Candidate(kind="flip", key="Maple longbow (u)", slots=1, window_gp=93936, patient=False,
                  item_ids=(62,), payload={"buy": 78, "sell": 87, "qty": 11742, "windows": 2})
    Terminal._print_plan([c], buy_slots=1, daytime=False, hours=8.0, fcal={})
    out = capsys.readouterr().out
    assert "× 11,742" in out and "2× buy-limit windows" in out    # overnight >1-window qty is labelled
    c1 = Candidate(kind="flip", key="Yew logs", slots=1, window_gp=100, patient=False,
                   item_ids=(1,), payload={"buy": 10, "sell": 12, "qty": 5000, "windows": 1})
    Terminal._print_plan([c1], buy_slots=1, daytime=True, hours=4.0, fcal={})
    assert "buy-limit windows" not in capsys.readouterr().out     # single-window flip: no label


def test_refine_verdict_offers_to_bank_a_partial_buy(monkeypatch):
    # a partially-filled slow buy: offer to bank what's filled now (same margin, frees the slot) instead
    # of waiting for the rest. A fully-unfilled buy has nothing to bank.
    from osrs_flipper import quote as quote_mod
    Q = types.SimpleNamespace(buy_px=100, sell_px=130, net_unit=27, t_buy_h=1.0, t_sell_h=0.5,
                              ev=100.0, p_round=0.8, p_buy=0.8, qty=100, name="Nature rune")
    monkeypatch.setattr(quote_mod, "optimal_quote", lambda *a, **k: Q)
    o = Offer(slot=2, item_id=561, is_buy=True, state="BUYING", qty=100, price=100, filled=60)
    _v, hint = Terminal._refine_verdict(o, "stale")
    assert "bank the 60" in hint and "sell @ 130" in hint
    o0 = Offer(slot=3, item_id=561, is_buy=True, state="BUYING", qty=100, price=100, filled=0)
    _v0, hint0 = Terminal._refine_verdict(o0, "stale")
    assert "bank the" not in hint0

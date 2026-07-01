"""The unified `go` screen's NEXT-action synthesis prioritises the right thing to do."""

import types

from osrs_flipper import terminal as term_mod
from osrs_flipper.journal import Journal
from osrs_flipper.runelite import Offer
from osrs_flipper.terminal import Terminal

na = Terminal._next_action


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
    # a stale SELL still at/under market is priced to sell, just slow → don't tell me to re-list
    monkeypatch.setattr(term_mod.api, "latest", lambda: {561: {"high": 6}})
    o = Offer(slot=0, item_id=561, is_buy=False, state="SELLING", qty=100, price=6)
    v, hint = Terminal._refine_verdict(o, "stale")
    assert v == "ontrack" and "just slow" in hint


def test_refine_sell_above_market_says_relist_lower(monkeypatch):
    monkeypatch.setattr(term_mod.api, "latest", lambda: {561: {"high": 6}})
    o = Offer(slot=0, item_id=561, is_buy=False, state="SELLING", qty=100, price=9)
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
    monkeypatch.setattr(term_mod.api, "latest", lambda: {554: {"high": 6}})
    o = Offer(slot=0, item_id=554, is_buy=False, state="SELLING", qty=50000, price=0)
    v, hint = Terminal._refine_verdict(o, "stale")
    assert v == "slow" and "market ask 6" in hint


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

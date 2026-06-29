"""The unified `go` screen's NEXT-action synthesis prioritises the right thing to do."""

import types

from osrs_flipper import terminal as term_mod
from osrs_flipper.journal import Journal
from osrs_flipper.runelite import Offer
from osrs_flipper.terminal import Terminal

na = Terminal._next_action


def _stub(j):
    return types.SimpleNamespace(
        j=j, _snapshot=lambda iid: {"avg_low": 100, "avg_high": 110, "vol_1h_binding": 5000})


def test_autodetect_logs_pending_offer_once(tmp_path, monkeypatch):
    j = Journal(path=str(tmp_path / "j.duckdb"))
    monkeypatch.setattr(term_mod.api, "mapping", lambda: [{"id": 561, "name": "Air rune"}])
    monkeypatch.setattr(term_mod.runelite, "active_offers",
                        lambda rl: [Offer(slot=0, item_id=561, is_buy=True, state="BUYING", qty=1000, price=4)])
    stub = _stub(j)
    assert Terminal._autodetect_placements(stub, {}) == 1   # first sync logs it
    assert Terminal._autodetect_placements(stub, {}) == 0   # idempotent — already an open attempt
    assert (561, "BUY") in {(a["item_id"], a["side"]) for a in j.open_attempts()}
    j.con.close()


def test_autodetect_skips_completed_offers(tmp_path, monkeypatch):
    # a BOUGHT offer is a completed fill (imported via completed_offers) — re-logging it would
    # double-count, so detection only records still-pending BUYING/SELLING offers.
    j = Journal(path=str(tmp_path / "j2.duckdb"))
    monkeypatch.setattr(term_mod.api, "mapping", lambda: [{"id": 561, "name": "Air rune"}])
    monkeypatch.setattr(term_mod.runelite, "active_offers",
                        lambda rl: [Offer(slot=0, item_id=561, is_buy=True, state="BOUGHT", qty=1000, price=4)])
    assert Terminal._autodetect_placements(_stub(j), {}) == 0
    assert not j.open_attempts()
    j.con.close()


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


def test_idle_when_nothing_to_do():
    assert "idle" in na([], sell_rows=[], free=0, picks=[])

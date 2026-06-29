"""The unified `go` screen's NEXT-action synthesis prioritises the right thing to do."""

from osrs_flipper.terminal import Terminal

na = Terminal._next_action


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

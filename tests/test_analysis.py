"""Realized-P&L matching: sells net against buys at weighted-avg cost, after tax; misses flagged."""

from osrs_flipper.analysis import realized_pnl
from osrs_flipper.runelite import Fill


def _f(iid, is_buy, qty, price, t_ms, name="X", state="BOUGHT"):
    return Fill(uuid=f"{iid}-{t_ms}", item_id=iid, name=name, is_buy=is_buy, qty=qty,
                price=price, state=state, t_ms=t_ms)


def test_matches_sells_to_buys_after_tax():
    fills = [_f(561, True, 100, 100, 1000), _f(561, False, 100, 110, 2000, state="SOLD")]
    r = realized_pnl(fills)
    # tax(110)=floor(2.2)=2 → net 108; pnl = (108-100)*100 = 800
    assert round(r["realized"]) == 800 and r["wins"] == 1 and r["losses"] == 0


def test_loss_on_adverse_move_is_counted():
    fills = [_f(1, True, 10, 100, 1), _f(1, False, 10, 90, 2, state="SOLD")]
    r = realized_pnl(fills)
    assert r["realized"] < 0 and r["losses"] == 1  # sold below buy → real loss, not hidden


def test_uncovered_sell_has_no_cost_basis_and_is_flagged():
    r = realized_pnl([_f(561, False, 50, 110, 1000, state="SOLD")])  # sell, no prior buy in data
    assert r["uncovered"] == 50 and r["realized"] == 0  # not counted as pure profit


def test_weighted_average_cost_across_two_buys():
    fills = [_f(1, True, 100, 100, 1), _f(1, True, 100, 120, 2), _f(1, False, 200, 130, 3, state="SOLD")]
    r = realized_pnl(fills)
    # avg cost 110; tax(130)=2 → net 128; pnl = (128-110)*200 = 3600
    assert round(r["realized"]) == 3600

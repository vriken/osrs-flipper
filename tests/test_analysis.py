"""Realized-P&L matching: sells net against buys at weighted-avg cost, after tax; misses flagged."""

from osrs_flipper.analysis import item_edges, realized_pnl, regime_shifts
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


def _round_trips(iid, buy_px, sell_px, n, t0=0):
    """n buy→sell round-trips of 100 units each."""
    out = []
    for i in range(n):
        out.append(_f(iid, True, 100, buy_px, t0 + 2 * i))
        out.append(_f(iid, False, 100, sell_px, t0 + 2 * i + 1, state="SOLD"))
    return out


def test_edge_penalises_a_sustained_loser_toward_the_floor():
    e = item_edges(_round_trips(1, 100, 90, 15), floor=0.3, gain=10)  # ~-11% ROI each
    assert e[1]["edge_mult"] < 0.5 and e[1]["edge_mult"] >= 0.3  # sunk, never below the floor


def test_edge_leaves_a_winner_neutral_penalty_only():
    e = item_edges(_round_trips(1, 100, 115, 15))  # profitable
    assert e[1]["edge_mult"] == 1.0  # penalty-only: winners aren't boosted, just not penalised


def test_edge_shrinks_small_samples_toward_neutral():
    one = item_edges(_round_trips(1, 100, 90, 1), gain=10)   # a single loss
    many = item_edges(_round_trips(1, 100, 90, 15), gain=10)
    assert one[1]["edge_mult"] > many[1]["edge_mult"] > 0.29  # one bad trade barely dents it


def test_regime_shift_flags_a_recovering_loser():
    edges = {1: {"edge_mult": 0.5, "ewma_roi": -0.05, "recent_roi": 0.04, "n": 10, "name": "X"}}
    got = regime_shifts(edges)
    assert got and got[0]["shift"] == "recovering"
    # ignored when sample too small or recent still negative
    assert regime_shifts({1: {"edge_mult": 0.5, "ewma_roi": -0.05, "recent_roi": 0.04, "n": 2, "name": "X"}}) == []
    assert regime_shifts({1: {"edge_mult": 0.5, "ewma_roi": -0.05, "recent_roi": -0.04, "n": 10, "name": "X"}}) == []

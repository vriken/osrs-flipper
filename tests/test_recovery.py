"""Recovery-hold mean-reversion read + double-down sizing."""

from osrs_flipper import recovery


def _week(mids):
    return [{"avgHighPrice": m + 1, "avgLowPrice": m - 1} for m in mids]


def test_recover_true_on_a_dip_that_was_green():
    # traded ~110 most of the week (above cost 105), then dipped to ~98 now → bounce candidate
    mids = [110] * 80 + [108, 104, 100, 98]
    a = recovery.assess_recovery(avg_cost=105, bail=97, mids=mids)
    assert a["recover"] and a["was_green"] and a["depressed"] and not a["rerating"]


def test_no_recover_on_steady_downtrend():
    # steadily declining all week (re-rating), not a dip — must NOT recommend holding/doubling
    mids = list(range(140, 40, -1))  # 140 → 41, monotonic down
    a = recovery.assess_recovery(avg_cost=120, bail=45, mids=mids)
    assert a["rerating"] and not a["recover"]


def test_no_recover_if_never_above_cost_this_week():
    # you overpaid: it never traded above your cost in the week → reversion to cost isn't supported
    mids = [90] * 60 + [88, 86, 85]
    a = recovery.assess_recovery(avg_cost=120, bail=84, mids=mids)
    assert not a["was_green"] and not a["recover"]


def test_double_down_blends_average_to_target():
    # 100 @ 200, now 170, target the week median 195
    qty, new_avg = recovery.double_down(held_qty=100, avg_cost=200, cur=170, target=195)
    assert qty == 20 and abs(new_avg - 195) < 0.5


def test_double_down_zero_when_price_at_or_above_target():
    assert recovery.double_down(100, 200, cur=196, target=195) == (0, 200)

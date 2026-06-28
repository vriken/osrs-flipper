"""Composite score + shrinkage behaviour."""

from osrs_flipper.scanner import MODE_WEIGHTS, _allocate, _composite, _schedule, _shrink, _worth_gp


def test_offline_ignores_fill_time():
    assert _composite(1000, fill_eta_h=2.0, time_weight=0.0) == 1000
    assert _composite(1000, fill_eta_h=99.0, time_weight=0.0) == 1000  # slow is fine offline


def test_online_divides_by_fill_time():
    assert _composite(1000, fill_eta_h=2.0, time_weight=1.0) == 500  # gp per hour


def test_balanced_uses_sqrt_of_time():
    assert _composite(1000, fill_eta_h=4.0, time_weight=0.5) == 500  # 1000 / sqrt(4)


def test_unknown_fill_time_unrankable_when_time_matters():
    assert _composite(1000, fill_eta_h=None, time_weight=1.0) == 0.0
    assert _composite(1000, fill_eta_h=None, time_weight=0.0) == 1000  # but fine offline


def test_mode_weights():
    assert MODE_WEIGHTS == {"online": 1.0, "balanced": 0.5, "offline": 0.0}


def test_shrink_pulls_unreliable_to_median():
    # an inflated but unreliable estimate (1000, reliability 0) collapses to the median
    out = _shrink([1000, 100, 100, 100, 100], [0.0, 1.0, 1.0, 1.0, 1.0])
    assert out[0] == 100  # median
    assert out[1] == 100  # reliable, unchanged


def test_shrink_identity_when_fully_reliable():
    assert _shrink([5, 3, 1], [1.0, 1.0, 1.0]) == [5, 3, 1]


def test_shrink_partial():
    # median of [300,100] = 200; reliability 0.5 pulls 300 halfway toward it → 250
    out = _shrink([300, 100], [0.5, 1.0])
    assert out == [250, 100]


def _pick(buy, cap, net=1, fill=1.0):
    return {"buy_px": buy, "cap_units": cap, "margin_abs": net, "p_complete": fill}


def test_allocate_splits_across_picks_by_liquidity():
    picks = [_pick(50, 100, 5), _pick(100, 1000, 10)]  # caps: 5,000 and 100,000
    out, idle = _allocate(picks, 20_000)
    assert out[0]["deploy"] == 5_000 and out[0]["qty"] == 100  # first capped by its liquidity
    assert out[1]["deploy"] == 15_000 and out[1]["qty"] == 150  # second takes the rest
    assert idle == 0


def test_allocate_leaves_idle_when_liquidity_capped():
    out, idle = _allocate([_pick(50, 100, 5)], 20_000)  # can only absorb 5,000
    assert out[0]["deploy"] == 5_000
    assert idle == 15_000


def test_worth_gp_flags_trivial_flips():
    tiara = {"margin_abs": 5, "p_complete": 1.0}  # liquidity-capped at 15 units
    assert _worth_gp(tiara, 15, "online") == 75  # below any sane floor → dropped
    big = {"margin_abs": 13, "p_complete": 0.5}
    assert _worth_gp(big, 1000, "online") == 6500
    assert _worth_gp(big, 1000, "hold") == 13000  # hold ignores fill (sells over time)


def test_schedule_queues_extra_buys_until_a_slot_frees():
    picks = [{"buy_eta_h": 1.0}, {"buy_eta_h": 2.0}, {"buy_eta_h": 1.0}]
    _schedule(picks, slots=2)
    assert picks[0]["place_at_h"] == 0 and picks[0]["fill_by_h"] == 1.0  # slot A now
    assert picks[1]["place_at_h"] == 0 and picks[1]["fill_by_h"] == 2.0  # slot B now
    # 3rd can't start until slot A frees at 1.0, then fills 1.0 later
    assert picks[2]["place_at_h"] == 1.0 and picks[2]["fill_by_h"] == 2.0


def test_allocate_fair_share_prevents_one_slot_soaking_all():
    # two deep-liquidity picks: fair share splits the pile rather than the first eating it
    out, idle = _allocate([_pick(1, 10**9, 1), _pick(1, 10**9, 1)], 100)
    assert out[0]["deploy"] == 50 and out[1]["deploy"] == 50
    assert idle == 0


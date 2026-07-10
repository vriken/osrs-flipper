"""Composite score + shrinkage behaviour."""

import pandas as pd

from osrs_flipper import config, scanner
from osrs_flipper.scanner import MODE_WEIGHTS, _allocate, _composite, _schedule, _shrink, _worth_gp


def test_mode_roi_weight_volume_by_day_margin_overnight():
    # active/day modes rank on throughput (roi weight 0); overnight ranks on margin%
    assert scanner._mode_roi_weight("online") == config.ROI_WEIGHT_FAST
    assert scanner._mode_roi_weight("balanced") == config.ROI_WEIGHT_FAST
    assert scanner._mode_roi_weight("offline") == config.ROI_WEIGHT_SLOW
    assert config.ROI_WEIGHT_FAST < config.ROI_WEIGHT_SLOW   # day = volume, night = margin


def test_offline_ignores_fill_time():
    assert _composite(1000, fill_eta_h=2.0, time_weight=0.0) == 1000
    assert _composite(1000, fill_eta_h=99.0, time_weight=0.0) == 1000  # slow is fine offline


def test_online_divides_by_fill_time():
    assert _composite(1000, fill_eta_h=2.0, time_weight=1.0) == 500  # gp per hour


def test_balanced_uses_sqrt_of_time():
    assert _composite(1000, fill_eta_h=4.0, time_weight=0.5) == 500  # 1000 / sqrt(4)


def test_roi_weight_zero_is_old_behaviour():
    # default and explicit 0 must reproduce the pure-gp composite
    assert _composite(1000, fill_eta_h=2.0, time_weight=1.0) == 500
    assert _composite(1000, fill_eta_h=2.0, time_weight=1.0, margin_pct=0.10, roi_weight=0.0) == 500


def test_roi_tilt_prefers_capital_efficiency():
    # same gp/cycle and fill time → the higher-ROI flip scores higher once roi_weight > 0
    lo = _composite(1000, fill_eta_h=2.0, time_weight=1.0, margin_pct=0.02, roi_weight=1.0)
    hi = _composite(1000, fill_eta_h=2.0, time_weight=1.0, margin_pct=0.10, roi_weight=1.0)
    assert hi > lo
    assert hi == 1000 * 0.10 / 2  # gp/hour × roi


def test_unknown_fill_time_unrankable_when_time_matters():
    assert _composite(1000, fill_eta_h=None, time_weight=1.0) == 0.0
    assert _composite(1000, fill_eta_h=None, time_weight=0.0) == 1000  # but fine offline


def test_mode_weights():
    assert MODE_WEIGHTS == {"online": 1.0, "balanced": 0.5, "offline": 0.0}


def test_variance_tilt_off_by_default_and_favours_reliable_when_on(monkeypatch):
    assert scanner._variance_tilt(0.4) == 1.0          # default λ=0 → no-op, ranking unchanged
    monkeypatch.setattr(config, "VARIANCE_AVERSION", 1.0)
    assert scanner._variance_tilt(0.9) > scanner._variance_tilt(0.4)  # reliable completion favoured
    assert scanner._variance_tilt(1.0) == 1.0          # a certain flip is never penalised


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
    # two deep-liquidity picks of EQUAL capital efficiency: the concentration cap splits the pile
    out, idle = _allocate([_pick(1, 10**9, 1), _pick(1, 10**9, 1)], 100)
    assert out[0]["deploy"] == 50 and out[1]["deploy"] == 50
    assert idle == 0


def test_allocate_concentrates_on_the_higher_roi_pick_within_the_cap():
    # three deep-liquidity picks; the higher-ROI one gets MORE than an even 1/3 share (compounding),
    # but no more than the concentration ceiling of the pile.
    picks = [_pick(100, 10**9, 20), _pick(100, 10**9, 5), _pick(100, 10**9, 5)]  # ROI 20% vs 5% vs 5%
    out, idle = _allocate(picks, 300_000, max_frac=0.5)
    deploys = [p["deploy"] for p in out]
    assert deploys[0] > 100_000                          # beats the even 1/3 share (100k)
    assert deploys[0] <= 150_000                          # but capped at 50% of the 300k pile
    assert abs(sum(deploys) + idle - 300_000) < 100       # cash conserved


def _candidate(iid, name, buy, sell, margin_abs, margin_fast, *, vol=50_000, limit=50_000, fill_mult=1.0):
    return {"item_id": iid, "name": name, "buy_px": buy, "sell_px": sell,
            "margin_abs": margin_abs, "margin_pct": margin_abs / buy, "margin_fast": margin_fast,
            "p_complete": 0.9, "liq_units": min(vol, limit), "buy_limit_eff": limit,
            "hold_units": min(vol, limit), "buy_rate": 5000.0, "fill_mult": fill_mult}


def test_slot_worth_floor_scales_with_net_worth_not_loose_cash(monkeypatch):
    # A 600-gp hold clears the dynamic slot-worth floor when it's based on 37k loose cash, but is
    # gated once the floor reflects a 400k net worth. This is the "don't fragment 37k into small
    # flips while 334k is landing" fix — the floor is a slot's opportunity cost, not the loose cash.
    df = pd.DataFrame([_candidate(561, "Small hold", 100, 107, margin_abs=6, margin_fast=-1,
                                  vol=100, limit=100)])  # worth = 6 × 100 = 600 gp
    monkeypatch.setattr(scanner, "scan", lambda **kw: df)
    on_cash, _, _ = scanner.build_portfolio(bankroll=37_000, free_slots=1)
    on_networth, _, _ = scanner.build_portfolio(bankroll=37_000, free_slots=1, net_worth=400_000)
    assert on_cash and not on_networth  # funded on the loose-cash floor, gated on the net-worth floor


def test_slot_worth_floor_is_dynamic_on_market_roi():
    # the floor rises with the ROI the market is paying: same net worth, fatter spreads → higher bar
    lean = pd.DataFrame([_candidate(1, "lean", 100, 103, margin_abs=3, margin_fast=1)])   # 3% roi
    rich = pd.DataFrame([_candidate(1, "rich", 100, 115, margin_abs=15, margin_fast=5)])  # 15% roi
    assert scanner.slot_worth_floor(1_000_000, rich) > scanner.slot_worth_floor(1_000_000, lean)


def test_blended_ref_is_relative_smooths_noise_reacts_to_a_crash():
    # weighting is a % of price, not fixed gp: a 1gp gap on a 100gp item barely moves the ref…
    assert abs(scanner.blended_ref(100, 101, 0.08) - 100) < 0.5
    # …the SAME 1gp gap on a 10gp item (10% ≥ 8% big-move) fully trusts the tick
    assert scanner.blended_ref(10, 9, 0.08) == 9
    # a crash (40% divergence) trusts the tick regardless of absolute size
    assert scanner.blended_ref(100, 60, 0.08) == 60
    assert scanner.blended_ref(None, 60, 0.08) == 60 and scanner.blended_ref(100, None, 0.08) == 100


def test_stack_roi_weight_scales_with_net_worth():
    import math

    from osrs_flipper import config
    # small stack → hard ROI tilt (compound); large stack → mode throughput floor; mid interpolates (log)
    assert scanner._stack_roi_weight("online", config.ROI_STACK_LO) == config.ROI_WEIGHT_SMALL_STACK
    assert scanner._stack_roi_weight("online", config.ROI_STACK_HI) == config.ROI_WEIGHT_FAST  # 0 by day
    mid = scanner._stack_roi_weight("online", int(round(math.sqrt(config.ROI_STACK_LO * config.ROI_STACK_HI))))
    assert config.ROI_WEIGHT_FAST < mid < config.ROI_WEIGHT_SMALL_STACK
    # mode still nudges the FLOOR: a large overnight stack keeps favouring fat margins
    assert scanner._stack_roi_weight("offline", config.ROI_STACK_HI) == config.ROI_WEIGHT_SLOW
    # a tiny stack tilts to the small-stack weight regardless of mode
    assert scanner._stack_roi_weight("offline", 1) == config.ROI_WEIGHT_SMALL_STACK


def test_roi_per_hour_floors_fill_time_so_near_instant_flips_dont_explode():
    # the reported bug: a 1.9% flip "in 0.0h" must NOT out-rate a 4% flip in 0.9h (was "1780× faster")
    fast = scanner.roi_per_hour(0.019, 0.0, 0.25)   # 0.019 / max(0, 0.25) = 0.076
    fat = scanner.roi_per_hour(0.040, 0.9, 0.25)    # 0.040 / 0.9        = 0.044
    assert fast < 2 * fat                            # no longer a swap suggestion
    assert scanner.roi_per_hour(0.05, float("inf"), 0.25) == 0.0  # no volume → unrankable


def test_rebalance_flags_slow_offer_but_spares_the_fast_and_near_done():
    alts = [{"alt_roi_h": 0.30, "alt_name": "A"}, {"alt_roi_h": 0.28, "alt_name": "B"}]
    offers = [
        {"slot": 0, "roi_h": 0.02, "fill_frac": 0.1},   # slow + early → swap (0.30 ≥ 2×0.02)
        {"slot": 1, "roi_h": 0.20, "fill_frac": 0.1},   # already fast (0.28 < 2×0.20) → keep
        {"slot": 2, "roi_h": 0.02, "fill_frac": 0.9},   # slow but ~done → keep progress
        {"slot": 3, "roi_h": -0.05, "fill_frac": 0.0},  # underwater/stuck → swap
    ]
    got = {s["slot"] for s in scanner.rebalance_swaps(offers, alts, ratio=2.0, max_fill=0.5)}
    assert got == {0, 3}


def test_rebalance_pairs_distinct_alts_never_the_same_one_twice():
    # two beatable offers, but only ONE good alt → only one swap (can't pour both into one item)
    offers = [{"slot": 0, "roi_h": 0.02, "fill_frac": 0.0}, {"slot": 1, "roi_h": 0.02, "fill_frac": 0.0}]
    one_alt = [{"alt_roi_h": 0.30, "alt_name": "Blood rune"}]
    swaps = scanner.rebalance_swaps(offers, one_alt, ratio=2.0, max_fill=0.5)
    assert len(swaps) == 1                       # not two offers both cancelled for one alt
    # with two distinct good alts, both can swap — each alt used once
    two_alts = [{"alt_roi_h": 0.30, "alt_name": "X"}, {"alt_roi_h": 0.29, "alt_name": "Y"}]
    swaps2 = scanner.rebalance_swaps(offers, two_alts, ratio=2.0, max_fill=0.5)
    assert len(swaps2) == 2 and {s["alt_name"] for s in swaps2} == {"X", "Y"}


def test_one_gp_tick_flip_is_dropped(monkeypatch):
    # reversal (was "penny staple kept as hold"): a 1gp integer-tick staple (Air rune 4→5) shows
    # 25% ROI but doesn't fill — you're behind a wall of identical 1gp bids (fill calibration ≈0).
    # Now gated by MIN_NET_MARGIN. A real ≥2gp flip stays.
    df = pd.DataFrame([
        _candidate(561, "Air rune", 4, 5, margin_abs=1, margin_fast=-1),          # 1gp → dropped
        _candidate(1391, "Battlestaff", 200, 215, margin_abs=10, margin_fast=5),  # real fast flip
    ])
    monkeypatch.setattr(scanner, "scan", lambda **kw: df)
    picks, _, _ = scanner.build_portfolio(bankroll=100_000, free_slots=1)
    by_name = {p["name"]: p for p in picks}
    assert "Air rune" not in by_name                     # 1gp margin < MIN_NET_MARGIN → gated out
    assert by_name["Battlestaff"]["tier"] != "hold"      # 10gp queue-jumpable flip is active


def test_hold_quality_floor_drops_low_roi(monkeypatch):
    # overflow cash only parks in holds that clear HOLD_MIN_MARGIN (3%); a 2% commodity is left
    # liquid rather than churned, while a 6% spread is kept.
    df = pd.DataFrame([
        _candidate(1391, "Battlestaff", 200, 215, margin_abs=10, margin_fast=5),   # active flip
        _candidate(561, "Quality hold", 100, 107, margin_abs=6, margin_fast=-1),   # 6% → hold
        _candidate(1117, "Junk hold", 100, 102, margin_abs=2, margin_fast=-1),     # 2% → dropped
    ])
    monkeypatch.setattr(scanner, "scan", lambda **kw: df)
    # 2 free slots so the active flip and the quality hold both fit; Junk is dropped for margin, not slots.
    picks, _, _ = scanner.build_portfolio(bankroll=100_000, free_slots=2)
    by_name = {p["name"]: p for p in picks}
    assert by_name["Quality hold"]["tier"] == "hold"
    assert "Junk hold" not in by_name  # below HOLD_MIN_MARGIN → left liquid, not churned


def test_build_portfolio_scales_gp_by_fill_mult(monkeypatch):
    # a flip with calibrated fill_mult 0.5 → its expected gp is halved (model self-corrects)
    df = pd.DataFrame([_candidate(1, "A", 100, 110, margin_abs=10, margin_fast=5, fill_mult=0.5)])
    monkeypatch.setattr(scanner, "scan", lambda **kw: df)
    picks, _, _ = scanner.build_portfolio(bankroll=100_000, free_slots=1)
    p = picks[0]
    assert abs(p["gp"] - 10 * p["qty"] * 0.9 * 0.5) < 1   # margin × qty × p_complete × fill_mult


def test_buyable_head_filters_knives_and_backfills_to_top():
    # the --no-persistence path now drops pumps/knives from the snapshot, same as the deep path.
    df = pd.DataFrame([_candidate(i, f"i{i}", 100, 110, 10, 5) for i in range(1, 11)])
    knives = {2, 4}  # not buyable (pump / falling knife)
    out = scanner._buyable_head(df, top=3, buyable=lambda iid: iid not in knives)
    assert list(out["item_id"]) == [1, 3, 5]  # 2 and 4 skipped, backfilled with the next buyable rows
    # nothing buyable → empty frame but columns preserved (callers still get a valid DataFrame)
    empty = scanner._buyable_head(df, top=3, buyable=lambda iid: False)
    assert empty.empty and list(empty.columns) == list(df.columns)


def test_build_portfolio_scales_gp_by_impact_mult(monkeypatch):
    # a flip whose intended size is a big share of volume takes a price-impact haircut on its gp
    df = pd.DataFrame([_candidate(1, "A", 100, 110, margin_abs=10, margin_fast=5)])
    df["impact_mult"] = 0.8
    monkeypatch.setattr(scanner, "scan", lambda **kw: df)
    picks, _, _ = scanner.build_portfolio(bankroll=100_000, free_slots=1)
    p = picks[0]
    assert abs(p["gp"] - 10 * p["qty"] * 0.9 * 0.8) < 1  # margin × qty × p_complete × impact_mult


def test_build_portfolio_hung_leg_hits_active_not_holds(monkeypatch):
    # an active flip with a shaky sell leg takes the hung-leg haircut on its gp…
    active = pd.DataFrame([_candidate(1, "Active", 100, 110, margin_abs=10, margin_fast=5)])
    active["hung_mult"] = 0.7
    monkeypatch.setattr(scanner, "scan", lambda **kw: active)
    picks, _, _ = scanner.build_portfolio(bankroll=100_000, free_slots=1)
    p = picks[0]
    assert p["tier"] != "hold"
    assert abs(p["gp"] - 10 * p["qty"] * 0.9 * 0.7) < 1   # active gp = margin × qty × p_complete × hung_mult

    # …but a hold sells over later cycles, so the hung-leg term does not apply to it
    hold = pd.DataFrame([_candidate(2, "Hold", 100, 107, margin_abs=6, margin_fast=-1)])  # 6% → hold
    hold["hung_mult"] = 0.7
    monkeypatch.setattr(scanner, "scan", lambda **kw: hold)
    picks2, _, _ = scanner.build_portfolio(bankroll=100_000, free_slots=1)
    h = picks2[0]
    assert h["tier"] == "hold"
    assert abs(h["gp"] - 6 * h["qty"]) < 1                # hold gp ignores hung_mult (fill=1.0, sells over time)


def test_placement_order_ranks_by_roi_not_just_speed(monkeypatch):
    # two equal-everything holds: the higher-ROI one is placed first (ROI-tilted gp/hour),
    # not left in scan/df order.
    df = pd.DataFrame([
        _candidate(561, "Low ROI", 100, 105, margin_abs=5, margin_fast=-1),    # 5%
        _candidate(562, "High ROI", 100, 111, margin_abs=10, margin_fast=-1),  # 10%
    ])
    monkeypatch.setattr(scanner, "scan", lambda **kw: df)
    picks, _, _ = scanner.build_portfolio(bankroll=100_000, free_slots=1)
    assert picks[0]["name"] == "High ROI"



# --- crowding / competition tilt: favour uncrowded niches over bot-raced staples --------------------

def test_crowding_tilt_boosts_niches_penalises_staples(monkeypatch):
    monkeypatch.setattr(config, "CROWDING_TILT", 0.25)
    monkeypatch.setattr(config, "CROWDING_PIVOT", 50_000_000)
    pivot = scanner._crowding_tilt(50_000_000)
    niche = scanner._crowding_tilt(1_000_000)      # well below pivot → quiet niche
    staple = scanner._crowding_tilt(2_000_000_000)  # far above pivot → bot-raced staple
    assert pivot == 1.0                              # at the pivot: neutral
    assert 1.0 < niche <= 1.25                       # niche boosted, capped at 1+gain
    assert staple < 1.0                              # crowded staple penalised
    assert niche > pivot > staple                    # monotonic in crowding


def test_crowding_tilt_disabled_and_edge_cases(monkeypatch):
    monkeypatch.setattr(config, "CROWDING_TILT", 0.0)
    assert scanner._crowding_tilt(1_000_000) == 1.0  # gain 0 → no-op
    monkeypatch.setattr(config, "CROWDING_TILT", 0.25)
    assert scanner._crowding_tilt(None) == 1.0        # missing turnover → neutral
    assert scanner._crowding_tilt(0) == 1.0
    assert scanner._crowding_tilt(10**15) >= 0.1      # floored, never zero/negative

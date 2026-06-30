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


def _candidate(iid, name, buy, sell, margin_abs, margin_fast, *, vol=50_000, limit=50_000, fill_mult=1.0):
    return {"item_id": iid, "name": name, "buy_px": buy, "sell_px": sell,
            "margin_abs": margin_abs, "margin_pct": margin_abs / buy, "margin_fast": margin_fast,
            "p_complete": 0.9, "liq_units": min(vol, limit), "buy_limit_eff": limit,
            "hold_units": min(vol, limit), "buy_rate": 5000.0, "fill_mult": fill_mult}


def test_one_gp_tick_flip_is_dropped(monkeypatch):
    # reversal (was "penny staple kept as hold"): a 1gp integer-tick staple (Air rune 4→5) shows
    # 25% ROI but doesn't fill — you're behind a wall of identical 1gp bids (fill calibration ≈0).
    # Now gated by MIN_NET_MARGIN. A real ≥2gp flip stays.
    df = pd.DataFrame([
        _candidate(561, "Air rune", 4, 5, margin_abs=1, margin_fast=-1),          # 1gp → dropped
        _candidate(1391, "Battlestaff", 200, 215, margin_abs=10, margin_fast=5),  # real fast flip
    ])
    monkeypatch.setattr(scanner, "scan", lambda **kw: df)
    picks, _ = scanner.build_portfolio(bankroll=100_000, free_slots=1)
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
    picks, _ = scanner.build_portfolio(bankroll=100_000, free_slots=1)
    by_name = {p["name"]: p for p in picks}
    assert by_name["Quality hold"]["tier"] == "hold"
    assert "Junk hold" not in by_name  # below HOLD_MIN_MARGIN → left liquid, not churned


def test_build_portfolio_scales_gp_by_fill_mult(monkeypatch):
    # a flip with calibrated fill_mult 0.5 → its expected gp is halved (model self-corrects)
    df = pd.DataFrame([_candidate(1, "A", 100, 110, margin_abs=10, margin_fast=5, fill_mult=0.5)])
    monkeypatch.setattr(scanner, "scan", lambda **kw: df)
    picks, _ = scanner.build_portfolio(bankroll=100_000, free_slots=1)
    p = picks[0]
    assert abs(p["gp"] - 10 * p["qty"] * 0.9 * 0.5) < 1   # margin × qty × p_complete × fill_mult


def test_placement_order_ranks_by_roi_not_just_speed(monkeypatch):
    # two equal-everything holds: the higher-ROI one is placed first (ROI-tilted gp/hour),
    # not left in scan/df order.
    df = pd.DataFrame([
        _candidate(561, "Low ROI", 100, 105, margin_abs=5, margin_fast=-1),    # 5%
        _candidate(562, "High ROI", 100, 111, margin_abs=10, margin_fast=-1),  # 10%
    ])
    monkeypatch.setattr(scanner, "scan", lambda **kw: df)
    picks, _ = scanner.build_portfolio(bankroll=100_000, free_slots=1)
    assert picks[0]["name"] == "High ROI"


"""Calibration math: β is the realised spread haircut, fill correction includes the misses."""

from osrs_flipper import calibration


def test_liquidity_buckets():
    # buckets are on gp TURNOVER (units × mid), not unit count
    assert calibration.liquidity_bucket(500_000) == "low"
    assert calibration.liquidity_bucket(5_000_000) == "med"
    assert calibration.liquidity_bucket(50_000_000) == "high"


def test_turnover_from_row():
    # 3 units/h at a ~522k mid → ~1.57M gp/h (a Karil's-skirt-like big-ticket item)
    assert calibration._turnover({"vol_1h_binding": 3, "avg_low": 515_000, "avg_high": 530_000}) == 1_567_500


def test_beta_measures_where_in_the_spread_the_fill_landed():
    # spread 100 (avg_low 1000 / avg_high 1100); a BUY filled at 1025 → β = 0.25
    rows = [{"side": "BUY", "avg_low": 1000, "avg_high": 1100, "spread": 100,
             "fill_px": 1025, "vol_1h_binding": 5000, "qty": 10, "filled_qty": 10, "pred_p_fill": 0.5}]
    out = calibration.calibrate_beta(rows, prior=0.25)
    assert out["global_measured"] == 0.25


def test_sell_beta_is_distance_below_the_ask():
    # SELL filled at 1075 with avg_high 1100, spread 100 → β = 0.25
    rows = [{"side": "SELL", "avg_low": 1000, "avg_high": 1100, "spread": 100,
             "fill_px": 1075, "vol_1h_binding": 5000, "qty": 10, "filled_qty": 10}]
    out = calibration.calibrate_beta(rows, prior=0.5)
    assert out["global_measured"] == 0.25


def test_shrinkage_pulls_small_samples_toward_prior():
    # one measurement of β=0.0 against a 0.25 prior should barely move with k=20
    assert calibration.shrink(0.0, 0.25, n=1, k=20) > 0.23
    # with lots of data it converges to the measurement
    assert calibration.shrink(0.0, 0.25, n=2000, k=20) < 0.01


def test_expired_attempts_drag_fill_correction_down():
    # one fill (100% of qty) at predicted 0.5 → factor 2.0; one expired (0% filled) → factor 0.0
    rows = [
        {"qty": 10, "filled_qty": 10, "pred_p_fill": 0.5, "status": "filled", "vol_1h_binding": 100},
        {"qty": 10, "filled_qty": 0, "pred_p_fill": 0.5, "status": "expired", "vol_1h_binding": 100},
    ]
    out = calibration.calibrate_fill(rows)
    assert out["n"] == 2
    assert out["global_measured"] == 1.0  # median(2.0, 0.0) — the miss is counted, not dropped


def test_effective_beta_uses_shrunk_global_clamped_else_prior():
    assert calibration.effective_beta(None, 0.25) == 0.25                 # no calibration → prior
    assert calibration.effective_beta({"global": 0.05}, 0.25) == 0.05      # calibrated value applied
    assert calibration.effective_beta({"global": None}, 0.25) == 0.25      # no measure → prior
    assert calibration.effective_beta({"global": 0.9}, 0.25) == 0.5        # clamped to hi
    assert calibration.effective_beta({"global": -0.3}, 0.25) == 0.0       # clamped to lo


def test_fill_multiplier_demotes_overoptimistic_but_shrinks_and_clamps():
    # heavy over-optimism on a high-turnover item (all misses) → multiplier < 1 but shrunk off 0
    # 5000 units × ~5000 mid = 25M gp/h turnover → "high" bucket
    rows = [{"qty": 10, "filled_qty": 0, "pred_p_fill": 0.8, "status": "expired",
             "vol_1h_binding": 5000, "avg_low": 4900, "avg_high": 5100} for _ in range(4)]
    cal = calibration.calibrate_fill(rows)
    m = calibration.fill_multiplier(cal, 25_000_000)
    assert 0.1 <= m < 1.0                                       # demoted, shrunk toward 1.0, floored
    assert calibration.fill_multiplier(None, 25_000_000) == 1.0  # no calibration → no change


def test_beta_skips_rows_without_a_fill_price():
    rows = [{"side": "BUY", "avg_low": 1000, "avg_high": 1100, "spread": 100,
             "fill_px": None, "vol_1h_binding": 5000, "qty": 10, "filled_qty": 0}]
    out = calibration.calibrate_beta(rows, prior=0.3)
    assert out["n"] == 0
    assert out["global"] == 0.3  # falls back to the prior


# --- fill-time (ETA) calibration -------------------------------------------------------------------

def _eta_row(pred, ts, filled_ts=None, resolved_ts=None, status="filled", filled_qty=10,
             avg_low=48, avg_high=52, vol=5000):
    return {"pred_eta_h": pred, "ts": ts, "filled_ts": filled_ts, "resolved_ts": resolved_ts,
            "status": status, "filled_qty": filled_qty, "avg_low": avg_low, "avg_high": avg_high,
            "vol_1h_binding": vol}


def test_pv_bucket_is_2d_price_x_volume():
    assert calibration.pv_bucket(500, 500) == "cheap/thin"
    assert calibration.pv_bucket(50_000, 5_000) == "mid/med"
    assert calibration.pv_bucket(500_000, 50_000) == "dear/deep"


def test_calibrate_eta_learns_a_slow_bucket():
    # predicted 1h, actually took 2h to fill → ratio 2.0 (model too optimistic on speed)
    rows = [_eta_row(1.0, ts=0, filled_ts=7200, status="filled") for _ in range(30)]
    out = calibration.calibrate_eta(rows)
    assert out["global_measured"] == 2.0 and out["global"] > 1.3   # shrunk toward 1.0 but clearly slow


def test_calibrate_eta_uses_never_fills_as_a_lower_bound_only_when_over():
    # a never-filled offer that sat 3× its predicted ETA and still didn't fill → pushes the ratio UP
    over = [_eta_row(1.0, ts=0, resolved_ts=10800, status="expired", filled_qty=0) for _ in range(20)]
    assert calibration.calibrate_eta(over)["global_measured"] == 3.0
    # a never-fill that resolved SOONER than predicted tells us nothing about fill speed → ignored
    under = [_eta_row(2.0, ts=0, resolved_ts=1800, status="cancelled", filled_qty=0)]
    assert calibration.calibrate_eta(under)["n"] == 0


def test_eta_multiplier_defaults_to_1_and_clamps():
    assert calibration.eta_multiplier(None, 100, 5000) == 1.0
    assert calibration.eta_multiplier({}, 100, 5000) == 1.0
    hot = {"buckets": {"cheap/med": {"shrunk": 99.0}}, "global": 99.0}
    assert calibration.eta_multiplier(hot, 100, 5000) == 3.0   # clamped to hi


def test_attribution_classifies_the_miss_reason():
    c = calibration
    assert c.attribute({"status": "expired", "filled_qty": 0, "qty": 100}) == "never_filled"
    assert c.attribute({"status": "cancelled", "filled_qty": 0, "qty": 100}) == "never_filled"
    assert c.attribute({"status": "filled", "filled_qty": 40, "qty": 100}) == "partial"
    assert c.attribute({"status": "filled", "filled_qty": 100, "qty": 100,
                        "pred_eta_h": 1.0, "ts": 0, "filled_ts": 7200}) == "slow"    # 2h vs 1h
    assert c.attribute({"status": "filled", "filled_qty": 100, "qty": 100,
                        "pred_eta_h": 2.0, "ts": 0, "filled_ts": 1800}) == "fast"    # 0.5h vs 2h
    assert c.attribute({"status": "filled", "filled_qty": 100, "qty": 100,
                        "pred_eta_h": 1.0, "ts": 0, "filled_ts": 3600}) == "on_time"


def test_eta_attribution_counts():
    rows = [{"status": "expired", "filled_qty": 0, "qty": 100},
            {"status": "expired", "filled_qty": 0, "qty": 100},
            {"status": "filled", "filled_qty": 100, "qty": 100, "pred_eta_h": 1.0, "ts": 0, "filled_ts": 7200}]
    assert calibration.eta_attribution(rows) == {"never_filled": 2, "slow": 1}


def test_mid_falls_back_to_the_present_side():
    # a snapshot missing one avg side must use the side it HAS, not treat the missing one as 0 (which
    # halves the mid and can misbucket the row's liquidity).
    assert calibration._mid(1000, None) == 1000
    assert calibration._mid(None, 800) == 800
    assert calibration._mid(1000, 2000) == 1500
    assert calibration._mid(None, None) == 0
    assert calibration._turnover({"vol_1h_binding": 50, "avg_low": 100_000, "avg_high": None}) == 5_000_000

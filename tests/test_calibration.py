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

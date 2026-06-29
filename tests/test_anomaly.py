"""Anomaly detector: screen / analyze / classify and the deep-check that needs a volume signature."""

from osrs_flipper import anomaly


def _lp(high, low):
    return {"high": high, "low": low}


def _hp(ah, al, hv, lv):
    return {"avgHighPrice": ah, "avgLowPrice": al, "highPriceVolume": hv, "lowPriceVolume": lv}


def test_screen_flags_liquid_dislocation_not_thin():
    latest = {1: _lp(150, 140), 2: _lp(150, 140)}          # both live mid ~145 vs avg 100 → +45%
    hourly = {1: _hp(105, 95, 5000, 5000),                  # liquid → flagged
              2: _hp(105, 95, 50, 50)}                      # thin → ignored (illiquidity, not manip)
    hits = {c["item_id"] for c in anomaly.screen(latest, hourly, div_min=0.15, vol_min=1000)}
    assert hits == {1}


def test_screen_ignores_small_divergence():
    latest = {1: _lp(102, 98)}                              # live ~100 vs avg ~100 → ~0%
    hourly = {1: _hp(101, 99, 5000, 5000)}
    assert anomaly.screen(latest, hourly, div_min=0.15, vol_min=1000) == []


def test_analyze_baseline_volz_slope():
    # flat ~100 history, then a final volume-spiked, higher bar
    bars = [_hp(101, 99, 100, 100) for _ in range(10)] + [_hp(141, 139, 5000, 5000)]
    a = anomaly.analyze(bars)
    assert a is not None
    assert 99 <= a["baseline"] <= 101        # median ignores the one spike bar
    assert a["vol_z"] > 3                     # last bar's volume is wildly abnormal
    assert a["slope"] > 0                     # price rose into the spike


def test_analyze_needs_enough_bars():
    assert anomaly.analyze([_hp(100, 100, 10, 10)] * 3) is None


def test_classify_phases():
    assert anomaly.classify(0.30, slope=2.0, div_min=0.15)[0] == "PUMP↑"
    assert anomaly.classify(0.30, slope=-2.0, div_min=0.15)[0] == "FADE↓"
    assert anomaly.classify(-0.30, slope=2.0, div_min=0.15)[0] == "RECOVER↑"
    assert anomaly.classify(-0.30, slope=-2.0, div_min=0.15)[0] == "DUMP↓"
    assert anomaly.classify(0.05, slope=0.0, div_min=0.15) == ("", "")


def test_detect_requires_volume_signature():
    # a real price dislocation but NO abnormal volume (flat vols) → not flagged as manipulation
    latest = {1: _lp(150, 140)}
    hourly = {1: _hp(105, 95, 5000, 5000)}
    flat_bars = [_hp(101, 99, 100, 100) for _ in range(12)]
    assert anomaly.detect(latest, hourly, {1: "X"}, lambda i: flat_bars,
                          div_min=0.15, vol_min=1000, vol_z_min=2.0) == []


def test_detect_flags_overdump_with_revert_ev():
    # live ~70 dumped below a ~100 baseline, on a volume spike, still falling → DUMP↓, positive EV.
    # the 1h-avg lags at the old baseline (~100) — that's what makes the screen flag the dislocation
    latest = {1: _lp(72, 68)}
    hourly = {1: _hp(103, 97, 6000, 6000)}
    bars = [_hp(101, 99, 100, 100) for _ in range(10)] + [_hp(72, 68, 8000, 8000)]
    hits = anomaly.detect(latest, hourly, {1: "Dumpy"}, lambda i: bars,
                          div_min=0.15, vol_min=1000, vol_z_min=2.0)
    assert len(hits) == 1
    h = hits[0]
    assert h["phase"] in ("DUMP↓", "RECOVER↑") and h["div_now"] < 0
    assert h["revert_ev_unit"] > 0          # buy ~72, baseline ~100 → reversion profit per unit

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


def _bars(mids, vols):
    return [{"avgHighPrice": m + 1, "avgLowPrice": m - 1,
             "highPriceVolume": v // 2, "lowPriceVolume": v // 2} for m, v in zip(mids, vols, strict=True)]


def test_assess_price_normal():
    bars = _bars([100] * 20, [200] * 20)
    a = anomaly.assess(1, {1: {"high": 101, "low": 99}}, {1: {}}, lambda i, s: bars)
    assert abs(a["div"]) < 0.15 and "normal" in anomaly.summary_line(a)


def test_assess_falling_knife_on_normal_volume():
    # drifted ~45% below baseline on FLAT volume → re-rating, not a dip (swamp-paste case)
    bars = _bars([100] * 17 + [80, 70, 55], [200] * 20)
    a = anomaly.assess(1, {1: {"high": 56, "low": 54}}, {1: {}}, lambda i, s: bars)
    assert a["div"] < -0.15
    assert "falling knife" in anomaly.summary_line(a)


def test_assess_overdump_recovering_on_volume_is_revert_buy():
    bars = _bars([100] * 17 + [60, 55, 68], [200] * 19 + [9000])  # dumped, volume spike, turning up
    a = anomaly.assess(1, {1: {"high": 69, "low": 67}}, {1: {}}, lambda i, s: bars)
    assert a["div"] < -0.15 and a["vol_z"] >= 2
    assert "revert-buy" in anomaly.summary_line(a)


def _assess(mids, vols, live):
    return anomaly.assess(1, {1: {"high": live + 1, "low": live - 1}}, {1: {}}, lambda i, s: _bars(mids, vols))


def test_is_buyable_matches_the_summary_line_verdict():
    # the buy filter and the `why` text must never disagree — same rule behind both.
    cases = {
        "normal":       _assess([100] * 20, [200] * 20, 100),                           # buyable
        "revert":       _assess([100] * 17 + [60, 55, 68], [200] * 19 + [9000], 68),    # buyable
        "knife":        _assess([100] * 17 + [80, 70, 55], [200] * 20, 55),             # not
        "wait_floor":   _assess([100] * 17 + [70, 62, 55], [200] * 19 + [9000], 55),    # not
        "pump":         _assess([100] * 17 + [130, 140, 150], [200] * 20, 150),         # not
    }
    for a in cases.values():
        line = anomaly.summary_line(a)  # "price normal" — not bare "normal" (knife line says "normal volume")
        assert anomaly.is_buyable(a) is (("price normal" in line) or ("revert-buy" in line)), line
    assert anomaly.is_buyable(cases["normal"]) and anomaly.is_buyable(cases["revert"])
    assert not any(anomaly.is_buyable(cases[k]) for k in ("knife", "wait_floor", "pump"))


def test_is_buyable_true_when_no_baseline():
    assert anomaly.is_buyable({"ref_baseline": None, "live_mid": None}) is True


def test_steep_decline_within_normal_band_is_not_buyable():
    # only ~-8% off norm (inside the ±15% band) but falling hard bar-over-bar → a falling knife before
    # it crosses the band. Must be blocked, and the `why` must agree (no pick/why contradiction).
    a = _assess([100] * 17 + [98, 95, 92], [200] * 20, 92)
    assert abs(a["div"]) < 0.15                       # inside the "normal" band
    assert not anomaly.is_buyable(a)                  # but free-falling → blocked
    assert "sliding" in anomaly.summary_line(a)       # why reflects the same verdict


def test_normal_band_gentle_drift_stays_buyable():
    # inside the band and only drifting slowly → still a fine buy (don't over-reject ordinary noise)
    a = _assess([100] * 20, [200] * 20, 99)
    assert abs(a["div"]) < 0.15 and anomaly.is_buyable(a)
    assert "price normal" in anomaly.summary_line(a)


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

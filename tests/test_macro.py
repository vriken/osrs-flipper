"""Bond macro signal: gp-inflation read from bond price drift."""

from osrs_flipper import config, macro


def _series(base, growth, n=30):
    return [{"avgHighPrice": base * (1 + growth) ** i, "avgLowPrice": base * (1 + growth) ** i}
            for i in range(n)]


def _wire(monkeypatch, series, price=None):
    monkeypatch.setattr(macro.api, "timeseries", lambda iid, ts: series)
    last = series[-1]["avgHighPrice"] if series else None
    monkeypatch.setattr(macro.api, "latest",
                        lambda: {config.BOND_ITEM_ID: {"high": int(price or last or 0)}})


def test_bond_signal_inflating(monkeypatch):
    _wire(monkeypatch, _series(7_000_000, 0.005))
    s = macro.bond_signal()
    assert s["direction"] == "inflating" and s["mu"] > 0 and s["weekly"] > 0
    assert "GP inflating" in macro.bond_line() and "▲" in macro.bond_line()


def test_bond_signal_deflating(monkeypatch):
    _wire(monkeypatch, _series(7_000_000, -0.005))
    s = macro.bond_signal()
    assert s["direction"] == "deflating" and s["mu"] < 0
    assert "GP deflating" in macro.bond_line() and "▼" in macro.bond_line()


def test_bond_signal_stable(monkeypatch):
    _wire(monkeypatch, _series(7_000_000, 0.0002))  # drift below the inflation epsilon
    assert macro.bond_signal()["direction"] == "stable"


def test_bond_signal_no_data(monkeypatch):
    def boom(iid, ts):
        raise RuntimeError("api down")
    monkeypatch.setattr(macro.api, "timeseries", boom)
    monkeypatch.setattr(macro.api, "latest", lambda: {})
    assert macro.bond_signal() is None
    assert macro.bond_line() is None


def test_bond_signal_price_only_short_series(monkeypatch):
    _wire(monkeypatch, _series(7_000_000, 0.01, n=5), price=7_500_000)  # too few bars for stats
    s = macro.bond_signal()
    assert s["price"] == 7_500_000 and s["mu"] is None
    assert macro.bond_line() == "macro: bond 7,500,000"


def test_arrow_and_direction_never_disagree(monkeypatch):
    # regression: arrow (was 8-bar change) and direction (30-bar μ) could contradict. Now both derive
    # from μ, so a series whose recent bars rise but whose overall drift falls must read consistently.
    rising_tail = [{"avgHighPrice": p, "avgLowPrice": p} for p in
                   ([10_000_000 * (0.99) ** i for i in range(22)] + [8_000_000, 8_400_000, 8_800_000,
                    9_200_000, 9_600_000, 10_000_000, 10_400_000, 10_800_000])]
    monkeypatch.setattr(macro.api, "timeseries", lambda iid, ts: rising_tail)
    monkeypatch.setattr(macro.api, "latest", lambda: {config.BOND_ITEM_ID: {"high": 10_800_000}})
    s = macro.bond_signal()
    line = macro.bond_line()
    up = s["weekly"] > 0
    assert (("▲" in line) == up) and (("▼" in line) == (not up and s["weekly"] < 0))
    # direction word must match the arrow's sign, never the contradictory case we fixed
    if up:
        assert "inflating" in line
    elif s["weekly"] < 0:
        assert "deflating" in line

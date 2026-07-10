"""The store-of-value screen: bounded universe, risk/return ranking, worth-holding flag."""

from osrs_flipper import treasury


def _flat_series(base, growth, n=30):
    """A clean geometric price path → constant log-return μ=ln(1+growth), σ≈0."""
    return [{"avgHighPrice": base * (1 + growth) ** i, "avgLowPrice": base * (1 + growth) ** i}
            for i in range(n)]


def _volatile_series(base, n=30):
    """Alternating ±15% mid → σ well above STORE_MAX_VOL, so it's rejected as a store."""
    return [{"avgHighPrice": base * (1.15 if i % 2 else 0.87),
             "avgLowPrice": base * (1.15 if i % 2 else 0.87)} for i in range(n)]


# id → (price, high_vol, low_vol) for the /1h pre-filter; turnover = price*(hv+lv)
_UNIVERSE = {
    1: ("SteadyRiser", 200_000, 20, 20),      # deep, rising, calm → the ideal store
    2: ("Bleeder", 300_000, 15, 15),          # deep, calm, but DROPPING → not worth holding
    3: ("Volatile", 250_000, 16, 16),         # deep but too swingy → rejected
    4: ("CheapJunk", 50_000, 100, 100),       # below STORE_MIN_PRICE → filtered pre-timeseries
    5: ("ThinPricey", 500_000, 1, 1),         # rich but illiquid (1M gp/h) → filtered pre-timeseries
}
_SERIES = {
    1: _flat_series(200_000, 0.003),   # +0.3%/day
    2: _flat_series(300_000, -0.003),  # −0.3%/day
    3: _volatile_series(250_000),
    4: _flat_series(50_000, 0.002),
    5: _flat_series(500_000, 0.002),
}


def _wire(monkeypatch):
    monkeypatch.setattr(treasury.config, "MEMBERS", False)  # skip the members gate
    mapping = [{"id": i, "name": v[0], "members": False} for i, v in _UNIVERSE.items()]
    hr = {i: {"avgHighPrice": v[1], "avgLowPrice": v[1], "highPriceVolume": v[2], "lowPriceVolume": v[3]}
          for i, v in _UNIVERSE.items()}
    latest = {i: {"high": v[1], "low": v[1]} for i, v in _UNIVERSE.items()}
    calls = []

    def fake_ts(iid, timestep):
        calls.append(iid)
        return _SERIES[iid]

    monkeypatch.setattr(treasury.api, "timeseries", fake_ts)
    return mapping, latest, hr, calls


def test_rank_stores_filters_and_ranks(monkeypatch):
    mapping, latest, hr, calls = _wire(monkeypatch)
    rows = treasury.rank_stores(mapping, latest, hr, top=10)
    names = [r["name"] for r in rows]

    # cheap + thin items never even get a timeseries pull (bounded universe)
    assert set(calls) == {1, 2, 3}
    assert "CheapJunk" not in names and "ThinPricey" not in names
    # the volatile item clears the pre-filter but is rejected on σ
    assert "Volatile" not in names
    # riser and bleeder survive; riser ranks first (higher Sharpe)
    assert names == ["SteadyRiser", "Bleeder"]


def test_worth_holding_flag(monkeypatch):
    mapping, latest, hr, _ = _wire(monkeypatch)
    rows = {r["name"]: r for r in treasury.rank_stores(mapping, latest, hr, top=10)}
    assert rows["SteadyRiser"]["worth_holding"] is True    # μ>0, low σ → utility>0, beats cash
    assert rows["SteadyRiser"]["mu"] > 0 and rows["SteadyRiser"]["sharpe"] > 0
    assert rows["Bleeder"]["worth_holding"] is False        # μ<0 → utility<0, worse than cash
    assert rows["Bleeder"]["mu"] < 0

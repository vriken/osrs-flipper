"""Quote optimiser: fill rates must move the right way and the frontier must be Pareto."""

from osrs_flipper import api, quote
from osrs_flipper.quote import _frontier, _rates

BARS = [
    {"avgLowPrice": 96, "lowPriceVolume": 100, "avgHighPrice": 100, "highPriceVolume": 50},
    {"avgLowPrice": 97, "lowPriceVolume": 200, "avgHighPrice": 99, "highPriceVolume": 80},
    {"avgLowPrice": 98, "lowPriceVolume": 300, "avgHighPrice": 101, "highPriceVolume": 60},
]


def test_robust_price_ignores_single_bar_glitch():
    from osrs_flipper.quote import _robust
    bars = [{"avgLowPrice": 40}, {"avgLowPrice": 40}, {"avgLowPrice": 39},
            {"avgLowPrice": 41}, {"avgLowPrice": 10}]  # last bar is a glitch
    assert _robust(bars, "avgLowPrice") == 40  # median, not the stray 10


def test_buy_rate_increases_with_price():
    rate_buy, _ = _rates(BARS, window_h=3)
    assert rate_buy(98) > rate_buy(96)  # a higher buy price qualifies more sell volume → faster fill
    assert rate_buy(95) == 0  # nobody sold that low


def test_sell_rate_increases_as_price_drops():
    _, rate_sell = _rates(BARS, window_h=3)
    assert rate_sell(99) > rate_sell(101)  # undercutting fills faster
    assert rate_sell(102) == 0  # nobody bought that high


def test_patient_target_fill_bids_below_the_live_bid_for_a_fatter_margin(monkeypatch):
    # sellers have recently dumped at 98, but the live bid is 100. Eager can't bid below the live
    # bid (fills fast at 100); patient bids 98 — still fills within 8h, fatter margin.
    bars = [{"avgLowPrice": 98, "lowPriceVolume": 5000, "avgHighPrice": 110, "highPriceVolume": 100000}
            for _ in range(72)]
    monkeypatch.setattr(quote.api, "timeseries", lambda *a, **k: bars)
    monkeypatch.setattr(quote.api, "latest", lambda: {1: {"low": 100, "high": 110}})
    monkeypatch.setattr(quote.api, "one_hour", lambda: {1: {"avgLowPrice": 100, "avgHighPrice": 110}})
    eager = quote.optimal_quote(1, 1000, horizon_h=8.0)
    patient = quote.optimal_quote(1, 1000, horizon_h=8.0, target_fill_h=8.0)
    assert eager and patient
    assert patient.buy_px < eager.buy_px           # patient bids below the live bid (eager can't)
    assert patient.net_unit > eager.net_unit       # …for a fatter margin
    assert patient.t_buy_h <= 8.0 + 1e-6           # …while still filling within the window


def test_frontier_is_one_rung_per_margin_level():
    results = [
        {"net_unit": 1, "ev": 100},
        {"net_unit": 1, "ev": 150},  # better EV at the same margin → this one wins
        {"net_unit": 2, "ev": 80},
        {"net_unit": 3, "ev": 200},
    ]
    f = _frontier(results)
    assert [r["net_unit"] for r in f] == [1, 2, 3]  # one per net, sorted by margin
    assert f[0]["ev"] == 150  # max EV kept at net=1


def _declining(monkeypatch, top, live_low, live_high):
    # 14 bars declining from `top` down to the live book, so recent bars carry volume at the live
    # price level (the fill-rate model needs history near the quoted prices).
    bars = [{"avgLowPrice": round(top - k * (top - live_low) / 13),
             "avgHighPrice": round((top + 10) - k * ((top + 10) - live_high) / 13),
             "lowPriceVolume": 500, "highPriceVolume": 500} for k in range(14)]
    monkeypatch.setattr(api, "timeseries", lambda i, s=None: bars)
    monkeypatch.setattr(api, "latest", lambda: {1: {"low": live_low, "high": live_high}})
    monkeypatch.setattr(api, "one_hour", lambda: {1: {"avgLowPrice": live_low, "avgHighPrice": live_high,
                                                      "lowPriceVolume": 500, "highPriceVolume": 500}})


def test_quote_anchors_to_live_book_not_lagging_median(monkeypatch):
    # recent median ~375 but the market has fallen to ~355 (gap under the guard). The quote must price
    # off the LIVE book, not the stale-high median — never a buy above the live ask.
    _declining(monkeypatch, 400, 350, 360)
    q = quote.optimal_quote(1, 1, name="X")
    assert q is not None
    assert 350 <= q.buy_px < 360 and 350 < q.sell_px <= 360   # inside the live book, not ~375
    assert q.buy_px <= q.ask                                   # never quote a buy above the live ask


def test_quote_rejects_when_live_far_from_recent_median(monkeypatch):
    # steep drop 500→350: the recent median (~425) sits >15% above the live book → mid-swing, no
    # trustworthy price → no quote (even though volume exists near the live level).
    _declining(monkeypatch, 500, 350, 360)
    assert quote.optimal_quote(1, 1, name="X") is None

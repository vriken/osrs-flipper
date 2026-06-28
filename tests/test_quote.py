"""Quote optimiser: fill rates must move the right way and the frontier must be Pareto."""

from osrs_flipper.quote import _frontier, _rates

BARS = [
    {"avgLowPrice": 96, "lowPriceVolume": 100, "avgHighPrice": 100, "highPriceVolume": 50},
    {"avgLowPrice": 97, "lowPriceVolume": 200, "avgHighPrice": 99, "highPriceVolume": 80},
    {"avgLowPrice": 98, "lowPriceVolume": 300, "avgHighPrice": 101, "highPriceVolume": 60},
]


def test_buy_rate_increases_with_price():
    rate_buy, _ = _rates(BARS, window_h=3)
    assert rate_buy(98) > rate_buy(96)  # a higher buy price qualifies more sell volume → faster fill
    assert rate_buy(95) == 0  # nobody sold that low


def test_sell_rate_increases_as_price_drops():
    _, rate_sell = _rates(BARS, window_h=3)
    assert rate_sell(99) > rate_sell(101)  # undercutting fills faster
    assert rate_sell(102) == 0  # nobody bought that high


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

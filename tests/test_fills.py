"""The fill model is the credibility-critical part — test its conservatism."""

from osrs_flipper.fills import (
    capacity_units,
    completion_probability,
    fill_units,
    haircut_prices,
)


def test_haircut_keeps_you_inside_the_spread():
    buy, sell = haircut_prices(110, 100, beta=0.25)
    assert 100 < buy < sell < 110  # you don't get the full spread for free


def test_haircut_collapses_penny_spread():
    # a 1gp spread leaves essentially nothing after the haircut + integer rounding
    buy, sell = haircut_prices(6, 5, beta=0.25)
    assert sell - buy <= 1


def test_zero_beta_is_the_optimistic_ceiling():
    buy, sell = haircut_prices(110, 100, beta=0.0)
    assert (buy, sell) == (100, 110)  # full spread captured (the fantasy)


def test_fill_units_capped_by_contra_volume():
    assert fill_units(100, 200, gamma=0.15) == 30  # floor(0.15*200)
    assert fill_units(10, 200, gamma=0.15) == 10  # capped by remaining target
    assert fill_units(100, 0) == 0  # no volume, no fill


def test_capacity_takes_the_binding_constraint():
    # tiny bankroll binds well before the buy limit
    assert capacity_units(buy_limit=10_000, volume_binding=1_000_000, bankroll=200_000, buy_px=5) \
        == min(10_000, int(0.10 * 1_000_000), 200_000 // 5)
    assert capacity_units(10_000, 1_000_000, 200_000, 5) == 10_000  # limit binds here
    assert capacity_units(10_000, 1_000_000, 1_000, 5) == 200  # capital binds here


def test_completion_probability_bounded_and_monotonic():
    low = completion_probability(1000, 500, 500)
    high = completion_probability(1000, 50_000, 50_000)
    assert 0.0 <= low <= high <= 1.0

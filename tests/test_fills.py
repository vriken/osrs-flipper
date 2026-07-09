"""The fill model is the credibility-critical part — test its conservatism."""

from osrs_flipper.fills import (
    capacity_units,
    completion_probability,
    fill_units,
    haircut_prices,
    hung_leg_mult,
    leg_fill_prob,
    market_impact_mult,
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


def test_leg_fill_prob_is_the_shared_linear_capped_shape():
    # fraction of the target cleared by α·rate over the horizon, capped at 1 — the same shape the
    # quote uses, so snapshot ordering agrees with the deep re-price.
    assert leg_fill_prob(1000, 500, 2.0, capture=0.1) == 0.1      # 0.1·500·2/1000
    assert leg_fill_prob(1000, 50_000, 2.0, capture=0.1) == 1.0   # saturates (capped at 1)
    assert leg_fill_prob(0, 500, 2.0) == 0.0                       # no target
    assert leg_fill_prob(1000, 0, 2.0) == 0.0                      # no rate
    assert leg_fill_prob(1000, 500, 0) == 0.0                      # no time


def test_completion_probability_bounded_and_monotonic():
    low = completion_probability(1000, 500, 500)
    high = completion_probability(1000, 50_000, 50_000)
    assert 0.0 <= low <= high <= 1.0


def test_market_impact_is_penalty_only_and_monotone():
    assert market_impact_mult(0, 1000) == 1.0        # no position → no penalty
    assert market_impact_mult(100, 0) == 1.0         # no volume estimate → neutral, never punish blindly
    small = market_impact_mult(10, 100_000)          # a sip of the flow
    big = market_impact_mult(20_000, 100_000)        # 20% of the flow
    assert small > big                                # a bigger share of volume → a bigger haircut
    assert 0.0 < big < 1.0 and small <= 1.0
    # never docks below the floor, and k=0 disables it entirely
    assert market_impact_mult(10**9, 1, k=1.0, floor=0.5) == 0.5
    assert market_impact_mult(10**9, 1, k=0.0) == 1.0


def test_hung_leg_penalises_thin_shaky_spares_fat_reliable():
    fat = hung_leg_mult(p_sell=0.9, margin_pct=0.10)     # reliable sell, fat margin
    thin = hung_leg_mult(p_sell=0.5, margin_pct=0.02)    # shaky sell, thin margin
    assert fat > thin
    assert fat > 0.98                                     # fat + reliable → barely touched
    assert thin <= 0.5 + 1e-9                             # thin + shaky → floored (heavy penalty)
    assert hung_leg_mult(0.9, 0.05) > hung_leg_mult(0.6, 0.05)   # more reliable sell → smaller penalty
    assert hung_leg_mult(0.7, 0.10) > hung_leg_mult(0.7, 0.03)   # fatter margin → smaller penalty
    assert hung_leg_mult(0.5, 0.02, frac=0.0) == 1.0      # disabled
    assert hung_leg_mult(0.0, 0.05) == 1.0                # no sell-prob info → neutral

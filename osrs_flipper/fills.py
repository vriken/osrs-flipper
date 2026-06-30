"""Fill model — the part that separates a real backtest from a fantasy generator.

A flip is two limit orders (buy at the bid, sell at the ask). The naive assumption
that every buy fills at avgLow and every sell fills at avgHigh hands you the full
spread for free and assumes 100% fills — that overstates returns 3–10x. This module
provides the conservative primitives used by both the live scanner (forward EV) and
the backtest replay:

  - haircut_prices: you transact INSIDE the spread, not at its favourable extreme.
    With integer-gp rounding this alone collapses penny-spread "opportunities".
  - fill_units: per bar you capture only a fraction (γ) of the contra-side volume.
  - completion_probability: chance both legs fully fill within the holding window.
"""

from __future__ import annotations

import math

from .config import ALPHA, BETA, GAMMA


def haircut_prices(avg_high: float, avg_low: float, beta: float = BETA) -> tuple[int, int]:
    """Realistic (buy, sell) fill prices: post inside the spread by β·spread each side.

    Returns integer gp (GE prices are whole numbers). On a 1gp spread the haircut
    rounds buy and sell together, so the margin honestly collapses to ~0.
    """
    spread = avg_high - avg_low
    buy_px = round(avg_low + beta * spread)
    sell_px = round(avg_high - beta * spread)
    return int(buy_px), int(sell_px)


def q_aggression(beta: float = BETA) -> float:
    """Price-aggression multiplier coupled to the haircut: more haircut → fills faster."""
    return min(1.0, 0.4 + beta)


def fill_units(remaining: int, contra_volume: float, gamma: float = GAMMA) -> int:
    """Units filled in one bar: capped by your remaining target and your share (γ) of
    the contra-side volume that traded in that bar."""
    if remaining <= 0 or contra_volume <= 0:
        return 0
    return min(remaining, int(math.floor(gamma * contra_volume)))


def leg_completion_probability(
    units_target: int, contra_volume_window: float, *, gamma: float = GAMMA, beta: float = BETA
) -> float:
    """Probability one leg fully fills within the window, given target size vs available
    contra-volume. A smooth saturating function of how much headroom γ·volume has over
    the target, scaled by price aggression. Forward-EV approximation for the scanner.
    """
    if units_target <= 0:
        return 0.0
    if contra_volume_window <= 0:
        return 0.0
    headroom = (gamma * contra_volume_window) / units_target
    # saturating: headroom>=1 → near-certain; headroom<<1 → unlikely
    p = 1.0 - math.exp(-headroom)
    return min(1.0, q_aggression(beta) * p)


def completion_probability(
    units_target: int, buy_volume_window: float, sell_volume_window: float,
    *, gamma: float = GAMMA, beta: float = BETA,
) -> float:
    """P(both legs fill) = P(buy) · P(sell). The buy leg fills against instant-sell
    volume (low side); the sell leg against instant-buy volume (high side)."""
    p_buy = leg_completion_probability(units_target, buy_volume_window, gamma=gamma, beta=beta)
    p_sell = leg_completion_probability(units_target, sell_volume_window, gamma=gamma, beta=beta)
    return p_buy * p_sell


def capacity_units(buy_limit: int, volume_binding: float, bankroll: int, buy_px: int,
                   *, alpha: float = ALPHA, liquidity_floor: int = 0) -> int:
    """Units you can realistically commit to a flip: the min of the legal buy limit,
    a passive share (α) of market volume, and what your bankroll affords.

    `liquidity_floor` lifts the volume-share cap to ≥ that many units for big-ticket gear, where
    α·volume floors to 0 (a few trades/hour) yet the position is genuinely fillable, just slowly —
    so fill ETA, not a units floor, decides if it's worth a slot. Buy-limit and bankroll still bind.
    """
    liq = int(math.floor(alpha * volume_binding)) if volume_binding > 0 else 0
    caps = [
        buy_limit if buy_limit else 0,
        max(liq, liquidity_floor),
        bankroll // buy_px if buy_px > 0 else 0,
    ]
    return max(0, min(caps))

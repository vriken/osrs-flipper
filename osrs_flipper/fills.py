"""Fill model — the part that separates a real backtest from a fantasy generator.

A flip is two limit orders (buy at the bid, sell at the ask). The naive assumption
that every buy fills at avgLow and every sell fills at avgHigh hands you the full
spread for free and assumes 100% fills — that overstates returns 3–10x. This module
provides the conservative primitives used by both the live scanner (forward EV) and
the backtest replay:

  - haircut_prices: you transact INSIDE the spread, not at its favourable extreme.
    With integer-gp rounding this alone collapses penny-spread "opportunities".
  - fill_units: per bar you capture only a fraction (γ) of the contra-side volume.
  - leg_fill_prob / completion_probability: chance a leg (and both legs) fully fill within the
    horizon — ONE shape shared with the live quote, so snapshot ranking tracks the deep re-price.
"""

from __future__ import annotations

import math

from .config import (
    ALPHA,
    BETA,
    GAMMA,
    HUNG_LEG_COST_FRAC,
    HUNG_LEG_FLOOR,
    IMPACT_FLOOR,
    IMPACT_K,
    SNAPSHOT_HORIZON_H,
)


def haircut_prices(avg_high: float, avg_low: float, beta: float = BETA) -> tuple[int, int]:
    """Realistic (buy, sell) fill prices: post inside the spread by β·spread each side.

    Returns integer gp (GE prices are whole numbers). On a 1gp spread the haircut
    rounds buy and sell together, so the margin honestly collapses to ~0.
    """
    spread = avg_high - avg_low
    buy_px = round(avg_low + beta * spread)
    sell_px = round(avg_high - beta * spread)
    return int(buy_px), int(sell_px)


def leg_fill_prob(target_units: int, rate_per_h: float, horizon_h: float, *, capture: float = ALPHA) -> float:
    """Probability one leg fully fills within `horizon_h`: the fraction of the target that a passive
    capture (α) of the contra-side fill RATE (units/hour) clears in that time, capped at 1.

    The SINGLE leg shape shared by the live quote and the snapshot scanner, so candidate ordering
    agrees with the deep re-price. The quote feeds a price-specific rate from /timeseries; the snapshot
    feeds the 1h contra volume as the rate proxy — same function, richer data on the deep path."""
    if target_units <= 0 or rate_per_h <= 0 or horizon_h <= 0:
        return 0.0
    return min(1.0, capture * rate_per_h * horizon_h / target_units)


def fill_units(remaining: int, contra_volume: float, gamma: float = GAMMA) -> int:
    """Units filled in one bar: capped by your remaining target and your share (γ) of
    the contra-side volume that traded in that bar."""
    if remaining <= 0 or contra_volume <= 0:
        return 0
    return min(remaining, int(math.floor(gamma * contra_volume)))


def completion_probability(
    units_target: int, buy_volume_window: float, sell_volume_window: float,
    *, horizon_h: float = SNAPSHOT_HORIZON_H, capture: float = ALPHA,
) -> float:
    """P(both legs fill) = P(buy) · P(sell), via the shared `leg_fill_prob` shape. The buy leg fills
    against instant-sell volume (low side); the sell leg against instant-buy volume (high side), each
    treated as a units/hour rate over a representative snapshot horizon. Matches the quote's leg model
    so the snapshot ranking tracks the deep re-price instead of a different, harsher curve."""
    p_buy = leg_fill_prob(units_target, buy_volume_window, horizon_h, capture=capture)
    p_sell = leg_fill_prob(units_target, sell_volume_window, horizon_h, capture=capture)
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


def market_impact_mult(target_units: int, vol_binding: float, *,
                       k: float = IMPACT_K, floor: float = IMPACT_FLOOR) -> float:
    """EV haircut for PRICE impact: your resting order claims a share p = target/contra-volume of
    what trades; taking a large share walks the price against you, shrinking the realized margin.
    Multiplicative, penalty-only, monotone in p:  mult = 1 / (1 + k·p),  floored.

    ≈1 when your size is a sip of the flow (a small bankroll-bound stack, or a deep commodity);
    bites as the position becomes a large fraction of volume (a big stack pushing its full α-share,
    or a thin item). Fill-probability degradation is modelled separately (completion_probability);
    this is the distinct price-impact term. k=0 disables it."""
    if k <= 0 or vol_binding <= 0 or target_units <= 0:
        return 1.0
    p = target_units / vol_binding
    return max(floor, 1.0 / (1.0 + k * p))


def hung_leg_mult(p_sell: float, margin_pct: float, *,
                  frac: float = HUNG_LEG_COST_FRAC, floor: float = HUNG_LEG_FLOOR) -> float:
    """EV haircut for a HUNG BUY LEG: you fill the buy, then the sell fails to complete within the
    window, trapping capital in inventory you must grind out (opportunity cost + likely markdown).
    EV = qty·net·p_round scores that state as zero; this restores its expected (negative) contribution.

    Derived as (EV_gross − expected_hung_cost)/EV_gross, which cancels the position size and p_buy to
    a clean multiplier needing only the sell-fill probability and the return rate:
        mult = 1 − frac · (1−p_sell)/p_sell · (1/margin_pct),  floored.
    So a shaky sell leg (low p_sell) and/or a thin margin (small margin_pct) — exactly the flips a
    trapped buy wipes — are penalised hard, while a fat-margin flip with a reliable sell is barely
    touched. Because p_sell is horizon-aware upstream, a patient quote is penalised less. frac=0 off."""
    if frac <= 0 or margin_pct <= 0 or p_sell <= 0:
        return 1.0
    cost_ratio = frac * (1.0 - min(1.0, p_sell)) / p_sell / margin_pct
    return max(floor, 1.0 - cost_ratio)

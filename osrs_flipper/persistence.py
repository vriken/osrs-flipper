"""Spread-persistence: is an item's spread real and capturable, or just integer churn?

A single snapshot can't tell Jug of water (a stable 2gp spread) from Earth rune (bid
and ask whipsawing 4↔6 on bot volume). This deep-checks a candidate's recent history:

  realizable_spread = median(spread) − median(|bar-to-bar mid move|)

If the mid jumps around more than the spread is wide, you get adversely picked off
between buying and selling — the spread isn't really capturable. We also require the
spread to actually exist in most recent bars (persist fraction).
"""

from __future__ import annotations

import statistics
from typing import Any

from . import api, config
from .tax import post_tax_received


def spread_stats(highs: list[float | None], lows: list[float | None]) -> dict[str, Any] | None:
    """Compute persistence stats from aligned high/low series. None if too little data."""
    pairs = [(h, low) for h, low in zip(highs, lows, strict=False) if h is not None and low is not None]
    if len(pairs) < config.PERSIST_MIN_BARS:
        return None
    spreads = [h - low for h, low in pairs]
    mids = [(h + low) / 2 for h, low in pairs]
    mid_moves = [abs(mids[i] - mids[i - 1]) for i in range(1, len(mids))]

    med_spread = statistics.median(spreads)
    mid_vol = statistics.median(mid_moves) if mid_moves else 0.0
    realizable = med_spread - mid_vol
    persist = sum(1 for s in spreads if s > 0) / len(spreads)
    # factor in [0,1]: how much of the spread survives the mid noise
    factor = max(0.0, realizable / med_spread) if med_spread > 0 else 0.0
    return {
        "med_spread": med_spread,
        "mid_vol": mid_vol,
        "realizable_spread": realizable,
        "persist": persist,
        "persist_factor": min(1.0, factor),
        "n_bars": len(pairs),
    }


def fetch_persistence(item_id: int, timestep: str = config.PERSIST_TIMESTEP) -> dict[str, Any] | None:
    """Pull recent /timeseries for one item and compute its spread persistence."""
    pts = api.timeseries(item_id, timestep)
    if not pts:
        return None
    highs = [p.get("avgHighPrice") for p in pts]
    lows = [p.get("avgLowPrice") for p in pts]
    return spread_stats(highs, lows)


def reliability_stats(bars: list[dict], item_id: int, quoted_net: float, *,
                      n_bars: int | None = None) -> dict[str, Any]:
    """Fine-grained margin-decay check the 1h/2-day `spread_stats` can't see: over the last few 5m
    bars, how often was the ACHIEVABLE post-tax margin at least a fraction of what we're quoting?

    A 1h bar averages away an intraday collapse, so a heavily-flipped tight item passes `spread_stats`
    yet its margin evaporates minutes after you place. Here each 5m bar's net = post_tax(avgHigh)−avgLow
    is compared to a threshold (RELIAB_RATIO × quoted_net, floored at RELIAB_MIN_NET); `uptime` is the
    fraction of valid bars that clear it. The multiplier is penalty-only and floored (de-ranks a
    fleeting-spread item without banning it — it keeps getting shots so a recovery is noticed).

    Returns neutral (mult 1.0) when there's too little data to judge — never punishes on thin history."""
    n = n_bars or config.RELIAB_BARS
    recent = bars[-n:] if n else bars
    nets = [post_tax_received(int(h), item_id=item_id) - low
            for b in recent
            if (h := b.get("avgHighPrice")) is not None and (low := b.get("avgLowPrice")) is not None]
    if len(nets) < config.RELIAB_MIN_BARS:                       # too little to judge → neutral, no penalty
        return {"uptime": 1.0, "gone_frac": 0.0, "reliab_mult": 1.0, "n_bars": len(nets), "thin": True}
    thresh = max(config.RELIAB_MIN_NET, config.RELIAB_RATIO * quoted_net)
    uptime = sum(1 for net in nets if net >= thresh) / len(nets)
    gone_frac = sum(1 for net in nets if net <= 0) / len(nets)
    reliab_mult = config.RELIAB_FLOOR + (1.0 - config.RELIAB_FLOOR) * uptime   # penalty-only, floored
    return {"uptime": uptime, "gone_frac": gone_frac, "reliab_mult": reliab_mult,
            "n_bars": len(nets), "thin": False}


def fetch_reliability(item_id: int, quoted_net: float,
                      timestep: str = config.RELIAB_TIMESTEP) -> dict[str, Any] | None:
    """Pull recent fine-grained /timeseries for one item and score its margin reliability vs `quoted_net`."""
    pts = api.timeseries(item_id, timestep)
    if not pts:
        return None
    return reliability_stats(pts, item_id, quoted_net)

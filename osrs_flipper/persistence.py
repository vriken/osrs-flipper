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

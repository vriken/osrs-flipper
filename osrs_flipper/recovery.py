"""Recovery hold: for an underwater holding, read its past ~week to judge whether the dip is
likely to bounce (hold, maybe double down to lower the average) or is a re-rating to cut.

This is a mean-reversion read, NOT a guarantee — a dip can be a permanent markdown (a nerf, a
supply change, a fading fad). So the bar to recommend holding/doubling is deliberately conjunctive:
the item must have traded above your cost this week, be statistically low right now, AND not be in
a steady week-long decline. The double-down target is a level the item *actually reached* this
week, so the implied break-even is reachable rather than wishful.
"""

from __future__ import annotations

import statistics

from . import config


def week_mids(bars: list[dict], lookback: int | None = None) -> list[float]:
    """Mid prices from /timeseries bars, last `lookback` (≈1 week of 1h bars)."""
    lookback = config.RECOVERY_LOOKBACK_BARS if lookback is None else lookback
    mids = [(b["avgHighPrice"] + b["avgLowPrice"]) / 2 for b in bars
            if b.get("avgHighPrice") is not None and b.get("avgLowPrice") is not None]
    return mids[-lookback:]


def assess_recovery(avg_cost: float, bail: float, mids: list[float], *, min_dip: float | None = None,
                    min_green: float | None = None, z_thresh: float | None = None) -> dict | None:
    """Judge an underwater holding. `bail` = post-tax instant-sell value now. Returns None if
    there's too little week history. `recover` is True only when it's underwater AND traded above
    your cost this week AND is statistically low now AND isn't in a steady decline."""
    min_dip = config.RECOVERY_MIN_DIP if min_dip is None else min_dip
    min_green = config.RECOVERY_MIN_GREEN if min_green is None else min_green
    z_thresh = config.RECOVERY_Z if z_thresh is None else z_thresh
    if len(mids) < 12:
        return None
    mean, med, hi = statistics.mean(mids), statistics.median(mids), max(mids)
    std = statistics.pstdev(mids) or 1.0
    cur = mids[-1]
    z = (cur - mean) / std
    pct_below_med = (med - cur) / med if med else 0.0
    half = len(mids) // 2
    early, late = statistics.median(mids[:half]), statistics.median(mids[half:])
    rerating = late < early * (1 - min_dip)              # second half meaningfully below first → downtrend
    was_green = hi >= avg_cost * (1 + min_green)         # traded ≥ a few % above your cost this week
    underwater = bail < avg_cost
    depressed = z <= z_thresh or pct_below_med >= min_dip
    return {"mean": mean, "median": med, "high": hi, "cur": cur, "z": z,
            "pct_below_median": pct_below_med, "was_green": was_green, "rerating": rerating,
            "underwater": underwater, "depressed": depressed,
            "recover": underwater and was_green and depressed and not rerating}


def double_down(held_qty: int, avg_cost: float, cur: float, target: float) -> tuple[int, float]:
    """Units to buy at `cur` to blend the holding's average down to `target` (a reachable bounce
    level). Returns (qty, new_avg); qty is 0 if cur ≥ target (can't average down by buying higher)."""
    if held_qty <= 0 or cur >= target:
        return 0, avg_cost
    q = max(0, round(held_qty * (avg_cost - target) / (target - cur)))
    new_avg = (held_qty * avg_cost + q * cur) / (held_qty + q) if (held_qty + q) else avg_cost
    return q, new_avg

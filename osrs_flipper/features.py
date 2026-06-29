"""Per-item feature computation for the live scanner.

Joins /latest (current bid/ask + trade timestamps), /1h (averaged prices + volume),
and /mapping (buy limit, members, name) into one DataFrame, then derives margins
(naive vs fill-model), capacity, completion probability, and the liquidity/staleness
gates that keep stale "ghost" items off the ranking.
"""

from __future__ import annotations

import math
import time
from typing import Any

import pandas as pd

from . import config
from .fills import capacity_units, completion_probability, haircut_prices
from .tax import post_tax_received


def build_features(
    latest: dict[int, dict[str, Any]],
    hourly: dict[int, dict[str, Any]],
    mapping: list[dict[str, Any]],
    *,
    bankroll: int | None = None,
    now_ts: int | None = None,
    limit_used: dict[int, int] | None = None,
) -> pd.DataFrame:
    """Return a feature DataFrame (one row per item that has a /mapping entry).

    `limit_used` maps item_id -> units already bought in the rolling 4h window; the
    effective buy limit is reduced accordingly so maxed-out items drop out.
    """
    bankroll = config.BANKROLL if bankroll is None else bankroll
    now_ts = int(time.time()) if now_ts is None else now_ts
    limit_used = limit_used or {}
    rows: list[dict[str, Any]] = []

    for meta in mapping:
        iid = meta["id"]
        lp = latest.get(iid)
        hp = hourly.get(iid)
        if not lp or not hp:
            continue

        high, low = lp.get("high"), lp.get("low")
        if high is not None and low is not None and low > high:
            continue  # crossed/inverted live book (bid > ask) → prices unreliable, skip
        avg_high, avg_low = hp.get("avgHighPrice"), hp.get("avgLowPrice")
        high_vol = hp.get("highPriceVolume") or 0
        low_vol = hp.get("lowPriceVolume") or 0

        # Prefer the hourly average for stable feature math; fall back to latest.
        ah = avg_high if avg_high is not None else high
        al = avg_low if avg_low is not None else low
        if ah is None or al is None or al <= 0:
            continue

        spread = ah - al
        mid = (ah + al) / 2
        rel_spread = spread / mid if mid else 0.0
        vol_binding = min(high_vol, low_vol)

        buy_px, sell_px = haircut_prices(ah, al)
        margin_abs = post_tax_received(sell_px, item_id=iid) - buy_px
        margin_pct = margin_abs / buy_px if buy_px else 0.0
        # Naive (no haircut) for comparison — uses live bid/ask.
        n_buy, n_sell = (low, high) if (low is not None and high is not None) else (al, ah)
        margin_abs_naive = post_tax_received(int(n_sell), item_id=iid) - int(n_buy)

        buy_limit = meta.get("limit") or 0
        buy_limit_eff = max(0, buy_limit - limit_used.get(iid, 0))  # remaining 4h buy-limit room
        cap = capacity_units(buy_limit_eff, vol_binding, bankroll, buy_px)
        p_complete = completion_probability(cap, low_vol, high_vol) if cap > 0 else 0.0
        exp_gp_cycle = margin_abs * cap * p_complete

        # estimated real-time hours to fill both legs at a passive share (α) of volume —
        # the throughput constraint for a time-limited trader
        buy_rate = config.ALPHA * low_vol
        sell_rate = config.ALPHA * high_vol
        fill_eta_h = (cap / buy_rate if buy_rate > 0 else math.inf) + \
                     (cap / sell_rate if sell_rate > 0 else math.inf)
        gp_per_hour = exp_gp_cycle / fill_eta_h if math.isfinite(fill_eta_h) and fill_eta_h > 0 else 0.0

        # queue-jump (fast-fill) margin: buy at bid+1, sell at ask-1 — what you actually
        # net if you refuse to wait. Penny spreads go ≤0 here; that's the point for online.
        if high is not None and low is not None:
            fast_buy, fast_sell = low + 1, high - 1
            margin_fast = post_tax_received(fast_sell, item_id=iid) - fast_buy
        else:
            fast_buy, fast_sell, margin_fast = buy_px, sell_px, margin_abs
        exp_gp_cycle_fast = margin_fast * cap * p_complete

        age_low = now_ts - lp["lowTime"] if lp.get("lowTime") else math.inf
        age_high = now_ts - lp["highTime"] if lp.get("highTime") else math.inf
        staleness = max(age_low, age_high)
        liq_score = vol_binding * math.exp(-staleness / config.TAU_S) if math.isfinite(staleness) else 0.0

        # which cap is binding — tells the trader why qty is what it is
        cap_candidates = {
            "limit": buy_limit,
            "liquidity": int(math.floor(config.ALPHA * vol_binding)),
            "capital": bankroll // buy_px if buy_px else 0,
        }
        bound_by = min(cap_candidates, key=cap_candidates.get) if cap > 0 else "none"

        rows.append({
            "item_id": iid,
            "name": meta.get("name"),
            "members": bool(meta.get("members")),
            "buy_limit": buy_limit,
            "buy_px": buy_px,
            "sell_px": sell_px,
            "margin_abs": margin_abs,
            "margin_pct": margin_pct,
            "margin_abs_naive": margin_abs_naive,
            "spread": spread,
            "rel_spread": rel_spread,
            "vol_1h_binding": vol_binding,
            "capacity": cap,
            "capital_deployed": cap * buy_px,  # how much of your pile this flip ties up
            "buy_limit_eff": buy_limit_eff,  # remaining buy-limit room (4h)
            "liq_units": min(buy_limit_eff, int(math.floor(config.ALPHA * vol_binding))),  # cash-independent absorb cap
            "bound_by": bound_by,
            "p_complete": p_complete,
            "exp_gp_cycle": exp_gp_cycle,
            "buy_rate": buy_rate,  # units/hr you can passively buy (for buy-leg ETA ordering)
            "fast_buy": fast_buy,
            "fast_sell": fast_sell,
            "margin_fast": margin_fast,
            "exp_gp_cycle_fast": exp_gp_cycle_fast,
            "fill_eta_h": fill_eta_h if math.isfinite(fill_eta_h) else None,
            "gp_per_hour": gp_per_hour,
            "staleness_s": staleness if math.isfinite(staleness) else None,
            "liq_score": liq_score,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["tradeable"] = is_tradeable(df, now_ts)
    df["suspect"] = (df["rel_spread"] > 0.10) & (df["vol_1h_binding"] < config.V_SUSPICIOUS_1H)
    return df


def is_tradeable(df: pd.DataFrame, now_ts: int) -> pd.Series:
    """Gate out ghosts: need both live prices, a recent trade, and enough volume."""
    fresh = df["staleness_s"].notna() & (df["staleness_s"] < config.STALENESS_MAX_S)
    liquid = df["vol_1h_binding"] >= config.V_MIN_1H
    return fresh & liquid

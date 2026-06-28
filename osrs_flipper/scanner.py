"""Live scanner: fetch → features → gate → rank by fill-model expected gp/cycle."""

from __future__ import annotations

import pandas as pd

from . import api, config
from .features import build_features
from .persistence import fetch_persistence

# composite score = expected gp/cycle ÷ fill_eta^w, where w is the online↔offline dial
RANK_COL = "score"
MODE_WEIGHTS = {"online": 1.0, "balanced": 0.5, "offline": 0.0}


def _composite(gp_cycle: float, fill_eta_h: float | None, time_weight: float) -> float:
    """EV per unit of the scarce resource: real-time (online) vs GE slot/cycle (offline)."""
    if time_weight <= 0:
        return gp_cycle  # offline: wall-clock is free, only the per-cycle haul matters
    if fill_eta_h and fill_eta_h > 0:
        return gp_cycle / (fill_eta_h ** time_weight)
    return 0.0  # can't estimate fill time and time matters → unrankable


def scan(
    *,
    members: bool | None = None,
    bankroll: int | None = None,
    top: int = 20,
    include_suspect: bool = False,
    persistence: bool = True,
    candidates: int | None = None,
    mode: str = "balanced",
) -> pd.DataFrame:
    """Return the top ranked flips by the mode-weighted composite score.

    score = (margin × capacity × P(complete) × persist) / fill_eta^w, with w set by
    `mode` (online=1, balanced=0.5, offline=0). Stale/illiquid/penny-churn traps are
    gated out by the tradeable + spread-persistence checks first.
    """
    time_weight = MODE_WEIGHTS.get(mode, 0.5)
    df = build_features(api.latest(), api.one_hour(), api.mapping(), bankroll=bankroll)
    if df.empty:
        return df

    members = config.MEMBERS if members is None else members
    if not members:
        df = df[~df["members"]]

    df = df[df["tradeable"] & (df["margin_abs"] > 0) & (df["capacity"] > 0)]
    if not include_suspect:
        df = df[~df["suspect"]]
    if df.empty:
        return df

    # online = fill NOW, which means queue-jumping (buy bid+1 / sell ask-1). Score on that
    # fast-net margin so penny spreads (which go ≤0 when jumped) correctly sink.
    online = mode == "online"
    base_col = "exp_gp_cycle_fast" if online else "exp_gp_cycle"
    if online:
        df = df.assign(
            buy_px=df["fast_buy"], sell_px=df["fast_sell"], margin_abs=df["margin_fast"],
            margin_pct=df["margin_fast"] / df["fast_buy"].where(df["fast_buy"] > 0, 1),
        )

    df["score"] = [_composite(c, e, time_weight) for c, e in zip(df[base_col], df["fill_eta_h"], strict=False)]
    df = df[df["score"] > 0]
    if df.empty:
        return df
    df = df.sort_values(RANK_COL, ascending=False).reset_index(drop=True)
    if not persistence:
        return df.head(top)

    return _apply_persistence(df, candidates or config.PERSIST_CANDIDATES, time_weight, base_col).head(top).reset_index(drop=True)


def _apply_persistence(df: pd.DataFrame, candidates: int, time_weight: float, base_col: str) -> pd.DataFrame:
    """Deep-check the top snapshot candidates and re-score with the spread-stability factor."""
    pool = df.head(candidates).copy()
    stats = [fetch_persistence(int(iid)) for iid in pool["item_id"]]
    pool["persist"] = [s["persist"] if s else None for s in stats]
    pool["realizable_spread"] = [s["realizable_spread"] if s else None for s in stats]
    pool["persist_factor"] = [s["persist_factor"] if s else 0.0 for s in stats]
    pool["exp_gp_cycle_adj"] = pool[base_col] * pool["persist_factor"]
    pool["score"] = [_composite(c, e, time_weight)
                     for c, e in zip(pool["exp_gp_cycle_adj"], pool["fill_eta_h"], strict=False)]

    keep = (
        pool["realizable_spread"].notna()
        & (pool["realizable_spread"] > 0)
        & (pool["persist"] >= config.PERSIST_MIN_FRAC)
        & (pool["score"] > 0)
    )
    return pool[keep].sort_values(RANK_COL, ascending=False)


def bond_progress(bankroll: int | None = None) -> dict[str, float | int | None]:
    """How close the bankroll is to affording a bond (the F2P → members milestone)."""
    bankroll = config.BANKROLL if bankroll is None else bankroll
    bond = api.latest().get(config.BOND_ITEM_ID, {})
    price = bond.get("high")  # what you'd pay to instant-buy a bond
    pct = (bankroll / price * 100) if price else None
    return {"bond_price": price, "bankroll": bankroll, "pct": pct}

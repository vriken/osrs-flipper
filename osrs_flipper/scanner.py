"""Live scanner: fetch → features → gate → rank by fill-model expected gp/cycle."""

from __future__ import annotations

import statistics

import pandas as pd

from . import api, config
from .features import build_features
from .persistence import fetch_persistence

RANK_COL = "score"
# snapshot (--no-persistence) quick path: composite = gp/cycle ÷ fill_eta^w
MODE_WEIGHTS = {"online": 1.0, "balanced": 0.5, "offline": 0.0}
# deep path: mode sets the quote horizon — short = fill-now (online), long = patient (offline)
MODE_HORIZON = {"online": 0.5, "balanced": 2.0, "offline": 8.0}


def _composite(gp_cycle: float, fill_eta_h: float | None, time_weight: float) -> float:
    """EV per unit of the scarce resource: real-time (online) vs GE slot/cycle (offline)."""
    if time_weight <= 0:
        return gp_cycle  # offline: wall-clock is free, only the per-cycle haul matters
    if fill_eta_h and fill_eta_h > 0:
        return gp_cycle / (fill_eta_h ** time_weight)
    return 0.0  # can't estimate fill time and time matters → unrankable


def _shrink(scores: list[float], reliabilities: list[float]) -> list[float]:
    """Shrink each estimate toward the cross-sectional median, scaled by reliability.

    Counters the optimizer's curse: ranking by estimated EV systematically surfaces the
    most upward-biased estimates at the top. Low-reliability picks (thin fills, unstable
    spreads) get pulled back hard; reliable ones keep their edge.
    """
    if not scores:
        return []
    med = statistics.median(scores)
    return [med + (s - med) * r for s, r in zip(scores, reliabilities, strict=False)]


def scan(
    *,
    members: bool | None = None,
    bankroll: int | None = None,
    top: int = 20,
    include_suspect: bool = False,
    persistence: bool = True,
    candidates: int | None = None,
    mode: str = "balanced",
    min_gp: int = 0,
    limit_used: dict[int, int] | None = None,
) -> pd.DataFrame:
    """Return the top ranked flips by the mode-weighted composite score.

    score = (margin × capacity × P(complete) × persist) / fill_eta^w, with w set by
    `mode` (online=1, balanced=0.5, offline=0). Stale/illiquid/penny-churn traps are
    gated out by the tradeable + spread-persistence checks first.
    """
    time_weight = MODE_WEIGHTS.get(mode, 0.5)
    df = build_features(api.latest(), api.one_hour(), api.mapping(), bankroll=bankroll, limit_used=limit_used)
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
        if min_gp:
            df = df[df[base_col] >= min_gp]
        return df.head(top)

    out = _apply_persistence(df, candidates or config.PERSIST_CANDIDATES, mode)
    if min_gp and not out.empty:
        out = out[out["exp_gp_cycle_adj"] >= min_gp]  # drop flips too small to be worth a slot
    return out.head(top).reset_index(drop=True)


def _apply_persistence(df: pd.DataFrame, candidates: int, mode: str) -> pd.DataFrame:
    """Deep-check the top snapshot candidates: re-price each with the quote optimiser (one
    source of truth — price-specific fills), then shrink the scores against the curse.

    The displayed buy/sell/net/fill all come from the quote here, so the scanner can never
    disagree with `quote <item>` again. Mode sets the quote horizon (online=fast, offline=patient).
    """
    from .quote import optimal_quote

    horizon = MODE_HORIZON.get(mode, 2.0)
    rows = []
    for _, row in df.head(candidates).iterrows():
        iid = int(row["item_id"])
        st = fetch_persistence(iid)
        if not st or st["realizable_spread"] <= 0 or st["persist"] < config.PERSIST_MIN_FRAC:
            continue
        q = optimal_quote(iid, int(row["capacity"]), name=row["name"], horizon_h=horizon)
        if not q or q.ev <= 0:
            continue
        reliability = st["persist_factor"] * min(1.0, q.p_round / 0.5)
        rows.append({
            **row.to_dict(),
            "buy_px": q.buy_px, "sell_px": q.sell_px, "margin_abs": q.net_unit,
            "margin_pct": q.net_unit / q.buy_px if q.buy_px else 0.0,
            "capital_deployed": q.buy_px * int(row["capacity"]),
            "p_complete": q.p_round, "fill_eta_h": q.t_buy_h + q.t_sell_h,
            "persist": st["persist"], "realizable_spread": st["realizable_spread"],
            "exp_gp_cycle_adj": q.ev, "raw_score": q.ev / horizon, "reliability": reliability,
        })
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    out["score"] = _shrink(list(out["raw_score"]), list(out["reliability"]))
    return out.sort_values(RANK_COL, ascending=False)


def _allocate(picks: list[dict], bankroll: float) -> tuple[list[dict], float]:
    """Split cash across picks by FAIR SHARE so no single position soaks the whole pile.

    Each position's budget is its even share of the cash still unallocated, capped by its
    own `cap_units` (fast-fill liquidity for active flips; buy limit for accumulation).
    Underused budget spills forward. Returns (allocated picks, idle cash).
    """
    remaining = float(bankroll)
    out = []
    for i, p in enumerate(picks):
        buy = p["buy_px"]
        slots_left = len(picks) - i
        if buy <= 0 or remaining <= 0 or slots_left <= 0:
            continue
        budget = remaining / slots_left  # fair share of what's left (spillover-aware)
        alloc = min(budget, p["cap_units"] * buy)
        qty = int(alloc // buy)
        if qty <= 0:
            continue
        out.append({**p, "qty": qty, "deploy": qty * buy})
        remaining -= qty * buy
    return out, remaining


def _pick_row(row, tier: str, cap_units: int) -> dict:
    return {
        "tier": tier, "item_id": int(row["item_id"]), "name": row["name"],
        "buy_px": int(row["buy_px"]), "sell_px": int(row["sell_px"]),
        "margin_abs": int(row["margin_abs"]), "p_complete": float(row["p_complete"]),
        "cap_units": max(0, cap_units), "buy_rate": float(row.get("buy_rate", 0.0)),
    }


def _worth_gp(row, cap_units: int, tier: str) -> float:
    """Max gp this position could net at its cap — used to drop slot-wasting trivial flips."""
    fill = float(row["p_complete"]) if tier != "hold" else 1.0
    return int(row["margin_abs"]) * cap_units * fill


def build_portfolio(*, bankroll: int, held_ids=(), free_slots: int, members: bool | None = None,
                    max_accumulate: int = 6, min_gp: int | None = None,
                    min_margin: float = 0.01, limit_used: dict[int, int] | None = None) -> tuple[list[dict], float]:
    """Two-tier capital deployment:
      ACTIVE  — one diversified flip per free slot (online/balanced/offline), capped by
                fast-fill liquidity — what you work in your slots right now.
      HOLD    — extra positions to absorb idle cash into inventory, capped by each item's
                buy limit (you accumulate over 4h cycles; the 3 slots only gate offers).
    Only items whose spread survives a queue-jump (fast_net > 0) qualify, so the pile
    can't pour into penny traps. Returns (allocated picks, idle cash).
    """
    if free_slots <= 0:
        return [], float(bankroll)  # no free slots → can't place any new buys
    # a flip must clear this to be worth a slot + the clicks (≈0.2% of bankroll, floor 250)
    if min_gp is None:
        min_gp = max(250, int(bankroll * 0.002))
    roles = ["online", "balanced", "offline"][:max(0, free_slots)]
    modes = dict.fromkeys([*roles, "balanced"])
    rankings = {m: scan(mode=m, bankroll=bankroll, members=members, top=40, limit_used=limit_used)
                for m in modes}
    taken = {int(i) for i in held_ids}

    def ok(row) -> bool:
        return (int(row["item_id"]) not in taken
                and float(row.get("margin_fast", 1)) > 0  # spread survives a queue-jump
                and float(row["margin_pct"]) >= min_margin)  # return rate worth the capital/risk

    picks: list[dict] = []
    for role in roles:  # active: best worth-it flip for the role
        for _, row in rankings[role].iterrows():
            if ok(row) and _worth_gp(row, int(row["liq_units"]), role) >= min_gp:
                picks.append(_pick_row(row, role, int(row["liq_units"])))
                taken.add(int(row["item_id"]))
                break
    for _, row in rankings["balanced"].iterrows():  # hold: accumulate the rest into inventory
        if sum(p["tier"] == "hold" for p in picks) >= max_accumulate:
            break
        if ok(row) and _worth_gp(row, int(row["buy_limit_eff"]), "hold") >= min_gp:
            picks.append(_pick_row(row, "hold", int(row["buy_limit_eff"])))
            taken.add(int(row["item_id"]))

    allocated, idle = _allocate(picks, bankroll)
    for p in allocated:  # active gp is per-cycle; hold gp is the spread captured once sold
        fill = p["p_complete"] if p["tier"] != "hold" else 1.0
        p["gp"] = p["margin_abs"] * p["qty"] * fill
        br = p.get("buy_rate", 0.0)
        p["buy_eta_h"] = p["qty"] / br if br > 0 else float("inf")
    # placement order: fastest-filling buys first, so slots clear and you can cycle the rest
    allocated.sort(key=lambda p: p["buy_eta_h"])
    _schedule(allocated, free_slots)
    return allocated, idle


def _schedule(picks: list[dict], slots: int) -> None:
    """Annotate each pick with place_at_h / fill_by_h, simulating placement across `slots`
    GE slots: you place `slots` at once; a later pick waits for the earliest slot to free."""
    slot_free = [0.0] * max(1, slots)
    for p in picks:
        k = min(range(len(slot_free)), key=lambda i: slot_free[i])
        eta = p["buy_eta_h"] if p["buy_eta_h"] < 100 else 100.0
        p["place_at_h"] = slot_free[k]
        p["fill_by_h"] = slot_free[k] + eta
        slot_free[k] = p["fill_by_h"]


def bond_progress(bankroll: int | None = None) -> dict[str, float | int | None]:
    """How close the bankroll is to affording a bond (the F2P → members milestone)."""
    bankroll = config.BANKROLL if bankroll is None else bankroll
    bond = api.latest().get(config.BOND_ITEM_ID, {})
    price = bond.get("high")  # what you'd pay to instant-buy a bond
    pct = (bankroll / price * 100) if price else None
    return {"bond_price": price, "bankroll": bankroll, "pct": pct}

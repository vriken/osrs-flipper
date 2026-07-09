"""Live scanner: fetch → features → gate → rank by fill-model expected gp/cycle."""

from __future__ import annotations

import math
import statistics

import pandas as pd

from . import anomaly, api, calibration, config
from .features import build_features
from .persistence import fetch_persistence, fetch_reliability

RANK_COL = "score"
# snapshot (--no-persistence) quick path: composite = gp/cycle ÷ fill_eta^w
MODE_WEIGHTS = {"online": 1.0, "balanced": 0.5, "offline": 0.0}
# Per-mode ROI tilt: active/day modes rank on pure throughput (volume × margin); the overnight
# (offline) mode ranks on margin% so slow capital sits in fat-margin holds worth leaving overnight.
MODE_ROI_WEIGHT = {"online": config.ROI_WEIGHT_FAST, "balanced": config.ROI_WEIGHT_FAST,
                   "offline": config.ROI_WEIGHT_SLOW}
# deep path: mode sets the quote horizon — short = fill-now (online), long = patient (offline)
MODE_HORIZON = {"online": 0.5, "balanced": 2.0, "offline": 8.0}
FAST_MODES = {"online", "balanced"}  # throughput trading — where a minutes-scale margin collapse bites;
#                                      offline/overnight waits through the noise, so skip the 5m penalty


def _mode_roi_weight(mode: str) -> float:
    """ROI tilt for a scan mode: volume/throughput by day (online/balanced), margin% overnight."""
    return MODE_ROI_WEIGHT.get(mode, config.SCORE_ROI_WEIGHT)


def _stack_roi_weight(mode: str, net_worth: int | None) -> float:
    """ROI-tilt exponent as a function of stack size — capital-awareness baked into the ranking, always on.

    A small stack is capital-bound, so it compounds fastest by tilting HARD to ROI% (favour margin%); a
    large stack can't push size through shallow high-ROI items, so the tilt fades to the mode's throughput
    floor. Mode still sets that floor (overnight keeps favouring fat margins). Log-interpolated across
    [ROI_STACK_LO, ROI_STACK_HI] on net worth, since a stack spans orders of magnitude."""
    floor = _mode_roi_weight(mode)                 # large-stack weight (0 day, 0.5 overnight)
    small = config.ROI_WEIGHT_SMALL_STACK
    nw = float(net_worth or 0)
    if nw <= config.ROI_STACK_LO:
        return small
    if nw >= config.ROI_STACK_HI:
        return floor
    t = (math.log(nw) - math.log(config.ROI_STACK_LO)) / (math.log(config.ROI_STACK_HI) - math.log(config.ROI_STACK_LO))
    return small + (floor - small) * t             # small stack → `small`, large → mode `floor`


def _roi_mult(margin_pct: float | None, roi_weight: float) -> float:
    """Capital-efficiency tilt: × margin_pct^roi_weight (1.0 when disabled or no ROI)."""
    if roi_weight and margin_pct and margin_pct > 0:
        return margin_pct ** roi_weight
    return 1.0


def _composite(gp_cycle: float, fill_eta_h: float | None, time_weight: float,
               *, margin_pct: float | None = None, roi_weight: float = 0.0) -> float:
    """EV per unit of the scarce resource: real-time (online) vs GE slot/cycle (offline),
    tilted toward ROI by × margin_pct^roi_weight (roi_weight=0 → pure gp, the old behaviour)."""
    roi = _roi_mult(margin_pct, roi_weight)
    if time_weight <= 0:
        return gp_cycle * roi  # offline: wall-clock is free, only the per-cycle haul matters
    if fill_eta_h and fill_eta_h > 0:
        return gp_cycle * roi / (fill_eta_h ** time_weight)
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
    fill_cal: dict | None = None,
    edges: dict | None = None,
    beta: float | None = None,
    net_worth: int | None = None,
) -> pd.DataFrame:
    """Return the top ranked flips by the mode-weighted composite score.

    score = (margin × capacity × P(complete) × persist) / fill_eta^w, with w set by
    `mode` (online=1, balanced=0.5, offline=0). Stale/illiquid/penny-churn traps are
    gated out by the tradeable + spread-persistence checks first.
    """
    time_weight = MODE_WEIGHTS.get(mode, 0.5)
    df = build_features(api.latest(), api.one_hour(), api.mapping(), bankroll=bankroll,
                        limit_used=limit_used, beta=beta)  # beta None → config prior (build_features default)
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

    # drop integer-tick flips: a ≤1gp after-tax margin shows fat ROI% on cheap items but doesn't
    # fill (calibration ≈0). Gate on absolute margin — the % floors miss it at low prices.
    df = df[df["margin_abs"].abs() >= config.MIN_NET_MARGIN]
    if df.empty:
        return df

    # auto-applied fill calibration: deflate EV by the (shrunk, per-liquidity) measured fill rate,
    # so the model self-corrects from your real fills. 1.0 per item when there's no calibration yet.
    df["fill_mult"] = [calibration.fill_multiplier(fill_cal, t) for t in df["turnover_1h"]]
    # per-item realized-edge multiplier: down-weight items you've actually been LOSING on (rolling,
    # decaying, floored — never a permanent ban). 1.0 when there's no edge history for the item.
    em = edges or {}
    df["edge_mult"] = [float(em.get(int(i), {}).get("edge_mult", 1.0)) for i in df["item_id"]]
    df["exp_gp_cycle"] = df["exp_gp_cycle"] * df["fill_mult"] * df["edge_mult"]
    df["exp_gp_cycle_fast"] = df["exp_gp_cycle_fast"] * df["fill_mult"] * df["edge_mult"]

    # stack-aware ROI tilt: small stack compounds on margin%, large stack ranks on throughput (mode floor)
    roi_weight = _stack_roi_weight(mode, net_worth if net_worth is not None else bankroll)
    df["score"] = [_composite(c, e, time_weight, margin_pct=m, roi_weight=roi_weight)
                   for c, e, m in zip(df[base_col], df["fill_eta_h"], df["margin_pct"], strict=False)]
    df = df[df["score"] > 0]
    if df.empty:
        return df
    df = df.sort_values(RANK_COL, ascending=False).reset_index(drop=True)
    if not persistence:
        if min_gp:
            df = df[df[base_col] >= min_gp]
        return df.head(top)

    out = _apply_persistence(df, candidates or config.PERSIST_CANDIDATES, mode, fill_cal, edges, roi_weight)
    if min_gp and not out.empty:
        out = out[out["exp_gp_cycle_adj"] >= min_gp]  # drop flips too small to be worth a slot
    return out.head(top).reset_index(drop=True)


def _apply_persistence(df: pd.DataFrame, candidates: int, mode: str,
                       fill_cal: dict | None = None, edges: dict | None = None,
                       roi_weight: float | None = None) -> pd.DataFrame:
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
        # don't recommend a buy the `why` would warn against: skip pumps (don't chase), still-falling
        # dumps (wait for the floor) and normal-volume re-ratings (falling knife). Reuses the 1h
        # timeseries fetch_persistence just cached, so it adds no API call.
        if not anomaly.is_buyable(anomaly.assess(iid, api.latest(), api.one_hour(), api.timeseries)):
            continue
        q = optimal_quote(iid, int(row["capacity"]), name=row["name"], horizon_h=horizon)
        if not q or q.ev <= 0 or q.net_unit < config.MIN_NET_MARGIN:
            continue  # integer-tick flip — fat ROI%, doesn't fill
        # fast margin-decay guard: the 1h persistence check above can't see a spread that collapses
        # within minutes, so re-score the last hour at 5m resolution vs the quoted net. Penalty-only,
        # one cached API call, top-N only, and fast modes only (throughput is where the decay bites).
        rel = fetch_reliability(iid, q.net_unit) if mode in FAST_MODES else None
        if rel and config.RELIAB_HARD_FRAC > 0 and not rel["thin"] and rel["uptime"] < config.RELIAB_HARD_FRAC:
            continue  # margin gone most of the last hour — don't recommend it at all
        reliab_mult = rel["reliab_mult"] if rel else 1.0
        edge_mult = float((edges or {}).get(iid, {}).get("edge_mult", 1.0))
        fmult = calibration.fill_multiplier(fill_cal, row.get("turnover_1h") or 0)
        mult = fmult * edge_mult * reliab_mult  # fill cal × realized-edge × margin-reliability — EV, gp, score
        reliability = st["persist_factor"] * reliab_mult * min(1.0, q.p_round / 0.5)
        rows.append({
            **row.to_dict(),
            "buy_px": q.buy_px, "sell_px": q.sell_px, "margin_abs": q.net_unit,
            "margin_pct": q.net_unit / q.buy_px if q.buy_px else 0.0,
            "capital_deployed": q.buy_px * int(row["capacity"]),
            "p_complete": q.p_round, "fill_eta_h": q.t_buy_h + q.t_sell_h,
            "persist": st["persist"], "realizable_spread": st["realizable_spread"],
            "exp_gp_cycle_adj": q.ev * mult, "reliability": reliability,
            "fill_mult": fmult, "edge_mult": edge_mult, "reliab_mult": reliab_mult,
            "reliab_uptime": rel["uptime"] if rel else None,
            "reliab_gone_frac": rel["gone_frac"] if rel else None,
            "raw_score": q.ev / horizon * mult * _roi_mult(
                q.net_unit / q.buy_px if q.buy_px else None,
                _mode_roi_weight(mode) if roi_weight is None else roi_weight),
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
        "fill_mult": float(row.get("fill_mult", 1.0)), "edge_mult": float(row.get("edge_mult", 1.0)),
        "reliab_mult": float(row.get("reliab_mult", 1.0)), "reliab_gone_frac": row.get("reliab_gone_frac"),
    }


def _worth_gp(row, cap_units: int, tier: str) -> float:
    """Max gp this position could net at its cap — used to drop slot-wasting trivial flips."""
    fill = float(row["p_complete"]) if tier != "hold" else 1.0
    return int(row["margin_abs"]) * cap_units * fill


def _place_score(p: dict) -> float:
    """Placement priority: ROI-tilted gp-per-hour. Fast active flips (high gp, short ETA) rank
    above slow holds; within a tier, higher ROI wins. Replaces fastest-fill-first, which buried
    high-ROI slow flips under fat-but-thin-margin churn."""
    eta = p["buy_eta_h"] if p.get("buy_eta_h", float("inf")) < 100 else 100.0
    roi = p["margin_abs"] / p["buy_px"] if p.get("buy_px") else 0.0
    # build_portfolio is the active/day plan → throughput order (the overnight plan ranks separately)
    return p["gp"] / max(eta, 1e-9) * _roi_mult(roi, config.ROI_WEIGHT_FAST)


def build_portfolio(*, bankroll: int, held_ids=(), free_slots: int, members: bool | None = None,
                    max_accumulate: int = 6, min_gp: int | None = None, min_margin: float = 0.01,
                    limit_used: dict[int, int] | None = None, net_worth: int | None = None,
                    fill_cal: dict | None = None, edges: dict | None = None,
                    beta: float | None = None) -> tuple[list[dict], float]:
    """Two-tier daytime capital deployment (the night plan lives in `overnight`):
      ACTIVE  — one diversified patient ~2h flip per free slot (balanced horizon), capped by
                fast-fill liquidity — what you work in your slots right now and cycle.
      HOLD    — extra positions to absorb idle cash into inventory, capped by each item's buy
                limit, gated by HOLD_MIN_MARGIN so only quality spreads soak the overflow.
    Only items whose spread survives a queue-jump (fast_net > 0) qualify as ACTIVE, so the pile
    can't pour into penny traps. Picks are ordered by ROI-tilted gp/hour. Returns (picks, idle).
    """
    if free_slots <= 0:
        return [], float(bankroll), 0  # no free slots → can't place any new buys
    # daytime plan: every free slot is a patient ~2h flip (you cycle them between check-ins).
    # the slow/offline deals are reserved for the night plan (`overnight`), so during the day
    # we rank every slot on the balanced (2h) horizon.
    roles = ["balanced"] * max(0, free_slots)
    modes = dict.fromkeys([*roles, "balanced"])
    rankings = {m: scan(mode=m, bankroll=bankroll, members=members, top=40, limit_used=limit_used,
                        fill_cal=fill_cal, edges=edges, beta=beta, net_worth=net_worth)
                for m in modes}
    # a flip must clear the slot's opportunity cost to be worth committing a slot + the clicks —
    # derived live from net worth and the ROI the market is paying (see slot_worth_floor).
    if min_gp is None:
        min_gp = slot_worth_floor(int(net_worth or bankroll), rankings["balanced"])
    taken = {int(i) for i in held_ids}

    def ok(row, *, fast: bool) -> bool:
        if int(row["item_id"]) in taken or float(row["margin_pct"]) < min_margin:
            return False  # already used, or return rate too thin for the capital/risk
        # ACTIVE flips queue-jump (buy bid+1 / sell ask-1) → must clear the fast spread.
        # HOLDS place a passive bid and accumulate over 4h → the patient spread is what they
        # capture, so high-volume penny-spread staples (Air rune 4→5) belong here, not nowhere.
        m = float(row.get("margin_fast", 0)) if fast else float(row["margin_abs"])
        return m >= config.MIN_NET_MARGIN  # ≥2gp: 1gp integer-tick flips don't fill (calibration ≈0)

    picks: list[dict] = []
    for role in roles:  # active: best worth-it flip for the role
        for _, row in rankings[role].iterrows():
            fm = float(row.get("fill_mult", 1.0)) * float(row.get("edge_mult", 1.0))  # fill × realized-edge
            if ok(row, fast=True) and _worth_gp(row, int(row["liq_units"]), role) * fm >= min_gp:
                picks.append(_pick_row(row, role, int(row["liq_units"])))
                taken.add(int(row["item_id"]))
                break
    for _, row in rankings["balanced"].iterrows():  # hold: accumulate the rest into inventory
        if sum(p["tier"] == "hold" for p in picks) >= max_accumulate:
            break
        # quality floor: a hold ties capital up for hours, so only park cash in it if it clears
        # HOLD_MIN_MARGIN. Below that, leave the gold liquid to recycle rather than churn junk.
        if (ok(row, fast=False) and float(row["margin_pct"]) >= config.HOLD_MIN_MARGIN
                and _worth_gp(row, int(row["hold_units"]), "hold")
                * float(row.get("fill_mult", 1.0)) * float(row.get("edge_mult", 1.0)) >= min_gp):
            picks.append(_pick_row(row, "hold", int(row["hold_units"])))  # cap by realizable volume
            taken.add(int(row["item_id"]))

    # Rank every candidate, then keep only as many as we can actually place: a position — active OR
    # hold — needs a free slot, so never recommend more than free_slots (was: free_slots active flips
    # PLUS up to max_accumulate holds, which told you to place 4 things with 2 slots). Provisional
    # allocation first gives each the gp/ETA the ranking needs; re-allocating across just the
    # survivors then concentrates the cash into the slots you have (less idle) instead of spreading it
    # over picks you can't place.
    allocated, idle = _allocate(picks, bankroll)
    _finalize_gp(allocated)
    allocated.sort(key=_place_score, reverse=True)  # best ROI-tilted gp/hour first
    allocated = allocated[: max(0, free_slots)]     # cap to placeable slots — the best ones survive
    allocated, idle = _allocate(allocated, bankroll)
    _finalize_gp(allocated)
    _schedule(allocated, free_slots)
    return allocated, idle, min_gp


def slot_worth_floor(net_worth: int, ranking: pd.DataFrame, *, slots: int | None = None,
                     lam: float | None = None) -> int:
    """Dynamic minimum profit for a flip to be WORTH a GE slot = the slot's opportunity cost:
    fair-share capital per slot (net_worth / slots) × the ROI the market is currently paying
    (median margin% of the top candidates) × λ. Self-calibrating — no hard-coded fraction; a
    bigger account or fatter market raises the bar, more slots lowers the per-slot bar. Floored
    at the raw click cost (250)."""
    slots = slots or config.GE_SLOTS
    lam = config.SLOT_WORTH_LAMBDA if lam is None else lam
    fair_share = net_worth / max(1, slots)
    rois = [float(x) for x in ranking["margin_pct"].head(10)] if not ranking.empty else []
    achievable_roi = statistics.median(rois) if rois else config.MIN_MARGIN_PCT
    return max(250, int(fair_share * achievable_roi * lam))


def blended_ref(avg5m: float | None, tick: float | None, big_move: float) -> float | None:
    """Short-term price reference: the 5-minute average pulled toward the last tick in proportion
    to how far the tick has diverged from it. A small gap (noise) barely moves it; a divergence of
    `big_move` or more fully trusts the tick — so a genuine crash/spike isn't smoothed away for 5
    minutes. Returns whichever of the two exists if the other is missing."""
    if not avg5m:
        return tick
    if not tick or big_move <= 0:
        return avg5m
    w = min(1.0, abs(tick - avg5m) / avg5m / big_move)  # weight on the tick, grows with divergence
    return (1 - w) * avg5m + w * tick


def roi_per_hour(roi: float, eta_h: float, floor_h: float) -> float:
    """ROI-per-hour with the fill-time floored, so a high-volume flip whose estimated fill rounds
    toward 0 can't explode the rate and dominate a fatter-margin flip on an artifact. Unrankable
    (0.0) when there's no fill estimate at all (infinite time / no volume)."""
    if not eta_h < float("inf"):
        return 0.0
    return roi / max(eta_h, floor_h)


def rebalance_swaps(offers_roi: list[dict], alts: list[dict], *,
                    ratio: float, max_fill: float) -> list[dict]:
    """Pair the weakest early active buys with the strongest DISTINCT candidate that beats each by
    `ratio` in ROI-per-hour. Each candidate is consumed at most once — you can't pour two cancelled
    slots into one item — so N offers never all point at the same alt (the over-eager failure mode).
    Offers carry `roi_h`/`fill_frac`; alts carry `alt_roi_h` (+ display fields). A near-done buy
    (≥ max_fill filled) keeps its progress; a stuck/underwater buy (roi_h ≤ 0) loses to any real alt."""
    eligible = sorted((o for o in offers_roi if o["fill_frac"] < max_fill), key=lambda o: o["roi_h"])
    ranked = sorted(alts, key=lambda a: -a["alt_roi_h"])
    used, out = set(), []
    for o in eligible:  # worst offer first, matched to the best still-unused candidate that beats it
        for i, a in enumerate(ranked):
            if i not in used and a["alt_roi_h"] >= ratio * max(o["roi_h"], 1e-9):
                out.append({**o, **a})
                used.add(i)
                break
    return out


def _finalize_gp(picks: list[dict]) -> None:
    """Set each pick's realised gp and buy ETA from its allocated qty. Active gp is the per-cycle
    spread × fill; a hold's is the full spread captured once it sells (no queue-jump discount)."""
    for p in picks:
        fill = p["p_complete"] if p["tier"] != "hold" else 1.0
        p["gp"] = p["margin_abs"] * p["qty"] * fill * p.get("fill_mult", 1.0) * p.get("edge_mult", 1.0)
        br = p.get("buy_rate", 0.0)
        p["buy_eta_h"] = p["qty"] / br if br > 0 else float("inf")


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

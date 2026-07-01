"""Trade-history analysis: realized P&L, win rate, per-item and per-day breakdown, churn.

Reads the lock-free audit trail (Flip Exporter history.json = new, Flipping Utilities = old),
deduped by offer uuid, and matches sells against buys by weighted-average cost to attribute
realized P&L per item. The journal DB holds the same P&L but is single-writer-locked while the
terminal is open, so the JSON audit log is the portable source (and covers more history).
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from . import api, config, flip_exporter, runelite
from .runelite import Fill
from .tax import post_tax_received


def collect_fills() -> list[Fill]:
    """All completed buys/sells across sources, deduped by uuid, oldest first."""
    fills: dict[str, Fill] = {}
    for f in flip_exporter.completed_offers(flip_exporter.read_history()):
        fills[f.uuid or f"ex:{f.item_id}:{f.t_ms}"] = f
    rl = runelite.read()
    if rl:
        for f in runelite.completed_offers(rl):
            fills.setdefault(f.uuid or f"fu:{f.item_id}:{f.t_ms}", f)
    return sorted(fills.values(), key=lambda f: f.t_ms)


def realized_pnl(fills: list[Fill]) -> dict:
    """Match sells against buys (weighted-avg cost, post-tax) → realized P&L and diagnostics.
    Pure given `fills`; returns totals, per-item, per-day, win/loss counts and uncovered sells."""
    inv: dict[int, list] = defaultdict(lambda: [0, 0.0])  # item -> [qty, total_cost]
    per_item: dict[int, dict] = defaultdict(lambda: {"name": "", "realized": 0.0, "buys": 0, "sells": 0})
    per_day: dict[str, float] = defaultdict(float)
    realized = uncovered = wins = losses = 0.0
    for f in fills:
        pi = per_item[f.item_id]
        pi["name"] = f.name or str(f.item_id)
        if f.is_buy:
            inv[f.item_id][0] += f.qty
            inv[f.item_id][1] += f.qty * f.price
            pi["buys"] += 1
        else:
            held, cost = inv[f.item_id]
            avg = cost / held if held else 0.0
            matched = min(f.qty, held)
            if matched > 0:
                pnl = matched * (post_tax_received(f.price, item_id=f.item_id) - avg)
                realized += pnl
                pi["realized"] += pnl
                wins += pnl > 0
                losses += pnl < 0
                if f.t_ms:
                    per_day[f"{dt.datetime.fromtimestamp(f.t_ms / 1000):%Y-%m-%d}"] += pnl
                inv[f.item_id][0] -= matched
                inv[f.item_id][1] -= matched * avg
            uncovered += f.qty - matched
            pi["sells"] += 1
    return {"realized": realized, "per_item": dict(per_item), "per_day": dict(per_day),
            "wins": int(wins), "losses": int(losses), "uncovered": int(uncovered),
            "held_cost": sum(c for q, c in inv.values() if q > 0)}


def item_edges(fills: list[Fill], *, half_life: float | None = None, fast_half_life: float | None = None,
               floor: float | None = None, gain: float | None = None, k: float | None = None) -> dict[int, dict]:
    """Rolling per-item realized-edge, EWMA over round-trips → a ranking multiplier.

    For each realized sell we take its ROI vs weighted-avg cost (post-tax) and fold it into a
    recency-weighted average (half-life in TRADES, so old regimes fade). The multiplier is
    penalty-only: a proven loser sinks toward `floor` (never banned — it keeps getting shots so we
    can notice it recover), everything else stays ~1.0. Small samples are shrunk toward neutral.
    A second, faster EWMA (`recent_roi`) is carried only to flag regime shifts."""
    hl = half_life or config.EDGE_HALF_LIFE
    fhl = fast_half_life or config.EDGE_FAST_HALF_LIFE
    floor = config.EDGE_FLOOR if floor is None else floor
    gain = config.EDGE_GAIN if gain is None else gain
    k = config.EDGE_SHRINK_K if k is None else k
    lam, flam = 0.5 ** (1 / hl), 0.5 ** (1 / fhl)
    inv: dict[int, list] = defaultdict(lambda: [0, 0.0])
    st: dict[int, dict] = defaultdict(lambda: {"name": "", "e": 0.0, "w": 0.0, "fe": 0.0, "fw": 0.0, "n": 0})
    for f in fills:
        if f.is_buy:
            inv[f.item_id][0] += f.qty
            inv[f.item_id][1] += f.qty * f.price
            continue
        held, cost = inv[f.item_id]
        avg = cost / held if held else 0.0
        matched = min(f.qty, held)
        if matched <= 0 or avg <= 0:
            continue
        roi = (post_tax_received(f.price, item_id=f.item_id) - avg) / avg
        s = st[f.item_id]
        s["name"] = f.name or str(f.item_id)
        s["e"], s["w"] = lam * s["e"] + (1 - lam) * roi, lam * s["w"] + (1 - lam)
        s["fe"], s["fw"] = flam * s["fe"] + (1 - flam) * roi, flam * s["fw"] + (1 - flam)
        s["n"] += 1
        inv[f.item_id][0] -= matched
        inv[f.item_id][1] -= matched * avg
    out = {}
    for iid, s in st.items():
        ewma = s["e"] / s["w"] if s["w"] else 0.0
        recent = s["fe"] / s["fw"] if s["fw"] else 0.0
        shrunk = ewma * s["n"] / (s["n"] + k)
        mult = max(floor, min(1.0, 1 + gain * min(0.0, shrunk)))  # penalty-only, floored
        out[iid] = {"edge_mult": mult, "ewma_roi": ewma, "recent_roi": recent, "n": s["n"], "name": s["name"]}
    return out


def regime_shifts(edges: dict[int, dict], *, min_n: int = 4, band: float = 0.02) -> list[dict]:
    """Items whose RECENT realized edge diverges from their baseline — the vigilance signal.
    'recovering' = penalised (mult < 1) but lately positive; 'deteriorating' = neutral but lately
    bleeding. Only fires with enough trades and a divergence past `band` (ignore noise)."""
    out = []
    for e in edges.values():
        if e["n"] < min_n or abs(e["recent_roi"] - e["ewma_roi"]) < band:
            continue
        if e["edge_mult"] < 1.0 and e["recent_roi"] > band:
            out.append({**e, "shift": "recovering"})
        elif e["edge_mult"] >= 1.0 and e["recent_roi"] < -band:
            out.append({**e, "shift": "deteriorating"})
    return sorted(out, key=lambda e: e["recent_roi"] - e["ewma_roi"], reverse=True)


def report() -> str:
    """A formatted analysis of your whole trade history."""
    fills = collect_fills()
    if not fills:
        return "  no trade history found (Flip Exporter history.json / Flipping Utilities)."
    r = realized_pnl(fills)
    ts = [f.t_ms for f in fills if f.t_ms]
    span_h = (max(ts) - min(ts)) / 3_600_000 if ts else 0
    buys = [f for f in fills if f.is_buy]
    sells = [f for f in fills if not f.is_buy]
    cancels = [f for f in fills if "CANCEL" in (f.state or "")]
    hourly = api.one_hour()
    lines = ["  === TRADE ANALYSIS ==="]
    if ts:
        lines.append(f"  {len(fills)} fills · {dt.datetime.fromtimestamp(min(ts)/1000):%b %d} → "
                     f"{dt.datetime.fromtimestamp(max(ts)/1000):%b %d} ({span_h:.0f}h span)")
    lines.append(f"  buys {len(buys)} · sells {len(sells)} · partial-cancels {len(cancels)} "
                 f"({len(cancels)/max(1,len(fills))*100:.0f}% — churn)")
    lines.append(f"  REALIZED P&L: {r['realized']:>+13,.0f} gp   ({r['wins']}W/{r['losses']}L, "
                 f"{r['wins']/max(1,r['wins']+r['losses'])*100:.0f}% win)")
    if span_h:
        lines.append(f"  gp/hour (wall-clock incl. idle): {r['realized']/span_h:>+11,.0f}")
    if r["uncovered"]:
        lines.append(f"  (+{r['uncovered']:,} sold units had no matching buy in the data — profit uncounted, "
                     f"so this understates the total)")
    if r["held_cost"]:
        lines.append(f"  unsold inventory at cost: {r['held_cost']:,.0f} gp — realises later")

    ranked = sorted(r["per_item"].values(), key=lambda p: p["realized"], reverse=True)
    edges = item_edges(fills)
    id_by_name = {p["name"]: iid for iid, p in r["per_item"].items()}
    lines.append("  --- top winners / losers (1h vol · edge = ranking weight, <1 = down-weighted) ---")
    for p in ranked[:6] + ranked[-5:]:
        iid = id_by_name.get(p["name"], 0)
        v = hourly.get(iid, {})
        vbind = min(v.get("lowPriceVolume") or 0, v.get("highPriceVolume") or 0)
        em = edges.get(iid, {}).get("edge_mult", 1.0)
        lines.append(f"    {p['name'][:22]:22} {p['realized']:>+11,.0f}   1h vol {vbind:>9,}   edge ×{em:.2f}")
    shifts = regime_shifts(edges)
    if shifts:
        lines.append("  --- REGIME WATCH (recent edge diverging from baseline — stay vigilant) ---")
        for s in shifts[:6]:
            arrow = "↑ recovering" if s["shift"] == "recovering" else "↓ deteriorating"
            lines.append(f"    {s['name'][:22]:22} {arrow}: baseline {s['ewma_roi']:+.1%}/flip → "
                         f"recent {s['recent_roi']:+.1%}  (edge ×{s['edge_mult']:.2f})")
    return "\n".join(lines)

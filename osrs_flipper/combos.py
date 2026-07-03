"""Price GE *combinations* (sets, later recipes) in both directions, honestly.

The whole edge lives in one asymmetry: **GE tax is paid only on the SOLD leg.** So the two directions of
a set net differently and must both be modelled:

  * ASSEMBLE — buy the parts, combine (free/instant at the clerk), sell the output. Tax hits the output
    ONCE.
  * BREAK    — buy the output, break it, sell each part. Tax hits EACH part.

Because the tax is capped at 5M/item, ASSEMBLE is structurally favoured for very expensive sets (one
capped tax vs. up to N per-part taxes) — which is exactly why we compute both and pick the winner.

Pricing is not re-implemented here: every leg is priced by `features.build_features`, so combos inherit
the same spread haircut, liquidity/staleness gates, divergence/adverse-move rejection, and buy-limit
accounting the single-item scanner already trusts. A combo is only real if EVERY leg prices and is
tradeable & not suspect; a leg build_features dropped means "can't price this combo now," not an error.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

from .combinations import Combo, item_ids
from .features import build_features
from .tax import ge_tax, post_tax_received

Row = dict[str, Any]


def _post_tax(price: float, iid: int, on_date: dt.date | None) -> int:
    return post_tax_received(int(price), item_id=iid, on_date=on_date)


def price_combination(combo: Combo, feat: dict[int, Row], *, cash: int, members: bool,
                      on_date: dt.date | None = None, keep_unprofitable: bool = False) -> Row | None:
    """Price one combo against a per-item feature map (`{item_id: build_features row}`) and return the
    better of its two directions, or None if no leg-complete, liquid, profitable flip exists.

    `feat` rows are the dict form of `build_features` output; each must expose buy_px, sell_px,
    buy_limit, buy_limit_eff, hold_units, tradeable, suspect, members, fill_eta_h.
    """
    # --- leg gather + hard liquidity gate --------------------------------------------------------
    legs = {iid: feat.get(iid) for iid in item_ids(combo)}
    if any(r is None for r in legs.values()):
        return None  # a leg didn't price (missing/crossed/stale/divergent) → can't do this combo now
    if any((not r["tradeable"]) or r["suspect"] for r in legs.values()):
        return None
    if not members and any(r["members"] for r in legs.values()):
        return None

    out = feat[combo.output_id]

    # --- ASSEMBLE: buy parts (+secondaries) → sell output; tax the output once -------------------
    cost_in = sum(feat[pid]["buy_px"] * qty for pid, qty in combo.inputs) \
        + sum(feat[pid]["buy_px"] * qty for pid, qty in combo.secondaries) + combo.fee
    if combo.output_type == "coins":  # high-alch etc. — output is untaxed coins (Phase 2)
        proceeds_out = out["sell_px"] * combo.output_yield  # sell_px carries the alch value for coin combos
        tax_assemble = 0
    else:
        proceeds_out = _post_tax(out["sell_px"], combo.output_id, on_date) * combo.output_yield
        tax_assemble = int(ge_tax(int(out["sell_px"]), item_id=combo.output_id, on_date=on_date) * combo.output_yield)
    profit_assemble = proceeds_out - cost_in

    # --- BREAK: buy output → sell parts; tax each part (sets only; recipes are one-way) ----------
    profit_break = None
    if combo.reversible:
        cost_out = out["buy_px"] + combo.fee
        proceeds_parts = sum(_post_tax(feat[pid]["sell_px"], pid, on_date) * qty for pid, qty in combo.inputs)
        tax_break = sum(ge_tax(int(feat[pid]["sell_px"]), item_id=pid, on_date=on_date) * qty
                        for pid, qty in combo.inputs)
        profit_break = proceeds_parts - cost_out

    # --- pick the winning direction --------------------------------------------------------------
    assemble = (profit_assemble, "ASSEMBLE", cost_in, proceeds_out, tax_assemble,
                combo.inputs + combo.secondaries)
    cand = [assemble]
    if profit_break is not None:
        cand.append((profit_break, "BREAK", cost_out, proceeds_parts, tax_break, ((combo.output_id, 1),)))
    profit, direction, cost_pc, proceeds_pc, tax_pc, bought = max(cand, key=lambda c: c[0])
    if profit <= 0 and not keep_unprofitable:
        return None

    # --- sizing: capital ∩ buy-limit(bought legs) ∩ volume-realizable(all legs) ------------------
    all_legs = combo.inputs + combo.secondaries + ((combo.output_id, 1),)
    afford = int(cash // cost_pc) if cost_pc > 0 else 0
    limit_cap, limit_leg = math.inf, None
    for pid, qty in bought:
        r = feat[pid]
        if r["buy_limit"] == 0:  # untracked / no buy limit → not a constraint
            continue
        c = r["buy_limit_eff"] // qty
        if c < limit_cap:
            limit_cap, limit_leg = c, pid
    liq_cap, liq_leg = math.inf, None
    for pid, qty in all_legs:
        c = feat[pid]["hold_units"] // qty
        if c < liq_cap:
            liq_cap, liq_leg = c, pid
    conversions = max(0, int(min(afford, limit_cap, liq_cap)))

    binding = min(("capital", afford), (f"limit:{feat[limit_leg]['name'] if limit_leg else '—'}", limit_cap),
                  (f"liquidity:{feat[liq_leg]['name'] if liq_leg else '—'}", liq_cap), key=lambda kv: kv[1])
    bound_by = binding[0]

    etas = [feat[pid]["fill_eta_h"] for pid, _ in all_legs if feat[pid]["fill_eta_h"] is not None]
    fill_eta_h = max(etas) if etas else None

    return {
        "id": combo.id, "name": combo.name, "kind": combo.kind,
        "direction": direction,
        "cost_per_conv": cost_pc, "proceeds_per_conv": proceeds_pc, "profit_per_conv": profit,
        "roi": profit / cost_pc if cost_pc > 0 else 0.0,
        "tax_per_conv": tax_pc,
        "conversions": conversions, "total_gp": profit * conversions,
        "bound_by": bound_by, "fill_eta_h": fill_eta_h,
        "members": any(r["members"] for r in legs.values()),
        "bought_ids": tuple(pid for pid, _ in bought),  # legs to run the anomaly/pump gate on
        "skill": combo.skill, "level": combo.level,
    }


def scan_combinations(combos: list[Combo], latest: dict, hourly: dict, mapping: list, *,
                      cash: int, limit_used: dict[int, int] | None, beta: float, staleness_max: int,
                      members: bool, on_date: dt.date | None = None,
                      keep_unprofitable: bool = False) -> list[Row]:
    """Price every combo against one `build_features` pass and return profitable rows, ranked by total gp.

    Network-free: it consumes already-fetched latest/hourly/mapping. The anomaly/pump gate on bought legs
    is applied by the caller (lazily, top-N) — see `terminal.cmd_sets` — so this stays pure and testable.
    """
    df = build_features(latest, hourly, mapping, bankroll=cash, limit_used=limit_used or {},
                        beta=beta, staleness_max=staleness_max)
    if df.empty:
        return []
    feat = {int(r["item_id"]): r for r in df.to_dict("records")}
    rows = [r for c in combos
            if (r := price_combination(c, feat, cash=cash, members=members, on_date=on_date,
                                       keep_unprofitable=keep_unprofitable)) is not None]
    rows.sort(key=lambda r: r["total_gp"], reverse=True)
    return rows

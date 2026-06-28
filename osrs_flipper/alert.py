"""Output: formatted console table + optional Discord webhook."""

from __future__ import annotations

import pandas as pd

from . import config
from .http import get_session

_BASE_COLUMNS = [
    ("name", "item", 20, "s"),
    ("buy_px", "buy", 8, ",d"),
    ("sell_px", "sell", 8, ",d"),
    ("margin_abs", "net", 6, ",d"),
    ("margin_pct", "mg%", 6, ".1%"),
    ("capacity", "qty", 7, ",d"),
    ("capital_deployed", "deploy", 9, ",d"),
    ("fill_eta_h", "eta(h)", 6, ".1f"),
    ("p_complete", "fill", 5, ".0%"),
]
# shown only when the persistence stage ran
_PERSIST_COLUMNS = [
    ("persist", "persist", 7, ".0%"),
    ("exp_gp_cycle_adj", "gp/cyc", 9, ",.0f"),
    ("score", "SCORE", 10, ",.0f"),
]
_SNAPSHOT_COL = [
    ("exp_gp_cycle", "gp/cyc", 9, ",.0f"),
    ("score", "SCORE", 10, ",.0f"),
]

_MODE_NOTE = {
    "online": "SCORE = gp per real-time HOUR (fast fills win — you're at the keyboard)",
    "offline": "SCORE = gp per CYCLE (fat margins win — fill speed ignored, you're away)",
    "balanced": "SCORE = gp/cycle ÷ √fill_eta (balanced speed vs margin)",
}


def format_table(df: pd.DataFrame, mode: str = "balanced") -> str:
    if df.empty:
        return "(no flips passed the filters)"
    cols = _BASE_COLUMNS + (_PERSIST_COLUMNS if "exp_gp_cycle_adj" in df.columns else _SNAPSHOT_COL)
    header = " ".join(f"{title:>{w}}" if fmt != "s" else f"{title:<{w}}"
                      for _, title, w, fmt in cols)
    lines = [header, "-" * len(header)]
    for _, row in df.iterrows():
        cells = []
        for col, _title, w, fmt in cols:
            val = row[col]
            if val is None or (isinstance(val, float) and pd.isna(val)):
                cells.append(f"{'—':>{w}}" if fmt != "s" else f"{'—':<{w}}")
            elif fmt == "s":
                cells.append(f"{str(val)[:w]:<{w}}")
            else:
                cells.append(f"{val:>{w}{fmt}}")
        lines.append(" ".join(cells))
    lines.append("")
    lines.append(f"[{mode}] " + _MODE_NOTE.get(mode, _MODE_NOTE["balanced"]) + "  ·  eta = hrs to fill both legs")
    if "exp_gp_cycle_adj" in df.columns:
        lines.append(f"scores shrunk for the optimizer's curse · spread capital across your top "
                     f"{config.GE_SLOTS} — don't all-in #1")
    return "\n".join(lines)


def format_bond_line(progress: dict) -> str:
    price, pct = progress.get("bond_price"), progress.get("pct")
    if not price:
        return "bond: price unavailable"
    return f"bond: {price:,.0f} gp  |  bankroll {progress['bankroll']:,} = {pct:.1f}% of a bond"


def format_portfolio_summary(df: pd.DataFrame, bankroll: int, slots: int = config.GE_SLOTS) -> str:
    """The 'worth it' check: greedily split your cash across the top `slots` flips (each
    capped by what it can absorb), then report realistic deployed / idle / gp-per-cycle."""
    if df.empty or "capital_deployed" not in df.columns or not bankroll:
        return ""
    gp_col = "exp_gp_cycle_adj" if "exp_gp_cycle_adj" in df.columns else "exp_gp_cycle"
    remaining, deployed, gp_total, used = float(bankroll), 0.0, 0.0, 0
    for _, r in df.head(slots).iterrows():
        cap_dep = float(r["capital_deployed"])
        if cap_dep <= 0 or remaining <= 0:
            continue
        alloc = min(remaining, cap_dep)
        gp_total += float(r[gp_col]) * (alloc / cap_dep)  # pro-rate gp by capital actually placed
        deployed += alloc
        remaining -= alloc
        used += 1
    idle = max(0.0, bankroll - deployed)
    line = (f"best {used}-slot allocation deploys {deployed:,.0f} of {bankroll:,.0f} "
            f"({deployed / bankroll * 100:.0f}%, {idle:,.0f} idle)  →  ~{gp_total:,.0f} gp/cycle")
    if idle > 0.5 * bankroll:
        line += ("\n⚠ over half your cash is idle — F2P flips can't absorb it. Use --mode offline, run "
                 "more items, or grind toward a bond (members = 8 slots + a far deeper market).")
    return line


def format_portfolio(picks: list[dict], bankroll: int, held=None, idle: float = 0.0) -> str:
    """Render the two-tier plan: active flips (work in your slots) + hold (accumulate)."""
    held = held or []
    n_active = sum(p["tier"] != "hold" for p in picks)
    lines = [f"=== portfolio · cash {bankroll:,} · {len(held)} held · {n_active} active + "
             f"{len(picks) - n_active} accumulate ==="]
    if not picks:
        lines.append("  (nothing passed the filters)")
    else:
        lines.append(f"  {'type':9} {'item':18} {'buy':>8} {'sell':>8} {'qty':>8} {'deploy':>9} {'gp':>9}")
        lines.append("  " + "-" * 72)
        tot_dep = tot_gp = 0.0
        for p in picks:
            label = p["tier"] if p["tier"] != "hold" else "hold↓"
            lines.append(f"  {label:9} {p['name'][:18]:18} {p['buy_px']:>8,} {p['sell_px']:>8,} "
                         f"{p['qty']:>8,} {p['deploy']:>9,} {p['gp']:>9,.0f}")
            tot_dep += p["deploy"]
            tot_gp += p["gp"]
        lines.append("  " + "-" * 72)
        lines.append(f"  deploy {tot_dep:,.0f} of {bankroll:,} ({idle:,.0f} idle)  ·  ~{tot_gp:,.0f} gp potential")
        lines.append("  active = work in your slots now · hold↓ = accumulate into inventory over buy-limit cycles")
    if held:
        lines.append("  held (selling): " + ", ".join(f"{h.name} ({h.qty:,})" for h in held[:6]))
    if bankroll and idle > 0.5 * bankroll:
        lines.append("  ⚠ still over half idle — even buy-limit accumulation can't absorb it; "
                     "the F2P market is the ceiling. A bond opens the members market.")
    return "\n".join(lines)


def format_quote(q) -> str:
    """Render an optimal-quote result with per-leg fill probabilities and the frontier."""
    if q is None:
        return "(no quote — insufficient data or no profitable price exists)"
    lines = [
        f"=== {q.name} — optimal quote (qty {q.qty:,}, {q.horizon_h:g}h horizon) ===",
        f"market: bid {q.bid:,} / ask {q.ask:,}",
        f"RECOMMEND  buy {q.buy_px:,}  sell {q.sell_px:,}  →  net {q.net_unit:,}/unit  |  "
        f"fill: buy {q.p_buy:.0%} · sell {q.p_sell:.0%} · round {q.p_round:.0%}  |  EV {q.ev:,.0f} gp",
        "",
        "frontier (thin/fast → fat/slow):",
        f"  {'buy':>6} {'sell':>6} {'net':>5} {'buy%':>6} {'sell%':>6} {'round%':>7} {'EV':>10}",
        "  " + "-" * 50,
    ]
    for r in q.frontier:
        lines.append(
            f"  {r['buy']:>6,} {r['sell']:>6,} {r['net_unit']:>5,} {r['p_buy']:>6.0%} "
            f"{r['p_sell']:>6.0%} {r['p_round']:>7.0%} {r['ev']:>10,.0f}"
        )
    lines.append("")
    lines.append(f"fill% = expected fraction of {q.qty:,} filled within {q.horizon_h:g}h at that price")
    return "\n".join(lines)


def to_discord(content: str, webhook_url: str | None = None) -> bool:
    """Post a fenced message to a Discord webhook. Returns True on success."""
    url = webhook_url or config.DISCORD_WEBHOOK_URL
    if not url:
        return False
    resp = get_session().post(url, json={"content": f"```\n{content[:1900]}\n```"}, timeout=15)
    return resp.ok

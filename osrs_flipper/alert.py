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
    return "\n".join(lines)


def format_bond_line(progress: dict) -> str:
    price, pct = progress.get("bond_price"), progress.get("pct")
    if not price:
        return "bond: price unavailable"
    return f"bond: {price:,.0f} gp  |  bankroll {progress['bankroll']:,} = {pct:.1f}% of a bond"


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

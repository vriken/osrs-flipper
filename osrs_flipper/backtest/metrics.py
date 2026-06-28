"""Backtest metrics — never a single headline number. Reports completion/fill rates
(the diagnostics that reveal an over-optimistic model) alongside P&L."""

from __future__ import annotations

import statistics
from typing import Any

from .. import config

BAR_SECONDS = {"5m": 300, "1h": 3600, "6h": 21600, "24h": 86400}


def compute_metrics(trades: list[dict[str, Any]], *, timestep: str, capital: int) -> dict[str, Any]:
    """Aggregate trade records into honest performance metrics."""
    attempted = len(trades)
    filled = [t for t in trades if t["units"] > 0]
    completed = [t for t in filled if t["completed"]]
    bar_s = BAR_SECONDS.get(timestep, 3600)

    if not filled:
        return {"attempted": attempted, "filled": 0, "completed": 0, "total_pnl": 0,
                "note": "no fills — strategy produced no executable trades on this data"}

    pnls = [t["pnl"] for t in filled]
    total_pnl = sum(pnls)
    holds = [t["hold_bars"] for t in filled]
    costs = [t["cost"] for t in filled]
    wins = [t for t in filled if t["pnl"] > 0]

    # gp/day from the wall-clock span the trades cover
    t0 = min(t["entry_ts"] for t in filled)
    t1 = max(t["exit_ts"] for t in filled)
    span_days = max((t1 - t0) / 86400, 1e-9)

    # drawdown on cumulative realised P&L (ordered by exit)
    cum, peak, max_dd = 0, 0, 0
    for t in sorted(filled, key=lambda x: x["exit_ts"]):
        cum += t["pnl"]
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)

    n_offers = 2 * len(completed) + len([t for t in filled if not t["completed"]])
    active_minutes = n_offers * config.SECONDS_PER_OFFER / 60

    return {
        "attempted": attempted,
        "filled": len(filled),
        "completed": len(completed),
        "buy_fill_rate": len(filled) / attempted if attempted else 0.0,
        "completion_rate": len(completed) / len(filled),
        "win_rate": len(wins) / len(filled),
        "total_pnl": total_pnl,
        "roi_pct": total_pnl / sum(costs) if sum(costs) else 0.0,
        "gp_per_day": total_pnl / span_days,
        "gp_per_hour": total_pnl / (sum(holds) * bar_s / 3600) if sum(holds) else 0.0,
        "gp_per_active_min": total_pnl / active_minutes if active_minutes else 0.0,
        "avg_hold_bars": statistics.mean(holds),
        "median_hold_bars": statistics.median(holds),
        "max_drawdown": max_dd,
        "span_days": span_days,
    }


def format_metrics(by_beta: dict[float, dict[str, Any]], *, strategy: str, timestep: str, n_items: int) -> str:
    """Render a β-sensitivity table (β=0 optimistic ceiling → β=0.5 pessimistic floor)."""
    lines = [
        f"strategy={strategy}  timestep={timestep}  items={n_items}",
        f"{'beta':>5} {'fills':>6} {'compl%':>7} {'win%':>6} {'total_pnl':>12} {'gp/day':>10} {'roi%':>7} {'maxDD':>10}",
        "-" * 72,
    ]
    for beta in sorted(by_beta):
        m = by_beta[beta]
        if not m.get("filled"):
            lines.append(f"{beta:>5.2f} {'0':>6}  (no fills)")
            continue
        lines.append(
            f"{beta:>5.2f} {m['filled']:>6} {m['completion_rate']:>6.0%} {m['win_rate']:>6.0%} "
            f"{m['total_pnl']:>12,.0f} {m['gp_per_day']:>10,.0f} {m['roi_pct']:>6.1%} {m['max_drawdown']:>10,.0f}"
        )
    lines.append("")
    lines.append("β=0 is the free-spread fantasy ceiling; β=0.25 is the honest default; β=0.5 the floor.")
    return "\n".join(lines)

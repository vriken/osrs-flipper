"""Net-worth progress chart: a realized-history staircase + live equity, and a milestone
projection to 10M/100M.

The series, rate fit, and ETA math are pure (unit-tested). `render` lazily imports matplotlib
so the rest of the tool never depends on a plotting library being installed.
"""

from __future__ import annotations

import math


def build_history(ledger_rows: list[tuple], cash_now: float) -> tuple[float, list[int], list[float]]:
    """`ledger_rows`: (ts, cash_delta, realized_pnl) sorted by ts. Net worth AT COST = the cash you
    started with + cumulative realized P&L — buys/sells net to zero on net worth except the booked
    spread, so the curve steps up only on profitable sells. Returns (initial, times, networth)."""
    sum_cd = sum(cd for _, cd, _ in ledger_rows)
    initial = cash_now - sum_cd  # cash before the first recorded trade = starting net worth
    times, series, cum = [], [], 0.0
    for ts, _cd, pnl in ledger_rows:
        cum += pnl or 0.0
        times.append(ts)
        series.append(initial + cum)
    return initial, times, series


def fit_daily_rate(ledger_rows: list[tuple], base: float) -> tuple[float | None, float]:
    """Compounding %/day implied by realized profit over the recorded span, relative to `base`
    (starting net worth). Returns (rate, span_days); rate is None when there's < 1h of history
    (too little to annualise without absurd numbers)."""
    if len(ledger_rows) < 2 or base <= 0:
        return None, 0.0
    span_days = (ledger_rows[-1][0] - ledger_rows[0][0]) / 86400
    if span_days < 1 / 24:
        return None, span_days
    realized = sum((p or 0) for _, _, p in ledger_rows)
    return max(0.0, (realized / base) / span_days), span_days


def eta_days(start: float, target: float, daily_rate: float) -> float | None:
    """Days to compound from `start` to `target` at `daily_rate`; None if unreachable."""
    if daily_rate <= 0 or target <= start:
        return None
    return math.log(target / start) / math.log(1 + daily_rate)


def render(out_path: str, *, initial: float, times: list[int], networth: list[float],
           equity_now: float, daily_rate: float | None, span_days: float,
           milestones=(10_000_000, 100_000_000), horizon_days: int = 180) -> str | None:
    """Draw the 2-panel chart (actual history + projection) to `out_path`. Returns the path, or
    None if matplotlib isn't available."""
    import datetime as dt
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 9))
    # --- panel 1: actual realized history (linear) + live-equity marker ---------------------
    t = [dt.datetime.fromtimestamp(x) for x in times]
    t = [t[0] - dt.timedelta(minutes=5)] + t
    nw = [initial] + list(networth)
    ax1.step(t, [n / 1e6 for n in nw], where="post", color="#1f9d55", lw=2, label="net worth @ cost")
    ax1.fill_between(t, initial / 1e6, [n / 1e6 for n in nw], step="post", alpha=0.12, color="#1f9d55")
    ax1.scatter([t[-1]], [equity_now / 1e6], color="#b45309", zorder=5, label="live equity (mkt)")
    unreal = equity_now - nw[-1]
    ax1.annotate(f"+{unreal:,.0f} unrealised", (t[-1], equity_now / 1e6),
                 textcoords="offset points", xytext=(-10, 8), ha="right", fontsize=9, color="#b45309")
    ax1.set_ylabel("net worth (M gp)")
    ax1.set_title(f"Actual — +{nw[-1] - initial:,.0f} realized over {span_days * 24:.1f}h  ·  "
                  f"live equity ~{equity_now:,.0f} gp")
    ax1.grid(alpha=0.3)
    ax1.legend(loc="upper left", fontsize=9)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%b-%d %H:%M"))
    fig.autofmt_xdate()

    # --- panel 2: projection to milestones (log) --------------------------------------------
    ax2.set_yscale("log")
    if daily_rate and daily_rate > 0:
        r_lo, r_hi = 0.6 * daily_rate, 1.5 * daily_rate  # asymmetric: more downside (rate decays)
        days = list(range(0, horizon_days + 1))
        lo = [equity_now * (1 + r_lo) ** d for d in days]
        hi = [equity_now * (1 + r_hi) ** d for d in days]
        ax2.fill_between(days, lo, hi, alpha=0.18, color="#2563eb",
                         label=f"compounding {r_lo:.1%}–{r_hi:.1%}/day (fit {daily_rate:.1%})")
        ax2.plot(days, lo, "--", color="#2563eb", lw=1.5)
        ax2.plot(days, hi, "--", color="#2563eb", lw=1.5)
        for tgt in milestones:
            ax2.axhline(tgt, color="#b91c1c", ls=":", lw=1)
            d_hi, d_lo = eta_days(equity_now, tgt, r_hi), eta_days(equity_now, tgt, r_lo)
            lbl = f"{tgt/1e6:.0f}M"
            if d_hi and d_lo:
                lbl += f"  ~{d_hi:.0f}–{d_lo:.0f} days"
            ax2.annotate(lbl, (1, tgt * 1.06), fontsize=9, color="#b91c1c")
        decay_from = eta_days(equity_now, 20_000_000, r_hi)
        if decay_from and decay_from < horizon_days:
            ax2.axvspan(decay_from, horizon_days, alpha=0.07, color="red")
            ax2.annotate("rate decays past ~20M (S-curve)\n→ real path bends below this cone",
                         (decay_from + 2, equity_now * 1.4), fontsize=8, color="#7f1d1d")
        ax2.set_xlim(0, horizon_days)
        ax2.set_ylim(min(initial, equity_now) * 0.8, max(milestones) * 2)
        ax2.legend(loc="lower right", fontsize=9)
    else:
        ax2.text(0.5, 0.5, "not enough history to project a rate yet — keep flipping",
                 ha="center", va="center", transform=ax2.transAxes, fontsize=11, color="#666")
    ax2.scatter([0], [equity_now], color="#1f9d55", zorder=5)
    ax2.annotate("you are here", (0, equity_now), textcoords="offset points",
                 xytext=(8, -4), fontsize=9, color="#1f9d55")
    ax2.set_xlabel("days from now (assumes a steady daily flipping pace; optimistic — growth slows at scale)")
    ax2.set_ylabel("net worth (gp, log)")
    ax2.set_title("Projection — constant-rate extrapolation")
    ax2.grid(alpha=0.3, which="both")

    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    return out_path

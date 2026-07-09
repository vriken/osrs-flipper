"""Net-worth progress chart: a realized-history staircase + live equity, and a milestone
projection to 10M/100M.

The series, rate fit, and ETA math are pure (unit-tested). `render` lazily imports matplotlib
so the rest of the tool never depends on a plotting library being installed.
"""

from __future__ import annotations

import math

# Validated palette (dataviz skill reference instance). blue↔orange is the canonical colourblind-safe
# pair (validator: worst adjacent ΔE 96.7 on the light surface); the rest are the reference chart tokens.
_SURFACE, _INK, _INK2, _MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
_GRID, _AXIS = "#e1e0d9", "#c3c2b7"
_SERIES, _EQUITY, _WARN = "#2a78d6", "#eb6834", "#b45309"  # net worth · live-equity/"now" · caveat


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

    rc = {"font.size": 10, "text.color": _INK, "axes.labelcolor": _INK2,
          "xtick.color": _MUTED, "ytick.color": _MUTED, "axes.edgecolor": _AXIS}
    with plt.rc_context(rc):  # scope styling to this figure (don't leak into scripts/plot.py)
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 9))
        fig.patch.set_facecolor(_SURFACE)
        for ax in (ax1, ax2):
            ax.set_facecolor(_SURFACE)
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.tick_params(length=0)
            ax.set_axisbelow(True)

        # --- panel 1: realized history + the live-equity "now" marker ------------------------
        t = [dt.datetime.fromtimestamp(x) for x in times]
        t = [t[0] - dt.timedelta(minutes=5)] + t
        nw = [initial] + list(networth)
        nw_m = [n / 1e6 for n in nw]
        ax1.step(t, nw_m, where="post", color=_SERIES, lw=2, label="net worth @ cost")
        ax1.fill_between(t, initial / 1e6, nw_m, step="post", alpha=0.10, color=_SERIES, lw=0)
        # tie the realized end to the live-equity dot so it reads as one continuous "now"
        ax1.plot([t[-1], t[-1]], [nw_m[-1], equity_now / 1e6], color=_EQUITY, lw=1.5, ls=":", zorder=4)
        ax1.scatter([t[-1]], [equity_now / 1e6], s=44, color=_EQUITY, zorder=5, label="live equity (mkt)")
        unreal = equity_now - nw[-1]
        ax1.annotate(f"{unreal:+,.0f} unrealised", (t[-1], equity_now / 1e6),
                     textcoords="offset points", xytext=(-8, 7), ha="right", fontsize=9, color=_EQUITY)
        ax1.set_ylabel("net worth (M gp)")
        ax1.set_title(f"Actual  ·  +{nw[-1] - initial:,.0f} realized over {span_days * 24:.1f}h  ·  "
                      f"live equity ~{equity_now:,.0f} gp", color=_INK, fontsize=12, loc="left", pad=10)
        ax1.grid(axis="y", color=_GRID, lw=0.8)
        ax1.legend(loc="upper left", fontsize=9, frameon=False)
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%b-%d %H:%M"))
        fig.autofmt_xdate()

        # --- panel 2: your climb so far + forward projection, on ONE elapsed-time axis --------
        # "now" sits at the elapsed span — you are NOT at day 0; the cone continues the journey.
        ax2.set_yscale("log")
        now_x = max(0.0, span_days)
        if daily_rate and daily_rate > 0:
            r_lo, r_hi = 0.6 * daily_rate, 1.5 * daily_rate  # asymmetric: more downside (rate decays)
            fwd = list(range(0, horizon_days + 1))
            xs = [now_x + d for d in fwd]                      # forward days offset onto the elapsed axis
            lo = [equity_now * (1 + r_lo) ** d for d in fwd]
            hi = [equity_now * (1 + r_hi) ** d for d in fwd]
            ax2.fill_between(xs, lo, hi, alpha=0.16, color=_SERIES, lw=0,
                             label=f"compounding {r_lo:.0%}–{r_hi:.0%}/day  ·  fit {daily_rate:.0%}")
            ax2.plot(xs, lo, color=_SERIES, lw=1.5)
            ax2.plot(xs, hi, color=_SERIES, lw=1.5)
            # your realized climb so far (day 0 → now), faint, so the cone visibly continues it
            hx = [(ts - times[0]) / 86400 for ts in times]
            ax2.plot(hx, list(networth), color=_SERIES, lw=1.2, alpha=0.5)
            # milestones: calm reference lines (muted); ETA is "from now", labelled at the left
            for tgt in milestones:
                ax2.axhline(tgt, color=_MUTED, ls=(0, (2, 3)), lw=1)
                d_hi, d_lo = eta_days(equity_now, tgt, r_hi), eta_days(equity_now, tgt, r_lo)
                lbl = f"{tgt / 1e6:.0f}M"
                if d_hi and d_lo:
                    lbl += f"  ~{d_hi:.0f}–{d_lo:.0f} days from now"
                ax2.annotate(lbl, (0.5, tgt * 1.12), fontsize=9, color=_INK2)
            # decay caveat: a quiet boundary + faint wash (offset onto the elapsed axis)
            decay_from = eta_days(equity_now, 20_000_000, r_hi)
            if decay_from and decay_from < horizon_days:
                ax2.axvspan(now_x + decay_from, now_x + horizon_days, color=_MUTED, alpha=0.06, lw=0)
                ax2.axvline(now_x + decay_from, color=_WARN, ls="--", lw=1)
                ax2.annotate("growth slows past ~20M (S-curve)\nreal path bends below the cone",
                             (now_x + decay_from, equity_now * 2.2), textcoords="offset points", xytext=(7, 0),
                             fontsize=8, color=_WARN, va="center")
            ax2.set_xlim(0, now_x + horizon_days)
            ax2.set_ylim(min(initial, equity_now) * 0.8, max(milestones) * 2)
            ax2.legend(loc="lower right", fontsize=9, frameon=False)
        else:
            ax2.text(0.5, 0.5, "not enough history to project a rate yet — keep flipping",
                     ha="center", va="center", transform=ax2.transAxes, fontsize=11, color=_MUTED)
        ax2.scatter([now_x], [equity_now], s=44, color=_EQUITY, zorder=5)
        ax2.annotate(f"you are here  ·  {now_x:.1f}d in", (now_x, equity_now), textcoords="offset points",
                     xytext=(10, -2), fontsize=9, color=_EQUITY)
        ax2.set_xlabel("days since you started  ·  cone = forward projection (optimistic; growth slows at scale)")
        ax2.set_ylabel("net worth (gp, log)")
        ax2.set_title("Projection  ·  constant-rate extrapolation from now", color=_INK, fontsize=12,
                      loc="left", pad=10)
        ax2.grid(axis="y", which="major", color=_GRID, lw=0.8)

        fig.tight_layout()
        fig.savefig(out_path, dpi=110, facecolor=_SURFACE)
        plt.close(fig)
    return out_path

"""Net-worth progress chart: a realized-history staircase + live equity, and a Monte-Carlo
projection to 10M/100M.

Growth is measured over ACTIVE time — long idle gaps (days you weren't trading, nothing growing)
are clamped out so they don't dilute the compounding rate. The projection is a Monte-Carlo fan
(random daily returns around the fitted rate/vol, drift decaying at scale), not a single line.

The series, growth fit, MC, and ETA math are pure (unit-tested). `render` lazily imports matplotlib
so the rest of the tool never depends on a plotting library being installed.
"""

from __future__ import annotations

import math

import numpy as np

from .config import MC_DECAY_PIVOT, MC_DEFAULT_CV, MC_PATHS, MC_SEED, PROGRESS_IDLE_GAP_MAX_H

# Validated palette (dataviz skill reference instance). blue↔orange is the canonical colourblind-safe
# pair (validator: worst adjacent ΔE 96.7 on the light surface); the rest are the reference chart tokens.
_SURFACE, _INK, _INK2, _MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
_GRID, _AXIS = "#e1e0d9", "#c3c2b7"
_SERIES, _EQUITY = "#2a78d6", "#eb6834"  # net worth · live-equity/"now"


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


def _active_positions(times: list[int], gap_max_h: float = PROGRESS_IDLE_GAP_MAX_H) -> list[float]:
    """Cumulative ACTIVE days at each timestamp. Inter-trade gaps longer than `gap_max_h` are clamped
    to it — during a multi-day absence nothing was growing, so that dead time must not count toward the
    compounding clock. Returns a list aligned to `times` (first element 0.0)."""
    cap = gap_max_h * 3600
    pos, acc = [], 0.0
    for i, ts in enumerate(times):
        if i > 0:
            acc += min(max(0, ts - times[i - 1]), cap)
        pos.append(acc / 86400)
    return pos


def _active_day_returns(ledger_rows: list[tuple], pos: list[float], base: float) -> list[float]:
    """Fractional net-worth return per integer ACTIVE day: realized P&L summed within each active-day
    bucket, over the net worth at that bucket's start. The series the MC's volatility is fit from."""
    nw = base
    buckets: dict[int, list[float]] = {}
    order: list[int] = []
    for (_ts, _cd, pnl), p in zip(ledger_rows, pos, strict=False):
        d = int(p)
        if d not in buckets:
            buckets[d] = [0.0, nw]  # [pnl_sum, nw_at_bucket_start]
            order.append(d)
        buckets[d][0] += (pnl or 0.0)
        nw += (pnl or 0.0)
    return [buckets[d][0] / buckets[d][1] for d in order if buckets[d][1] > 0]


def fit_growth(ledger_rows: list[tuple], base: float, *,
               gap_max_h: float = PROGRESS_IDLE_GAP_MAX_H) -> tuple[float | None, float, float]:
    """Compounding stats over ACTIVE time. Returns (daily_rate, daily_vol, active_days):
      * daily_rate — mean fractional growth per ACTIVE day relative to `base`; idle gaps are clamped
        out, so days you weren't trading don't dilute it;
      * daily_vol  — std of the per-active-day returns, or MC_DEFAULT_CV·rate when history is too thin;
      * active_days — elapsed active span (wall-clock minus clamped idle).
    daily_rate is None when there's < 1h of active history (too little to annualise honestly)."""
    if len(ledger_rows) < 2 or base <= 0:
        return None, 0.0, 0.0
    times = [r[0] for r in ledger_rows]
    pos = _active_positions(times, gap_max_h)
    active_days = pos[-1]
    if active_days < 1 / 24:
        return None, 0.0, active_days
    realized = sum((r[2] or 0) for r in ledger_rows)
    rate = max(0.0, (realized / base) / active_days)
    daily = _active_day_returns(ledger_rows, pos, base)
    vol = float(np.std(daily)) if len(daily) >= 3 else MC_DEFAULT_CV * rate
    return rate, vol, active_days


def simulate_paths(start: float, daily_rate: float, daily_vol: float, horizon_days: int, *,
                   n_paths: int = MC_PATHS, seed: int = MC_SEED,
                   decay_pivot: float = MC_DECAY_PIVOT) -> np.ndarray:
    """Monte-Carlo net-worth paths. Each ACTIVE day multiplies by (1 + draw), draw ~ Normal(drift,
    daily_vol) floored at −0.99 (you can't lose more than ~everything in a day). `drift` decays as net
    worth passes `decay_pivot` — the S-curve: liquidity and buy limits bite at scale, so a fixed %/day
    isn't sustainable forever. Deterministic under `seed` so the chart is stable between renders.
    Returns a (horizon_days + 1, n_paths) array."""
    rng = np.random.default_rng(seed)
    paths = np.empty((horizon_days + 1, n_paths))
    paths[0] = start
    for d in range(1, horizon_days + 1):
        prev = paths[d - 1]
        drift = daily_rate / (1.0 + prev / decay_pivot)  # → daily_rate when small, → 0 far above the pivot
        draws = np.maximum(-0.99, rng.normal(drift, daily_vol, n_paths))
        paths[d] = prev * (1.0 + draws)
    return paths


def percentile_fan(paths: np.ndarray, qs=(10, 25, 50, 75, 90)) -> dict[int, np.ndarray]:
    """Per-day percentiles across the simulated paths — the fan drawn on the projection."""
    return {q: np.percentile(paths, q, axis=1) for q in qs}


def crossing_stats(paths: np.ndarray, target: float, *, step_days: float = 1.0) -> dict:
    """When does `target` first get reached across the paths? Returns the reach probability within the
    horizon and, over the paths that reach it, the first-crossing-day distribution (median, p10, p90)."""
    reached = paths >= target
    ever = reached.any(axis=0)
    if not ever.any():
        return {"prob": 0.0, "median": None, "p10": None, "p90": None}
    first = reached.argmax(axis=0).astype(float)[ever] * step_days
    return {"prob": float(ever.mean()), "median": float(np.median(first)),
            "p10": float(np.percentile(first, 10)), "p90": float(np.percentile(first, 90))}


def eta_days(start: float, target: float, daily_rate: float) -> float | None:
    """Days to compound from `start` to `target` at `daily_rate`; None if unreachable."""
    if daily_rate <= 0 or target <= start:
        return None
    return math.log(target / start) / math.log(1 + daily_rate)


def render(out_path: str, *, initial: float, times: list[int], networth: list[float],
           equity_now: float, span_days: float, active_days: float, paths: np.ndarray | None = None,
           daily_rate: float | None = None, daily_vol: float | None = None,
           milestones=(10_000_000, 100_000_000)) -> str | None:
    """Draw the 2-panel chart (realized history + Monte-Carlo projection) to `out_path`. `paths` is the
    MC array from `simulate_paths` (None when there's too little active history). Returns the path, or
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

        # --- panel 1: realized history + the live-equity "now" marker (wall-clock) -----------
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

        # --- panel 2: your climb so far + a Monte-Carlo forward fan, on an ACTIVE-day axis ---
        # "now" sits at the elapsed ACTIVE span (idle excluded) — you are NOT at day 0.
        ax2.set_yscale("log")
        now_x = max(0.0, active_days)
        if paths is not None and len(paths) > 1:
            horizon = paths.shape[0] - 1
            xs = now_x + np.arange(horizon + 1)
            fan = percentile_fan(paths)
            # nested uncertainty bands (same hue, sequential): 10–90% outer, 25–75% inner, median line
            ax2.fill_between(xs, fan[10], fan[90], color=_SERIES, alpha=0.12, lw=0, label="10–90% of sims")
            ax2.fill_between(xs, fan[25], fan[75], color=_SERIES, alpha=0.22, lw=0, label="25–75%")
            ax2.plot(xs, fan[50], color=_SERIES, lw=2, label="median path")
            # your realized climb so far (day 0 → now), faint, so the fan visibly continues it
            ax2.plot(_active_positions(times), list(networth), color=_SERIES, lw=1.2, alpha=0.5)
            # milestones: reach probability + first-crossing-day distribution FROM the simulation
            for tgt in milestones:
                ax2.axhline(tgt, color=_MUTED, ls=(0, (2, 3)), lw=1)
                cs = crossing_stats(paths, tgt)
                if cs["prob"] <= 0:
                    lbl = f"{tgt / 1e6:.0f}M  ·  not reached in {horizon}d"
                else:
                    lbl = (f"{tgt / 1e6:.0f}M  ·  {cs['prob'] * 100:.0f}% reach  ·  "
                           f"median ~{cs['median']:.0f}d (10–90%: {cs['p10']:.0f}–{cs['p90']:.0f}d)")
                ax2.annotate(lbl, (0.5, tgt * 1.12), fontsize=9, color=_INK2)
            ax2.set_xlim(0, now_x + horizon)
            ax2.set_ylim(min(initial, equity_now) * 0.8, max(milestones) * 2)
            sub = f"{MC_PATHS:,} paths"
            if daily_rate is not None:
                sub += (f"  ·  fit {daily_rate:.0%}/day, vol {daily_vol:.0%}"
                        f"  ·  drift decays past {MC_DECAY_PIVOT / 1e6:.0f}M")
            ax2.legend(loc="lower right", fontsize=9, frameon=False, title=sub, title_fontsize=8)
        else:
            ax2.text(0.5, 0.5, "not enough active history to simulate yet — keep flipping",
                     ha="center", va="center", transform=ax2.transAxes, fontsize=11, color=_MUTED)
        ax2.scatter([now_x], [equity_now], s=44, color=_EQUITY, zorder=5)
        ax2.annotate(f"you are here  ·  {now_x:.1f} active days in", (now_x, equity_now),
                     textcoords="offset points", xytext=(10, -2), fontsize=9, color=_EQUITY)
        ax2.set_xlabel("active days since you started  ·  idle gaps excluded  ·  Monte-Carlo forward paths")
        ax2.set_ylabel("net worth (gp, log)")
        ax2.set_title("Projection  ·  Monte-Carlo simulation from now", color=_INK, fontsize=12,
                      loc="left", pad=10)
        ax2.grid(axis="y", which="major", color=_GRID, lw=0.8)

        fig.tight_layout()
        fig.savefig(out_path, dpi=110, facecolor=_SURFACE)
        plt.close(fig)
    return out_path

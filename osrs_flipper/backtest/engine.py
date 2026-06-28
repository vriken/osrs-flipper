"""Backtest replay engine.

Per item: walk bars, let the strategy decide from data strictly before bar i, then
fill at bar i through the conservative model (price haircut, partial fills capped by
contra-volume, adverse-selection gate, mark-to-market bail-out on unsold units).
Runs the whole thing across β ∈ {0, 0.25, 0.5} so every result ships with its
sensitivity band rather than a single flattering number.
"""

from __future__ import annotations

import pandas as pd

from .. import api, config
from ..fills import capacity_units, fill_units, haircut_prices
from ..tax import post_tax_received
from .metrics import compute_metrics, format_metrics
from .strategies import get_strategy
from .strategies.base import EntryState, Strategy

BETAS = [0.0, 0.25, 0.5]
_RENAME = {
    "timestamp": "ts", "avgHighPrice": "avg_high", "avgLowPrice": "avg_low",
    "highPriceVolume": "high_vol", "lowPriceVolume": "low_vol",
}


def _timeseries_df(item_id: int, timestep: str) -> pd.DataFrame:
    pts = api.timeseries(item_id, timestep)
    if not pts:
        return pd.DataFrame()
    return pd.DataFrame(pts).rename(columns=_RENAME)


def _mid(ah: float, al: float) -> float:
    return (ah + al) / 2


def _simulate_item(strat: Strategy, hist: pd.DataFrame, *, item_id: int, limit: int,
                   capital: int, beta: float) -> list[dict]:
    n = len(hist)
    ah, al = hist["avg_high"], hist["avg_low"]
    hv, lv = hist["high_vol"].fillna(0), hist["low_vol"].fillna(0)
    ts = hist["ts"]
    al_ff = al.ffill()  # for MTM fallback only

    trades: list[dict] = []
    entry: EntryState | None = None
    i = strat.warmup
    while i < n:
        if entry is None:
            if not strat.should_enter(hist, i):
                i += 1
                continue
            a, b = ah.iloc[i], al.iloc[i]
            if pd.isna(a) or pd.isna(b) or b <= 0:
                i += 1
                continue
            # adverse gate: a resting buy fills when price is flat-or-down (sellers hit you)
            if config.ADVERSE_GATE and i > 0:
                pm = _mid(ah.iloc[i - 1], al.iloc[i - 1])
                if not pd.isna(pm) and _mid(a, b) > pm:
                    i += 1
                    continue
            buy_px, _ = haircut_prices(a, b, beta)
            target = capacity_units(limit, lv.iloc[i], capital, buy_px)
            units = fill_units(target, lv.iloc[i])
            if units <= 0:
                i += 1
                continue
            entry = EntryState(entry_idx=i, buy_px=buy_px, units=units,
                               metadata=strat.entry_metadata(hist, i))
            i += 1
            continue

        # in position — exit on signal or forced max_hold
        hold = i - entry.entry_idx
        if not (strat.should_exit(hist, i, entry) or hold >= strat.max_hold):
            i += 1
            continue

        a, b = ah.iloc[i], al.iloc[i]
        gate_ok = True
        if config.ADVERSE_GATE and i > 0:
            pm = _mid(ah.iloc[i - 1], al.iloc[i - 1])
            gate_ok = pd.isna(pm) or (not pd.isna(a) and _mid(a, b) >= pm)

        if not pd.isna(a) and not pd.isna(b) and gate_ok:
            _, sell_px = haircut_prices(a, b, beta)
            sold = fill_units(entry.units, hv.iloc[i])
        else:
            sell_px, sold = 0, 0
        remaining = entry.units - sold

        # bail-out: dump unsold units at the current instant-sell price (or last known)
        mtm_low = al_ff.iloc[i]
        mtm_px = int(round(mtm_low)) if not pd.isna(mtm_low) else entry.buy_px
        proceeds = sold * post_tax_received(sell_px, item_id=item_id) + \
            remaining * post_tax_received(mtm_px, item_id=item_id)
        cost = entry.units * entry.buy_px

        trades.append({
            "item_id": item_id, "entry_ts": int(ts.iloc[entry.entry_idx]), "exit_ts": int(ts.iloc[i]),
            "buy_px": entry.buy_px, "sell_px": sell_px, "units": entry.units, "sold": sold,
            "cost": cost, "proceeds": proceeds, "pnl": proceeds - cost,
            "hold_bars": hold, "completed": remaining == 0,
        })
        entry = None
        i += 1

    return trades


def run_backtest(strategy: str, *, timestep: str = "24h", top: int = 30, members: bool = False,
                 capital: int | None = None) -> dict[float, dict]:
    capital = capital or config.BACKTEST_BANKROLL
    mapping = api.mapping()
    hourly = api.one_hour()

    # watchlist = most liquid items (F2P-only unless --members), by 1h binding volume
    watch = []
    for r in mapping:
        if not members and r.get("members"):
            continue
        v = hourly.get(r["id"])
        if not v:
            continue
        vol = min(v.get("highPriceVolume") or 0, v.get("lowPriceVolume") or 0)
        watch.append((vol, r["id"], r.get("limit") or 0))
    watch.sort(reverse=True)
    watch = watch[:top]

    series = {}
    for _vol, iid, limit in watch:
        df = _timeseries_df(iid, timestep)
        if not df.empty and len(df) > get_strategy(strategy).warmup:
            series[iid] = (df, limit)

    by_beta: dict[float, dict] = {}
    for beta in BETAS:
        trades: list[dict] = []
        for iid, (df, limit) in series.items():
            strat = get_strategy(strategy)
            trades += _simulate_item(strat, df, item_id=iid, limit=limit, capital=capital, beta=beta)
        by_beta[beta] = compute_metrics(trades, timestep=timestep, capital=capital)

    print(format_metrics(by_beta, strategy=strategy, timestep=timestep, n_items=len(series)))
    return by_beta

"""Store-of-value screen — where to park capital near the cash cap.

This is NOT spread capture. Near MAX_LIQUID_GP you can't keep growing liquid cash (see wealth.py), so
capital has to sit in held assets. A good store is a quant risk/return call, not just "low volatility":

  * drift  μ  — mean daily log-return over the lookback (does it hold/appreciate, or bleed?)
  * risk   σ  — std of those returns (how much it swings while you hold)
  * Sharpe    — μ/σ, return per unit of risk
  * utility U — μ − ½·λ·σ²  (mean-variance; λ = STORE_RISK_AVERSION)

Cash is the baseline: U = 0, zero risk, zero nominal growth. A store must beat that (U > 0) to be worth
holding on merit — a stable asset that slowly bleeds is worse than cash. (Near the cap the glide forces
conversion anyway, because cash above the cap simply can't be held.) Liquidity (both-side gp/hour) is a
hard gate + soft tiebreak: you must be able to enter AND exit size without moving the price.
"""

from __future__ import annotations

import math
import statistics

from . import api, config

_VOL_FLOOR = 1e-4  # floor on σ for the Sharpe ratio, so a near-flat riser doesn't divide by ~0


def _mid(entry: dict) -> float | None:
    """Bar mid-price from averaged high/low; whichever side exists if only one does."""
    hi, lo = entry.get("avgHighPrice"), entry.get("avgLowPrice")
    if hi and lo:
        return (hi + lo) / 2
    return float(hi or lo) if (hi or lo) else None


def _turnover(entry: dict) -> float:
    """Both-side gp/hour from a /1h entry — liquidity by value, not unit count."""
    hi, lo = entry.get("avgHighPrice") or 0, entry.get("avgLowPrice") or 0
    return hi * (entry.get("highPriceVolume") or 0) + lo * (entry.get("lowPriceVolume") or 0)


def _returns_stats(series: list[float | None]) -> tuple[float, float] | None:
    """(μ, σ) of daily log-returns over the price series, or None if too short to estimate."""
    prices = [p for p in series if p and p > 0]
    if len(prices) < 12:
        return None
    rets = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    if len(rets) < 8:
        return None
    return statistics.fmean(rets), statistics.stdev(rets)


def rank_stores(mapping: list[dict] | None = None,
                latest: dict | None = None, hr: dict | None = None,
                *, top: int = 15) -> list[dict]:
    """Rank stable, deep, appreciating assets to park capital in.

    Universe is bounded to keep timeseries calls in check: items priced ≥ STORE_MIN_PRICE with
    ≥ STORE_MIN_TURNOVER both-side gp/hour, then the top STORE_CANDIDATES by turnover get a
    STORE_TIMESTEP series pulled to measure drift/vol. Rejects σ above STORE_MAX_VOL. Returns rows
    sorted best-first (by Sharpe), each with μ/σ/Sharpe/utility and a `worth_holding` (U>0) flag."""
    mapping = api.mapping() if mapping is None else mapping
    latest = api.latest() if latest is None else latest
    hr = api.one_hour() if hr is None else hr
    names = {m["id"]: m["name"] for m in mapping}
    members = {m["id"]: bool(m.get("members")) for m in mapping}

    # cheap pre-filter (no timeseries): high price AND deep both-side liquidity
    universe: list[tuple[int, float, float]] = []  # (id, price, turnover)
    for iid, h in hr.items():
        if config.MEMBERS and not members.get(iid, False):
            continue
        price = _mid(h) or ((latest.get(iid, {}) or {}).get("high") if latest else None)
        if not price or price < config.STORE_MIN_PRICE:
            continue
        turn = _turnover(h)
        if turn < config.STORE_MIN_TURNOVER:
            continue
        universe.append((iid, float(price), turn))
    universe.sort(key=lambda t: -t[2])  # deepest first — those are the ones worth analysing

    rows: list[dict] = []
    for iid, price, turn in universe[: config.STORE_CANDIDATES]:
        try:
            ts = api.timeseries(iid, config.STORE_TIMESTEP)
        except Exception:  # noqa: BLE001 — one bad item must not sink the whole screen
            continue
        stats = _returns_stats([_mid(b) for b in ts][-config.STORE_LOOKBACK:])
        if stats is None:
            continue
        mu, sigma = stats
        if sigma > config.STORE_MAX_VOL:
            continue  # too swingy to be a store
        sharpe = mu / max(sigma, _VOL_FLOOR)
        utility = mu - 0.5 * config.STORE_RISK_AVERSION * sigma * sigma
        cur = (latest.get(iid, {}) or {}) if latest else {}
        rows.append({
            "item_id": iid, "name": names.get(iid, str(iid)), "price": int(price),
            "buy_px": int(cur.get("low") or hr[iid].get("avgLowPrice") or price),
            "sell_px": int(cur.get("high") or hr[iid].get("avgHighPrice") or price),
            "turnover_1h": int(turn), "mu": mu, "sigma": sigma,
            "sharpe": sharpe, "utility": utility, "worth_holding": utility > 0,
        })
    # best risk-adjusted return first; deeper market breaks ties
    rows.sort(key=lambda r: (-r["sharpe"], -r["turnover_1h"]))
    return rows[:top]

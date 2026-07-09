"""Quantitative order pricing.

Solve for buy/sell limit prices, reporting the expected FILL % of each leg within a
horizon — so an over-sized order shows low fill probability instead of silently
disappearing. Fill rates are estimated empirically from recent /timeseries volume:

  buy fills  when sellers dump at ≤ b → rate_buy(b)  = Σ lowPriceVolume[avgLow ≤ b] / hours
  sell fills when buyers lift at ≥ s  → rate_sell(s) = Σ highPriceVolume[avgHigh ≥ s] / hours

  fill%_buy(b)  = min(1, α·rate_buy(b)·H / qty)      fraction of the order filled in H hours
  fill%_sell(s) = min(1, α·rate_sell(s)·H / qty)
  round%        = fill%_buy · fill%_sell             both legs complete in the horizon
  EV            = qty · (post_tax(s) − b) · round%   expected gp realised

Rank by EV. This is fill-intensity vs margin, grounded in real volume.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from . import api, config
from .fills import capacity_units, leg_fill_prob
from .tax import post_tax_received

_BAR_HOURS = {"5m": 1 / 12, "1h": 1.0, "6h": 6.0, "24h": 24.0}


def suggested_qty(item_id: int, buy_limit: int, bankroll: int) -> int:
    """Liquidity- and capital-aware default order size (matches the scanner's capacity)."""
    bid = api.latest().get(item_id, {}).get("low")
    v = api.one_hour().get(item_id, {})
    vol = min(v.get("highPriceVolume") or 0, v.get("lowPriceVolume") or 0)
    if not bid:
        return 0
    return capacity_units(buy_limit, vol, bankroll, bid)


@dataclass
class Quote:
    item_id: int
    name: str
    qty: int
    bid: int
    ask: int
    horizon_h: float
    buy_px: int
    sell_px: int
    net_unit: int
    p_buy: float
    p_sell: float
    p_round: float
    ev: float
    t_buy_h: float
    t_sell_h: float
    frontier: list[dict] = field(default_factory=list)


def _robust(bars: list[dict], key: str, n: int = 12) -> float | None:
    """Median of the last n bars for a price key — ignores single-bar glitches
    (e.g. a stray avgLow=10 on an item that trades at 40)."""
    vals = [b[key] for b in bars[-n:] if b.get(key)]
    return statistics.median(vals) if vals else None


def _rates(bars: list[dict], window_h: float):
    """Return callables rate_buy(b), rate_sell(s) in units/hour from historical volume."""
    lows = [(b["avgLowPrice"], b.get("lowPriceVolume") or 0) for b in bars if b.get("avgLowPrice")]
    highs = [(b["avgHighPrice"], b.get("highPriceVolume") or 0) for b in bars if b.get("avgHighPrice")]

    def rate_buy(price: float) -> float:
        return sum(v for p, v in lows if p <= price) / window_h if window_h else 0.0

    def rate_sell(price: float) -> float:
        return sum(v for p, v in highs if p >= price) / window_h if window_h else 0.0

    return rate_buy, rate_sell


def optimal_quote(
    item_id: int,
    qty: int,
    *,
    name: str | None = None,
    capture: float = config.ALPHA,
    timestep: str = config.PERSIST_TIMESTEP,
    horizon_h: float = 1.0,
    recent_bars: int = config.QUOTE_RECENT_BARS,
    target_fill_h: float | None = None,
) -> Quote | None:
    """Solve for the EV-maximising (buy, sell) prices, with per-leg fill probabilities.

    With `target_fill_h` (patient / overnight): instead of the EV-max price, bid the LOWEST price
    whose buy leg still fills within that window — so an overnight buy fills near morning at the
    best margin, not in 1-2h at a price you paid up for. Lets the bid drop BELOW the live bid."""
    if qty <= 0:
        return None
    bars = api.timeseries(item_id, timestep)
    if not bars:
        return None
    bars = bars[-recent_bars:]  # current regime only — stale volume must not price old levels
    window_h = len(bars) * _BAR_HOURS.get(timestep, 1.0)
    rate_buy, rate_sell = _rates(bars, window_h)

    # Price off the 1h average (stable, matches the scanner's build_features), falling back
    # to the live latest. Using live-only made quote return None whenever a side was momentarily
    # null/collapsed while the scanner (on 1h averages) still showed the item.
    cur = api.latest().get(item_id, {})
    hr = api.one_hour().get(item_id, {})
    clow, chigh = cur.get("low"), cur.get("high")
    if clow is not None and chigh is not None and clow > chigh:
        return None  # crossed/inverted live book → prices unreliable
    rbid, rask = _robust(bars, "avgLowPrice"), _robust(bars, "avgHighPrice")  # median, glitch-resistant
    med_bid = int(round(rbid)) if rbid is not None else None
    med_ask = int(round(rask)) if rask is not None else None
    # Anchor to the LIVE book — the price a fast fill actually gets right now. The recent-bar median
    # only (a) rejects the quote when it and the live book diverge too far (a glitch, a deflating pump,
    # or a price mid-swing we shouldn't flip into) and (b) fills in when a live side is missing.
    # Anchoring to the median instead lags on a trending item and quotes off the current market — e.g.
    # a buy above the live ask on a falling item, an illusory spread that never fills.
    bid = int(clow) if clow is not None else (med_bid if med_bid is not None else (hr.get("avgLowPrice") or cur.get("low")))
    ask = int(chigh) if chigh is not None else (med_ask if med_ask is not None else (hr.get("avgHighPrice") or cur.get("high")))
    if bid is None or ask is None:
        return None
    # reject when the live book sits far from the recent-bar median: a glitchy tick, a deflating pump,
    # or a price mid-move — no point estimate is trustworthy, so don't flip until it settles.
    if med_bid is not None and med_ask is not None:
        med_mid, live_mid = (med_bid + med_ask) / 2, (bid + ask) / 2
        if med_mid > 0 and abs(live_mid - med_mid) / med_mid > config.PRICE_DIVERGENCE_MAX:
            return None

    # Only quote marketable prices: a buy must sit in [bid, ask) and a sell in (bid, ask].
    # Quoting below the bid or above the ask isn't a fast fill — you'd just sit out of market.
    spread = int(ask) - int(bid)
    if spread < 1:
        return None  # no spread to capture
    step = max(1, spread // 30)
    # never recommend a buy below the current live bid: you'd sit out of market (it won't fill)
    # and the live monitor would rightly flag it "margin gone". Clamp the buy floor up to the
    # live bid when the book has moved above the 1h-median.
    buy_lo = int(bid)
    if clow is not None and int(clow) > buy_lo:
        buy_lo = min(int(clow), int(ask) - 1)
    # patient/overnight: let the bid drop BELOW the live bid, down to the recent floor (where sellers
    # have actually dumped), so we can pick a low, fat-margin bid that still fills by morning.
    grid_lo = buy_lo
    if target_fill_h is not None:
        lows = [b["avgLowPrice"] for b in bars if b.get("avgLowPrice")]
        if lows:
            grid_lo = max(1, min(int(min(lows)), buy_lo))
    buy_grid = range(grid_lo, int(ask), step)
    sell_grid = range(int(bid) + 1, int(ask) + 1, step)

    results = []
    for b in buy_grid:
        rb = rate_buy(b)
        if rb <= 0:
            continue
        p_buy = leg_fill_prob(qty, rb, horizon_h, capture=capture)
        for s in sell_grid:
            if s <= b:
                continue
            net = post_tax_received(s, item_id=item_id) - b
            if net <= 0:
                continue
            rs = rate_sell(s)
            if rs <= 0:
                continue
            p_sell = leg_fill_prob(qty, rs, horizon_h, capture=capture)
            p_round = p_buy * p_sell
            results.append({
                "buy": b, "sell": s, "net_unit": net,
                "p_buy": p_buy, "p_sell": p_sell, "p_round": p_round,
                "ev": qty * net * p_round,
                "t_buy_h": qty / (capture * rb), "t_sell_h": qty / (capture * rs),
            })

    if not results:
        return None
    results.sort(key=lambda r: -r["ev"])
    if target_fill_h is not None:
        # patient: the LOWEST bid that still fills the buy leg within the window (fattest margin that
        # completes by morning); ties broken by EV. If nothing fills in time, take the fastest bid.
        fillable = [r for r in results if r["t_buy_h"] <= target_fill_h]
        best = min(fillable, key=lambda r: (r["buy"], -r["ev"])) if fillable \
            else min(results, key=lambda r: r["t_buy_h"])
    else:
        best = results[0]
    return Quote(
        item_id=item_id, name=name or str(item_id), qty=qty, bid=buy_lo, ask=ask, horizon_h=horizon_h,
        buy_px=best["buy"], sell_px=best["sell"], net_unit=best["net_unit"],
        p_buy=best["p_buy"], p_sell=best["p_sell"], p_round=best["p_round"], ev=best["ev"],
        t_buy_h=best["t_buy_h"], t_sell_h=best["t_sell_h"], frontier=_frontier(results),
    )


def sell_frontier(item_id: int, qty: int, avg_cost: float, *, capture: float = config.ALPHA,
                  timestep: str = config.PERSIST_TIMESTEP, recent_bars: int = config.QUOTE_RECENT_BARS,
                  rows_max: int = 14) -> list[dict] | None:
    """Sell-side tradeoff for inventory you hold: at each list price, the estimated fill
    time (qty ÷ α·sell-rate) and profit vs avg cost. Higher price = more profit, slower fill."""
    bars = api.timeseries(item_id, timestep)
    if not bars:
        return None
    bars = bars[-recent_bars:]
    window_h = len(bars) * _BAR_HOURS.get(timestep, 1.0)
    _, rate_sell = _rates(bars, window_h)
    cur, hr = api.latest().get(item_id, {}), api.one_hour().get(item_id, {})
    rask = _robust(bars, "avgHighPrice")  # median, glitch-resistant
    ask = rask if rask is not None else (hr.get("avgHighPrice") or cur.get("high"))
    if not ask:
        return None
    ask = int(round(ask))
    lo, hi = ask - 2, ask + max(3, round(0.05 * ask))  # from just below the ask up to +5%
    step = max(1, (hi - lo) // rows_max)
    rows = []
    for s in range(lo, hi + 1, step):
        rs = rate_sell(s)
        eta = qty / (capture * rs) if rs > 0 else float("inf")
        net = post_tax_received(s, item_id=item_id) - avg_cost
        rows.append({"price": s, "eta_h": eta, "net_unit": int(net), "profit": net * qty})
    return rows


def _frontier(results: list[dict]) -> list[dict]:
    """One rung per margin level (the highest-EV quote at each net), sorted by margin."""
    best_per_net: dict[int, dict] = {}
    for r in results:
        cur = best_per_net.get(r["net_unit"])
        if cur is None or r["ev"] > cur["ev"]:
            best_per_net[r["net_unit"]] = r
    return [best_per_net[n] for n in sorted(best_per_net)]

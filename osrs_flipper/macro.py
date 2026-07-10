"""Market-wide macro signals — starting with bonds as the GP-inflation gauge.

An Old School Bond is bought with real money and sold on the GE for gp, so its GP price is the market's
read on how much gp is chasing a fixed real-money good — i.e. gold inflation. A rising bond price (in
gp) means gp is losing value, which strengthens the case for holding wealth in assets over cash (see
wealth.py / treasury.py). This module reads price, drift, and volatility for that signal; `go` shows it
so you can understand the regime. (Order flow would live here too, if the API ever exposed it — for now
the closest proxy is the per-item high/low volume split.)
"""

from __future__ import annotations

from . import api, config
from .treasury import _mid, _returns_stats

# Daily drift beyond ±this reads as inflating / deflating rather than stable (≈0.7%/wk at 0.001/day).
_INFLATION_EPS = 0.001
_LOOKBACK = 30  # 24h bars (~1 month) — enough to read the trend without chasing daily noise


def bond_signal() -> dict | None:
    """Bond gp-price macro read, or None if data is unavailable.

    Returns price (instant-buy gp), daily drift μ and volatility σ from log-returns, the ~weekly change,
    and a direction in {inflating, deflating, stable}. `inflating` = gp losing value = tilt toward assets."""
    cur = (api.latest().get(config.BOND_ITEM_ID) or {}).get("high")
    try:
        ts = api.timeseries(config.BOND_ITEM_ID, "24h")
    except Exception:  # noqa: BLE001 — macro is a nice-to-have; never break `go` over it
        ts = []
    prices = [p for p in (_mid(b) for b in ts) if p and p > 0][-_LOOKBACK:]
    price = int(cur or (prices[-1] if prices else 0)) or None
    if price is None:
        return None
    stats = _returns_stats(prices)
    if stats is None:
        return {"price": price, "mu": None, "sigma": None, "weekly": None, "direction": "unknown"}
    mu, sigma = stats
    # arrow, weekly figure, and direction ALL derive from the fitted drift μ, so they can never disagree
    # (a point-to-point 8-bar change vs a 30-bar μ contradicted each other). A rising bond gp-price = more
    # gp per bond = gp losing value = INFLATION (favours holding assets); falling = deflation (favours cash).
    weekly = mu * 7
    direction = "inflating" if mu > _INFLATION_EPS else "deflating" if mu < -_INFLATION_EPS else "stable"
    return {"price": price, "mu": mu, "sigma": sigma, "weekly": weekly, "direction": direction}


def bond_line() -> str | None:
    """One-line macro summary for the `go` header, or None if unavailable."""
    s = bond_signal()
    if not s:
        return None
    if s["mu"] is None:
        return f"macro: bond {s['price']:,}"
    arrow = "▲" if s["weekly"] > 0 else "▼" if s["weekly"] < 0 else "→"
    tag = {"inflating": "GP inflating — favours assets", "deflating": "GP deflating — favours cash",
           "stable": "GP stable", "unknown": ""}[s["direction"]]
    return (f"macro: bond {s['price']:,} {arrow} {s['weekly'] * 100:+.1f}%/wk · σ {s['sigma'] * 100:.1f}%/d"
            + (f" · {tag}" if tag else ""))

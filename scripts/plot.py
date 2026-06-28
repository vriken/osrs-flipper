"""Plot price history (bid/ask + volume) for items, straight from /timeseries.

    .venv/bin/python -m scripts.plot "Oak logs" "Jug of water" --timestep 1h

Saves a PNG per item under data/charts/ and prints the paths.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from osrs_flipper import api  # noqa: E402
from osrs_flipper.config import DATA_DIR  # noqa: E402

_RENAME = {"timestamp": "ts", "avgHighPrice": "avg_high", "avgLowPrice": "avg_low",
           "highPriceVolume": "high_vol", "lowPriceVolume": "low_vol"}


def _resolve(mapping: list[dict], token: str) -> dict | None:
    if token.isdigit():
        return next((r for r in mapping if r["id"] == int(token)), None)
    return next((r for r in mapping if r["name"].lower() == token.lower()), None)


def plot_item(meta: dict, timestep: str, out_dir) -> str | None:
    pts = api.timeseries(meta["id"], timestep)
    if not pts:
        print(f"  no data for {meta['name']}")
        return None
    df = pd.DataFrame(pts).rename(columns=_RENAME)
    df["t"] = [datetime.fromtimestamp(x, tz=UTC) for x in df["ts"]]

    fig, (ax, axv) = plt.subplots(2, 1, figsize=(11, 6), height_ratios=[3, 1], sharex=True)
    ax.plot(df["t"], df["avg_high"], color="#c0392b", lw=1.2, label="ask (sell into)")
    ax.plot(df["t"], df["avg_low"], color="#27ae60", lw=1.2, label="bid (buy at)")
    ax.fill_between(df["t"], df["avg_low"], df["avg_high"], color="#95a5a6", alpha=0.15)

    last_hi, last_lo = df["avg_high"].dropna().iloc[-1], df["avg_low"].dropna().iloc[-1]
    spread = last_hi - last_lo
    pct = spread / last_lo * 100 if last_lo else 0
    ax.set_title(f"{meta['name']}  —  bid {last_lo:,.0f} / ask {last_hi:,.0f}  "
                 f"(spread {spread:,.0f}, {pct:.1f}%)  ·  {timestep}")
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.2)
    ax.set_ylabel("price (gp)")

    axv.bar(df["t"], df["high_vol"].fillna(0) + df["low_vol"].fillna(0), width=0.02,
            color="#2980b9", alpha=0.6)
    axv.set_ylabel("volume")
    axv.grid(alpha=0.2)
    fig.tight_layout()

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{meta['name'].replace(' ', '_').replace('/', '-')}_{timestep}.png"
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return str(path)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("items", nargs="*", default=["Oak logs", "Jug of water", "Earth rune"])
    p.add_argument("--timestep", default="1h", choices=["5m", "1h", "6h", "24h"])
    a = p.parse_args()
    mapping = api.mapping()
    out_dir = DATA_DIR / "charts"
    for token in a.items:
        meta = _resolve(mapping, token)
        if not meta:
            print(f"  not found: {token}")
            continue
        path = plot_item(meta, a.timestep, out_dir)
        if path:
            print(path)


if __name__ == "__main__":
    main()

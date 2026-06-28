"""Seed history into DuckDB from /timeseries so backtests have data on day 1.

Per-item /timeseries caps at 365 points: 24h≈1yr, 6h≈3mo, 1h≈15d, 5m≈30h. Pulls
the most liquid items (F2P-only unless --members) at the requested timestep.

    .venv/bin/python -m scripts.bootstrap --timestep 24h --top 50
"""

from __future__ import annotations

import argparse
import time

from osrs_flipper import api
from osrs_flipper.store import Store


def _liquid_items(mapping: list[dict], hourly: dict, top: int, members: bool) -> list[int]:
    watch = []
    for r in mapping:
        if not members and r.get("members"):
            continue
        v = hourly.get(r["id"])
        if not v:
            continue
        vol = min(v.get("highPriceVolume") or 0, v.get("lowPriceVolume") or 0)
        watch.append((vol, r["id"]))
    watch.sort(reverse=True)
    return [iid for _v, iid in watch[:top]]


def bootstrap(*, timestep: str = "24h", top: int = 50, members: bool = False) -> None:
    mapping = api.mapping()
    hourly = api.one_hour()
    items = _liquid_items(mapping, hourly, top, members)
    with Store() as s:
        s.save_mapping(mapping)
        total = 0
        for iid in items:
            pts = api.timeseries(iid, timestep)
            total += s.save_timeseries(iid, timestep, pts)
            time.sleep(0.2)  # be gentle on the API
    print(f"bootstrapped {len(items)} items @ {timestep}: {total} points stored")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--timestep", default="24h", choices=["5m", "1h", "6h", "24h"])
    p.add_argument("--top", type=int, default=50)
    p.add_argument("--members", action="store_true")
    a = p.parse_args()
    bootstrap(timestep=a.timestep, top=a.top, members=a.members)

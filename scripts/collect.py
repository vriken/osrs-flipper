"""Snapshot current prices into DuckDB. Run on a 5-minute cron to accumulate the
fine-grained history the API itself only keeps ~30h of, so margin-flip can be
honestly backtested after a few weeks.

    */5 * * * *  cd /path/to/osrs-flipper && .venv/bin/python -m scripts.collect
"""

from __future__ import annotations

import time

from osrs_flipper import api
from osrs_flipper.store import Store


def collect_once() -> None:
    now = int(time.time())
    bucket = now - (now % 300)  # align to the 5m block so reruns dedup on the PK
    latest = api.latest()
    five = api.five_min()
    mapping = api.mapping()
    with Store() as s:
        n_map = s.save_mapping(mapping)
        n_lat = s.save_latest(latest, now)
        n_5m = s.save_prices(five, "5m", bucket)
    print(f"collected @ {bucket}: mapping={n_map} latest={n_lat} 5m={n_5m}")


if __name__ == "__main__":
    collect_once()

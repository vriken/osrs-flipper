"""DuckDB persistence for snapshots, mapping, and timeseries history.

One file at config.DB_PATH. All writes are idempotent (INSERT OR REPLACE on the
natural key) so re-running a collector or bootstrap never duplicates rows.

Tables
  mapping(item_id PK, name, buy_limit, value, highalch, members)
  prices(interval, ts, item_id, avg_high, avg_low, high_vol, low_vol)   -- 5m/1h snapshots
  latest(captured_at, item_id, high, high_time, low, low_time)          -- for staleness
  timeseries(item_id, timestep, ts, avg_high, avg_low, high_vol, low_vol)
"""

from __future__ import annotations

from typing import Any

import duckdb
import pandas as pd

from .config import DATA_DIR, DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mapping (
    item_id INTEGER PRIMARY KEY, name TEXT, buy_limit INTEGER,
    value BIGINT, highalch BIGINT, members BOOLEAN
);
CREATE TABLE IF NOT EXISTS prices (
    interval TEXT, ts BIGINT, item_id INTEGER,
    avg_high BIGINT, avg_low BIGINT, high_vol BIGINT, low_vol BIGINT,
    PRIMARY KEY (interval, ts, item_id)
);
CREATE TABLE IF NOT EXISTS latest (
    captured_at BIGINT, item_id INTEGER,
    high BIGINT, high_time BIGINT, low BIGINT, low_time BIGINT,
    PRIMARY KEY (captured_at, item_id)
);
CREATE TABLE IF NOT EXISTS timeseries (
    item_id INTEGER, timestep TEXT, ts BIGINT,
    avg_high BIGINT, avg_low BIGINT, high_vol BIGINT, low_vol BIGINT,
    PRIMARY KEY (item_id, timestep, ts)
);
"""


class Store:
    """Thin wrapper around a DuckDB connection. Use as a context manager."""

    def __init__(self, path: str | None = None):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(path or DB_PATH))
        self.con.execute(_SCHEMA)

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.con.close()

    # --- writes --------------------------------------------------------------
    def save_mapping(self, records: list[dict[str, Any]]) -> int:
        rows = [
            (r["id"], r.get("name"), r.get("limit"), r.get("value"), r.get("highalch"), r.get("members"))
            for r in records
        ]
        self.con.executemany(
            "INSERT OR REPLACE INTO mapping VALUES (?, ?, ?, ?, ?, ?)", rows
        )
        return len(rows)

    def save_prices(self, prices: dict[int, dict[str, Any]], interval: str, ts: int) -> int:
        rows = [
            (interval, ts, item_id, p.get("avgHighPrice"), p.get("avgLowPrice"),
             p.get("highPriceVolume"), p.get("lowPriceVolume"))
            for item_id, p in prices.items()
        ]
        self.con.executemany("INSERT OR REPLACE INTO prices VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    def save_latest(self, latest: dict[int, dict[str, Any]], captured_at: int) -> int:
        rows = [
            (captured_at, item_id, p.get("high"), p.get("highTime"), p.get("low"), p.get("lowTime"))
            for item_id, p in latest.items()
        ]
        self.con.executemany("INSERT OR REPLACE INTO latest VALUES (?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    def save_timeseries(self, item_id: int, timestep: str, points: list[dict[str, Any]]) -> int:
        rows = [
            (item_id, timestep, p["timestamp"], p.get("avgHighPrice"), p.get("avgLowPrice"),
             p.get("highPriceVolume"), p.get("lowPriceVolume"))
            for p in points
        ]
        self.con.executemany("INSERT OR REPLACE INTO timeseries VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    # --- reads ---------------------------------------------------------------
    def mapping_df(self) -> pd.DataFrame:
        return self.con.execute("SELECT * FROM mapping").df()

    def timeseries_df(self, item_id: int, timestep: str) -> pd.DataFrame:
        return self.con.execute(
            "SELECT ts, avg_high, avg_low, high_vol, low_vol FROM timeseries "
            "WHERE item_id = ? AND timestep = ? ORDER BY ts",
            [item_id, timestep],
        ).df()

    def prices_df(self, interval: str, item_id: int | None = None, since_ts: int | None = None) -> pd.DataFrame:
        sql = ("SELECT ts, item_id, avg_high, avg_low, high_vol, low_vol FROM prices "
               "WHERE interval = ?")
        params: list[Any] = [interval]
        if item_id is not None:
            sql += " AND item_id = ?"
            params.append(item_id)
        if since_ts is not None:
            sql += " AND ts >= ?"
            params.append(since_ts)
        sql += " ORDER BY ts"
        return self.con.execute(sql, params).df()

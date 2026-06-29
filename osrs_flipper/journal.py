"""Portfolio journal: cash, positions, and a realised-P&L trade log, in DuckDB.

Network-free (so it's unit-testable) — the terminal feeds it live prices for
mark-to-market. Cash is a single tracked balance; the ledger is append-only history.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

import duckdb

from .config import BUY_LIMIT_WINDOW_H, DATA_DIR, DB_PATH
from .tax import ge_tax, post_tax_received

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value DOUBLE);
CREATE TABLE IF NOT EXISTS positions (
    item_id INTEGER PRIMARY KEY, name TEXT, qty BIGINT, avg_cost DOUBLE
);
CREATE TABLE IF NOT EXISTS ledger (
    ts BIGINT, item_id INTEGER, name TEXT, side TEXT, qty BIGINT,
    price BIGINT, tax BIGINT, cash_delta DOUBLE, realized_pnl DOUBLE
);
CREATE TABLE IF NOT EXISTS predictions (
    ts BIGINT, item_id INTEGER, name TEXT, qty BIGINT,
    buy_px BIGINT, sell_px BIGINT, p_buy DOUBLE, p_sell DOUBLE, p_round DOUBLE, ev DOUBLE,
    source TEXT DEFAULT 'quote'
);
CREATE TABLE IF NOT EXISTS imported_offers (uuid TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY, ts BIGINT, item_id INTEGER, name TEXT, side TEXT,
    qty BIGINT, limit_px BIGINT, horizon_h DOUBLE,
    avg_low BIGINT, avg_high BIGINT, spread BIGINT, vol_1h_binding BIGINT,
    pred_p_fill DOUBLE, pred_eta_h DOUBLE, pred_ev DOUBLE,
    filled_qty BIGINT DEFAULT 0, fill_px DOUBLE, filled_ts BIGINT,
    status TEXT DEFAULT 'open'
);
"""


@dataclass
class Position:
    item_id: int
    name: str
    qty: int
    avg_cost: float


class Journal:
    def __init__(self, path: str | None = None):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(path or DB_PATH))
        self.con.execute(_SCHEMA)
        # migrate older journals that created `predictions` before `source` existed
        self.con.execute("ALTER TABLE predictions ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'quote'")

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.con.close()

    # --- cash ----------------------------------------------------------------
    def cash(self) -> float:
        row = self.con.execute("SELECT value FROM meta WHERE key='cash'").fetchone()
        return row[0] if row else 0.0

    def set_cash(self, amount: float) -> None:
        self.con.execute("INSERT OR REPLACE INTO meta VALUES ('cash', ?)", [amount])

    def _adjust_cash(self, delta: float) -> None:
        self.set_cash(self.cash() + delta)

    # --- positions -----------------------------------------------------------
    def position(self, item_id: int) -> Position | None:
        r = self.con.execute("SELECT item_id,name,qty,avg_cost FROM positions WHERE item_id=?",
                             [item_id]).fetchone()
        return Position(*r) if r else None

    def positions(self) -> list[Position]:
        rows = self.con.execute("SELECT item_id,name,qty,avg_cost FROM positions WHERE qty>0 ORDER BY name").fetchall()
        return [Position(*r) for r in rows]

    # --- trades --------------------------------------------------------------
    def record_buy(self, item_id: int, name: str, qty: int, price: int) -> float:
        """Log a buy fill. Returns cash spent. Buys are untaxed."""
        cost = qty * price
        pos = self.position(item_id)
        if pos:
            new_qty = pos.qty + qty
            new_avg = (pos.qty * pos.avg_cost + cost) / new_qty
        else:
            new_qty, new_avg = qty, float(price)
        self.con.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?)",
                         [item_id, name, new_qty, new_avg])
        self._adjust_cash(-cost)
        self.con.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?,?,?)",
                         [int(time.time()), item_id, name, "BUY", qty, price, 0, -cost, 0.0])
        return cost

    def record_sell(self, item_id: int, name: str, qty: int, price: int) -> tuple[float, float]:
        """Log a sell fill. Returns (net proceeds, realised pnl). Applies GE tax."""
        pos = self.position(item_id)
        avg_cost = pos.avg_cost if pos else 0.0
        sell_qty = min(qty, pos.qty) if pos else 0
        tax_unit = ge_tax(price, item_id=item_id)
        net_unit = price - tax_unit
        proceeds = sell_qty * net_unit
        realized = sell_qty * (net_unit - avg_cost)
        if pos:
            self.con.execute("UPDATE positions SET qty=qty-? WHERE item_id=?", [sell_qty, item_id])
        self._adjust_cash(proceeds)
        self.con.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?,?,?)",
                         [int(time.time()), item_id, name, "SELL", sell_qty, price,
                          tax_unit * sell_qty, proceeds, realized])
        return proceeds, realized

    # --- reporting -----------------------------------------------------------
    def import_offer(self, uuid: str, item_id: int, name: str, is_buy: bool,
                     qty: int, price: int) -> bool:
        """Record a RuneLite fill once. Returns True if newly imported, False if already seen."""
        if self.con.execute("SELECT 1 FROM imported_offers WHERE uuid = ?", [uuid]).fetchone():
            return False
        if is_buy:
            self.record_buy(item_id, name, qty, price)
        else:
            self.record_sell(item_id, name, qty, price)
        self.con.execute("INSERT INTO imported_offers VALUES (?)", [uuid])
        return True

    def units_bought_since(self, since_ts: int) -> dict[int, int]:
        """Units bought per item since `since_ts` (for buy-limit tracking)."""
        rows = self.con.execute(
            "SELECT item_id, SUM(qty) FROM ledger WHERE side='BUY' AND ts >= ? GROUP BY item_id",
            [since_ts],
        ).fetchall()
        return {int(r[0]): int(r[1]) for r in rows}

    def buy_limit_used(self, window_h: float = BUY_LIMIT_WINDOW_H) -> dict[int, int]:
        """Units bought per item within the rolling buy-limit window (default 4h)."""
        return self.units_bought_since(int(time.time()) - int(window_h * 3600))

    def realized_pnl(self) -> float:
        r = self.con.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM ledger").fetchone()
        return r[0]

    def inventory_value(self, bids: dict[int, int | None]) -> float:
        """Mark inventory at the post-tax instant-sell price (conservative bail value)."""
        total = 0.0
        for p in self.positions():
            bid = bids.get(p.item_id)
            if bid:
                total += p.qty * post_tax_received(bid, item_id=p.item_id)
        return total

    def equity(self, bids: dict[int, int | None]) -> float:
        return self.cash() + self.inventory_value(bids)

    def log_prediction(self, item_id: int, name: str, qty: int, buy_px: int, sell_px: int,
                       p_buy: float, p_sell: float, p_round: float, ev: float,
                       source: str = "quote") -> None:
        """Record what the model predicted at decision time, to calibrate against real fills later.

        source="buy" pairs a prediction with an actual entry (the gold calibration signal);
        source="quote" is a deliberate lookup you may or may not act on.
        """
        self.con.execute("INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         [int(time.time()), item_id, name, qty, buy_px, sell_px,
                          p_buy, p_sell, p_round, ev, source])

    def recent_predictions(self, n: int = 10) -> list[dict[str, Any]]:
        rows = self.con.execute(
            "SELECT ts,name,qty,buy_px,sell_px,p_round,ev,source FROM predictions ORDER BY ts DESC LIMIT ?", [n]
        ).fetchall()
        return [{"ts": r[0], "name": r[1], "qty": r[2], "buy_px": r[3], "sell_px": r[4],
                 "p_round": r[5], "ev": r[6], "source": r[7]} for r in rows]

    # --- order-attempt lifecycle (calibration) -------------------------------
    def record_attempt(self, item_id: int, name: str, side: str, qty: int, limit_px: int, *,
                       horizon_h: float, avg_low: int | None, avg_high: int | None,
                       vol_1h_binding: int, pred_p_fill: float | None = None,
                       pred_eta_h: float | None = None, pred_ev: float | None = None) -> str:
        """Record an order the user actually PLACED, with the decision-time market snapshot and
        model prediction. Reconciled against real fills later. Returns a short attempt id."""
        aid = uuid.uuid4().hex[:8]
        spread = (avg_high or 0) - (avg_low or 0)
        self.con.execute(
            "INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [aid, int(time.time()), item_id, name, side.upper(), qty, limit_px, horizon_h,
             avg_low, avg_high, spread, vol_1h_binding, pred_p_fill, pred_eta_h, pred_ev,
             0, None, None, "open"])
        return aid

    def reconcile_fill(self, item_id: int, is_buy: bool, qty: int, price: int,
                       fill_ts: int) -> str | None:
        """Attach a real fill to the oldest matching OPEN attempt (same item + side, placed
        before the fill). Marks it filled or partial; VWAPs the fill price. Returns the id."""
        side = "BUY" if is_buy else "SELL"
        row = self.con.execute(
            "SELECT attempt_id, qty, filled_qty, fill_px FROM attempts WHERE item_id=? AND side=? "
            "AND status IN ('open','partial') AND ts <= ? ORDER BY ts LIMIT 1",
            [item_id, side, fill_ts]).fetchone()
        if not row:
            return None
        aid, target_qty, prior_filled, prior_px = row[0], row[1], row[2] or 0, row[3] or 0.0
        new_filled = prior_filled + qty
        vwap = (prior_filled * prior_px + qty * price) / new_filled if new_filled else float(price)
        status = "filled" if new_filled >= target_qty else "partial"
        self.con.execute(
            "UPDATE attempts SET filled_qty=?, fill_px=?, filled_ts=?, status=? WHERE attempt_id=?",
            [new_filled, vwap, fill_ts, status, aid])
        return aid

    def expire_stale_attempts(self, now_ts: int) -> int:
        """Mark open attempts past their horizon as expired — these never-filled cases are
        first-class calibration data (they keep the fill-rate estimate from being optimistic).
        Returns how many were expired by this call."""
        n = self.con.execute(
            "SELECT COUNT(*) FROM attempts WHERE status='open' AND ts + horizon_h*3600 < ?",
            [now_ts]).fetchone()[0]
        if n:
            self.con.execute(
                "UPDATE attempts SET status='expired' WHERE status='open' AND ts + horizon_h*3600 < ?",
                [now_ts])
        return n

    def open_attempts(self) -> list[dict[str, Any]]:
        rows = self.con.execute(
            "SELECT attempt_id,item_id,name,side,qty,limit_px,filled_qty FROM attempts "
            "WHERE status='open' ORDER BY ts").fetchall()
        return [{"attempt_id": r[0], "item_id": r[1], "name": r[2], "side": r[3], "qty": r[4],
                 "limit_px": r[5], "filled_qty": r[6]} for r in rows]

    def calibration_rows(self) -> list[dict[str, Any]]:
        """Resolved attempts (filled / partial / expired) with snapshot + outcome for calibration."""
        cols = ["side", "qty", "limit_px", "avg_low", "avg_high", "spread", "vol_1h_binding",
                "pred_p_fill", "filled_qty", "fill_px", "status"]
        rows = self.con.execute(
            f"SELECT {','.join(cols)} FROM attempts WHERE status IN ('filled','partial','expired')"
        ).fetchall()
        return [dict(zip(cols, r, strict=True)) for r in rows]

    def recent(self, n: int = 10) -> list[dict[str, Any]]:
        rows = self.con.execute(
            "SELECT ts,name,side,qty,price,realized_pnl FROM ledger ORDER BY ts DESC LIMIT ?", [n]
        ).fetchall()
        return [{"ts": r[0], "name": r[1], "side": r[2], "qty": r[3], "price": r[4], "pnl": r[5]}
                for r in rows]

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
CREATE TABLE IF NOT EXISTS manual_fills (
    ts BIGINT, item_id INTEGER, name TEXT, is_buy BOOLEAN, qty BIGINT, price BIGINT
);
CREATE TABLE IF NOT EXISTS offer_progress (uuid TEXT PRIMARY KEY, accounted_qty BIGINT);
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
        self._repair_phantom_realized()

    def _repair_phantom_realized(self) -> int:
        """One-time: zero the realised P&L of sells that were booked with no cost basis — rows where
        realised == proceeds, an artefact of importing sells whose matching buy predated the journal.
        They counted the whole sale as profit and inflated realised P&L (cash was never affected).
        Idempotent via a meta flag; returns the gp removed so the caller can report it."""
        if self.con.execute("SELECT 1 FROM meta WHERE key='realized_repair_v1'").fetchone():
            return 0
        removed = self.con.execute(
            "SELECT COALESCE(sum(realized_pnl),0) FROM ledger "
            "WHERE side='SELL' AND cash_delta > 0 AND realized_pnl = cash_delta").fetchone()[0]
        self.con.execute("UPDATE ledger SET realized_pnl = 0 "
                         "WHERE side='SELL' AND cash_delta > 0 AND realized_pnl = cash_delta")
        self.con.execute("INSERT OR REPLACE INTO meta VALUES ('realized_repair_v1', ?)", [float(removed)])
        return int(removed)

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
        """Log a sell fill (full quantity). Returns (net proceeds, realised pnl). Applies GE tax.

        Records the FULL qty sold — never silently caps at the tracked position. Capping (the old
        `min(qty, pos.qty)`) silently dropped the excess when a sale's matching buy was imported
        out of order, leaving a phantom position. An over-sell now floors the position at 0;
        `reconcile_positions` is the authoritative correction from the full offer history."""
        pos = self.position(item_id)
        avg_cost = pos.avg_cost if pos else 0.0
        tax_unit = ge_tax(price, item_id=item_id)
        net_unit = price - tax_unit
        proceeds = qty * net_unit
        # No tracked cost basis (the buy predates the journal / was made off-device) → the profit is
        # unknowable, so book 0 realised rather than counting the ENTIRE sale as profit, which is what
        # inflated realised P&L. Cash still receives the full proceeds; only the P&L attribution is held.
        realized = qty * (net_unit - avg_cost) if avg_cost > 0 else 0.0
        new_qty = max(0, (pos.qty if pos else 0) - qty)
        self.con.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?)",
                         [item_id, name, new_qty, avg_cost if new_qty else 0.0])
        self._adjust_cash(proceeds)
        self.con.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?,?,?)",
                         [int(time.time()), item_id, name, "SELL", qty, price,
                          tax_unit * qty, proceeds, realized])
        return proceeds, realized

    def record_manual_fill(self, item_id: int, name: str, is_buy: bool, qty: int, price: int = 0) -> None:
        """Record a trade NOT in this device's RuneLite (an other-device trade or a `forget`), so
        the position reconcile folds it in and doesn't undo it from the RuneLite-only net."""
        self.con.execute("INSERT INTO manual_fills VALUES (?,?,?,?,?,?)",
                         [int(time.time()), item_id, name, is_buy, qty, price])

    def manual_fills(self) -> list:
        """Manual adjustments as Fill-shaped objects, for reconcile to fold in alongside RuneLite."""
        from .runelite import Fill
        rows = self.con.execute("SELECT item_id,name,is_buy,qty,price FROM manual_fills").fetchall()
        return [Fill(uuid="", item_id=r[0], name=r[1], is_buy=bool(r[2]), qty=r[3], price=r[4] or 0,
                     state="", t_ms=0) for r in rows]

    def forget_position(self, item_id: int, name: str, qty: int) -> None:
        """Untrack a position disposed of elsewhere: record a manual SELL of `qty` (so the reconcile
        keeps it gone) and drop the position now. Cash/P&L untouched."""
        self.record_manual_fill(item_id, name, is_buy=False, qty=qty)
        self.con.execute("DELETE FROM positions WHERE item_id=?", [item_id])

    def hold_position(self, item_id: int, name: str, qty: int, avg_cost: float) -> None:
        """Declare a holding acquired off this device (inverse of forget): record a manual BUY (so
        the reconcile keeps it) and set the position. Cash/P&L untouched — the gold was already
        spent elsewhere and your `bank` read already reflects it."""
        self.record_manual_fill(item_id, name, is_buy=True, qty=qty, price=int(round(avg_cost)))
        self.con.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?)",
                         [item_id, name, qty, float(avg_cost)])

    def sync_positions_to_bag(self, holdings: dict[int, int], fills) -> list[tuple[str, int, int]]:
        """Bag = ground truth for held QUANTITY (you keep flip stock in your bag). Set each position's
        qty to the bag amount, with avg_cost from the existing position (else from buy history, else
        0). Drop positions not in the bag. Idempotent and SELF-HEALING: it replaces the fragile
        manual_fills / dual-reconcile mechanism, so a momentarily-stale snapshot corrects on the next
        sync instead of corrupting permanently. Call ONLY with a live bag snapshot.

        Cash/realised P&L are untouched (cash is read live from coins; P&L from the sell ledger).
        Clears manual_fills — with the bag authoritative, the old forget/hold/reconcile-drop
        adjustments are obsolete and were the source of the suppression bug.

        `holdings` maps tradeable item_id → units held (local_export.holdings, noted-folded).
        `fills` is RuneLite's offer history (for the cost basis). Returns [(name, old, new)]."""
        bought: dict[int, int] = {}
        cost: dict[int, int] = {}
        name_of: dict[int, str] = {}
        for f in fills:
            name_of[f.item_id] = f.name
            if f.is_buy:
                bought[f.item_id] = bought.get(f.item_id, 0) + f.qty
                cost[f.item_id] = cost.get(f.item_id, 0) + f.qty * f.price
        cur = {p.item_id: p for p in self.positions()}
        changes = []
        for iid, qty in holdings.items():
            if qty <= 0:
                continue
            p = cur.get(iid)
            if p and p.qty == qty:
                continue  # already correct
            if not p and not bought.get(iid):
                continue  # in the bag but never bought through this journal — incidental junk
                          # (a stray vial, an off-device item); use `hold <item> <qty> <avg>` to track
            avg = p.avg_cost if p else cost[iid] / bought[iid]
            nm = (p.name if p else name_of.get(iid)) or str(iid)
            self.con.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?)", [iid, nm, qty, avg])
            changes.append((nm, p.qty if p else 0, qty))
        for iid, p in cur.items():
            if holdings.get(iid, 0) <= 0:
                self.con.execute("DELETE FROM positions WHERE item_id=?", [iid])
                changes.append((p.name, p.qty, 0))
        self.con.execute("DELETE FROM manual_fills")  # bag is authoritative now; drop stale adjustments
        return changes

    def reconcile_to_holdings(self, holdings: dict[int, int]) -> list[tuple[str, int, int]]:
        """Reduce each tracked position to what you ACTUALLY hold (bag + GE, from local_export), so
        positions left over from sells that never reached this device's RuneLite (window rolled over,
        or sold elsewhere) are cleared. The shortfall is logged as a manual SELL so the fills-reconcile
        keeps it gone rather than re-adding it. REDUCE-ONLY — never invents stock the journal doesn't
        know the cost of. Cash/P&L untouched (the gold is already reflected in your live coin balance).
        Returns [(name, old, new)] for the positions it corrected.

        Assumes flip stock lives in your bag, not the bank (bank is excluded). If you ever bank a
        holding, re-declare it with `own`."""
        drift = []
        for p in self.positions():
            real = holdings.get(p.item_id, 0)
            if real >= p.qty:
                continue
            drift.append((p.name, p.qty, real))
            self.record_manual_fill(p.item_id, p.name, is_buy=False, qty=p.qty - real)
            if real > 0:
                self.con.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?)",
                                 [p.item_id, p.name, real, p.avg_cost])
            else:
                self.con.execute("DELETE FROM positions WHERE item_id=?", [p.item_id])
        return drift

    def reconcile_positions(self, fills) -> list[tuple[str, int, int]]:
        """Recompute each held position from the authoritative offer history — Σbought − Σsold per
        item, which is ORDER-INDEPENDENT, so out-of-order incremental imports can't leave a phantom.
        Folds in manual_fills (other-device trades / forgets) so the RuneLite-only net doesn't undo
        them. Only touches items with a buy in the history; returns [(name, old_qty, new_qty)] for
        the ones that changed, surfacing the drift rather than silently fudging it.

        `fills` is RuneLite's full completed-offer list (runelite.completed_offers)."""
        agg: dict[int, dict[str, Any]] = {}
        for f in [*fills, *self.manual_fills()]:
            a = agg.setdefault(f.item_id, {"name": f.name, "bought": 0, "sold": 0, "cost": 0})
            if f.is_buy:
                a["bought"] += f.qty
                a["cost"] += f.qty * f.price
            else:
                a["sold"] += f.qty
        drift = []
        for iid, a in agg.items():
            if a["bought"] == 0:
                continue  # only sells in history → the buy predates RuneLite's window; can't
                          # trust the net, so leave the position rather than wrongly clear it
            new_qty = max(0, a["bought"] - a["sold"])
            cur = self.position(iid)
            old_qty = cur.qty if cur else 0
            if new_qty == old_qty:
                continue
            drift.append((a["name"], old_qty, new_qty))
            if new_qty > 0:
                avg = a["cost"] / a["bought"] if a["bought"] else 0.0
                self.con.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?)",
                                 [iid, a["name"], new_qty, avg])
            else:
                self.con.execute("DELETE FROM positions WHERE item_id=?", [iid])
        return drift

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

    def account_fill_delta(self, uuid: str, item_id: int, name: str, is_buy: bool,
                           cqit: int, price: int) -> int:
        """Credit/debit only the units newly filled since last sync for this offer, so a partially
        sold listing books as it sells — not only on completion. Tracks accounted cQIT per uuid.
        Migration-safe: a uuid already imported under the old all-or-nothing path counts as
        accounted, so it isn't re-credited. Returns the delta accounted (0 if none)."""
        row = self.con.execute("SELECT accounted_qty FROM offer_progress WHERE uuid=?", [uuid]).fetchone()
        if row is not None:
            prev = row[0]
        elif self.con.execute("SELECT 1 FROM imported_offers WHERE uuid=?", [uuid]).fetchone():
            prev = cqit  # legacy fully-imported offer → already accounted
        else:
            prev = 0
        delta = cqit - prev
        if delta > 0:
            (self.record_buy if is_buy else self.record_sell)(item_id, name, delta, price)
        self.con.execute("INSERT OR REPLACE INTO offer_progress VALUES (?,?)", [uuid, cqit])
        return max(0, delta)

    def migrate_fill_accounting_if_needed(self, fills) -> bool:
        """One-time baseline for incremental fill accounting: mark every currently-visible fill as
        already accounted, so existing partials aren't re-credited on top of your current `bank`
        read. Credits only deltas from here on. Returns True if it migrated this call."""
        if self.con.execute("SELECT 1 FROM meta WHERE key='fill_acct_v2'").fetchone():
            return False
        for f in fills:
            self.con.execute("INSERT OR REPLACE INTO offer_progress VALUES (?,?)", [f.uuid, f.qty])
        self.con.execute("INSERT OR REPLACE INTO meta VALUES ('fill_acct_v2', 1)")
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

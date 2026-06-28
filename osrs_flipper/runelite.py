"""Read-only view of live GE state from the RuneLite Flipping Utilities plugin.

Flipping Utilities (Belieal, v1.x) writes ~/.runelite/flipping/<account>.json — an
AccountData object. We read it (never write) to learn TRUE slot occupancy and active
offers, so the portfolio's free-slot count is observed instead of assumed. This is
consistent with ADR 0001: we observe state, execution stays manual.

Offer fields, decoded from a real file:
  b   = is-buy (True = buy offer)        id = item id          s  = GE slot index
  st  = state: BUYING/BOUGHT/SELLING/SOLD/CANCELLED_BUY/CANCELLED_SELL/EMPTY
  tQIT= quantity in the trade            p  = price (0 until fills)   t = unix ms
A slot is occupied iff its slotTimer carries a `currentOffer` (a filled-but-uncollected
offer still holds the slot until collected).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

FLIPPING_DIR = Path.home() / ".runelite" / "flipping"


@dataclass
class Offer:
    slot: int
    item_id: int
    is_buy: bool
    state: str
    qty: int
    price: int
    started_ms: int = 0
    filled: int = 0


@dataclass
class Fill:
    uuid: str
    item_id: int
    name: str
    is_buy: bool
    qty: int
    price: int
    state: str
    t_ms: int


def account_files() -> list[Path]:
    if not FLIPPING_DIR.exists():
        return []
    return [p for p in FLIPPING_DIR.glob("*.json") if p.stem != "accountwide"]


def latest_account_file() -> Path | None:
    """Most-recently-updated account file (handles multiple OSRS accounts)."""
    files = account_files()
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def read(path: Path | None = None) -> dict | None:
    """Parse the account JSON; None if RuneLite/Flipping Utilities data isn't present."""
    path = path or latest_account_file()
    if not path or not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, ValueError, OSError):
        return None


def active_offers(data: dict) -> list[Offer]:
    """In-progress offers occupying a slot, from slotTimers[*].currentOffer."""
    out = []
    for timer in data.get("slotTimers", []):
        off = timer.get("currentOffer")
        if not off:
            continue
        out.append(Offer(
            slot=off.get("s", timer.get("slotIndex", -1)),
            item_id=off.get("id", 0),
            is_buy=bool(off.get("b")),
            state=off.get("st", ""),
            qty=off.get("tQIT", 0),
            price=off.get("p", 0),
            started_ms=off.get("tradeStartedAt", 0),
            filled=off.get("cQIT", 0),
        ))
    return out


def margin_collapsed(live_net: float, avg_net: float | None) -> bool:
    """True if the currently-achievable flip margin has gone (≤0) or collapsed to a
    fraction of its recent-average — the market moved against the open offer."""
    if live_net <= 0:
        return True
    return avg_net is not None and avg_net > 0 and live_net < 0.3 * avg_net


def review_verdict(state: str, progress: float, elapsed_h: float, eta_h: float) -> str:
    """Advise on an active offer from time/progress alone (we don't get the offer price).
    Returns: collect | stale | slow | ontrack | done."""
    if state in ("BOUGHT", "SOLD"):
        return "collect"
    if progress >= 1:
        return "done"
    if eta_h and eta_h < float("inf") and elapsed_h > 2 * eta_h and progress < 0.5:
        return "stale"
    if eta_h and eta_h < float("inf") and elapsed_h > eta_h:
        return "slow"
    return "ontrack"


def occupied_slots(data: dict) -> int:
    return sum(1 for t in data.get("slotTimers", []) if t.get("currentOffer"))


def free_slots(data: dict, total: int) -> int:
    """Observed free GE slots = total usable slots − slots holding an active offer."""
    return max(0, total - occupied_slots(data))


_COMPLETED_STATES = {"BOUGHT", "SOLD", "CANCELLED_BUY", "CANCELLED_SELL"}


def completed_offers(data: dict) -> list[Fill]:
    """Filled buys/sells from trades[*].h.sO (each carries a uuid for idempotency).

    Includes the FILLED portion of cancelled offers — `cQIT` is what actually traded, so
    a fully-unfilled cancel (cQIT 0) is skipped while a partial cancel is captured.
    """
    out = []
    for trade in data.get("trades", []):
        name = trade.get("name", str(trade.get("id", "")))
        for off in trade.get("h", {}).get("sO", []):
            if off.get("st", "") not in _COMPLETED_STATES:
                continue
            qty = off.get("cQIT")  # actual filled quantity
            qty = qty if qty is not None else off.get("tQIT", 0)
            if qty <= 0 or not off.get("uuid"):
                continue
            out.append(Fill(
                uuid=off["uuid"], item_id=off.get("id", 0), name=name,
                is_buy=bool(off.get("b")), qty=int(qty), price=int(off.get("p", 0)),
                state=off.get("st", ""), t_ms=int(off.get("t", 0)),
            ))
    return out


def limit_used(data: dict, now_ms: int | None = None) -> dict[int, int]:
    """Per-item units bought in the current 4h buy-limit window, from the plugin's own
    counter (iBTLW) — more accurate than summing journal buys. Resets once past nGLR."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    out = {}
    for trade in data.get("trades", []):
        h = trade.get("h", {})
        used, reset = h.get("iBTLW", 0), h.get("nGLR", 0)
        if used and reset and now_ms < reset:
            out[int(trade["id"])] = int(used)
    return out

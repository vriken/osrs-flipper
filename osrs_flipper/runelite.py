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
        ))
    return out


def occupied_slots(data: dict) -> int:
    return sum(1 for t in data.get("slotTimers", []) if t.get("currentOffer"))


def free_slots(data: dict, total: int) -> int:
    """Observed free GE slots = total usable slots − slots holding an active offer."""
    return max(0, total - occupied_slots(data))

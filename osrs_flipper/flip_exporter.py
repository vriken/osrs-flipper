"""Reader for the Flip Exporter RuneLite plugin — the single canonical data source that replaces
Flipping Utilities + Local Data Exporter.

~/.runelite/flip-exporter/:
  * latest.json  — snapshot: cashOnHand (coins + platinum), inventory with NOTED items already folded
    onto their tradeable id, live GE offers (real listedPrice, qty, isBuy, placedAt, uuid).
  * history.json — persisted, deduped completed/cancelled fills (cost-basis + audit record).

The plugin resolves noted ids and emits real prices, so this needs none of the work-arounds the
old two-plugin readers carried. Offer *state* is BUYING/BOUGHT/SELLING/SOLD/CANCELLED_* — note
"BUY" is NOT a substring of "BOUGHT", so we key off the plugin's `isBuy` flag, never the state text.
Read-only (ADR 0001)."""

from __future__ import annotations

import json
from pathlib import Path

from .runelite import Fill, Offer

EXPORT_DIR = Path.home() / ".runelite" / "flip-exporter"
LATEST = EXPORT_DIR / "latest.json"
HISTORY = EXPORT_DIR / "history.json"


def available() -> bool:
    """True if the Flip Exporter plugin is installed and has written a snapshot."""
    return LATEST.exists()


def _read(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, ValueError, OSError):
        return None


def read(path: Path | None = None) -> dict | None:
    return _read(path or LATEST)


def read_history(path: Path | None = None) -> dict | None:
    return _read(path or HISTORY)


def _live(data: dict | None) -> bool:
    """Usable for cash/holdings: logged in with a loaded inventory."""
    if not data or data.get("gameState") not in (None, "LOGGED_IN"):
        return False
    return bool((data.get("inventory") or {}).get("loaded"))


def cash(data: dict | None) -> int | None:
    """Deployable gp on hand (coins + platinum×1000), or None if the snapshot isn't live."""
    if not _live(data):
        return None
    return int(data.get("cashOnHand", 0))


def offers(data: dict | None) -> list[dict]:
    return list((data or {}).get("offers") or [])


def _offer_key(o: dict) -> str:
    """Stable id for an offer across snapshots. Prefer the plugin uuid; fall back to slot+item for
    offers that predate the plugin (their uuid is null until they next change and the plugin sees it)."""
    return o.get("uuid") or f"s{o.get('slot')}:{o.get('id')}"


def active_offers(data: dict | None) -> list[Offer]:
    """Live GE offers as runelite.Offer objects — real prices, placement time, offer id."""
    out = []
    for o in offers(data):
        out.append(Offer(
            slot=int(o.get("slot", -1)),
            item_id=int(o.get("id", 0)),
            is_buy=bool(o.get("isBuy")),
            state=o.get("state") or "",
            qty=int(o.get("total", 0)),
            price=int(o.get("listedPrice", 0)),
            started_ms=int(o.get("placedAt", 0) or 0),
            filled=int(o.get("completed", 0)),
            uuid=o.get("uuid") or "",
        ))
    return out


def tied_gold(data: dict | None) -> int:
    """Gold locked in open offers but not in your coin pile, added back so equity stays continuous
    when cash is bare coins. The COMPLETED units of buys are counted as held stock (holdings +
    inventory_value), so only the still-reserved and uncollected legs go here:
        BUY  → listedPrice × remaining  (reserved for units not yet bought)
        SELL → spent                    (proceeds earned, not yet collected)."""
    total = 0
    for o in offers(data):
        if o.get("isBuy"):
            total += int(o.get("listedPrice", 0)) * int(o.get("remaining", 0))
        else:
            total += int(o.get("spent", 0))
    return total


def holdings(data: dict | None) -> dict[int, int] | None:
    """Units actually held per tradeable item: bag (non-coin inventory, already noted-resolved) +
    bought units on BUY offers (BUYING partials + BOUGHT uncollected) + unsold units on SELL offers.
    None unless the snapshot is live. Keys off `isBuy` — "BOUGHT" doesn't contain "BUY"."""
    if not _live(data):
        return None
    have: dict[int, int] = {}
    for it in (data.get("inventory") or {}).get("items") or []:
        have[int(it["id"])] = have.get(int(it["id"]), 0) + int(it.get("qty", 0))
    for o in offers(data):
        iid = int(o.get("id", 0))
        units = int(o.get("completed", 0)) if o.get("isBuy") else int(o.get("remaining", 0))
        if units:
            have[iid] = have.get(iid, 0) + units
    return have


def completed_offers(history: dict | None) -> list[Fill]:
    """Completed buys/sells from history.json. Price is the realised avg fill (spent/qty) — the
    accurate cost basis."""
    out = []
    for t in (history or {}).get("trades") or []:
        qty = int(t.get("qty", 0))
        if qty <= 0:
            continue
        out.append(Fill(
            uuid=t.get("uuid") or "", item_id=int(t.get("id", 0)), name=t.get("name", ""),
            is_buy=bool(t.get("isBuy")), qty=qty,
            price=int(t.get("avgPrice") or t.get("listedPrice", 0)),
            state=t.get("state", ""), t_ms=int(t.get("completedAt", 0) or 0),
        ))
    return out


def all_fills(data: dict | None, history: dict | None) -> list[Fill]:
    """Everything the journal accounts for, deduped by offer key: completed trades (history.json) +
    the filled units of currently-active offers (so collected stock books before an offer completes).
    completed grows monotonically, so the larger-completed record per key wins."""
    by_key: dict[str, Fill] = {}
    for f in completed_offers(history):
        by_key[f.uuid or f"{f.item_id}:{f.t_ms}"] = f
    for o in offers(data):
        completed = int(o.get("completed", 0))
        price = int(o.get("avgPrice") or o.get("listedPrice", 0))
        if completed <= 0 or price <= 0:
            continue
        key = _offer_key(o)
        prior = by_key.get(key)
        if prior and prior.qty >= completed:
            continue
        by_key[key] = Fill(uuid=key, item_id=int(o.get("id", 0)), name=o.get("name", ""),
                           is_buy=bool(o.get("isBuy")), qty=completed, price=price,
                           state=o.get("state", ""), t_ms=int(o.get("placedAt", 0) or 0))
    return list(by_key.values())

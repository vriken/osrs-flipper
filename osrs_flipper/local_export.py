"""Read-only view of the Local Data Exporter plugin (GoblinTek), which snapshots live account
state to ~/.runelite/local-data-exporter/latest.json every few game ticks.

We deliberately use only TWO things from it (bank is ignored — it holds non-flip junk):

  * coins on hand — inventory item 995, the deployable cash you actually see in game. Unlike the
    journal's reconstructed cash it already reflects placed buys (gold leaves your coins the instant
    you place a buy) and collected sells, so it needs no manual `bank` correction.
  * open GE offers — each carries `listedPrice` (the price YOU set, known even at 0% fill, which the
    Flipping Utilities file hides as 0 until it starts filling), so gold tied up in open offers can
    be valued exactly for a continuous net worth.

Read-only, like runelite.py: we observe state, execution stays manual (ADR 0001).
"""

from __future__ import annotations

import json
from pathlib import Path

from .runelite import Offer

EXPORT_DIR = Path.home() / ".runelite" / "local-data-exporter"
LATEST = EXPORT_DIR / "latest.json"
COINS_ID = 995
PLATINUM_ID = 13204  # platinum token = 1,000 gp; how cash is stacked once it grows
PLATINUM_GP = 1000


def read(path: Path | None = None) -> dict | None:
    """Parse the latest snapshot; None if the plugin isn't installed/running."""
    path = path or LATEST
    if not path or not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, ValueError, OSError):
        return None


def _inventory_fresh(data: dict) -> bool:
    """Coins(995) may only overwrite cash when the inventory snapshot is LIVE — loaded this session
    and not served from an old cache — and the client is logged in. A stale/cache read would show 0
    or yesterday's coins and silently zero out your cash."""
    if not data.get("inventoryLoaded") or data.get("inventoryFromCache"):
        return False
    state = data.get("gameState")
    return state in (None, "LOGGED_IN")


def _items(container: dict | None) -> list[dict]:
    """Items out of a container, whose `items` is a slot-keyed object ({"0": {...}}) — or a plain
    list if the plugin ever changes. Returns the item dicts either way."""
    items = (container or {}).get("items")
    if isinstance(items, dict):
        return list(items.values())
    return items or []


def coins(data: dict | None) -> int | None:
    """Deployable gp on hand = coins (995) + platinum tokens (13204 × 1,000 gp), or None if the
    inventory snapshot isn't live (so the caller leaves cash untouched rather than trusting a stale
    read). Platinum matters once wealth grows — coins alone would undercount the instant you convert."""
    if not data or not _inventory_fresh(data):
        return None
    gp = 0
    for it in _items(data.get("inventory")):
        if it.get("id") == COINS_ID:
            gp += int(it.get("quantity", 0))
        elif it.get("id") == PLATINUM_ID:
            gp += int(it.get("quantity", 0)) * PLATINUM_GP
    return gp  # 0 if live inventory holds neither → genuinely broke


def open_offers(data: dict | None) -> list[dict]:
    """Active GE offers (slot → offer), each with listedPrice/state/remainingQuantity/spent."""
    if not data:
        return []
    ge = data.get("grandExchange") or {}
    if not ge.get("loaded"):
        return []
    return list((ge.get("offers") or {}).values())


def canonical_id(iid: int, tradeable: set[int] | None) -> int:
    """Normalise a NOTED item id to its tradeable (unnoted) form. GE-bought items are collected to
    your bag as noted items, whose id is the unnoted id + 1 and is NOT in the tradeable mapping —
    so without this the journal (which tracks tradeable ids) never recognises collected stock."""
    if tradeable and iid not in tradeable and (iid - 1) in tradeable:
        return iid - 1
    return iid


def holdings(data: dict | None, tradeable_ids: set[int] | None = None) -> dict[int, int] | None:
    """Units you ACTUALLY hold per item (tradeable ids), the ground truth to reconcile journal
    positions against: bag (non-coin inventory) + unsold units in SELL offers + bought-but-
    uncollected units in BUY offers (the collect box). Noted bag items are folded onto their
    tradeable id via `tradeable_ids` so collected stock matches the journal.

    Bank is intentionally excluded — you keep flip stock in your bag, not the bank. Returns None
    unless BOTH the inventory snapshot is live AND the GE offers are loaded, so the caller never
    reconciles against a blank/stale read and wrongly drops real stock. Coins/platinum are skipped.

    The buy/sell legs may double-count a partially-collected offer (its completedQuantity stays while
    the collected units also sit in the bag) — harmless here: this is only used to find what's NOT
    held at all, and an over-count errs toward keeping a position, never dropping a real one."""
    if not data or not _inventory_fresh(data):
        return None
    ge = data.get("grandExchange") or {}
    if not ge.get("loaded"):
        return None
    have: dict[int, int] = {}
    for it in _items(data.get("inventory")):
        iid = it.get("id")
        if iid in (COINS_ID, PLATINUM_ID):
            continue
        have[canonical_id(iid, tradeable_ids)] = have.get(canonical_id(iid, tradeable_ids), 0) + int(it.get("quantity", 0))
    for o in open_offers(data):
        state = o.get("state") or ""
        iid = o.get("itemId")  # GE offers already carry the tradeable id
        if "SELL" in state:
            have[iid] = have.get(iid, 0) + int(o.get("remainingQuantity", 0))  # unsold, still yours
        elif "BUY" in state:
            have[iid] = have.get(iid, 0) + int(o.get("completedQuantity", 0))  # bought, not yet collected
    return have


def active_offers(data: dict | None) -> list[Offer]:
    """Active GE offers as runelite.Offer objects, read from client.getGrandExchangeOffers().

    Two advantages over the Flipping Utilities slotTimers: it carries the REAL listed price (FU
    reports 0 until an offer starts filling), and it survives a relog — the client repopulates your
    GE offers on login, whereas FU's slotTimers don't come back cleanly in a new session (which is
    why active orders 'disappear' after a restart). `started_ms` isn't exported, so it's 0 (= unknown
    age); the caller enriches it from FU when both are present."""
    out = []
    for o in open_offers(data):
        state = o.get("state") or ""
        out.append(Offer(
            slot=int(o.get("slot", -1)),
            item_id=int(o.get("itemId", 0)),
            is_buy="BUY" in state,
            state=state,
            qty=int(o.get("totalQuantity", 0)),
            price=int(o.get("listedPrice", 0)),
            started_ms=0,
            filled=int(o.get("completedQuantity", 0)),
            uuid="",
        ))
    return out


def tied_gold(data: dict | None) -> int:
    """Gold locked in open offers but not yet sitting in your coin pile, added back so net worth
    stays continuous when cash is read as bare coins:

      BUY  → spent + listedPrice × remainingQuantity
             spent = gold already paid for filled units (which active buys don't import into
             positions until they complete) — valued at cost, conservative; plus the reserve still
             held for the unfilled units. Together ≈ the full gold that left your coins at placement,
             so an open buy is equity-neutral rather than a phantom loss.
      SELL → spent
             proceeds already earned and awaiting collection. The unsold units stay in journal
             positions (marked to market in inventory_value), so only the earned gold is added here.

    Conservative by construction (filled buys at cost, no unrealised mark-up claimed). The one soft
    spot: a SELL's `spent` keeps counting gold you may have already collected into coins — so right
    after a collect this can briefly overstate until the offer clears. Cash-on-hand (coins) is exact
    regardless; this only colours the supplementary equity figure."""
    total = 0
    for o in open_offers(data):
        state = o.get("state") or ""
        spent = int(o.get("spent", 0))
        if "BUY" in state:
            total += spent + int(o.get("listedPrice", 0)) * int(o.get("remainingQuantity", 0))
        elif "SELL" in state:
            total += spent
    return total

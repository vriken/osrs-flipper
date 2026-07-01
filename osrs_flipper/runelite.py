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
    uuid: str = ""
    placement_observed: bool = True  # False → started_ms is first-seen, not a real placement time


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


def schema_health(data: dict | None) -> list[str]:
    """Detect plugin-format drift. The slot/limit/fill readers all use dict.get(...) with
    empty-collection defaults, so a renamed/missing key fails OPEN — `free_slots` reports
    every slot free, `limit_used` reports nothing used — and the tool confidently recommends
    trades that exceed your real slots/limits. Return human-readable warnings (empty = healthy)
    so the caller can fail loud instead. A genuinely idle account (no trades yet) is healthy:
    the keys exist and are empty; we only warn when an expected key is ABSENT."""
    if not data:
        return []
    warnings = []
    if "slotTimers" not in data:
        warnings.append("no 'slotTimers' key — free-slot detection is blind (assumes all slots free)")
    if "trades" not in data:
        warnings.append("no 'trades' key — fill sync and 4h buy-limit tracking are blind")
    return warnings


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
            uuid=off.get("uuid", ""),
        ))
    return out


def all_fills(data: dict, names: dict[int, str]) -> list[Fill]:
    """Every fill the journal should account for, deduped by uuid:
      - completed offers (full filled qty), and
      - active offers' PARTIAL fills (cQIT > 0), BUY and SELL — so units collected from a still-
        filling buy become a tracked position, and gold from a partially-sold listing is credited
        as it sells, not only when the whole offer completes.
    The same offer appears in slotTimers while filling and in trades once done; cQIT grows
    monotonically, so keeping the larger-cQIT record per uuid is correct. The fill price must be
    real (p > 0 — RuneLite reports 0 until an offer starts filling) so we never book a 0-price fill.
    (Importing active BUY fills is safe now that cash is read live from your coin balance rather than
    debited per fill.) Active offers carry no name, so it's resolved from `names`."""
    by_uuid: dict[str, Fill] = {f.uuid: f for f in completed_offers(data)}
    for timer in data.get("slotTimers", []):
        off = timer.get("currentOffer")
        if not off or off.get("st") not in ("BUYING", "SELLING"):
            continue
        u, cqit, price = off.get("uuid"), off.get("cQIT", 0), int(off.get("p", 0))
        if not u or cqit <= 0 or price <= 0:
            continue
        prior = by_uuid.get(u)
        if prior and prior.qty >= cqit:
            continue
        iid = off.get("id", 0)
        by_uuid[u] = Fill(uuid=u, item_id=iid, name=names.get(iid, str(iid)), is_buy=bool(off.get("b")),
                          qty=int(cqit), price=price, state=off.get("st", ""),
                          t_ms=int(off.get("t", 0)))
    return list(by_uuid.values())


def holdings_split(positions, offers) -> dict[int, dict]:
    """Split each owned item into BANK (collected, sellable now) vs IN-GE, from journal positions
    (net completed fills) + live offers:
      listed   = units tied up in active SELL offers (not yet sold → still in the position)
      incoming = still-UNFILLED units in active BUY offers (not yours yet). The FILLED units are
                 imported as a position by all_fills, so only the remainder counts as incoming.
      bank     = position − listed  (negative ⇒ journal/RuneLite drift)
    `positions` is a list of objects with .item_id/.name/.qty/.avg_cost; `offers` a list of Offer."""
    listed: dict[int, int] = {}
    incoming: dict[int, int] = {}
    for o in offers:
        if o.is_buy and o.state == "BUYING":
            # filled buy units are now imported as a position (all_fills), so only the UNFILLED
            # remainder is still "incoming" — counting o.filled here would double it.
            incoming[o.item_id] = incoming.get(o.item_id, 0) + max(0, o.qty - o.filled)
        elif not o.is_buy and o.state == "SELLING":
            listed[o.item_id] = listed.get(o.item_id, 0) + max(0, o.qty - o.filled)  # UNSOLD portion
            # (the sold part is already accounted out of the position via incremental fills)
    out: dict[int, dict] = {}
    for p in positions:
        ln = listed.get(p.item_id, 0)
        out[p.item_id] = {"name": p.name, "total": p.qty, "bank": p.qty - ln, "listed": ln,
                          "incoming": incoming.get(p.item_id, 0), "avg_cost": p.avg_cost}
    seen = {p.item_id for p in positions}
    for iid, q in incoming.items():  # being bought, nothing in the bank yet
        if iid not in seen:
            out[iid] = {"name": None, "total": 0, "bank": 0, "listed": 0, "incoming": q, "avg_cost": 0.0}
    return out


def margin_collapsed(live_net: float, avg_net: float | None) -> bool:
    """True if the currently-achievable flip margin has gone (≤0) or collapsed to a
    fraction of its recent-average — the market moved against the open offer."""
    if live_net <= 0:
        return True
    return avg_net is not None and avg_net > 0 and live_net < 0.3 * avg_net


def margin_alert(live_net: float, avg_net: float | None, elapsed_h: float, *,
                 min_age_h: float, floor: float) -> bool:
    """Should `review` warn that an open BUY's margin is gone? The live book is one noisy
    last-trade tick, so two guards stop false alarms before consulting margin_collapsed:
      * the order must be older than `min_age_h` — a real adverse move takes longer than the
        seconds since you placed it; under that, a ≤0 reading is just instantaneous noise;
      * the item's recent-average margin must exceed `floor` — on a 1gp penny spread a flicker
        to 0 is normal and not a loss worth a cancel."""
    if elapsed_h < min_age_h or avg_net is None or avg_net <= floor:
        return False
    return margin_collapsed(live_net, avg_net)


def review_verdict(state: str, progress: float, elapsed_h: float | None, eta_h: float,
                   observed: bool = True) -> str:
    """Advise on an active offer from time/progress alone (we don't get the offer price).
    Returns: collect | stale | slow | ontrack | open | done.

    When `observed` is False the placement wasn't witnessed (offer already open at login), so
    `elapsed_h` is a LOWER BOUND on the true age (time since first-seen), not the real age.
    stale/slow off a lower bound are still sound (true age ≥ elapsed_h), so we keep flagging them;
    but within the thresholds we return the neutral `open` instead of a false `ontrack`, because the
    true age could be anything up to unknown. `elapsed_h` None means we have no timestamp at all."""
    if state in ("BOUGHT", "SOLD"):
        return "collect"
    if progress >= 1:
        return "done"
    if elapsed_h is None:
        return "open"
    if eta_h and eta_h < float("inf") and elapsed_h > 2 * eta_h and progress < 0.5:
        return "stale"
    if eta_h and eta_h < float("inf") and elapsed_h > eta_h:
        return "slow"
    return "ontrack" if observed else "open"


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

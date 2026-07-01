"""Attention monitor: turn live RuneLite offers into verdicts, and (optionally) push a
Discord alert when an offer NEWLY needs you — so you can step away from the terminal.

`review_offers` is pure (data in, verdicts out) so both the terminal's `review`/`go` and the
background `watch_loop` share one source of truth. The loop only alerts on transitions into an
attention state and de-dupes until it clears, so it never spams the same fill every poll.
"""

from __future__ import annotations

import time

from . import api, config, datasource, runelite
from .tax import post_tax_received

# verdicts worth a push, with the phone-friendly reason shown in the alert
ATTENTION = {
    "collect": "COLLECT — filled, frees a slot",
    "margin": "MARGIN GONE — cancel & re-quote",
    "stale": "STALE — likely mispriced, re-price",
}


def review_offers(offers: list, hourly: dict, latest: dict, now_ms: int) -> list[tuple]:
    """Per offer: (Offer, verdict, elapsed_h, eta_h, progress). Pure — no IO."""
    out = []
    for o in offers:
        v = hourly.get(o.item_id, {})
        vol = (v.get("lowPriceVolume") if o.is_buy else v.get("highPriceVolume")) or 0
        rate = config.ALPHA * vol
        eta_h = o.qty / rate if rate > 0 else float("inf")
        # elapsed is None when we never witnessed the placement (an offer already open at login) —
        # started_ms is then just first-seen, so any age/staleness read would be false.
        elapsed_h = (now_ms - o.started_ms) / 3_600_000 if (o.started_ms and o.placement_observed) else None
        prog = o.filled / o.qty if o.qty else 0.0
        verdict = runelite.review_verdict(o.state, prog, elapsed_h, eta_h)
        # market-moved check (buys): is the round-trip margin still there? guarded against snapshot
        # noise on fresh orders / penny spreads (see runelite.margin_alert). Needs a real age for the
        # min-age guard, so skip it when the placement time is unknown.
        if verdict != "collect" and o.is_buy and elapsed_h is not None:
            lo = latest.get(o.item_id, {})
            lbid, lask = lo.get("low"), lo.get("high")
            if lbid and lask:
                live_net = post_tax_received(lask, item_id=o.item_id) - lbid
                abid, aask = v.get("avgLowPrice"), v.get("avgHighPrice")
                avg_net = post_tax_received(aask, item_id=o.item_id) - abid if (abid and aask) else None
                if runelite.margin_alert(live_net, avg_net, elapsed_h,
                                         min_age_h=config.REVIEW_MARGIN_MIN_AGE_H,
                                         floor=config.REVIEW_MARGIN_FLOOR):
                    verdict = "margin"
        out.append((o, verdict, elapsed_h, eta_h, prog))
    return out


def attention_events(rows: list[tuple]) -> dict[tuple[int, int], str]:
    """{(slot, item_id): verdict} for the offers that need action right now."""
    return {(o.slot, o.item_id): v for (o, v, *_rest) in rows if v in ATTENTION}


def diff_new(current: dict, alerted: dict) -> list[tuple[tuple[int, int], str]]:
    """Events present now whose verdict hasn't already been alerted (transition into attention)."""
    return [(k, v) for k, v in current.items() if alerted.get(k) != v]


def _live_attention(names: dict) -> dict[tuple[int, int], str]:
    offers = datasource.active().active_offers()
    rows = review_offers(offers, api.one_hour(), api.latest(), int(time.time() * 1000))
    return attention_events(rows)


def watch_loop(stop, *, interval_s: int | None = None, webhook: str | None = None) -> None:
    """Blocking poll loop (run in a daemon thread): push a Discord alert when an offer newly
    needs attention. Resilient — a transient API/RuneLite error never kills the loop. State
    that clears (offer collected / re-priced) is forgotten so a later re-entry alerts again."""
    from .alert import post_discord

    interval_s = interval_s or config.ALERT_POLL_S
    names: dict[int, str] = {}
    alerted: dict[tuple[int, int], str] = {}
    while not stop.is_set():
        try:
            if not names:
                names = {r["id"]: r["name"] for r in api.mapping()}
            current = _live_attention(names)
            for k in [k for k in alerted if k not in current]:
                del alerted[k]  # cleared → allow a future re-alert
            new = diff_new(current, alerted)
            if new:
                lines = [f"slot {slot}: {names.get(iid, iid)} — {ATTENTION[v]}"
                         for ((slot, iid), v) in new]
                ok, _detail = post_discord("\U0001f514 osrs-flipper needs you:\n" + "\n".join(lines), webhook)
                if ok:
                    alerted.update(dict(new))  # only mark sent on success → failed push retries
        except Exception:
            pass  # a watcher that dies silently is worse than one that retries next tick
        stop.wait(interval_s)

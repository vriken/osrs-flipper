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
        # elapsed = time since started_ms. When placement wasn't witnessed this is a LOWER BOUND on the
        # true age (started_ms is only first-seen); review_verdict treats it soundly. None if no stamp.
        elapsed_h = (now_ms - o.started_ms) / 3_600_000 if o.started_ms else None
        prog = o.filled / o.qty if o.qty else 0.0
        verdict = runelite.review_verdict(o.state, prog, elapsed_h, eta_h, observed=o.placement_observed)
        # market-moved check (buys): is the round-trip margin still there? guarded against snapshot
        # noise on fresh orders / penny spreads (see runelite.margin_alert). The min-age guard uses
        # elapsed as a floor, so it only fires once we've watched it long enough — sound either way.
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


_VERDICT_TAG = {"collect": "✅ collect", "margin": "🟠 margin gone", "stale": "🟡 re-price",
                "slow": "🐌 slow", "ontrack": "🟢 on track", "open": "· open", "done": "✅ done"}


def status_text(rows: list[tuple], names: dict, free: int) -> str:
    """A compact, always-current snapshot for the live status message: free slots + every offer + verdict."""
    lines = [f"osrs-flipper · {free} slot(s) free · {len(rows)} working"]
    for (o, v, _elapsed_h, eta_h, prog) in sorted(rows, key=lambda x: x[0].slot):
        eta = f"{eta_h:.1f}h" if eta_h and eta_h < 100 else "—"
        lines.append(f"  {o.slot} {str(names.get(o.item_id, o.item_id))[:16]:16} "
                     f"{'BUY' if o.is_buy else 'SELL':4} {prog:>3.0%} {eta:>5}  {_VERDICT_TAG.get(v, v)}")
    return "\n".join(lines)


def reprice_hint(o, latest: dict) -> str:
    """A concrete re-list target for a stuck offer, read off the live book — so a STALE alert tells you
    the NUMBER, not just 're-price'. A stale SELL drops to the current instabuy (high, still competitive);
    a stale BUY steps up to the current instasell (low). Empty when the live book isn't available."""
    lo = latest.get(o.item_id) or {}
    bid, ask = lo.get("low"), lo.get("high")
    if not bid or not ask:
        return ""
    tgt, verb = (bid, "re-bid") if o.is_buy else (ask, "re-list")
    return f"→ {verb} ~{int(tgt):,} (you're @ {int(o.price):,}; mkt {int(bid):,}↔{int(ask):,})"


def watch_loop(stop, *, interval_s: int | None = None, webhook: str | None = None) -> None:
    """Blocking poll loop (daemon thread), independent of `go`. Two outputs to Discord:
      • a LIVE STATUS message (bot only) edited in place each tick — current slots/offers/verdicts;
      • discrete ALERTS on transitions — an offer newly needs you (collect/margin/stale) or you PLACED
        a new offer. Deduped so it never re-pings the same state. Resilient: a transient error just
        retries next tick. Read-only (RuneLite + API) — never touches the journal, so no DB lock."""
    from .alert import bot_enabled, edit_bot, notify, post_bot

    interval_s = interval_s or config.ALERT_POLL_S
    names: dict[int, str] = {}
    alerted: dict[tuple[int, int], str] = {}
    seen_offers: set | None = None          # None until the first poll (don't alert existing offers as "placed")
    status_id: str | None = None
    last_status = ""
    while not stop.is_set():
        try:
            if not names:
                names = {r["id"]: r["name"] for r in api.mapping()}
            offers = datasource.active().active_offers()
            rows = review_offers(offers, api.one_hour(), api.latest(), int(time.time() * 1000))

            # live status message (bot only — webhooks can't edit): edit in place, only when it changed
            if bot_enabled():
                txt = status_text(rows, names, max(0, config.GE_SLOTS - len(offers)))
                if txt != last_status:
                    if status_id and edit_bot(status_id, txt):
                        last_status = txt
                    else:
                        ok, mid = post_bot(txt)
                        if ok:
                            status_id, last_status = mid, txt

            # discrete alert: you PLACED a new offer (transition into a slot we hadn't seen)
            keys = {(o.slot, o.item_id, "BUY" if o.is_buy else "SELL") for o in offers}
            if seen_offers is not None:
                placed = [k for k in keys - seen_offers]
                if placed:
                    lines = [f"placed {'BUY' if s == 'BUY' else 'SELL'} {names.get(iid, iid)} (slot {sl})"
                             for (sl, iid, s) in placed]
                    notify("\U0001f4e5 offer placed:\n" + "\n".join(lines))
            seen_offers = keys

            # discrete alert: an offer newly NEEDS you (collect / margin gone / stale)
            current = attention_events(rows)
            for k in [k for k in alerted if k not in current]:
                del alerted[k]  # cleared → allow a future re-alert
            new = diff_new(current, alerted)
            if new:
                lines = [f"slot {slot}: {names.get(iid, iid)} — {ATTENTION[v]}"
                         for ((slot, iid), v) in new]
                if notify("\U0001f514 osrs-flipper needs you:\n" + "\n".join(lines)):
                    alerted.update(dict(new))  # only mark sent on success → failed push retries
        except Exception:
            pass  # a watcher that dies silently is worse than one that retries next tick
        stop.wait(interval_s)

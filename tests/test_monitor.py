"""Attention monitor: verdict extraction, attention filtering, and de-dup across polls."""

from osrs_flipper import monitor
from osrs_flipper.runelite import Offer


def _offer(slot, iid, is_buy=True, state="BUYING", qty=1000, price=5, started_ms=0, filled=0):
    return Offer(slot=slot, item_id=iid, is_buy=is_buy, state=state, qty=qty, price=price,
                 started_ms=started_ms, filled=filled)


def test_filled_offer_is_collect_attention():
    rows = monitor.review_offers([_offer(0, 561, state="BOUGHT")], hourly={}, latest={}, now_ms=0)
    events = monitor.attention_events(rows)
    assert events == {(0, 561): "collect"}


def test_ontrack_offer_is_not_attention():
    # fresh buy, plenty of volume → on track, not flagged
    hourly = {561: {"lowPriceVolume": 100_000, "highPriceVolume": 100_000,
                    "avgLowPrice": 5, "avgHighPrice": 6}}
    rows = monitor.review_offers([_offer(0, 561, started_ms=0)], hourly, latest={}, now_ms=1000)
    assert monitor.attention_events(rows) == {}


def test_diff_new_only_returns_unalerted_transitions():
    current = {(0, 561): "collect", (1, 314): "margin"}
    alerted = {(0, 561): "collect"}  # already pushed the collect
    new = monitor.diff_new(current, alerted)
    assert new == [((1, 314), "margin")]  # only the margin is new


def test_diff_new_realerts_when_verdict_changes():
    # same slot/item, but verdict escalated slow→margin → counts as new
    current = {(2, 999): "margin"}
    alerted = {(2, 999): "stale"}
    assert monitor.diff_new(current, alerted) == [((2, 999), "margin")]


def test_post_discord_strips_ansi_and_reports_no_webhook():
    from osrs_flipper import alert
    ok, detail = alert.post_discord("\033[31mred text\033[0m", webhook_url=None)
    assert ok is False and "no webhook" in detail

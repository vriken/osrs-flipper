"""Attention monitor: verdict extraction, attention filtering, and de-dup across polls."""

from osrs_flipper import monitor
from osrs_flipper.runelite import Offer


def _offer(slot, iid, is_buy=True, state="BUYING", qty=1000, price=5, started_ms=0, filled=0,
           placement_observed=True):
    return Offer(slot=slot, item_id=iid, is_buy=is_buy, state=state, qty=qty, price=price,
                 started_ms=started_ms, filled=filled, placement_observed=placement_observed)


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


def test_unobserved_recent_offer_is_open_not_ontrack():
    # placement not witnessed + only just first-seen (tiny age floor) → can't claim on-track, and the
    # floor is too small to call stale → neutral `open`. Floor is known (not None); no attention.
    hourly = {561: {"lowPriceVolume": 100_000, "highPriceVolume": 100_000, "avgLowPrice": 5, "avgHighPrice": 6}}
    o = _offer(0, 561, started_ms=1_000, placement_observed=False)
    (row,) = monitor.review_offers([o], hourly, latest={}, now_ms=2_000)  # ~0.0003h floor
    _o, v, elapsed_h, _eta, _p = row
    assert v == "open" and elapsed_h is not None
    assert monitor.attention_events([row]) == {}


def test_unobserved_but_long_open_is_soundly_stale():
    # watched it far longer than its expected fill (floor >> eta) → stale is sound even unobserved:
    # the true age is at least the floor, so it's definitely overdue.
    hourly = {561: {"lowPriceVolume": 1, "highPriceVolume": 1, "avgLowPrice": 5, "avgHighPrice": 6}}
    o = _offer(0, 561, started_ms=1, placement_observed=False)
    (row,) = monitor.review_offers([o], hourly, latest={}, now_ms=10**12)
    _o, v, _e, _eta, _p = row
    assert v == "stale"


def test_observed_old_offer_still_flags_stale():
    # identical offer, placement WAS witnessed → real age → the stale read still fires.
    hourly = {561: {"lowPriceVolume": 1, "highPriceVolume": 1, "avgLowPrice": 5, "avgHighPrice": 6}}
    o = _offer(0, 561, started_ms=1, placement_observed=True)
    (row,) = monitor.review_offers([o], hourly, latest={}, now_ms=10**12)
    _o, v, elapsed_h, _eta, _p = row
    assert elapsed_h is not None and v == "stale"


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


def test_status_text_renders_free_slots_offers_and_verdicts():
    o = Offer(slot=2, item_id=561, is_buy=True, state="BUYING", qty=1000, price=5, filled=600)
    txt = monitor.status_text([(o, "ontrack", 0.5, 1.2, 0.6)], {561: "Air rune"}, free=3)
    assert "3 slot(s) free" in txt and "Air rune" in txt and "BUY" in txt and "60%" in txt


def test_reprice_hint_targets_the_competitive_side():
    sell = Offer(slot=2, item_id=561, is_buy=False, state="SELLING", qty=10, price=8000)
    hint = monitor.reprice_hint(sell, {561: {"low": 7600, "high": 7900}})
    assert "re-list ~7,900" in hint and "8,000" in hint          # stale SELL → drop to current instabuy
    buy = Offer(slot=1, item_id=561, is_buy=True, state="BUYING", qty=10, price=100)
    assert "re-bid ~7,600" in monitor.reprice_hint(buy, {561: {"low": 7600, "high": 7900}})
    assert monitor.reprice_hint(sell, {}) == ""                  # no live book → no hint

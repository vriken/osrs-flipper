"""RuneLite Flipping Utilities reader — parse offers and derive slot occupancy."""

from osrs_flipper import runelite

SAMPLE = {
    "slotTimers": [
        {"slotIndex": 0, "currentOffer": {"s": 0, "id": 1071, "b": True, "st": "BUYING", "tQIT": 17, "p": 0}},
        {"slotIndex": 1, "currentOffer": {"s": 1, "id": 1033, "b": True, "st": "BOUGHT", "tQIT": 8, "p": 50}},
        {"slotIndex": 2, "offerOccurredAtUnknownTime": False},  # free slot
        {"slotIndex": 3, "offerOccurredAtUnknownTime": False},  # free slot
    ],
    "trades": [],
}


def test_active_offers_parsed():
    offers = runelite.active_offers(SAMPLE)
    assert len(offers) == 2  # only slots with a currentOffer
    assert offers[0].item_id == 1071 and offers[0].is_buy and offers[0].qty == 17
    assert offers[1].state == "BOUGHT"  # filled-but-uncollected still occupies


def test_occupied_and_free_slots():
    assert runelite.occupied_slots(SAMPLE) == 2
    assert runelite.free_slots(SAMPLE, total=3) == 1  # F2P
    assert runelite.free_slots(SAMPLE, total=8) == 6  # members


def test_read_missing_file_returns_none(tmp_path):
    assert runelite.read(tmp_path / "nope.json") is None


def test_schema_health_passes_on_valid_data():
    assert runelite.schema_health(SAMPLE) == []
    assert runelite.schema_health(None) == []  # no data is not a schema problem
    assert runelite.schema_health({"slotTimers": [], "trades": []}) == []  # idle account is healthy


def test_schema_health_flags_missing_keys():
    # plugin renamed/dropped the keys we depend on → must warn (fail loud, not open)
    assert len(runelite.schema_health({"trades": []})) == 1  # no slotTimers
    assert len(runelite.schema_health({"foo": "bar"})) == 2  # neither key


TRADES = {
    "trades": [{
        "id": 2297, "name": "Anchovy pizza", "tGL": 10000,
        "h": {
            "sO": [{"uuid": "u1", "b": True, "id": 2297, "cQIT": 51, "p": 450,
                    "st": "BOUGHT", "tQIT": 51, "t": 1782679105000}],
            "iBTLW": 51, "nGLR": 9_999_999_999_999,
        },
    }],
}


def test_completed_offers_parsed():
    fills = runelite.completed_offers(TRADES)
    assert len(fills) == 1
    f = fills[0]
    assert f.item_id == 2297 and f.is_buy and f.qty == 51 and f.price == 450 and f.uuid == "u1"


def test_completed_offers_captures_partial_cancel_skips_unfilled():
    data = {"trades": [{"id": 1, "name": "X", "h": {"sO": [
        {"uuid": "a", "b": True, "id": 1, "cQIT": 5, "tQIT": 10, "p": 100, "st": "CANCELLED_BUY"},
        {"uuid": "b", "b": True, "id": 1, "cQIT": 0, "tQIT": 10, "p": 0, "st": "CANCELLED_BUY"},
    ]}}]}
    fills = runelite.completed_offers(data)
    assert len(fills) == 1  # only the partially-filled cancel
    assert fills[0].uuid == "a" and fills[0].qty == 5


def test_limit_used_from_plugin_counter():
    assert runelite.limit_used(TRADES, now_ms=0)[2297] == 51  # window active → counts


def test_limit_used_resets_past_window():
    assert runelite.limit_used(TRADES, now_ms=10**18) == {}  # past nGLR → reset


def test_margin_collapsed():
    assert runelite.margin_collapsed(-5, 100) is True   # exit now loses money
    assert runelite.margin_collapsed(0, 100) is True
    assert runelite.margin_collapsed(20, 100) is True   # 20 < 0.3×100 → collapsed vs recent
    assert runelite.margin_collapsed(50, 100) is False  # still healthy
    assert runelite.margin_collapsed(10, None) is False  # positive, no baseline → fine


def test_review_verdict():
    assert runelite.review_verdict("BOUGHT", 1.0, 5, 1) == "collect"
    assert runelite.review_verdict("BUYING", 0.0, 5.0, 1.0) == "stale"   # 5x over ETA, unfilled
    assert runelite.review_verdict("BUYING", 0.3, 1.5, 1.0) == "slow"    # past ETA
    assert runelite.review_verdict("BUYING", 0.2, 0.3, 1.0) == "ontrack"  # under ETA
    assert runelite.review_verdict("BUYING", 0.0, 99, float("inf")) == "ontrack"  # no ETA → no nag

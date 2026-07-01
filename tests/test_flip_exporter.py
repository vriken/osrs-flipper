"""Reader for the Flip Exporter plugin's unified latest.json + history.json."""

from osrs_flipper import flip_exporter as fe

LATEST = {
    "gameState": "LOGGED_IN",
    "cashOnHand": 532_618, "coins": 532_618, "platinum": 0,
    "inventory": {"loaded": True, "items": [
        {"slot": 0, "id": 2114, "noted": True, "qty": 1500, "name": "Pineapple"},
    ]},
    "offers": [
        {"slot": 0, "uuid": "u1", "state": "BUYING", "isBuy": True, "id": 561, "name": "Nature rune",
         "listedPrice": 100, "total": 1000, "completed": 300, "remaining": 700, "spent": 30_000,
         "avgPrice": 100, "placedAt": 111},
        {"slot": 1, "uuid": "u2", "state": "SELLING", "isBuy": False, "id": 235, "name": "Unicorn horn dust",
         "listedPrice": 533, "total": 690, "completed": 90, "remaining": 600, "spent": 47_000,
         "avgPrice": 520, "placedAt": 222},
        # a fully-BOUGHT, uncollected offer with NO uuid (predates the plugin) — must still count as held
        {"slot": 2, "uuid": None, "state": "BOUGHT", "isBuy": True, "id": 822, "name": "Mithril dart tip",
         "listedPrice": 130, "total": 3000, "completed": 3000, "remaining": 0, "spent": 390_000,
         "avgPrice": 130, "placedAt": 0},
    ],
}
HISTORY = {"trades": [
    {"uuid": "h1", "id": 2114, "name": "Pineapple", "isBuy": True, "qty": 1630, "avgPrice": 202,
     "listedPrice": 202, "spent": 329_260, "state": "BOUGHT", "completedAt": 999},
]}


def test_cash_is_cash_on_hand_when_live():
    assert fe.cash(LATEST) == 532_618
    assert fe.cash(None) is None
    assert fe.cash({**LATEST, "gameState": "LOGIN_SCREEN"}) is None
    assert fe.cash({**LATEST, "inventory": {"loaded": False}}) is None


def test_holdings_counts_bought_uncollected_via_isbuy_not_state_text():
    # the critical bug this guards: "BUY" is NOT a substring of "BOUGHT" — keying off state text
    # would drop the 3000 bought-uncollected Mithril dart tips.
    h = fe.holdings(LATEST)
    assert h == {2114: 1500, 561: 300, 235: 600, 822: 3000}
    assert fe.holdings({**LATEST, "inventory": {"loaded": False}}) is None


def test_tied_gold_buy_reserve_only_plus_sell_uncollected():
    # BUY 561: listed 100 × remaining 700 = 70_000 (NOT + spent — bought units are held stock).
    # BUY 822 (BOUGHT): listed 130 × remaining 0 = 0. SELL 235: spent 47_000.
    assert fe.tied_gold(LATEST) == 70_000 + 47_000


def test_active_offers_carry_real_price_uuid_placedat():
    o = {x.slot: x for x in fe.active_offers(LATEST)}
    assert o[0].is_buy and o[0].item_id == 561 and o[0].price == 100 and o[0].uuid == "u1" and o[0].started_ms == 111
    assert not o[1].is_buy and o[1].price == 533
    assert o[2].is_buy and o[2].state == "BOUGHT" and o[2].uuid == ""   # missing uuid → empty string


def test_all_fills_merges_history_and_active_partials_incl_uuidless():
    fills = {f.item_id: f for f in fe.all_fills(LATEST, HISTORY)}
    assert fills[2114].qty == 1630                    # from history
    assert fills[561].is_buy and fills[561].qty == 300  # active buy partial (uuid u1)
    assert fills[822].qty == 3000 and fills[822].price == 130  # BOUGHT, uuid-less → synthetic key


def test_completed_offers_use_avg_fill_price():
    fills = fe.completed_offers(HISTORY)
    assert len(fills) == 1 and fills[0].qty == 1630 and fills[0].price == 202


def test_read_missing_returns_none(tmp_path):
    assert fe.read(tmp_path / "nope.json") is None
    assert fe.read_history(tmp_path / "nope.json") is None

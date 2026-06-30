"""Local Data Exporter reader — live coins + gold tied in open offers."""

from osrs_flipper import local_export

FRESH = {
    "gameState": "LOGGED_IN",
    "inventoryLoaded": True,
    "inventoryFromCache": False,
    "inventory": {"items": {  # slot-keyed object, as the plugin actually writes it
        "0": {"slot": 0, "id": 995, "name": "Coins", "quantity": 40_147},
        "1": {"slot": 1, "id": 561, "name": "Nature rune", "quantity": 200},
    }},
    "grandExchange": {"loaded": True, "offers": {
        "0": {"slot": 0, "state": "BUYING", "itemId": 561, "listedPrice": 100,
              "totalQuantity": 1000, "completedQuantity": 300, "remainingQuantity": 700, "spent": 30_000},
        "1": {"slot": 1, "state": "SELLING", "itemId": 235, "listedPrice": 533,
              "totalQuantity": 690, "completedQuantity": 90, "remainingQuantity": 600, "spent": 47_000},
    }},
}


def test_read_missing_returns_none(tmp_path):
    assert local_export.read(tmp_path / "nope.json") is None


def test_coins_from_live_inventory():
    assert local_export.coins(FRESH) == 40_147


def test_coins_none_when_not_live():
    assert local_export.coins(None) is None
    assert local_export.coins({**FRESH, "inventoryLoaded": False}) is None
    assert local_export.coins({**FRESH, "inventoryFromCache": True}) is None  # stale cache read
    assert local_export.coins({**FRESH, "gameState": "LOGIN_SCREEN"}) is None  # logged out


def test_coins_zero_when_live_but_no_coin_slot():
    data = {**FRESH, "inventory": {"items": {"0": {"id": 561, "quantity": 5}}}}
    assert local_export.coins(data) == 0  # genuinely broke, not "unknown"


def test_coins_handles_list_shape_fallback():
    data = {**FRESH, "inventory": {"items": [{"id": 995, "quantity": 7}]}}
    assert local_export.coins(data) == 7  # tolerate a plain-list items if the plugin changes


def test_coins_includes_platinum_tokens():
    # 40_147 coins + 3 platinum tokens × 1_000 = 43_147 gp on hand
    data = {**FRESH, "inventory": {"items": {
        "0": {"id": 995, "quantity": 40_147},
        "1": {"id": 13204, "quantity": 3},
    }}}
    assert local_export.coins(data) == 40_147 + 3_000


def test_active_offers_map_to_offer_with_real_price():
    offers = {o.slot: o for o in local_export.active_offers(FRESH)}
    assert set(offers) == {0, 1}
    buy = offers[0]
    assert buy.is_buy and buy.item_id == 561 and buy.price == 100  # real listedPrice, not 0
    assert buy.qty == 1000 and buy.filled == 300
    sell = offers[1]
    assert not sell.is_buy and sell.state == "SELLING" and sell.price == 533


def test_active_offers_empty_without_data():
    assert local_export.active_offers(None) == []
    assert local_export.active_offers({"grandExchange": {"loaded": False}}) == []


def test_open_offers_gated_on_loaded():
    assert len(local_export.open_offers(FRESH)) == 2
    assert local_export.open_offers({"grandExchange": {"loaded": False, "offers": {"0": {}}}}) == []
    assert local_export.open_offers(None) == []


def test_tied_gold_buy_spent_plus_reserve_and_sell_uncollected():
    # BUY: spent 30_000 (filled, unimported) + listed 100 × remaining 700 = 70_000 → 100_000
    # SELL: spent (uncollected proceeds) 47_000
    assert local_export.tied_gold(FRESH) == (30_000 + 70_000) + 47_000


def test_tied_gold_zero_without_offers():
    assert local_export.tied_gold({"grandExchange": {"loaded": True, "offers": {}}}) == 0
    assert local_export.tied_gold(None) == 0

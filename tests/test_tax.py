"""Tax is correctness-critical and time-dependent — test the edges hard."""

import datetime as dt

from osrs_flipper.config import TAX_CAP
from osrs_flipper.tax import breakeven_sell, effective_tax_rate, ge_tax, post_tax_received

POST = dt.date(2025, 6, 1)  # after the 2% cutoff
PRE = dt.date(2025, 1, 1)  # 1% era
CUTOVER = dt.date(2025, 5, 29)


def test_sub_50gp_is_exempt():
    assert ge_tax(49, on_date=POST) == 0
    assert ge_tax(1, on_date=POST) == 0


def test_50gp_is_first_taxable():
    # 2% of 50 = 1
    assert ge_tax(50, on_date=POST) == 1


def test_breakeven_sell_covers_cost():
    # cost 80 (taxed ~2%): break-even is the lowest price whose post-tax proceeds reach 80
    be = breakeven_sell(80, on_date=POST)
    assert post_tax_received(be, on_date=POST) >= 80
    assert post_tax_received(be - 1, on_date=POST) < 80  # one tick lower is a loss


def test_breakeven_sell_low_price_no_tax():
    # sub-50gp is exempt → break-even equals the cost (no tax to cover)
    assert breakeven_sell(40, on_date=POST) == 40


def test_breakeven_sell_large_price_terminates_fast():
    # closed-form start keeps the loop O(1) even for a 1.8M-cost item
    be = breakeven_sell(1_800_000, on_date=POST)
    assert post_tax_received(be, on_date=POST) >= 1_800_000


def test_two_percent_floored():
    assert ge_tax(100, on_date=POST) == 2
    assert ge_tax(149, on_date=POST) == 2  # 2.98 -> floor 2
    assert ge_tax(150, on_date=POST) == 3


def test_one_percent_before_cutoff():
    assert ge_tax(1000, on_date=PRE) == 10  # 1%
    assert ge_tax(1000, on_date=POST) == 20  # 2%


def test_cutover_date_is_two_percent():
    assert ge_tax(1000, on_date=CUTOVER) == 20


def test_5m_cap_binds_on_expensive_items():
    # 2% of 250M = 5M exactly; anything above stays capped
    assert ge_tax(250_000_000, on_date=POST) == TAX_CAP
    assert ge_tax(1_000_000_000, on_date=POST) == TAX_CAP
    # just below the cap threshold is uncapped
    assert ge_tax(100_000_000, on_date=POST) == 2_000_000


def test_effective_rate_drops_above_cap():
    assert abs(effective_tax_rate(100_000_000, on_date=POST) - 0.02) < 1e-9
    assert effective_tax_rate(1_000_000_000, on_date=POST) == 0.005  # 5M / 1B


def test_exempt_item_pays_nothing():
    bond_id = 13190
    assert ge_tax(8_000_000, item_id=bond_id, on_date=POST) == 0


def test_common_exempt_staples_pay_no_tax():
    # Verified GE-tradeable members of the wiki exempt category (resolved via /mapping,
    # exact match). One representative per category — a regression guard on EXEMPT_ITEM_IDS.
    exempt = {
        558: "Mind rune", 882: "Bronze arrow", 886: "Steel arrow", 806: "Bronze dart",
        2309: "Bread", 379: "Lobster", 329: "Salmon", 2347: "Hammer", 1755: "Chisel",
        8007: "Varrock teleport (tablet)",
    }
    for iid, name in exempt.items():
        assert ge_tax(1000, item_id=iid, on_date=POST) == 0, name
        assert post_tax_received(1000, item_id=iid, on_date=POST) == 1000, name


def test_unknown_item_is_taxed_conservatively():
    assert ge_tax(1000, item_id=999_999, on_date=POST) == 20


def test_post_tax_received():
    assert post_tax_received(1000, on_date=POST) == 980
    assert post_tax_received(49, on_date=POST) == 49  # exempt, full amount

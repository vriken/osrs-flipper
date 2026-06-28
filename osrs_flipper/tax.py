"""Grand Exchange sell tax — correctness-critical, isolated, fully unit-tested.

Rules (verified against the OSRS Wiki):
  - Only the seller pays. Buyer pays the full listed price.
  - Rate is 2% of the sale price (was 1% before 2025-05-29), floored to whole gp.
  - Capped at 5,000,000 gp per item, regardless of price.
  - Items selling below 50 gp are exempt (the 2% would floor to 0 anyway).
  - Certain items are fully exempt (bonds + ~45 tools) — see config.EXEMPT_ITEM_IDS.

All gp values are integers; tax uses integer arithmetic to avoid float drift.
"""

from __future__ import annotations

import datetime as dt

from .config import (
    EXEMPT_ITEM_IDS,
    TAX_CAP,
    TAX_CHANGE_DATE,
    TAX_MIN_PRICE,
    TAX_RATE_DEN,
    TAX_RATE_NUM,
    TAX_RATE_NUM_OLD,
)


def ge_tax(price: int, *, item_id: int | None = None, on_date: dt.date | None = None) -> int:
    """Tax paid by the seller on a single item sold at `price`.

    Pass `on_date` to backtest historical trades with the correct rate; defaults to today.
    Unknown items are assumed taxed (conservative).
    """
    if price < TAX_MIN_PRICE:
        return 0
    if item_id is not None and item_id in EXEMPT_ITEM_IDS:
        return 0
    on_date = on_date or dt.date.today()
    rate_num = TAX_RATE_NUM if on_date >= TAX_CHANGE_DATE else TAX_RATE_NUM_OLD
    tax = price * rate_num // TAX_RATE_DEN  # integer floor
    return min(tax, TAX_CAP)


def post_tax_received(price: int, *, item_id: int | None = None, on_date: dt.date | None = None) -> int:
    """Net gp the seller actually receives after tax."""
    return price - ge_tax(price, item_id=item_id, on_date=on_date)


def effective_tax_rate(price: int, *, item_id: int | None = None, on_date: dt.date | None = None) -> float:
    """Realised tax as a fraction of price (drops below 2% once the 5M cap binds)."""
    if price <= 0:
        return 0.0
    return ge_tax(price, item_id=item_id, on_date=on_date) / price

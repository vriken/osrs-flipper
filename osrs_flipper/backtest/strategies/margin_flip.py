"""Margin-flip (market-making): capture the bid-ask spread on any bar with a
worthwhile relative spread, exit the next bar. This is a capacity game, not alpha —
on coarse timeseries it's a rough proxy; treat its live numbers as the scanner's
forward EV (see plan), and only trust a backtest once 5m self-collected data exists."""

from __future__ import annotations

import pandas as pd

from ... import config
from .base import EntryState, Strategy


class MarginFlip(Strategy):
    name = "margin_flip"
    warmup = 1
    max_hold = 1  # buy, sell next bar

    def __init__(self, min_margin_pct: float = config.MIN_MARGIN_PCT):
        self.min_margin_pct = min_margin_pct

    def should_enter(self, hist: pd.DataFrame, i: int) -> bool:
        ah, al = hist["avg_high"].iloc[i - 1], hist["avg_low"].iloc[i - 1]
        if pd.isna(ah) or pd.isna(al) or al <= 0:
            return False
        return (ah - al) / al >= self.min_margin_pct

    def should_exit(self, hist: pd.DataFrame, i: int, entry: EntryState) -> bool:
        return True  # max_hold=1 forces the sell on the next bar regardless

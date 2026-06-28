"""Strategy base class and shared types for the backtest engine.

A strategy makes ENTRY/EXIT decisions only from data strictly before the current
bar (`hist.iloc[:i]`) — the engine fills at bar `i`. This decision-bar ≠ fill-bar
split is what prevents same-bar look-ahead bias (the #1 backtest footgun).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class EntryState:
    """Bookkeeping for an open position carried from entry to exit."""

    entry_idx: int
    buy_px: int
    units: int
    metadata: dict = field(default_factory=dict)


class Strategy(ABC):
    """Long-only flip strategy: buy, then sell. Decisions use only past bars."""

    name: str = "base"
    warmup: int = 1  # bars of history required before the first decision
    max_hold: int = 24  # force-exit (and mark-to-market) after this many bars

    @abstractmethod
    def should_enter(self, hist: pd.DataFrame, i: int) -> bool:
        """Enter at bar i? May read only hist.iloc[:i] (strictly before i)."""

    @abstractmethod
    def should_exit(self, hist: pd.DataFrame, i: int, entry: EntryState) -> bool:
        """Exit at bar i? May read only hist.iloc[:i] (strictly before i)."""

    def entry_metadata(self, hist: pd.DataFrame, i: int) -> dict:
        """Optional per-entry state to carry to exit (e.g. a breakout level)."""
        return {}

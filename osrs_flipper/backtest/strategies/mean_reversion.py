"""Mean-reversion: buy when the instant-sell price is unusually low vs its recent
mean (z-score), exit when it reverts. Signal is on `avg_low` (the price you buy at),
not the mid, so a wide spread doesn't contaminate it."""

from __future__ import annotations

import pandas as pd

from ... import config
from .base import EntryState, Strategy


class MeanReversion(Strategy):
    name = "mean_reversion"

    def __init__(self, z_entry: float = config.Z_ENTRY, z_exit: float = config.Z_EXIT,
                 window: int = config.Z_WINDOW):
        self.z_entry = z_entry
        self.z_exit = z_exit
        self.window = window
        self.warmup = window + 1
        self.max_hold = window  # don't hold a failed reversion forever

    def _zscore(self, hist: pd.DataFrame, i: int) -> float | None:
        sub = hist["avg_low"].iloc[:i].dropna()
        if len(sub) < self.window + 1:
            return None
        ref = sub.iloc[-self.window - 1:-1]
        mu, sd = ref.mean(), ref.std(ddof=1)
        if not sd or sd < 1e-9 * max(mu, 1):  # flat/illiquid → no honest signal
            return None
        return (sub.iloc[-1] - mu) / sd

    def should_enter(self, hist: pd.DataFrame, i: int) -> bool:
        z = self._zscore(hist, i)
        return z is not None and z < -self.z_entry

    def should_exit(self, hist: pd.DataFrame, i: int, entry: EntryState) -> bool:
        z = self._zscore(hist, i)
        return z is not None and z >= -self.z_exit

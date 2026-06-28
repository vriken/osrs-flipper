"""Momentum / breakout: buy when price breaks above its recent range on a volume
spike (often update-driven structural repricings), exit when it falls back in range."""

from __future__ import annotations

import pandas as pd

from ... import config
from .base import EntryState, Strategy


class Momentum(Strategy):
    name = "momentum"

    def __init__(self, range_bars: int = config.BREAKOUT_RANGE_DAYS, vol_z: float = config.VOL_Z_BREAKOUT):
        self.range_bars = range_bars
        self.vol_z = vol_z
        self.warmup = range_bars + 2
        self.max_hold = range_bars

    def _range_high(self, hist: pd.DataFrame, i: int) -> float | None:
        ref = hist["avg_high"].iloc[max(0, i - 1 - self.range_bars):i - 1].dropna()
        return ref.max() if len(ref) >= self.range_bars else None

    def should_enter(self, hist: pd.DataFrame, i: int) -> bool:
        rng_high = self._range_high(hist, i)
        if rng_high is None:
            return False
        last_high = hist["avg_high"].iloc[i - 1]
        if pd.isna(last_high) or last_high <= rng_high:
            return False
        vol = (hist["high_vol"].fillna(0) + hist["low_vol"].fillna(0)).iloc[:i]
        ref = vol.iloc[-self.range_bars - 1:-1]
        mu, sd = ref.mean(), ref.std(ddof=1)
        if not sd:
            return False
        return (vol.iloc[-1] - mu) / sd > self.vol_z

    def entry_metadata(self, hist: pd.DataFrame, i: int) -> dict:
        return {"breakout_level": self._range_high(hist, i)}

    def should_exit(self, hist: pd.DataFrame, i: int, entry: EntryState) -> bool:
        level = entry.metadata.get("breakout_level")
        if level is None:
            return False
        last_low = hist["avg_low"].iloc[i - 1]
        return not pd.isna(last_low) and last_low < level

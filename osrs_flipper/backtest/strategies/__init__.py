"""Strategy registry."""

from __future__ import annotations

from .base import Strategy
from .margin_flip import MarginFlip
from .mean_reversion import MeanReversion
from .momentum import Momentum

REGISTRY: dict[str, type[Strategy]] = {
    "mean_reversion": MeanReversion,
    "momentum": Momentum,
    "margin_flip": MarginFlip,
}


def get_strategy(name: str) -> Strategy:
    if name not in REGISTRY:
        raise ValueError(f"unknown strategy {name!r}; choose from {sorted(REGISTRY)}")
    return REGISTRY[name]()

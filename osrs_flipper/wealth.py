"""Wealth-cap glide.

OSRS caps a coin stack at 2^31-1; platinum tokens hold the overflow, so the real "can't hold more
liquid value" ceiling is max coins + max platinum (config.MAX_LIQUID_GP). As net worth approaches it,
recycling cash through fast flips stops compounding — you can't grow a maxed number — so new capital
has to move into held assets (see store.py). The glide factor is how far into that regime you are:
0 below CAP_GLIDE_START_FRAC of the cap, ramping linearly to 1 at the cap. It's the single dial the
`go` plan uses to tilt deployment from flips toward stores-of-value.
"""

from __future__ import annotations

from . import config


def glide_factor(net_worth: float,
                 cap: float | None = None, start_frac: float | None = None) -> float:
    """Fraction (0..1) of the way from the glide-start threshold to the liquid cap.

    0 while net worth is below start_frac·cap; ramps linearly to 1 at the cap (and stays 1 above it).
    Pure (config injected) so it's unit-testable and cheap to call every `go`."""
    cap = config.MAX_LIQUID_GP if cap is None else cap
    start_frac = config.CAP_GLIDE_START_FRAC if start_frac is None else start_frac
    if cap <= 0 or net_worth <= 0:
        return 0.0
    start = start_frac * cap
    if net_worth <= start:
        return 0.0
    if net_worth >= cap:
        return 1.0
    return (net_worth - start) / (cap - start)

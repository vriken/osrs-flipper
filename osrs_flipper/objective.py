"""Adaptive ranking objective — trend-based competition detector.

What we optimise depends on the competition regime:
  * Low / stable competition → raw gp/hour: grab throughput from the uncrowded niches while they last.
  * Competition arriving → variance-penalised gp/hour: steadier, higher-completion flips, because the
    easy edge is compressing and consistency compounds better than a lumpy gamble.

We detect "competition arriving" from a RISE in our own realized Sharpe (per-active-day return ÷ its
volatility) ABOVE its trailing baseline — not its absolute level. The absolute level conflates "the
market got efficient/crowded" with "I'm simply good at a small stack"; a rise vs a slow EWMA baseline
isolates the regime CHANGE. So a persistently high Sharpe becomes the new normal (baseline catches up,
λ relaxes), and only a fresh climb engages risk-aversion. The mechanism is one dial — the variance
aversion λ weighting the ranking SCORE by p_complete^λ (fill-rate-driven, so reliable flips win as λ
grows). Fill-rate accuracy is never traded off here: it's applied upstream on the EV inputs via the
fill calibration; this only decides how hard to additionally punish variance.

The causality (Sharpe-rise → competition) is a hypothesis, not a law — competition compresses both the
mean and the variance. Hence everything is config-tunable and the tilt is fully off when
VARIANCE_AVERSION_MAX == 0.
"""

from __future__ import annotations

from . import config


def realized_sharpe(rate: float | None, vol: float | None) -> float | None:
    """Per-active-day Sharpe from fit_growth's (rate, vol), or None when it can't be formed."""
    if not rate or not vol or vol <= 0:
        return None
    return rate / vol


def update_baseline(current: float | None, prior: float | None, *, alpha: float | None = None) -> float | None:
    """Slow EWMA of the Sharpe baseline. Seeds with the first real reading, then tracks it slowly so a
    sustained regime becomes the new normal. `current` None (no signal) leaves the baseline unchanged."""
    alpha = config.OBJ_BASELINE_ALPHA if alpha is None else alpha
    if current is None:
        return prior
    if prior is None:
        return current
    return alpha * current + (1 - alpha) * prior


def variance_aversion(current_sharpe: float | None, baseline: float | None, *,
                      base: float | None = None, rise_full: float | None = None,
                      lam_max: float | None = None) -> float:
    """Variance-aversion λ from the RISE of current Sharpe above its baseline.

    λ = base (the VARIANCE_AVERSION floor) while Sharpe is at/below baseline (no competition signal);
    ramps linearly to `lam_max` as the rise reaches `rise_full`. Never below the floor, so a manually
    set VARIANCE_AVERSION is always honoured; fully off when lam_max == 0."""
    base = config.VARIANCE_AVERSION if base is None else base
    rise_full = config.OBJ_SHARPE_RISE_FULL if rise_full is None else rise_full
    lam_max = config.VARIANCE_AVERSION_MAX if lam_max is None else lam_max
    if current_sharpe is None or baseline is None or lam_max <= 0 or rise_full <= 0:
        return base
    rise = current_sharpe - baseline
    if rise <= 0:
        return base
    frac = min(1.0, rise / rise_full)
    return max(base, base + frac * (lam_max - base))

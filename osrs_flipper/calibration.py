"""Empirical calibration of the fill model from real order attempts.

Read-only analysis — no parameter is auto-applied. It compares what the model predicted at
placement time against what actually filled, to measure the spread haircut β and a fill-
probability correction, bucketed by liquidity and shrunk toward a prior so a handful of fills
can't swing the estimate. Two deliberate choices keep it honest:

  * Expired (never-filled) attempts are included, not just fills — otherwise the fill-rate
    estimate is survivorship-biased and the model gets more optimistic over time.
  * Bucket estimates are partial-pooled toward the global, and the global toward the config
    prior; with little data you barely move off the prior (pessimistic-wrong > optimistic-wrong).

The fastest path to a bond is honest EV. This is how the guessed β eventually becomes a
measured one — once enough real attempts have accumulated.
"""

from __future__ import annotations

import statistics

_BUCKETS = ("low", "med", "high")


def liquidity_bucket(vol: float) -> str:
    """Coarse buckets on the binding (min buy/sell) 1h volume — fine buckets just split
    scarce fills into noise."""
    if vol < 2000:
        return "low"
    if vol < 10000:
        return "med"
    return "high"


def _beta_of(row: dict) -> float | None:
    """Realised spread haircut for one filled attempt: how far into the spread the fill
    landed. A BUY fills at avg_low + β·spread; a SELL at avg_high − β·spread."""
    spread = row.get("spread") or 0
    if spread <= 0 or row.get("fill_px") is None:
        return None
    if row["side"] == "BUY" and row.get("avg_low") is not None:
        return (row["fill_px"] - row["avg_low"]) / spread
    if row["side"] == "SELL" and row.get("avg_high") is not None:
        return (row["avg_high"] - row["fill_px"]) / spread
    return None


def _fill_frac(row: dict) -> float | None:
    """Fraction of the placed quantity that actually filled (0 for an expired attempt)."""
    if not row.get("qty"):
        return None
    return min(1.0, (row.get("filled_qty") or 0) / row["qty"])


def shrink(measured: float, prior: float, n: int, k: int = 20) -> float:
    """Partial-pool `measured` toward `prior`; weight on the measurement grows with n
    (n = k → halfway). Guards against calibrating to noise on a small sample."""
    w = n / (n + k) if n > 0 else 0.0
    return w * measured + (1 - w) * prior


def calibrate_beta(rows: list[dict], prior: float, k: int = 20) -> dict:
    """Measured/shrunk β overall and per liquidity bucket, with sample counts."""
    betas = [(liquidity_bucket(r.get("vol_1h_binding") or 0), b)
             for r in rows if (b := _beta_of(r)) is not None]
    out: dict = {"n": len(betas), "prior": prior, "buckets": {}}
    if betas:
        g = statistics.median(b for _, b in betas)
        out["global_measured"], out["global"] = g, shrink(g, prior, len(betas), k)
    else:
        out["global_measured"], out["global"] = None, prior
    for name in _BUCKETS:
        bs = [b for bk, b in betas if bk == name]
        if bs:
            m = statistics.median(bs)
            out["buckets"][name] = {"n": len(bs), "measured": m,
                                    "shrunk": shrink(m, out["global"], len(bs), k)}
    return out


def calibrate_fill(rows: list[dict], *, prior: float = 1.0, k: int = 20) -> dict:
    """Fill-probability correction = median(actual_fill_fraction / predicted_p_fill), overall and
    per liquidity bucket, shrunk toward `prior` (1.0 = model unbiased).

    >1 ⇒ model too pessimistic (fills more than predicted); <1 ⇒ too optimistic. Expired attempts
    (fill fraction 0) pull it down — that's the point. Same partial-pooling as β so a handful of
    fills barely moves it off 1.0. `global_measured` is the raw read; `global` the shrunk one."""
    pairs = []
    for r in rows:
        ff, p = _fill_frac(r), r.get("pred_p_fill")
        if ff is not None and p and p > 0:
            pairs.append((liquidity_bucket(r.get("vol_1h_binding") or 0), ff / p))
    out: dict = {"n": len(pairs), "prior": prior, "buckets": {}}
    if pairs:
        g = statistics.median(f for _, f in pairs)
        out["global_measured"], out["global"] = g, shrink(g, prior, len(pairs), k)
    else:
        out["global_measured"], out["global"] = None, prior
    for name in _BUCKETS:
        fs = [f for bk, f in pairs if bk == name]
        if fs:
            m = statistics.median(fs)
            out["buckets"][name] = {"n": len(fs), "measured": m,
                                    "shrunk": shrink(m, out["global"], len(fs), k)}
    return out


def fill_multiplier(cal: dict | None, vol: float, *, lo: float = 0.1, hi: float = 1.5) -> float:
    """Per-item EV correction: the item's liquidity-bucket shrunk fill factor (else global, else
    1.0), clamped so a thin/degenerate sample can't zero out or balloon the estimate."""
    if not cal:
        return 1.0
    bucket = cal.get("buckets", {}).get(liquidity_bucket(vol))
    c = bucket["shrunk"] if bucket else cal.get("global", 1.0)
    return max(lo, min(hi, c if c is not None else 1.0))

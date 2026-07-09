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


def liquidity_bucket(turnover: float) -> str:
    """Coarse buckets on binding-side gp TURNOVER (units × mid), not unit count — so the fill
    correction tracks liquidity by value and treats a few high-value trades like many cheap ones
    (matching the scanner's gate). Fine buckets just split scarce fills into noise."""
    if turnover < 2_000_000:
        return "low"
    if turnover < 20_000_000:
        return "med"
    return "high"


def _turnover(row: dict) -> float:
    """Binding-side gp turnover for a recorded attempt, from its decision-time snapshot."""
    vol = row.get("vol_1h_binding") or 0
    al, ah = row.get("avg_low"), row.get("avg_high")
    mid = ((al or 0) + (ah or 0)) / 2 if (al is not None or ah is not None) else 0
    return vol * mid


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
    betas = [(liquidity_bucket(_turnover(r)), b)
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
            pairs.append((liquidity_bucket(_turnover(r)), ff / p))
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


def effective_beta(cal: dict | None, fallback: float, *, lo: float = 0.0, hi: float = 0.5) -> float:
    """The β to actually use = the calibrated (shrunk) global spread-haircut, clamped to a sane
    range. Falls back to the config prior when there's no calibration yet (the shrink already keeps
    it near the prior with little data, so this is mostly belt-and-suspenders)."""
    if not cal:
        return fallback
    g = cal.get("global")
    return max(lo, min(hi, g if g is not None else fallback))


def fill_multiplier(cal: dict | None, turnover: float, *, lo: float = 0.1, hi: float = 1.5) -> float:
    """Per-item EV correction: the item's turnover-bucket shrunk fill factor (else global, else
    1.0), clamped so a thin/degenerate sample can't zero out or balloon the estimate."""
    if not cal:
        return 1.0
    bucket = cal.get("buckets", {}).get(liquidity_bucket(turnover))
    c = bucket["shrunk"] if bucket else cal.get("global", 1.0)
    return max(lo, min(hi, c if c is not None else 1.0))


# --- fill-TIME (ETA) calibration: learn realized live-time vs predicted, 2D by price×volume ---------
def _price_band(price: float) -> str:
    return "cheap" if price < 1_000 else "mid" if price < 100_000 else "dear"


def _vol_band(volume: float) -> str:
    return "thin" if volume < 1_000 else "med" if volume < 20_000 else "deep"


def pv_bucket(price: float, volume: float) -> str:
    """2D price×volume bucket — so 'similarly priced, volumed items' share one fill-time correction
    (a cheap-liquid item fills on a very different clock than an expensive-thin one)."""
    return f"{_price_band(price or 0)}/{_vol_band(volume or 0)}"


def _realized_eta_ratio(row: dict) -> tuple[str, float] | None:
    """(bucket, realized/pred) for a resolved attempt, or None if untimeable. A filled attempt gives an
    exact ratio; a never-filled one (expired/cancelled, 0 filled) is right-censored — a LOWER bound that
    only informs us when it already exceeded the prediction ('took ≥X and still didn't fill' → too fast)."""
    pred = row.get("pred_eta_h")
    ts = row.get("ts")
    if pred is None or pred <= 0 or ts is None:
        return None
    status, filled_qty = row.get("status"), row.get("filled_qty") or 0
    if status == "filled" and row.get("filled_ts"):
        realized = (row["filled_ts"] - ts) / 3600.0
        censored = False
    elif status in ("expired", "cancelled") and filled_qty == 0 and (row.get("resolved_ts") or row.get("filled_ts")):
        realized = ((row.get("resolved_ts") or row["filled_ts"]) - ts) / 3600.0
        censored = True
    else:
        return None  # partial / in-progress → ambiguous fill time, skip
    ratio = max(0.0, realized) / pred
    if censored and ratio <= 1.0:
        return None  # a never-fill that resolved sooner than predicted tells us nothing about speed
    mid = ((row.get("avg_low") or 0) + (row.get("avg_high") or 0)) / 2
    return pv_bucket(mid, row.get("vol_1h_binding") or 0), ratio


def calibrate_eta(rows: list[dict], *, prior: float = 1.0, k: int = 20) -> dict:
    """Realized-fill-time / predicted-ETA, median per price×volume bucket, shrunk global→prior. Same shape
    as calibrate_fill. `>1` ⇒ items fill SLOWER than modelled (ETA too optimistic); `<1` ⇒ faster."""
    obs = [o for r in rows if (o := _realized_eta_ratio(r)) is not None]
    out: dict = {"n": len(obs), "prior": prior, "global_measured": None, "global": prior, "buckets": {}}
    if obs:
        g = statistics.median(x for _, x in obs)
        out["global_measured"], out["global"] = g, shrink(g, prior, len(obs), k)
        by: dict[str, list[float]] = {}
        for b, x in obs:
            by.setdefault(b, []).append(x)
        for name, xs in by.items():
            m = statistics.median(xs)
            out["buckets"][name] = {"n": len(xs), "measured": m, "shrunk": shrink(m, out["global"], len(xs), k)}
    return out


def eta_multiplier(cal: dict | None, price: float, volume: float, *, lo: float = 0.5, hi: float = 3.0) -> float:
    """Per-item ETA correction: the price×volume bucket's shrunk factor (else global, else 1.0), clamped.
    `>1` stretches the predicted fill time (this kind fills slower than modelled)."""
    if not cal:
        return 1.0
    bucket = cal.get("buckets", {}).get(pv_bucket(price, volume))
    c = bucket["shrunk"] if bucket else cal.get("global", 1.0)
    return max(lo, min(hi, c if c is not None else 1.0))

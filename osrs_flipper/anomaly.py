"""Anomaly / manipulation detector: items whose LIVE price has dislocated from their recent
baseline on abnormal volume — pump-ups (avoid / sell into) and over-dumps (mean-revert buy).

The scanner deliberately FILTERS these out (the divergence + adverse-move gates in features.py);
this surfaces them instead. In OSRS the only low-risk exploit is the reversion side — buy an
over-dumped staple back toward its baseline. You can't short a pump, and the 4h buy limit caps how
much you can deploy, so this is opportunistic, not a core engine.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from typing import Any

from . import config
from .tax import post_tax_received


def screen(latest: dict[int, dict], hourly: dict[int, dict], *, div_min: float,
           vol_min: int) -> list[dict[str, Any]]:
    """Cheap first pass over every item: live mid vs 1h-average mid divergence, on real volume.
    Thin items are excluded — a wide gap on no volume is illiquidity, not manipulation."""
    out = []
    for iid, lp in latest.items():
        hp = hourly.get(iid)
        if not hp:
            continue
        high, low = lp.get("high"), lp.get("low")
        ah, al = hp.get("avgHighPrice"), hp.get("avgLowPrice")
        if None in (high, low, ah, al) or al <= 0 or low > high:
            continue
        live_mid, avg_mid = (high + low) / 2, (ah + al) / 2
        if avg_mid <= 0:
            continue
        div = (live_mid - avg_mid) / avg_mid
        vol = min(hp.get("highPriceVolume") or 0, hp.get("lowPriceVolume") or 0)
        if abs(div) >= div_min and vol >= vol_min:
            out.append({"item_id": iid, "live_mid": live_mid, "avg_mid": avg_mid, "divergence": div,
                        "vol_binding": vol, "live_high": int(high), "live_low": int(low)})
    out.sort(key=lambda c: -abs(c["divergence"]) * c["vol_binding"])
    return out


def analyze(bars: list[dict], min_bars: int = 8) -> dict[str, float] | None:
    """From recent /timeseries bars: baseline = median mid (robust to the spike), volume z-score
    of the latest bar, and recent slope. None if too little data."""
    mids, vols = [], []
    for b in bars:
        ah, al = b.get("avgHighPrice"), b.get("avgLowPrice")
        if ah is None or al is None:
            continue
        mids.append((ah + al) / 2)
        vols.append((b.get("highPriceVolume") or 0) + (b.get("lowPriceVolume") or 0))
    if len(mids) < min_bars:
        return None
    vstd = statistics.pstdev(vols) or 1.0
    return {
        "baseline": statistics.median(mids),
        "vol_z": (vols[-1] - statistics.median(vols)) / vstd,
        "slope": mids[-1] - statistics.median(mids[-4:-1]),
    }


def classify(div_now: float, slope: float, *, div_min: float) -> tuple[str, str]:
    """(divergence-from-baseline, recent slope) → phase + plain-English verdict."""
    if div_now > div_min:
        return ("PUMP↑", "being pumped — don't chase; sell into it if you hold") if slope >= 0 else \
               ("FADE↓", "post-pump deflation — wait for the floor, don't catch the knife")
    if div_now < -div_min:
        return ("RECOVER↑", "over-dumped, reverting up — revert-buy toward baseline") if slope > 0 else \
               ("DUMP↓", "over-dumped & still falling — revert-buy once it floors")
    return ("", "")


def detect(latest: dict[int, dict], hourly: dict[int, dict], names: dict[int, str],
           timeseries_fn: Callable[[int], list[dict]], *, div_min: float | None = None,
           vol_min: int | None = None, vol_z_min: float | None = None,
           candidates: int | None = None) -> list[dict[str, Any]]:
    """Screen all items cheaply, then deep-check the top candidates with /timeseries: confirm an
    abnormal-volume signature and classify pump vs over-dump. `timeseries_fn(item_id)` is injected
    so this is testable without network."""
    div_min = config.ANOMALY_DIV_MIN if div_min is None else div_min
    vol_min = config.ANOMALY_MIN_VOL if vol_min is None else vol_min
    vol_z_min = config.ANOMALY_VOL_Z_MIN if vol_z_min is None else vol_z_min
    candidates = config.ANOMALY_CANDIDATES if candidates is None else candidates

    out = []
    for c in screen(latest, hourly, div_min=div_min, vol_min=vol_min)[:candidates]:
        a = analyze(timeseries_fn(c["item_id"]))
        if not a or abs(a["vol_z"]) < vol_z_min:
            continue  # no abnormal-volume signature → ordinary drift, not manipulation
        baseline = a["baseline"]
        div_now = (c["live_mid"] - baseline) / baseline if baseline else 0.0
        phase, verdict = classify(div_now, a["slope"], div_min=div_min)
        if not phase:
            continue
        iid = c["item_id"]
        # revert-buy EV only on the dumped side: pay the live ask, sell back at ~baseline
        revert_ev = post_tax_received(int(baseline), item_id=iid) - c["live_high"] if div_now < 0 else 0
        out.append({**c, "name": names.get(iid, str(iid)), "baseline": baseline, "vol_z": a["vol_z"],
                    "slope": a["slope"], "div_now": div_now, "phase": phase, "verdict": verdict,
                    "revert_ev_unit": revert_ev})
    # show the exploitable (reverting) ones first, then by dislocation size
    out.sort(key=lambda h: (h["div_now"] >= 0, -abs(h["div_now"])))
    return out

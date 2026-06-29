"""Client for the OSRS Wiki real-time prices API.

Endpoints (base = config.API_BASE):
  /latest                      -> {item_id: {high, highTime, low, lowTime}}
  /5m,  /1h  [?timestamp=]     -> {item_id: {avgHighPrice, highPriceVolume,
                                             avgLowPrice, lowPriceVolume}}
  /mapping                     -> [ {id, name, limit, value, highalch, members, ...} ]
  /timeseries ?id= &timestep=  -> [ {timestamp, avgHighPrice, avgLowPrice,
                                      highPriceVolume, lowPriceVolume} ]  (<=365 pts)

The bulk endpoints return every traded item in one response — never loop /latest?id=.
Item ids are returned as ints (the API keys them as strings).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from . import cache
from .config import API_BASE, CACHE_DEFAULT_TTL_S, CACHE_ENABLED, CACHE_TTL_S, HTTP_TIMEOUT
from .http import get_session

TimeStep = str  # one of "5m", "1h", "6h", "24h"
_VALID_TIMESTEPS = {"5m", "1h", "6h", "24h"}


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    key = path + ("?" + urlencode(sorted(params.items())) if params else "")
    ttl = CACHE_TTL_S.get(path, CACHE_DEFAULT_TTL_S)
    if CACHE_ENABLED:
        hit = cache.get(key, ttl)
        if hit is not None:
            return hit
    resp = get_session().get(f"{API_BASE}{path}", params=params, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if CACHE_ENABLED:
        cache.set(key, data)
    return data


def latest() -> dict[int, dict[str, Any]]:
    """Most recent instant-buy (high) / instant-sell (low) price + unix timestamps."""
    data = _get("/latest")["data"]
    return {int(k): v for k, v in data.items()}


def five_min(timestamp: int | None = None) -> dict[int, dict[str, Any]]:
    """5-minute averaged high/low prices and volumes for all items."""
    params = {"timestamp": timestamp} if timestamp is not None else None
    data = _get("/5m", params)["data"]
    return {int(k): v for k, v in data.items()}


def one_hour(timestamp: int | None = None) -> dict[int, dict[str, Any]]:
    """1-hour averaged high/low prices and volumes for all items."""
    params = {"timestamp": timestamp} if timestamp is not None else None
    data = _get("/1h", params)["data"]
    return {int(k): v for k, v in data.items()}


def mapping() -> list[dict[str, Any]]:
    """Static item metadata: id, name, limit (buy limit / 4h), value, highalch, members."""
    return _get("/mapping")


def timeseries(item_id: int, timestep: TimeStep = "5m") -> list[dict[str, Any]]:
    """Up to 365 historical points for one item at the given timestep."""
    if timestep not in _VALID_TIMESTEPS:
        raise ValueError(f"timestep must be one of {_VALID_TIMESTEPS}, got {timestep!r}")
    return _get("/timeseries", {"id": item_id, "timestep": timestep})["data"]

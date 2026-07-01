"""One interface to whatever provides live account data, so the rest of the tool never branches on
*how* we read it. Change the data provider (plugins, an API, a new export format, …) by editing only
this module — the terminal just calls `datasource.active().holdings()` etc.

Backends today, in priority order:
  * FlipExporterSource — the Flip Exporter plugin's latest.json + history.json (one clean source).
  * LegacySource       — the older Local Data Exporter + Flipping Utilities pair (fallback).

Every method returns the SAME shapes regardless of backend:
  cash()            -> int | None          deployable gp on hand (None if no live snapshot)
  tied_gold()       -> int                 gold locked in open offers (equity continuity)
  holdings()        -> dict[id,int] | None  units held per tradeable item (None if not live)
  active_offers()   -> list[runelite.Offer]
  all_fills()       -> list[runelite.Fill]  fills to account (completed + active partials)
  completed_offers()-> list[runelite.Fill]  completed trade history
  limit_used()      -> dict[id,int] | None  4h buy-limit counter (None → caller uses the journal)
  warnings()        -> list[str]           data-health warnings to surface
"""

from __future__ import annotations

from . import api, flip_exporter, local_export, runelite
from .runelite import Fill, Offer


class DataSource:
    name = "none"

    def cash(self) -> int | None:
        return None

    def tied_gold(self) -> int:
        return 0

    def holdings(self) -> dict[int, int] | None:
        return None

    def active_offers(self) -> list[Offer]:
        return []

    def all_fills(self) -> list[Fill]:
        return []

    def completed_offers(self) -> list[Fill]:
        return []

    def limit_used(self) -> dict[int, int] | None:
        return None  # None → caller falls back to the journal's own buy-limit sum

    def warnings(self) -> list[str]:
        return []


class FlipExporterSource(DataSource):
    """The Flip Exporter plugin — a single canonical source (noted-resolved holdings, real prices,
    placement times, persisted trade history)."""

    name = "Flip Exporter plugin"

    def __init__(self) -> None:
        self._d = flip_exporter.read()
        self._h = flip_exporter.read_history()

    def cash(self) -> int | None:
        return flip_exporter.cash(self._d)

    def tied_gold(self) -> int:
        return flip_exporter.tied_gold(self._d)

    def holdings(self) -> dict[int, int] | None:
        return flip_exporter.holdings(self._d)

    def active_offers(self) -> list[Offer]:
        return flip_exporter.active_offers(self._d)

    def all_fills(self) -> list[Fill]:
        return flip_exporter.all_fills(self._d, self._h)

    def completed_offers(self) -> list[Fill]:
        return flip_exporter.completed_offers(self._h)


class LegacySource(DataSource):
    """The pre-plugin pair: Local Data Exporter (cash/holdings/offers) + Flipping Utilities (fills,
    buy-limit counter). Offers are the LDE view enriched with FU placement times."""

    name = "Local Data Exporter + Flipping Utilities"

    def __init__(self) -> None:
        self._le = local_export.read()
        self._rl = runelite.read()
        self._names: dict[int, str] | None = None

    def _names_map(self) -> dict[int, str]:
        if self._names is None:
            self._names = {r["id"]: r["name"] for r in api.mapping()}
        return self._names

    def cash(self) -> int | None:
        return local_export.coins(self._le)

    def tied_gold(self) -> int:
        return local_export.tied_gold(self._le)

    def holdings(self) -> dict[int, int] | None:
        return local_export.holdings(self._le, set(self._names_map()))

    def active_offers(self) -> list[Offer]:
        leo = local_export.active_offers(self._le)
        fu = runelite.active_offers(self._rl) if self._rl else []
        if not leo:
            return fu
        by_slot = {o.slot: o for o in fu}  # enrich LDE offers with FU placement time / uuid
        for o in leo:
            f = by_slot.get(o.slot)
            if f and f.item_id == o.item_id:
                o.started_ms = f.started_ms or o.started_ms
                o.uuid = f.uuid or o.uuid
        return leo

    def all_fills(self) -> list[Fill]:
        return runelite.all_fills(self._rl, self._names_map()) if self._rl else []

    def completed_offers(self) -> list[Fill]:
        return runelite.completed_offers(self._rl) if self._rl else []

    def limit_used(self) -> dict[int, int] | None:
        return runelite.limit_used(self._rl) if self._rl else None

    def warnings(self) -> list[str]:
        return runelite.schema_health(self._rl)


def active() -> DataSource:
    """The best available live data source (constructs a fresh snapshot each call — cheap file read)."""
    return FlipExporterSource() if flip_exporter.available() else LegacySource()

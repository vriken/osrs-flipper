"""Rank heterogeneous trade candidates — fast cyclable flips, patient big-ticket "gear", and GE set
arbitrage — on ONE honest currency so `go` can recommend the best use of your free slots.

The currency is **expected gp over the time window you have, per GE slot**:
  * fast flips cycle, so the caller feeds their throughput (gp/hour × window) as `window_gp`;
  * gear and sets fill once, so their `window_gp` is a single expected profit — and because they're
    priced best-case (β=0, "fill AT the bid/ask"), a confidence haircut is applied before ranking so an
    optimistic set can't crowd out an honestly-priced flip. Only when a patient play genuinely beats a
    haircut-adjusted flip (an idle slot, or overnight when flips fill once too) does it win the slot.

A set ASSEMBLE occupies N buy slots at once, so its per-slot value divides by the slots it ties up and it
is skipped when it doesn't fit the free slots — it competes fairly against N single flips. Pure and
network-free; all pricing/gating happens upstream.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from . import config


@dataclass(frozen=True)
class Candidate:
    kind: str                              # "flip" | "gear" | "set"
    key: str                               # display label ("Yew logs", "Ahrim's set · ASSEMBLE")
    slots: int                             # GE slots tied up now (1 flip/gear/set-BREAK; N set-ASSEMBLE)
    window_gp: float                       # expected gp over the window, PRE-haircut
    patient: bool = False                  # best-case (β=0) pricing → haircut applies
    item_ids: tuple[int, ...] = ()         # for dedupe across picks and vs held/offers
    fill_eta_h: float | None = None
    cost: float = 0.0                      # gp of capital this pick ties up (0 = unknown → no budget cap)
    payload: dict = field(default_factory=dict)  # underlying row for rendering / placement


def _attention_discount(fill_eta_h: float | None) -> float:
    """Discount a candidate's per-slot value by the manual click overhead it implies. A flip that only
    reaches its window_gp by cycling every few minutes demands constant clicking; one that fills once
    over hours barely any. The handling FRACTION of the window is actions·(sec_per_offer/3600)/cycle_h
    (the window cancels), so a fast-cycling flip is discounted more than a slow hold/gear. Neutral when
    the fill time is unknown (overnight/gear rows that don't carry it) or the click cost is disabled."""
    if config.SECONDS_PER_OFFER <= 0 or config.ACTIONS_PER_CYCLE <= 0 or not fill_eta_h or fill_eta_h <= 0:
        return 1.0
    cycle_h = max(fill_eta_h, config.ATTENTION_MIN_ETA_H)  # floor so a near-instant fill can't nuke the score
    handling_frac = config.ACTIONS_PER_CYCLE * (config.SECONDS_PER_OFFER / 3600.0) / cycle_h
    return 1.0 / (1.0 + handling_frac)


def per_slot_score(c: Candidate, *, patient_confidence: float) -> float:
    """Expected gp per slot, after haircutting best-case (patient) EV and the manual-handling overhead
    a fast-cycling flip implies. The ranking key (discounts the score, not the displayed window_gp)."""
    ev = c.window_gp * (patient_confidence if c.patient else 1.0)
    return ev / max(1, c.slots) * _attention_discount(c.fill_eta_h)


def rank(cands: list[Candidate], *, free_slots: int, patient_confidence: float,
         exclude_ids: set[int] | None = None, budget: float | None = None,
         verify: Callable[[Candidate], bool] | None = None) -> list[Candidate]:
    """Greedily fill `free_slots` with the highest per-slot-value candidates.

    A candidate is skipped if it doesn't fit the remaining slots, or if any of its item ids is already
    taken (by an earlier pick or by `exclude_ids` — items you already hold / have on offer). Sets consume
    `slots` each. If `budget` is given, a candidate is also skipped when its `cost` (capital it ties up)
    would overrun the remaining cash — different candidate kinds are sized against DIFFERENT capital
    baselines upstream (flips split the whole pile; gear/set/decant take one slot's fair share), so
    without a shared cash cap the greedy pick could commit more than you actually have. A candidate with
    `cost == 0` (unknown) is never budget-skipped. `verify`, if given, is called ONLY on patient
    candidates that would otherwise be chosen (so the network-costly pump/knife gate runs lazily on the
    few picks that matter); returning False drops that pick. Returns the chosen candidates in ranked order.
    """
    if free_slots <= 0:
        return []
    taken_ids: set[int] = set(exclude_ids or ())
    ordered = sorted(cands, key=lambda c: per_slot_score(c, patient_confidence=patient_confidence),
                     reverse=True)
    chosen: list[Candidate] = []
    remaining = free_slots
    spent = 0.0
    for c in ordered:
        if c.slots > remaining:
            continue  # doesn't fit (e.g. a 4-piece set with 2 slots left) — try the next best
        if c.window_gp <= 0:
            continue
        if taken_ids.intersection(c.item_ids):
            continue  # already buying/holding one of these items — don't double up
        if budget is not None and c.cost > 0 and spent + c.cost > budget:
            continue  # would overrun the shared cash budget — skip and try a cheaper pick
        if c.patient and verify is not None and not verify(c):
            continue  # best-case play whose bought legs look pumped / falling — skip it
        chosen.append(c)
        taken_ids.update(c.item_ids)
        remaining -= c.slots
        spent += c.cost
        if remaining <= 0:
            break
    return chosen

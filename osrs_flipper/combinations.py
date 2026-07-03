"""Static table of GE *combinations* — buying components and selling the combined output for more than
the parts cost, or the reverse. Two kinds share one schema:

  * **set**    — armour/weapon sets exchanged at the GE set clerk. Combining/breaking is FREE, INSTANT,
                 and needs no skill, so it's pure price arbitrage in BOTH directions (assemble ↔ break).
  * **recipe** — a skilling conversion (buy inputs, apply a skill, sell the output). Usually one-way,
                 may need a level, consume secondaries, cost a per-op fee, or lose yield to burn/failure.

A set is just a recipe with no skill, no fee, no secondaries, full yield, and `reversible=True` — so the
same `Combo` and the same pricer (`combos.py`) serve both. Phase 1 ships sets only; the recipe fields
below default to the set case and stay unused until recipes land.

WHY A PYTHON MODULE (not data/combinations.json): the prices API carries no composition data, so this
mapping must be bundled — and `data/` is gitignored while the package ships pure-Python (no package-data),
so a committed, auto-packaged, import-cheap module constant is the right home. IDs here are verified
against /mapping; `tests/test_combinations.py` guards them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Combo:
    id: str                                   # stable slug (test/reference key; never the numeric id)
    name: str
    output_id: int
    inputs: tuple[tuple[int, int], ...]       # ((item_id, qty), …) — the parts
    kind: str = "set"                         # "set" | "recipe"
    reversible: bool = True                   # sets assemble AND break; recipes are usually one-way
    # --- recipe-only (defaults describe the set case) ---
    skill: str | None = None                  # "Herblore", "Cooking", "Magic" (alch), …
    level: int = 0                            # min level to perform the conversion
    xp: float = 0.0                           # informational only
    secondaries: tuple[tuple[int, int], ...] = ()   # ((item_id, qty), …) consumed per op, priced at BUY
    fee: int = 0                              # fixed gp per op (clerk/tan fee); 0 for sets
    output_yield: float = 1.0                 # expected outputs per op (<1 for burn/fail loss)
    output_type: str = "item"                 # "item" (taxed GE sale) | "coins" (alch — untaxed)


def _set(slug: str, name: str, output_id: int, pieces: tuple[int, ...]) -> Combo:
    """A 4-piece (or n-piece) GE set: every piece qty 1, both directions, no skill/fee."""
    return Combo(id=slug, name=name, output_id=output_id,
                 inputs=tuple((pid, 1) for pid in pieces), kind="set")


# --- Sets (component ids verified against /mapping) --------------------------------------------------
# Barrows brothers — helm/body/legs/weapon. All members.
_SETS: list[Combo] = [
    _set("dharok_set",  "Dharok's armour set",  12877, (4716, 4720, 4722, 4718)),
    _set("guthan_set",  "Guthan's armour set",  12873, (4724, 4728, 4730, 4726)),
    _set("karil_set",   "Karil's armour set",   12883, (4732, 4736, 4738, 4734)),
    _set("verac_set",   "Verac's armour set",   12875, (4753, 4757, 4759, 4755)),
    _set("torag_set",   "Torag's armour set",   12879, (4745, 4749, 4751, 4747)),
    _set("ahrim_set",   "Ahrim's armour set",   12881, (4708, 4712, 4714, 4710)),
    # God dragonhide — coif/body/chaps/bracers. All members.
    _set("bandos_dhide_set",    "Bandos dragonhide set",    13167, (12504, 12500, 12502, 12498)),
    _set("armadyl_dhide_set",   "Armadyl dragonhide set",   13169, (12512, 12508, 12510, 12506)),
    _set("ancient_dhide_set",   "Ancient dragonhide set",   13171, (12496, 12492, 12494, 12490)),
    _set("guthix_dhide_set",    "Guthix dragonhide set",    13165, (10382, 10378, 10380, 10376)),
    _set("saradomin_dhide_set", "Saradomin dragonhide set", 13163, (10390, 10386, 10388, 10384)),
    _set("zamorak_dhide_set",   "Zamorak dragonhide set",   13161, (10374, 10370, 10372, 10368)),
]

# Phase 2 fills this in (high-alch, decanting, skilling conversions).
_RECIPES: list[Combo] = []

COMBINATIONS: list[Combo] = _SETS + _RECIPES


def load(kind: str | None = None) -> list[Combo]:
    """All combinations, or only those of `kind` ("set" | "recipe")."""
    if kind is None:
        return list(COMBINATIONS)
    return [c for c in COMBINATIONS if c.kind == kind]


def item_ids(combo: Combo) -> set[int]:
    """Every item id the combo touches — output, inputs, and secondaries. Used to gather feature rows."""
    ids = {combo.output_id}
    ids.update(pid for pid, _ in combo.inputs)
    ids.update(pid for pid, _ in combo.secondaries)
    return ids


def validate(combo: Combo, mapping_ids: set[int] | None = None) -> list[str]:
    """Structural problems with a combo (empty inputs, bad qty, non-int ids). If `mapping_ids` is given,
    also flags any id absent from /mapping. Empty list = clean. Drives the data-integrity test."""
    errs: list[str] = []
    if not combo.inputs:
        errs.append(f"{combo.id}: no inputs")
    for pid, qty in combo.inputs + combo.secondaries + ((combo.output_id, 1),):
        if not isinstance(pid, int):
            errs.append(f"{combo.id}: non-int id {pid!r}")
        if not isinstance(qty, int) or qty <= 0:
            errs.append(f"{combo.id}: bad qty {qty!r} for id {pid}")
    if combo.output_yield <= 0:
        errs.append(f"{combo.id}: output_yield must be > 0")
    if combo.kind == "recipe" and combo.skill and combo.level <= 0:
        errs.append(f"{combo.id}: recipe skill {combo.skill} has no level")
    if mapping_ids is not None:
        for iid in item_ids(combo):
            if iid not in mapping_ids:
                errs.append(f"{combo.id}: id {iid} not in /mapping")
    return errs

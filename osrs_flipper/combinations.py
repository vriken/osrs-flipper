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

import re
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
    byproducts: tuple[tuple[int, int], ...] = ()     # ((item_id, qty), …) produced as a SIDE output per op
                                                     # (e.g. vials freed by decanting up); sold post-tax, SOFT-credited
    fee: int = 0                              # fixed gp per op (clerk/tan fee); 0 for sets
    output_yield: float = 1.0                 # expected outputs per op (<1 for burn/fail loss)
    output_type: str = "item"                 # "item" (taxed GE sale) | "coins" (alch — untaxed)
    in_dose: int = 0                          # decant display only: source dose (e.g. 3 in a (3)→(4)); 0 = n/a
    out_dose: int = 0                         # decant display only: output dose (e.g. 4); 0 = n/a


def _set(slug: str, name: str, output_id: int, pieces: tuple[int, ...]) -> Combo:
    """A 4-piece (or n-piece) GE set: every piece qty 1, both directions, no skill/fee."""
    return Combo(id=slug, name=name, output_id=output_id,
                 inputs=tuple((pid, 1) for pid in pieces), kind="set")


# --- Sets — components sourced from the OSRS Wiki (==Components== / {{CostLine}}) and verified
#     against /mapping; regenerate if the wiki changes. `members` is derived at scan time from
#     the legs, not stored here. lg = platelegs variant, sk = plateskirt variant.
_SETS: list[Combo] = [
    # Barrows brothers
    _set('ahrims_armour_set', "Ahrim's armour set", 12881, (4708, 4712, 4714, 4710)),
    _set('dharoks_armour_set', "Dharok's armour set", 12877, (4716, 4720, 4722, 4718)),
    _set('guthans_armour_set', "Guthan's armour set", 12873, (4724, 4728, 4730, 4726)),
    _set('karils_armour_set', "Karil's armour set", 12883, (4732, 4736, 4738, 4734)),
    _set('torags_armour_set', "Torag's armour set", 12879, (4745, 4749, 4751, 4747)),
    _set('veracs_armour_set', "Verac's armour set", 12875, (4753, 4757, 4759, 4755)),
    # Dragonhide
    _set('ancient_dragonhide_set', 'Ancient dragonhide set', 13171, (12496, 12492, 12494, 12490)),
    _set('armadyl_dragonhide_set', 'Armadyl dragonhide set', 13169, (12512, 12508, 12510, 12506)),
    _set('bandos_dragonhide_set', 'Bandos dragonhide set', 13167, (12504, 12500, 12502, 12498)),
    _set('black_dragonhide_set', 'Black dragonhide set', 12871, (2503, 2497, 2491)),
    _set('blue_dragonhide_set', 'Blue dragonhide set', 12867, (2499, 2493, 2487)),
    _set('gilded_dragonhide_set', 'Gilded dragonhide set', 23124, (23264, 23267, 23261)),  # F2P
    _set('green_dragonhide_set', 'Green dragonhide set', 12865, (1135, 1099, 1065)),  # F2P
    _set('guthix_dragonhide_set', 'Guthix dragonhide set', 13165, (10382, 10378, 10380, 10376)),
    _set('red_dragonhide_set', 'Red dragonhide set', 12869, (2501, 2495, 2489)),
    _set('saradomin_dragonhide_set', 'Saradomin dragonhide set', 13163, (10390, 10386, 10388, 10384)),
    _set('zamorak_dragonhide_set', 'Zamorak dragonhide set', 13161, (10374, 10370, 10372, 10368)),
    # Metal / god rune / dragon armour (lg = platelegs, sk = plateskirt)
    _set('ancient_rune_armour_set_lg', 'Ancient rune armour set (lg)', 13060, (12466, 12460, 12462, 12468)),  # F2P
    _set('ancient_rune_armour_set_sk', 'Ancient rune armour set (sk)', 13062, (12466, 12460, 12464, 12468)),  # F2P
    _set('armadyl_rune_armour_set_lg', 'Armadyl rune armour set (lg)', 13052, (12476, 12470, 12472, 12478)),  # F2P
    _set('armadyl_rune_armour_set_sk', 'Armadyl rune armour set (sk)', 13054, (12476, 12470, 12474, 12478)),  # F2P
    _set('bandos_rune_armour_set_lg', 'Bandos rune armour set (lg)', 13056, (12486, 12480, 12482, 12488)),  # F2P
    _set('bandos_rune_armour_set_sk', 'Bandos rune armour set (sk)', 13058, (12486, 12480, 12484, 12488)),  # F2P
    _set('dragon_armour_set_lg', 'Dragon armour set (lg)', 21882, (11335, 21892, 4087, 21895)),
    _set('dragon_armour_set_sk', 'Dragon armour set (sk)', 21885, (11335, 21892, 4585, 21895)),
    _set('gilded_armour_set_lg', 'Gilded armour set (lg)', 13036, (3486, 3481, 3483, 3488)),  # F2P
    _set('gilded_armour_set_sk', 'Gilded armour set (sk)', 13038, (3486, 3481, 3485, 3488)),  # F2P
    _set('guthix_armour_set_lg', 'Guthix armour set (lg)', 13048, (2673, 2669, 2671, 2675)),  # F2P
    _set('guthix_armour_set_sk', 'Guthix armour set (sk)', 13050, (2673, 2669, 3480, 2675)),  # F2P
    _set('rune_armour_set_lg', 'Rune armour set (lg)', 13024, (1163, 1127, 1079, 1201)),  # F2P
    _set('rune_armour_set_sk', 'Rune armour set (sk)', 13026, (1163, 1127, 1093, 1201)),  # F2P
    _set('saradomin_armour_set_lg', 'Saradomin armour set (lg)', 13040, (2665, 2661, 2663, 2667)),  # F2P
    _set('saradomin_armour_set_sk', 'Saradomin armour set (sk)', 13042, (2665, 2661, 3479, 2667)),  # F2P
    _set('zamorak_armour_set_lg', 'Zamorak armour set (lg)', 13044, (2657, 2653, 2655, 2659)),  # F2P
    _set('zamorak_armour_set_sk', 'Zamorak armour set (sk)', 13046, (2657, 2653, 3478, 2659)),  # F2P
    # Raid, boss & other armour
    _set('ancestral_robes_set', 'Ancestral robes set', 21049, (21018, 21021, 21024)),
    _set('blood_moon_armour_set', 'Blood moon armour set', 31136, (29028, 29022, 29025, 28997)),
    _set('bloodbark_armour_set', 'Bloodbark armour set', 31163, (25413, 25404, 25416, 25407, 25410)),
    _set('blue_moon_armour_set', 'Blue moon armour set', 31139, (29019, 29013, 29016, 28988)),
    _set('dagonhai_robes_set', "Dagon'hai robes set", 24333, (24288, 24291, 24294)),
    _set('dragonstone_armour_set', 'Dragonstone armour set', 23667, (24034, 24037, 24040, 24046, 24043)),
    _set('dwarf_cannon_set', 'Dwarf cannon set', 12863, (10, 6, 12, 8)),
    _set('eclipse_moon_armour_set', 'Eclipse moon armour set', 31142, (29010, 29004, 29007, 29000)),
    _set('hueycoatl_hide_armour_set', 'Hueycoatl hide armour set', 31169, (30073, 30076, 30079, 30082)),
    _set('inquisitors_armour_set', "Inquisitor's armour set", 24488, (24419, 24420, 24421)),
    _set('justiciar_armour_set', 'Justiciar armour set', 22438, (22326, 22327, 22328)),
    _set('masori_armour_set_f', 'Masori armour set (f)', 27355, (27235, 27238, 27241)),
    _set('mixed_hide_armour_set', 'Mixed hide armour set', 31166, (29280, 29283, 29286, 29289)),
    _set('oathplate_armour_set', 'Oathplate armour set', 30744, (30750, 30753, 30756)),
    _set('obsidian_armour_set', 'Obsidian armour set', 21279, (21298, 21301, 21304)),
    _set('rock_shell_armour_set', 'Rock-shell armour set', 31151, (6128, 6129, 6130, 6151, 6145)),
    _set('skeletal_armour_set', 'Skeletal armour set', 31154, (6137, 6139, 6141, 6153, 6147)),
    _set('spined_armour_set', 'Spined armour set', 31157, (6131, 6133, 6135, 6149, 6143)),
    _set('sunfire_fanatic_armour_set', 'Sunfire fanatic armour set', 29424, (28933, 28936, 28939)),
    _set('swampbark_armour_set', 'Swampbark armour set', 31160, (25398, 25389, 25401, 25392, 25395)),
    _set('torva_armour_set', 'Torva armour set', 31145, (26382, 26384, 26386)),
    _set('virtus_armour_set', 'Virtus armour set', 31148, (26241, 26243, 26245)),
    # Potion sets
    _set('combat_potion_set', 'Combat potion set', 13064, (2428, 113, 2432)),
    _set('super_potion_set', 'Super potion set', 13066, (2436, 2440, 2442)),
    # God book pages
    _set('book_of_balance_page_set', 'Book of balance page set', 13153, (3835, 3836, 3837, 3838)),
    _set('book_of_darkness_page_set', 'Book of darkness page set', 13159, (12621, 12622, 12623, 12624)),
    _set('book_of_law_page_set', 'Book of law page set', 13157, (12617, 12618, 12619, 12620)),
    _set('book_of_war_page_set', 'Book of war page set', 13155, (12613, 12614, 12615, 12616)),
    _set('holy_book_page_set', 'Holy book page set', 13149, (3827, 3828, 3829, 3830)),
    _set('unholy_book_page_set', 'Unholy book page set', 13151, (3831, 3832, 3833, 3834)),
    # Leagues relic hunter (cosmetic — usually thin/illiquid)
    _set('demonic_pacts_relic_hunter_t1_armour_set', 'Demonic pacts relic hunter (t1) armour set', 33451, (33260, 33263, 33266, 33269)),
    _set('demonic_pacts_relic_hunter_t2_armour_set', 'Demonic pacts relic hunter (t2) armour set', 33454, (33272, 33275, 33278, 33281)),
    _set('demonic_pacts_relic_hunter_t3_armour_set', 'Demonic pacts relic hunter (t3) armour set', 33457, (33284, 33287, 33290, 33293)),
    _set('raging_echoes_relic_hunter_t1_armour_set', 'Raging echoes relic hunter (t1) armour set', 30331, (30404, 30406, 30408, 30410)),
    _set('raging_echoes_relic_hunter_t2_armour_set', 'Raging echoes relic hunter (t2) armour set', 30334, (30412, 30414, 30416, 30418)),
    _set('raging_echoes_relic_hunter_t3_armour_set', 'Raging echoes relic hunter (t3) armour set', 30337, (30420, 30422, 30424, 30426)),
    _set('shattered_relic_hunter_t1_armour_set', 'Shattered relic hunter (t1) armour set', 26554, (26427, 26430, 26433, 26436)),
    _set('shattered_relic_hunter_t2_armour_set', 'Shattered relic hunter (t2) armour set', 26557, (26439, 26442, 26445, 26448)),
    _set('shattered_relic_hunter_t3_armour_set', 'Shattered relic hunter (t3) armour set', 26560, (26451, 26454, 26457, 26460)),
    _set('trailblazer_relic_hunter_t1_armour_set', 'Trailblazer relic hunter (t1) armour set', 25380, (25028, 25031, 25034, 25037)),
    _set('trailblazer_relic_hunter_t2_armour_set', 'Trailblazer relic hunter (t2) armour set', 25383, (25016, 25019, 25022, 25025)),
    _set('trailblazer_relic_hunter_t3_armour_set', 'Trailblazer relic hunter (t3) armour set', 25386, (25001, 25004, 25007, 25010)),
    _set('trailblazer_reloaded_relic_hunter_t1_armour_set', 'Trailblazer reloaded relic hunter (t1) armour set', 28777, (28712, 28715, 28718, 28721)),
    _set('trailblazer_reloaded_relic_hunter_t2_armour_set', 'Trailblazer reloaded relic hunter (t2) armour set', 28780, (28724, 28727, 28730, 28733)),
    _set('trailblazer_reloaded_relic_hunter_t3_armour_set', 'Trailblazer reloaded relic hunter (t3) armour set', 28783, (28736, 28739, 28742, 28745)),
    _set('twisted_relic_hunter_t1_armour_set', 'Twisted relic hunter (t1) armour set', 24469, (24405, 24407, 24409, 24411)),
    _set('twisted_relic_hunter_t2_armour_set', 'Twisted relic hunter (t2) armour set', 24472, (24397, 24399, 24401, 24403)),
    _set('twisted_relic_hunter_t3_armour_set', 'Twisted relic hunter (t3) armour set', 24475, (24387, 24389, 24391, 24393)),
    # Holiday / rare
    _set('halloween_mask_set', 'Halloween mask set', 13175, (1057, 1053, 1055)),  # F2P
    _set('partyhat_set', 'Partyhat set', 13173, (1038, 1040, 1044, 1042, 1046, 1048)),  # F2P
]

# Phase 2 fills this in (high-alch, decanting, skilling conversions).
_RECIPES: list[Combo] = []

COMBINATIONS: list[Combo] = _SETS + _RECIPES


def load(kind: str | None = None) -> list[Combo]:
    """All combinations, or only those of `kind` ("set" | "recipe")."""
    if kind is None:
        return list(COMBINATIONS)
    return [c for c in COMBINATIONS if c.kind == kind]


_DOSE_RE = re.compile(r"^(.+?)\((\d)\)$")  # "Prayer potion(4)" → ("Prayer potion", 4); no space before the paren

# per conversion, decanting UP to (4): (source_dose, inputs_bought, outputs_made) — doses conserved (dose×qty == 4×out)
_DECANT_SPECS: tuple[tuple[int, int, int], ...] = ((1, 4, 1), (2, 2, 1), (3, 4, 3))


def _slug(name: str) -> str:
    """Stable snake-case slug from a potion base name. '+' → 'plus' so Antidote / Antidote+ / Antidote++
    (and the anti-venoms) get distinct, non-colliding slugs."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower().replace("+", " plus ")).strip("_")


def decant_recipes(mapping: list) -> list[Combo]:
    """Derive up-decant recipes from live /mapping: for every potion shipping a full (1)-(4) set, buy the
    low dose, decant UP to (4) at Bob Barter (free, instant, no skill), sell the (4). Freed empty vials
    (inputs − outputs) are returned and credited as a byproduct. Decanting is a MEMBERS-only service.

    IDs are read straight from /mapping — never computed: real dose ids are irregular (e.g. Antipoison is
    179/177/175/2446). Composition lives in the item NAME ("<base>(<dose>)"), which is why this is built
    dynamically rather than bundled like `_SETS`. `output_yield` carries the (3)→(4) case (4 in → 3 out)."""
    vial = next((it["id"] for it in mapping if it.get("name") == "Vial"), None)
    doses: dict[str, dict[int, int]] = {}
    for it in mapping:
        m = _DOSE_RE.match(it.get("name", "") or "")
        if m and 1 <= int(m.group(2)) <= 4:
            doses.setdefault(m.group(1), {})[int(m.group(2))] = it["id"]

    out: list[Combo] = []
    for base, dv in sorted(doses.items()):
        if not all(k in dv for k in (1, 2, 3, 4)):
            continue  # only real 4-dose potion families decant up to (4)
        slug = _slug(base)
        for src, in_qty, out_qty in _DECANT_SPECS:
            freed = in_qty - out_qty  # vials returned when decanting up
            bp = ((vial, freed),) if vial is not None and freed > 0 else ()
            out.append(Combo(
                id=f"decant_{slug}_{src}_to_4", name=f"{base} ({src}→4)",
                output_id=dv[4], inputs=((dv[src], in_qty),),
                kind="recipe", reversible=False, output_yield=float(out_qty), byproducts=bp,
                in_dose=src, out_dose=4))
    return out


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
    for pid, qty in combo.inputs + combo.secondaries + combo.byproducts + ((combo.output_id, 1),):
        if not isinstance(pid, int):
            errs.append(f"{combo.id}: non-int id {pid!r}")
        if not isinstance(qty, int) or qty <= 0:
            errs.append(f"{combo.id}: bad qty {qty!r} for id {pid}")
    if combo.output_yield <= 0:
        errs.append(f"{combo.id}: output_yield must be > 0")
    if combo.kind == "recipe" and combo.skill and combo.level <= 0:
        errs.append(f"{combo.id}: recipe skill {combo.skill} has no level")
    if mapping_ids is not None:
        # byproducts aren't in item_ids() (they're a soft credit, not a hard leg-gate) — check them here too
        for iid in item_ids(combo) | {pid for pid, _ in combo.byproducts}:
            if iid not in mapping_ids:
                errs.append(f"{combo.id}: id {iid} not in /mapping")
    return errs

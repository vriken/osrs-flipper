"""The bundled combination table must be structurally sound (unique slugs, positive int ids/qtys,
non-empty inputs). Wrong component ids give wrong advice, so an optional network-gated check confirms
every id resolves in the live /mapping and members flags line up. Run that with
OSRS_FLIPPER_NET_TESTS=1."""

import os

import pytest

from osrs_flipper.combinations import Combo, _slug, decant_recipes, item_ids, load, validate

# A full potion family + the empty vial + a partial family that must NOT yield decant recipes.
_FAKE_MAPPING = [
    {"id": 11, "name": "Prayer potion(1)"}, {"id": 12, "name": "Prayer potion(2)"},
    {"id": 13, "name": "Prayer potion(3)"}, {"id": 14, "name": "Prayer potion(4)"},
    {"id": 31, "name": "Antidote+(1)"}, {"id": 32, "name": "Antidote+(2)"},
    {"id": 33, "name": "Antidote+(3)"}, {"id": 34, "name": "Antidote+(4)"},
    {"id": 21, "name": "Agility mix(1)"}, {"id": 22, "name": "Agility mix(2)"},  # partial → excluded
    {"id": 229, "name": "Vial"},
]


def test_every_combo_is_structurally_valid():
    for c in load():
        assert validate(c) == [], f"{c.id}: {validate(c)}"


def test_slugs_are_unique():
    slugs = [c.id for c in load()]
    assert len(slugs) == len(set(slugs))


def test_sets_are_reversible_with_no_skill_or_fee():
    for c in load("set"):
        assert c.reversible and c.skill is None and c.fee == 0 and c.output_yield == 1.0
        assert c.inputs and c.output_type == "item"


def test_item_ids_covers_output_inputs_secondaries():
    c = Combo(id="x", name="x", output_id=9, inputs=((1, 1), (2, 3)), secondaries=((5, 1),))
    assert item_ids(c) == {9, 1, 2, 5}


def test_validate_flags_bad_data():
    assert validate(Combo(id="bad", name="b", output_id=1, inputs=()))          # no inputs
    assert validate(Combo(id="bad", name="b", output_id=1, inputs=((2, 0),)))   # zero qty
    assert validate(Combo(id="bad", name="b", output_id=1, inputs=((2, 1),), output_yield=0))


def test_load_filters_by_kind():
    assert all(c.kind == "set" for c in load("set"))
    assert set(load()) == set(load("set")) | set(load("recipe"))


# --- decant recipes (derived from /mapping) --------------------------------------------------------

def test_decant_recipes_conserve_doses_and_return_vials():
    recipes = decant_recipes(_FAKE_MAPPING)
    prayer = {c.id: c for c in recipes if "prayer" in c.id}
    assert set(prayer) == {"decant_prayer_potion_1_to_4", "decant_prayer_potion_2_to_4",
                           "decant_prayer_potion_3_to_4"}
    # (source_dose_id, input_qty) → yield, freed vials — doses conserved (dose×qty == 4×yield)
    expect = {1: (11, 4, 1.0, 3), 2: (12, 2, 1.0, 1), 3: (13, 4, 3.0, 1)}
    for src, (in_id, in_qty, yld, vials) in expect.items():
        c = prayer[f"decant_prayer_potion_{src}_to_4"]
        assert c.inputs == ((in_id, in_qty),) and c.output_id == 14
        assert src * in_qty == 4 * yld == c.output_yield * 4        # dose conservation
        assert c.byproducts == ((229, vials),)                      # vials returned = inputs − outputs
        assert in_qty - yld == vials
        assert (c.in_dose, c.out_dose) == (src, 4)                  # display labels for the terminal output


def test_decant_recipes_are_one_way_skill_free_recipes():
    for c in decant_recipes(_FAKE_MAPPING):
        assert c.kind == "recipe" and not c.reversible
        assert c.skill is None and c.fee == 0 and c.output_type == "item"
        assert validate(c) == [], f"{c.id}: {validate(c)}"
    slugs = [c.id for c in decant_recipes(_FAKE_MAPPING)]
    assert len(slugs) == len(set(slugs))


def test_decant_skips_incomplete_potion_families():
    # Agility mix only ships (1),(2) → no full (4)-dose target, so no recipes.
    assert not any("agility_mix" in c.id for c in decant_recipes(_FAKE_MAPPING))


def test_slug_disambiguates_plus_variants():
    assert _slug("Antidote") != _slug("Antidote+") != _slug("Antidote++")
    assert _slug("Antidote+") == "antidote_plus"
    ids = {c.id for c in decant_recipes(_FAKE_MAPPING)}
    assert "decant_antidote_plus_1_to_4" in ids       # the '+' family kept its own slug


def test_decant_without_vial_in_mapping_omits_byproduct():
    no_vial = [it for it in _FAKE_MAPPING if it["name"] != "Vial"]
    assert all(c.byproducts == () for c in decant_recipes(no_vial))


@pytest.mark.skipif(os.environ.get("OSRS_FLIPPER_NET_TESTS") != "1",
                    reason="hits the live /mapping API; set OSRS_FLIPPER_NET_TESTS=1 to run")
def test_all_ids_resolve_in_live_mapping():
    from osrs_flipper import api
    mapping = api.mapping()
    mapping_ids = {r["id"] for r in mapping}
    for c in load():
        assert validate(c, mapping_ids) == [], f"{c.id}: {validate(c, mapping_ids)}"
    recipes = decant_recipes(mapping)
    assert len(recipes) >= 150, f"expected many decant recipes, got {len(recipes)}"
    for c in recipes:
        assert validate(c, mapping_ids) == [], f"{c.id}: {validate(c, mapping_ids)}"

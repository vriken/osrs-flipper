"""The bundled combination table must be structurally sound (unique slugs, positive int ids/qtys,
non-empty inputs). Wrong component ids give wrong advice, so an optional network-gated check confirms
every id resolves in the live /mapping and members flags line up. Run that with
OSRS_FLIPPER_NET_TESTS=1."""

import os

import pytest

from osrs_flipper.combinations import Combo, item_ids, load, validate


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


@pytest.mark.skipif(os.environ.get("OSRS_FLIPPER_NET_TESTS") != "1",
                    reason="hits the live /mapping API; set OSRS_FLIPPER_NET_TESTS=1 to run")
def test_all_ids_resolve_in_live_mapping():
    from osrs_flipper import api
    mapping_ids = {r["id"] for r in api.mapping()}
    for c in load():
        assert validate(c, mapping_ids) == [], f"{c.id}: {validate(c, mapping_ids)}"

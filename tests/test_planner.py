"""The unified planner must rank fast flips, gear, and sets on one honest per-slot currency: haircut
best-case (patient) EV, normalize a multi-slot set against N single flips, dedupe, and fill only the free
slots."""

from osrs_flipper.planner import Candidate, per_slot_score, rank
from osrs_flipper.terminal import Terminal


def flip(key, window_gp, iid, slots=1):
    return Candidate(kind="flip", key=key, slots=slots, window_gp=window_gp, patient=False,
                     item_ids=(iid,))


def gear(key, window_gp, iid):
    return Candidate(kind="gear", key=key, slots=1, window_gp=window_gp, patient=True, item_ids=(iid,))


def setc(key, window_gp, ids):
    return Candidate(kind="set", key=key, slots=len(ids), window_gp=window_gp, patient=True, item_ids=ids)


def test_haircut_applies_to_patient_only():
    f = flip("f", 1000, 1)
    g = gear("g", 1000, 2)
    assert per_slot_score(f, patient_confidence=0.5) == 1000       # honest flip untouched
    assert per_slot_score(g, patient_confidence=0.5) == 500        # best-case gear discounted


def test_per_slot_normalization_for_sets():
    s = setc("set", 4000, (1, 2, 3, 4))  # 4 slots
    assert per_slot_score(s, patient_confidence=1.0) == 1000       # 4000 / 4 slots


def test_high_throughput_flip_beats_best_case_set_in_long_window():
    # daytime: a cheap flip cycles → large window_gp; a set fills once (best-case, haircut)
    f = flip("Yew logs", 60_000, 1)
    s = setc("Ahrim's set", 120_000, (10, 11, 12, 13))   # 30k/slot pre-haircut
    picks = rank([s, f], free_slots=4, patient_confidence=0.55)
    assert picks[0].key == "Yew logs"                    # 60k/slot > 30k×0.55 = 16.5k/slot


def test_set_wins_when_flips_are_weak_one_shot():
    # overnight: flips priced one-shot (small window_gp) → a strong set wins the slots
    flips = [flip(f"f{i}", 5_000, i) for i in range(4)]
    s = setc("Torva set", 90_000, (100, 101, 102))       # 30k/slot ×0.55 = 16.5k > 5k
    picks = rank([*flips, s], free_slots=4, patient_confidence=0.55)
    assert picks[0].key == "Torva set"


def test_set_skipped_when_it_does_not_fit_free_slots():
    s = setc("4-piece set", 100_000, (1, 2, 3, 4))        # needs 4 slots
    f = flip("f", 1_000, 9)
    picks = rank([s, f], free_slots=2, patient_confidence=1.0)
    assert [p.key for p in picks] == ["f"]                # set can't fit 2 slots → skipped


def test_dedupe_by_item_ids_and_exclude():
    a = flip("dupe", 1000, 5)
    b = flip("dupe2", 900, 5)                              # same item id → only one taken
    picks = rank([a, b], free_slots=3, patient_confidence=1.0)
    assert [p.key for p in picks] == ["dupe"]
    # an excluded (already held) id is never picked
    assert rank([a], free_slots=3, patient_confidence=1.0, exclude_ids={5}) == []


def test_fills_only_free_slots_in_value_order():
    cands = [flip("a", 300, 1), flip("b", 900, 2), flip("c", 600, 3)]
    picks = rank(cands, free_slots=2, patient_confidence=1.0)
    assert [p.key for p in picks] == ["b", "c"]


def test_nonpositive_window_gp_skipped():
    assert rank([flip("loss", -10, 1), flip("zero", 0, 2)], free_slots=2, patient_confidence=1.0) == []


def test_flip_window_gp_cycles_fast_and_holds_once():
    # a fast flip cycles within the window (throughput > one cycle); a slow flip or a hold fills once
    fwg = Terminal._flip_window_gp
    fast = {"gp": 1000, "buy_eta_h": 0.5, "tier": "active"}   # ~1h round-trip → ~6 cycles in 6h
    slow = {"gp": 1000, "buy_eta_h": 5.0, "tier": "active"}   # 10h round-trip > 6h → one cycle
    hold = {"gp": 1000, "buy_eta_h": 0.5, "tier": "hold"}     # accumulation fills once, no cycling
    assert fwg(fast, 6.0) > 1000
    assert fwg(slow, 6.0) == 1000
    assert fwg(hold, 6.0) == 1000


def test_verify_drops_patient_pick_but_not_flips():
    # verify (the anomaly gate) runs only on patient picks; a rejected gear falls through to the flip
    g = gear("pumped gear", 100_000, 1)
    f = flip("clean flip", 10_000, 2)
    picks = rank([g, f], free_slots=1, patient_confidence=1.0, verify=lambda c: False)
    assert [p.key for p in picks] == ["clean flip"]        # gear vetoed, flip (not verified) kept
    picks = rank([g, f], free_slots=1, patient_confidence=1.0, verify=lambda c: True)
    assert [p.key for p in picks] == ["pumped gear"]        # gear accepted → wins on value

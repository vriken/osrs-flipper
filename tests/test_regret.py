"""Pull evaluation: good_pull when the dropped flip degraded, regret when it held up."""

from osrs_flipper import regret


def test_good_pull_when_the_spread_dies_or_collapses():
    assert regret.classify_pull(snap_net=50, cur_net=0, min_net=2) == "good_pull"    # gone
    assert regret.classify_pull(snap_net=50, cur_net=20, min_net=2) == "good_pull"   # <50% of pre-pull


def test_regret_when_the_spread_holds_up():
    assert regret.classify_pull(snap_net=50, cur_net=45, min_net=2) == "regret"


def test_none_without_a_current_price():
    assert regret.classify_pull(50, None, min_net=2) is None


def test_below_min_net_is_a_good_pull_even_without_a_snapshot():
    assert regret.classify_pull(None, 1, min_net=2) == "good_pull"
    assert regret.classify_pull(None, 5, min_net=2) == "regret"

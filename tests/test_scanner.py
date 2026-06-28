"""Composite score: the online↔offline time weight must behave correctly."""

from osrs_flipper.scanner import MODE_WEIGHTS, _composite


def test_offline_ignores_fill_time():
    assert _composite(1000, fill_eta_h=2.0, time_weight=0.0) == 1000
    assert _composite(1000, fill_eta_h=99.0, time_weight=0.0) == 1000  # slow is fine offline


def test_online_divides_by_fill_time():
    assert _composite(1000, fill_eta_h=2.0, time_weight=1.0) == 500  # gp per hour


def test_balanced_uses_sqrt_of_time():
    assert _composite(1000, fill_eta_h=4.0, time_weight=0.5) == 500  # 1000 / sqrt(4)


def test_unknown_fill_time_unrankable_when_time_matters():
    assert _composite(1000, fill_eta_h=None, time_weight=1.0) == 0.0
    assert _composite(1000, fill_eta_h=None, time_weight=0.0) == 1000  # but fine offline


def test_mode_weights():
    assert MODE_WEIGHTS == {"online": 1.0, "balanced": 0.5, "offline": 0.0}

"""Composite score + shrinkage behaviour."""

from osrs_flipper.scanner import MODE_WEIGHTS, _composite, _shrink


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


def test_shrink_pulls_unreliable_to_median():
    # an inflated but unreliable estimate (1000, reliability 0) collapses to the median
    out = _shrink([1000, 100, 100, 100, 100], [0.0, 1.0, 1.0, 1.0, 1.0])
    assert out[0] == 100  # median
    assert out[1] == 100  # reliable, unchanged


def test_shrink_identity_when_fully_reliable():
    assert _shrink([5, 3, 1], [1.0, 1.0, 1.0]) == [5, 3, 1]


def test_shrink_partial():
    # median of [300,100] = 200; reliability 0.5 pulls 300 halfway toward it → 250
    out = _shrink([300, 100], [0.5, 1.0])
    assert out == [250, 100]


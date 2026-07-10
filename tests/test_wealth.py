"""The wealth-cap glide factor ramps 0→1 from the start threshold to the liquid cap."""

import pytest

from osrs_flipper import wealth

CAP = 1_000_000  # a round cap for the arithmetic; start_frac 0.7 → glide begins at 700k


@pytest.mark.parametrize("nw,expected", [
    (0, 0.0),
    (500_000, 0.0),          # below the 70% start → no tilt
    (700_000, 0.0),          # exactly at the start → still 0
    (850_000, 0.5),          # halfway from start (700k) to cap (1M)
    (1_000_000, 1.0),        # at the cap → full tilt
    (2_000_000, 1.0),        # above the cap stays clamped at 1
])
def test_glide_factor_ramp(nw, expected):
    assert wealth.glide_factor(nw, cap=CAP, start_frac=0.7) == pytest.approx(expected)


def test_glide_factor_defaults_to_config(monkeypatch):
    monkeypatch.setattr(wealth.config, "MAX_LIQUID_GP", CAP)
    monkeypatch.setattr(wealth.config, "CAP_GLIDE_START_FRAC", 0.7)
    assert wealth.glide_factor(600_000) == 0.0
    assert wealth.glide_factor(1_000_000) == 1.0

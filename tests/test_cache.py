"""Disk cache: round-trips within TTL, misses once expired."""

from osrs_flipper import cache


def test_round_trip_within_ttl():
    cache.set("unit-test-key", {"a": 1, "b": [2, 3]})
    assert cache.get("unit-test-key", ttl=100) == {"a": 1, "b": [2, 3]}


def test_miss_when_expired():
    cache.set("unit-test-key-2", {"x": 1})
    assert cache.get("unit-test-key-2", ttl=0) is None  # ttl=0 → always stale


def test_miss_when_absent():
    assert cache.get("no-such-key-xyz", ttl=100) is None

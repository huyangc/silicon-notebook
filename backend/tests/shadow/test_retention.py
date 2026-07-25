from __future__ import annotations

import pytest

from app.migration.shadow.retention import (
    RetentionBounds,
    deletion_exclusive_ceiling,
)


def _bounds(**overrides) -> RetentionBounds:
    values = {
        "verified_checkpoint": 900_000,
        "active_verifier_barrier": None,
        "replay_checkpoint": None,
        "poison_seq": None,
        "cutoff_utc": "2026-07-01 00:00:00",
    }
    values.update(overrides)
    return RetentionBounds(**values)


def test_retention_requires_verified_checkpoint_and_preserves_tail():
    assert deletion_exclusive_ceiling(
        _bounds(), source_high_water=1_000_000, tail_events=100_000
    ) == 900_000
    assert deletion_exclusive_ceiling(
        _bounds(verified_checkpoint=200_000),
        source_high_water=1_000_000,
        tail_events=100_000,
    ) == 200_001


def test_oldest_barrier_replay_and_poison_each_tighten_the_bound():
    bounds = _bounds(
        active_verifier_barrier=700_000,
        replay_checkpoint=600_000,
        poison_seq=500_000,
    )
    assert deletion_exclusive_ceiling(
        bounds, source_high_water=1_000_000, tail_events=100_000
    ) == 500_000


def test_poison_and_every_later_event_are_retained():
    assert deletion_exclusive_ceiling(
        _bounds(poison_seq=42), source_high_water=2_000_000, tail_events=100_000
    ) == 42


@pytest.mark.parametrize("tail", [0, -1, True])
def test_invalid_tail_is_rejected(tail):
    with pytest.raises(ValueError):
        deletion_exclusive_ceiling(_bounds(), source_high_water=10, tail_events=tail)


def test_empty_or_small_log_has_no_deletable_prefix():
    assert deletion_exclusive_ceiling(
        _bounds(), source_high_water=99_999, tail_events=100_000
    ) == 0

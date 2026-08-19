"""PostgreSQL conformance for ``RetrievalExperienceStorePort`` (Agentic Memory
P2, T5).

Scope is deliberately narrow, mirroring
``test_agent_profile_store_conformance.py``'s rationale: the SQLite side
already has full behavioural coverage in
``tests/test_retrieval_experience_store.py`` (this store is a behavioural
mirror by design). This file proves only what is genuinely backend-specific:

- ``jsonb`` round-trips ``situation``/``provenance`` the same way JSON text
  does on SQLite — including that a stored row still re-hashes to its own
  content-addressed id after ``jsonb`` has reordered its keys, which is the
  property the cross-deployment merge depends on;
- the merge's read takes real ``FOR UPDATE`` row locking instead of SQLite's
  process-wide write mutex, so an overlapping batch cannot double-count
  ``support``;
- eviction is expressed here as a ``DESC``-ordered ``OFFSET`` while SQLite
  uses an ``ASC``-ordered ``LIMIT``. Those coincide only because the ordering
  is TOTAL, so the tie-break has to be asserted on THIS backend rather than
  assumed from the other one.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from app.repositories.postgres.retrieval_experience_store import (
    RetrievalExperienceStore,
)
from app.services.repository_runtime import RepositoryCompatibilitySeams
from app.services.retrieval_experience_projection import experience_id

NOW = "2026-08-19T00:00:00+00:00"
LATER = "2026-09-19T00:00:00+00:00"

SITUATION = {
    "mode": "reasoning",
    "result_scope": "ranked",
    "retrieval_effort": "standard",
    "completeness_required": False,
    "entity_count": "few",
    "topic_count": "many",
    "has_constraints": True,
    "has_exclusions": False,
}

pytestmark = pytest.mark.postgres_integration


class _Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> str:
        return self.value


def _seams(clock) -> RepositoryCompatibilitySeams:
    lock = threading.Lock()
    counter: dict[str, int] = {}

    def new_id(prefix: str) -> str:
        with lock:
            counter[prefix] = counter.get(prefix, 0) + 1
            return f"{prefix}-rx-{counter[prefix]:04d}"

    return RepositoryCompatibilitySeams(
        new_id=new_id,
        now=clock,
        copy_chunk_size=lambda: 100,
        remap_json_ids=lambda value, _mapping: value,
        in_chunk_size=lambda: 100,
    )


@dataclass
class RetrievalExperienceHarness:
    database: object
    store: RetrievalExperienceStore
    clock: _Clock


@pytest.fixture
def retrieval_experience_harness(request) -> RetrievalExperienceHarness:
    clock = _Clock()
    seams = _seams(clock)
    database = request.getfixturevalue("postgres_database")
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(database).migrate() == 31
    # Nothing is seeded: this table has no foreign key in either direction and
    # no tenancy column, which is the structural fact behind its deep-copy
    # exclusion and its global-union classification.
    yield RetrievalExperienceHarness(
        database=database,
        store=RetrievalExperienceStore(database, now=seams.now),
        clock=clock,
    )


def _upsert(harness, entry_id, **overrides):
    kwargs = {
        "situation": SITUATION,
        "action": "exact_lookup",
        "polarity": "bad",
        "rationale": "这类问题里精查基本空手",
        "provenance": ["run-1"],
        "provenance_max": 10,
        "replace_conclusion": False,
    }
    kwargs.update(overrides)
    return harness.store.upsert_experience(entry_id, **kwargs)


def test_jsonb_round_trips_the_situation_and_provenance(
    retrieval_experience_harness,
):
    harness = retrieval_experience_harness
    row = _upsert(harness, "rx_one", provenance=["run-1", "run-2"])
    assert row["situation"] == SITUATION
    assert row["provenance"] == ["run-1", "run-2"]
    assert isinstance(row["situation"]["completeness_required"], bool)
    stored = harness.store.read_experience("rx_one")
    assert stored["situation"] == SITUATION


def test_a_stored_row_still_re_hashes_to_its_own_id_after_jsonb(
    retrieval_experience_harness,
):
    """``jsonb`` discards key order, so this is the backend where "an entry's
    id can be re-verified from its row" could quietly stop being true — and
    that verifiability is what makes ``merge_dbs``' primary-key union an
    auditable operation rather than a trusted one."""
    harness = retrieval_experience_harness
    entry_id = experience_id(SITUATION, "exact_lookup")
    _upsert(harness, entry_id)
    row = harness.store.read_experience(entry_id)
    assert experience_id(row["situation"], row["action"]) == entry_id


def test_an_overlapping_batch_does_not_double_count_support(
    retrieval_experience_harness,
):
    harness = retrieval_experience_harness
    _upsert(harness, "rx_one", provenance=["run-1", "run-2"])
    row = _upsert(harness, "rx_one", provenance=["run-2", "run-3"])
    assert row["support"] == 3
    assert row["provenance"] == ["run-1", "run-2", "run-3"]


def test_a_no_op_merge_leaves_updated_at_alone(retrieval_experience_harness):
    harness = retrieval_experience_harness
    _upsert(harness, "rx_one", provenance=["run-1"])
    harness.clock.value = LATER
    row = _upsert(harness, "rx_one", provenance=["run-1"])
    assert row["updated_at"].startswith("2026-08-19")


def test_an_update_replaces_the_conclusion_without_moving_support(
    retrieval_experience_harness,
):
    harness = retrieval_experience_harness
    _upsert(harness, "rx_one", provenance=["run-1"])
    row = _upsert(
        harness,
        "rx_one",
        provenance=["run-1"],
        polarity="good",
        rationale="改口了",
        replace_conclusion=True,
    )
    assert row["polarity"] == "good"
    assert row["rationale"] == "改口了"
    assert row["support"] == 1


def test_eviction_keeps_the_best_entries_and_breaks_ties_by_id(
    retrieval_experience_harness,
):
    """The DESC/OFFSET shape here has to delete exactly what SQLite's
    ASC/LIMIT shape deletes, and it only does so because ``id`` makes the
    ordering total."""
    harness = retrieval_experience_harness
    for entry_id in ("rx_aaa", "rx_bbb", "rx_ccc"):
        _upsert(harness, entry_id, provenance=[f"{entry_id}-run"])
    harness.store.note_adopted(["rx_ccc"], 2)

    assert harness.store.evict_to_limit(2) == 1
    surviving = {row["id"] for row in harness.store.read_all(50)}
    # rx_ccc survives on ``adopted``; between the two tied entries the id
    # tie-break keeps the LATER one (descending order names survivors), which
    # is the same row SQLite's ascending LIMIT would have spared.
    assert surviving == {"rx_ccc", "rx_bbb"}


def test_read_all_orders_by_the_c_collation(retrieval_experience_harness):
    harness = retrieval_experience_harness
    for entry_id in ("rx_b", "rx_A", "rx_a"):
        _upsert(harness, entry_id, provenance=[f"{entry_id}-run"])
    # ``COLLATE "C"`` is byte order, so uppercase sorts before lowercase —
    # matching SQLite's default binary collation rather than a locale's.
    assert [row["id"] for row in harness.store.read_all(50)] == [
        "rx_A", "rx_a", "rx_b",
    ]


def test_note_adopted_refuses_a_negative_delta(retrieval_experience_harness):
    harness = retrieval_experience_harness
    _upsert(harness, "rx_one")
    with pytest.raises(ValueError):
        harness.store.note_adopted(["rx_one"], -1)

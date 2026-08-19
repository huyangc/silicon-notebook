"""Store-level coverage for ``RetrievalExperienceStorePort`` (Agentic Memory
P2, T5).

Built directly on ``app.repositories.sqlite.retrieval_experience_store`` plus a
bare migrated ``SqliteDatabase``, mirroring ``test_agent_profile_store.py``'s
rationale: this file proves the STORE primitive in isolation, and the
distillation service that drives it has its own file.

The table has no foreign key in either direction and no tenancy column, so
these tests seed nothing at all — which is itself worth noticing, because it is
the structural fact that makes deep copy unable to reach the table and makes
``scripts/merge_dbs.py`` classify it as a global union table.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator
from app.repositories.sqlite.retrieval_experience_store import (
    RetrievalExperienceStore,
)
from app.core.config import Settings

SITUATION = {
    "mode": "reasoning",
    "result_scope": "ranked",
    "retrieval_effort": "standard",
    "completeness_required": False,
    "entity_count": "few",
    "topic_count": "few",
    "has_constraints": False,
    "has_exclusions": False,
}


class _Clock:
    """A clock the test drives, so ``updated_at`` assertions are about the
    store's decisions rather than about how fast the test ran."""

    def __init__(self) -> None:
        self.value = "2026-08-19T00:00:00+00:00"

    def __call__(self) -> str:
        return self.value


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def store(tmp_path: Path, clock: _Clock) -> RetrievalExperienceStore:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}")
    database = SqliteDatabase(settings, tmp_path)
    migrated = SqliteMigrator(database, settings).migrate()
    assert migrated, "fresh database must actually run the migration ladder"
    return RetrievalExperienceStore(database, now=clock)


def test_a_fresh_entry_counts_its_provenance_as_support(store):
    row = store.upsert_experience(
        "rx_one",
        situation=SITUATION,
        action="exact_lookup",
        polarity="bad",
        rationale="很少命中",
        provenance=["run-1", "run-2"],
        provenance_max=10,
        replace_conclusion=False,
    )
    assert row["support"] == 2
    assert row["adopted"] == 0
    assert row["provenance"] == ["run-1", "run-2"]
    assert row["situation"] == SITUATION
    assert store.count() == 1


def test_a_repeated_batch_does_not_inflate_support(store):
    """The single property that lets this feature work with no cursor table.

    Distillation reads "the most recent N completed asks" with no watermark, so
    two rounds close together see overlapping runs by construction. If the
    overlap counted twice, ``support`` would measure how often distillation ran
    rather than how much evidence exists — and ``support`` is the second key of
    the eviction ordering.
    """
    for _ in range(3):
        row = store.upsert_experience(
            "rx_one",
            situation=SITUATION,
            action="ppr",
            polarity="good",
            rationale="图谱库里值得先试",
            provenance=["run-1", "run-2"],
            provenance_max=10,
            replace_conclusion=False,
        )
    assert row["support"] == 2
    assert row["provenance"] == ["run-1", "run-2"]


def test_new_runs_add_support_and_the_provenance_list_stays_bounded(store):
    store.upsert_experience(
        "rx_one",
        situation=SITUATION,
        action="ppr",
        polarity="good",
        rationale="先试",
        provenance=["run-1", "run-2"],
        provenance_max=3,
        replace_conclusion=False,
    )
    row = store.upsert_experience(
        "rx_one",
        situation=SITUATION,
        action="ppr",
        polarity="good",
        rationale="先试",
        provenance=["run-2", "run-3", "run-4"],
        provenance_max=3,
        replace_conclusion=False,
    )
    # run-2 was already known, so support grew by the two genuinely new ids.
    assert row["support"] == 4
    # The retained list keeps the NEWEST ids — dropping the oldest is what
    # keeps the bound, and keeping the newest is what makes the next
    # overlapping batch de-duplicate against something useful.
    assert row["provenance"] == ["run-2", "run-3", "run-4"]


def test_an_add_that_lands_on_an_existing_entry_keeps_its_conclusion(store):
    store.upsert_experience(
        "rx_one",
        situation=SITUATION,
        action="ppr",
        polarity="good",
        rationale="原来的说法",
        provenance=["run-1"],
        provenance_max=10,
        replace_conclusion=False,
    )
    row = store.upsert_experience(
        "rx_one",
        situation=SITUATION,
        action="ppr",
        polarity="bad",
        rationale="新的说法",
        provenance=["run-2"],
        provenance_max=10,
        replace_conclusion=False,
    )
    assert row["polarity"] == "good"
    assert row["rationale"] == "原来的说法"
    assert row["support"] == 2


def test_an_update_replaces_the_conclusion(store):
    store.upsert_experience(
        "rx_one",
        situation=SITUATION,
        action="ppr",
        polarity="good",
        rationale="原来的说法",
        provenance=["run-1"],
        provenance_max=10,
        replace_conclusion=False,
    )
    row = store.upsert_experience(
        "rx_one",
        situation=SITUATION,
        action="ppr",
        polarity="bad",
        rationale="新的说法",
        provenance=["run-1"],
        provenance_max=10,
        replace_conclusion=True,
    )
    assert row["polarity"] == "bad"
    assert row["rationale"] == "新的说法"
    # No NEW run ids, so the evidence count must not move even though the
    # conclusion did.
    assert row["support"] == 1


def test_a_no_op_merge_does_not_refresh_updated_at(store, clock):
    """``updated_at`` is the last tie-break of the eviction ordering.

    An entry that keeps being re-observed with no new runs and no new
    conclusion must age like any other, or it outlives entries with strictly
    more evidence purely because distillation kept looking at it.
    """
    store.upsert_experience(
        "rx_one",
        situation=SITUATION,
        action="ppr",
        polarity="good",
        rationale="先试",
        provenance=["run-1"],
        provenance_max=10,
        replace_conclusion=False,
    )
    clock.value = "2026-09-01T00:00:00+00:00"
    row = store.upsert_experience(
        "rx_one",
        situation=SITUATION,
        action="ppr",
        polarity="good",
        rationale="先试",
        provenance=["run-1"],
        provenance_max=10,
        replace_conclusion=False,
    )
    assert row["updated_at"] == "2026-08-19T00:00:00+00:00"


def _seed(store, clock, entry_id: str, *, adopted: int, support: int, at: str):
    clock.value = at
    store.upsert_experience(
        entry_id,
        situation=SITUATION,
        action="ppr",
        polarity="good",
        rationale="x",
        provenance=[f"{entry_id}-run-{i}" for i in range(support)],
        provenance_max=50,
        replace_conclusion=False,
    )
    if adopted:
        store.note_adopted([entry_id], adopted)


def test_eviction_drops_unused_entries_before_thinly_supported_ones(store, clock):
    _seed(store, clock, "rx_used", adopted=5, support=1, at="2026-08-01T00:00:00+00:00")
    _seed(store, clock, "rx_backed", adopted=0, support=9, at="2026-08-02T00:00:00+00:00")
    _seed(store, clock, "rx_thin", adopted=0, support=1, at="2026-08-03T00:00:00+00:00")

    assert store.evict_to_limit(2) == 1
    surviving = {row["id"] for row in store.read_all(50)}
    # ``adopted`` outranks ``support``: the single-run entry someone actually
    # acted on survives, the never-adopted single-run entry does not.
    assert surviving == {"rx_used", "rx_backed"}


def test_eviction_breaks_ties_by_id_not_by_insertion_order(store, clock):
    """The final tie-break is ``id``, and it is load-bearing twice over.

    Within one backend: SQLite's clock is second-granular (the
    ``memory_revisions`` lesson), so three entries written by the same batch
    tie on all three earlier keys and "which one survived" would otherwise be
    physical row order — a different answer after a VACUUM.

    Across backends: SQLite evicts the ``overflow`` WORST rows (ascending,
    LIMIT) while PostgreSQL keeps the ``max_entries`` BEST ones (descending,
    OFFSET). Those two describe the same deletion only while the ordering is
    TOTAL, which is exactly what this tie-break provides.

    The entries are inserted in DESCENDING id order on purpose: insertion order
    and id order then disagree, so an implementation that fell back on physical
    order would evict ``rx_ccc`` instead of ``rx_aaa``.
    """
    for name in ("rx_ccc", "rx_bbb", "rx_aaa"):
        _seed(store, clock, name, adopted=0, support=1, at="2026-08-01T00:00:00+00:00")
    assert store.evict_to_limit(2) == 1
    assert {row["id"] for row in store.read_all(50)} == {"rx_bbb", "rx_ccc"}


def test_eviction_is_a_no_op_below_the_limit(store, clock):
    _seed(store, clock, "rx_a", adopted=0, support=1, at="2026-08-01T00:00:00+00:00")
    assert store.evict_to_limit(300) == 0
    assert store.count() == 1


def test_note_adopted_only_touches_the_named_entries(store, clock):
    _seed(store, clock, "rx_a", adopted=0, support=1, at="2026-08-01T00:00:00+00:00")
    _seed(store, clock, "rx_b", adopted=0, support=1, at="2026-08-01T00:00:00+00:00")
    assert store.note_adopted(["rx_a"]) == 1
    by_id = {row["id"]: row for row in store.read_all(50)}
    assert by_id["rx_a"]["adopted"] == 1
    assert by_id["rx_b"]["adopted"] == 0


def test_note_adopted_refuses_a_negative_delta(store, clock):
    _seed(store, clock, "rx_a", adopted=0, support=1, at="2026-08-01T00:00:00+00:00")
    with pytest.raises(ValueError):
        store.note_adopted(["rx_a"], -1)


def test_read_all_is_deterministically_ordered_and_bounded(store, clock):
    for name in ("rx_c", "rx_a", "rx_b"):
        _seed(store, clock, name, adopted=0, support=1, at="2026-08-01T00:00:00+00:00")
    assert [row["id"] for row in store.read_all(50)] == ["rx_a", "rx_b", "rx_c"]
    assert [row["id"] for row in store.read_all(2)] == ["rx_a", "rx_b"]


def test_a_stored_row_round_trips_to_its_own_content_addressed_id(store):
    """The audit property behind the cross-deployment union.

    ``merge_dbs`` unions this table by primary key, which is only correct while
    an entry's id still describes its content. Re-hashing a row read back out
    of the store has to reproduce that id, or a merged database can hold an
    entry filed under a situation it is not about, and nothing would say so.
    """
    from app.services.retrieval_experience_projection import experience_id

    entry_id = experience_id(SITUATION, "exact_lookup")
    store.upsert_experience(
        entry_id,
        situation=SITUATION,
        action="exact_lookup",
        polarity="bad",
        rationale="x",
        provenance=["run-1"],
        provenance_max=10,
        replace_conclusion=False,
    )
    row = store.read_experience(entry_id)
    assert row is not None
    assert experience_id(row["situation"], row["action"]) == entry_id

"""Store-level coverage for ``RetrievalExperienceStorePort`` (Agentic Memory
P2, T5).

Built directly on ``app.repositories.sqlite.retrieval_experience_store`` plus a
bare migrated ``SqliteDatabase``, mirroring ``test_agent_profile_store.py``'s
rationale: this file proves the STORE primitive in isolation, and the
distillation service that drives it has its own file.

The table has no foreign key in either direction, so these tests seed no
notebook, no user and no run — and a partition id here is just a string nobody
has to have created. That is not an accident of the fixture: it is why a
deleted notebook's partition has to be cleared by an explicit registry entry
rather than by a cascade, and why ``scripts/merge_dbs.py`` still classifies the
table as a global union table after SQLite v79 gave it a ``notebook_id``.
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
    # ``provenance`` arrives NEWEST-FIRST — the only real caller builds it
    # from a query ordered ``created_at DESC`` — so ``run-2`` (newer) is
    # listed before ``run-1`` (older) here; the store reverses it before
    # storing, so the row itself comes back oldest-first.
    row = store.upsert_experience(
        "rx_one",
        situation=SITUATION,
        action="exact_lookup",
        polarity="bad",
        rationale="很少命中",
        provenance=["run-2", "run-1"],
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
            provenance=["run-2", "run-1"],  # newest-first, same batch each time
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
        provenance=["run-2", "run-1"],  # newest-first: run-2 newer than run-1
        provenance_max=3,
        replace_conclusion=False,
    )
    row = store.upsert_experience(
        "rx_one",
        situation=SITUATION,
        action="ppr",
        polarity="good",
        rationale="先试",
        provenance=["run-4", "run-3", "run-2"],  # newest-first
        provenance_max=3,
        replace_conclusion=False,
    )
    # run-2 was already known, so support grew by the two genuinely new ids.
    assert row["support"] == 4
    # The retained list keeps the NEWEST ids — dropping the oldest is what
    # keeps the bound, and keeping the newest is what makes the next
    # overlapping batch de-duplicate against something useful.
    assert row["provenance"] == ["run-2", "run-3", "run-4"]


def test_overlapping_batches_evict_the_genuinely_oldest_run_first(store):
    """The R1/R2/R3 shape the reviewers reproduced: three successive batches,
    each overlapping the previous one by design (distillation has no
    watermark), with ``provenance_max`` shrunk to 3 so eviction actually has
    to choose.

    Before the fix, ``incoming`` stayed in the caller's newest-first order and
    the trailing ``[-keep:]`` slice — meant to drop the oldest — instead cut
    into whichever batch's ids happened to land first in the concatenation,
    which is not necessarily the oldest ones. The property this test pins:
    after any sequence of overlapping batches, ``support`` equals the number
    of DISTINCT run ids actually observed (never inflated by a wrongly
    re-admitted id), and the surviving provenance is the newest 3, not some
    row order artifact.
    """
    # Batch R1: three runs, newest-first (r3 newest ... r1 oldest).
    row = store.upsert_experience(
        "rx_one", situation=SITUATION, action="ppr", polarity="bad",
        rationale="r1", provenance=["r3", "r2", "r1"],
        provenance_max=3, replace_conclusion=False,
    )
    assert row["support"] == 3
    assert row["provenance"] == ["r1", "r2", "r3"]

    # Batch R2: overlaps on r2/r3, adds one genuinely new run r4.
    row = store.upsert_experience(
        "rx_one", situation=SITUATION, action="ppr", polarity="bad",
        rationale="r2", provenance=["r4", "r3", "r2"],
        provenance_max=3, replace_conclusion=False,
    )
    assert row["support"] == 4
    assert row["provenance"] == ["r2", "r3", "r4"]

    # Batch R3: overlaps on r3/r4, adds r5. r1/r2 have aged out of the
    # bounded provenance list by now — that is the accepted invariant
    # (batch size <= provenance max), not a bug this test is proving against.
    row = store.upsert_experience(
        "rx_one", situation=SITUATION, action="ppr", polarity="bad",
        rationale="r3", provenance=["r5", "r4", "r3"],
        provenance_max=3, replace_conclusion=False,
    )
    assert row["support"] == 5
    # The newest 3 survive — r3, r4, r5 — not some mid-sequence artifact.
    assert row["provenance"] == ["r3", "r4", "r5"]


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
    surviving = {row["id"] for row in store.read_partition("", 50)}
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
    assert {row["id"] for row in store.read_partition("", 50)} == {"rx_bbb", "rx_ccc"}


def test_eviction_is_a_no_op_below_the_limit(store, clock):
    _seed(store, clock, "rx_a", adopted=0, support=1, at="2026-08-01T00:00:00+00:00")
    assert store.evict_to_limit(300) == 0
    assert store.count() == 1


def test_note_adopted_only_touches_the_named_entries(store, clock):
    _seed(store, clock, "rx_a", adopted=0, support=1, at="2026-08-01T00:00:00+00:00")
    _seed(store, clock, "rx_b", adopted=0, support=1, at="2026-08-01T00:00:00+00:00")
    assert store.note_adopted(["rx_a"]) == 1
    by_id = {row["id"]: row for row in store.read_partition("", 50)}
    assert by_id["rx_a"]["adopted"] == 1
    assert by_id["rx_b"]["adopted"] == 0


def test_note_adopted_refuses_a_negative_delta(store, clock):
    _seed(store, clock, "rx_a", adopted=0, support=1, at="2026-08-01T00:00:00+00:00")
    with pytest.raises(ValueError):
        store.note_adopted(["rx_a"], -1)


def test_read_partition_is_deterministically_ordered_and_bounded(store, clock):
    for name in ("rx_c", "rx_a", "rx_b"):
        _seed(store, clock, name, adopted=0, support=1, at="2026-08-01T00:00:00+00:00")
    assert [row["id"] for row in store.read_partition("", 50)] == ["rx_a", "rx_b", "rx_c"]
    assert [row["id"] for row in store.read_partition("", 2)] == ["rx_a", "rx_b"]


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


def _signal(store, partition: str = ""):
    """This partition's own half of ``version_signal``.

    The method returns ``(this partition, the global partition)`` so one
    aggregate serves a run that reads both; tests about one partition want the
    first half, and ``partition=""`` makes both halves the same tuple anyway.
    """
    return store.version_signal(partition)[0]


def test_the_version_signal_tracks_inserts_updates_and_evictions(store, clock):
    """The injection side's memo key:
    ``(mutation revision, row count, newest updated_at)``.

    The DB halves catch what each other misses — an in-place UPDATE leaves the
    count alone (the id is content-addressed), an eviction leaves the newest
    timestamp alone (it deletes the oldest rows) — and the in-process revision
    (codex #524 R12 P2) moves on EVERY write, closing the cases the two DB
    halves cannot see (lexicographic MAX over offset-carrying text under a
    UTC-offset change or clock step, same-signature evict+insert batches).
    """
    assert store.version_signal("") == ((0, 0, ""), (0, 0, ""))

    _seed(store, clock, "rx_one", adopted=0, support=1, at="2026-08-01T00:00:00+00:00")
    inserted = _signal(store)
    assert inserted[1:] == (1, "2026-08-01T00:00:00+00:00")
    assert inserted[0] > 0

    clock.value = "2026-08-02T00:00:00+00:00"
    store.upsert_experience(
        "rx_one",
        situation=SITUATION,
        action="ppr",
        polarity="bad",
        rationale="换了一个结论",
        provenance=["rx_one-new-run"],
        provenance_max=50,
        replace_conclusion=True,
    )
    updated = _signal(store)
    assert updated[1:] == (1, "2026-08-02T00:00:00+00:00")
    assert updated[0] > inserted[0]

    _seed(store, clock, "rx_two", adopted=0, support=1, at="2026-08-03T00:00:00+00:00")
    before_evict = _signal(store)
    assert store.evict_to_limit(1) == 1
    after_evict = _signal(store)
    assert after_evict[1] == 1
    assert after_evict[0] > before_evict[0]


def test_an_adoption_is_deliberately_invisible_to_the_version_signal(store, clock):
    """``note_adopted`` must not move ``updated_at`` — that column is the last
    tie-break of the eviction ordering, and letting an adoption refresh it
    would make a frequently-injected entry immortal. The memo therefore misses
    adoptions, which is correct: ``adopted`` is neither rendered into the
    prompt block nor part of the injection-side selection ordering, so a memo
    that misses it still serves identical rows.

    The clock is advanced BEFORE ``note_adopted`` — without that, this
    assertion would hold even if ``note_adopted`` wrote ``updated_at``,
    because ``now()`` at seed time and at adoption time would be identical and
    the signal would coincidentally match either way. Advancing the clock is
    what makes "the signal did not move" prove the invariant rather than
    prove nothing.
    """
    _seed(store, clock, "rx_one", adopted=0, support=1, at="2026-08-01T00:00:00+00:00")
    before = _signal(store)
    clock.value = "2026-08-19T00:00:00+00:00"
    store.note_adopted(["rx_one"])
    assert _signal(store) == before


def test_a_write_moves_the_signal_even_when_the_db_halves_cannot_see_it(store, clock):
    """codex #524 R12 P2:不推时钟做一次 replace_conclusion——count 与
    MAX(updated_at) 都纹丝不动,只有进程内修订能让注入缓存看见这次更新。"""
    _seed(store, clock, "rx_one", adopted=0, support=1, at="2026-08-01T00:00:00+00:00")
    before = _signal(store)
    store.upsert_experience(
        "rx_one",
        situation=SITUATION,
        action="ppr",
        polarity="good",
        rationale="同一时刻改写结论",
        provenance=[],
        provenance_max=50,
        replace_conclusion=True,
    )
    after = _signal(store)
    assert after[1:] == before[1:]     # DB 两元对这次写完全失明
    assert after[0] > before[0]        # 修订元看得见


# ----------------------------------------------------- SQLite v79: partitions


def _seed_in(store, clock, entry_id: str, partition: str, *, at: str):
    clock.value = at
    store.upsert_experience(
        entry_id,
        situation=SITUATION,
        action="ppr",
        polarity="good",
        rationale="x",
        provenance=[f"{entry_id}-run"],
        provenance_max=50,
        replace_conclusion=False,
        notebook_id=partition,
    )


def test_a_partitioned_read_sees_only_its_own_partition(store, clock):
    """The whole point of v79, stated as the smallest possible property.

    Three partitions, one entry each. Each read has to return exactly one row,
    and the global read must not act as "everything" — the failure this pins is
    a predicate accidentally dropped from ``read_partition``, which would still
    look right on any fixture that only ever wrote one partition.
    """
    _seed_in(store, clock, "rx_g", "", at="2026-08-01T00:00:00+00:00")
    _seed_in(store, clock, "rx_a", "nb-a", at="2026-08-01T00:00:00+00:00")
    _seed_in(store, clock, "rx_b", "nb-b", at="2026-08-01T00:00:00+00:00")

    assert [row["id"] for row in store.read_partition("", 50)] == ["rx_g"]
    assert [row["id"] for row in store.read_partition("nb-a", 50)] == ["rx_a"]
    assert [row["id"] for row in store.read_partition("nb-b", 50)] == ["rx_b"]
    assert store.read_partition("nb-never-written", 50) == []


def test_the_signal_is_scoped_to_the_two_partitions_a_run_reads(store, clock):
    """一次提问只读「本库 + 全局」两个分区,签名就只能看见这两块。

    这是 PR-3(注入默认开)必须做的那一条:默认开之后每条 reasoning 提问都付
    一次 ``version_signal``,若它仍是全表 COUNT/MAX,一个进程里任何一个库蒸出
    一条就会让**所有**库的注入缓存作废,而且那次聚合还要扫过所有库的分区。
    """
    _seed_in(store, clock, "rx_g", "", at="2026-08-01T00:00:00+00:00")
    _seed_in(store, clock, "rx_a", "nb-a", at="2026-08-02T00:00:00+00:00")

    own, shared = store.version_signal("nb-a")
    assert own[1:] == (1, "2026-08-02T00:00:00+00:00")
    assert shared[1:] == (1, "2026-08-01T00:00:00+00:00")

    # 从没写过的库:自己那半是空的,全局那半照常。
    empty_own, still_shared = store.version_signal("nb-never-written")
    assert empty_own[1:] == (0, "")
    assert still_shared == shared

    # ``""`` 就是全局分区本身,两半是同一个签名。
    assert store.version_signal("") == (shared, shared)

    # 别的库蒸馏:nb-a 与全局两半都不动——这正是缓存不再被连累的判据。
    before_a, before_shared = store.version_signal("nb-a")
    _seed_in(store, clock, "rx_b", "nb-b", at="2026-08-03T00:00:00+00:00")
    assert store.version_signal("nb-a") == (before_a, before_shared)

    # 本库写只动本库那半;全局写只动全局那半。
    _seed_in(store, clock, "rx_a2", "nb-a", at="2026-08-04T00:00:00+00:00")
    after_a, after_shared = store.version_signal("nb-a")
    assert after_a != before_a and after_shared == before_shared
    _seed_in(store, clock, "rx_g2", "", at="2026-08-05T00:00:00+00:00")
    final_a, final_shared = store.version_signal("nb-a")
    assert final_a == after_a and final_shared != after_shared


def test_a_row_reports_the_partition_it_was_written_into(store, clock):
    _seed_in(store, clock, "rx_g", "", at="2026-08-01T00:00:00+00:00")
    _seed_in(store, clock, "rx_a", "nb-a", at="2026-08-01T00:00:00+00:00")
    assert store.read_experience("rx_g")["notebook_id"] == ""
    assert store.read_experience("rx_a")["notebook_id"] == "nb-a"


def test_a_merge_into_an_existing_entry_leaves_its_partition_alone(store, clock):
    """The UPDATE branch must not touch ``notebook_id``.

    The id already encodes the partition, so there is nothing for the column
    to become: a merge reached by that id is by construction in that
    partition. The conclusion and the counters move; the partition does not.
    (A caller that passes a DIFFERENT partition is not exercising this path at
    all — it is refused; see the mismatch test below.)
    """
    _seed_in(store, clock, "rx_a", "nb-a", at="2026-08-01T00:00:00+00:00")
    store.upsert_experience(
        "rx_a",
        situation=SITUATION,
        action="ppr",
        polarity="bad",
        rationale="改写结论",
        provenance=["rx_a-run-2"],
        provenance_max=50,
        replace_conclusion=True,
        notebook_id="nb-a",
    )
    row = store.read_experience("rx_a")
    assert row["notebook_id"] == "nb-a"
    assert row["polarity"] == "bad"
    assert [entry["id"] for entry in store.read_partition("nb-a", 50)] == ["rx_a"]


def test_eviction_stays_inside_the_partition_it_was_asked_about(store, clock):
    """The failure this pins is the expensive one: a busy notebook trimming
    somebody else's entries.

    ``nb-a`` is three rows over a cap of one. The global partition's two rows
    are OLDER and equally unadopted, so a whole-table eviction — or one with
    the predicate on the count but not on the inner select — would delete them
    first and leave ``nb-a`` untouched, reporting a perfectly plausible count.
    """
    _seed_in(store, clock, "rx_g1", "", at="2026-07-01T00:00:00+00:00")
    _seed_in(store, clock, "rx_g2", "", at="2026-07-02T00:00:00+00:00")
    for index, name in enumerate(("rx_a1", "rx_a2", "rx_a3", "rx_a4")):
        _seed_in(store, clock, name, "nb-a", at=f"2026-08-0{index + 1}T00:00:00+00:00")

    assert store.evict_to_limit(1, "nb-a") == 3
    assert [row["id"] for row in store.read_partition("nb-a", 50)] == ["rx_a4"]
    assert [row["id"] for row in store.read_partition("", 50)] == ["rx_g1", "rx_g2"]


def test_count_answers_one_partition_or_the_whole_table(store, clock):
    _seed_in(store, clock, "rx_g", "", at="2026-08-01T00:00:00+00:00")
    _seed_in(store, clock, "rx_a", "nb-a", at="2026-08-01T00:00:00+00:00")
    _seed_in(store, clock, "rx_b", "nb-b", at="2026-08-01T00:00:00+00:00")

    assert store.count() == 3            # no argument = the whole table
    assert store.count(None) == 3
    assert store.count("") == 1          # "" is a partition, not "everything"
    assert store.count("nb-a") == 1
    assert store.count("nb-missing") == 0


def test_the_global_partitions_id_is_byte_identical_to_the_pre_v79_one():
    """The compatibility guarantee behind "v79 recomputes nothing", pinned as
    a LITERAL rather than as a comparison against the code that produces it.

    ``rx_dfd8a477f74a873ffe793e595d6af03b`` was computed by the pre-v79
    implementation for this exact (situation, action) — before ``partition``
    existed as a parameter at all. A test that re-derived the expectation from
    today's ``experience_id`` would pass no matter how the payload changed;
    this one fails the moment the global branch stops hashing what v54's rows
    were filed under, which is the moment every already-deployed database and
    every cross-deployment ``merge_dbs`` union quietly starts duplicating
    entries instead of collapsing them.
    """
    from app.services.retrieval_experience_projection import experience_id

    assert experience_id(SITUATION, "ppr") == "rx_dfd8a477f74a873ffe793e595d6af03b"
    assert experience_id(SITUATION, "ppr", "") == (
        "rx_dfd8a477f74a873ffe793e595d6af03b"
    )


def test_each_partition_files_the_same_conclusion_under_its_own_id():
    """Same situation, same action, three partitions, three ids — and the
    non-global ones are pinned as literals too, because changing the payload's
    shape silently re-keys every notebook's entries into rows nothing will ever
    read again.
    """
    from app.services.retrieval_experience_projection import experience_id

    ids = {
        partition: experience_id(SITUATION, "ppr", partition)
        for partition in ("", "nb-a", "nb-b")
    }
    assert len(set(ids.values())) == 3
    assert ids["nb-a"] == "rx_58aa1795e12131b8167acabbefb9787d"
    assert ids["nb-b"] == "rx_4d2dd051cc05566f2920344be94c8ea3"


def test_two_partitions_hold_the_same_conclusion_as_two_independent_rows(
    store, clock
):
    """The store-level consequence of the id branch above: the same
    (situation, action) written into two partitions is two rows with
    independent counters, not one row whose evidence is pooled.
    """
    from app.services.retrieval_experience_projection import experience_id

    for partition, support in (("nb-a", 1), ("nb-b", 3)):
        store.upsert_experience(
            experience_id(SITUATION, "ppr", partition),
            situation=SITUATION,
            action="ppr",
            polarity="good",
            rationale="x",
            provenance=[f"{partition}-run-{index}" for index in range(support)],
            provenance_max=50,
            replace_conclusion=False,
            notebook_id=partition,
        )

    a = store.read_partition("nb-a", 50)
    b = store.read_partition("nb-b", 50)
    assert [row["support"] for row in a] == [1]
    assert [row["support"] for row in b] == [3]
    assert a[0]["id"] != b[0]["id"]
    assert store.count() == 2


# ------------------------------------- v79 review follow-ups: argument shapes


def test_writing_a_global_id_into_a_notebook_partition_is_refused(store, clock):
    """P2-1: the one cross-partition mistake the store CAN see, so it must.

    The id says "global", the argument says "nb-a". The two can only disagree
    if the caller derived them from different values — and the merge branch is
    reading the stored row anyway, so catching it costs nothing. Writing
    instead would fold one library's evidence into the shared entry with every
    counter still adding up, which is the shape nothing downstream could ever
    detect.
    """
    from app.services.retrieval_experience_projection import experience_id

    global_id = experience_id(SITUATION, "ppr")
    _seed_in(store, clock, global_id, "", at="2026-08-01T00:00:00+00:00")
    before = store.read_experience(global_id)

    with pytest.raises(ValueError, match="partition mismatch"):
        store.upsert_experience(
            global_id,
            situation=SITUATION,
            action="ppr",
            polarity="bad",
            rationale="来自另一个分区的结论",
            provenance=["nb-a-run"],
            provenance_max=50,
            replace_conclusion=True,
            notebook_id="nb-a",
        )

    # "refused" has to mean the table did not move — not merely that the call
    # returned an error after writing.
    assert store.read_experience(global_id) == before
    assert store.count() == 1
    assert store.read_partition("nb-a", 50) == []


def test_the_mismatch_error_never_names_the_entry(store, clock):
    """The message is a reported surface; a content-addressed id is the hash
    of a situation fingerprint and has no business in one."""
    from app.services.retrieval_experience_projection import experience_id

    entry_id = experience_id(SITUATION, "ppr", "nb-a")
    _seed_in(store, clock, entry_id, "nb-a", at="2026-08-01T00:00:00+00:00")
    with pytest.raises(ValueError) as caught:
        store.upsert_experience(
            entry_id,
            situation=SITUATION,
            action="ppr",
            polarity="good",
            rationale="x",
            provenance=["run"],
            provenance_max=50,
            replace_conclusion=False,
            notebook_id="nb-b",
        )
    assert entry_id not in str(caught.value)
    assert "nb-a" not in str(caught.value) and "nb-b" not in str(caught.value)


@pytest.mark.parametrize("partition", [None, 0, b"", ["nb-a"]])
def test_a_non_string_partition_is_refused_rather_than_coerced(store, partition):
    """P2-2: ``None`` must not read as the global partition.

    ``str(x or "")`` would turn a lost notebook id into ``""`` — a REAL
    partition, the shared one. Reading it would serve the wrong library's
    advice; evicting it would delete from the shared library. Both are
    silent, so the argument shape is refused at the door instead.
    """
    with pytest.raises(TypeError):
        store.read_partition(partition, 50)
    with pytest.raises(TypeError):
        store.evict_to_limit(1, partition)


def test_count_alone_still_accepts_none_as_the_whole_table(store, clock):
    """The registered asymmetry: ``None`` is a legal argument to ``count`` and
    only to ``count``. It is the absent-argument case there, and the whole
    table is a real question; for the other two there is no such thing as
    acting on every partition at once."""
    _seed_in(store, clock, "rx_g", "", at="2026-08-01T00:00:00+00:00")
    _seed_in(store, clock, "rx_a", "nb-a", at="2026-08-01T00:00:00+00:00")
    assert store.count(None) == 2
    assert store.count() == 2
    assert store.count("") == 1


def test_a_partitioned_read_seeks_one_partition_instead_of_walking_the_table(
    store, clock
):
    """P3-2: the v79 index is ``(notebook_id, id)``, and this is what the
    second column buys.

    ``WHERE notebook_id = ? ORDER BY id`` is every read of this table, and the
    trap is that it looks fine either way: ``id`` is the PRIMARY KEY, so its
    autoindex already supplies the order, and with a ``notebook_id``-only
    index SQLite happily answers this by walking the WHOLE table through that
    autoindex and applying the partition as a filter — no sort, no complaint,
    and no benefit from partitioning on the read side at all (measured: the
    plan is ``SCAN ... USING INDEX sqlite_autoindex_...``). The trailing
    column is what turns it into ``SEARCH ... USING COVERING INDEX
    (notebook_id=?)``.

    Asserted against the planner rather than against the index DDL, because
    the DDL is exactly the thing a future edit would change while believing
    the read is unaffected.
    """
    for index in range(40):
        _seed_in(
            store, clock, f"rx_a{index:04d}", "nb-a",
            at="2026-08-01T00:00:00+00:00",
        )
    with store.database.connect() as connection:
        plan = " ".join(
            str(row["detail"])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM retrieval_experiences "
                "WHERE notebook_id=? ORDER BY id LIMIT ?",
                ("nb-a", 50),
            ).fetchall()
        )
    assert "idx_retrieval_experiences_notebook" in plan, plan
    assert "SEARCH" in plan.upper(), plan
    assert "TEMP B-TREE" not in plan.upper(), plan


# ============================================================================
# 取数侧:``AskStateStore.recent_completed_ask_runs`` 的**可选**分区谓词
# ============================================================================
#
# 它不是这张表的方法,但它是这条链路的另一半取数面,而且两侧 store 各写一遍
# SQL——所以它的行为断言跟着 T2 落在这里,而不是散在某条服务层用例的替身里。
# PostgreSQL 的同款断言在 ``tests/postgres/test_content_store_conformance.py``。

_ASK_NOW = "2026-09-22T00:00:00+00:00"


def _ask_state(tmp_path, store):
    """复用 store fixture 已经迁移好的那个库,再挂一个 AskStateStore 上去。"""
    from types import SimpleNamespace

    from app.repositories.sqlite.ask_state_store import AskStateStore

    return AskStateStore(
        store.database,
        SimpleNamespace(now=lambda: _ASK_NOW, new_id=lambda prefix: f"{prefix}-1"),
    )


def _seed_tenants(store, notebook_ids):
    """``ask_jobs`` 有外键,所以这两张表得先有行——而 ``retrieval_experiences``
    两头都没有外键,这正是它的分区必须靠显式登记删除、而不是靠级联的原因。"""
    with store.database.write() as db:
        db.execute(
            "INSERT OR IGNORE INTO users(id,email,display_name,role,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            ("user-x", "x@example.test", "X", "user", "active", _ASK_NOW, _ASK_NOW),
        )
        for notebook_id in notebook_ids:
            db.execute(
                "INSERT OR IGNORE INTO notebooks(id,name,purpose,primary_domain,"
                "status,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (notebook_id, notebook_id, "", "engineering", "ready", "user-x",
                 _ASK_NOW, _ASK_NOW),
            )


def _seed_ask(store, job_id, *, notebook_id, mode="reasoning", status="done"):
    """一条已完成的提问 + 两条轨迹步,直接写 SQL。

    绕开 ``begin_durable_job`` 是刻意的:这里要钉的是**读**的谓词,走写侧会把
    会话生命周期拖进一条关于取数的用例里。
    """
    import json

    _seed_tenants(store, [notebook_id])
    with store.database.write() as db:
        db.execute(
            "INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,"
            "question,status,trace_json,answer_id,error,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,'','','',?,?)",
            (job_id, notebook_id, f"conv-{job_id}", "user-x", mode,
             f"{notebook_id} 里的问题", status, _ASK_NOW, _ASK_NOW),
        )
        steps = (
            {"step_type": "intent", "summary": "已开始检索",
             "detail": {"resolved_question": "原问题", "result_scope": "ranked",
                        "completeness_required": False,
                        "retrieval_effort": "standard", "entities": ["a"],
                        "constraints": [], "excluded_topics": [],
                        "mandatory_topics": []}},
            {"step_type": "ppr", "summary": "扩展", "detail": {"count": 0}},
        )
        for seq, step in enumerate(steps):
            db.execute(
                "INSERT INTO ask_trace_steps(job_id,seq,step_json,created_at) "
                "VALUES (?,?,?,?)",
                (job_id, seq, json.dumps(step, ensure_ascii=False), _ASK_NOW),
            )


def test_the_run_sample_confines_itself_to_one_notebook_when_asked(tmp_path, store):
    """单库链路的取数:``AND notebook_id = ?`` 在 SQL 里,不是 Python 侧过滤。

    三条断言合起来才是判据:不传谓词时两个库都在(全局链路一个字没变)、传了
    只剩那个库、传一个没有 run 的库得到空列表(而不是悄悄退回全表)。
    """
    ask_state = _ask_state(tmp_path, store)
    _seed_ask(store, "job-a", notebook_id="nb-a")
    _seed_ask(store, "job-b", notebook_id="nb-b")

    everything = ask_state.recent_completed_ask_runs(job_limit=40, step_limit=600)
    assert {row["run_id"] for row in everything} == {"job-a", "job-b"}

    only_a = ask_state.recent_completed_ask_runs(
        job_limit=40, step_limit=600, notebook_id="nb-a"
    )
    assert [row["run_id"] for row in only_a] == ["job-a"]
    assert len(only_a[0]["steps"]) == 2

    assert ask_state.recent_completed_ask_runs(
        job_limit=40, step_limit=600, notebook_id="nb-nobody"
    ) == []


def test_the_partitioned_sample_projects_exactly_what_the_global_one_does(
    tmp_path, store
):
    """谓词决定**哪些 run 被数**,绝不改变一条 run 留下什么。

    这是分区化对隐私保证的全部承诺:投影面一个字节都没动,所以模型看到的东西
    与分区落地前同形——没有问题原文、没有 ``created_by``、没有 ``notebook_id``。
    """
    ask_state = _ask_state(tmp_path, store)
    _seed_ask(store, "job-a", notebook_id="nb-a")

    partitioned = ask_state.recent_completed_ask_runs(
        job_limit=40, step_limit=600, notebook_id="nb-a"
    )
    unpartitioned = ask_state.recent_completed_ask_runs(job_limit=40, step_limit=600)

    assert partitioned == unpartitioned
    assert set(partitioned[0]) == {"run_id", "mode", "steps"}
    rendered = repr(partitioned)
    assert "nb-a" not in rendered
    assert "原问题" not in rendered and "里的问题" not in rendered
    assert "user-x" not in rendered


def test_the_partitioned_sample_still_refuses_unfinished_and_non_reasoning_runs(
    tmp_path, store
):
    """既有的两道闸在谓词之后仍然成立——分区不是一条把它们绕开的新入口。"""
    ask_state = _ask_state(tmp_path, store)
    _seed_ask(store, "job-done", notebook_id="nb-a")
    _seed_ask(store, "job-failed", notebook_id="nb-a", status="failed")
    _seed_ask(store, "job-chunk", notebook_id="nb-a", mode="chunk")

    rows = ask_state.recent_completed_ask_runs(
        job_limit=40, step_limit=600, notebook_id="nb-a"
    )
    assert [row["run_id"] for row in rows] == ["job-done"]

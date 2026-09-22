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

    assert PostgresMigrator(database).migrate() == 63
    # Nothing is seeded: this table has no foreign key in either direction, so
    # a partition id here is just a string nobody has to have created. Since
    # 0059 that column exists (a PARTITION key, not a tenancy column and still
    # no owner), which is why deep copy now EXCLUDES the table deliberately
    # rather than structurally, while the global-union classification in
    # scripts/merge_dbs.py is unchanged.
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
    # newest-first, matching the only real caller's ``created_at DESC`` order
    # — the store reverses it, so it comes back oldest-first.
    row = _upsert(harness, "rx_one", provenance=["run-2", "run-1"])
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
    # newest-first per batch, matching the real caller.
    _upsert(harness, "rx_one", provenance=["run-2", "run-1"])
    row = _upsert(harness, "rx_one", provenance=["run-3", "run-2"])
    assert row["support"] == 3
    assert row["provenance"] == ["run-1", "run-2", "run-3"]


def test_overlapping_batches_evict_the_genuinely_oldest_run_first(
    retrieval_experience_harness,
):
    """The R1/R2/R3 reproduction shape, proved on this backend too — see the
    SQLite mirror's identically named test for the full explanation of why
    ``incoming`` has to be reversed before it touches any trailing-slice
    logic. ``provenance_max`` is shrunk to 3 so eviction actually has to
    choose, and the property pinned is: after a sequence of overlapping
    batches, ``support`` equals the number of DISTINCT run ids actually
    observed, and the survivors are the newest 3 — never a row-order
    artifact of which batch happened to come first in the merge.
    """
    harness = retrieval_experience_harness
    row = _upsert(
        harness, "rx_one", provenance=["r3", "r2", "r1"], provenance_max=3,
    )
    assert row["support"] == 3
    assert row["provenance"] == ["r1", "r2", "r3"]

    row = _upsert(
        harness, "rx_one", provenance=["r4", "r3", "r2"], provenance_max=3,
    )
    assert row["support"] == 4
    assert row["provenance"] == ["r2", "r3", "r4"]

    row = _upsert(
        harness, "rx_one", provenance=["r5", "r4", "r3"], provenance_max=3,
    )
    assert row["support"] == 5
    assert row["provenance"] == ["r3", "r4", "r5"]


def test_a_genuinely_concurrent_insert_falls_back_to_merge(
    retrieval_experience_harness, monkeypatch,
):
    """The fresh-row race that ``cursor.rowcount`` exists to detect (codex
    R2): two runs distilling the SAME new entry at once. Neither's
    ``SELECT ... FOR UPDATE`` sees a row — PostgreSQL does not lock rows that
    do not exist yet — so both reach the INSERT branch, and exactly one wins
    ``ON CONFLICT (id) DO NOTHING``. Before the fix, the branch assumed it had
    always inserted and never re-read the winner's row, so the loser's own
    provenance and rationale were silently discarded instead of merged in.

    The interleaving is reproduced deterministically — not raced against wall
    clock time — by pausing inside ``_canonical_situation``, the last thing
    the INSERT branch evaluates before issuing the INSERT itself. Only the
    FIRST call to it (the "loser", started first) pauses; the winner's own
    call to it passes straight through, so its INSERT commits first.
    """
    import app.repositories.postgres.retrieval_experience_store as module

    harness = retrieval_experience_harness
    entry_id = experience_id(SITUATION, "exact_lookup")
    entered = threading.Event()
    release = threading.Event()
    paused_once = threading.Event()
    real_canonical = module._canonical_situation

    def _paused_canonical(situation):
        if not paused_once.is_set():
            paused_once.set()
            entered.set()
            assert release.wait(timeout=5), "winner never released the loser"
        return real_canonical(situation)

    monkeypatch.setattr(module, "_canonical_situation", _paused_canonical)

    results: dict[str, dict] = {}

    def _loser() -> None:
        results["loser"] = harness.store.upsert_experience(
            entry_id, situation=SITUATION, action="exact_lookup",
            polarity="bad", rationale="慢的那个", provenance=["run-a"],
            provenance_max=10, replace_conclusion=False,
        )

    thread = threading.Thread(target=_loser)
    thread.start()
    assert entered.wait(timeout=5), "loser never reached the INSERT branch"

    winner = harness.store.upsert_experience(
        entry_id, situation=SITUATION, action="exact_lookup",
        polarity="bad", rationale="快的那个", provenance=["run-b"],
        provenance_max=10, replace_conclusion=False,
    )
    # The winner's write() has committed by the time upsert_experience
    # returns, so releasing the loser now genuinely reproduces "the row
    # already exists when my INSERT fires" rather than a lucky ordering.
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive(), "loser thread never finished"

    assert winner["support"] == 1
    assert winner["provenance"] == ["run-b"]

    loser_row = results["loser"]
    # The loser's own run id must not be lost: it merged into the winner's
    # row rather than silently discarding its own contribution.
    assert loser_row["support"] == 2
    assert set(loser_row["provenance"]) == {"run-a", "run-b"}

    stored = harness.store.read_experience(entry_id)
    assert stored["support"] == 2
    assert set(stored["provenance"]) == {"run-a", "run-b"}


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
    surviving = {row["id"] for row in harness.store.read_partition("", 50)}
    # rx_ccc survives on ``adopted``; between the two tied entries the id
    # tie-break keeps the LATER one (descending order names survivors), which
    # is the same row SQLite's ascending LIMIT would have spared.
    assert surviving == {"rx_ccc", "rx_bbb"}


def test_read_partition_orders_by_the_c_collation(retrieval_experience_harness):
    harness = retrieval_experience_harness
    for entry_id in ("rx_b", "rx_A", "rx_a"):
        _upsert(harness, entry_id, provenance=[f"{entry_id}-run"])
    # ``COLLATE "C"`` is byte order, so uppercase sorts before lowercase —
    # matching SQLite's default binary collation rather than a locale's.
    assert [row["id"] for row in harness.store.read_partition("", 50)] == [
        "rx_A", "rx_a", "rx_b",
    ]


def test_note_adopted_refuses_a_negative_delta(retrieval_experience_harness):
    harness = retrieval_experience_harness
    _upsert(harness, "rx_one")
    with pytest.raises(ValueError):
        harness.store.note_adopted(["rx_one"], -1)


def _signal(harness, partition: str = ""):
    """This partition's own half of ``version_signal``. The method returns
    ``(this partition, the global partition)`` so one aggregate serves a run
    that reads both; ``partition=""`` makes the two halves identical."""
    return harness.store.version_signal(partition)[0]


def test_the_version_signal_tracks_inserts_updates_and_evictions(
    retrieval_experience_harness,
):
    """The injection side's memo key, proved on this backend.

    ``updated_at`` is a ``timestamptz`` here and TEXT on SQLite, which is why
    the method renders it to text: the key is only ever compared for equality
    against the previously observed one, and one shape beats one per driver.

    All three transitions matter and each catches something the other half
    misses — an insert moves both, an in-place UPDATE moves only the timestamp
    (the id is content-addressed, so the count stays put), and an eviction
    moves only the count (deleting the oldest rows leaves the newest timestamp
    alone).
    """
    harness = retrieval_experience_harness
    assert harness.store.version_signal("") == ((0, 0, ""), (0, 0, ""))

    _upsert(harness, "rx_one", provenance=["run-1"])
    inserted = _signal(harness)
    assert inserted[1] == 1 and inserted[2] != ""
    assert inserted[0] > 0          # codex #524 R12:进程内修订元

    # 拨钟:in-place UPDATE 只动时间戳那一半,不拨钟的话 (count, max_ts) 两半
    # 都不变——SQLite 侧同名用例就是这么写的,这里镜像它。
    harness.clock.value = "2026-08-20T00:00:00+00:00"
    _upsert(
        harness, "rx_one", provenance=["run-2"], replace_conclusion=True,
        rationale="换了一个结论",
    )
    updated = _signal(harness)
    assert updated[1] == 1 and updated[1:] != inserted[1:]
    assert updated[0] > inserted[0]

    _upsert(harness, "rx_two", provenance=["run-3"])
    before_evict = _signal(harness)
    assert harness.store.evict_to_limit(1) == 1
    after_evict = _signal(harness)
    assert after_evict[1] == 1
    assert after_evict[0] > before_evict[0]


def test_an_adoption_is_deliberately_invisible_to_the_version_signal(
    retrieval_experience_harness,
):
    """``note_adopted`` must not move ``updated_at`` — that column is the last
    tie-break of the eviction ordering, and letting an adoption refresh it
    would make a frequently-injected entry immortal. The memo consequently
    misses adoptions, which is correct: ``adopted`` is neither rendered into
    the prompt block nor part of the injection-side selection ordering, so a
    memo that misses it still serves identical rows.

    The clock is advanced BEFORE ``note_adopted`` — without that, this
    assertion would hold even if ``note_adopted`` wrote ``updated_at``,
    because ``now()`` at insert time and at adoption time would be identical
    and the signal would coincidentally match either way. Advancing the clock
    is what makes "the signal did not move" prove the invariant rather than
    prove nothing.
    """
    harness = retrieval_experience_harness
    _upsert(harness, "rx_one", provenance=["run-1"])
    before = _signal(harness)
    harness.clock.value = LATER
    harness.store.note_adopted(["rx_one"])
    assert _signal(harness) == before


# ----------------------------------------------------- PostgreSQL 0059


def test_the_partition_predicate_confines_both_reads_and_evictions(
    retrieval_experience_harness,
):
    """0059's partition column, on the backend whose eviction is the
    OFFSET-shaped one.

    Worth its own case rather than trusting the SQLite mirror: here the
    ``notebook_id`` predicate has to sit on the INNER select, because the
    ``OFFSET`` is what names the survivors. Put it on the outer ``DELETE``
    instead and the offset still counts the whole table, so the global
    partition's rows below would be deleted while ``nb-a`` stayed over its cap
    — and the returned row count would look entirely reasonable.
    """
    harness = retrieval_experience_harness
    for entry_id in ("rx_g1", "rx_g2"):
        _upsert(harness, entry_id, provenance=[f"{entry_id}-run"], notebook_id="")
    for entry_id in ("rx_a1", "rx_a2", "rx_a3"):
        _upsert(harness, entry_id, provenance=[f"{entry_id}-run"], notebook_id="nb-a")

    assert [row["id"] for row in harness.store.read_partition("nb-a", 50)] == [
        "rx_a1", "rx_a2", "rx_a3",
    ]
    assert [row["id"] for row in harness.store.read_partition("", 50)] == [
        "rx_g1", "rx_g2",
    ]
    assert harness.store.read_experience("rx_a1")["notebook_id"] == "nb-a"
    assert harness.store.read_experience("rx_g1")["notebook_id"] == ""

    assert harness.store.evict_to_limit(1, "nb-a") == 2
    assert [row["id"] for row in harness.store.read_partition("nb-a", 50)] == ["rx_a3"]
    assert [row["id"] for row in harness.store.read_partition("", 50)] == [
        "rx_g1", "rx_g2",
    ]
    assert harness.store.count() == 3
    assert harness.store.count("") == 2
    assert harness.store.count("nb-a") == 1


def test_the_signal_is_scoped_to_the_two_partitions_a_run_reads(
    retrieval_experience_harness,
):
    """PR-3(注入默认开)要求的分区签名,在 PG 侧证一遍。

    值得单开一条而不是信 SQLite 镜像:这里的 ``MAX(updated_at)`` 是
    ``timestamptz::text``,``GROUP BY notebook_id`` 之后每组各渲染各的,漏掉
    分组或把谓词写丢都会让两半互相串味。
    """
    harness = retrieval_experience_harness
    _upsert(harness, "rx_g1", provenance=["rx_g1-run"], notebook_id="")
    harness.clock.value = LATER
    _upsert(harness, "rx_a1", provenance=["rx_a1-run"], notebook_id="nb-a")

    own, shared = harness.store.version_signal("nb-a")
    assert own[1] == 1 and shared[1] == 1
    assert own[2] != shared[2], "两块各渲染各的 MAX(updated_at)"

    empty_own, still_shared = harness.store.version_signal("nb-never-written")
    assert empty_own[1:] == (0, "")
    assert still_shared == shared
    assert harness.store.version_signal("") == (shared, shared)

    # 别的库蒸馏不动本库、也不动全局。
    before = harness.store.version_signal("nb-a")
    _upsert(harness, "rx_b1", provenance=["rx_b1-run"], notebook_id="nb-b")
    assert harness.store.version_signal("nb-a") == before


def test_writing_a_global_id_into_a_notebook_partition_is_refused(
    retrieval_experience_harness,
):
    """P2-1 on the backend whose merge branch has TWO reads.

    Worth its own case rather than trusting the SQLite mirror: here the refusal
    has to survive a real transaction rollback (SQLite rolls back through the
    connection context manager; this rolls back through the pool's), and the
    check sits after an ``ON CONFLICT DO NOTHING`` insert attempt that may or
    may not have fired. "Refused" therefore has to mean the table is unchanged,
    not merely that an exception came back.
    """
    harness = retrieval_experience_harness
    global_id = experience_id(SITUATION, "exact_lookup")
    _upsert(harness, global_id, provenance=["global-run"], notebook_id="")
    before = harness.store.read_experience(global_id)

    with pytest.raises(ValueError, match="partition mismatch"):
        _upsert(
            harness,
            global_id,
            provenance=["nb-a-run"],
            replace_conclusion=True,
            rationale="来自另一个分区的结论",
            notebook_id="nb-a",
        )

    assert harness.store.read_experience(global_id) == before
    assert harness.store.count() == 1
    assert harness.store.read_partition("nb-a", 50) == []


@pytest.mark.parametrize("partition", [None, 0, b"", ["nb-a"]])
def test_a_non_string_partition_is_refused_rather_than_coerced(
    retrieval_experience_harness, partition
):
    """P2-2 mirror. The two backends must refuse the same argument shapes, or
    a caller that works against SQLite silently evicts from the shared
    partition against PostgreSQL."""
    harness = retrieval_experience_harness
    with pytest.raises(TypeError):
        harness.store.read_partition(partition, 50)
    with pytest.raises(TypeError):
        harness.store.evict_to_limit(1, partition)
    assert harness.store.count(None) == 0


def test_the_partition_index_carries_both_columns_in_order(
    retrieval_experience_harness,
):
    """P3-2 on PostgreSQL: ``(notebook_id, id)``, in that order, non-unique.

    Deliberately an assertion about the INDEX, not about a PLAN. The SQLite
    mirror pins the plan because SQLite's planner is deterministic enough for
    that to be a guarantee; here the planner weighs this index against the
    id-ordered primary key using statistics, and a measurement on this very
    database shows it switching to the primary key as the table grows. An
    "index name appears in EXPLAIN" assertion would therefore be a flake
    dressed as a guarantee — so this pins the thing the migration actually
    controls, and the leading/trailing order that makes the index usable for
    "seek one partition, already in id order" at all.

    Read live from ``pg_indexes`` rather than from the migration text: the
    migration file is what a future edit changes, so asserting against it
    would assert the edit against itself.
    """
    harness = retrieval_experience_harness
    with harness.database.connect() as db:
        definition = db.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename='retrieval_experiences' AND indexname=%s",
            ("idx_retrieval_experiences_notebook",),
        ).fetchone()
    assert definition is not None, "0059's partition index is missing"
    indexdef = definition["indexdef"]
    assert "UNIQUE" not in indexdef.upper(), indexdef
    columns = indexdef[indexdef.index("(") + 1: indexdef.rindex(")")]
    assert [part.strip() for part in columns.split(",")] == [
        "notebook_id", "id",
    ], indexdef

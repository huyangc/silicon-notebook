"""PostgreSQL conformance for ``AgentProfileStorePort`` (Agentic Memory P1, T2).

Scope is deliberately narrow, mirroring ``test_catalog_store_conformance.py``'s
own rationale: the SQLite side already has full behavioural coverage in
``tests/test_agent_profile_store.py`` (identical fixtures, identical
assertions — this store is a byte-for-byte behavioural mirror by design).
This file only proves the things that are genuinely backend-specific:

- ``jsonb`` round-trips ``evidence``/``history`` the same way JSON text does
  on SQLite;
- ``started_at``/``finished_at`` are nullable ``timestamptz`` here (vs
  SQLite's ``TEXT DEFAULT ''``) and must still read back as ``""`` before the
  chain has ever run, per the store module's own documented convention;
- the CAS on ``write_block``/``clear_block`` is real ``FOR UPDATE`` row
  locking (or, for the "row doesn't exist yet" branch, a caught
  ``UniqueViolation``) instead of SQLite's process-wide write mutex;
- ``read_blocks``' ordering is ``COLLATE "C"``, matching SQLite's default
  binary collation even on a non-C-collated database.

One exception to "SQLite already covers the behaviour": the history ring's
DIRECTION is asserted here too. It used to be a private per-backend copy of
the same trimming code, and a copy that kept the OLDEST twenty entries passed
every conformance case in this file — the row still came back with twenty
well-formed entries. The shared ``append_profile_history`` in ``ports.py`` now
makes that divergence unrepresentable, and
``test_write_block_history_ring_keeps_the_newest_entries`` below is the
regression that would catch a re-introduced local copy on this backend.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from app.repositories.postgres.agent_profile_store import AgentProfileStore
from app.repositories.ports import (
    AGENT_PROFILE_RESTART_FAILURE_MESSAGE,
    AGENT_PROFILE_SETTLE_GONE,
    AGENT_PROFILE_SETTLE_SUPERSEDED,
    AGENT_PROFILE_SETTLED,
    AgentProfileClaimSuperseded,
    AgentProfileRevisionConflict,
)
from app.services.repository_runtime import RepositoryCompatibilitySeams

NOW = "2026-08-18T00:00:00+00:00"
NOTEBOOK_ID = "nb-agent-profile"

pytestmark = pytest.mark.postgres_integration


def _seams() -> RepositoryCompatibilitySeams:
    lock = threading.Lock()
    counter: dict[str, int] = {}

    def new_id(prefix: str) -> str:
        with lock:
            counter[prefix] = counter.get(prefix, 0) + 1
            return f"{prefix}-agentprofile-{counter[prefix]:04d}"

    return RepositoryCompatibilitySeams(
        new_id=new_id,
        now=lambda: NOW,
        copy_chunk_size=lambda: 100,
        remap_json_ids=lambda value, _mapping: value,
        in_chunk_size=lambda: 100,
    )


def _seed(database, *, notebook_id: str) -> None:
    mark = "%s"
    with database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            f"VALUES ({','.join([mark] * 11)})",
            (
                "user-owner", "owner@example.test", "Owner", "admin", "active",
                NOW, NOW, "u00654321", "", "", 0,
            ),
        )
        connection.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) "
            f"VALUES ({','.join([mark] * 9)})",
            (
                notebook_id, "NB", "", "engineering", "ready", "user-owner",
                NOW, NOW, "personal",
            ),
        )


@dataclass
class AgentProfileHarness:
    database: object
    store: AgentProfileStore
    notebook_id: str


@pytest.fixture
def agent_profile_harness(request) -> AgentProfileHarness:
    seams = _seams()
    database = request.getfixturevalue("postgres_database")
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(database).migrate() == 49
    _seed(database, notebook_id=NOTEBOOK_ID)
    yield AgentProfileHarness(
        database=database,
        store=AgentProfileStore(database, new_id=seams.new_id, now=seams.now),
        notebook_id=NOTEBOOK_ID,
    )


def test_write_block_round_trips_jsonb_evidence_and_history(agent_profile_harness):
    harness = agent_profile_harness
    block = harness.store.write_block(
        harness.notebook_id, "", "corpus_shape",
        value="这本库主要是模拟电路论文",
        evidence=[{"claim_index": 0, "source_ids": ["src-1", "src-2"]}],
        expected_revision=0, origin="job", actor="",
    )
    assert block["revision"] == 1
    assert block["evidence"] == [{"claim_index": 0, "source_ids": ["src-1", "src-2"]}]
    assert block["history"] == [
        {
            "before": None,
            "after": "这本库主要是模拟电路论文",
            "origin": "job",
            "actor": "",
            "at": NOW,
            "revision": 1,
        }
    ]
    # A fresh point read decodes the same jsonb columns the same way.
    reread = harness.store.read_block(harness.notebook_id, "", "corpus_shape")
    assert reread == block


def test_write_block_expected_revision_zero_against_a_concurrent_creator_conflicts(
    agent_profile_harness,
):
    """The "no row yet" branch is an INSERT, not a SELECT ... FOR UPDATE — a
    concurrent creator racing it hits a real ``UniqueViolation`` on the
    primary key, which the store must catch and re-raise as
    ``AgentProfileRevisionConflict`` rather than let bubble as a raw
    ``psycopg`` error."""
    harness = agent_profile_harness
    harness.store.write_block(
        harness.notebook_id, "", "corpus_shape", value="v1",
        evidence=[], expected_revision=0, origin="job", actor="",
    )
    with pytest.raises(AgentProfileRevisionConflict):
        harness.store.write_block(
            harness.notebook_id, "", "corpus_shape", value="v2",
            evidence=[], expected_revision=0, origin="job", actor="",
        )
    assert harness.store.read_block(harness.notebook_id, "", "corpus_shape")["value"] == "v1"


def test_write_block_stale_revision_under_for_update_conflicts(agent_profile_harness):
    harness = agent_profile_harness
    harness.store.write_block(
        harness.notebook_id, "", "corpus_shape", value="v1",
        evidence=[], expected_revision=0, origin="job", actor="",
    )
    with pytest.raises(AgentProfileRevisionConflict):
        harness.store.write_block(
            harness.notebook_id, "", "corpus_shape", value="v2-stale",
            evidence=[], expected_revision=99, origin="job", actor="",
        )
    current = harness.store.read_block(harness.notebook_id, "", "corpus_shape")
    assert current["value"] == "v1"
    assert current["revision"] == 1


def test_write_block_history_ring_keeps_the_newest_entries(agent_profile_harness):
    """The ring is capped at 20 AND keeps the MOST RECENT 20 — dropping the
    oldest first.

    Both halves are load-bearing and only the second one is fragile: a
    ``history[:20]`` (keep the oldest) instead of ``history[-20:]`` still
    yields a 20-entry list of well-formed entries, so a length-only assertion
    passes and the panel silently shows a frozen prehistory instead of the
    recent edits. Asserting the ring's CONTENT at both ends is what makes
    that inversion visible on this backend, where the trimming used to live
    in a private copy of the SQLite function.
    """
    harness = agent_profile_harness
    block = harness.store.write_block(
        harness.notebook_id, "", "corpus_shape", value="v0",
        evidence=[], expected_revision=0, origin="job", actor="",
    )
    for i in range(1, 25):
        block = harness.store.write_block(
            harness.notebook_id, "", "corpus_shape", value=f"v{i}",
            evidence=[], expected_revision=block["revision"], origin="job", actor="",
        )
    assert block["revision"] == 25
    assert len(block["history"]) == 20
    # Newest kept, oldest dropped: 25 transitions (None->v0, v0->v1, ...,
    # v23->v24) trimmed to the last 20 starts at v4->v5.
    assert block["history"][0]["before"] == "v4"
    assert block["history"][0]["after"] == "v5"
    assert block["history"][-1]["before"] == "v23"
    assert block["history"][-1]["after"] == "v24"
    assert [entry["revision"] for entry in block["history"]] == list(range(6, 26))
    # The dropped prehistory really is gone, not merely reordered.
    assert all(entry["after"] != "v0" for entry in block["history"])
    # And it survives a fresh jsonb decode, not just the write's return value.
    assert harness.store.read_block(
        harness.notebook_id, "", "corpus_shape"
    )["history"] == block["history"]


def test_clear_block_keeps_the_row_and_history_and_a_missing_row_raises(
    agent_profile_harness,
):
    harness = agent_profile_harness
    written = harness.store.write_block(
        harness.notebook_id, "", "corpus_shape", value="v1",
        evidence=[{"claim_index": 0, "source_ids": ["src-1"]}],
        expected_revision=0, origin="job", actor="",
    )
    cleared = harness.store.clear_block(
        harness.notebook_id, "", "corpus_shape",
        expected_revision=written["revision"], actor="user-owner",
    )
    assert cleared["value"] == ""
    assert cleared["evidence"] == []
    assert cleared["revision"] == written["revision"] + 1
    assert len(cleared["history"]) == 2
    assert harness.store.read_block(harness.notebook_id, "", "corpus_shape") is not None

    with pytest.raises(KeyError):
        harness.store.clear_block(
            harness.notebook_id, "", "usage_gaps",
            expected_revision=0, actor="user-owner",
        )


def test_clear_all_deletes_only_the_requested_scope(agent_profile_harness):
    harness = agent_profile_harness
    harness.store.write_block(
        harness.notebook_id, "", "corpus_shape", value="base",
        evidence=[], expected_revision=0, origin="job", actor="",
    )
    harness.store.write_block(
        harness.notebook_id, "user-a", "retrieval_notes", value="overlay-a",
        evidence=[], expected_revision=0, origin="job", actor="",
    )
    deleted = harness.store.clear_all(harness.notebook_id, "")
    assert deleted == 1
    assert harness.store.read_block(harness.notebook_id, "", "corpus_shape") is None
    assert harness.store.read_block(harness.notebook_id, "user-a", "retrieval_notes") is not None


def test_read_blocks_owner_predicate_and_collate_c_ordering(agent_profile_harness):
    harness = agent_profile_harness
    harness.store.write_block(
        harness.notebook_id, "", "corpus_shape", value="shared base",
        evidence=[], expected_revision=0, origin="job", actor="",
    )
    harness.store.write_block(
        harness.notebook_id, "user-a", "retrieval_notes", value="A's private notes",
        evidence=[], expected_revision=0, origin="job", actor="",
    )
    harness.store.write_block(
        harness.notebook_id, "user-b", "retrieval_notes", value="B's private notes",
        evidence=[], expected_revision=0, origin="job", actor="",
    )

    a_view = harness.store.read_blocks(harness.notebook_id, "user-a")
    values = {row["value"] for row in a_view}
    assert values == {"shared base", "A's private notes"}
    assert "B's private notes" not in values
    # COLLATE "C" ORDER BY owner_id,label: the empty-string base owner sorts
    # first, ahead of any non-empty owner id.
    assert [row["owner_id"] for row in a_view] == ["", "user-a"]


def test_job_row_started_at_and_finished_at_are_the_empty_string_sentinel_before_they_happen(
    agent_profile_harness,
):
    """``started_at``/``finished_at`` are nullable ``timestamptz`` on
    PostgreSQL (vs SQLite's ``TEXT DEFAULT ''``); the store must still hand
    back ``""`` for "has not happened yet" on both backends."""
    harness = agent_profile_harness
    assert harness.store.bump_signal(harness.notebook_id, "", delta=1) == 1
    fresh = harness.store.job_row(harness.notebook_id, "")
    assert fresh["started_at"] == ""
    assert fresh["finished_at"] == ""

    first = harness.store.claim(harness.notebook_id, "")
    assert first.pending_signal == 1
    claimed = harness.store.job_row(harness.notebook_id, "")
    assert claimed["started_at"] == NOW
    assert claimed["finished_at"] == ""
    assert claimed["claim_token"] == first.token != ""

    assert harness.store.settle(
        harness.notebook_id, "", "done", blocks_written=2, consumed=1,
        claim_token=first.token,
    ) == AGENT_PROFILE_SETTLED
    settled = harness.store.job_row(harness.notebook_id, "")
    assert settled["finished_at"] == NOW
    assert settled["status"] == "done"
    assert settled["pending_signal"] == 0
    assert settled["runs"] == 1

    # ``claim``'s reset writes NULL back into these nullable timestamptz
    # columns; the sentinel normalisation must survive that round trip too.
    second = harness.store.claim(harness.notebook_id, "")
    assert second.pending_signal == 0
    reclaimed = harness.store.job_row(harness.notebook_id, "")
    assert reclaimed["finished_at"] == ""
    assert reclaimed["started_at"] == NOW
    assert reclaimed["claim_token"] == second.token != first.token


def test_claim_is_for_update_cas_and_settle_is_idempotent(agent_profile_harness):
    harness = agent_profile_harness
    harness.store.bump_signal(harness.notebook_id, "", delta=3)
    claimed = harness.store.claim(harness.notebook_id, "")
    assert claimed.pending_signal == 3
    assert harness.store.claim(harness.notebook_id, "") is None

    assert harness.store.settle(
        harness.notebook_id, "", "done", consumed=3, claim_token=claimed.token
    ) == AGENT_PROFILE_SETTLED
    # A repeated settle after the chain already landed a terminal status
    # touches nothing — and the row is still there, so the outcome is
    # "superseded" rather than "gone".
    assert harness.store.settle(
        harness.notebook_id, "", "failed", consumed=0, claim_token=claimed.token
    ) == AGENT_PROFILE_SETTLE_SUPERSEDED
    assert harness.store.job_row(harness.notebook_id, "")["status"] == "done"


def test_claim_creates_the_row_and_settle_floors_the_signal_with_greatest(
    agent_profile_harness,
):
    """``ON CONFLICT DO NOTHING`` row-ensure and ``GREATEST(0, ...)`` are this
    backend's spellings of ``INSERT OR IGNORE`` / two-argument ``MAX``; both
    are SQL-level and therefore genuinely backend-specific."""
    harness = agent_profile_harness
    assert harness.store.job_row(harness.notebook_id, "user-fresh") is None
    fresh = harness.store.claim(harness.notebook_id, "user-fresh")
    assert fresh.pending_signal == 0
    created = harness.store.job_row(harness.notebook_id, "user-fresh")
    assert created["status"] == "running"
    assert created["pending_signal"] == 0

    # Signals landing mid-run survive a settle that consumes only the
    # snapshot, and an over-large consumed value floors at zero instead of
    # going negative.
    harness.store.bump_signal(harness.notebook_id, "user-fresh", delta=12)
    harness.store.settle(
        harness.notebook_id, "user-fresh", "done", consumed=0,
        claim_token=fresh.token,
    )
    assert harness.store.job_row(
        harness.notebook_id, "user-fresh"
    )["pending_signal"] == 12

    again = harness.store.claim(harness.notebook_id, "user-fresh")
    assert again.pending_signal == 12
    harness.store.settle(
        harness.notebook_id, "user-fresh", "done", consumed=99,
        claim_token=again.token,
    )
    assert harness.store.job_row(
        harness.notebook_id, "user-fresh"
    )["pending_signal"] == 0


def test_sweep_stale_on_start_force_settles_queued_and_running_rows(
    agent_profile_harness,
):
    harness = agent_profile_harness
    harness.store.bump_signal(harness.notebook_id, "", delta=1)
    harness.store.claim(harness.notebook_id, "")
    harness.store.bump_signal(harness.notebook_id, "user-a", delta=1)  # idle, never claimed

    swept = harness.store.sweep_stale_on_start()
    assert swept == 1
    base_row = harness.store.job_row(harness.notebook_id, "")
    assert base_row["status"] == "failed"
    assert base_row["failure_reason"] == AGENT_PROFILE_RESTART_FAILURE_MESSAGE
    assert base_row["finished_at"] == NOW

    idle_row = harness.store.job_row(harness.notebook_id, "user-a")
    assert idle_row["status"] == "idle"


def test_the_claim_generation_is_enforced_on_this_backend_too(
    agent_profile_harness,
):
    """The P2 claim generation, proved on PostgreSQL rather than assumed from
    the SQLite mirror.

    Both halves are genuinely backend-specific here. ``settle``'s
    "why did the CAS not land" re-read has to share the settling transaction,
    which on this backend is a real transaction rather than SQLite's
    process-wide write mutex; and ``write_block``'s generation check takes
    ``FOR SHARE`` on the job row, without which a concurrent ``clear_job_row``
    could commit between the check and the write (SQLite's
    ``begin_immediate`` makes that impossible for free).
    """
    harness = agent_profile_harness
    store = harness.store
    notebook = harness.notebook_id

    store.write_block(
        notebook, "user-gen", "retrieval_notes", value="current",
        evidence=[], expected_revision=0, origin="job", actor="",
    )
    before = store.read_block(notebook, "user-gen", "retrieval_notes")

    store.bump_signal(notebook, "user-gen", delta=4)
    stale = store.claim(notebook, "user-gen")

    # Removed, re-added and re-claimed: a byte-identical primary key, a fresh
    # generation.
    store.clear_job_row(notebook, "user-gen")
    store.bump_signal(notebook, "user-gen", delta=7)
    fresh = store.claim(notebook, "user-gen")
    assert fresh.token != stale.token

    with pytest.raises(AgentProfileClaimSuperseded):
        store.write_block(
            notebook, "user-gen", "retrieval_notes", value="stale",
            evidence=[], expected_revision=before["revision"],
            origin="job", actor="", claim_token=stale.token,
        )
    assert store.read_block(notebook, "user-gen", "retrieval_notes") == before

    assert store.settle(
        notebook, "user-gen", "done", blocks_written=3, consumed=4,
        claim_token=stale.token,
    ) == AGENT_PROFILE_SETTLE_SUPERSEDED
    row = store.job_row(notebook, "user-gen")
    assert row["status"] == "running"
    assert row["pending_signal"] == 7
    assert row["runs"] == 0

    # And the row really being gone still reports "gone", which is the branch
    # the caller's revoked-overlay wipe keys off.
    store.clear_job_row(notebook, "user-gen")
    assert store.settle(
        notebook, "user-gen", "done", consumed=7, claim_token=fresh.token
    ) == AGENT_PROFILE_SETTLE_GONE
    assert store.job_row(notebook, "user-gen") is None

"""Store-level coverage for ``AgentProfileStorePort`` (Agentic Memory P1, T2).

Deliberately built directly on
``app.repositories.sqlite.agent_profile_store.AgentProfileStore`` plus a bare
migrated ``SqliteDatabase`` — not through the full ``SQLiteRepository``/
``app.services.repository_runtime`` composition, mirroring
``test_catalog_store.py``'s own rationale: this file proves the STORE
primitive in isolation. T2 wires no consumer to it yet (no route, no
injection, no job), so there is nothing else to exercise it through.

``owner_id`` carries no foreign key (see ``_migration_50``'s docstring), so
these tests use bare strings like ``"user-a"``/``"user-b"`` without seeding
real ``users`` rows for them — only ``notebooks.created_by`` needs a real
user to satisfy that table's own FK.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.ports import (
    AGENT_PROFILE_RESTART_FAILURE_MESSAGE,
    AGENT_PROFILE_SETTLE_GONE,
    AGENT_PROFILE_SETTLE_SUPERSEDED,
    AGENT_PROFILE_SETTLED,
    AgentProfileClaimSuperseded,
    AgentProfileRevisionConflict,
)
from app.repositories.sqlite.agent_profile_store import AgentProfileStore
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator

NOW = "2026-08-18T00:00:00+00:00"
NOTEBOOK_ID = "nb-1"


@pytest.fixture
def store(tmp_path: Path) -> AgentProfileStore:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}")
    database = SqliteDatabase(settings, tmp_path)
    migrated = SqliteMigrator(database, settings).migrate()
    assert migrated, "fresh database must actually run the migration ladder"

    counter = {"n": 0}

    def new_id(prefix: str) -> str:
        counter["n"] += 1
        return f"{prefix}-{counter['n']:04d}"

    with database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?)",
            ("user-owner", "owner@example.test", "Owner", "admin", "active", NOW, NOW),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,"
            "created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (NOTEBOOK_ID, "NB", "", "engineering", "ready", "user-owner", NOW, NOW),
        )

    return AgentProfileStore(database, new_id=new_id, now=lambda: NOW)


# --------------------------------------------------------------- write_block
def test_write_block_creates_a_new_row_at_revision_one(store: AgentProfileStore):
    block = store.write_block(
        NOTEBOOK_ID, "", "corpus_shape",
        value="这本库主要是模拟电路论文",
        evidence=[{"claim_index": 0, "source_ids": ["src-1"]}],
        expected_revision=0,
        origin="job",
        actor="",
    )
    assert block["revision"] == 1
    assert block["value"] == "这本库主要是模拟电路论文"
    assert block["evidence"] == [{"claim_index": 0, "source_ids": ["src-1"]}]
    assert block["updated_origin"] == "job"
    assert block["updated_by"] == ""
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
    # Round-trips through a fresh read.
    assert store.read_block(NOTEBOOK_ID, "", "corpus_shape") == block


def test_write_block_with_expected_revision_zero_against_an_existing_row_conflicts(
    store: AgentProfileStore,
):
    store.write_block(
        NOTEBOOK_ID, "", "corpus_shape", value="v1",
        evidence=[], expected_revision=0, origin="job", actor="",
    )
    with pytest.raises(AgentProfileRevisionConflict):
        store.write_block(
            NOTEBOOK_ID, "", "corpus_shape", value="v2",
            evidence=[], expected_revision=0, origin="job", actor="",
        )
    # The conflicting write left the row untouched.
    assert store.read_block(NOTEBOOK_ID, "", "corpus_shape")["value"] == "v1"


# -------------------------------------------------------- CAS revision conflict
def test_write_block_stale_expected_revision_raises_and_leaves_row_untouched(
    store: AgentProfileStore,
):
    first = store.write_block(
        NOTEBOOK_ID, "", "corpus_shape", value="v1",
        evidence=[], expected_revision=0, origin="job", actor="",
    )
    assert first["revision"] == 1

    with pytest.raises(AgentProfileRevisionConflict) as excinfo:
        store.write_block(
            NOTEBOOK_ID, "", "corpus_shape", value="v2-stale",
            evidence=[], expected_revision=99, origin="job", actor="",
        )
    assert excinfo.value.notebook_id == NOTEBOOK_ID
    assert excinfo.value.owner_id == ""
    assert excinfo.value.label == "corpus_shape"

    # No partial write: value, revision and history all unchanged.
    current = store.read_block(NOTEBOOK_ID, "", "corpus_shape")
    assert current["value"] == "v1"
    assert current["revision"] == 1
    assert len(current["history"]) == 1

    # The CORRECT revision still succeeds afterwards.
    second = store.write_block(
        NOTEBOOK_ID, "", "corpus_shape", value="v2",
        evidence=[], expected_revision=1, origin="user", actor="user-owner",
    )
    assert second["revision"] == 2
    assert second["value"] == "v2"


# --------------------------------------------------------------- history ring
def test_write_block_history_ring_stays_bounded_at_twenty_and_drops_oldest(
    store: AgentProfileStore,
):
    block = store.write_block(
        NOTEBOOK_ID, "", "corpus_shape", value="v0",
        evidence=[], expected_revision=0, origin="job", actor="",
    )
    for i in range(1, 25):
        block = store.write_block(
            NOTEBOOK_ID, "", "corpus_shape", value=f"v{i}",
            evidence=[], expected_revision=block["revision"], origin="job", actor="",
        )
    # 25 total writes (v0..v24) -> ring holds only the most recent 20.
    assert block["revision"] == 25
    assert len(block["history"]) == 20
    # Oldest entries (v0 -> v1, v1 -> v2, ...) were dropped; the ring keeps
    # the tail, ending with the very last transition.
    assert block["history"][0]["before"] == "v4"
    assert block["history"][0]["after"] == "v5"
    assert block["history"][-1]["before"] == "v23"
    assert block["history"][-1]["after"] == "v24"


# ----------------------------------------------------------------- clear_block
def test_clear_block_blanks_value_but_keeps_the_row_and_its_history(
    store: AgentProfileStore,
):
    written = store.write_block(
        NOTEBOOK_ID, "", "corpus_shape", value="v1",
        evidence=[{"claim_index": 0, "source_ids": ["src-1"]}],
        expected_revision=0, origin="job", actor="",
    )
    cleared = store.clear_block(
        NOTEBOOK_ID, "", "corpus_shape",
        expected_revision=written["revision"], actor="user-owner",
    )
    assert cleared["value"] == ""
    assert cleared["evidence"] == []
    assert cleared["revision"] == written["revision"] + 1
    assert cleared["updated_origin"] == "user"
    assert cleared["updated_by"] == "user-owner"
    # The row itself, and its prior history entry, survive the clear.
    assert len(cleared["history"]) == 2
    assert cleared["history"][0]["after"] == "v1"
    assert cleared["history"][-1] == {
        "before": "v1", "after": "", "origin": "user",
        "actor": "user-owner", "at": NOW, "revision": written["revision"] + 1,
    }
    # Reading it back confirms the row is still there, not deleted.
    assert store.read_block(NOTEBOOK_ID, "", "corpus_shape") is not None


def test_clear_block_on_a_never_written_label_raises_key_error(
    store: AgentProfileStore,
):
    with pytest.raises(KeyError):
        store.clear_block(
            NOTEBOOK_ID, "", "usage_gaps", expected_revision=0, actor="user-owner",
        )


def test_clear_block_stale_revision_conflicts(store: AgentProfileStore):
    store.write_block(
        NOTEBOOK_ID, "", "corpus_shape", value="v1",
        evidence=[], expected_revision=0, origin="job", actor="",
    )
    with pytest.raises(AgentProfileRevisionConflict):
        store.clear_block(
            NOTEBOOK_ID, "", "corpus_shape", expected_revision=99, actor="user-owner",
        )
    assert store.read_block(NOTEBOOK_ID, "", "corpus_shape")["value"] == "v1"


# ------------------------------------------------------------------- clear_all
def test_clear_all_deletes_every_row_in_the_scope_and_nothing_else(
    store: AgentProfileStore,
):
    store.write_block(
        NOTEBOOK_ID, "", "corpus_shape", value="base-1",
        evidence=[], expected_revision=0, origin="job", actor="",
    )
    store.write_block(
        NOTEBOOK_ID, "", "key_entities", value="base-2",
        evidence=[], expected_revision=0, origin="job", actor="",
    )
    store.write_block(
        NOTEBOOK_ID, "user-a", "retrieval_notes", value="overlay-a",
        evidence=[], expected_revision=0, origin="job", actor="",
    )

    deleted = store.clear_all(NOTEBOOK_ID, "")
    assert deleted == 2
    assert store.read_block(NOTEBOOK_ID, "", "corpus_shape") is None
    assert store.read_block(NOTEBOOK_ID, "", "key_entities") is None
    # A different scope (the overlay) is untouched.
    assert store.read_block(NOTEBOOK_ID, "user-a", "retrieval_notes") is not None


# -------------------------------------------------------------- read isolation
def test_read_blocks_never_returns_another_users_overlay(store: AgentProfileStore):
    store.write_block(
        NOTEBOOK_ID, "", "corpus_shape", value="shared base",
        evidence=[], expected_revision=0, origin="job", actor="",
    )
    store.write_block(
        NOTEBOOK_ID, "user-a", "retrieval_notes", value="A's private notes",
        evidence=[], expected_revision=0, origin="job", actor="",
    )
    store.write_block(
        NOTEBOOK_ID, "user-b", "retrieval_notes", value="B's private notes",
        evidence=[], expected_revision=0, origin="job", actor="",
    )

    a_view = store.read_blocks(NOTEBOOK_ID, "user-a")
    a_labels = {(row["owner_id"], row["label"], row["value"]) for row in a_view}
    assert a_labels == {
        ("", "corpus_shape", "shared base"),
        ("user-a", "retrieval_notes", "A's private notes"),
    }
    # B's overlay never appears in A's read, and vice versa.
    assert "B's private notes" not in {row["value"] for row in a_view}

    b_view = store.read_blocks(NOTEBOOK_ID, "user-b")
    b_labels = {(row["owner_id"], row["label"], row["value"]) for row in b_view}
    assert b_labels == {
        ("", "corpus_shape", "shared base"),
        ("user-b", "retrieval_notes", "B's private notes"),
    }
    assert "A's private notes" not in {row["value"] for row in b_view}

    # owner_id='' degrades to base-only through the SAME statement.
    base_view = store.read_blocks(NOTEBOOK_ID, "")
    assert [row["label"] for row in base_view] == ["corpus_shape"]


# ------------------------------------------------------------------- job rows
def test_job_row_is_none_until_the_chain_is_ever_touched(store: AgentProfileStore):
    assert store.job_row(NOTEBOOK_ID, "") is None


def test_bump_signal_creates_the_row_on_first_touch_and_accumulates(
    store: AgentProfileStore,
):
    assert store.bump_signal(NOTEBOOK_ID, "", delta=1) == 1
    row = store.job_row(NOTEBOOK_ID, "")
    assert row is not None
    assert row["status"] == "idle"
    assert row["pending_signal"] == 1

    assert store.bump_signal(NOTEBOOK_ID, "", delta=1) == 2
    assert store.bump_signal(NOTEBOOK_ID, "", delta=3) == 5
    assert store.job_row(NOTEBOOK_ID, "")["pending_signal"] == 5


def test_bump_signal_rejects_a_negative_delta(store: AgentProfileStore):
    """A negative bump is refused, not clamped: the only legitimate decrement
    is ``settle(consumed=...)``, which is CAS'd on the chain being claimed."""
    with pytest.raises(ValueError):
        store.bump_signal(NOTEBOOK_ID, "", delta=-1)
    # Nothing was written by the rejected call.
    assert store.job_row(NOTEBOOK_ID, "") is None


# --------------------------------------------------------------------- claim
def test_claim_returns_the_pending_signal_snapshot_and_only_once(
    store: AgentProfileStore,
):
    store.bump_signal(NOTEBOOK_ID, "", delta=5)

    claimed = store.claim(NOTEBOOK_ID, "")
    assert claimed.pending_signal == 5
    row = store.job_row(NOTEBOOK_ID, "")
    assert row["status"] == "running"
    assert row["started_at"] == NOW
    # The token handed back is the one stamped on the row.
    assert claimed.token
    assert row["claim_token"] == claimed.token

    # A second claim while still running is refused.
    assert store.claim(NOTEBOOK_ID, "") is None

    # Once settled back to a non-busy state, claim can succeed again — with a
    # DIFFERENT token, which is the whole point of the generation.
    assert store.settle(
        NOTEBOOK_ID, "", "done", consumed=5, claim_token=claimed.token
    ) == AGENT_PROFILE_SETTLED
    again = store.claim(NOTEBOOK_ID, "")
    assert again.pending_signal == 0
    assert again.token != claimed.token


def test_claim_creates_the_row_for_a_never_signalled_chain(
    store: AgentProfileStore,
):
    """A manual "rebuild now" must be able to take the slot on a chain that
    has never crossed a threshold — without faking a bump that would corrupt
    the automatic path's counter. The claim itself creates the row."""
    assert store.job_row(NOTEBOOK_ID, "") is None
    assert store.claim(NOTEBOOK_ID, "").pending_signal == 0
    row = store.job_row(NOTEBOOK_ID, "")
    assert row is not None
    assert row["status"] == "running"
    assert row["pending_signal"] == 0
    # And it is still single-flight on this freshly created row.
    assert store.claim(NOTEBOOK_ID, "") is None


def test_claim_resets_the_previous_runs_leftovers(store: AgentProfileStore):
    """Without this, a successful run keeps displaying the last failure's
    reason (and its blocks_written / finished_at)."""
    store.bump_signal(NOTEBOOK_ID, "", delta=1)
    first = store.claim(NOTEBOOK_ID, "")
    store.settle(
        NOTEBOOK_ID, "", "failed",
        failure_reason="模型未配置", diagnostic="no binding",
        blocks_written=2, consumed=0, claim_token=first.token,
    )
    stale = store.job_row(NOTEBOOK_ID, "")
    assert stale["failure_reason"] == "模型未配置"
    assert stale["finished_at"] == NOW

    assert store.claim(NOTEBOOK_ID, "").pending_signal == 1
    fresh = store.job_row(NOTEBOOK_ID, "")
    assert fresh["status"] == "running"
    assert fresh["failure_reason"] == ""
    assert fresh["diagnostic"] == ""
    assert fresh["blocks_written"] == 0
    assert fresh["finished_at"] == ""
    # runs is a lifetime counter and is NOT part of the reset.
    assert fresh["runs"] == 1


# -------------------------------------------------------------------- settle
def test_settle_consumes_the_claimed_snapshot_and_increments_runs(
    store: AgentProfileStore,
):
    store.bump_signal(NOTEBOOK_ID, "", delta=5)
    claimed = store.claim(NOTEBOOK_ID, "")
    assert claimed.pending_signal == 5

    assert store.settle(
        NOTEBOOK_ID, "", "done", blocks_written=3,
        consumed=claimed.pending_signal, claim_token=claimed.token,
    ) == AGENT_PROFILE_SETTLED
    row = store.job_row(NOTEBOOK_ID, "")
    assert row["status"] == "done"
    assert row["pending_signal"] == 0
    assert row["runs"] == 1
    assert row["blocks_written"] == 3
    assert row["finished_at"] == NOW


def test_settle_keeps_signals_that_arrived_while_the_run_was_in_flight(
    store: AgentProfileStore,
):
    """The whole reason ``claim`` returns a snapshot instead of ``settle``
    zeroing the counter: twelve files uploaded DURING a consolidation must
    still count toward the next round. A blanket reset discarded them and
    left the corpus un-reconsolidated until twelve more arrived."""
    store.bump_signal(NOTEBOOK_ID, "", delta=5)
    claimed = store.claim(NOTEBOOK_ID, "")
    assert claimed.pending_signal == 5

    # Twelve more source changes land while the run is still going.
    for _ in range(12):
        store.bump_signal(NOTEBOOK_ID, "", delta=1)
    assert store.job_row(NOTEBOOK_ID, "")["pending_signal"] == 17

    store.settle(
        NOTEBOOK_ID, "", "done",
        consumed=claimed.pending_signal, claim_token=claimed.token,
    )
    # Only the 5 the run actually looked at were consumed.
    assert store.job_row(NOTEBOOK_ID, "")["pending_signal"] == 12


def test_settle_with_consumed_zero_leaves_pending_signal_untouched(
    store: AgentProfileStore,
):
    """A failed run consumed nothing, so its triggering changes must still
    count toward the retry."""
    store.bump_signal(NOTEBOOK_ID, "", delta=5)
    claimed = store.claim(NOTEBOOK_ID, "")
    store.settle(
        NOTEBOOK_ID, "", "failed", failure_reason="模型未配置", consumed=0,
        claim_token=claimed.token,
    )
    row = store.job_row(NOTEBOOK_ID, "")
    assert row["status"] == "failed"
    assert row["failure_reason"] == "模型未配置"
    assert row["pending_signal"] == 5


def test_settle_floors_pending_signal_at_zero(store: AgentProfileStore):
    """``consumed`` larger than the current counter (a caller passing a stale
    snapshot) must not drive it negative."""
    store.bump_signal(NOTEBOOK_ID, "", delta=2)
    claimed = store.claim(NOTEBOOK_ID, "")
    store.settle(
        NOTEBOOK_ID, "", "done", consumed=99, claim_token=claimed.token
    )
    assert store.job_row(NOTEBOOK_ID, "")["pending_signal"] == 0


def test_settle_is_cas_and_a_repeated_settle_touches_nothing(
    store: AgentProfileStore,
):
    store.bump_signal(NOTEBOOK_ID, "", delta=1)
    claimed = store.claim(NOTEBOOK_ID, "")
    assert store.settle(
        NOTEBOOK_ID, "", "done",
        consumed=claimed.pending_signal, claim_token=claimed.token,
    ) == AGENT_PROFILE_SETTLED
    # Already terminal: a second settle is a no-op, not an overwrite. The row
    # is still there, so the outcome is "superseded", not "gone".
    assert store.settle(
        NOTEBOOK_ID, "", "failed", consumed=0, claim_token=claimed.token
    ) == AGENT_PROFILE_SETTLE_SUPERSEDED
    assert store.job_row(NOTEBOOK_ID, "")["status"] == "done"


def test_settle_rejects_a_non_terminal_status(store: AgentProfileStore):
    store.bump_signal(NOTEBOOK_ID, "", delta=1)
    claimed = store.claim(NOTEBOOK_ID, "")
    with pytest.raises(ValueError):
        store.settle(
            NOTEBOOK_ID, "", "running", consumed=1, claim_token=claimed.token
        )


# ------------------------------------------------------------ sweep_stale_on_start
def test_sweep_stale_on_start_force_settles_queued_and_running_chains(
    store: AgentProfileStore,
):
    store.bump_signal(NOTEBOOK_ID, "", delta=1)
    store.claim(NOTEBOOK_ID, "")  # -> running
    store.bump_signal(NOTEBOOK_ID, "user-a", delta=1)
    store.claim(NOTEBOOK_ID, "user-a")  # -> running
    store.bump_signal(NOTEBOOK_ID, "user-b", delta=1)  # -> idle, never claimed

    swept = store.sweep_stale_on_start()
    assert swept == 2

    base_row = store.job_row(NOTEBOOK_ID, "")
    assert base_row["status"] == "failed"
    assert base_row["failure_reason"] == AGENT_PROFILE_RESTART_FAILURE_MESSAGE

    a_row = store.job_row(NOTEBOOK_ID, "user-a")
    assert a_row["status"] == "failed"
    assert a_row["failure_reason"] == AGENT_PROFILE_RESTART_FAILURE_MESSAGE

    # The idle chain (never claimed) is untouched — sweep only reclaims
    # queued/running rows.
    b_row = store.job_row(NOTEBOOK_ID, "user-b")
    assert b_row["status"] == "idle"

    # Idempotent: nothing left to sweep the second time.
    assert store.sweep_stale_on_start() == 0


# --------------------------------------------------- claim generation (P2)
def test_a_settle_from_a_superseded_claim_lands_on_nothing(
    store: AgentProfileStore,
):
    """THE ABA case (Agentic Memory P2, closing the registered R4 residual).

    A member is removed (``clear_job_row``) and re-added while their overlay
    run is inside its model call; the re-add lets a NEW run claim the chain.
    Under P1's status-only CAS the stale worker's settle matched the
    replacement row — it consumed the new run's snapshot and moved the new
    run's status to terminal, releasing a slot it never held.

    The token makes the two rows distinguishable even though their primary key
    is byte-identical: the stale settle now touches nothing and reports
    ``superseded`` — NOT ``gone``, which is what makes the caller keep its
    hands off blocks the new generation may already have written.
    """
    store.bump_signal(NOTEBOOK_ID, "user-a", delta=4)
    stale = store.claim(NOTEBOOK_ID, "user-a")

    # Removal, re-add, and a fresh claim — all while the stale run is running.
    store.clear_job_row(NOTEBOOK_ID, "user-a")
    store.bump_signal(NOTEBOOK_ID, "user-a", delta=7)
    fresh = store.claim(NOTEBOOK_ID, "user-a")
    assert fresh.token != stale.token

    assert store.settle(
        NOTEBOOK_ID, "user-a", "done", blocks_written=3,
        consumed=stale.pending_signal, claim_token=stale.token,
    ) == AGENT_PROFILE_SETTLE_SUPERSEDED

    row = store.job_row(NOTEBOOK_ID, "user-a")
    # The new generation still holds the slot, with its own snapshot intact.
    assert row["status"] == "running"
    assert row["claim_token"] == fresh.token
    assert row["pending_signal"] == 7
    assert row["blocks_written"] == 0
    assert row["runs"] == 0


def test_a_settle_after_a_real_removal_reports_gone(store: AgentProfileStore):
    """The other half of the tri-state, and the reason it cannot collapse back
    into a bool: here the row really is gone (only member removal deletes it),
    so the caller's revoked-overlay wipe MUST fire."""
    store.bump_signal(NOTEBOOK_ID, "user-a", delta=2)
    claimed = store.claim(NOTEBOOK_ID, "user-a")
    store.clear_job_row(NOTEBOOK_ID, "user-a")

    assert store.settle(
        NOTEBOOK_ID, "user-a", "done",
        consumed=claimed.pending_signal, claim_token=claimed.token,
    ) == AGENT_PROFILE_SETTLE_GONE
    # A settle never resurrects the row.
    assert store.job_row(NOTEBOOK_ID, "user-a") is None


def test_a_write_from_a_superseded_claim_changes_no_byte(
    store: AgentProfileStore,
):
    """The write half of the same ABA. The service's pre-write ``job_row``
    probe passes here — a row IS present — so only the generation carried into
    the write transaction can refuse this."""
    store.write_block(
        NOTEBOOK_ID, "user-a", "retrieval_notes",
        value="post-rejoin", evidence=[], expected_revision=0,
        origin="job", actor="",
    )
    before = store.read_block(NOTEBOOK_ID, "user-a", "retrieval_notes")

    stale = store.claim(NOTEBOOK_ID, "user-a")
    store.clear_job_row(NOTEBOOK_ID, "user-a")
    fresh = store.claim(NOTEBOOK_ID, "user-a")
    assert fresh.token != stale.token

    with pytest.raises(AgentProfileClaimSuperseded):
        store.write_block(
            NOTEBOOK_ID, "user-a", "retrieval_notes",
            value="pre-removal", evidence=[],
            expected_revision=before["revision"],
            origin="job", actor="", claim_token=stale.token,
        )
    assert store.read_block(NOTEBOOK_ID, "user-a", "retrieval_notes") == before

    # The CURRENT generation writes through the same call unchanged.
    store.write_block(
        NOTEBOOK_ID, "user-a", "retrieval_notes",
        value="current", evidence=[], expected_revision=before["revision"],
        origin="job", actor="", claim_token=fresh.token,
    )
    assert store.read_block(
        NOTEBOOK_ID, "user-a", "retrieval_notes"
    )["value"] == "current"


def test_a_write_without_a_token_asserts_no_generation(
    store: AgentProfileStore,
):
    """The user-facing edit path holds no claim and must not be forced to fake
    one: an empty ``claim_token`` skips the generation check entirely, even
    while a run holds the chain."""
    store.claim(NOTEBOOK_ID, "user-a")
    row = store.write_block(
        NOTEBOOK_ID, "user-a", "retrieval_notes",
        value="mine", evidence=[], expected_revision=0,
        origin="user", actor="user-a",
    )
    assert row["value"] == "mine"


def test_the_startup_sweep_strands_every_in_flight_token(
    store: AgentProfileStore,
):
    """``sweep_stale_on_start`` deliberately ignores the token (it force-settles
    rows whose workers died with the previous process). The consequence is the
    wanted one: a worker that somehow survived the sweep can no longer settle,
    because the row it holds a token for is terminal."""
    store.bump_signal(NOTEBOOK_ID, "user-a", delta=1)
    claimed = store.claim(NOTEBOOK_ID, "user-a")
    assert store.sweep_stale_on_start() == 1

    assert store.settle(
        NOTEBOOK_ID, "user-a", "done",
        consumed=claimed.pending_signal, claim_token=claimed.token,
    ) == AGENT_PROFILE_SETTLE_SUPERSEDED
    assert store.job_row(NOTEBOOK_ID, "user-a")["status"] == "failed"


def test_a_deployed_v52_database_gains_the_claim_token_column(tmp_path: Path):
    """已部署库补建:回滚到 v52 再开一次,v53 必须补上这一列。

    同时钉住本迁移必须**可重跑**:「已部署库补建」这一族测试的做法是只回滚
    ``user_version``,整条梯子会在一张**可能已经带着这一列**的表上重跑本迁移。
    SQLite 没有 ``ADD COLUMN IF NOT EXISTS``,裸 ALTER 会以
    ``duplicate column name`` 炸在启动路径上——五条历史补建用例一起红,而它们
    与 Agent 库理解毫无关系,单看报错完全指不回这里。
    """
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'deployed.db'}")
    database = SqliteDatabase(settings, tmp_path)
    SqliteMigrator(database, settings).migrate()

    with database.write() as db:
        db.execute("ALTER TABLE agent_profile_jobs DROP COLUMN claim_token")
        db.execute("PRAGMA user_version = 52")
    database.close_local()

    reopened = SqliteDatabase(settings, tmp_path)
    SqliteMigrator(reopened, settings).migrate()
    with reopened.connect() as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(agent_profile_jobs)")}
        assert "claim_token" in columns

    # Re-running the ladder over the already-patched table must be a no-op,
    # not a duplicate-column error.
    with reopened.write() as db:
        db.execute("PRAGMA user_version = 52")
    reopened.close_local()
    again = SqliteDatabase(settings, tmp_path)
    assert SqliteMigrator(again, settings).migrate()

"""Store-level coverage for ``AgentObservationStorePort`` (Agentic Memory P3,
T2).

Deliberately built directly on
``app.repositories.sqlite.agent_observation_store.AgentObservationStore``
plus a bare migrated ``SqliteDatabase`` — not through the full
``SQLiteRepository``/``app.services.repository_runtime`` composition,
mirroring ``test_agent_profile_store.py``'s own rationale: this file proves
the STORE primitive in isolation. T2 wires no consumer to it yet (no MCP
tool, no consolidation job, no route), so there is nothing else to exercise
it through.

``owner_id``/``agent_profile_id`` carry no foreign key (see
``_migration_55``'s docstring), so these tests use bare strings like
``"user-a"``/``"agent-1"`` without seeding real ``users``/``agent_profiles``
rows for them — only ``notebooks.created_by`` needs a real user to satisfy
that table's own FK.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.ports import AGENT_OBSERVATION_RING_MAX
from app.repositories.sqlite.agent_observation_store import AgentObservationStore
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator

NOW = "2026-08-20T00:00:00+00:00"
NOTEBOOK_ID = "nb-1"
OTHER_NOTEBOOK_ID = "nb-2"


class _Clock:
    """A monotonically advancing ISO clock — one call, one second later.

    Used instead of a fixed timestamp wherever a test cares about eviction
    ORDER, so the ordering assertion exercises ``created_at`` rather than
    coincidentally passing off the ``id`` tie-break alone.
    """

    def __init__(self, start: str = NOW) -> None:
        self._base = datetime.fromisoformat(start)
        self._n = 0

    def __call__(self) -> str:
        value = (self._base + timedelta(seconds=self._n)).isoformat()
        self._n += 1
        return value


@dataclass
class Harness:
    database: SqliteDatabase
    store: AgentObservationStore
    clock: _Clock


def _seed_notebooks(database: SqliteDatabase) -> None:
    with database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?)",
            ("user-owner", "owner@example.test", "Owner", "admin", "active", NOW, NOW),
        )
        for notebook_id in (NOTEBOOK_ID, OTHER_NOTEBOOK_ID):
            db.execute(
                "INSERT INTO notebooks(id,name,purpose,primary_domain,status,"
                "created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (notebook_id, "NB", "", "engineering", "ready", "user-owner", NOW, NOW),
            )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}")
    database = SqliteDatabase(settings, tmp_path)
    migrated = SqliteMigrator(database, settings).migrate()
    assert migrated, "fresh database must actually run the migration ladder"
    _seed_notebooks(database)

    counter = {"n": 0}

    def new_id(prefix: str) -> str:
        counter["n"] += 1
        return f"{prefix}-{counter['n']:06d}"

    clock = _Clock()
    return Harness(
        database=database,
        store=AgentObservationStore(database, new_id=new_id, now=clock),
        clock=clock,
    )


# -------------------------------------------------------------- idempotency
def test_append_observation_same_key_returns_the_same_row_and_does_not_duplicate(
    harness: Harness,
):
    first_id, first_dup = harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1",
        text="noticed X while working here", client_request_id="req-1",
    )
    assert first_dup is False

    second_id, second_dup = harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1",
        text="a different text — must be ignored", client_request_id="req-1",
    )
    assert second_id == first_id
    assert second_dup is True

    rows = harness.store.list_observations(NOTEBOOK_ID, "user-a", limit=10)
    assert len(rows) == 1
    assert rows[0]["text"] == "noticed X while working here"


def test_append_observation_idempotency_key_includes_agent_profile_id(
    harness: Harness,
):
    """⚠ Mutation-tested contract point: two DIFFERENT Agents that happen to
    mint the same client-side ``client_request_id`` must each get their own
    row. If the idempotency key ever drops ``agent_profile_id`` (from either
    the pre-insert SELECT or the ``IntegrityError`` re-read), this collapses
    to one row and this test goes red — see the T2 report for the manual
    mutation run that confirmed it."""
    id_a, dup_a = harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1",
        text="from agent 1", client_request_id="shared-req",
    )
    id_b, dup_b = harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-2",
        text="from agent 2", client_request_id="shared-req",
    )
    assert dup_a is False
    assert dup_b is False
    assert id_a != id_b

    rows = harness.store.list_observations(NOTEBOOK_ID, "user-a", limit=10)
    assert {row["agent_profile_id"] for row in rows} == {"agent-1", "agent-2"}
    assert len(rows) == 2


# ------------------------------------------------------------------ eviction
def test_append_observation_evicts_down_to_the_ring_max_keeping_the_newest(
    harness: Harness,
):
    total = AGENT_OBSERVATION_RING_MAX + 5
    inserted_ids: list[str] = []
    for i in range(total):
        observation_id, deduplicated = harness.store.append_observation(
            NOTEBOOK_ID, "user-a", "agent-1",
            text=f"note {i}", client_request_id=f"req-{i}",
        )
        assert deduplicated is False
        inserted_ids.append(observation_id)

    rows = harness.store.list_observations(NOTEBOOK_ID, "user-a", limit=total)
    assert len(rows) == AGENT_OBSERVATION_RING_MAX

    kept_ids = {row["id"] for row in rows}
    assert kept_ids == set(inserted_ids[-AGENT_OBSERVATION_RING_MAX:])
    # The ring keeps the group at exactly the bound at the database level too
    # — not just what a bounded read happens to return.
    with harness.database.connect() as db:
        total_rows = db.execute(
            "SELECT COUNT(*) AS n FROM agent_observations "
            "WHERE notebook_id=? AND owner_id=?",
            (NOTEBOOK_ID, "user-a"),
        ).fetchone()["n"]
    assert total_rows == AGENT_OBSERVATION_RING_MAX


def test_ring_eviction_is_per_owner_scope(harness: Harness):
    """A's ring filling up must never evict B's rows in the same notebook."""
    for i in range(AGENT_OBSERVATION_RING_MAX + 5):
        harness.store.append_observation(
            NOTEBOOK_ID, "user-a", "agent-1",
            text=f"a-note {i}", client_request_id=f"a-req-{i}",
        )
    harness.store.append_observation(
        NOTEBOOK_ID, "user-b", "agent-1",
        text="b-note", client_request_id="b-req-1",
    )
    b_rows = harness.store.list_observations(NOTEBOOK_ID, "user-b", limit=10)
    assert len(b_rows) == 1
    assert b_rows[0]["text"] == "b-note"


# ------------------------------------------------------------------ isolation
def test_recent_observations_is_scoped_by_owner_id(harness: Harness):
    harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1", text="a-only", client_request_id="r1",
    )
    harness.store.append_observation(
        NOTEBOOK_ID, "user-b", "agent-1", text="b-only", client_request_id="r1",
    )
    a_rows = harness.store.recent_observations(NOTEBOOK_ID, "user-a", limit=10)
    b_rows = harness.store.recent_observations(NOTEBOOK_ID, "user-b", limit=10)
    assert [row["text"] for row in a_rows] == ["a-only"]
    assert [row["text"] for row in b_rows] == ["b-only"]
    # And the four-field projection never carries owner_id at all.
    assert set(a_rows[0]) == {"id", "agent_profile_id", "text", "created_at"}


def test_list_observations_matches_recent_observations(harness: Harness):
    for i in range(3):
        harness.store.append_observation(
            NOTEBOOK_ID, "user-a", "agent-1",
            text=f"note {i}", client_request_id=f"req-{i}",
        )
    assert harness.store.list_observations(
        NOTEBOOK_ID, "user-a", limit=10
    ) == harness.store.recent_observations(NOTEBOOK_ID, "user-a", limit=10)


# ---------------------------------------------------------------------- clear
def test_clear_observations_by_agent_profile_only_removes_that_agent(
    harness: Harness,
):
    harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1", text="keep-target", client_request_id="r1",
    )
    harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-2", text="other-agent", client_request_id="r1",
    )
    removed = harness.store.clear_observations(
        NOTEBOOK_ID, "user-a", agent_profile_id="agent-1"
    )
    assert removed == 1
    remaining = harness.store.recent_observations(NOTEBOOK_ID, "user-a", limit=10)
    assert [row["agent_profile_id"] for row in remaining] == ["agent-2"]


def test_clear_observations_without_agent_profile_removes_everything_in_scope(
    harness: Harness,
):
    harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1", text="a", client_request_id="r1",
    )
    harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-2", text="b", client_request_id="r2",
    )
    harness.store.append_observation(
        NOTEBOOK_ID, "user-b", "agent-1", text="other-owner", client_request_id="r1",
    )
    removed = harness.store.clear_observations(NOTEBOOK_ID, "user-a")
    assert removed == 2
    assert harness.store.recent_observations(NOTEBOOK_ID, "user-a", limit=10) == []
    # user-b's scope is untouched.
    assert len(harness.store.recent_observations(NOTEBOOK_ID, "user-b", limit=10)) == 1


# -------------------------------------------------------------------- cascade
def test_notebook_delete_cascades_observation_rows(harness: Harness):
    harness.store.append_observation(
        NOTEBOOK_ID, "user-a", "agent-1", text="a", client_request_id="r1",
    )
    harness.store.append_observation(
        OTHER_NOTEBOOK_ID, "user-a", "agent-1", text="b", client_request_id="r1",
    )
    with harness.database.write() as db:
        db.execute("DELETE FROM notebooks WHERE id=?", (NOTEBOOK_ID,))
    with harness.database.connect() as db:
        remaining = db.execute(
            "SELECT notebook_id FROM agent_observations"
        ).fetchall()
    assert [row["notebook_id"] for row in remaining] == [OTHER_NOTEBOOK_ID]

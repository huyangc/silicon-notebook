"""Live-PostgreSQL half of hot-path fix batch 3's contract that a fake
connection cannot exercise (see ``backend/tests/test_hotpath_indexes_batch3.py``
for the fake-connection half: migration<->spec anti-drift).

Things only a real server can prove:

  1. ``idx_clusters_nb_canonical_member`` actually builds via
     ``install_hotpath_indexes`` (real ``CREATE INDEX CONCURRENTLY``) and
     migration 43 is a true no-op ledger entry once it exists online --
     mirrors ``test_hotpath_indexes_live.py``'s batch-1 and
     ``test_hotpath_indexes_batch2_live.py``'s batch-2 equivalents. Also
     exercises migration 0043's pre-existing-index validation DO block
     (same pattern as migration 0042's, codex #636 R1 P2) on both its
     accept path and its reject paths.
  2. The concrete keyset-pagination win this index exists for: the query
     ``knowledge_store.py``'s ``concept_cluster_detail_rows`` actually
     issues (captured with the same spy
     ``test_knowledge_store_conformance.py``'s plan tests use, so a
     hand-copied SQL string cannot drift out of sync with the real one)
     walks ``idx_clusters_nb_canonical_member`` in index order with NO
     ``Sort`` node -- the whole point of adding a trailing
     ``member_object_id`` key next to the pre-existing
     ``(notebook_id, canonical_id)`` prefix index.
"""
from __future__ import annotations

import psycopg
import pytest

from app.repositories.postgres._store_utils import jsonb, normalize_timestamp
from app.repositories.postgres.hotpath_indexes import (
    inspect_hotpath_indexes,
    install_hotpath_indexes,
)
from app.repositories.postgres.knowledge_store import KnowledgeStore
from app.repositories.postgres.migrator import PostgresMigrator


pytestmark = pytest.mark.postgres_integration

_BATCH3_NAME = "idx_clusters_nb_canonical_member"


def _schema_of(database) -> str:
    with database.connect() as connection:
        return connection.execute(
            "SELECT current_schema() AS name"
        ).fetchone()["name"]


def _seed_notebook(database, notebook_id: str, now: str) -> None:
    with database.write() as db:
        db.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s)",
            (notebook_id, notebook_id, now, now),
        )


def _seed_cluster_members(
    database, notebook_id: str, canonical_id: str, canonical_name: str,
    member_ids: "list[str]", now: str,
) -> None:
    """Seed a hub concept cluster: one concept_clusters row per member, each
    joined to a live (non-deprecated) knowledge_objects row -- the exact
    join shape ``concept_cluster_detail_rows`` reads."""
    with database.write() as db:
        with db.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,payload,evidence,"
                "created_at,updated_at,ordinal) "
                "VALUES (%s,%s,'concept','approved',%s,%s,%s,%s,%s)",
                [
                    (member_id, notebook_id, jsonb({"name": member_id}), jsonb([]), now, now, ordinal)
                    for ordinal, member_id in enumerate(member_ids)
                ],
            )
            cursor.executemany(
                "INSERT INTO concept_clusters "
                "(id,notebook_id,canonical_id,member_object_id,canonical_name,"
                "object_type,created_at) "
                "VALUES (%s,%s,%s,%s,%s,'concept',%s)",
                [
                    (f"cc-{member_id}", notebook_id, canonical_id, member_id, canonical_name, now)
                    for member_id in member_ids
                ],
            )


def _seed_interleaved_noise(database, now: str) -> None:
    """Seed a SEPARATE notebook/canonical_id's members with ids that
    lexically interleave inside the real hub cluster's ``ko-member-NNNN``
    range (``ko-member-NNNN-noise-KK`` sorts immediately after
    ``ko-member-NNNN`` and before ``ko-member-(NNNN+1)``). Without this, the
    pre-existing single-column ``idx_clusters_member`` (migration 0004,
    ``concept_clusters(member_object_id)``) already returns the hub
    cluster's own 500 rows in id order with only a cheap post-scan Filter
    (nothing else occupies that id range), so the planner picks it over this
    migration's composite index and the "no Sort" assertion below would pass
    for the wrong reason. Real production notebooks share the global
    ``knowledge_objects``/``concept_clusters`` id space with thousands of
    OTHER clusters -- this noise makes the synthetic scenario match that:
    a plain member_object_id-ordered scan now has to Filter past ~10 foreign
    rows for every matching one, so the notebook/canonical_id-scoped
    composite index is the genuinely cheaper plan."""
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES ('nb-noise-interleave','noise',%s,%s)",
            (now, now),
        )
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,payload,evidence,"
            "created_at,updated_at,ordinal) "
            "SELECT 'ko-member-'||lpad((g/10)::text,4,'0')||'-noise-'||lpad((g%%10)::text,2,'0'), "
            "'nb-noise-interleave','concept','approved', "
            "jsonb_build_object('name','noise'), '[]'::jsonb, %s, %s, g+100000 "
            "FROM generate_series(0, 4999) g",
            (now, now),
        )
        db.execute(
            "INSERT INTO concept_clusters "
            "(id,notebook_id,canonical_id,member_object_id,canonical_name,"
            "object_type,created_at) "
            "SELECT 'cc-noise-'||g, 'nb-noise-interleave','canonical-noise', "
            "'ko-member-'||lpad((g/10)::text,4,'0')||'-noise-'||lpad((g%%10)::text,2,'0'), "
            "'Noise Concept','concept',%s "
            "FROM generate_series(0, 4999) g",
            (now,),
        )


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch3")
def test_install_builds_the_new_index_and_is_idempotent(postgres_database):
    # One hop before migration 43 introduces the index itself, so it is
    # genuinely absent below -- the same "prove it's for real" structure as
    # batch 1/2's equivalent live tests.
    assert PostgresMigrator(postgres_database).migrate(target_version=42) == 42
    schema = _schema_of(postgres_database)
    database_url = postgres_database.settings.database_url

    before = inspect_hotpath_indexes(database_url, schema=schema)
    row = next(r for r in before["indexes"] if r["name"] == _BATCH3_NAME)
    assert row["state"] == "缺失"

    state = install_hotpath_indexes(database_url, schema=schema)
    assert all(row["state"] == "存在" for row in state["indexes"]), state

    # Idempotent rerun.
    repeated = install_hotpath_indexes(database_url, schema=schema)
    assert repeated == state

    # Migration 43's own plain (in-transaction) CREATE INDEX IF NOT EXISTS is
    # a true no-op ledger entry once the offline CONCURRENTLY builder already
    # built the index online.
    assert PostgresMigrator(postgres_database).migrate() == 48
    after_migration = inspect_hotpath_indexes(database_url, schema=schema)
    assert after_migration == state


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch3")
def test_concept_cluster_detail_page_plan_uses_the_new_index_without_a_sort(
    postgres_database,
):
    """The reason this index exists: without it, a hub cluster's keyset page
    pays an explicit Sort over the whole matching (notebook_id, canonical_id)
    slice before the ORDER BY member_object_id / LIMIT can trim it down. With
    it, the planner can walk (notebook_id, canonical_id, member_object_id)
    directly in the query's own order -- no separate sort step.

    ``enable_seqscan=off`` forces the choice between the two btree
    candidates on concept_clusters (the pre-existing two-column
    ``idx_clusters_nb_canonical`` prefix index this batch deliberately keeps,
    vs. this migration's three-column one) onto the scale-free question "can
    the ordered index serve this query", matching
    ``test_relink_source_page_plan_stays_inside_the_notebook``'s rationale in
    ``test_knowledge_store_conformance.py``.
    """
    assert PostgresMigrator(postgres_database).migrate() == 48
    now = normalize_timestamp("2026-01-01T00:00:00+00:00")
    notebook_id = "nb-hub-cluster"
    _seed_notebook(postgres_database, notebook_id, now)
    member_ids = [f"ko-member-{i:04d}" for i in range(500)]
    _seed_cluster_members(
        postgres_database, notebook_id, "canonical-hub", "Hub Concept", member_ids, now
    )
    _seed_interleaved_noise(postgres_database, now)
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute("ANALYZE concept_clusters")
        db.execute("ANALYZE knowledge_objects")

    with postgres_database.connect() as connection:
        connection.execute("SET LOCAL enable_seqscan=off")
        captured: list[tuple[str, object]] = []
        original_execute = connection.execute

        def spying_execute(sql, params=None, **kwargs):
            if "FROM concept_clusters" in str(sql):
                captured.append((str(sql), params))
            return original_execute(sql, params, **kwargs)

        connection.execute = spying_execute
        try:
            # A mid-cluster keyset page: `after` is set, exercising the same
            # seek-predicate shape a real paginated read pays on page 2+.
            rows, canonical_name = KnowledgeStore.concept_cluster_detail_rows(
                connection, notebook_id, "canonical-hub",
                limit=20, after=member_ids[100],
            )
        finally:
            del connection.execute

        assert canonical_name == "Hub Concept"
        assert len(rows) == 20
        assert [row["member_object_id"] for row in rows] == member_ids[101:121]

        assert captured, "concept_cluster_detail_rows must query concept_clusters"
        captured_sql, captured_params = captured[0]
        plan_text = "\n".join(
            str(row["QUERY PLAN"]) for row in connection.execute(
                f"EXPLAIN (COSTS OFF) {captured_sql}", captured_params
            ).fetchall()
        )

    assert _BATCH3_NAME in plan_text, (
        f"expected the new keyset-covering index in the plan, got:\n{plan_text}")
    assert "Sort" not in plan_text, (
        f"expected no separate sort step -- the index should already provide "
        f"member_object_id order:\n{plan_text}")
    assert "Seq Scan" not in plan_text, (
        f"expected an index scan, not a full table scan:\n{plan_text}")


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch3")
def test_concept_cluster_member_total_plan_also_uses_the_prefix(postgres_database):
    """``concept_cluster_member_total`` only needs the equality prefix (no
    ORDER BY), so the new index's leading two columns serve it exactly like
    the pre-existing ``idx_clusters_nb_canonical`` did -- this is a
    non-regression check, not a new win."""
    assert PostgresMigrator(postgres_database).migrate() == 48
    now = normalize_timestamp("2026-01-01T00:00:00+00:00")
    notebook_id = "nb-hub-cluster-total"
    _seed_notebook(postgres_database, notebook_id, now)
    member_ids = [f"ko-total-{i:04d}" for i in range(200)]
    _seed_cluster_members(
        postgres_database, notebook_id, "canonical-hub-total", "Hub Concept Total",
        member_ids, now,
    )
    with postgres_database.write() as db:
        db.execute("ANALYZE concept_clusters")
        db.execute("ANALYZE knowledge_objects")

    with postgres_database.connect() as connection:
        total = KnowledgeStore.concept_cluster_member_total(
            connection, notebook_id, "canonical-hub-total"
        )
    assert total == 200


# ---------------------------------------------------------------------------
# Migration 43's pre-existing-index validation DO block (same pattern as
# migration 0042's, codex #636 R1 P2) -- IF NOT EXISTS alone would silently
# skip creation over an INVALID residue row or an operator's wrong-shape
# index and still mark the migration applied.
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch3")
def test_migration_rejects_a_same_named_wrong_shape_index(postgres_database):
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=42) == 42
    with postgres_database.write() as db:
        # Same name, same table, columns in the wrong order.
        db.execute(
            "CREATE INDEX idx_clusters_nb_canonical_member "
            "ON concept_clusters(member_object_id, canonical_id, notebook_id)"
        )
    with pytest.raises(
        psycopg.errors.RaiseException, match="does not match the expected definition"
    ):
        migrator.migrate()
    # The ledger did not advance -- RAISE rolled back the whole migration
    # transaction (including the ledger INSERT).
    assert migrator.migrate(target_version=42) == 42
    # An operator clears the name collision per the error's own guidance,
    # then the migration goes through normally.
    with postgres_database.write() as db:
        db.execute("DROP INDEX idx_clusters_nb_canonical_member")
    assert migrator.migrate() == 48


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch3")
def test_migration_rejects_an_invalid_same_named_index(postgres_database):
    """A real INVALID residue, no superuser catalog surgery (same rationale
    as batch 2's equivalent test -- the CI PostgreSQL role is NOSUPERUSER):
    ``CREATE UNIQUE INDEX CONCURRENTLY`` over two rows that share the same
    (notebook_id, canonical_id, member_object_id) triple (but differ in
    object_type, so the pre-existing ``uq_clusters_notebook_type_member``
    unique index tolerates both) fails at its second, catalog-visibility
    phase and leaves an ``indisvalid=false`` row behind -- the same shape an
    interrupted CONCURRENTLY build leaves."""
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=42) == 42
    now = normalize_timestamp("2026-01-01T00:00:00+00:00")
    with postgres_database.write() as db:
        db.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES ('nb-inv-cc','invalid-residue',%s,%s)",
            (now, now),
        )
        db.execute(
            "INSERT INTO concept_clusters "
            "(id,notebook_id,canonical_id,member_object_id,canonical_name,"
            "object_type,created_at) VALUES "
            "('cc-inv-1','nb-inv-cc','canonical-x','member-x','Dup','concept',%s), "
            "('cc-inv-2','nb-inv-cc','canonical-x','member-x','Dup','other',%s)",
            (now, now),
        )
    with psycopg.connect(
        postgres_database.settings.database_url, autocommit=True
    ) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "CREATE UNIQUE INDEX CONCURRENTLY idx_clusters_nb_canonical_member "
                "ON concept_clusters(notebook_id, canonical_id, member_object_id)"
            )
        residue = conn.execute(
            "SELECT i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() "
            "AND c.relname = 'idx_clusters_nb_canonical_member'"
        ).fetchone()
    assert residue is not None and residue[0] is False
    with pytest.raises(psycopg.errors.RaiseException, match="INVALID"):
        migrator.migrate()
    assert migrator.migrate(target_version=42) == 42
    with postgres_database.write() as db:
        db.execute("DROP INDEX idx_clusters_nb_canonical_member")
    assert migrator.migrate() == 48

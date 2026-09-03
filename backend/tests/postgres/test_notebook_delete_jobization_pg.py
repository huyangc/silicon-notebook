"""Batch 3·W1 PR-3 Phase A — PostgreSQL lane (representative subset, not a
full duplication of ``tests/test_notebook_delete_jobization.py``'s SQLite
coverage; see ``test_notebook_lifecycle_visibility_pg.py``'s own docstring
for this codebase's "抽查" convention on PG-lane sibling files: semantics are
already pinned by the SQLite behavioral tests, this file only checks the
things that can plausibly diverge by BACKEND — real advisory-lock-adjacent
CAS behavior, real EXPLAIN plans for the three new indexes, PG SQL dialect
correctness).

Covers:
  1. Migration 0049 is idempotent and a true no-op once already applied.
  2. The three new indexes' EXPLAIN plans (Index Scan, not Seq Scan) —
     G3's "三条新索引各自的 EXPLAIN 验收" requirement.
  3. Tombstone CAS + quiesce dual leg + phase 5 finalize end to end on a
     real PostgreSQL connection (FOR UPDATE locking, statement_timeout
     scoping).
  4. Sweep's two drivers against real timestamptz columns.
"""
from __future__ import annotations

import pytest


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_notebook_delete_jobization"),
]

NOW = "2026-09-01T00:00:00+00:00"


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def _insert_user(db, user_id: str) -> None:
    db.execute(
        "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (user_id, f"{user_id}@x", user_id.upper(), "user", "active", user_id, NOW, NOW),
    )


def _insert_notebook(db, nid: str, owner: str, status: str = "ready") -> None:
    db.execute(
        "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at)"
        " VALUES (%s,%s,%s,%s,%s,%s)",
        (nid, f"NB-{nid}", owner, status, NOW, NOW),
    )


# ---------------------------------------------------------------------------
# 1. Migration idempotence
# ---------------------------------------------------------------------------


def test_migration_0049_applied_and_idempotent(postgres_repository):
    repo = postgres_repository
    with repo._connect() as db:
        tables = {
            row["table_name"]
            for row in db.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=current_schema() AND table_name IN "
                "('notebook_delete_jobs','notebook_delete_files')"
            ).fetchall()
        }
    assert tables == {"notebook_delete_jobs", "notebook_delete_files"}

    # Re-running the migrator must be a no-op (every DDL statement is
    # IF NOT EXISTS / guarded) — mirrors 0042/0043's own "true no-op ledger
    # entry" contract.
    from app.repositories.postgres.migrator import PostgresMigrator

    migrator = PostgresMigrator(repo._runtime.database)
    final_version = migrator.migrate()
    assert final_version == 51


# ---------------------------------------------------------------------------
# 2. EXPLAIN plans for the three new indexes (G3 requirement)
# ---------------------------------------------------------------------------


def test_agent_tokens_fk_cascade_probe_uses_an_index_scan(postgres_repository):
    """相位 5 的 `DELETE FROM notebooks` FK 级联探查从 Seq Scan 变 Index Scan
    (design doc §1.1 — agent_access_tokens 是 47 条 L1 FK 里唯一缺前导索引
    的那张表)。"""
    repo = postgres_repository
    with repo._connect() as db:
        plan = db.execute(
            "EXPLAIN (FORMAT TEXT) SELECT 1 FROM agent_access_tokens "
            "WHERE default_notebook_id = 'nb-does-not-exist'"
        ).fetchall()
    text = "\n".join(row["QUERY PLAN"] for row in plan)
    assert "Index" in text and "Seq Scan" not in text, text


def test_knowhow_cell_code_column_leg_uses_an_index_scan(postgres_repository):
    repo = postgres_repository
    with repo._connect() as db:
        plan = db.execute(
            "EXPLAIN (FORMAT TEXT) SELECT 1 FROM knowhow_cell_code "
            "WHERE column_id = 'col-does-not-exist'"
        ).fetchall()
    text = "\n".join(row["QUERY PLAN"] for row in plan)
    assert "Index" in text and "Seq Scan" not in text, text


def test_conversations_notebook_leg_uses_an_index_scan(postgres_repository):
    """形二(ctid)内层 `SELECT ... WHERE notebook_id=%s ... LIMIT n` 的前导索引
    (Phase B 才真正驱动这条循环,但索引现在就必须就位)。"""
    repo = postgres_repository
    with repo._connect() as db:
        plan = db.execute(
            "EXPLAIN (FORMAT TEXT) SELECT ctid FROM conversations "
            "WHERE notebook_id = 'nb-does-not-exist' LIMIT 500"
        ).fetchall()
    text = "\n".join(row["QUERY PLAN"] for row in plan)
    assert "Index" in text and "Seq Scan" not in text, text


# ---------------------------------------------------------------------------
# 3. Tombstone CAS + quiesce + finalize end to end (real PostgreSQL)
# ---------------------------------------------------------------------------


def test_end_to_end_delete_on_postgres(postgres_repository):
    repo = postgres_repository
    owner = "u-pg-e2e"
    nb = "nb-pg-e2e"
    with repo._write() as db:
        _insert_user(db, owner)
        _insert_notebook(db, nb, owner)
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,file_path,source_type,"
            "status,parse_status,created_at,updated_at) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("src-pg-e2e", nb, "S", "/tmp/pg-e2e.pdf", "pdf", "ready", "parsed",
             NOW, NOW),
        )

    rt = repo._runtime
    job = rt.notebook_delete_jobs.request(nb, owner)
    assert job["status"] == "queued"
    with repo._connect() as db:
        status = db.execute(
            "SELECT status FROM notebooks WHERE id=%s", (nb,)
        ).fetchone()["status"]
    assert status == "deleting"

    runner = rt.notebook_delete
    runner.run(job["id"])

    with pytest.raises(KeyError):
        rt.notebook_delete_jobs.get(job["id"])
    with repo._connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM notebooks WHERE id=%s", (nb,)
        ).fetchone()["c"]
        files = db.execute(
            "SELECT COUNT(*) AS c FROM notebook_delete_files WHERE job_id=%s",
            (job["id"],),
        ).fetchone()["c"]
    assert remaining == 0
    assert files == 0


def test_quiesce_leg_a_blocks_on_postgres_then_clears(postgres_repository):
    repo = postgres_repository
    owner = "u-pg-quiesce"
    nb = "nb-pg-quiesce"
    with repo._write() as db:
        _insert_user(db, owner)
        _insert_notebook(db, nb, owner)
        db.execute(
            "INSERT INTO kg_build_jobs (id,notebook_id,created_by,mode,status,"
            "stage,total_sources,completed_sources,failed_sources,error_code,"
            "error_message,created_at,updated_at,finished_at) VALUES "
            "(%s,%s,%s,%s,'running','extracting',1,0,0,'','',%s,%s,NULL)",
            ("kgj-pg-quiesce", nb, owner, "incremental", NOW, NOW),
        )

    rt = repo._runtime
    runner = rt.notebook_delete
    runner._quiesce_timeout_seconds = 0.3
    job = rt.notebook_delete_jobs.request(nb, owner)
    runner.run(job["id"])

    waiting = rt.notebook_delete_jobs.get(job["id"])
    assert waiting["status"] == "waiting"

    with repo._write() as db:
        db.execute(
            "UPDATE kg_build_jobs SET status='succeeded' WHERE id='kgj-pg-quiesce'"
        )
    runner._quiesce_timeout_seconds = 30
    runner.run(job["id"])
    with pytest.raises(KeyError):
        rt.notebook_delete_jobs.get(job["id"])


def test_request_cas_conflict_on_postgres(postgres_repository):
    from app.repositories.ports import NotebookAlreadyDeletingError

    repo = postgres_repository
    owner = "u-pg-cas"
    nb = "nb-pg-cas"
    with repo._write() as db:
        _insert_user(db, owner)
        _insert_notebook(db, nb, owner)

    rt = repo._runtime
    rt.notebook_delete_jobs.request(nb, owner)
    with pytest.raises(NotebookAlreadyDeletingError):
        rt.notebook_delete_jobs.request(nb, owner)
    with pytest.raises(KeyError):
        rt.notebook_delete_jobs.request("nb-does-not-exist", owner)


def test_finalize_timeout_config_is_scoped_to_the_one_transaction(
    postgres_repository, postgres_settings
):
    """D-4:事务局部 `set_config` 不得泄漏到池的下一个借用者。"""
    repo = postgres_repository
    owner = "u-pg-timeout"
    nb = "nb-pg-timeout"
    with repo._write() as db:
        _insert_user(db, owner)
        _insert_notebook(db, nb, owner)

    rt = repo._runtime
    rt.notebook_store._finalize_timeout_ms = 5000
    job = rt.notebook_delete_jobs.request(nb, owner)
    rt.notebook_store.delete_row_and_orphan_embeddings(nb, job_id=job["id"])

    with repo._connect() as db:
        row = db.execute(
            "SELECT setting FROM pg_settings WHERE name='statement_timeout'"
        ).fetchone()
    # Pool's own restore (postgres/database.py's _restore_session_defaults)
    # reasserts the session-level value on every borrow — the transaction-
    # local override from the finalize transaction above must not leak here.
    # pg_settings.setting for a 'ms'-unit GUC is the raw millisecond integer
    # as text (unlike SHOW, which renders a human-friendly unit like '2s').
    expected_ms = postgres_settings.postgres_statement_timeout_seconds * 1000
    assert row["setting"] == str(expected_ms)
    assert row["setting"] != "5000"  # the transaction-local override, leaked


def test_set_config_transaction_local_reverts_on_commit_same_connection(
    postgres_database,
):
    """P2-e (code review): the test above proves the value is gone by the
    time a NEW pooled borrow observes it -- which the pool's own
    ``_restore_session_defaults`` reset-on-return would ALSO guarantee even
    if D-4's ``set_config(..., true)`` had a bug (e.g. the third arg flipped
    to session-scoped ``false``), masking a real regression. This test
    isolates D-4's OWN mechanism directly: assert the override is visible
    INSIDE the same transaction, on the SAME connection, before it is ever
    returned to the pool -- then assert it is gone the instant that
    transaction commits (still well before any pool reset could run)."""
    with postgres_database.write() as connection:
        connection.execute(
            "SELECT set_config('statement_timeout', %s, true)", ("5000ms",)
        )
        # Same connection, same transaction, before this `with` block even
        # exits (let alone before the connection returns to the pool).
        mid_transaction = connection.execute(
            "SELECT setting FROM pg_settings WHERE name='statement_timeout'"
        ).fetchone()
        assert mid_transaction["setting"] == "5000"
    # The `with` block above committed on exit -- `true` (transaction-local)
    # means the override is already gone at this instant, independent of
    # anything the pool does on return.
    with postgres_database.connect() as connection:
        after_commit = connection.execute(
            "SELECT setting FROM pg_settings WHERE name='statement_timeout'"
        ).fetchone()
    assert after_commit["setting"] != "5000"


# ---------------------------------------------------------------------------
# 4. Sweep's two drivers against real timestamptz columns
# ---------------------------------------------------------------------------


def test_sweep_driver_a_on_postgres(postgres_repository):
    repo = postgres_repository
    owner = "u-pg-sweep-a"
    nb = "nb-pg-sweep-a"
    with repo._write() as db:
        _insert_user(db, owner)
        _insert_notebook(db, nb, owner)

    rt = repo._runtime
    job = rt.notebook_delete_jobs.request(nb, owner)
    rt.notebook_delete_jobs.mark_running(job["id"], stale_cutoff_seconds=300)
    with repo._write() as db:
        db.execute(
            "UPDATE notebook_delete_jobs SET updated_at='2000-01-01T00:00:00+00:00' "
            "WHERE id=%s",
            (job["id"],),
        )

    runner = rt.notebook_delete
    runner._sweep_seconds = 1
    assert runner.sweep_once() == 1
    from app.services import background_jobs

    background_jobs._drain_maintenance_executors_for_tests(timeout=10.0)
    with repo._connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM notebooks WHERE id=%s", (nb,)
        ).fetchone()["c"]
    assert remaining == 0


def test_sweep_driver_b_on_postgres(postgres_repository):
    repo = postgres_repository
    owner = "u-pg-sweep-b"
    nb = "nb-pg-sweep-b"
    with repo._write() as db:
        _insert_user(db, owner)
        _insert_notebook(db, nb, owner, status="deleting")

    rt = repo._runtime
    runner = rt.notebook_delete
    assert runner.sweep_once() == 1
    from app.services import background_jobs

    background_jobs._drain_maintenance_executors_for_tests(timeout=10.0)
    with repo._connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM notebooks WHERE id=%s", (nb,)
        ).fetchone()["c"]
    assert remaining == 0

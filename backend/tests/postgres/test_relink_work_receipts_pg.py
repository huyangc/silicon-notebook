"""Real PostgreSQL twin for durable relink receipt behavior."""
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from app.repositories.postgres import relink_work_store as store
from tests.test_relink_work_receipts import receipt_contract, seed_tables

pytestmark = pytest.mark.postgres_integration


@pytest.mark.parametrize("rollback", [False, True])
@pytest.mark.parametrize("staged", [False, True])
def test_replacement_requeues_surviving_partitions_atomically(postgres_settings, rollback, staged):
    from app.services.postgres_repository import PostgresRepository
    from tests.relink_staged_cleanup_cases import assert_staged_cleanup_receipts

    repo = PostgresRepository(postgres_settings)
    try:
        assert_staged_cleanup_receipts(repo, postgres=True, rollback=rollback, staged=staged)
    finally:
        repo.close()


def test_receipts_resume_and_reject_stale_results(postgres_database):
    with postgres_database.write() as db:
        seed_tables(db, postgres=True)
        receipt_contract(db, store)


def test_import_reset_is_scoped_and_rolls_back_with_graph_transaction(
    postgres_database, postgres_settings, tmp_path,
):
    from types import SimpleNamespace

    from app.migration.sync.import_ import _Backend, _reset_local_relink_receipts

    backend = _Backend(postgres_settings, tmp_path)
    context = SimpleNamespace(
        manifest=SimpleNamespace(notebooks=['nb']), deleted_notebooks={'deleted'},
    )
    try:
        with postgres_database.write() as db:
            seed_tables(db, postgres=True)
            db.execute("INSERT INTO notebooks VALUES ('deleted'),('local-kept')")
            for notebook_id in ('nb', 'deleted', 'local-kept'):
                store.prepare(db, notebook_id, 1)
                store.enqueue(db, notebook_id, '', 1)
                for row in store.page(db, notebook_id, None, 100):
                    assert store.finish(db, notebook_id, row['source_id'], row['revision'], 0, 0, 0)
                assert store.finish_full(db, notebook_id)
            db.execute('SAVEPOINT graph_import')
            _reset_local_relink_receipts(backend, db, 'knowledge_objects', context, include_deleted=True)
            assert store.summary(db, 'nb')['total'] == 0
            db.execute('ROLLBACK TO SAVEPOINT graph_import')
            assert store.summary(db, 'nb')['done'] > 0
            _reset_local_relink_receipts(backend, db, 'knowledge_relations', context, include_deleted=True)
            assert store.summary(db, 'nb')['total'] == 0
            assert store.summary(db, 'deleted')['total'] == 0
            assert store.summary(db, 'local-kept')['done'] == 1
            assert store.prepare(db, 'nb', 1)['full_pending']
    finally:
        backend.close()


def test_deleted_object_invalidates_owner_and_both_adjacent_partitions(postgres_database):
    with postgres_database.write() as db:
        seed_tables(db, postgres=True)
        db.execute("ALTER TABLE knowledge_objects ADD COLUMN id TEXT")
        db.execute("CREATE TEMP TABLE knowledge_relations "
                   "(notebook_id TEXT, source_object_id TEXT, target_object_id TEXT)")
        db.execute("INSERT INTO notebooks VALUES ('other')")
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,source_id) VALUES "
                   "('deleted','nb','b'),('incoming','nb','c'),('outgoing','nb','d'),"
                   "('unrelated','nb','e'),('foreign','other','foreign-source')")
        db.execute("INSERT INTO knowledge_relations VALUES "
                   "('nb','incoming','deleted'),('nb','deleted','outgoing'),"
                   "('other','deleted','foreign')")
        for source_id in ('b', 'c', 'd', 'e'):
            store.enqueue(db, 'nb', source_id, 1)
            assert store.finish(db, 'nb', source_id, 1, 0, 0, 0)
        store.enqueue(db, 'other', 'foreign-source', 1)
        assert store.finish(db, 'other', 'foreign-source', 1, 0, 0, 0)

        # Mutation owners invalidate while endpoints still exist, then delete
        # the object and incident edges in the same transaction.
        store.enqueue_for_objects(db, 'nb', ['deleted'], 1, adjacent=True)
        db.execute("DELETE FROM knowledge_relations WHERE notebook_id='nb' "
                   "AND (source_object_id='deleted' OR target_object_id='deleted')")
        db.execute("DELETE FROM knowledge_objects WHERE notebook_id='nb' AND id='deleted'")
        pending = store.page(db, 'nb', None, 10)
        assert [(row['source_id'], row['revision']) for row in pending] == [
            ('b', 2), ('c', 2), ('d', 2),
        ]
        assert store.count_pending(db, 'other') == 0
        assert not store.finish(db, 'nb', 'b', 1, 0, 0, 0)


@pytest.mark.postgres_fresh_schema
@pytest.mark.parametrize("parent", ["sources", "notebooks"])
def test_parent_publication_does_not_deadlock_relink_receipt(postgres_scope, parent):
    """A source/notebook publisher can enqueue while relink waits on its parent.

    The pre-fix worker held the receipt at that point, so enqueue would time out
    (or PostgreSQL would choose a deadlock victim). No timing-based interleaving:
    wait for the real PostgreSQL lock wait before attempting the publication.
    """
    ddl = (Path(__file__).parents[2] / "app/repositories/postgres/migrations/0091_kg_relink_work.sql").read_text()
    with psycopg.connect(postgres_scope.url, autocommit=True, row_factory=dict_row) as db:
        db.execute("CREATE TABLE notebooks (id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE sources (id TEXT PRIMARY KEY,notebook_id TEXT)")
        db.execute(ddl)
        db.execute("INSERT INTO notebooks VALUES ('nb')")
        db.execute("INSERT INTO sources VALUES ('src','nb')")
        store.enqueue(db, "nb", "src", 1)

    def relink():
        with psycopg.connect(postgres_scope.url, row_factory=dict_row) as db:
            db.execute("SET LOCAL statement_timeout='5s'")
            db.execute("SET LOCAL lock_timeout='3s'")
            current = store.current_for_write(db, "nb", "src", 1)
            if current:
                # Model the parent FK lock of relation insertion. This makes
                # the former receipt-before-parent inversion reproducible too.
                db.execute(
                    f"SELECT id FROM {parent} WHERE id=%s FOR KEY SHARE",
                    ("src" if parent == "sources" else "nb",),
                ).fetchone()
            return current

    with psycopg.connect(postgres_scope.url, row_factory=dict_row) as publisher:
        publisher.execute("SET LOCAL lock_timeout='1s'")
        publisher.execute(
            f"SELECT id FROM {parent} WHERE id=%s FOR UPDATE",
            ("src" if parent == "sources" else "nb",),
        ).fetchone()
        blocker_pid = publisher.info.backend_pid
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(relink)
            try:
                with psycopg.connect(postgres_scope.url, autocommit=True) as inspector:
                    deadline = time.monotonic() + 2
                    while True:
                        assert not future.done(), "relink did not wait on the publisher's parent"
                        waiting = inspector.execute(
                            "SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
                            "AND wait_event_type='Lock' AND %s=ANY(pg_blocking_pids(pid))",
                            (blocker_pid,),
                        ).fetchone()
                        if waiting:
                            break
                        assert time.monotonic() < deadline, "relink did not reach parent lock"
                        time.sleep(0.01)
                store.enqueue(publisher, "nb", "src", 1)
                publisher.commit()
                assert future.result(timeout=5) is False
            finally:
                publisher.rollback()

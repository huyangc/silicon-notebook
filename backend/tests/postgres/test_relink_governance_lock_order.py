"""Real lock waits pin receipt-before-object ordering against source replacement."""
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from app.models.schemas import KnowledgeUpdate, NotebookCreate
from app.repositories.postgres import relink_work_store
from app.repositories.postgres.governance_store import GovernanceStore
from app.services.postgres_repository import PostgresRepository

pytestmark = pytest.mark.postgres_integration
NOW = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def orphan_objects(postgres_settings):
    repo = PostgresRepository(postgres_settings)
    nb = repo.create_notebook(NotebookCreate(name="reservation refusal")).id
    with repo._write() as db:
        for oid, kind in (('a', 'claim'), ('b', 'concept')):
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,source_id,object_type,"
                       "status,payload,evidence,created_at,updated_at) "
                       "VALUES (%s,%s,'',%s,'approved','{}','[]',%s,%s)",
                       (oid, nb, kind, NOW, NOW))
    try:
        yield repo, nb
    finally:
        repo.close()


def test_refused_mutations_leave_no_receipt_even_if_caller_commits(orphan_objects):
    repo, nb = orphan_objects
    with repo._write() as db:
        with pytest.raises(ValueError):
            GovernanceStore.update_object_in_transaction(
                db, nb, 'a', KnowledgeUpdate(status='invalid'), NOW,
            )
        with pytest.raises(ValueError):
            GovernanceStore.merge_objects_in_transaction(db, nb, 'a', 'b', NOW)
    with repo._connect() as db:
        assert relink_work_store.summary(db, nb)['total'] == 0


def test_nonwaiting_reservation_defers_missing_and_contended_receipts(orphan_objects):
    repo, nb = orphan_objects

    @relink_work_store.receipt_lock_scope
    def reserve(db):
        return relink_work_store.reserve_for_objects(db, nb, ['a'], wait=False)

    with repo._write() as db:
        assert not reserve(db)
        assert relink_work_store.summary(db, nb)['total'] == 0
        relink_work_store.enqueue(db, nb, '', 1)
    with repo._write() as holder:
        relink_work_store.enqueue(holder, nb, '', 1)
        def follow():
            with repo._write() as follower:
                follower.execute("SET LOCAL lock_timeout='1s'")
                return reserve(follower)
        with ThreadPoolExecutor(max_workers=1) as workers:
            assert workers.submit(follow).result(timeout=2) is False
        holder.rollback()
    with repo._connect() as db:
        assert db.execute("SELECT revision FROM kg_relink_work WHERE notebook_id=%s", (nb,)).fetchone()['revision'] == 1


def test_removed_placeholder_keeps_key_fenced_until_outer_commit(orphan_objects):
    repo, nb = orphan_objects

    @relink_work_store.receipt_lock_scope
    def reserve(db):
        assert relink_work_store.reserve_for_objects(db, nb, ['a'])

    started = Event()
    writer_pid = []

    def writer():
        with repo._write() as db:
            writer_pid.append(db.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid'])
            started.set()
            relink_work_store.enqueue(db, nb, '', 1)

    with repo._write() as outer:
        reserve(outer)
        assert relink_work_store.summary(outer, nb)['total'] == 0
        holder_pid = outer.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(writer)
            try:
                assert started.wait(timeout=2)
                deadline = time.monotonic() + 2
                while True:
                    assert not future.done(), 'reservation fence ended before graph transaction'
                    if outer.execute('SELECT %s=ANY(pg_blocking_pids(%s)) AS blocked',
                                     (holder_pid, writer_pid[0])).fetchone()['blocked']:
                        break
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                outer.commit()
                future.result(timeout=3)
            finally:
                outer.rollback()


@pytest.mark.parametrize("mutation", ["edit", "merge"])
def test_replacement_can_delete_while_governance_waits_for_receipt(postgres_settings, mutation):
    repo = PostgresRepository(postgres_settings)
    try:
        nb = repo.create_notebook(NotebookCreate(name="receipt lock order")).id
        with repo._write() as db:
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,"
                       "parse_status,file_name,summary,created_at,updated_at) "
                       "VALUES ('source',%s,'source','file','ready','ready','','',%s,%s)",
                       (nb, NOW, NOW))
            for oid in ('edited', 'target'):
                db.execute("INSERT INTO knowledge_objects (id,notebook_id,source_id,object_type,"
                           "status,payload,evidence,created_at,updated_at) "
                           "VALUES (%s,%s,'source','claim','approved','{}','[]',%s,%s)",
                           (oid, nb, NOW, NOW))
            repo._runtime.knowledge.relink_work.enqueue(db, nb, 'source', 1)

        started = Event()
        editor_pid = []

        def edit():
            try:
                with repo._write() as db:
                    db.execute("SET LOCAL lock_timeout='4s'")
                    editor_pid.append(db.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid'])
                    started.set()
                    if mutation == 'edit':
                        GovernanceStore.update_object_in_transaction(
                            db, nb, 'edited', KnowledgeUpdate(payload={'name': 'edited'}), NOW,
                        )
                    else:
                        GovernanceStore.merge_objects_in_transaction(db, nb, 'edited', 'target', NOW)
            except KeyError:
                return 'replacement won'
            return 'edited'

        with repo._write() as publisher:
            publisher.execute("SET LOCAL lock_timeout='1s'")
            repo._runtime.knowledge.relink_work.enqueue(publisher, nb, 'source', 1)
            publisher_pid = publisher.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
            with ThreadPoolExecutor(max_workers=1) as workers:
                future = workers.submit(edit)
                try:
                    assert started.wait(timeout=2)
                    deadline = time.monotonic() + 2
                    while True:
                        assert not future.done(), 'mutation did not wait for receipt'
                        blocked = publisher.execute(
                            'SELECT %s = ANY(pg_blocking_pids(%s)) AS blocked',
                            (publisher_pid, editor_pid[0]),
                        ).fetchone()['blocked']
                        if blocked:
                            break
                        assert time.monotonic() < deadline
                        time.sleep(0.01)
                    # The old edit/merge already holds an object lock while
                    # waiting here, so replacement hits lock_timeout/deadlock.
                    repo._runtime.knowledge.clear_source_graph_state(publisher, 'source', nb)
                    publisher.commit()
                    assert future.result(timeout=5) == 'replacement won'
                finally:
                    publisher.rollback()
    finally:
        repo.close()

"""First-use receipts must not invert source teardown versus notebook deletion."""
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest

from app.models.schemas import NotebookCreate
from app.repositories.postgres.source_store import SourceStore
from app.services.postgres_repository import PostgresRepository

pytestmark = pytest.mark.postgres_integration


def test_first_receipt_during_teardown_does_not_deadlock_notebook_finalize(postgres_settings, monkeypatch):
    repo = PostgresRepository(postgres_settings)
    try:
        nb = repo.create_notebook(NotebookCreate(name="parent lock order")).id
        with repo._write() as db:
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,"
                       "parse_status,file_name,summary,created_at,updated_at) "
                       "VALUES ('parent-source',%s,'s','file','ready','ready','','',now(),now())", (nb,))
        started = Event()
        finalizer_pid = []
        database = repo._runtime.database
        original_write = database.write

        @contextmanager
        def observed_write():
            with original_write() as db:
                db.execute("SET LOCAL lock_timeout='4s'")
                finalizer_pid.append(db.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
                started.set()
                yield db

        with original_write() as remover:
            remover.execute("SET LOCAL lock_timeout='1s'")
            SourceStore.lock_sources_for_teardown_tx(remover, ['parent-source'])
            holder_pid = remover.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            monkeypatch.setattr(database, 'write', observed_write)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(repo._runtime.notebook_store.delete_row_and_orphan_embeddings, nb)
                try:
                    assert started.wait(timeout=2)
                    deadline = time.monotonic() + 2
                    while True:
                        assert not future.done(), 'finalize must wait for active source teardown'
                        blocked = remover.execute("SELECT %s=ANY(pg_blocking_pids(%s)) AS blocked",
                                                  (holder_pid, finalizer_pid[0])).fetchone()['blocked']
                        if blocked:
                            break
                        assert time.monotonic() < deadline
                        time.sleep(0.01)
                    # No receipt existed before teardown. Its first insertion
                    # checks the notebook FK while finalize is already waiting.
                    repo._runtime.knowledge.relink_work.enqueue(remover, nb, 'parent-source', 1)
                    SourceStore.delete_source_rows(remover, ['parent-source'])
                    remover.commit()
                    assert future.result(timeout=5) == []
                finally:
                    remover.rollback()
        with repo._connect() as db:
            assert db.execute("SELECT 1 FROM notebooks WHERE id=%s", (nb,)).fetchone() is None
            assert db.execute("SELECT 1 FROM kg_relink_work WHERE notebook_id=%s", (nb,)).fetchone() is None
    finally:
        repo.close()

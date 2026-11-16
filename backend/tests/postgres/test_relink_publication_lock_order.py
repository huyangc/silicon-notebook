"""Source publication must serialize affected receipts before graph replacement."""
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event, local

import psycopg
import pytest
from psycopg.rows import dict_row

from app.models.schemas import NotebookCreate
from app.services.postgres_repository import PostgresRepository

pytestmark = pytest.mark.postgres_integration
NOW = "2026-01-01T00:00:00+00:00"


def test_cross_source_replacements_do_not_lock_each_others_receipts(postgres_settings, monkeypatch):
    repo = PostgresRepository(postgres_settings)
    release_first = Event()
    try:
        nb = repo.create_notebook(NotebookCreate(name="cross-source replacement")).id
        service = repo._runtime.knowledge_lifecycle
        with repo._write() as db:
            for sid in ("a", "b"):
                db.execute(
                    "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                    "parse_status,file_name,summary,created_at,updated_at) "
                    "VALUES (%s,%s,%s,'file','ready','ready','','',%s,%s)",
                    (sid, nb, sid, NOW, NOW),
                )
                db.execute(
                    "INSERT INTO knowledge_objects (id,notebook_id,source_id,object_type,"
                    "status,payload,evidence,created_at,updated_at) "
                    "VALUES (%s,%s,%s,'claim','approved','{}','[]',%s,%s)",
                    ("object-" + sid, nb, sid, NOW, NOW),
                )
                service.knowledge.relink_work.enqueue(db, nb, sid, 1)
            db.execute(
                "INSERT INTO knowledge_relations (id,notebook_id,source_id,source_object_id,"
                "target_object_id,edge_type,evidence,created_at) "
                "VALUES ('cross-edge',%s,'a','object-a','object-b','about','[]',%s)",
                (nb, NOW),
            )

        first_at_clear = Event()
        writer_started = {sid: Event() for sid in ("a", "b")}
        writer_pids = {}
        current = local()
        original_write = service._write
        original_clear = service.knowledge.clear_source_graph_state

        @contextmanager
        def observed_write():
            with original_write() as db:
                db.execute("SET LOCAL lock_timeout='4s'")
                writer_pids[current.source] = db.execute(
                    "SELECT pg_backend_pid() AS pid"
                ).fetchone()["pid"]
                writer_started[current.source].set()
                yield db

        def controlled_clear(db, source_id, notebook_id):
            if source_id == "a":
                first_at_clear.set()
                assert release_first.wait(timeout=8), "controller did not release first replacement"
            return original_clear(db, source_id, notebook_id)

        monkeypatch.setattr(service, "_write", observed_write)
        monkeypatch.setattr(service.knowledge, "clear_source_graph_state", controlled_clear)

        def replace(source_id):
            current.source = source_id
            return service.store_kg(nb, source_id, [], [], replace_source=True)

        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(replace, "a")
            try:
                assert first_at_clear.wait(timeout=3)
                second = workers.submit(replace, "b")
                assert writer_started["b"].wait(timeout=3)
                # Observe the actual database wait. With the old order B owns
                # receipt B and waits for A; letting A continue closes the
                # cycle. With ordered publication B waits before owning B.
                with psycopg.connect(
                    postgres_settings.database_url, autocommit=True, row_factory=dict_row,
                ) as observer:
                    deadline = time.monotonic() + 3
                    while True:
                        assert not second.done(), "second publication failed before the lock check"
                        blocked = observer.execute(
                            "SELECT %s=ANY(pg_blocking_pids(%s)) AS blocked",
                            (writer_pids["a"], writer_pids["b"]),
                        ).fetchone()["blocked"]
                        if blocked:
                            break
                        assert time.monotonic() < deadline, "second replacement did not wait for first"
                        time.sleep(0.01)
                release_first.set()
                assert first.result(timeout=6) == (0, 0)
                assert second.result(timeout=6) == (0, 0)
            finally:
                release_first.set()
        with repo._connect() as db:
            assert db.execute(
                "SELECT COUNT(*) AS n FROM knowledge_objects WHERE notebook_id=%s", (nb,),
            ).fetchone()["n"] == 0
            assert {row["source_id"]: row["status"] for row in db.execute(
                "SELECT source_id,status FROM kg_relink_work WHERE notebook_id=%s", (nb,),
            )} == {"a": "pending", "b": "pending"}
    finally:
        release_first.set()
        repo.close()

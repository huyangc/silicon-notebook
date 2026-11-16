"""Full relink preparation shares writers' source-key lock order."""
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from app.models.schemas import NotebookCreate
from app.repositories.postgres import relink_work_store
from app.services.postgres_repository import PostgresRepository

pytestmark = pytest.mark.postgres_integration


def test_full_prepare_does_not_lock_later_receipt_before_waiting_for_first(postgres_settings):
    repo = PostgresRepository(postgres_settings)
    try:
        notebook_id = repo.create_notebook(NotebookCreate(name="ordered full relink")).id
        with repo._write() as db:
            db.execute(
                "INSERT INTO kg_relink_state (notebook_id,algorithm_version,full_pending) "
                "VALUES (%s,1,0)", (notebook_id,),
            )
            # Deliberately insert in the reverse of the writers' source-key
            # order. These orphan partitions must survive full preparation.
            for source_id in ("b", "a"):
                db.execute(
                    "INSERT INTO kg_relink_work (notebook_id,source_id,algorithm_version,"
                    "revision,status,isolated_before,isolated_after,edges_added) "
                    "VALUES (%s,%s,1,1,'done',3,1,2)",
                    (notebook_id, source_id),
                )

        preparing = Event()
        prepare_pid = []

        def prepare_full():
            with repo._write() as db:
                db.execute("SET LOCAL lock_timeout='4s'")
                prepare_pid.append(db.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
                preparing.set()
                return relink_work_store.prepare(db, notebook_id, 1, full=True)

        with repo._write() as writer:
            writer.execute("SET LOCAL lock_timeout='4s'")
            writer_pid = writer.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            writer.execute(
                "SELECT source_id FROM kg_relink_work WHERE notebook_id=%s "
                "AND source_id='a' FOR UPDATE", (notebook_id,),
            ).fetchone()
            with ThreadPoolExecutor(max_workers=1) as workers:
                preparation = workers.submit(prepare_full)
                try:
                    assert preparing.wait(timeout=3)
                    deadline = time.monotonic() + 3
                    while True:
                        assert not preparation.done(), "full preparation failed before the lock check"
                        if writer.execute(
                            "SELECT %s=ANY(pg_blocking_pids(%s)) AS blocked",
                            (writer_pid, prepare_pid[0]),
                        ).fetchone()["blocked"]:
                            break
                        assert time.monotonic() < deadline, "full preparation did not wait for a"
                        time.sleep(0.01)
                    # A heap-ordered bulk UPDATE has already locked b before
                    # waiting for a, so this ordered writer closes a deadlock.
                    # Ordered preparation waits for a without first owning b.
                    relink_work_store.enqueue(writer, notebook_id, "b", 1)
                    writer.commit()
                    result = preparation.result(timeout=5)
                    assert result["full_pending"] is True
                    assert result["resumed"] is False
                finally:
                    writer.rollback()

        with repo._connect() as db:
            receipts = {row["source_id"]: dict(row) for row in db.execute(
                "SELECT source_id,revision,status,isolated_before,isolated_after,edges_added "
                "FROM kg_relink_work WHERE notebook_id=%s", (notebook_id,),
            )}
            assert {key: row["revision"] for key, row in receipts.items()} == {"a": 2, "b": 3}
            assert all(row["status"] == "pending" for row in receipts.values())
            assert all(
                (row["isolated_before"], row["isolated_after"], row["edges_added"]) == (0, 0, 0)
                for row in receipts.values()
            )
    finally:
        repo.close()

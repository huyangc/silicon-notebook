"""Observable resume/CAS semantics shared by the two relink receipt adapters."""
import sqlite3
from pathlib import Path

from app.repositories.sqlite import relink_work_store as store


def seed_tables(db, postgres=False):
    prefix = "CREATE TEMP TABLE " if postgres else "CREATE TABLE "
    db.execute(prefix + "notebooks (id TEXT PRIMARY KEY)")
    db.execute(prefix + "sources (id TEXT PRIMARY KEY,notebook_id TEXT)")
    db.execute(prefix + "knowledge_objects (source_id TEXT,notebook_id TEXT)")
    db.execute(prefix + "unified_kg_state (notebook_id TEXT,kg_mutation_seq INTEGER)")
    ddl = (Path(__file__).parents[1] / "app/repositories/postgres/migrations/0091_kg_relink_work.sql").read_text()
    if postgres:
        ddl = ddl.replace("CREATE TABLE ", "CREATE TEMP TABLE ")
        # psycopg accepts unparameterized multi-statement DDL.
        db.execute(ddl)
    else:
        db.executescript(ddl.replace('COLLATE "C"', "COLLATE BINARY"))
    db.execute("INSERT INTO notebooks VALUES ('nb')")
    db.execute("INSERT INTO sources VALUES ('a','nb'),('b','nb')")
    db.execute("INSERT INTO knowledge_objects VALUES ('a','nb'),('orphan','nb'),('','nb')")
    db.execute("INSERT INTO unified_kg_state VALUES ('nb',7)")


def receipt_contract(db, api):
    first = api.prepare(db, "nb", 1)
    assert first == {"full_pending": True, "resumed": False, "started_at_seq": 7}
    page = api.page(db, "nb", None, 2)
    assert [r["source_id"] for r in page] == ["", "a"]
    assert api.count_pending(db, "nb") == 4
    empty = page[0]
    assert api.current_for_write(db, "nb", "", empty["revision"])
    assert api.finish(db, "nb", "", empty["revision"], 2, 0, 1)
    assert not api.finish(db, "nb", "", empty["revision"], 2, 0, 1)
    # Restart reuses completed receipts, including when caller explicitly requests full.
    resumed = api.prepare(db, "nb", 1, full=True)
    assert resumed["resumed"] and resumed["started_at_seq"] == 7
    assert api.count_pending(db, "nb") == 3
    assert not api.finish_full(db, "nb")
    a = api.page(db, "nb", None, 1)[0]
    api.enqueue(db, "nb", "a", 1)
    assert not api.current_for_write(db, "nb", "a", a["revision"])
    assert not api.finish(db, "nb", "a", a["revision"], 0, 0, 0)
    for row in api.page(db, "nb", None, 10):
        assert api.finish(db, "nb", row["source_id"], row["revision"], 0, 0, 0)
    assert api.finish_full(db, "nb")
    assert api.summary(db, "nb") == {
        "total": 4, "done": 4, "pending": 0,
        "isolated_before": 2, "isolated_after": 0, "edges_added": 1,
    }
    # Automatic subsequent runs do not re-open old sources.
    assert not api.prepare(db, "nb", 1)["full_pending"]
    assert api.count_pending(db, "nb") == 0
    api.enqueue(db, "nb", "b", 1)
    assert [r["source_id"] for r in api.page(db, "nb", None, 10)] == ["b"]
    # Changed algorithm forces a full pass; old revision tokens cannot commit.
    before = api.page(db, "nb", None, 1)[0]
    assert api.prepare(db, "nb", 2)["full_pending"]
    assert api.count_pending(db, "nb") == 4
    assert not api.current_for_write(db, "nb", "b", before["revision"])


def test_receipts_resume_and_reject_stale_results():
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        seed_tables(db)
        receipt_contract(db, store)


def test_receipt_rollback_leaves_partition_pending():
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        seed_tables(db)
        store.prepare(db, "nb", 1)
        db.commit()
        row = store.page(db, "nb", None, 1)[0]
        assert store.finish(db, "nb", row["source_id"], row["revision"], 0, 0, 0)
        db.rollback()
        assert store.current_for_write(db, "nb", row["source_id"], row["revision"])

"""Database observations retain complete rows and multiplicities on SQLite."""
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

from tests import reasoning_stage_characterization_cases as cases


def test_snapshot_keeps_duplicate_rows_empty_tables_and_cross_table_changes():
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE duplicates (payload TEXT, extra TEXT);
            CREATE TABLE unrelated (value INTEGER);
            CREATE TABLE empty_table (value INTEGER);
            INSERT INTO duplicates VALUES ('same', NULL), ('same', NULL), ('unique', '完整');
        """)

        @contextmanager
        def connect():
            yield db

        repo = SimpleNamespace(_runtime=SimpleNamespace(
            database=SimpleNamespace(connect=connect),
        ))
        backend = cases.Backend(
            name="sqlite", placeholder="?",
            tables=lambda _repo: ["duplicates", "unrelated", "empty_table"],
            invalidate_caches=lambda: None,
        )
        before = cases._dump_db(repo, backend)
        assert sorted(before["duplicates"].values()) == [1, 2]
        assert before["empty_table"] == {}
        db.execute("DELETE FROM duplicates WHERE rowid = 1")
        db.execute("UPDATE duplicates SET extra = 'changed' WHERE payload = 'same'")
        db.execute("INSERT INTO unrelated VALUES (7)")
        after = cases._dump_db(repo, backend)

    assert cases._db_diff(before, after) == {
        "duplicates": {"added": 1, "removed": 2},
        "unrelated": {"added": 1, "removed": 0},
    }
    assert sorted(before["duplicates"].values()) == [1, 2]

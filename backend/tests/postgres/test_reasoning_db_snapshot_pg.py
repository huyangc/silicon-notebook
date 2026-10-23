"""Observable equivalence of complete snapshots on real PostgreSQL connections."""
import dataclasses
import threading
import time
from contextlib import closing

import psycopg
import pytest

from app.repositories.read_budget import ReadBudgetExceeded, read_budget
from app.services.repository_factory import create_repository
from tests import reasoning_stage_characterization_cases as cases
from tests.postgres.reasoning_snapshot import read_table_rows
from tests.postgres.test_reasoning_stage_characterization_pg import BACKEND

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def snapshot_repo(postgres_settings):
    repo = create_repository(postgres_settings)
    try:
        yield repo
    finally:
        repo.close()


def test_full_snapshot_matches_serial_and_observes_duplicate_and_non_business_rows(snapshot_repo):
    database = snapshot_repo._runtime.database
    with database.connect() as db:
        db.execute("CREATE TABLE snapshot_duplicates (payload JSONB, extra TEXT)")
        db.execute("CREATE TABLE snapshot_unrelated (value INTEGER)")
        db.execute("CREATE TABLE snapshot_empty (value INTEGER)")
        db.execute("""INSERT INTO snapshot_duplicates VALUES
            ('{"完整": [1, null]}'::jsonb, NULL),
            ('{"完整": [1, null]}'::jsonb, NULL),
            ('{"other": true}'::jsonb, 'unique')""")
    serial_backend = dataclasses.replace(BACKEND, read_table_rows=None)
    before = cases._dump_db(snapshot_repo, BACKEND)
    assert before == cases._dump_db(snapshot_repo, serial_backend)
    assert {"silicon_schema_migrations", "sources", "snapshot_empty"} <= before.keys()
    assert sorted(before["snapshot_duplicates"].values()) == [1, 2]
    assert before["snapshot_empty"] == {}

    with database.connect() as db:
        db.execute("DELETE FROM snapshot_duplicates WHERE ctid = "
                   "(SELECT ctid FROM snapshot_duplicates WHERE extra IS NULL LIMIT 1)")
        db.execute("UPDATE snapshot_duplicates SET extra = 'changed' WHERE extra IS NULL")
        db.execute("INSERT INTO snapshot_unrelated VALUES (7)")
        db.execute("CREATE TABLE snapshot_added (value TEXT)")
        db.execute("INSERT INTO snapshot_added VALUES ('new')")
        # An uncommitted row must be visible: batching uses this exact connection.
        with closing(read_table_rows(db, ["snapshot_unrelated"])) as tables:
            assert dict(tables)["snapshot_unrelated"] == [{"value": 7}]

    after = cases._dump_db(snapshot_repo, BACKEND)
    assert after == cases._dump_db(snapshot_repo, serial_backend)
    assert cases._db_diff(before, after) == {
        "snapshot_duplicates": {"added": 1, "removed": 2},
        "snapshot_unrelated": {"added": 1, "removed": 0},
        "snapshot_added": {"added": 1, "removed": 0},
    }


def test_failed_snapshot_propagates_and_rolls_back_the_connection(snapshot_repo):
    database = snapshot_repo._runtime.database
    cursors = []

    class ObservedConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, query):
            cursor = self.connection.execute(query)
            cursors.append(cursor)
            return cursor

    with pytest.raises(psycopg.errors.UndefinedTable):
        with database.connect() as db:
            with closing(read_table_rows(
                ObservedConnection(db), ["sources", "snapshot_missing_table"],
            )) as tables:
                dict(tables)
    assert cursors and all(cursor.closed for cursor in cursors)
    with database.connect() as db:
        assert db.execute("SELECT 7 AS value").fetchone()["value"] == 7


def test_snapshot_keeps_budget_checks_and_connection_execute_proxies(snapshot_repo):
    database = snapshot_repo._runtime.database
    with read_budget(time.monotonic() + 30):
        assert cases._dump_db(snapshot_repo, BACKEND) == cases._dump_db(
            snapshot_repo, dataclasses.replace(BACKEND, read_table_rows=None),
        )

    cancel = threading.Event()

    class CancelAfterRead:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, query):
            result = self.connection.execute(query)
            cancel.set()
            return result

    with pytest.raises(ReadBudgetExceeded):
        with read_budget(time.monotonic() + 30, cancel):
            with database.connect() as db:
                with closing(read_table_rows(CancelAfterRead(db), ["sources", "notebooks"])) as tables:
                    dict(tables)

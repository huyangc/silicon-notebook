from __future__ import annotations

import pytest

from app.core.config import Settings
from app.repositories.sqlite import migrations as sqlite_migrations
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/knowhow.db",
            storage_dir=str(tmp_path / "storage"),
        )
    )


def test_schema_version_is_27():
    assert sqlite_migrations.SCHEMA_VERSION == 27


def _columns(repo, table: str) -> dict[str, str]:
    with repo._runtime.database.connect() as db:
        return {
            row["name"]: row["type"]
            for row in db.execute(f"PRAGMA table_info({table})").fetchall()
        }


def test_knowhow_changes_table_shape(repo):
    columns = _columns(repo, "knowhow_changes")
    assert columns == {
        "id": "TEXT",
        "table_id": "TEXT",
        "seq": "INTEGER",
        "kind": "TEXT",
        "actor": "TEXT",
        "origin": "TEXT",
        "payload_json": "TEXT",
        "fingerprint": "TEXT",
        "note": "TEXT",
        "created_at": "TEXT",
    }


def test_knowhow_milestones_table_shape(repo):
    columns = _columns(repo, "knowhow_milestones")
    assert columns == {
        "id": "TEXT",
        "table_id": "TEXT",
        "seq": "INTEGER",
        "name": "TEXT",
        "note": "TEXT",
        "created_by": "TEXT",
        "created_at": "TEXT",
    }


def test_indexes_exist(repo):
    with repo._runtime.database.connect() as db:
        names = {
            row["name"]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    assert "idx_knowhow_changes_table" in names
    assert "idx_knowhow_milestones_table" in names


def test_changes_seq_is_unique_per_table(repo):
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at)"
            " VALUES ('nb','n','now','now')"
        )
        db.execute(
            "INSERT INTO knowhow_tables (id, notebook_id, title, description,"
            " created_at, updated_at) VALUES ('t1','nb','x','','now','now')"
        )
        db.execute(
            "INSERT INTO knowhow_changes (id, table_id, seq, kind, payload_json,"
            " fingerprint, created_at) VALUES ('c1','t1',1,'cell_update','{}','f','now')"
        )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        with repo._runtime.database.write() as db:
            db.execute(
                "INSERT INTO knowhow_changes (id, table_id, seq, kind, payload_json,"
                " fingerprint, created_at) VALUES ('c2','t1',1,'cell_update','{}','f','now')"
            )


def test_changes_cascade_with_table(repo):
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at)"
            " VALUES ('nb','n','now','now')"
        )
        db.execute(
            "INSERT INTO knowhow_tables (id, notebook_id, title, description,"
            " created_at, updated_at) VALUES ('t2','nb','x','','now','now')"
        )
        db.execute(
            "INSERT INTO knowhow_changes (id, table_id, seq, kind, payload_json,"
            " fingerprint, created_at) VALUES ('c3','t2',1,'cell_update','{}','f','now')"
        )
        db.execute("DELETE FROM knowhow_tables WHERE id='t2'")
        left = db.execute(
            "SELECT COUNT(*) AS n FROM knowhow_changes WHERE table_id='t2'"
        ).fetchone()["n"]
    assert left == 0

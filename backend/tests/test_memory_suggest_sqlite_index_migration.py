"""Exact-time index upgrades and raw offline connections share one function."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest

from app.core.config import Settings
from app.domain.instant_order import utc_microsecond_key
from app.repositories.sqlite import migrations
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator
from app.repositories.sqlite_functions import register_sqlite_functions
from tests.test_memory_suggest_corrected import env as env


@pytest.mark.real_sqlite_migration
def test_v105_history_upgrades_through_a_plain_connection_without_retiming(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    settings = Settings(database_url=f"sqlite:///{path}", _env_file=None)
    database = SqliteDatabase(settings, root_dir=tmp_path)
    try:
        with monkeypatch.context() as old:
            old.setattr(migrations, "SCHEMA_VERSION", 105)
            SqliteMigrator(database, settings).migrate()
        with database.write() as db:
            db.execute("INSERT INTO notebooks(id,name,created_at,updated_at) VALUES ('nb','old','now','now')")
            stamps = ["2026-10-01T09:00:00.000200+01:00", "2026-10-01T08:00:00.000201+00:00", "", "invalid"]
            db.executemany(
                "INSERT INTO ask_jobs(id,notebook_id,conversation_id,mode,question,status,created_at,updated_at) "
                "VALUES (?,'nb','conv','chunk','q','done',?,'now')",
                [(str(index), stamp) for index, stamp in enumerate(stamps)],
            )
        database.close_local()

        class PlainDatabase:
            @contextmanager
            def connect(self):
                connection = sqlite3.connect(path)
                try:
                    with connection:
                        yield connection
                finally:
                    connection.close()

        # This deliberately supplies no runtime SqliteDatabase initializer:
        # the migration itself must register schema functions before CREATE.
        assert SqliteMigrator(PlainDatabase(), settings).migrate() == list(range(106, migrations.SCHEMA_VERSION + 1))
        with sqlite3.connect(path) as db:
            register_sqlite_functions(db)
            assert db.execute("SELECT created_at FROM ask_jobs ORDER BY id").fetchall() == [(stamp,) for stamp in stamps]
            assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert db.execute(
                "SELECT id FROM ask_jobs WHERE conversation_id='conv' "
                "AND sn_utc_us(created_at) > sn_utc_us(?) ORDER BY sn_utc_us(created_at),id",
                (stamps[0],),
            ).fetchall() == [("1",)]
    finally:
        database.close_local()


def test_registered_readonly_backup_and_writable_maintenance_keep_expression_indexes(env, tmp_path):
    env.seed()
    source = env.runtime.database
    with source.connect() as db:
        path = db.execute("PRAGMA database_list").fetchone()["file"]
    backup_path = tmp_path / "backup.db"
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as reader:
        register_sqlite_functions(reader)
        assert reader.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        with sqlite3.connect(backup_path) as backup:
            register_sqlite_functions(backup)
            reader.backup(backup)
    with sqlite3.connect(backup_path) as maintenance:
        register_sqlite_functions(maintenance)
        maintenance.execute("VACUUM")
        maintenance.execute(
            "INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,question,status,"
            "created_at,updated_at,submitted_via,output,report_id) "
            "VALUES ('offline',?,'conv','u','chunk','q','done',?,'now','web','answer','')",
            ("nb-sj", "2026-10-01T09:00:00.000201+01:00"),
        )
        assert maintenance.execute("SELECT sn_utc_us(created_at) FROM ask_jobs WHERE id='offline'").fetchone()[0] == utc_microsecond_key("2026-10-01T08:00:00.000201+00:00")
        assert maintenance.execute("PRAGMA integrity_check").fetchall() == [("ok",)]

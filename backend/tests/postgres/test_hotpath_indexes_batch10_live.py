"""The G20 online builder's catalog echo and 0087 collision handling."""
from __future__ import annotations

import psycopg
import pytest

from app.repositories.postgres.hotpath_indexes import (
    HOTPATH_INDEX_SPECS,
    inspect_hotpath_indexes,
    install_hotpath_indexes,
)
from app.repositories.postgres.migrator import PostgresMigrator

pytestmark = [pytest.mark.postgres_integration, pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch10")]
NAME = "idx_ask_jobs_memory_suggest_sample"


def _state(database):
    url = database.settings.database_url
    with psycopg.connect(url, autocommit=True) as connection:
        schema = connection.execute("SELECT current_schema()").fetchone()[0]
    return next(row["state"] for row in inspect_hotpath_indexes(url, schema=schema)["indexes"] if row["name"] == NAME)


def test_migration_accepts_online_builder_index_with_the_catalog_shape(postgres_database):
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=86) == 86
    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as connection:
        schema = connection.execute("SELECT current_schema()").fetchone()[0]
        spec = next(spec for spec in HOTPATH_INDEX_SPECS if spec.name == NAME)
        connection.execute(spec.ddl(schema, concurrently=True))
        actual = connection.execute(
            "SELECT pg_get_expr(i.indpred,i.indrelid,true) FROM pg_index i "
            "JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND c.relname=%s", (schema,NAME),
        ).fetchone()[0]
        assert actual == spec.predicate_shape
    assert _state(postgres_database) == "存在"
    assert migrator.migrate() == 88
    assert _state(postgres_database) == "存在"


@pytest.mark.parametrize("version", [53, 54, 70, 80])
def test_online_builder_preserves_historical_and_recorded_ask_fields(postgres_database, version):
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=version) == version
    now = "2026-11-02T00:00:00+00:00"
    expected = {"submitted_via": "", "output": "answer", "report_id": ""}
    recorded = {"submitted_via": "mcp", "output": "evidence", "report_id": "report-recorded"}
    with postgres_database.write() as db:
        schema = db.execute("SELECT current_schema() AS name").fetchone()["name"]
        columns = {row["column_name"] for row in db.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name='ask_jobs'",
            (schema,),
        ).fetchall()}
        db.execute("INSERT INTO notebooks(id,name,created_at,updated_at) VALUES ('nb-old','old',%s,%s)", (now, now))
        db.execute(
            "INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,question,status,created_at,updated_at) "
            "VALUES ('job-old','nb-old','c','u','reasoning','q','done',%s,%s)", (now, now),
        )
        for column, value in recorded.items():
            if column in columns:
                db.execute(psycopg.sql.SQL("UPDATE ask_jobs SET {}=%s WHERE id='job-old'").format(psycopg.sql.Identifier(column)), (value,))
                expected[column] = value

    state = install_hotpath_indexes(postgres_database.settings.database_url, schema=schema)
    assert all(row["state"] == "存在" for row in state["indexes"]), state
    assert migrator.migrate(target_version=version) == version  # Builder does not advance the ledger.
    with postgres_database.connect() as db:
        actual = db.execute("SELECT submitted_via,output,report_id FROM ask_jobs WHERE id='job-old'").fetchone()
        assert dict(actual) == expected
        collations = db.execute(
            "SELECT column_name,collation_name,is_nullable FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name='ask_jobs' AND column_name IN ('submitted_via','output','report_id')",
            (schema,),
        ).fetchall()
        assert len(collations) == len(expected)
        assert all(row["collation_name"] == "C" and row["is_nullable"] == "NO" for row in collations)
    assert install_hotpath_indexes(postgres_database.settings.database_url, schema=schema) == state
    assert migrator.migrate() == 88
    with postgres_database.connect() as db:
        assert dict(db.execute("SELECT submitted_via,output,report_id FROM ask_jobs WHERE id='job-old'").fetchone()) == expected


@pytest.mark.parametrize("ddl", [
    f"CREATE INDEX {NAME} ON ask_jobs(created_by,notebook_id,created_at,id)",
    f"CREATE UNIQUE INDEX {NAME} ON ask_jobs(created_by,notebook_id,created_at,id) WHERE status='done' AND submitted_via='web' AND output='answer' AND report_id=''",
    f"CREATE INDEX {NAME} ON ask_jobs(created_by,notebook_id,created_at,id) WHERE status='DONE' AND submitted_via='web' AND output='answer' AND report_id=''",
    f"CREATE INDEX {NAME} ON ask_jobs(created_by,notebook_id,created_at,id) WHERE status='done ' AND submitted_via='web' AND output='answer' AND report_id=''",
    f"CREATE INDEX {NAME} ON ask_jobs(created_by,notebook_id,created_at,id) WHERE status='done::text' AND submitted_via='web' AND output='answer' AND report_id=''",
    f"CREATE INDEX {NAME} ON ask_jobs(created_by,notebook_id,created_at,id COLLATE \"default\") WHERE status='done' AND submitted_via='web' AND output='answer' AND report_id=''",
])
def test_builder_and_migration_reject_a_same_named_wrong_definition(postgres_database, ddl):
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=86) == 86
    with postgres_database.write() as db:
        db.execute(ddl)
    assert _state(postgres_database) != "存在"
    with pytest.raises(psycopg.errors.RaiseException, match="does not match the expected definition"):
        migrator.migrate()
    assert migrator.migrate(target_version=86) == 86
    with postgres_database.write() as db:
        db.execute(f"DROP INDEX {NAME}")
    assert migrator.migrate() == 88


def test_migration_rejects_an_invalid_concurrent_build_residue(postgres_database):
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(target_version=86) == 86
    now = "2026-10-29T00:00:00+00:00"
    with postgres_database.write() as db:
        db.execute("INSERT INTO notebooks(id,name,created_at,updated_at) VALUES ('nb-invalid','n',%s,%s)", (now,now))
        for key in ("one","two"):
            db.execute("INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,question,status,created_at,updated_at) VALUES (%s,'nb-invalid','c','u','reasoning','q','done',%s,%s)", (key,now,now))
    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(f"CREATE UNIQUE INDEX CONCURRENTLY {NAME} ON ask_jobs(created_by)")
    with pytest.raises(psycopg.errors.RaiseException, match="INVALID"):
        migrator.migrate()
    assert migrator.migrate(target_version=86) == 86

"""PR-B·B1 twin: PostgreSQL and SQLite return the SAME page sequence and the
SAME count for one fixture (tests/kg_ceiling_fixture.py) under every ceiling
shape — none, include, ``()``, blank-only, outside-only (every evidence of
some objects excluded), duplicates, a 40k-id ceiling — both with a certified
reverse index and with the uncertified authoritative-JSON branch, and both
agree with the fixture's independent reference model.

The fixture holds the decisive shapes: an object with evidence in two sources
(one inside, one outside) is listed; an object with no evidence is listed
without a ceiling and never under one; an object OWNED by a ceiling source but
evidenced only outside it is not listed (support is evidence, not owner).
"""
from __future__ import annotations

import threading

import pytest

from app.core.config import Settings
from app.repositories.postgres.knowledge_store import KnowledgeStore as PgKnowledgeStore
from app.repositories.postgres.migrator import PostgresMigrator
from app.services.repository_runtime import RepositoryCompatibilitySeams
from app.services.sqlite_repository import SQLiteRepository
from tests import kg_ceiling_fixture as fx

pytestmark = pytest.mark.postgres_integration


def _seams() -> RepositoryCompatibilitySeams:
    lock = threading.Lock()
    counter: dict[str, int] = {}

    def new_id(prefix: str) -> str:
        with lock:
            counter[prefix] = counter.get(prefix, 0) + 1
            return f"{prefix}-{counter[prefix]:04d}"

    return RepositoryCompatibilitySeams(
        new_id=new_id, now=lambda: "2026-09-01T00:00:00+00:00",
        copy_chunk_size=lambda: 100, remap_json_ids=lambda value, _m: value,
        in_chunk_size=lambda: 100,
    )


def _walk(store, connection, allowed, limit):
    return fx.walk_pages(
        lambda after, n: store.knowledge_object_page_rows(
            connection, fx.NOTEBOOK_ID, fx.OBJECT_TYPE, after, n,
            allowed_source_ids=allowed,
        ),
        limit,
    )


def _count(store, connection, supported, excluding):
    return store.count_knowledge(
        connection, fx.NOTEBOOK_ID, fx.OBJECT_TYPE, fx.USABLE,
        supported_by_source_ids=supported, excluding_owner_source_ids=excluding,
    )


@pytest.mark.parametrize("backfilled", [True, False],
                         ids=["reverse_index", "authoritative_json"])
def test_postgres_and_sqlite_agree_on_pages_and_counts(
    postgres_database, tmp_path, monkeypatch, backfilled,
):
    assert PostgresMigrator(postgres_database).migrate() == 65
    with postgres_database.write() as connection:
        fx.seed(lambda sql, params: connection.execute(sql, params), "%s",
                backfilled=backfilled)
    pg = PgKnowledgeStore(postgres_database, _seams())

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'twin.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    sqlite_repo = SQLiteRepository(Settings())
    with sqlite_repo._write() as connection:
        fx.seed(lambda sql, params: connection.execute(sql, params), "?",
                backfilled=backfilled)
    lite = sqlite_repo._runtime.knowledge

    with postgres_database.connect() as pg_db, sqlite_repo._connect() as lite_db:
        for name, allowed in fx.CEILINGS.items():
            expected = fx.reference_page_ids(allowed)
            for limit in (1, 2, 3, 50):
                pg_pages = _walk(pg, pg_db, allowed, limit)
                lite_pages = _walk(lite, lite_db, allowed, limit)
                assert pg_pages == lite_pages, (name, limit)
                assert [i for page in pg_pages for i in page] == expected, (name, limit)
                assert all(len(page) == limit for page in pg_pages[:-1]), (name, limit)
            for exclusion, excluding in fx.EXCLUSIONS.items():
                pg_count = _count(pg, pg_db, allowed, excluding)
                assert pg_count == _count(lite, lite_db, allowed, excluding), (
                    name, exclusion)
                assert pg_count == fx.reference_count(allowed, excluding), (
                    name, exclusion)


def test_postgres_no_ceiling_statements_are_byte_identical(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 65
    with postgres_database.write() as connection:
        fx.seed(lambda sql, params: connection.execute(sql, params), "%s",
                backfilled=True)
    store = PgKnowledgeStore(postgres_database, _seams())
    with postgres_database.connect() as connection:
        recorder = fx.RecordingConnection(connection)
        store.knowledge_object_page_rows(recorder, fx.NOTEBOOK_ID, fx.OBJECT_TYPE, None, 5)
        store.knowledge_object_page_rows(
            recorder, fx.NOTEBOOK_ID, fx.OBJECT_TYPE,
            ("2026-09-01T00:00:01+00:00", "ko-01"), 5, allowed_source_ids=None,
        )
        store.count_knowledge(recorder, fx.NOTEBOOK_ID, fx.OBJECT_TYPE, fx.USABLE)
        store.count_knowledge(
            recorder, fx.NOTEBOOK_ID, fx.OBJECT_TYPE, fx.USABLE,
            supported_by_source_ids=None, excluding_owner_source_ids=(),
        )
        count_call = (
            "SELECT COUNT(*) AS count FROM knowledge_objects "
            "WHERE notebook_id = %s AND object_type = %s AND status IN (%s,%s)",
            (fx.NOTEBOOK_ID, fx.OBJECT_TYPE, *fx.USABLE),
        )
        assert recorder.calls == [
            ("SELECT id,object_type,source_id,payload,evidence,status,created_at "
             "FROM knowledge_objects WHERE notebook_id=%s AND object_type=%s "
             "ORDER BY created_at,id LIMIT %s",
             (fx.NOTEBOOK_ID, fx.OBJECT_TYPE, 5)),
            ("SELECT id,object_type,source_id,payload,evidence,status,created_at "
             "FROM knowledge_objects WHERE notebook_id=%s AND object_type=%s "
             "AND (created_at,id) > (%s,%s) ORDER BY created_at,id LIMIT %s",
             (fx.NOTEBOOK_ID, fx.OBJECT_TYPE, "2026-09-01T00:00:01+00:00", "ko-01", 5)),
            count_call,
            count_call,
        ]
        # ...and executed exactly as before (psycopg's default preparation).
        assert recorder.options == [{}, {}, {}, {}]
        deny = fx.RecordingConnection(connection)
        assert store.knowledge_object_page_rows(
            deny, fx.NOTEBOOK_ID, fx.OBJECT_TYPE, None, 5, allowed_source_ids=("",)
        ) == []
        assert store.count_knowledge(
            deny, fx.NOTEBOOK_ID, fx.OBJECT_TYPE, fx.USABLE,
            supported_by_source_ids=(),
        ) == 0
        assert deny.calls == []

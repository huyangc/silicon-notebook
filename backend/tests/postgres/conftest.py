from __future__ import annotations

import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest

from app.core.config import Settings
from app.core.database_url import database_identity


_DEDICATED_DATABASE = re.compile(
    r"^silicon_notebook_(?:test(?:_[a-z0-9_]+)?|[a-z0-9_]+_test)$"
)
_TEST_SCHEMA = re.compile(r"^sn_t4_[0-9a-f]{32}$")


@dataclass(frozen=True)
class ScopedPostgres:
    base_url: str
    url: str
    schema: str


def _require_dedicated_test_database(url: str, actual_database: str | None = None) -> str:
    expected = database_identity(url).database
    if not _DEDICATED_DATABASE.fullmatch(expected):
        raise RuntimeError(
            "TEST_POSTGRES_URL must name a dedicated silicon_notebook_*_test database"
        )
    if actual_database is not None and actual_database != expected:
        raise RuntimeError("TEST_POSTGRES_URL database identity does not match the server")
    return expected


def _url_with_search_path(url: str, schema: str) -> str:
    if not _TEST_SCHEMA.fullmatch(schema):
        raise RuntimeError("refusing to use an unvalidated PostgreSQL test schema")
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(key.lower() == "options" for key, _value in query):
        raise RuntimeError(
            "TEST_POSTGRES_URL must not set libpq options; the fixture owns search_path"
        )
    query.append(("options", f"-csearch_path={schema}"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


@pytest.fixture(autouse=True)
def _require_postgres_url_for_integration(request) -> None:
    if (
        request.node.get_closest_marker("postgres_integration")
        and not os.environ.get("TEST_POSTGRES_URL")
    ):
        pytest.skip("TEST_POSTGRES_URL is not configured")


@pytest.fixture
def postgres_scope() -> ScopedPostgres:
    base_url = os.environ.get("TEST_POSTGRES_URL")
    if not base_url:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    with _isolated_postgres_scope(base_url) as scoped:
        yield scoped


@contextmanager
def _isolated_postgres_scope(base_url: str):
    _require_dedicated_test_database(base_url)

    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    schema = f"sn_t4_{uuid.uuid4().hex}"
    assert _TEST_SCHEMA.fullmatch(schema)
    scoped_url = _url_with_search_path(base_url, schema)
    with psycopg.connect(base_url, autocommit=True, row_factory=dict_row) as conn:
        actual = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        _require_dedicated_test_database(base_url, str(actual))
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    try:
        scoped = ScopedPostgres(
            base_url=base_url,
            url=scoped_url,
            schema=schema,
        )
        with psycopg.connect(scoped.url, row_factory=dict_row) as conn:
            identity = conn.execute(
                "SELECT current_database() AS database, current_schema() AS schema"
            ).fetchone()
            _require_dedicated_test_database(base_url, str(identity["database"]))
            if identity["schema"] != schema:
                raise RuntimeError(
                    "scoped TEST_POSTGRES_URL did not select its isolated schema"
                )
        yield scoped
    finally:
        if not _TEST_SCHEMA.fullmatch(schema):
            raise RuntimeError("refusing to drop an unvalidated PostgreSQL test schema")
        with psycopg.connect(base_url, autocommit=True) as conn:
            actual = conn.execute("SELECT current_database()").fetchone()[0]
            _require_dedicated_test_database(base_url, str(actual))
            conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.fixture
def postgres_non_c_scope() -> ScopedPostgres:
    base_url = os.environ.get("TEST_POSTGRES_NON_C_URL")
    if not base_url:
        pytest.skip("TEST_POSTGRES_NON_C_URL is not configured")
    with _isolated_postgres_scope(base_url) as scoped:
        yield scoped


@pytest.fixture
def postgres_non_utf_scope() -> ScopedPostgres:
    base_url = os.environ.get("TEST_POSTGRES_NON_UTF_URL")
    if not base_url:
        pytest.skip("TEST_POSTGRES_NON_UTF_URL is not configured")
    with _isolated_postgres_scope(base_url) as scoped:
        yield scoped


@pytest.fixture
def postgres_settings(postgres_scope: ScopedPostgres) -> Settings:
    return Settings(
        database_url=postgres_scope.url,
        postgres_pool_min_size=1,
        postgres_pool_max_size=2,
        postgres_pool_acquire_timeout_seconds=1,
        postgres_statement_timeout_seconds=2,
        postgres_lock_timeout_seconds=1,
    )


@pytest.fixture
def postgres_database(postgres_settings: Settings):
    from app.repositories.postgres.database import PostgresDatabase

    database = PostgresDatabase(
        postgres_settings,
        Path(__file__).resolve().parents[3],
    )
    try:
        yield database
    finally:
        database.close()


def _database_for_scope(scope: ScopedPostgres):
    from app.repositories.postgres.database import PostgresDatabase

    settings = Settings(
        database_url=scope.url,
        postgres_pool_min_size=1,
        postgres_pool_max_size=2,
        postgres_pool_acquire_timeout_seconds=1,
        postgres_statement_timeout_seconds=10,
        postgres_lock_timeout_seconds=2,
    )
    return PostgresDatabase(settings, Path(__file__).resolve().parents[3])


@pytest.fixture
def postgres_non_c_database(postgres_non_c_scope: ScopedPostgres):
    database = _database_for_scope(postgres_non_c_scope)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def postgres_non_utf_database(postgres_non_utf_scope: ScopedPostgres):
    database = _database_for_scope(postgres_non_utf_scope)
    try:
        yield database
    finally:
        database.close()

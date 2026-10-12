from __future__ import annotations

import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest

from app.core.config import Settings
from app.core.database_url import database_identity
from tests.postgres.targets import (
    PARALLEL_WORKERS,
    TARGETS_ENV,
    configured_target_groups,
    worker_environment,
)


_DEDICATED_DATABASE = re.compile(
    r"^silicon_notebook_(?:test(?:_[a-z0-9_]+)?|[a-z0-9_]+_test)$"
)
_TEST_SCHEMA = re.compile(r"^sn_test_[0-9a-f]{32}$")
_MALFORMED_PERCENT_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")
_SAFE_CONNECTION_QUERY_KEYS = {"sslmode"}
_SAFE_SSLMODES = {
    "disable",
    "allow",
    "prefer",
    "require",
    "verify-ca",
    "verify-full",
}


def pytest_configure(config) -> None:
    """Each xdist worker owns three databases; ordinary serial runs are unchanged."""
    try:
        groups = configured_target_groups(os.environ)
        if groups is None:
            return
        for group in groups:
            for url in group.values():
                _require_dedicated_test_database(url)
                if urlsplit(url).password is not None:
                    raise RuntimeError("PostgreSQL worker URL contains a password")
        worker = getattr(config, "workerinput", None)
        if worker is None:
            if (
                config.getoption("numprocesses") != PARALLEL_WORKERS
                or config.getoption("dist") != "loadgroup"
                or str(config.getoption("maxworkerrestart")) != "0"
            ):
                raise RuntimeError(
                    "PostgreSQL target groups require four fixed loadgroup workers"
                )
            return
        os.environ.update(worker_environment(groups, worker["workerid"]))
        # Nested subprocess gates deliberately inherit only this worker's
        # serial targets, never another worker's targets or the controller map.
        os.environ.pop(TARGETS_ENV)
    except (RuntimeError, ValueError, KeyError):
        raise pytest.UsageError("invalid PostgreSQL worker target configuration") from None


@dataclass(frozen=True)
class ScopedPostgres:
    base_url: str
    url: str
    schema: str


@dataclass(frozen=True)
class DatabaseCatalog:
    database: str
    encoding: str
    provider: str | None
    provider_locale: str | None
    datcollate: str | None
    datctype: str | None


def _safe_ascii_text(
    value: object,
    label: str,
    *,
    allow_none: bool = False,
) -> str | None:
    """Normalize PostgreSQL identity/catalog text without repr-coercing bytes."""
    if value is None and allow_none:
        return None
    if isinstance(value, bytes):
        try:
            return value.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"PostgreSQL {label} must be ASCII text") from exc
    if isinstance(value, str):
        return value
    raise RuntimeError(f"PostgreSQL {label} must be ASCII text")


def _validate_test_url_options(url: str) -> tuple[tuple[str, str], ...]:
    """Parse a test URL query strictly and return canonical safe behavior only."""
    database_identity(url)
    parts = urlsplit(url)
    if _MALFORMED_PERCENT_ESCAPE.search(parts.query):
        raise RuntimeError("PostgreSQL integration URL query is malformed")
    try:
        query = parse_qsl(
            parts.query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=8,
        )
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError("PostgreSQL integration URL query is malformed") from exc

    canonical: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_key, value in query:
        key = raw_key.lower()
        if key in seen:
            raise RuntimeError("PostgreSQL integration URL query repeats a parameter")
        seen.add(key)
        if key == "options":
            raise RuntimeError(
                "PostgreSQL integration URLs must not set libpq options; "
                "the fixture owns search_path"
            )
        if key not in _SAFE_CONNECTION_QUERY_KEYS:
            raise RuntimeError(
                "PostgreSQL integration URL query parameter is not allowed"
            )
        if key == "sslmode" and value.lower() not in _SAFE_SSLMODES:
            raise RuntimeError("PostgreSQL integration sslmode is invalid")
        canonical.append((key, value.lower()))
    return tuple(canonical)


def _database_catalog(row: Mapping[str, Any]) -> DatabaseCatalog:
    raw_catalog = row.get("catalog")
    if not isinstance(raw_catalog, Mapping):
        raise RuntimeError("PostgreSQL database catalog response is invalid")

    def optional(key: str) -> str | None:
        return _safe_ascii_text(raw_catalog.get(key), key, allow_none=True)

    # PostgreSQL 16 names the ICU locale daticulocale; PostgreSQL 17 renamed it
    # to datlocale. Reading the full catalog row as jsonb lets one query work on
    # both versions without referring to a column that does not exist.
    provider_locale = optional("datlocale") or optional("daticulocale")
    return DatabaseCatalog(
        database=_safe_ascii_text(row.get("database"), "database"),
        encoding=_safe_ascii_text(row.get("encoding"), "server encoding"),
        provider=optional("datlocprovider"),
        provider_locale=provider_locale,
        datcollate=optional("datcollate"),
        datctype=optional("datctype"),
    )


def _validate_database_catalog(
    catalog: DatabaseCatalog,
    *,
    expected: str,
) -> None:
    if expected in {"utf8", "non-c"} and catalog.encoding != "UTF8":
        raise RuntimeError("server_encoding must be UTF8")
    if expected == "non-utf" and catalog.encoding == "UTF8":
        raise RuntimeError("negative target must not be UTF8")
    if expected != "non-c":
        return

    c_locales = {"C", "POSIX"}
    if catalog.provider == "i":
        if not catalog.provider_locale or catalog.provider_locale.upper() in c_locales:
            raise RuntimeError("ICU provider locale must be non-C")
        return
    if catalog.provider == "c":
        if (
            not catalog.datcollate
            or not catalog.datctype
            or catalog.datcollate.upper() in c_locales
            or catalog.datctype.upper() in c_locales
        ):
            raise RuntimeError("libc database collation must be non-C")
        return
    raise RuntimeError("non-C target uses an unsupported locale provider")


def _require_dedicated_test_database(
    url: str, actual_database: object | None = None
) -> str:
    _validate_test_url_options(url)
    expected = database_identity(url).database
    if not _DEDICATED_DATABASE.fullmatch(expected):
        raise RuntimeError(
            "TEST_POSTGRES_URL must name a dedicated silicon_notebook_*_test database"
        )
    if actual_database is not None and (
        _safe_ascii_text(actual_database, "database") != expected
    ):
        raise RuntimeError(
            "TEST_POSTGRES_URL database identity does not match the server"
        )
    return expected


def _url_with_search_path(url: str, schema: str) -> str:
    if not _TEST_SCHEMA.fullmatch(schema):
        raise RuntimeError("refusing to use an unvalidated PostgreSQL test schema")
    safe_query = _validate_test_url_options(url)
    parts = urlsplit(url)
    query = list(safe_query)
    query.append(("options", f"-csearch_path={schema}"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


@pytest.fixture(autouse=True)
def _require_postgres_url_for_integration(request) -> None:
    if (
        request.node.get_closest_marker("postgres_integration")
        and not os.environ.get("TEST_POSTGRES_URL")
    ):
        pytest.skip("TEST_POSTGRES_URL is not configured")


@pytest.fixture(autouse=True)
def _reset_postgres_knowledge_counts_cache():
    """``knowledge_counts_cache`` 是进程级模块全局(``_MEMO``/``_PENDING``/
    ``_VISIBLE_PENDING``/``_CHUNKS``),不随每个测试的隔离 schema 一起清空——两个测试
    即使各自建了互不相干的 schema,只要都用了同一个 ``notebook_id``(本目录下的测试
    偏爱 ``nb-pending`` 这类好记的字面量),后一个测试就可能读到前一个测试留下的
    memo 快照,而它对应的 seq 在新 schema 里从未出现过,不会自然失效。这里在每个
    测试前清空一次,让直插 SQL(不经过会 bump ``kg_mutation_seq`` 的服务层)的测试
    总是从冷状态开始。"""
    from app.repositories.postgres import knowledge_counts_cache

    knowledge_counts_cache.invalidate()


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

    schema = f"sn_test_{uuid.uuid4().hex}"
    assert _TEST_SCHEMA.fullmatch(schema)
    scoped_url = _url_with_search_path(base_url, schema)
    with psycopg.connect(base_url, autocommit=True, row_factory=dict_row) as conn:
        actual = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        _require_dedicated_test_database(base_url, actual)
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
            _require_dedicated_test_database(base_url, identity["database"])
            if _safe_ascii_text(identity["schema"], "schema") != schema:
                raise RuntimeError(
                    "scoped TEST_POSTGRES_URL did not select its isolated schema"
                )
        yield scoped
    finally:
        if not _TEST_SCHEMA.fullmatch(schema):
            raise RuntimeError("refusing to drop an unvalidated PostgreSQL test schema")
        with psycopg.connect(base_url, autocommit=True) as conn:
            actual = conn.execute("SELECT current_database()").fetchone()[0]
            _require_dedicated_test_database(base_url, actual)
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
        # 全局语句超时刻意只给 2 秒;chunk 词法 deadline 的默认值已提到 3.0,
        # 不显式压回来会被 validate_chunk_fts_timeout_ceiling 拒绝(codex #727 R1)。
        postgres_chunk_fts_timeout_seconds=1.0,
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


@dataclass(frozen=True)
class BystanderSnapshot:
    """Which of the two horizon states a plan pin runs under.

    ``held`` means another connection to the SAME database holds an open
    REPEATABLE READ transaction with an assigned xid from before the test
    migrated and seeded, so every row the test writes is newer than the
    oldest snapshot and VACUUM cannot mark a single page all-visible. That is
    the condition the planner flips Index Only Scan plans under (parallel
    lanes sharing one server, a long transaction in production).
    """

    held: bool

    def assert_visibility_map_blocked(self, connection, *tables: str) -> None:
        """In the held state, prove the bystander really did its job: after
        the test's VACUUM no page of ``tables`` is all-visible. A no-op when
        nothing is held (other sessions on the server make "all pages
        visible" too weak a claim to pin)."""
        if not self.held:
            return
        for table in tables:
            row = connection.execute(
                "SELECT relpages, relallvisible FROM pg_class "
                "WHERE oid = to_regclass(%s)",
                (table,),
            ).fetchone()
            assert row is not None, table
            assert row["relpages"] > 0 and row["relallvisible"] == 0, (table, row)


@pytest.fixture(params=["no_bystander", "bystander_rr_snapshot"])
def bystander_snapshot(request, postgres_settings: Settings):
    """Run a plan pin twice: with no other transaction, and with a bystander
    REPEATABLE READ transaction (``SELECT txid_current()``) held on the same
    database from before the test body until after its EXPLAINs. A pin that
    depends on the visibility map goes red in the second state."""
    if request.param == "no_bystander":
        yield BystanderSnapshot(held=False)
        return
    import psycopg

    with psycopg.connect(postgres_settings.database_url, autocommit=True) as holder:
        holder.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        holder.execute("SELECT txid_current()").fetchone()
        try:
            yield BystanderSnapshot(held=True)
        finally:
            holder.execute("ROLLBACK")


def explain_with_sole_index(
    connection, statement: str, params: tuple, *, table: str, index: str
) -> str:
    """``EXPLAIN (COSTS OFF) statement`` with ``index`` the ONLY index left on
    ``table`` (seqscan/bitmapscan off), then ROLLBACK -- the other indexes,
    constraint-backed ones included, are dropped inside this transaction and
    come back with it.

    The planner builds ONE path per index, and it is an Index Only Scan
    exactly when the index covers every column the query reads from
    ``table`` (``check_index_only``) -- whatever the visibility map says. So
    "Index Only Scan using <index>" in the result pins the covering
    capability of ``index`` deterministically, in both bystander states: an
    index that loses a covered column (e.g. its ``INCLUDE``) makes it an
    Index Scan. What this cannot pin is the planner's cost choice among
    competing indexes; that choice depends on the visibility map and is not
    stable on a test bed.
    """
    from psycopg import sql as pg_sql

    # Dropping an index takes ACCESS EXCLUSIVE on ``table``; an autovacuum
    # worker on it yields only after deadlock_timeout, so outwait it.
    connection.execute("SET LOCAL lock_timeout = '30s'")
    connection.execute("SET LOCAL enable_seqscan=off")
    connection.execute("SET LOCAL enable_bitmapscan=off")
    indexes = connection.execute(
        "SELECT ic.relname AS index_name, con.conname AS constraint_name "
        "FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
        "LEFT JOIN pg_constraint con ON con.conindid = i.indexrelid "
        "AND con.conrelid = i.indrelid AND con.contype IN ('p', 'u', 'x') "
        "WHERE i.indrelid = to_regclass(%s)",
        (table,),
    ).fetchall()
    names = sorted(row["index_name"] for row in indexes)
    assert index in names, (table, index, names)
    try:
        for row in indexes:
            if row["index_name"] == index:
                continue
            if row["constraint_name"]:
                connection.execute(
                    pg_sql.SQL("ALTER TABLE {} DROP CONSTRAINT {} CASCADE").format(
                        pg_sql.Identifier(table),
                        pg_sql.Identifier(row["constraint_name"]),
                    )
                )
            else:
                connection.execute(
                    pg_sql.SQL("DROP INDEX {}").format(
                        pg_sql.Identifier(row["index_name"])
                    )
                )
        rows = connection.execute(f"EXPLAIN (COSTS OFF) {statement}", params).fetchall()
    finally:
        connection.rollback()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


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

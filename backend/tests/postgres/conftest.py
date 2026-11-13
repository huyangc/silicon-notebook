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
from tests.postgres.background_quiesce import quiesce_process_background_work
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
    """``knowledge_counts_cache`` 是进程级模块全局(``_MEMO``/
    ``_VISIBLE_PENDING``/``_CHUNKS``),不随每个测试的隔离 schema 一起清空——两个测试
    即使各自建了互不相干的 schema,只要都用了同一个 ``notebook_id``(本目录下的测试
    偏爱 ``nb-pending`` 这类好记的字面量),后一个测试就可能读到前一个测试留下的
    memo 快照,而它对应的 seq 在新 schema 里从未出现过,不会自然失效。这里在每个
    测试前清空一次,让直插 SQL(不经过会 bump ``kg_mutation_seq`` 的服务层)的测试
    总是从冷状态开始。

    ``search._KNN_INDEX_CACHE`` 按 (库名, 表 OID) 缓存索引是否存在。schema 现在按
    worker 复用,表 OID 跨测试不变,所以也在这里清空,免得上一个测试的「没有索引」
    结论被下一个建了索引的测试读到。"""
    from app.repositories.postgres import knowledge_counts_cache, search

    knowledge_counts_cache.invalidate()
    search.reset_knn_index_cache()


@pytest.fixture
def postgres_scope(request) -> ScopedPostgres:
    base_url = os.environ.get("TEST_POSTGRES_URL")
    if not base_url:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    if request.node.get_closest_marker(FRESH_SCHEMA_MARKER) is not None:
        with _isolated_postgres_scope(base_url) as scoped:
            yield scoped
        return
    with _reused_postgres_scope(base_url) as scoped:
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
        # never drop a schema under pooled work a test left running (40P01)
        quiesce_process_background_work()
        with psycopg.connect(base_url, autocommit=True) as conn:
            actual = conn.execute("SELECT current_database()").fetchone()[0]
            _require_dedicated_test_database(base_url, actual)
            conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


# ---------------------------------------------------------------------------
# Per-worker schema reuse.
#
# Migrating an empty schema (about 90 files) used to be most of every PG
# test's setup: locally ~95% of setup, in CI about half of the whole PG lane.
# A freshly migrated schema holds no rows except the migration ledger and has
# never advanced a sequence (checked when the baseline is captured), so instead
# of migrating and dropping a schema per test, each worker process keeps at
# most one migrated schema per database and resets it between tests:
#
# * every test still gets its own ``sn_test_<uuid>`` name (the pooled schema is
#   renamed), so URL-keyed process caches and stray connections from an earlier
#   test can never address it;
# * at teardown the schema is renamed to a parking name, then, inside one
#   transaction that first takes ACCESS EXCLUSIVE on every table (so a
#   statement the finished test still has in flight either finishes first or
#   makes the reset time out), it is reused only if it is still pristine: the
#   catalog fingerprint (tables, column order and definitions, column options,
#   defaults, indexes, constraints, triggers including foreign-key enablement,
#   rules, functions, types, policies, sequence parameters, ACLs, owners,
#   reloptions, replica identity, extended statistics) and the migration ledger
#   match the freshly migrated baseline, and the planner has no column
#   statistics for it (``pg_stats`` empty). Anything else -- DDL, GRANTs, a
#   rewritten ledger, ANALYZE-driven stats, a lock that blocks the reset -- falls
#   back to dropping it, exactly as before;
# * the reset truncates every table the test touched, except the ledger
#   (RESTART IDENTITY; truncation also gives the table a new relfilenode, so
#   relpages/reltuples/relallvisible return to their never-vacuumed values),
#   restarts every sequence, and for tables whose cumulative modification
#   counter could reach autovacuum's analyze threshold ANALYZEs the empty table
#   and truncates it again, so autovacuum does not ANALYZE it during the next
#   test.
#
# Tests that need an empty, unmigrated schema (migration tests that migrate to
# a target version, raw-DDL tests) opt out with the ``postgres_fresh_schema``
# marker and keep the old create-and-drop path.
# ---------------------------------------------------------------------------
FRESH_SCHEMA_MARKER = "postgres_fresh_schema"
_LEDGER_TABLE = "silicon_schema_migrations"
_RESET_LOCK_TIMEOUT = "3s"
# base_url -> parked schema name ready for the next test.
_POOLED_SCHEMAS: dict[str, str] = {}
# base_url -> catalog items and ledger of a freshly migrated schema on that database.
_BASELINE_FINGERPRINTS: dict[str, tuple[frozenset[str], str]] = {}
# Outcome of every reuse teardown in this process ("reused" or why it was
# dropped); reported in the PostgreSQL lane's terminal summary so a reuse rate
# that silently collapses (for example only in CI) is visible.
_REUSE_OUTCOMES: dict[str, int] = {}
_REUSE_OUTCOMES_KEY = "postgres_schema_reuse"
# For catalog_changed outcomes: the first differing catalog item (truncated), counted.
_REUSE_CATALOG_DIFFS: dict[str, int] = {}
_REUSE_DIFFS_KEY = "postgres_schema_reuse_catalog_diffs"

_SCHEMA_FINGERPRINT_SQL = """
WITH ns AS (SELECT oid, nspname FROM pg_namespace WHERE nspname = %(schema)s)
SELECT item FROM (
  SELECT 'n|' || coalesce(n.nspacl::text, '') || '|' || pg_get_userbyid(n.nspowner)
    FROM pg_namespace n JOIN ns USING (oid)
  UNION ALL
  SELECT 'c|' || c.relname || '|' || c.relkind::text || '|' || pg_get_userbyid(c.relowner)
         || '|' || coalesce(c.relacl::text, '') || '|' || coalesce(c.reloptions::text, '')
         || '|' || c.relrowsecurity || '|' || c.relforcerowsecurity || '|' || c.relpersistence::text
         || '|' || c.relnatts || '|' || c.relreplident::text
         || '|' || coalesce((SELECT t.reloptions::text FROM pg_class t WHERE t.oid = c.reltoastrelid), '')
    FROM pg_class c JOIN ns ON c.relnamespace = ns.oid
  UNION ALL
  SELECT 'a|' || c.relname || '|' || a.attnum || '|' || a.attname || '|'
         || format_type(a.atttypid, a.atttypmod)
         || '|' || a.attnotnull || '|' || a.attidentity::text || '|' || a.attgenerated::text
         || '|' || coalesce(a.attcollation::regcollation::text, '')
         || '|' || a.attstorage::text || '|' || coalesce(a.attstattarget::text, '')
         -- Options prefixed pg_ are internal and cannot be set by DDL; pg_attver
         -- (minor releases from Nov 2026, e.g. 16.16; CVE-2026-6460) carries a
         -- per-migration version that differs between otherwise identical schemas.
         || '|' || coalesce(array_to_string(ARRAY(
              SELECT o FROM unnest(a.attoptions) o WHERE left(o, 3) <> 'pg_' ORDER BY o
            ), ','), '')
         || '|' || coalesce(replace(pg_get_expr(d.adbin, d.adrelid), ns.nspname || '.', ''), '')
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN ns ON c.relnamespace = ns.oid
    LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
   WHERE a.attnum > 0
  UNION ALL
  SELECT 'i|' || replace(pg_get_indexdef(i.indexrelid), ns.nspname || '.', '')
    FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid JOIN ns ON c.relnamespace = ns.oid
  UNION ALL
  SELECT 'k|' || c.relname || '|' || k.conname || '|' || k.condeferrable || '|' || k.condeferred
         || '|' || k.convalidated || '|'
         || replace(pg_get_constraintdef(k.oid), ns.nspname || '.', '')
    FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN ns ON c.relnamespace = ns.oid
  UNION ALL
  SELECT 't|' || c.relname || '|' || t.tgname || '|' || t.tgenabled::text || '|'
         || replace(pg_get_triggerdef(t.oid), ns.nspname || '.', '')
    FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN ns ON c.relnamespace = ns.oid
   WHERE NOT t.tgisinternal
  UNION ALL
  -- Internal (foreign-key) trigger names embed OIDs that change on every
  -- migration, so only their per-table enablement is compared.
  SELECT 'T|' || c.relname || '|' || string_agg(t.tgenabled::text, '' ORDER BY t.tgenabled::text)
    FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN ns ON c.relnamespace = ns.oid
   WHERE t.tgisinternal
   GROUP BY c.relname
  UNION ALL
  SELECT 'r|' || c.relname || '|' || r.rulename || '|' || r.ev_enabled::text || '|'
         || replace(pg_get_ruledef(r.oid), ns.nspname || '.', '')
    FROM pg_rewrite r JOIN pg_class c ON c.oid = r.ev_class JOIN ns ON c.relnamespace = ns.oid
  UNION ALL
  SELECT 'p|' || p.proname || '|' || pg_get_function_identity_arguments(p.oid) || '|'
         || md5(p.prosrc) || '|' || coalesce(p.proconfig::text, '') || '|'
         || coalesce(p.proacl::text, '') || '|' || pg_get_userbyid(p.proowner)
    FROM pg_proc p JOIN ns ON p.pronamespace = ns.oid
  UNION ALL
  SELECT 'd|' || y.typname || '|' || y.typtype::text || '|'
         || coalesce(pg_get_userbyid(y.typowner), '') || '|' || coalesce(y.typacl::text, '')
    FROM pg_type y JOIN ns ON y.typnamespace = ns.oid
  UNION ALL
  SELECT 'f|' || f.cfgname FROM pg_ts_config f JOIN ns ON f.cfgnamespace = ns.oid
  UNION ALL
  SELECT 's|' || c.relname || '|' || q.seqtypid::regtype::text || '|' || q.seqstart || '|'
         || q.seqincrement || '|' || q.seqmin || '|' || q.seqmax || '|' || q.seqcache
         || '|' || q.seqcycle
    FROM pg_sequence q JOIN pg_class c ON c.oid = q.seqrelid JOIN ns ON c.relnamespace = ns.oid
  UNION ALL
  SELECT 'y|' || c.relname || '|' || y.polname || '|' || y.polcmd::text || '|'
         || coalesce(pg_get_expr(y.polqual, y.polrelid), '') || '|'
         || coalesce(pg_get_expr(y.polwithcheck, y.polrelid), '')
    FROM pg_policy y JOIN pg_class c ON c.oid = y.polrelid JOIN ns ON c.relnamespace = ns.oid
  UNION ALL
  SELECT 'x|' || replace(pg_get_statisticsobjdef(e.oid), ns.nspname || '.', '')
    FROM pg_statistic_ext e JOIN ns ON e.stxnamespace = ns.oid
) catalog(item)
"""
# Tables whose storage or counters show that something wrote, deleted or
# vacuumed them; a never-touched table has a zero-length heap and
# never-vacuumed counters.
_TOUCHED_TABLES_SQL = (
    "SELECT c.relname FROM pg_class c "
    "WHERE c.relnamespace = to_regnamespace(%s) AND c.relkind = 'r' "
    "AND c.relname <> %s "
    "AND (pg_relation_size(c.oid) > 0 OR c.reltuples <> -1 "
    "     OR c.relpages <> 0 OR c.relallvisible <> 0) "
    "ORDER BY c.relname"
)


def _new_test_schema() -> str:
    schema = f"sn_test_{uuid.uuid4().hex}"
    assert _TEST_SCHEMA.fullmatch(schema)
    return schema


def _migrate_scope(url: str) -> None:
    from app.repositories.postgres.database import PostgresDatabase
    from app.repositories.postgres.migrator import PostgresMigrator

    database = PostgresDatabase(_scope_settings(url), Path(__file__).resolve().parents[3])
    try:
        PostgresMigrator(database).migrate()
    finally:
        database.close()


def _schema_fingerprint(conn, schema: str) -> tuple[frozenset[str], str]:
    from psycopg import sql

    # Catalog text (type names, defaults, index and rule definitions) is printed
    # schema-qualified unless the schema is on search_path, and the schema is
    # renamed for every test: pin search_path so the fingerprint is name-free.
    with conn.transaction():
        conn.execute(sql.SQL("SET LOCAL search_path = {}").format(sql.Identifier(schema)))
        catalog = frozenset(
            row[0] for row in conn.execute(_SCHEMA_FINGERPRINT_SQL, {"schema": schema})
        )
        ledger = conn.execute(
            sql.SQL(
                "SELECT md5(coalesce(string_agg(version::text || ':' || checksum, ',' "
                "ORDER BY version), '')) FROM {}.{}"
            ).format(sql.Identifier(schema), sql.Identifier(_LEDGER_TABLE))
        ).fetchone()[0]
    return catalog, ledger


def _capture_baseline(conn, base_url: str, schema: str) -> None:
    """Record the freshly migrated fingerprint, refusing reuse it cannot honour.

    The reset empties every touched table and restarts every sequence. If a
    migration ever seeds rows or advances a sequence, that reset would erase
    the seed for every test after a worker's first one; fail loudly instead.
    """
    seeded = [row[0] for row in conn.execute(_TOUCHED_TABLES_SQL, (schema, _LEDGER_TABLE))]
    advanced = [
        row[0] for row in conn.execute(
            "SELECT sequencename FROM pg_sequences "
            "WHERE schemaname = %s AND last_value IS NOT NULL",
            (schema,),
        )
    ]
    if seeded or advanced:
        raise RuntimeError(
            "a freshly migrated schema is not empty (tables "
            f"{seeded}, sequences {advanced}); per-worker schema reuse in "
            "tests/postgres/conftest.py would erase that seed between tests -- "
            "teach the reset to restore it before relying on reuse"
        )
    _BASELINE_FINGERPRINTS[base_url] = _schema_fingerprint(conn, schema)


def _rename_schema(conn, old: str, new: str) -> None:
    from psycopg import sql

    if not (_TEST_SCHEMA.fullmatch(old) and _TEST_SCHEMA.fullmatch(new)):
        raise RuntimeError("refusing to rename an unvalidated PostgreSQL test schema")
    conn.execute(
        sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(sql.Identifier(old), sql.Identifier(new))
    )


def _drop_schema(conn, schema: str) -> None:
    from psycopg import sql

    if not _TEST_SCHEMA.fullmatch(schema):
        raise RuntimeError("refusing to drop an unvalidated PostgreSQL test schema")
    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def _reset_if_pristine(conn, base_url: str, schema: str) -> str:
    """Reset ``schema`` to its freshly migrated state.

    Returns ``"reused"``, or the reason the schema must be dropped instead.
    """
    from psycopg import sql

    baseline = _BASELINE_FINGERPRINTS.get(base_url)
    if baseline is None:
        return "no_baseline"
    with conn.transaction():
        conn.execute(sql.SQL("SET LOCAL lock_timeout = {}").format(sql.Literal(_RESET_LOCK_TIMEOUT)))
        all_tables = [
            row[0] for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s ORDER BY tablename",
                (schema,),
            )
        ]
        if all_tables:
            # Wait out (or time out on) anything the finished test still has in
            # flight, so nothing commits into the schema after the checks below.
            conn.execute(
                sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(
                    sql.SQL(", ").join(sql.Identifier(schema, name) for name in all_tables)
                )
            )
        current = _schema_fingerprint(conn, schema)
        if current != baseline:
            if current[1] != baseline[1]:
                return "ledger_changed"
            first = min(current[0] ^ baseline[0])
            marker = ("+" if first in current[0] else "-") + first[:120]
            _REUSE_CATALOG_DIFFS[marker] = _REUSE_CATALOG_DIFFS.get(marker, 0) + 1
            return "catalog_changed"
        # Ledger statistics are irrelevant to every plan the tests pin, and
        # autovacuum may produce them on a fresh schema too (migration inserts
        # its rows); any other table's statistics mean the planner state moved.
        if conn.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_stats WHERE schemaname = %s AND tablename <> %s)",
            (schema, _LEDGER_TABLE),
        ).fetchone()[0]:
            return "planner_statistics"
        touched = [row[0] for row in conn.execute(_TOUCHED_TABLES_SQL, (schema, _LEDGER_TABLE))]
        if touched:
            conn.execute(
                sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                    sql.SQL(", ").join(sql.Identifier(schema, name) for name in touched)
                )
            )
        for (sequence,) in conn.execute(
            "SELECT sequencename FROM pg_sequences WHERE schemaname = %s", (schema,)
        ).fetchall():
            conn.execute(
                sql.SQL("ALTER SEQUENCE {} RESTART").format(sql.Identifier(schema, sequence))
            )
    # Truncation keeps the cumulative modification counters. A table whose
    # counter could reach autovacuum's analyze threshold would be ANALYZEd in
    # the middle of the next test, which a fresh schema never does. ANALYZE of
    # the now-empty table zeroes its counter without writing pg_stats, and the
    # following truncation (its own transaction: PostgreSQL truncates in place a
    # table already truncated in the same one) puts back the never-analyzed
    # reltuples = -1 that ANALYZE replaced with 0. Half the threshold leaves room
    # for counters a closing backend has not flushed yet. Most tests write far
    # fewer rows, so this second pass is the exception.
    conn.execute("SELECT pg_stat_clear_snapshot()")
    busy = [
        row[0] for row in conn.execute(
            "SELECT relname FROM pg_stat_user_tables WHERE schemaname = %s AND relname <> %s "
            "AND n_mod_since_analyze * 2 >= current_setting('autovacuum_analyze_threshold')::bigint "
            "ORDER BY relname",
            (schema, _LEDGER_TABLE),
        )
    ]
    if busy:
        with conn.transaction():
            conn.execute(
                sql.SQL("SET LOCAL lock_timeout = {}").format(sql.Literal(_RESET_LOCK_TIMEOUT))
            )
            for name in busy:
                conn.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(schema, name)))
            conn.execute(
                sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                    sql.SQL(", ").join(sql.Identifier(schema, name) for name in busy)
                )
            )
    return "reused"


@contextmanager
def _reused_postgres_scope(base_url: str):
    _require_dedicated_test_database(base_url)

    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    schema = _new_test_schema()
    with psycopg.connect(base_url, autocommit=True) as conn:
        actual = conn.execute("SELECT current_database()").fetchone()[0]
        _require_dedicated_test_database(base_url, actual)
        parked = _POOLED_SCHEMAS.pop(base_url, None)
        if parked is not None:
            _rename_schema(conn, parked, schema)
        else:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = ScopedPostgres(
        base_url=base_url,
        url=_url_with_search_path(base_url, schema),
        schema=schema,
    )
    try:
        if parked is None:
            _migrate_scope(scoped.url)
            if base_url not in _BASELINE_FINGERPRINTS:
                with psycopg.connect(base_url, autocommit=True) as conn:
                    _capture_baseline(conn, base_url, schema)
        with psycopg.connect(scoped.url, row_factory=dict_row) as conn:
            identity = conn.execute(
                "SELECT current_database() AS database, current_schema() AS schema"
            ).fetchone()
            _require_dedicated_test_database(base_url, identity["database"])
            if _safe_ascii_text(identity["schema"], "schema") != schema:
                raise RuntimeError("scoped TEST_POSTGRES_URL did not select its isolated schema")
    except BaseException:
        # A failed migration or identity check must not leave a half-migrated
        # schema behind for every later test to add another one.
        with psycopg.connect(base_url, autocommit=True) as conn:
            _drop_schema(conn, schema)
        raise
    try:
        yield scoped
    finally:
        # never reset or drop a schema under pooled work a test left running (40P01)
        quiesce_process_background_work()
        parking = _new_test_schema()
        with psycopg.connect(base_url, autocommit=True) as conn:
            actual = conn.execute("SELECT current_database()").fetchone()[0]
            _require_dedicated_test_database(base_url, actual)
            try:
                # New statements from this test address ``schema`` by name and no
                # longer reach the renamed one; statements already running are
                # waited out by the reset's table locks.
                _rename_schema(conn, schema, parking)
                outcome = _reset_if_pristine(conn, base_url, parking)
            except psycopg.Error as exc:
                outcome = f"error:{type(exc).__name__}"
            _REUSE_OUTCOMES[outcome] = _REUSE_OUTCOMES.get(outcome, 0) + 1
            if outcome == "reused":
                _POOLED_SCHEMAS[base_url] = parking
            else:
                _drop_schema(conn, parking)
                _drop_schema(conn, schema)


def pytest_sessionfinish(session) -> None:
    """Drop this process's parked schemas; each xdist worker owns its own."""
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        workeroutput[_REUSE_OUTCOMES_KEY] = dict(_REUSE_OUTCOMES)
        workeroutput[_REUSE_DIFFS_KEY] = dict(_REUSE_CATALOG_DIFFS)
    if not _POOLED_SCHEMAS:
        return
    import psycopg

    for base_url, schema in list(_POOLED_SCHEMAS.items()):
        try:
            with psycopg.connect(base_url, autocommit=True) as conn:
                _drop_schema(conn, schema)
        except psycopg.Error:
            pass
        _POOLED_SCHEMAS.pop(base_url, None)


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


def _scope_settings(url: str) -> Settings:
    return Settings(
        database_url=url,
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
def postgres_settings(postgres_scope: ScopedPostgres) -> Settings:
    return _scope_settings(postgres_scope.url)


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


BYSTANDER_STATES = ("no_bystander", "bystander_rr_snapshot")


@pytest.fixture(params=BYSTANDER_STATES)
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


# Sessions of this database that hold an xid or a snapshot. ``blocking``: the
# snapshot is not newer than the table's newest row (``{table}``), so VACUUM
# cannot mark that row's page all-visible while it lives; a session that
# started after the seed committed is listed but never decides anything.
# ``maintenance``: it ends on its own -- an autovacuum worker, or any backend
# with an ANALYZE or VACUUM in progress. An autovacuum ANALYZE keeps its
# snapshot for its whole sample (PROC_IN_VACUUM only covers the VACUUM phase),
# so one that began before a seed committed blocks the seed's pages until it
# is done: tens of seconds on a big or bloated table of the same database (a
# lane database's catalogs), longer under load. ``age()`` reads one stable
# reference xid per transaction, so both ages are comparable.
_HORIZON_HOLDERS_SQL = (
    "WITH newest AS (SELECT min(age(xmin)) AS age FROM {table}) "
    "SELECT a.pid, a.backend_type, a.state, a.backend_xid::text AS xid, "
    "a.backend_xmin::text AS xmin, a.application_name, "
    "round(extract(epoch FROM clock_timestamp() - a.xact_start)::numeric, 1)::float "
    "AS xact_seconds, left(a.query, 120) AS query, "
    "(greatest(coalesce(age(a.backend_xid), -1), coalesce(age(a.backend_xmin), -1)) "
    " >= (SELECT age FROM newest)) AS blocking, "
    "(a.backend_type = 'autovacuum worker' "
    " OR a.pid IN (SELECT pid FROM pg_stat_progress_analyze "
    "              UNION SELECT pid FROM pg_stat_progress_vacuum)) AS maintenance "
    "FROM pg_stat_activity a "
    "WHERE a.datname = current_database() AND a.pid <> pg_backend_pid() "
    "AND (a.backend_xid IS NOT NULL OR a.backend_xmin IS NOT NULL)"
)
# Horizon holders pg_stat_activity does not show; any of them fails the wait.
_HIDDEN_HORIZON_SQL = (
    "SELECT 'prepared' AS kind, gid AS name, transaction::text AS xmin "
    "FROM pg_prepared_xacts WHERE database = current_database() "
    "UNION ALL SELECT 'slot', slot_name, coalesce(xmin, catalog_xmin)::text "
    "FROM pg_replication_slots WHERE xmin IS NOT NULL OR catalog_xmin IS NOT NULL"
)


def vacuum_until_all_visible(
    database_url: str,
    table: str,
    timeout: float = 10.0,
    maintenance_timeout: float = 180.0,
    on_maintenance_wait=None,
) -> None:
    """VACUUM ``table`` until every page is all-visible (``relallvisible ==
    relpages``), then return; never return with an incomplete map.

    For the pins whose plan choice moves with the visibility map: they assert
    it only in the ``no_bystander`` state and only against a full map. A
    short-lived snapshot on the same database (a pooled connection between
    statements) is outwaited within ``timeout`` seconds. Past that, the wait
    goes on -- up to ``maintenance_timeout`` seconds in all -- only while every
    session whose snapshot predates the table's newest row is maintenance that
    ends by itself (an autovacuum worker, an ANALYZE or VACUUM in progress): an
    autoanalyze that began before the seed committed holds its snapshot until
    its sample is done. A blocking ordinary session or a prepared transaction /
    replication slot past ``timeout``, or anything past ``maintenance_timeout``,
    is a real stray holder: the call fails and names the sessions.

    ``on_maintenance_wait(holders)``, when given, is called every time the
    helper takes the maintenance-wait branch (past ``timeout``, only
    maintenance blocking), with the sessions it judged -- the seam its own
    tests synchronize on instead of timing an ANALYZE.
    """
    import time

    import psycopg
    from psycopg import sql as pg_sql
    from psycopg.rows import dict_row

    started = time.monotonic()
    holders_sql = pg_sql.SQL(_HORIZON_HOLDERS_SQL).format(table=pg_sql.Identifier(table))
    with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as raw:
        while True:
            raw.execute(pg_sql.SQL("VACUUM {}").format(pg_sql.Identifier(table)))
            row = raw.execute(
                "SELECT relpages, relallvisible FROM pg_class WHERE oid = to_regclass(%s)",
                (table,),
            ).fetchone()
            assert row is not None, table
            if row["relpages"] > 0 and row["relallvisible"] == row["relpages"]:
                return
            waited = time.monotonic() - started
            if waited >= timeout:
                holders = raw.execute(holders_sql).fetchall()
                hidden = raw.execute(_HIDDEN_HORIZON_SQL).fetchall()
                stray = hidden or not row["relpages"] or [
                    h for h in holders if h["blocking"] and not h["maintenance"]
                ]
                if stray or waited >= maintenance_timeout:
                    raise AssertionError(
                        f"visibility map of {table} still incomplete after {waited:.1f}s "
                        f"of VACUUM ({row['relallvisible']}/{row['relpages']} pages; "
                        f"bounds {timeout}s, {maintenance_timeout}s while only "
                        f"maintenance blocks it); sessions holding the horizon: "
                        f"{holders}; prepared transactions / slots: {hidden}"
                    )
                if on_maintenance_wait is not None:
                    on_maintenance_wait(holders)
            time.sleep(0.2)


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
    Index Scan. It does NOT pin the planner's choice among competing
    indexes; pair it with a full-competition EXPLAIN for that.

    Only for tables no foreign key references: dropping a referenced
    primary / unique key would take other tables' foreign keys (and their
    locks) with it and remove the uniqueness the planner reasons with, so it
    is refused before anything is dropped.
    """
    from psycopg import sql as pg_sql

    # Dropping an index takes ACCESS EXCLUSIVE on ``table``. The pool's 2s
    # statement_timeout would also bound the lock wait, so raise both: an
    # autovacuum worker on the table yields after deadlock_timeout.
    connection.execute("SET LOCAL statement_timeout = '30s'")
    connection.execute("SET LOCAL lock_timeout = '30s'")
    connection.execute("SET LOCAL enable_seqscan=off")
    connection.execute("SET LOCAL enable_bitmapscan=off")
    try:
        indexes = connection.execute(
            "SELECT ic.relname AS index_name, con.conname AS constraint_name, "
            "ARRAY(SELECT fk.conname::text FROM pg_constraint fk "
            "WHERE fk.contype = 'f' AND fk.conindid = i.indexrelid "
            "ORDER BY 1) AS referencing_keys "
            "FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
            "LEFT JOIN pg_constraint con ON con.conindid = i.indexrelid "
            "AND con.conrelid = i.indrelid AND con.contype IN ('p', 'u', 'x') "
            "WHERE i.indrelid = to_regclass(%s)",
            (table,),
        ).fetchall()
        names = sorted(row["index_name"] for row in indexes)
        assert index in names, (table, index, names)
        referenced = {
            row["index_name"]: row["referencing_keys"]
            for row in indexes
            if row["index_name"] != index and row["referencing_keys"]
        }
        assert not referenced, (
            f"explain_with_sole_index refuses {table}: foreign keys depend on "
            f"indexes it would drop: {referenced}"
        )
        for row in indexes:
            if row["index_name"] == index:
                continue
            if row["constraint_name"]:
                connection.execute(
                    pg_sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(
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


def scans_with_own_conditions(plan: str, table: str) -> list[tuple[str, list[str]]]:
    """Each scan node of ``table`` in a ``COSTS OFF`` plan with the condition
    lines directly under it (indented deeper, before the next node) -- never a
    neighbouring node's. A ``Bitmap Heap Scan`` node's own lines carry its
    ``Recheck Cond``; its ``Bitmap Index Scan`` child is a separate node."""
    import re

    lines = plan.splitlines()
    scans = []
    for at, line in enumerate(lines):
        if not re.search(rf"\bScan\b.* on {table}\b", line):
            continue
        depth = len(line) - len(line.lstrip())
        own = []
        for below in lines[at + 1:]:
            stripped = below.lstrip()
            if len(below) - len(stripped) <= depth or stripped.startswith("->"):
                break
            own.append(stripped)
        scans.append((line.strip(), own))
    return scans


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


def pytest_testnodedown(node, error) -> None:
    """Fold an xdist worker's reuse outcomes into the controller's counts."""
    output = getattr(node, "workeroutput", {})
    for outcome, count in output.get(_REUSE_OUTCOMES_KEY, {}).items():
        _REUSE_OUTCOMES[outcome] = _REUSE_OUTCOMES.get(outcome, 0) + count
    for marker, count in output.get(_REUSE_DIFFS_KEY, {}).items():
        _REUSE_CATALOG_DIFFS[marker] = _REUSE_CATALOG_DIFFS.get(marker, 0) + count


def pytest_terminal_summary(terminalreporter) -> None:
    if not _REUSE_OUTCOMES or getattr(terminalreporter.config, "workeroutput", None) is not None:
        return
    total = sum(_REUSE_OUTCOMES.values())
    detail = ", ".join(f"{outcome}={count}" for outcome, count in sorted(_REUSE_OUTCOMES.items()))
    terminalreporter.write_line(f"PostgreSQL schema reuse: {total} teardowns; {detail}")
    for marker, count in sorted(_REUSE_CATALOG_DIFFS.items(), key=lambda item: -item[1])[:8]:
        terminalreporter.write_line(f"  catalog change x{count}: {marker}")

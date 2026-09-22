"""Per-notebook FULL export packages -- the ``from_seq = 0`` special case of
docs/incremental-sync-design.md §8.

PR-2 has no source-side change log yet, so every export here is a table scan
over the notebooks being taken across, and both sequence numbers in the
package name and manifest are 0. PR-3 adds the incremental path on top of the
same package format; only where the rows come from changes.

Layering: this module is part of ``app.migration.sync``, whose import
whitelist (guarded by ``tests/test_sync_manifest.py``) deliberately keeps the
package free of services and repository facades. The exporter therefore talks
to the two ``Database`` classes directly and reads its own primary-key catalog,
instead of going through a repository -- an export must be able to run as an
offline tool against a quiesced database without composing the application.

Column TYPES, unlike primary keys, are not read from the live database. They
come from ``app.migration.shadow.postgres_catalog.EXPECTED_COLUMNS``, the
contract parsed out of the PostgreSQL migration DDL, and the SAME contract is
applied to a SQLite source. SQLite is dynamically typed and declares
``created_at TEXT``, so a SQLite source has no type information of its own;
sniffing types out of the values would make a column's encoding depend on what
happens to be stored in it, and two environments holding the same notebook
would produce different packages.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from app.core.database_url import database_identity
from app.migration.shadow.postgres_catalog import EXPECTED_COLUMNS
from app.migration.sync.manifest import (
    MappingKind,
    ScopeKind,
    scope_chain,
    spec_for,
    synced_tables,
)
from app.migration.sync.package import (
    ASSET_FILES_DIR,
    CHECKSUMS_NAME,
    DELETES_NAME,
    KG_EPOCHS_NAME,
    MANIFEST_NAME,
    NOTEBOOK_FILES_DIR,
    PACKAGE_FORMAT_VERSION,
    USERS_NAME,
    encode_value,
    json_document,
    json_line,
    notebook_assets_dir,
    notebook_files_dir,
    package_dir_name,
    rows_path,
    utc_timestamp_text,
)
from app.repositories.postgres.schema_manifest import (
    POSTGRES_ROWID_ORDINAL_TABLES,
    POSTGRES_SCHEMA_MANIFEST,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.config import Settings


# A notebook is skipped, not exported, while one of these lifecycle states is
# in flight: its rows are mid-copy, mid-delete or mid-import and no snapshot of
# them is a coherent notebook. Same predicate as the repositories'
# NOTEBOOK_LIVE_SQL, restated here because this package may not import them --
# the two must stay equal value for value.
_NOT_LIVE_STATUSES = ("copying", "deleting", "importing")

# notebook_grants.principal_type values whose principal_id is a groups.id.
# Restated from app.repositories.group_rows.GROUP_PRINCIPAL_TYPES, which this
# package's import whitelist keeps it from importing. 'group_admins' is the
# SAME group reached over a narrower edge (only its role='admin' members), so
# it scopes and maps exactly like 'group'; treating it as anything else drops
# every admin-only grant on the floor.
_GROUP_PRINCIPAL_TYPES = ("group", "group_admins")

# Ceiling on one ``IN (...)`` list. Notebook counts are small, but a scan of
# every notebook in a large environment must not build one statement with
# thousands of placeholders (SQLite's SQLITE_MAX_VARIABLE_NUMBER, and
# PostgreSQL's planning cost for a huge constant array).
_ID_BATCH = 500

# Rows per keyset page. Each page is an INDEPENDENT, fully-exhausted statement
# ending in ``LIMIT``, so this bounds what the driver materializes AND gives
# the rows file a deterministic order (the pages walk the primary key). It is
# not a ``fetchmany`` size against one open cursor: that would bound the
# client buffer but leave the order of a large table up to the planner.
_PAGE_ROWS = 1000

# Block size for copying a notebook's files while hashing them in one pass.
_COPY_BLOCK = 1 << 20

# How long a ``.tmp`` staging directory must have gone untouched before this
# run treats it as an abandoned one to sweep rather than a concurrent export's
# working directory. See _sweep_stale_staging.
_STALE_STAGING_SECONDS = 3600

# Liveness marker a running export keeps touching at its staging root. It is
# NOT the staging directory's own mtime: writes land in ``rows/`` and
# ``files/`` subdirectories, which never touches the root, so a big export
# that takes longer than _STALE_STAGING_SECONDS would otherwise look abandoned
# to a second export and be deleted out from under itself. Removed before the
# rename, so it never ships inside a package.
_HEARTBEAT_NAME = ".heartbeat"
# Floor on how often the marker is re-touched. The export beats far more often
# than this asks; the throttle keeps a per-row beat down to one clock read per
# _HEARTBEAT_ROWS rows.
_HEARTBEAT_INTERVAL_SECONDS = 60
_HEARTBEAT_ROWS = 4096

# Table and column names all come from the sync manifest and the database's
# own catalog, never from a caller. Validated anyway before interpolation,
# because these are the only identifiers this module cannot parameterize.
_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_JSON_TYPE = "jsonb"
_TIMESTAMP_TYPE = "timestamp with time zone"


class SyncExportError(RuntimeError):
    """An export could not be produced. Never raised for a skipped notebook
    (that is a reported outcome, not a failure) -- only for a malformed
    request or a source database that cannot answer the export's questions."""


@dataclass(frozen=True)
class ExportReport:
    package_dir: Path
    package_id: str
    # Notebook ids actually written into the package, sorted.
    notebooks: tuple[str, ...]
    # notebook id -> why it was left out (mirror / not live / unknown id).
    skipped: Mapping[str, str]
    # table name -> rows written, for every synced table (0 included, so the
    # report and the package's file set describe the same thing).
    table_counts: Mapping[str, int]
    file_count: int
    bytes_written: int
    # Always "full" in PR-2: there is no change log to read from yet, so an
    # existing watermark for this target does NOT turn the run into an
    # incremental one -- it is overwritten by another full package. The field
    # exists now so PR-3's "incremental" is a new value, not a new field.
    mode: str
    # Non-fatal conditions an operator should see, e.g. a stale staging
    # directory left by an earlier crashed run and removed by this one.
    warnings: tuple[str, ...] = ()
    # Package-relative paths of files that vanished between listing and
    # copying. Reported rather than raised: a source file deleted by a
    # concurrent notebook edit must not throw away a whole export.
    missing_files: tuple[str, ...] = ()


def _quoted(name: str) -> str:
    if not _SAFE_IDENT.match(name):
        raise SyncExportError(f"unsafe SQL identifier from catalog: {name!r}")
    return f'"{name}"'


def _batched(
    values: Sequence[Any], size: int = _ID_BATCH
) -> Iterator[tuple[Any, ...]]:
    """Slice a bind list into statement-sized chunks. Not str-only: the
    importer batches primary-key values through this too, and a composite key
    column can be an integer as easily as text."""
    for start in range(0, len(values), size):
        yield tuple(values[start : start + size])


def _contracts(table: str) -> Mapping[str, Any]:
    contracts = EXPECTED_COLUMNS.get(table)
    if not contracts:
        raise SyncExportError(
            f"{table}: no column contract in the PostgreSQL migration DDL "
            "(app.migration.shadow.postgres_catalog)"
        )
    return contracts


def _columns_of_type(table: str, columns: Sequence[str], data_type: str) -> frozenset:
    contracts = _contracts(table)
    return frozenset(
        name
        for name in columns
        if name in contracts and contracts[name].data_type == data_type
    )


# --------------------------------------------------------------- source db


class _Source:
    """Read/write handle over whichever backend ``settings`` names, with the
    one dialect difference the exporter actually hits (``?`` vs ``%s``) shimmed
    in a single place. Deliberately not an ORM or a repository: the exporter
    needs raw rows in the database's own shape."""

    def __init__(self, settings: "Settings", root_dir: Path) -> None:
        self.scheme = database_identity(settings.database_url).scheme
        if self.scheme == "postgresql":
            from app.repositories.postgres.database import PostgresDatabase

            self._database: Any = PostgresDatabase(settings, root_dir)
        else:
            from app.repositories.sqlite.database import SqliteDatabase

            self._database = SqliteDatabase(settings, root_dir)

    @property
    def is_postgres(self) -> bool:
        return self.scheme == "postgresql"

    def close(self) -> None:
        close = getattr(self._database, "close", None)
        if close is not None:
            close()

    def sql(self, statement: str) -> str:
        """Translate the module's ``?`` placeholders for PostgreSQL. Safe as a
        blind replace because no statement here contains a literal ``?`` or a
        ``LIKE`` pattern."""
        return statement.replace("?", "%s") if self.is_postgres else statement

    @staticmethod
    def _require_bare(conn: Any) -> Any:
        """Reject a read-budget wrapper.

        Both backends wrap ``connect()``'s result when a per-request read
        budget is in scope, and that wrapper prepends its own statement to
        every ``execute`` -- on PostgreSQL a ``set_config('statement_timeout',
        ..., true)``, which would steal the "first statement of the
        transaction" slot that ``SET TRANSACTION`` needs, and would then cap
        every page of a hours-long export at a request deadline. An export is
        an offline operation; it must not be running inside a request budget
        at all, so this is a loud failure rather than a silent downgrade.
        """
        if getattr(conn, "budget", None) is not None:
            raise SyncExportError(
                "an export must not run inside a request read budget: the "
                "budgeted connection wrapper re-times every statement and "
                "would truncate the export's own snapshot"
            )
        return conn

    @contextmanager
    def read(self) -> Iterator[Any]:
        """One read connection holding ONE consistent snapshot of the ROWS.

        Every table is read inside this window, so a package can never contain
        a chunk whose source row was deleted halfway through the run. Files
        are deliberately copied AFTER it closes -- see ``_write_files``.

        PostgreSQL: ``SET TRANSACTION`` is issued as the first statement of
        the transaction psycopg opens lazily -- the pool hands out an idle
        connection (``_restore_client_defaults`` rolls back and resets
        isolation/read_only before yielding), so no statement has run yet and
        the SET is legal. ``statement_timeout`` is then lifted for this
        transaction only: a whole-notebook scan can legitimately outlive the
        serving deadline the pool configures, the same reason the knowhow
        projection lock lifts it (``postgres/database.py``). ``SET LOCAL`` and
        ``SET TRANSACTION`` both last exactly one transaction, and the next
        borrower is reset again anyway, so nothing needs restoring.

        SQLite: a deferred ``BEGIN``; WAL then pins the snapshot at the first
        read and holds it until commit.
        """
        if self.is_postgres:
            with self._database.connect() as conn:
                self._require_bare(conn)
                conn.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                conn.execute("SET LOCAL statement_timeout = 0")
                yield conn
            return
        conn = self._require_bare(self._database.connect())
        started = not conn.in_transaction
        if started:
            conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            if started:
                conn.rollback()
            raise
        else:
            if started:
                conn.commit()

    @contextmanager
    def write(self) -> Iterator[Any]:
        with self._database.write() as conn:
            yield conn

    def fetch(
        self, conn: Any, statement: str, params: Sequence[Any] = ()
    ) -> list[dict[str, Any]]:
        """Run one statement the caller has already bounded (``LIMIT``, or a
        single-row lookup) and return its rows."""
        cursor = conn.execute(self.sql(statement), tuple(params))
        return [dict(row) for row in cursor.fetchall()]

    def stream(
        self, conn: Any, statement: str, params: Sequence[Any] = ()
    ) -> Iterator[dict[str, Any]]:
        """Iterate an UNBOUNDED statement without materializing it.

        PostgreSQL needs a named (server-side) cursor for this: an ordinary
        psycopg cursor fetches the whole result before the first row is
        visible. SQLite's cursor is already lazy.
        """
        if self.is_postgres:
            with conn.cursor(name=f"sync_export_{uuid.uuid4().hex}") as cursor:
                cursor.execute(self.sql(statement), tuple(params))
                for row in cursor:
                    yield dict(row)
            return
        for row in conn.execute(self.sql(statement), tuple(params)):
            yield dict(row)

    def columns(self, conn: Any, table: str) -> tuple[str, ...]:
        """The table's column names in its own column order. Only the NAMES
        are read live; types come from the migration DDL contract (see the
        module docstring)."""
        if self.is_postgres:
            rows = self.fetch(
                conn,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = ? "
                "ORDER BY ordinal_position",
                (table,),
            )
            found = tuple(str(row["column_name"]) for row in rows)
        else:
            rows = self.fetch(conn, f"PRAGMA table_info({_quoted(table)})")
            found = tuple(str(row["name"]) for row in rows)
        if not found:
            raise SyncExportError(f"source database has no table {table!r}")
        return found

    def _catalog_primary_key(self, conn: Any, table: str) -> tuple[str, ...]:
        """The table's primary-key columns, in key order, or ``()`` when the
        table has none. Read from the catalog rather than hard-coded, so a
        parent join and a keyset page follow the real key and a later schema
        change cannot leave a stale literal behind. Catalog-only -- callers
        that need a row identity for a table with no catalog primary key
        want ``sync_key`` instead, which also consults the sync manifest."""
        if self.is_postgres:
            rows = self.fetch(
                conn,
                "SELECT a.attname AS name FROM pg_index i "
                "CROSS JOIN LATERAL unnest(i.indkey::smallint[]) "
                "WITH ORDINALITY AS k(attnum, ord) "
                "JOIN pg_attribute a "
                "ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
                "WHERE i.indrelid = ?::regclass AND i.indisprimary "
                "ORDER BY k.ord",
                (table,),
            )
            return tuple(str(row["name"]) for row in rows)
        rows = self.fetch(conn, f"PRAGMA table_info({_quoted(table)})")
        keyed = sorted(
            (int(row["pk"]), str(row["name"])) for row in rows if int(row["pk"]) > 0
        )
        return tuple(name for _position, name in keyed)

    def _has_unique_surface(
        self, conn: Any, table: str, columns: Sequence[str]
    ) -> bool:
        """Whether the LIVE catalog backs ``columns`` with a unique index or
        constraint covering EXACTLY that column set (order does not matter).
        Used to guard a ``TableSyncSpec.key`` fallback: a registered key is
        only a real row identity if the schema actually enforces it is
        unique, not merely a claim in the manifest. Partial/predicate unique
        indexes do not count -- their uniqueness only holds for rows
        satisfying a predicate this check does not evaluate."""
        wanted = frozenset(columns)
        if self.is_postgres:
            rows = self.fetch(
                conn,
                "SELECT i.indexrelid AS relid, a.attname AS name FROM pg_index i "
                "CROSS JOIN LATERAL unnest(i.indkey::smallint[]) AS k(attnum) "
                "JOIN pg_attribute a "
                "ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
                "WHERE i.indrelid = ?::regclass AND i.indisunique "
                "AND i.indpred IS NULL",
                (table,),
            )
            by_index: dict[Any, set[str]] = {}
            for row in rows:
                by_index.setdefault(row["relid"], set()).add(str(row["name"]))
            return any(frozenset(cols) == wanted for cols in by_index.values())
        for index in self.fetch(conn, f"PRAGMA index_list({_quoted(table)})"):
            if int(index["unique"]) != 1 or int(index.get("partial", 0) or 0) == 1:
                continue
            info = self.fetch(
                conn, f"PRAGMA index_info({_quoted(str(index['name']))})"
            )
            if frozenset(str(row["name"]) for row in info) == wanted:
                return True
        return False

    def sync_key(self, conn: Any, table: str) -> tuple[str, ...]:
        """The table's synchronization key, in key order: the columns the
        capture triggers, the exporter's keyset pages, and the importer's
        upsert/prune all agree identify one row.

        The catalog primary key is the answer whenever the table has one.
        For the two tables that do not (``knowledge_object_sources``,
        ``community_members`` -- SQLite cannot add a primary key to an
        existing table in place, so v84 gives them a UNIQUE index instead of
        a PRIMARY KEY; PostgreSQL 0064 gives them a real one), the columns
        registered on ``TableSyncSpec.key`` are the answer, but only after
        confirming the live catalog actually backs that column set with a
        unique index/constraint -- a manifest claim with no enforcement
        behind it on THIS database is a schema/manifest drift, not a usable
        key. When both a catalog primary key and a registered ``key`` exist
        they must name the same columns (a later migration that gives a
        registered table a real primary key must update the manifest in the
        same change, or this catches the disagreement instead of silently
        preferring one). Every synced table must resolve to a non-empty key;
        this is a hard error rather than an empty-tuple return so a caller
        can never again fall through to an unkeyed code path.
        """
        catalog = self._catalog_primary_key(conn, table)
        declared = spec_for(table).key
        if catalog and declared and frozenset(catalog) != frozenset(declared):
            raise SyncExportError(
                f"{table}: TableSyncSpec.key {declared} disagrees with the "
                f"catalog primary key {catalog}"
            )
        if catalog:
            return catalog
        if not declared:
            raise SyncExportError(
                f"{table}: no primary key in the catalog and no "
                "TableSyncSpec.key registered; every synced table must "
                "resolve to a synchronization key"
            )
        if not self._has_unique_surface(conn, table, declared):
            raise SyncExportError(
                f"{table}: TableSyncSpec.key {declared} has no covering "
                "unique index/constraint on this catalog; the manifest key "
                "and the live schema have drifted"
            )
        return declared


# ------------------------------------------------------------- row encoding


def _row_encoder(table: str, columns: Sequence[str]):
    """Return ``row -> portable row`` for one table, keyed BY COLUMN.

    Three classes, decided from the migration DDL contract and never from the
    value: a ``jsonb`` column travels as text; a ``timestamp with time zone``
    column travels as UTC ISO text, with NULL staying NULL (a missing instant
    is not the empty string -- ``POSTGRES_EMPTY_TIME_SENTINELS`` columns such
    as ``knowledge_objects.last_reviewed`` write ``null`` here too, and the
    importer decides what its own backend stores for "no time"); everything
    else goes through ``encode_value`` for the two shapes JSON cannot carry
    natively, bytes and bool.
    """
    json_columns = _columns_of_type(table, columns, _JSON_TYPE)
    timestamp_columns = _columns_of_type(table, columns, _TIMESTAMP_TYPE)

    def encode(row: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in row.items():
            if name in timestamp_columns:
                result[name] = utc_timestamp_text(value)
            elif name in json_columns:
                result[name] = _json_text(value)
            else:
                result[name] = encode_value(value)
        return result

    return encode


def _json_text(value: Any) -> Any:
    """A JSON column's portable form: text, or NULL. psycopg hands back a
    decoded ``list``/``dict`` for ``jsonb``; SQLite already holds text.
    ``sort_keys`` because ``jsonb`` has no key order of its own to preserve,
    so sorting is what makes two exports of one row byte-identical."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _exported_columns(
    source: _Source, columns: Sequence[str], table: str
) -> tuple[str, ...]:
    """Column names this table contributes to the package. The PostgreSQL
    ``ordinal`` identity column of a rowid-ordinal table is dropped: it is a
    backend-local surrogate for SQLite's rowid and the target reassigns its
    own (§8)."""
    if source.is_postgres and table in POSTGRES_ROWID_ORDINAL_TABLES:
        return tuple(name for name in columns if name != "ordinal")
    return tuple(columns)


# --------------------------------------------------------------- scope SQL


# This registry's key set must stay equal to the manifest's GLOBAL synced-layer
# table set: when tests/test_sync_manifest.py's pinned GLOBAL roster changes,
# an entry has to be added or removed HERE in the same change. The scoping rule
# lives here rather than in the manifest on purpose -- PR-3's change-capture
# trigger only needs a scope's parent key, and the importer needs no scoping at
# all, so the exporter is the only consumer. tests/test_sync_export.py guards
# both that equality and this table's agreement with _GLOBAL_KEY_QUERIES.
#
# A GLOBAL table has no column of its own saying which notebook a row belongs
# to; each is reached over a DIFFERENT reference edge out of the exported
# notebooks (see each table's TableSyncSpec.notes):
#
#   groups / group_members -- the group grants of the exported notebooks.
#   object_schemas         -- the object types those notebooks' knowledge
#                             objects and per-notebook schema overrides name.
#                             Its own notebook_id has been '' for every row
#                             since v47, so a notebook-scoped filter would
#                             export nothing at all.
#
# ``(filter column on the GLOBAL table, name of the key set to bind)``.
_GLOBAL_SCOPES: dict[str, tuple[str, str]] = {
    "groups": ("id", "granted_group_ids"),
    "group_members": ("group_id", "granted_group_ids"),
    "object_schemas": ("object_type", "referenced_object_types"),
}

# How each key set above is collected from the exported notebooks. One entry
# may have several queries; their results are unioned.
_GLOBAL_KEY_QUERIES: dict[str, tuple[str, ...]] = {
    "granted_group_ids": (
        "SELECT DISTINCT principal_id AS value FROM notebook_grants "
        "WHERE principal_type IN ("
        + ", ".join(f"'{kind}'" for kind in _GROUP_PRINCIPAL_TYPES)
        + ") AND notebook_id IN ({placeholders})",
    ),
    "referenced_object_types": (
        "SELECT DISTINCT object_type AS value FROM knowledge_objects "
        "WHERE notebook_id IN ({placeholders})",
        "SELECT DISTINCT object_type AS value FROM notebook_object_schemas "
        "WHERE notebook_id IN ({placeholders})",
    ),
}


def _scope_of(table: str):
    """This table's ``TableScope``. ``None`` is the LOCAL sentinel, which the
    exporter must never reach -- it only ever walks ``synced_tables()``."""
    scope = spec_for(table).scope
    if scope is None:
        raise SyncExportError(f"{table}: LOCAL table has no export scope")
    return scope


def _global_scope(table: str) -> tuple[str, str]:
    scope = _GLOBAL_SCOPES.get(table)
    if scope is None:
        raise SyncExportError(
            f"{table}: GLOBAL table has no registered scoping rule; add one to "
            "_GLOBAL_SCOPES (and its key query) in the same change as the "
            "manifest entry"
        )
    return scope


def _parent_join_clause(source: _Source, conn: Any, table: str) -> tuple[str, str]:
    """``(JOIN clauses, filtering "<alias>.<column>")`` for a PARENT-scoped
    table: follow its ``scope_chain`` up to the ancestor that carries a
    notebook column. The chain can be more than one hop
    (``knowhow_cell_code`` -> ``knowhow_rows`` -> ``knowhow_tables``). Each
    hop joins on the parent's real primary key read from the catalog, not on
    a hard-coded ``id``, so a schema change cannot leave a stale literal here.
    """
    chain = scope_chain(table)
    joins: list[str] = []
    for index, (_child, child_column, parent) in enumerate(chain):
        parent_key = source.sync_key(conn, parent)
        if len(parent_key) != 1:
            raise SyncExportError(
                f"{table}: parent {parent!r} has the primary key {parent_key}; "
                "a scope chain can only follow a single-column key"
            )
        joins.append(
            f"JOIN {_quoted(parent)} t{index + 1} "
            f"ON t{index + 1}.{_quoted(parent_key[0])} "
            f"= t{index}.{_quoted(child_column)}"
        )
    root_column = _scope_of(chain[-1][2]).column
    return " ".join(joins), f"t{len(chain)}.{_quoted(root_column)}"


@dataclass(frozen=True)
class _TableQuery:
    # "SELECT <cols> FROM <table> t0 <joins>"
    prefix: str
    # The "<alias>.<column>" the scope key list is matched against.
    filtered: str
    # Synchronization key, in key order. Always non-empty -- sync_key()
    # raises for a table that cannot resolve one.
    key_columns: tuple[str, ...]
    # ORDER BY list, already alias-qualified.
    order_by: str


def _table_query(
    source: _Source, conn: Any, table: str, columns: Sequence[str]
) -> _TableQuery:
    """Resolve this table's scope and key against the catalog ONCE per table,
    so a multi-page, multi-batch scan does not re-read the catalog."""
    projection = ", ".join(f"t0.{_quoted(name)}" for name in columns)
    head = f"SELECT {projection} FROM {_quoted(table)} t0"
    scope = _scope_of(table)
    if scope.kind is ScopeKind.NOTEBOOK:
        joins, filtered = "", f"t0.{_quoted(scope.column)}"
    elif scope.kind is ScopeKind.PARENT:
        joins, filtered = _parent_join_clause(source, conn, table)
    else:
        joins, filtered = "", f"t0.{_quoted(_global_scope(table)[0])}"
    key_columns = source.sync_key(conn, table)
    return _TableQuery(
        prefix=f"{head} {joins}".rstrip(),
        filtered=filtered,
        key_columns=key_columns,
        order_by=", ".join(f"t0.{_quoted(name)}" for name in key_columns),
    )


def _scan(
    source: _Source, conn: Any, query: _TableQuery, batch: Sequence[str]
) -> Iterator[dict[str, Any]]:
    """Yield one scope batch's rows in synchronization-key order.

    Every synced table pages by keyset -- ``(key...) > (last key...)`` as an
    SQL row value, supported by SQLite >= 3.15 and PostgreSQL -- so each
    statement is bounded and the order is the key's. This includes the two
    tables with no catalog primary key of their own
    (``knowledge_object_sources``, ``community_members``): ``sync_key``
    resolves their ``TableSyncSpec.key`` instead, guarded to only ever return
    a column set the live catalog actually backs with a unique index/
    constraint (v84/0064), so the same paging logic applies to them too.

    Measured on PostgreSQL (120k rows per table, two notebooks, EXPLAIN
    (ANALYZE, BUFFERS) on the first page and on page ~50) before this shape
    was settled: no page re-sorts or re-scans the filtered set. Every page is
    an index scan of the CHILD table's own primary key with the cursor as an
    Index Cond, and the scope predicate resolves per row -- a filter for a
    NOTEBOOK table, a Memoized parent index lookup for a one-hop PARENT table
    (999 cache hits per 1000-row page), a per-row parent index scan plus a
    Materialized two-row scan for the two-hop case. Page 50 costs the same as
    page 1 (chunks 0.38 vs 0.48 ms, source_elements 0.26 vs 0.55 ms,
    knowhow_cells 0.70 vs 0.71 ms), so the per-page cost is O(page), not
    O(table). Batching by PARENT key instead was considered and rejected on
    those numbers: the deepest chain's parent (knowhow_rows) has as many rows
    as the child, so a parent-id list would need pagination of its own and
    would buy nothing -- and no index exists solely to serve an export.
    """
    placeholders = ",".join("?" for _ in batch)
    where = f"{query.filtered} IN ({placeholders})"
    keys = ", ".join(f"t0.{_quoted(name)}" for name in query.key_columns)
    cursor: tuple[Any, ...] | None = None
    while True:
        statement = f"{query.prefix} WHERE {where}"
        params = list(batch)
        if cursor is not None:
            statement += f" AND ({keys}) > ({','.join('?' for _ in cursor)})"
            params.extend(cursor)
        statement += f" ORDER BY {query.order_by} LIMIT {_PAGE_ROWS}"
        page = source.fetch(conn, statement, params)
        if not page:
            return
        yield from page
        advanced = tuple(page[-1][name] for name in query.key_columns)
        if advanced == cursor:
            raise SyncExportError(
                f"keyset cursor did not advance past {advanced!r}; the primary "
                "key read from the catalog is not unique"
            )
        cursor = advanced
        if len(page) < _PAGE_ROWS:
            return


# ----------------------------------------------------------- package writer


class _PackageWriter:
    """Writes into ``<final>.tmp`` and records each file's sha256 and size as
    it goes, so ``checksums.json`` never requires a second pass over the rows.
    ``write_order`` is the package's file-creation order, which is what makes
    "manifest.json is written last" checkable rather than a claim about
    timestamps."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.checksums: dict[str, str] = {}
        self.bytes_written = 0
        self.write_order: list[str] = []
        self._heartbeat = root / _HEARTBEAT_NAME
        self._beat_at = 0.0
        self.beat(force=True)

    def beat(self, *, force: bool = False) -> None:
        """Re-touch the liveness marker (throttled). Never recorded: the
        marker is not package content, so it stays out of ``checksums``,
        ``write_order`` and ``bytes_written``."""
        now = time.monotonic()
        if not force and now - self._beat_at < _HEARTBEAT_INTERVAL_SECONDS:
            return
        self._heartbeat.touch()
        self._beat_at = now

    def finish(self) -> None:
        """Drop the liveness marker, immediately before the rename. From here
        on the staging directory holds exactly the package's own files."""
        self._heartbeat.unlink(missing_ok=True)

    @contextmanager
    def lines(self, relative: str) -> Iterator[Any]:
        path = self._prepared(relative)
        digest = hashlib.sha256()
        counter = {"size": 0, "rows": 0}
        with path.open("wb") as handle:

            def write(payload: str) -> None:
                data = (payload + "\n").encode("utf-8")
                handle.write(data)
                digest.update(data)
                counter["size"] += len(data)
                counter["rows"] += 1
                # One table can outrun the staleness window on its own, so the
                # beat cannot wait for the table to finish.
                if counter["rows"] % _HEARTBEAT_ROWS == 0:
                    self.beat()

            yield write
        self._recorded(relative, digest.hexdigest(), counter["size"])
        self.beat()

    def document(self, relative: str, payload: Any, *, checksum: bool = True) -> str:
        """Write one JSON document and return its sha256."""
        path = self._prepared(relative)
        data = (json_document(payload) + "\n").encode("utf-8")
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        self._recorded(relative, digest if checksum else None, len(data))
        return digest

    def copy_file(self, relative: str, origin: Path) -> bool:
        """Copy one file while hashing it in a single streaming pass.

        Returns False, and writes nothing, when the SOURCE is gone or
        unreadable -- a concurrent notebook edit, which the caller reports as
        a missing file instead of throwing away the export. A failure on the
        TARGET side (out of space, read-only volume) is NOT that: it means
        this package cannot be written at all, so it propagates and
        ``_export`` removes the staging directory.
        """
        path = self._prepared(relative)
        digest = hashlib.sha256()
        size = 0
        try:
            handle = origin.open("rb")
        except (FileNotFoundError, PermissionError, IsADirectoryError):
            return False
        with handle, path.open("wb") as target:
            while True:
                block = handle.read(_COPY_BLOCK)
                if not block:
                    break
                target.write(block)
                digest.update(block)
                size += len(block)
                # A single attachment can be large enough to outlast the
                # staleness window by itself.
                self.beat()
        self._recorded(relative, digest.hexdigest(), size)
        self.beat()
        return True

    def _prepared(self, relative: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _recorded(self, relative: str, digest: str | None, size: int) -> None:
        if digest is not None:
            self.checksums[relative] = digest
        self.bytes_written += size
        self.write_order.append(relative)


# --------------------------------------------------------------- notebooks


@dataclass(frozen=True)
class _NotebookSelection:
    exported: tuple[str, ...]
    skipped: dict[str, str]


def _select_notebooks(
    source: _Source, conn: Any, requested: Sequence[str] | None
) -> _NotebookSelection:
    """Partition the requested notebooks (or every notebook) into what this
    package carries and what it leaves behind with a reason.

    A mirror (``sync_origin != ''``) is never a source: re-exporting one would
    launder another environment's notebook into this environment's name and
    make the two sides each other's upstream.
    """
    statement = "SELECT id, status, sync_origin FROM notebooks"
    found: dict[str, dict[str, Any]] = {}
    if requested is None:
        for row in source.stream(conn, f"{statement} ORDER BY id"):
            found[str(row["id"])] = row
    else:
        for batch in _batched(list(dict.fromkeys(requested))):
            placeholders = ",".join("?" for _ in batch)
            for row in source.fetch(
                conn, f"{statement} WHERE id IN ({placeholders})", batch
            ):
                found[str(row["id"])] = row

    skipped: dict[str, str] = {}
    exported: list[str] = []
    for notebook_id in sorted(found):
        row = found[notebook_id]
        if str(row["sync_origin"] or ""):
            skipped[notebook_id] = (
                f"mirror of {str(row['sync_origin'])!r}; a mirror cannot be a source"
            )
        elif str(row["status"]) in _NOT_LIVE_STATUSES:
            skipped[notebook_id] = f"status={str(row['status'])!r}"
        else:
            exported.append(notebook_id)
    for notebook_id in requested or ():
        if notebook_id not in found:
            skipped[notebook_id] = "no such notebook"
    return _NotebookSelection(tuple(exported), skipped)


def _global_keys(
    source: _Source, conn: Any, notebooks: Sequence[str]
) -> dict[str, tuple[str, ...]]:
    """Resolve every GLOBAL key set (see ``_GLOBAL_KEY_QUERIES``) against the
    exported notebooks, once per export rather than once per GLOBAL table."""
    resolved: dict[str, tuple[str, ...]] = {}
    for name, queries in _GLOBAL_KEY_QUERIES.items():
        found: set[str] = set()
        for batch in _batched(list(notebooks)):
            placeholders = ",".join("?" for _ in batch)
            for query in queries:
                for row in source.stream(
                    conn, query.format(placeholders=placeholders), batch
                ):
                    value = str(row["value"] or "")
                    if value:
                        found.add(value)
        resolved[name] = tuple(sorted(found))
    return resolved


# ------------------------------------------------------------------ tables


def _identity_columns(table: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(USER-mapped columns, PRINCIPAL-mapped columns)`` for one table.
    Resolved once per table, not once per row."""
    spec = spec_for(table)
    return (
        tuple(c.name for c in spec.mapped_columns if c.kind is MappingKind.USER),
        tuple(c.name for c in spec.mapped_columns if c.kind is MappingKind.PRINCIPAL),
    )


class _UserIds:
    """Accumulates every source user id the package's rows reference, so
    ``users.jsonl`` projects exactly the users the target must map."""

    def __init__(self) -> None:
        self.ids: set[str] = set()

    def observe(
        self,
        row: Mapping[str, Any],
        user_columns: Sequence[str],
        principal_columns: Sequence[str],
    ) -> None:
        for name in user_columns:
            self._add(row.get(name))
        # notebook_grants.principal_id is a user id only when that row says
        # so; a group principal is mapped by group name and 'everyone' is not
        # an identity at all.
        if principal_columns and str(row.get("principal_type") or "") == "user":
            for name in principal_columns:
                self._add(row.get(name))

    def _add(self, value: Any) -> None:
        if isinstance(value, str) and value:
            self.ids.add(value)


def _write_table(
    source: _Source,
    conn: Any,
    writer: _PackageWriter,
    table: str,
    notebooks: Sequence[str],
    global_keys: Mapping[str, tuple[str, ...]],
    users: _UserIds,
) -> tuple[tuple[str, ...], int]:
    """Write ``rows/<table>.jsonl`` and return ``(columns, row count)``. The
    file is created even when the table contributes nothing, so the package's
    file set is a function of the manifest alone."""
    columns = _exported_columns(source, source.columns(conn, table), table)
    encode = _row_encoder(table, columns)
    user_columns, principal_columns = _identity_columns(table)
    scope = _scope_of(table)
    keys = (
        global_keys[_global_scope(table)[1]]
        if scope.kind is ScopeKind.GLOBAL
        else notebooks
    )
    query = _table_query(source, conn, table, columns)
    written = 0
    with writer.lines(rows_path(table)) as write:
        for batch in _batched(list(keys), _ID_BATCH):
            for row in _scan(source, conn, query, batch):
                users.observe(row, user_columns, principal_columns)
                write(json_line(encode(row)))
                written += 1
    return columns, written


def _write_users(
    source: _Source, conn: Any, writer: _PackageWriter, user_ids: Sequence[str]
) -> int:
    written = 0
    with writer.lines(USERS_NAME) as write:
        for batch in _batched(list(user_ids)):
            placeholders = ",".join("?" for _ in batch)
            for row in source.fetch(
                conn,
                "SELECT id, username, display_name, role FROM users "
                f"WHERE id IN ({placeholders}) ORDER BY id",
                batch,
            ):
                write(
                    json_line(
                        {
                            "id": str(row["id"]),
                            "username": str(row["username"] or ""),
                            "display_name": str(row["display_name"] or ""),
                            "role": str(row["role"] or ""),
                        }
                    )
                )
                written += 1
    return written


# The two on-disk roots a notebook owns, as ``(storage subdirectory, package
# path builder)``. ``storage/notebooks/<id>/`` holds uploaded source files;
# ``storage/assets/<id>/`` holds attachment bodies whose metadata rows are in
# notebook_assets. Both must travel, and notebook delete already treats them
# as the same pair (services/notebook_catalog.py).
_FILE_ROOTS = (
    (NOTEBOOK_FILES_DIR, notebook_files_dir),
    (ASSET_FILES_DIR, notebook_assets_dir),
)


def _write_files(
    writer: _PackageWriter, storage_dir: Path, notebooks: Sequence[str]
) -> tuple[int, list[str]]:
    """Copy each exported notebook's on-disk roots into the package, returning
    ``(files copied, files that vanished mid-copy)``. A notebook with no
    uploaded file, or no attachment, simply has no such directory; that is
    normal, not an error.

    Runs OUTSIDE the row snapshot on purpose. A filesystem has no snapshot to
    join, so holding the database transaction open across a multi-gigabyte
    copy would buy nothing and cost plenty: on PostgreSQL a REPEATABLE READ
    transaction idle for the length of a file copy pins the xmin horizon
    against autovacuum and is exactly what ``idle_in_transaction_session_
    timeout`` exists to kill. What keeps the package coherent instead is the
    ORDER -- rows are read first, so every file copied afterwards is at least
    as new as the row that points at it -- plus ``missing_files`` for anything
    deleted in between.
    """
    copied = 0
    missing: list[str] = []
    for notebook_id in notebooks:
        for subdirectory, package_dir_for in _FILE_ROOTS:
            origin = storage_dir / subdirectory / notebook_id
            if not origin.is_dir():
                continue
            target_prefix = package_dir_for(notebook_id)
            for path in sorted(origin.rglob("*")):
                if not path.is_file():
                    continue
                relative = f"{target_prefix}/{path.relative_to(origin).as_posix()}"
                if writer.copy_file(relative, path):
                    copied += 1
                else:
                    missing.append(relative)
    return copied, missing


# ----------------------------------------------------------------- watermark


def _advance_watermark(
    source: _Source, target_env: str, package_id: str, exported_at: datetime
) -> None:
    """Upsert this target's watermark, only once the package is complete on
    disk (§7): a failed export must leave the watermark where it was so a
    re-run is always safe."""
    moment: Any = exported_at if source.is_postgres else exported_at.isoformat()
    with source.write() as conn:
        conn.execute(
            source.sql(
                "INSERT INTO sync_export_state "
                "(target_env, exported_through_seq, exported_at, package_id) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (target_env) DO UPDATE SET "
                "exported_through_seq = excluded.exported_through_seq, "
                "exported_at = excluded.exported_at, "
                "package_id = excluded.package_id"
            ),
            (target_env, 0, moment, package_id),
        )


# ------------------------------------------------------------------- export


def _staging_is_abandoned(path: Path, cutoff: float) -> bool:
    """Whether this staging directory belongs to no live run.

    The ``.heartbeat`` marker answers it when present. Its absence means one
    of two things, and both land on the root mtime: debris from a version
    older than the marker, or a run that died between ``mkdir`` and the
    writer's first beat -- a window of microseconds, whose root mtime is then
    the moment of that mkdir and therefore already the right answer.
    """
    try:
        return (path / _HEARTBEAT_NAME).stat().st_mtime <= cutoff
    except OSError:
        pass
    try:
        return path.stat().st_mtime <= cutoff
    except OSError:
        return False


def _sweep_stale_staging(out_dir: Path, source_env: str) -> list[str]:
    """Remove this source's ABANDONED ``.tmp`` staging directories.

    A staging directory belongs either to a run that died before its rename or
    to a run happening right now -- the two are indistinguishable by name, and
    two exports of the same source environment into one output directory is a
    thing operators do. Liveness is decided by the ``.heartbeat`` marker the
    running export keeps touching (see ``_PackageWriter.beat``), NOT by the
    staging directory's own mtime: every byte an export writes lands in
    ``rows/`` or ``files/``, which never updates the root's mtime, so a big
    export running longer than ``_STALE_STAGING_SECONDS`` would look abandoned
    by that measure and a second export would delete it mid-run. Debris left
    by a version that predates the marker has no ``.heartbeat`` at all; for
    those, and only those, the root mtime is the fallback. Another source
    environment's debris is left alone entirely; it is not ours to judge.
    """
    if not out_dir.is_dir():
        return []
    prefix = f"sync-{source_env}-"
    cutoff = time.time() - _STALE_STAGING_SECONDS
    warnings: list[str] = []
    for path in sorted(out_dir.iterdir()):
        if not (
            path.is_dir()
            and path.name.startswith(prefix)
            and path.name.endswith(".tmp")
        ):
            continue
        if not _staging_is_abandoned(path, cutoff):
            continue
        shutil.rmtree(path, ignore_errors=True)
        warnings.append(
            f"removed stale staging directory {path.name} left by an earlier "
            "export that did not finish"
        )
    return warnings


def export_notebooks(
    settings: "Settings",
    *,
    target_env: str,
    out_dir: Path,
    notebook_ids: Sequence[str] | None,
    source_env: str,
) -> ExportReport:
    """Write one full export package under ``out_dir`` and return its report.

    ``notebook_ids=None`` means "every live, non-mirror notebook". An EMPTY
    sequence is rejected rather than treated as None: an empty package is
    never what a caller meant, and a ``--notebook`` list that silently became
    empty upstream would otherwise produce a package that imports cleanly and
    carries nothing.

    The package is assembled in a sibling ``.tmp`` directory and renamed into
    place only after ``manifest.json`` is written last, so a reader can treat
    "the manifest is there" as "the package is complete" without a lock. A run
    that fails removes its own staging directory.
    """
    if not source_env or not target_env:
        raise SyncExportError("source_env and target_env are both required")
    if notebook_ids is not None and not notebook_ids:
        raise SyncExportError(
            "notebook_ids is an empty selection; pass None to export every "
            "live notebook"
        )
    root_dir = Path(__file__).resolve().parents[4]
    source = _Source(settings, root_dir)
    try:
        return _export(
            source,
            settings=settings,
            target_env=target_env,
            out_dir=Path(out_dir),
            notebook_ids=notebook_ids,
            source_env=source_env,
        )
    finally:
        source.close()


def _export(
    source: _Source,
    *,
    settings: "Settings",
    target_env: str,
    out_dir: Path,
    notebook_ids: Sequence[str] | None,
    source_env: str,
) -> ExportReport:
    package_id = uuid.uuid4().hex
    exported_at = datetime.now(timezone.utc)
    final_dir = out_dir / package_dir_name(source_env, 0, 0, package_id)
    staging_dir = final_dir.with_name(final_dir.name + ".tmp")
    out_dir.mkdir(parents=True, exist_ok=True)
    warnings = _sweep_stale_staging(out_dir, source_env)
    staging_dir.mkdir()
    try:
        return _assemble(
            source,
            settings=settings,
            writer=_PackageWriter(staging_dir),
            target_env=target_env,
            source_env=source_env,
            notebook_ids=notebook_ids,
            package_id=package_id,
            exported_at=exported_at,
            final_dir=final_dir,
            warnings=warnings,
        )
    except BaseException:
        # A half-written package must never be left where the next run would
        # sweep it and report it as someone else's crash.
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def _assemble(
    source: _Source,
    *,
    settings: "Settings",
    writer: _PackageWriter,
    target_env: str,
    source_env: str,
    notebook_ids: Sequence[str] | None,
    package_id: str,
    exported_at: datetime,
    final_dir: Path,
    warnings: Sequence[str],
) -> ExportReport:
    users = _UserIds()
    table_counts: dict[str, int] = {}
    table_entries: dict[str, dict[str, Any]] = {}

    with source.read() as conn:
        selection = _select_notebooks(source, conn, notebook_ids)
        global_keys = _global_keys(source, conn, selection.exported)
        for table in synced_tables():
            columns, written = _write_table(
                source, conn, writer, table, selection.exported, global_keys, users
            )
            table_counts[table] = written
            table_entries[table] = {
                "columns": list(columns),
                "rows": written,
                "sha256": writer.checksums[rows_path(table)],
            }
        user_count = _write_users(source, conn, writer, sorted(users.ids))
        with writer.lines(DELETES_NAME):
            pass  # PR-2 has no change log, so no deletes to replay.
        with writer.lines(KG_EPOCHS_NAME):
            pass  # ... and no KG epoch resets either.

    # Outside the row snapshot: rows first, then the files they point at (see
    # _write_files).
    file_count, missing_files = _write_files(
        writer, Path(settings.storage_dir), selection.exported
    )
    checksums_sha256 = writer.document(
        CHECKSUMS_NAME, dict(sorted(writer.checksums.items())), checksum=False
    )
    # Written last, and excluded from checksums.json: its presence is the
    # package-complete marker, and it carries checksums.json's own digest so a
    # reader can verify the checksum file before trusting anything in it.
    writer.document(
        MANIFEST_NAME,
        {
            "format_version": PACKAGE_FORMAT_VERSION,
            "package_id": package_id,
            "source_env": source_env,
            "target_env": target_env,
            "created_at": exported_at.isoformat(),
            "schema_pair": {
                "sqlite_version": POSTGRES_SCHEMA_MANIFEST.sqlite_version,
                "postgres_version": POSTGRES_SCHEMA_MANIFEST.postgres_version,
            },
            "embed_runtime_dim": int(settings.embed_runtime_dim),
            "from_seq": 0,
            "to_seq": 0,
            "notebooks": list(selection.exported),
            "tables": table_entries,
            "users": user_count,
            "files": file_count,
            # Package-relative paths the exporter could not read. Carried in
            # the manifest, not only in the operator's report, so an importer
            # can see that this package is knowingly incomplete rather than
            # discovering a dangling attachment later.
            "missing_files": list(missing_files),
            "checksums_sha256": checksums_sha256,
        },
        checksum=False,
    )
    writer.finish()
    writer.root.rename(final_dir)
    _advance_watermark(source, target_env, package_id, exported_at)
    return ExportReport(
        package_dir=final_dir,
        package_id=package_id,
        notebooks=selection.exported,
        skipped=MappingProxyType(dict(sorted(selection.skipped.items()))),
        table_counts=MappingProxyType(dict(sorted(table_counts.items()))),
        file_count=file_count,
        bytes_written=writer.bytes_written,
        mode="full",
        warnings=tuple(warnings),
        missing_files=tuple(missing_files),
    )

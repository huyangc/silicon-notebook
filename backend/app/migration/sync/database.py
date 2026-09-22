"""The source database handle the sync package reads through, and the SQL it
builds around it (docs/incremental-sync-design.md §7/§8).

Split out of ``export.py`` in PR-3b for two reasons that happen to coincide:

- ``incremental.py`` needs these primitives and ``export.py`` needs
  ``incremental.py`` for its mode dispatch. Anything shared has to live BELOW
  both or the two modules form an import cycle.
- ``_Source`` was already the one place the package decides between the two
  backends, and ``import_.py``/``cli.py`` were reaching into ``export.py`` to
  borrow it (``import_`` even aliases it ``_Backend``). §11 registered "three
  separate backend dispatches" as a debt; giving the facade its own module is
  what makes "one dispatch, imported by name" the obvious thing to do.

Layering: this module is part of ``app.migration.sync``, whose import
whitelist (guarded by ``tests/test_sync_manifest.py``) deliberately keeps the
package free of services and repository facades. It therefore talks to the two
``Database`` classes directly and reads its own primary-key catalog, instead of
going through a repository -- an export must be able to run as an offline tool
against a quiesced database without composing the application.

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

import json
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.core.database_url import database_identity
from app.migration.shadow.postgres_catalog import EXPECTED_COLUMNS
from app.migration.sync.manifest import ScopeKind, scope_chain, spec_for
from app.migration.sync.package import encode_value, utc_timestamp_text
from app.repositories.postgres.schema_manifest import POSTGRES_ROWID_ORDINAL_TABLES

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from app.core.config import Settings


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

    def current_snapshot(self, conn: Any) -> str | None:
        """This transaction's visibility snapshot as PostgreSQL's own text
        form (``xmin:xmax:xip1,xip2,...``), or ``None`` on SQLite.

        Must be called on the CALLER'S read connection, inside the window
        ``read()`` opened: under ``REPEATABLE READ`` the snapshot is the one
        every statement of that transaction sees, and it is only meaningful
        as a record of THAT window. Taken on a different connection it would
        describe a different (later) view, and the next export's compensation
        pass would silently skip whatever committed in between.

        The value is stored in ``sync_export_state.exported_snapshot`` so the
        NEXT export can find the transactions that were still in flight here
        and committed afterwards -- their ``sync_change_log`` rows carry a
        ``seq`` below this run's watermark yet became visible only later, so a
        plain ``seq > watermark`` window would skip them forever
        (docs/incremental-sync-design.md §7).

        SQLite has no equivalent and needs none: one writer at a time means
        ``seq`` order is commit order, so the log has no such gap. ``None``
        is the honest answer there, not a placeholder.
        """
        if not self.is_postgres:
            return None
        rows = self.fetch(conn, "SELECT pg_current_snapshot()::text AS snapshot")
        return str(rows[0]["snapshot"])

    @staticmethod
    def snapshot_xmin(snapshot: str) -> int:
        """The ``xmin`` of a stored ``pg_snapshot`` text -- the oldest
        transaction id that was still in flight when it was taken, and
        therefore the lower bound of the next export's compensation window
        (``txid >= xmin``, a range scan over ``idx_sync_change_log_txid``).

        Parsed in Python rather than handed to ``pg_snapshot_xmin()`` because
        the caller needs this number as a BIND PARAMETER for that range scan:
        a function call around the stored text would be opaque to the planner
        at plan time, while a plain integer bound keeps it an index range.
        The snapshot text is a closed, documented format --
        ``xmin:xmax:xip1,xip2,...``, all ASCII decimal, the xip list possibly
        empty -- so anything else is a corrupted or hand-edited watermark row
        and is refused rather than guessed at. The whole string is matched
        against that grammar with ``re.ASCII`` rather than checked field by
        field with ``str.isdigit()``: that predicate is true for superscripts
        and for every non-ASCII decimal script, and ``int()`` accepts only the
        latter -- so ``"²:0:"`` would have passed the check and then raised a
        bare ``ValueError`` out of this function instead of ``SyncExportError``.
        """
        if re.fullmatch(r"\d+:\d+:(?:\d+(?:,\d+)*)?", snapshot, re.ASCII) is None:
            raise SyncExportError(
                "sync_export_state.exported_snapshot is not a PostgreSQL "
                f"snapshot (expected 'xmin:xmax:xip', got {snapshot!r})"
            )
        return int(snapshot.split(":", 1)[0])

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
        constraint whose KEY columns are exactly that column set (order does
        not matter -- uniqueness is a property of the set).

        Used to guard a ``TableSyncSpec.key`` fallback: a registered key is
        only a real row identity if the schema actually enforces it is
        unique, not merely a claim in the manifest. Four kinds of index look
        unique in the catalog but do not enforce what this caller needs, and
        all four are rejected:

        - PARTIAL (``indpred`` / SQLite's ``partial`` flag): uniqueness holds
          only for rows satisfying a predicate this check does not evaluate.
        - EXPRESSION keys (a ``0`` in ``indkey``): what is unique is the value
          of some expression, not the column, so two rows can share the
          column values this caller is about to match on.
        - INCLUDE payload columns (everything past ``indnkeyatts``): they ride
          along in the index but are not part of the uniqueness at all, so an
          index on ``UNIQUE (a) INCLUDE (b)`` must not answer for ``(a, b)``.
        - NOT VALID / NOT READY (``indisvalid``/``indisready``): a failed or
          still-building ``CREATE INDEX CONCURRENTLY`` leaves an index the
          planner ignores and the executor does not enforce.

        SQLite has no INCLUDE columns and no invalid-index state, so only the
        partial rule applies there. Its ``origin='pk'`` entries DO count: for
        a WITHOUT ROWID or composite-primary-key table the primary key is a
        real unique surface, and ``sync_key`` would have returned it from
        ``_catalog_primary_key`` before ever reaching here anyway.
        """
        wanted = frozenset(columns)
        if self.is_postgres:
            rows = self.fetch(
                conn,
                "SELECT i.indexrelid AS relid, i.indnkeyatts AS keyatts, "
                "k.attnum AS attnum, k.ord AS ord, a.attname AS name "
                "FROM pg_index i "
                "CROSS JOIN LATERAL unnest(i.indkey::smallint[]) "
                "WITH ORDINALITY AS k(attnum, ord) "
                "LEFT JOIN pg_attribute a "
                "ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
                "WHERE i.indrelid = ?::regclass AND i.indisunique "
                "AND i.indpred IS NULL AND i.indisvalid AND i.indisready",
                (table,),
            )
            by_index: dict[Any, set[str]] = {}
            expression_indexes: set[Any] = set()
            for row in rows:
                relid = row["relid"]
                if int(row["ord"]) > int(row["keyatts"]):
                    continue  # INCLUDE payload, not part of the uniqueness
                if int(row["attnum"]) == 0:
                    # An expression key column. The whole index is unusable as
                    # a column-set identity, not just this one position.
                    expression_indexes.add(relid)
                    continue
                by_index.setdefault(relid, set()).add(str(row["name"]))
            return any(
                frozenset(cols) == wanted
                for relid, cols in by_index.items()
                if relid not in expression_indexes
            )
        for index in self.fetch(conn, f"PRAGMA index_list({_quoted(table)})"):
            if int(index["unique"]) != 1 or int(index["partial"] or 0) == 1:
                continue
            info = self.fetch(
                conn, f"PRAGMA index_info({_quoted(str(index['name']))})"
            )
            if any(row["name"] is None for row in info):
                continue  # an expression key column, same rule as PostgreSQL
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
        key.

        When both a catalog primary key and a registered ``key`` exist they
        must be the SAME ORDERED TUPLE, not merely the same column set. Order
        is part of the key here for the same reason it is in
        ``app.migration.sync.capture.key_columns``: the capture triggers build
        ``key_json`` by naming the columns in their order, so the same set in
        a different order produces a different JSON object for the same row,
        and a log written under one order matches nothing when read under the
        other. A later migration that gives a registered table a real primary
        key must update the manifest in the same change, and reordering one
        side is exactly as much of a break as renaming a column.

        Every synced table must resolve to a non-empty key; this is a hard
        error rather than an empty-tuple return so a caller can never again
        fall through to an unkeyed code path.
        """
        catalog = self._catalog_primary_key(conn, table)
        declared = spec_for(table).key
        if catalog and declared and tuple(catalog) != tuple(declared):
            raise SyncExportError(
                f"{table}: TableSyncSpec.key disagrees with the catalog "
                f"primary key (order is part of the key): "
                f"manifest={tuple(declared)} catalog={tuple(catalog)}"
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


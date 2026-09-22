"""Full-package import -- the target side of docs/incremental-sync-design.md
§8's ``from_seq = 0`` special case.

``import_package`` applies one package written by ``app.migration.sync.export``
to whichever backend ``settings.database_url`` names. The module name has a
trailing underscore because ``import`` is a keyword; the package path
(``app.migration.sync.import_``) is the only place that shows.

Phases, and what each one actually guarantees:

1. **Preflight.** Everything that can refuse the package. The package's own
   integrity (format version, checksum chain, no incremental payload) is
   filesystem-only and is checked BEFORE any database handle is opened. Then
   one read snapshot answers the questions that need the target: schema pair,
   column superset, "was this package already applied", "is this notebook a
   mirror of some other environment", and the identity mapping.
2. **Identity mapping.** §4. Reads only; users the target lacks are created at
   the start of phase 3, so a dry run creates nothing.
3. **Rows.** One transaction per table, in ``synced_tables()`` order, with the
   table's ``sync_import_progress`` row written inside that same transaction.
   A crash therefore loses at most the table that was in flight, and a re-run
   re-applies only tables that never completed.
4. **Files.** After the rows, because a row is what makes a file meaningful
   and a crash between them must not leave installed bytes with no row. Each
   notebook directory is staged beside its destination and swapped in; the
   directory it replaced is kept as ``.sync-old`` until phase 5 has succeeded,
   and is swapped BACK if anything in between fails.
5. **Finish.** Flip the notebooks this run inserted out of their in-flight
   ``copying`` state, stamp them as mirrors, close the ``sync_imports`` row,
   drop the retired file directories, write the report.

**Atomicity, stated honestly.** There is no transaction spanning the run. Rows
are atomic per table; files are atomic per notebook directory and are rolled
back on failure; the run as a whole is *resumable*, not atomic. A failure
leaves the tables that completed in place, `sync_imports.status='failed'`, and
a report on disk -- and a re-run picks up from there. Failure granularity is
the whole package: a package either ends applied in full or is left resumable.
Nothing partially applies "some notebooks" and reports success.

**A notebook this run inserts is created ``status='copying'``**, which
``NOTEBOOK_LIVE_SQL`` hides, and is flipped to the ordinary ``draft`` in phase
5. A half-imported notebook is therefore never visible. Re-syncing a notebook
the target ALREADY mirrors does not hide it: ``status`` is a target-owned
column (§5) and is never overwritten for an existing row, so an established
mirror stays readable while it is refreshed. That is the accepted trade-off --
the alternative is taking a live mirror offline for every sync.

**A SQLite target must be quiesced.** SQLite has one writer: an import holds
that writer for the duration of each table, and the application's own writes
interleave between tables against a half-applied schema-level view. Stop the
application before importing into SQLite. The report carries a warning saying
so whenever the target is SQLite. A PostgreSQL target does not need this.

Layering: like the exporter, this module talks to the two ``Database`` classes
directly instead of composing a repository facade -- an import must be able to
run as an offline tool against a quiesced target. It reuses the exporter's
backend handle and catalog readers rather than growing a second copy of them
(``tests/test_sync_manifest.py`` guards the package's import whitelist).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from app.migration.shadow.manifest import MANIFEST as _SHADOW_MANIFEST
from app.migration.sync.export import (
    SyncExportError,
    _batched,
    _quoted,
)
from app.migration.sync.export import _Source as _Backend
from app.migration.sync.identity import UserProjection, build_user_mapping
from app.migration.sync.manifest import (
    MappingKind,
    ScopeKind,
    SyncClass,
    SYNC_MANIFEST,
    spec_for,
    synced_tables,
)
from app.migration.sync.package import (
    ASSET_FILES_DIR,
    CHECKSUMS_NAME,
    DELETES_NAME,
    FILES_DIR,
    KG_EPOCHS_NAME,
    MANIFEST_NAME,
    NOTEBOOK_FILES_DIR,
    PACKAGE_FORMAT_VERSION,
    USERS_NAME,
    decode_row,
    json_document,
    rows_path,
)
from app.repositories.postgres.schema_manifest import (
    POSTGRES_EMPTY_TIME_SENTINELS,
    POSTGRES_ROWID_ORDINAL_TABLES,
    POSTGRES_SCHEMA_MANIFEST,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.config import Settings


# Rows applied per ``executemany``. Bounds the driver's parameter buffer for a
# table with millions of chunk rows; the whole table is still ONE transaction.
_ROW_BATCH = 1000

# The lifecycle state a notebook this run INSERTS is created in, and the one
# phase 5 flips it to. ``notebooks.status`` only ever holds 'draft' (the
# default every create path writes), 'copying' or 'deleting'; the first two of
# those are the in-flight markers ``NOTEBOOK_LIVE_SQL`` hides, which is
# exactly what a notebook whose rows are still arriving needs to be. The
# deep-copy path uses the same 'copying' marker for the same reason
# (services/notebook_sharing.py), so no new state had to be invented and the
# existing sweeper's predicate already understands it.
_IMPORT_IN_FLIGHT_STATUS = "copying"
_IMPORT_FINAL_STATUS = "draft"

# A user created by ``--create-missing-users``: no credentials, and the same
# placeholder email shape external-auth enrollment mints
# (``app/repositories/auth_store.py``). Bytes, not characters -- token_hex
# doubles it, matching that enrollment path's id width.
_CREATED_USER_ID_BYTES = 16
_CREATED_USER_EMAIL_DOMAIN = "users.silicon-notebook.local"
# Deliberately NOT the source user's role: a package carries whatever role the
# SOURCE environment gave a person, and honouring it would let an import grant
# administrator rights in the target environment. External-auth enrollment
# writes the same literal for the same reason.
_CREATED_USER_ROLE = "user"

# The importer's own output, written beside the package it applied. Not
# package content: the exporter never checksums it, and preflight ignores
# any file at the package root whose name starts with this.
_REPORT_PREFIX = "import-report-"

# ``sync_import_progress.table_name`` for the file phase. Not a table name and
# can never collide with one (no identifier may contain a leading underscore
# pair in this schema, and the guard tests pin the synced table roster).
_FILES_PHASE = "__files__"

# A ``sync_imports`` row still 'running' this long after it started belongs to
# a process that is gone: no import holds a row open for hours without writing
# progress. Taking it over is reported as a warning, never done silently.
_STALE_RUNNING_HOURS = 6

# Detailed skip entries kept in the report. Beyond this only the count grows,
# so a package that skips a million rows still produces a readable report.
_SKIPPED_ROW_DETAIL_LIMIT = 200

# shadow TableSpec by name -- ``transform_sqlite_value`` needs one to resolve
# a table's path columns and its empty-timestamp sentinels.
_SHADOW_SPECS = {spec.name: spec for spec in _SHADOW_MANIFEST.tables}

# A column holding an absolute filesystem path under ``storage/``, and the
# ``storage/`` subdirectory it lives in. The package carries the SOURCE host's
# absolute path, which means nothing here, so import re-anchors it on this
# environment's storage root -- exactly what ``copy_notebook`` does for the
# same column when it duplicates a notebook within one environment
# (``services/notebook_sharing.py``: ``destination_dir / Path(value).name``).
# Without this every mirrored source row points at a path that does not exist
# on this host. Kept as data, and cross-checked below against the shadow
# manifest's own ``path_columns`` so a newly declared path column cannot slip
# through un-rebased.
_STORAGE_PATH_COLUMNS: dict[tuple[str, str], str] = {
    ("sources", "file_path"): NOTEBOOK_FILES_DIR,
}


def _check_path_columns_are_covered() -> None:
    declared = {
        (spec.name, column)
        for spec in _SHADOW_MANIFEST.tables
        if spec.name in set(synced_tables())
        for column in spec.path_columns
    }
    missing = sorted(declared - set(_STORAGE_PATH_COLUMNS))
    extra = sorted(set(_STORAGE_PATH_COLUMNS) - declared)
    if missing or extra:
        raise ValueError(
            "_STORAGE_PATH_COLUMNS must cover exactly the shadow manifest's "
            f"path columns on synced tables: missing={missing}, stale={extra}"
        )


class SyncImportError(RuntimeError):
    """A package could not be applied. Every preflight rejection and every
    unrecoverable row-phase failure raises this with a reason that names the
    table, column or file at fault."""


class UnmappedPolicy(StrEnum):
    """What to do with a mapped column whose source id has no target
    counterpart. One value per rule in design doc §3.2."""

    # Fail the whole import: this row cannot exist without its referent.
    FAIL = "fail"
    # Drop this one row and log it; the rest of the table still applies.
    SKIP = "skip"
    # Keep the row, clear the column (the column is nullable by schema).
    NULL = "null"
    # Keep the row, attribute the column to whoever ran the import.
    IMPORTER = "importer"


# Every (table, column) that carries a user/group/principal id, and what an
# unmappable value there means. This is the design doc's §3.2 table restated
# as data: a (table, column) missing from here is a hard failure rather than
# a silent default, and ``_check_policy_is_total`` below refuses to import
# this module at all if the manifest and this table disagree.
_UNMAPPED_POLICY: dict[tuple[str, str], UnmappedPolicy] = {
    # The notebook cannot belong to nobody; without its creator the whole
    # notebook is refused (design doc §3.2).
    ("notebooks", "created_by"): UnmappedPolicy.FAIL,
    ("notebook_members", "user_id"): UnmappedPolicy.SKIP,
    # principal_id is polymorphic: an unmappable user OR group principal
    # drops the grant. 'everyone' is not an identity and never reaches here.
    ("notebook_grants", "principal_id"): UnmappedPolicy.SKIP,
    ("notebook_grants", "created_by"): UnmappedPolicy.IMPORTER,
    ("notebook_bases", "created_by"): UnmappedPolicy.IMPORTER,
    ("notebook_object_schemas", "created_by"): UnmappedPolicy.IMPORTER,
    ("notebook_assets", "created_by"): UnmappedPolicy.IMPORTER,
    # Nullable since 0045; rows written before it already carry NULL, so an
    # unknown uploader is a shape the read paths already handle.
    ("sources", "uploaded_by"): UnmappedPolicy.NULL,
    ("knowhow_tables", "created_by"): UnmappedPolicy.IMPORTER,
    ("knowhow_cell_code", "updated_by"): UnmappedPolicy.IMPORTER,
    ("knowhow_milestones", "created_by"): UnmappedPolicy.IMPORTER,
    ("knowhow_changes", "actor"): UnmappedPolicy.IMPORTER,
    ("groups", "created_by"): UnmappedPolicy.IMPORTER,
    ("groups", "invite_created_by"): UnmappedPolicy.NULL,
    # Design doc §3.2 offers "an admin member of that group at the target"
    # before falling back to the importer. That leg is unreachable for the
    # only case that ever writes this column: groups is seed_only, so an
    # existing target group is never updated, and a group this import
    # CREATES has no members yet (group_members is applied after groups).
    # The reachable rule is therefore the fallback, written as the rule.
    ("groups", "owner_id"): UnmappedPolicy.IMPORTER,
    # An unmappable group means the membership has no group to belong to.
    ("group_members", "group_id"): UnmappedPolicy.SKIP,
    ("group_members", "user_id"): UnmappedPolicy.SKIP,
    ("group_members", "added_by"): UnmappedPolicy.IMPORTER,
    # Same reasoning as notebooks.created_by: a memory with no author is not
    # a memory this environment can show or govern (design doc §3.2).
    ("memory_items", "created_by"): UnmappedPolicy.FAIL,
    ("memory_items", "confirmed_by"): UnmappedPolicy.NULL,
    ("memory_revisions", "changed_by"): UnmappedPolicy.IMPORTER,
}


def _check_policy_is_total() -> None:
    declared = {
        (spec.name, column.name)
        for spec in SYNC_MANIFEST
        if spec.sync_class is not SyncClass.LOCAL
        for column in spec.mapped_columns
    }
    missing = sorted(declared - set(_UNMAPPED_POLICY))
    extra = sorted(set(_UNMAPPED_POLICY) - declared)
    if missing or extra:
        raise ValueError(
            "_UNMAPPED_POLICY must cover exactly the sync manifest's mapped "
            f"columns: missing={missing}, stale={extra}. Add the rule in the "
            "same change as the manifest entry (docs/incremental-sync-design.md §3.2)."
        )


_check_policy_is_total()
_check_path_columns_are_covered()


# ------------------------------------------------------------------- report


@dataclass(frozen=True)
class TableOutcome:
    """Per-table row accounting. ``inserted + updated + skipped`` always equals
    the number of rows the package carries for that table, so a reader can
    reconcile the report against ``manifest.json`` without a second pass over
    the data -- except for a table ``resumed`` from an earlier run's
    ``sync_import_progress``, which this run did not read at all.

    ``inserted`` is decided by a primary-key probe before the write, which is
    exact for every table except a ``seed_only`` one: there, ``ON CONFLICT DO
    NOTHING`` carries no conflict target on purpose, so a row that collides on
    some OTHER unique index is dropped by the database while this counter has
    already called it inserted. The only such index in the synced set today is
    none at all, but the counter is documented as an upper bound rather than
    quietly assumed exact."""

    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    resumed: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped": self.skipped,
            "resumed": self.resumed,
        }


@dataclass(frozen=True)
class SkippedRow:
    table: str
    key: str
    reason: str

    def as_json(self) -> dict[str, str]:
        return {"table": self.table, "key": self.key, "reason": self.reason}


@dataclass(frozen=True)
class UserMappingResult:
    """§4's mapping outcome. ``matched``/``created`` are source-user-id ->
    target-user-id; ``unmatched`` names the usernames that stayed unresolved
    (only possible with ``create_missing_users=False``)."""

    matched: Mapping[str, str]
    created: Mapping[str, str]
    unmatched: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "matched": len(self.matched),
            "created": sorted(self.created.values()),
            "unmatched": list(self.unmatched),
        }


@dataclass(frozen=True)
class ImportReport:
    package_id: str
    source_env: str
    already_applied: bool
    dry_run: bool
    notebooks: tuple[str, ...]
    tables: Mapping[str, TableOutcome]
    # Capped at _SKIPPED_ROW_DETAIL_LIMIT; skipped_row_total is the real count.
    skipped_rows: tuple[SkippedRow, ...]
    skipped_row_total: int
    user_mapping: UserMappingResult
    groups_created: tuple[str, ...]
    files_copied: int
    warnings: tuple[str, ...]
    # Empty on success; the failure's message when the run could not finish.
    error: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "source_env": self.source_env,
            "already_applied": self.already_applied,
            "dry_run": self.dry_run,
            "notebooks": list(self.notebooks),
            "tables": {
                name: outcome.as_json() for name, outcome in sorted(self.tables.items())
            },
            "skipped_rows": [row.as_json() for row in self.skipped_rows],
            "skipped_row_total": self.skipped_row_total,
            "user_mapping": self.user_mapping.as_json(),
            "groups_created": list(self.groups_created),
            "files_copied": self.files_copied,
            "warnings": list(self.warnings),
            "error": self.error,
        }


class _Ledger:
    """Mutable accumulator the phases write into; frozen into an
    ``ImportReport`` once the run ends (successfully or not)."""

    def __init__(self) -> None:
        self.tables: dict[str, TableOutcome] = {}
        self.skipped_rows: list[SkippedRow] = []
        self.skipped_row_total = 0
        self.groups_created: list[str] = []
        self.files_copied = 0
        self.warnings: list[str] = []
        self.missing_files: list[str] = []

    def missing_file(self, label: str) -> None:
        """A row points at a ``storage/`` file this package did not carry.
        Collected rather than warned per row, so one deleted upload does not
        produce one warning per row that mentions it."""
        if len(self.missing_files) < _SKIPPED_ROW_DETAIL_LIMIT:
            self.missing_files.append(label)

    def skip(self, table: str, key: str, reason: str) -> None:
        self.skipped_row_total += 1
        if len(self.skipped_rows) < _SKIPPED_ROW_DETAIL_LIMIT:
            self.skipped_rows.append(SkippedRow(table, key, reason))

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


def _report(
    manifest: "_PackageManifest",
    ledger: _Ledger,
    mapping: UserMappingResult,
    *,
    already_applied: bool = False,
    dry_run: bool = False,
    error: str = "",
) -> ImportReport:
    warnings = list(ledger.warnings)
    if ledger.missing_files:
        warnings.append(
            f"{len(ledger.missing_files)} imported row(s) point at a storage "
            "file this package does not carry; the row is imported and the "
            f"file stays absent: {sorted(ledger.missing_files)[:10]}"
        )
    return ImportReport(
        package_id=manifest.package_id,
        source_env=manifest.source_env,
        already_applied=already_applied,
        dry_run=dry_run,
        notebooks=manifest.notebooks,
        tables=MappingProxyType(dict(sorted(ledger.tables.items()))),
        skipped_rows=tuple(ledger.skipped_rows),
        skipped_row_total=ledger.skipped_row_total,
        user_mapping=mapping,
        groups_created=tuple(sorted(ledger.groups_created)),
        files_copied=ledger.files_copied,
        warnings=tuple(warnings),
        error=error,
    )


_EMPTY_MAPPING = UserMappingResult(
    matched=MappingProxyType({}), created=MappingProxyType({}), unmatched=()
)


# ------------------------------------------------------------ package reader


@dataclass(frozen=True)
class _TableEntry:
    columns: tuple[str, ...]
    rows: int
    sha256: str


@dataclass(frozen=True)
class _PackageManifest:
    package_id: str
    source_env: str
    target_env: str
    from_seq: int
    to_seq: int
    embed_runtime_dim: int
    sqlite_version: int
    postgres_version: int
    notebooks: tuple[str, ...]
    tables: Mapping[str, _TableEntry]
    # sha256 of checksums.json itself. manifest.json is the package-complete
    # marker and is the one file checksums.json cannot cover, so this is what
    # makes the checksum chain closed: verify this, and every per-file digest
    # inside checksums.json is then trustworthy.
    checksums_sha256: str
    # Package-relative paths the exporter meant to carry but could not read
    # (deleted by a concurrent edit while the export ran). Reported, never
    # fatal: the rows that point at them still import.
    missing_files: tuple[str, ...]


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise SyncImportError(f"package is missing {path.name}: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SyncImportError(f"package file is not valid JSON: {path}") from exc


def _read_manifest(package_dir: Path) -> _PackageManifest:
    document = _read_json(package_dir / MANIFEST_NAME)
    if not isinstance(document, dict):
        raise SyncImportError(f"{MANIFEST_NAME} is not a JSON object")
    version = document.get("format_version")
    if version != PACKAGE_FORMAT_VERSION:
        raise SyncImportError(
            f"unsupported package format_version {version!r}; this build reads "
            f"{PACKAGE_FORMAT_VERSION}"
        )
    pair = document.get("schema_pair") or {}
    tables = {}
    for name, entry in (document.get("tables") or {}).items():
        tables[str(name)] = _TableEntry(
            columns=tuple(str(column) for column in entry.get("columns") or ()),
            rows=int(entry.get("rows") or 0),
            sha256=str(entry.get("sha256") or ""),
        )
    return _PackageManifest(
        package_id=str(document.get("package_id") or ""),
        source_env=str(document.get("source_env") or ""),
        target_env=str(document.get("target_env") or ""),
        from_seq=int(document.get("from_seq") or 0),
        to_seq=int(document.get("to_seq") or 0),
        embed_runtime_dim=int(document.get("embed_runtime_dim") or 0),
        sqlite_version=int(pair.get("sqlite_version") or 0),
        postgres_version=int(pair.get("postgres_version") or 0),
        notebooks=tuple(str(item) for item in document.get("notebooks") or ()),
        tables=MappingProxyType(tables),
        checksums_sha256=str(document.get("checksums_sha256") or ""),
        missing_files=tuple(str(item) for item in document.get("missing_files") or ()),
    )


def _iter_lines(path: Path) -> Iterator[dict[str, Any]]:
    """Stream one ``*.jsonl`` file. A missing file is an empty table: the
    exporter writes one per synced table, but a package produced by a build
    with a smaller manifest must not crash the reader before preflight has
    had its say."""
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except ValueError as exc:
                raise SyncImportError(f"{path.name}:{number} is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise SyncImportError(f"{path.name}:{number} is not a JSON object")
            yield payload


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------- target SQL


def _ident(name: str) -> str:
    """The exporter's catalog-identifier guard, re-raised in this module's
    error type so a caller only ever has to catch ``SyncImportError``."""
    try:
        return _quoted(name)
    except SyncExportError as exc:
        raise SyncImportError(str(exc)) from None


def _moment(backend: _Backend, value: datetime) -> Any:
    """A timestamp in the shape the target's control tables take: an aware
    ``datetime`` for PostgreSQL's ``timestamptz``, ISO text for SQLite."""
    return value if backend.is_postgres else value.isoformat()


def _read_moment(value: Any) -> datetime | None:
    """Parse a control-table timestamp back, whichever backend wrote it."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _executemany(
    backend: _Backend, conn: Any, statement: str, rows: Sequence[Sequence[Any]]
) -> None:
    if not rows:
        return
    prepared = backend.sql(statement)
    if backend.is_postgres:
        with conn.cursor() as cursor:
            cursor.executemany(prepared, rows)
        return
    conn.executemany(prepared, rows)


def _postgres_columns(backend: _Backend, conn: Any, table: str):
    """``PostgresColumn`` per column name for ``transform_sqlite_value``.

    Read from the LIVE ``information_schema`` -- the same read
    ``shadow/bulk_copy.py::_postgres_columns`` makes for the same purpose --
    and cross-checked against the type contract the shadow migration parses
    out of the migration DDL (``postgres_catalog.EXPECTED_COLUMNS``), which is
    the source the exporter decides jsonb/timestamptz columns from. A
    disagreement between the two is schema drift this import must not paper
    over, so it is a hard failure naming the column.

    The live catalog is the authority rather than the contract because this
    module is the one that WRITES: the constraint a value has to satisfy is
    the one the target database will actually enforce, and nullability and the
    column set are facts about that database. The contract's job here is to
    catch the case where the two have diverged, not to stand in for the
    database.
    """
    from app.migration.shadow.postgres_catalog import EXPECTED_COLUMNS
    from app.migration.shadow.transform import PostgresColumn

    contracts = EXPECTED_COLUMNS.get(table) or {}
    rows = backend.fetch(
        conn,
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ? "
        "ORDER BY ordinal_position",
        (table,),
    )
    if not rows:
        raise SyncImportError(f"target database has no table {table!r}")
    columns: dict[str, Any] = {}
    for row in rows:
        name = str(row["column_name"])
        data_type = str(row["data_type"])
        contract = contracts.get(name)
        if contract is not None and contract.data_type != data_type:
            raise SyncImportError(
                f"{table}.{name}: the target column is {data_type!r} but the "
                f"migration DDL contract says {contract.data_type!r}; the "
                "target's schema has drifted from its migrations"
            )
        columns[name] = PostgresColumn(
            name=name, data_type=data_type, nullable=str(row["is_nullable"]) == "YES"
        )
    return columns


def _upsert_statement(
    table: str,
    columns: Sequence[str],
    primary_key: Sequence[str],
    update_columns: Sequence[str],
    *,
    seed_only: bool,
) -> str:
    names = ", ".join(_ident(column) for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    head = f"INSERT INTO {_ident(table)} ({names}) VALUES ({placeholders})"
    if not primary_key:
        # No conflict target to name. Only reachable for the primary-key-less
        # tables handled by the replace path below, which has already removed
        # the rows this insert is about to write.
        return head
    if seed_only:
        # Bare DO NOTHING rather than a named conflict target: a seed_only
        # table must not overwrite ANY pre-existing row, including one that
        # collides on a unique index other than the primary key.
        return f"{head} ON CONFLICT DO NOTHING"
    conflict = ", ".join(_ident(column) for column in primary_key)
    if not update_columns:
        return f"{head} ON CONFLICT ({conflict}) DO NOTHING"
    assignments = ", ".join(
        f"{_ident(column)} = excluded.{_ident(column)}" for column in update_columns
    )
    return f"{head} ON CONFLICT ({conflict}) DO UPDATE SET {assignments}"


class _TargetKeys:
    """Which primary keys this table already holds at the target, so the
    report can tell an insert from an update without asking either driver for
    per-row affected counts (neither reports that usefully through
    ``executemany``).

    The probe is shaped by the table's scope, so the total work is linear in
    the rows this package touches rather than quadratic in the batch count:

    - NOTEBOOK-scoped: ONE query up front for the package's notebooks. These
      are the big tables, and every one of them has a notebook column to
      filter on, so the set is bounded by what this import is about to write.
    - GLOBAL-scoped: ONE query for the whole table. The three GLOBAL tables
      (groups, group_members, object_schemas) are environment-level registries
      whose size is bounded by the organisation, not by content.
    - PARENT-scoped: probed per batch. Every PARENT-scoped table has a
      single-column primary key, so ``WHERE pk IN (batch)`` returns at most
      one row per row in the batch -- exact, and linear. (Asserted: a future
      composite-key PARENT table has to choose a probe deliberately.)
    """

    def __init__(
        self, backend: _Backend, conn: Any, table: str, primary_key: Sequence[str],
        context: "_Context",
    ) -> None:
        self._backend = backend
        self._conn = conn
        self._table = table
        self._key = tuple(primary_key)
        self._preloaded: set[tuple[Any, ...]] | None = None
        if not self._key:
            self._preloaded = set()
            return
        scope = spec_for(table).scope
        if scope is None:
            raise SyncImportError(f"{table}: LOCAL table has no import scope")
        if scope.kind is ScopeKind.NOTEBOOK:
            self._preloaded = self._select(
                f"WHERE {_ident(scope.column)} IN ({{placeholders}})",
                list(context.manifest.notebooks),
            )
        elif scope.kind is ScopeKind.GLOBAL:
            self._preloaded = self._select("", None)
        elif len(self._key) != 1:
            raise SyncImportError(
                f"{table}: parent-scoped with the composite primary key "
                f"{self._key}; its existence probe has to be chosen explicitly"
            )

    def _select(
        self, where: str, values: Sequence[Any] | None
    ) -> set[tuple[Any, ...]]:
        projection = ", ".join(_ident(column) for column in self._key)
        head = f"SELECT {projection} FROM {_ident(self._table)}"
        found: set[tuple[Any, ...]] = set()
        if values is None:
            for row in self._backend.stream(self._conn, head):
                found.add(tuple(row[column] for column in self._key))
            return found
        for chunk in _batched(values):
            placeholders = ",".join("?" for _ in chunk)
            statement = f"{head} {where.format(placeholders=placeholders)}"
            for row in self._backend.fetch(self._conn, statement, chunk):
                found.add(tuple(row[column] for column in self._key))
        return found

    def present(self, keys: Sequence[tuple[Any, ...]]) -> set[tuple[Any, ...]]:
        if self._preloaded is not None:
            return {key for key in keys if key in self._preloaded}
        return self._select(
            f"WHERE {_ident(self._key[0])} IN ({{placeholders}})",
            [key[0] for key in keys],
        )

    def note_inserted(self, keys: Sequence[tuple[Any, ...]]) -> None:
        """Keys this run has just written. Only matters for a package that
        repeats a key within one table, which a package produced from a
        primary-keyed table never does -- recorded anyway so the counters
        cannot drift if one ever did."""
        if self._preloaded is not None:
            self._preloaded.update(keys)


def _existing_ids(
    backend: _Backend, conn: Any, table: str, column: str, values: Sequence[Any]
) -> set[Any]:
    if not values:
        return set()
    found: set[Any] = set()
    for chunk in _batched(sorted({value for value in values}, key=repr)):
        placeholders = ",".join("?" for _ in chunk)
        for row in backend.fetch(
            conn,
            f"SELECT {_ident(column)} AS value FROM {_ident(table)} "
            f"WHERE {_ident(column)} IN ({placeholders})",
            chunk,
        ):
            found.add(row["value"])
    return found


# ------------------------------------------------------------------ context


@dataclass
class _Context:
    backend: _Backend
    settings: "Settings"
    package_dir: Path
    manifest: _PackageManifest
    ledger: _Ledger
    # checksums.json as preflight verified it. Kept so the file phase and the
    # storage-path rewrite can ask "does the package carry this file" without
    # a second pass over the bytes.
    checksums: Mapping[str, str] = field(default_factory=dict)
    users: dict[str, str] = field(default_factory=dict)
    groups: dict[str, str] = field(default_factory=dict)
    importer_user_id: str = ""
    # Notebook directories swapped in by the file phase, as
    # (destination, retired) -- retired is kept until phase 5 succeeds.
    installed: list[tuple[Path, Path]] = field(default_factory=list)


def _row_key(row: Mapping[str, Any], primary_key: Sequence[str]) -> str:
    if not primary_key:
        return "<no primary key>"
    return "/".join(str(row.get(column)) for column in primary_key)


# A table whose own PRIMARY KEY is an identity the import remaps. Only
# ``groups``: a group is matched to the target by NAME (§4), so the target's
# row for "the same group" carries a different id, and the package row has to
# be rewritten onto it. Without this, a name-matched group would be inserted a
# second time under its source id -- ``groups.name`` has no unique index to
# stop it -- and the target would hold two groups with one name, which
# ``_build_group_mapping`` then refuses to map on the NEXT import.
_SELF_MAPPED_KEY: dict[str, tuple[str, MappingKind]] = {
    "groups": ("id", MappingKind.GROUP),
}


def _map_row(
    table: str, row: dict[str, Any], context: _Context
) -> tuple[dict[str, Any] | None, str]:
    """Rewrite one decoded package row for the target. Returns ``(row, "")``
    when it should be written, or ``(None, reason)`` when the manifest's rule
    for an unmappable reference says to drop it. Raises ``SyncImportError``
    for the FAIL rule."""
    spec = spec_for(table)
    key_column = _SELF_MAPPED_KEY.get(table)
    if key_column is not None:
        name, kind = key_column
        source_id = row.get(name)
        by_kind = context.groups if kind is MappingKind.GROUP else context.users
        if isinstance(source_id, str) and source_id:
            row[name] = by_kind.get(source_id, source_id)
    for column in spec.mapped_columns:
        value = row.get(column.name)
        if not isinstance(value, str) or not value:
            # Empty means "nobody" and is a shape every one of these columns
            # already allows; there is nothing to map.
            continue
        if column.kind is MappingKind.USER:
            resolved = context.users.get(value)
        elif column.kind is MappingKind.GROUP:
            resolved = context.groups.get(value)
        else:
            principal = str(row.get("principal_type") or "")
            if principal == "user":
                resolved = context.users.get(value)
            elif principal == "group":
                resolved = context.groups.get(value)
            else:
                # 'everyone' (and any future non-identity principal) is not an
                # id at all -- it travels verbatim.
                continue
        if resolved is not None:
            row[column.name] = resolved
            continue
        policy = _UNMAPPED_POLICY.get((table, column.name))
        if policy is None:
            raise SyncImportError(
                f"{table}.{column.name} has no _UNMAPPED_POLICY rule; refusing "
                "to guess what an unmappable reference there means"
            )
        if policy is UnmappedPolicy.FAIL:
            raise SyncImportError(
                f"{table}.{column.name}={value!r} has no counterpart in the "
                "target environment and its rule is 'fail' "
                "(docs/incremental-sync-design.md §3.2). Re-run with "
                "--create-missing-users, or create that user first."
            )
        if policy is UnmappedPolicy.SKIP:
            return None, f"{column.name}={value!r} maps to no target identity"
        if policy is UnmappedPolicy.NULL:
            row[column.name] = None
        else:
            row[column.name] = context.importer_user_id
    for column in spec.severed_columns:
        if column in row:
            row[column] = None
    _rebase_storage_paths(table, row, context)
    return row, ""


def _rebase_storage_paths(table: str, row: dict[str, Any], context: _Context) -> None:
    """Re-anchor this row's ``storage/`` paths on THIS environment.

    The package carries the source host's absolute path, which is meaningless
    here; the bytes land under this environment's own storage root keeping
    their basename (the file phase runs after this one, so the check is
    against what the PACKAGE carries, not against what is installed yet).
    A file the package does not carry is recorded as a warning rather than a
    failure -- the exporter reports the same gap in ``missing_files``, and a
    source row whose upload was already deleted is a shape this environment
    has to tolerate anyway."""
    storage = Path(context.settings.storage_dir)
    for (owner, column), root in _STORAGE_PATH_COLUMNS.items():
        if owner != table:
            continue
        value = row.get(column)
        if not isinstance(value, str) or not value:
            continue
        notebook_id = str(row.get("notebook_id") or "")
        if not notebook_id:
            raise SyncImportError(
                f"{table}.{column} carries a path but the row has no "
                "notebook_id to re-anchor it under"
            )
        name = Path(value).name
        row[column] = str(storage / root / notebook_id / name)
        if f"{FILES_DIR}/{root}/{notebook_id}/{name}" not in context.checksums:
            context.ledger.missing_file(f"{table}.{column}: {name}")


def _apply_optional_refs(
    table: str, rows: list[dict[str, Any]], context: _Context, conn: Any
) -> list[dict[str, Any]]:
    """Drop rows whose ``optional_refs`` point at a row the target does not
    have. Checked per batch, against the target's live state, so a reference
    satisfied by an EARLIER table of this same import resolves."""
    spec = spec_for(table)
    if not spec.optional_refs or not rows:
        return rows
    kept = rows
    own_key = context.backend.primary_key(conn, table)
    for column, referenced in spec.optional_refs:
        key = context.backend.primary_key(conn, referenced)
        if len(key) != 1:
            raise SyncImportError(
                f"{table}.{column}: optional ref into {referenced!r} whose "
                f"primary key {key} is not a single column"
            )
        wanted = [
            row[column]
            for row in kept
            if isinstance(row.get(column), str) and row[column]
        ]
        present = _existing_ids(context.backend, conn, referenced, key[0], wanted)
        surviving: list[dict[str, Any]] = []
        for row in kept:
            value = row.get(column)
            if isinstance(value, str) and value and value not in present:
                context.ledger.skip(
                    table,
                    _row_key(row, own_key),
                    f"{column}={value!r} is not present in {referenced}",
                )
                continue
            surviving.append(row)
        kept = surviving
    return kept


# ------------------------------------------------- phase 1a -- the package alone


def _verify_checksums(package_dir: Path, manifest: _PackageManifest) -> dict[str, str]:
    """Verify the package's integrity chain and return ``checksums.json``.

    Filesystem only -- no database handle is open while this runs, so a large
    package's hashing never holds a read snapshot (PostgreSQL) or a WAL reader
    (SQLite) for the duration.
    """
    checksums_path = package_dir / CHECKSUMS_NAME
    if not checksums_path.is_file():
        raise SyncImportError(f"package is missing {CHECKSUMS_NAME}: {checksums_path}")
    # Closes the chain before anything inside checksums.json is believed: the
    # manifest (written last, and itself unchecksummed) vouches for
    # checksums.json, and checksums.json vouches for every other file.
    if not manifest.checksums_sha256:
        raise SyncImportError(
            f"{MANIFEST_NAME} carries no checksums_sha256; the package's "
            "integrity chain is open and it cannot be verified"
        )
    actual = _digest(checksums_path)
    if actual != manifest.checksums_sha256:
        raise SyncImportError(
            f"{CHECKSUMS_NAME} does not match manifest.checksums_sha256 "
            f"(found {actual}, expected {manifest.checksums_sha256})"
        )
    recorded = _read_json(checksums_path)
    if not isinstance(recorded, dict):
        raise SyncImportError(f"{CHECKSUMS_NAME} is not a JSON object")
    on_disk = {
        relative
        for relative in (
            path.relative_to(package_dir).as_posix()
            for path in package_dir.rglob("*")
            if path.is_file()
        )
        # An earlier import of this same package (this environment's, or
        # another target reading the same directory) left its report beside
        # the package. That is import OUTPUT, not package content: it is
        # neither checksummed by the exporter nor an unrecorded file smuggled
        # into the package.
        if not relative.startswith(_REPORT_PREFIX)
    } - {MANIFEST_NAME, CHECKSUMS_NAME}
    missing = sorted(set(recorded) - on_disk)
    extra = sorted(on_disk - set(recorded))
    if missing or extra:
        raise SyncImportError(
            f"package file set does not match {CHECKSUMS_NAME}: "
            f"missing={missing}, unrecorded={extra}"
        )
    corrupt = [
        relative
        for relative, digest in sorted(recorded.items())
        if _digest(package_dir / relative) != digest
    ]
    if corrupt:
        raise SyncImportError(f"package files failed their sha256 check: {corrupt}")
    mismatched = [
        table
        for table, entry in sorted(manifest.tables.items())
        if entry.sha256 != recorded.get(rows_path(table))
    ]
    if mismatched:
        raise SyncImportError(
            "manifest.json's per-table sha256 disagrees with checksums.json "
            f"for: {mismatched}"
        )
    return {str(key): str(value) for key, value in recorded.items()}


def _reject_incremental_payload(package_dir: Path) -> None:
    for name in (DELETES_NAME, KG_EPOCHS_NAME):
        if any(True for _ in _iter_lines(package_dir / name)):
            raise SyncImportError(
                f"{name} is not empty: this build imports full packages only "
                "(docs/incremental-sync-design.md §10, PR-3 adds the "
                "incremental path)"
            )


# --------------------------------------------------- phase 1b -- against the target


def _verify_columns(backend: _Backend, conn: Any, manifest: _PackageManifest) -> None:
    """Every column the package carries must exist at the target.

    Deliberately one-directional. A target column the package does NOT carry
    is fine and expected -- it takes the target's default on insert and keeps
    its current value on update (that is how a target running a newer build,
    or a LOCAL-only column, behaves). What cannot be tolerated is a column the
    package has and the target does not: its value would have nowhere to go.
    """
    problems: list[str] = []
    for table in synced_tables():
        entry = manifest.tables.get(table)
        if entry is None:
            problems.append(f"{table}: absent from the package manifest")
            continue
        try:
            target = set(backend.columns(conn, table))
        except SyncExportError as exc:
            problems.append(str(exc))
            continue
        unknown = sorted(set(entry.columns) - target)
        if unknown:
            problems.append(f"{table}: target has no column(s) {unknown}")
    if problems:
        raise SyncImportError(
            "package columns are not a subset of the target schema: "
            + "; ".join(problems)
        )


def _preflight(backend: _Backend, conn: Any, context: _Context) -> bool:
    """The refusals that need the target. Returns True when this package was
    already applied in full. The package's own integrity has already been
    checked, outside any transaction."""
    manifest = context.manifest
    if not manifest.package_id or not manifest.source_env:
        raise SyncImportError("package manifest carries no package_id/source_env")
    if (manifest.sqlite_version, manifest.postgres_version) != (
        POSTGRES_SCHEMA_MANIFEST.sqlite_version,
        POSTGRES_SCHEMA_MANIFEST.postgres_version,
    ):
        raise SyncImportError(
            "schema pair mismatch: package "
            f"(sqlite={manifest.sqlite_version}, pg={manifest.postgres_version}) "
            f"vs target (sqlite={POSTGRES_SCHEMA_MANIFEST.sqlite_version}, "
            f"pg={POSTGRES_SCHEMA_MANIFEST.postgres_version})"
        )
    target_dim = int(context.settings.embed_runtime_dim)
    if manifest.embed_runtime_dim != target_dim:
        raise SyncImportError(
            f"EMBED_RUNTIME_DIM mismatch: package {manifest.embed_runtime_dim} "
            f"vs target {target_dim}; vectors cannot be carried across "
            "(docs/incremental-sync-design.md §6)"
        )
    _verify_columns(backend, conn, manifest)

    for row in backend.fetch(
        conn,
        "SELECT status FROM sync_imports WHERE package_id = ?",
        (manifest.package_id,),
    ):
        if str(row["status"]) == "done":
            return True

    foreign: list[str] = []
    for batch in _batched(list(manifest.notebooks)):
        placeholders = ",".join("?" for _ in batch)
        for row in backend.fetch(
            conn,
            f"SELECT id, sync_origin FROM notebooks WHERE id IN ({placeholders})",
            batch,
        ):
            origin = str(row["sync_origin"] or "")
            if origin != manifest.source_env:
                foreign.append(
                    f"{row['id']} (sync_origin="
                    f"{origin!r}, package source_env={manifest.source_env!r})"
                )
    if foreign:
        raise SyncImportError(
            "the target already has these notebook ids and they are not "
            f"mirrors of this package's source environment: {sorted(foreign)}. "
            "A local notebook, or a mirror of a different environment, is "
            "never overwritten by an import."
        )
    return False


# ------------------------------------------------- phase 2 -- identity mapping


def _target_users(backend: _Backend, conn: Any) -> list[UserProjection]:
    return [
        UserProjection(
            id=str(row["id"]),
            username=str(row["username"] or ""),
            display_name=str(row["display_name"] or ""),
            role=str(row["role"] or ""),
        )
        for row in backend.stream(
            conn, "SELECT id, username, display_name, role FROM users"
        )
    ]


def _package_users(package_dir: Path) -> list[UserProjection]:
    return [
        UserProjection(
            id=str(row.get("id") or ""),
            username=str(row.get("username") or ""),
            display_name=str(row.get("display_name") or ""),
            role=str(row.get("role") or ""),
        )
        for row in _iter_lines(package_dir / USERS_NAME)
    ]


def _resolve_importer(
    backend: _Backend, conn: Any, importer_user_id: str | None
) -> str:
    if importer_user_id:
        rows = backend.fetch(
            conn, "SELECT id FROM users WHERE id = ?", (importer_user_id,)
        )
        if not rows:
            raise SyncImportError(
                f"importer_user_id {importer_user_id!r} does not exist at the target"
            )
        return importer_user_id
    rows = backend.fetch(
        conn,
        "SELECT id FROM users WHERE role = 'admin' ORDER BY created_at, id LIMIT 1",
    )
    if not rows:
        raise SyncImportError(
            "the target environment has no role='admin' user to attribute "
            "imported rows to; pass importer_user_id explicitly"
        )
    return str(rows[0]["id"])


def _build_group_mapping(
    backend: _Backend, conn: Any, context: _Context
) -> tuple[dict[str, str], list[str]]:
    """``source group id -> target group id`` plus the names this import will
    have to CREATE. Groups match by name (design doc §4).

    Two ambiguities are handled rather than assumed away, and both are scoped
    to the names the package actually carries -- an unrelated duplicate
    elsewhere in the target's group list is none of this import's business:

    - two TARGET groups sharing a name the package uses: refused, because
      "match by name" cannot choose between them;
    - two SOURCE groups sharing one name: folded onto a single target group
      and reported, because inserting both would create the first ambiguity
      at the target and poison every later import.

    Nothing is written here -- creation happens with the rest of the
    ``groups`` table in the row phase, so a dry run creates no group."""
    package_rows = [
        (str(row.get("id") or ""), str(row.get("name") or ""))
        for row in _iter_lines(context.package_dir / rows_path("groups"))
    ]
    wanted = {name for _id, name in package_rows if name}
    by_name: dict[str, str] = {}
    existing_ids: set[str] = set()
    for row in backend.stream(conn, "SELECT id, name FROM groups"):
        group_id, name = str(row["id"]), str(row["name"] or "")
        existing_ids.add(group_id)
        if name not in wanted:
            continue
        if name in by_name:
            raise SyncImportError(
                f"the target has more than one group named {name!r}, which "
                "this package needs to map; group mapping is by name and "
                "cannot choose between them"
            )
        by_name[name] = group_id

    mapping: dict[str, str] = {}
    created: list[str] = []
    minted: dict[str, str] = {}
    for group_id, name in package_rows:
        if not group_id:
            continue
        target = by_name.get(name)
        if target is not None:
            mapping[group_id] = target
            continue
        folded = minted.get(name)
        if folded is not None:
            mapping[group_id] = folded
            context.ledger.warn(
                f"the package carries more than one group named {name!r}; they "
                "were folded onto one group at the target rather than "
                "inserted twice under a name that cannot be mapped again"
            )
            continue
        if group_id in existing_ids:
            raise SyncImportError(
                f"group id {group_id!r} already exists at the target under a "
                f"different name than the package's {name!r}; refusing to "
                "merge two unrelated groups"
            )
        # Created in the row phase under its own source id: ids are
        # uuid4-wide, so reusing it keeps group_members/notebook_grants
        # resolvable without a second remap table.
        mapping[group_id] = group_id
        minted[name] = group_id
        created.append(name)
    return mapping, created


def _create_missing_users(
    backend: _Backend, unmatched: Sequence[UserProjection]
) -> dict[str, str]:
    """One credential-free local user per unmatched source user, written the
    way external-auth enrollment writes a first-login user
    (``app/repositories/auth_store.py``): placeholder email, empty password
    material, plain 'user' role. Returns ``source id -> new target id``."""
    if not unmatched:
        return {}
    created: dict[str, str] = {}
    now = datetime.now(timezone.utc)
    with backend.write() as conn:
        moment = _moment(backend, now)
        rows = []
        for user in unmatched:
            if not user.username:
                raise SyncImportError(
                    f"package user {user.id!r} has no username; it cannot be "
                    "matched or created (docs/incremental-sync-design.md §4)"
                )
            new_id = "user-" + secrets.token_hex(_CREATED_USER_ID_BYTES)
            created[user.id] = new_id
            rows.append(
                (
                    new_id,
                    f"{new_id}@{_CREATED_USER_EMAIL_DOMAIN}",
                    user.display_name or user.username,
                    _CREATED_USER_ROLE,
                    "active",
                    user.username,
                    "",
                    moment,
                    moment,
                )
            )
        _executemany(
            backend,
            conn,
            "INSERT INTO users "
            "(id, email, display_name, role, status, username, password_hash, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return created


# ------------------------------------------------------- claiming the package


def _claim_import(
    backend: _Backend, context: _Context, *, resume: bool
) -> None:
    """Take ownership of this source environment's import slot and open (or
    re-open) this package's ``sync_imports`` row, in ONE write transaction.

    The check and the claim have to be one transaction or they are not a
    check: two importers that both read "nothing is running" would both then
    write a running row. PostgreSQL takes a transaction-scoped advisory lock
    on the source environment so the read is serialized across connections;
    SQLite gets the same guarantee from ``write()``'s process-wide writer
    lock (and, across processes, from the database file's own write lock).

    A row already 'running' for this source environment blocks the run.
    ``resume=True`` is the operator saying "that process is gone, this is the
    same package continuing" -- it is never assumed, because assuming it turns
    a genuine concurrent import into silent interleaving. A row that has been
    'running' longer than ``_STALE_RUNNING_HOURS`` is taken over regardless,
    with a warning.
    """
    manifest = context.manifest
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(hours=_STALE_RUNNING_HOURS)
    with backend.write() as conn:
        if backend.is_postgres:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (manifest.source_env,)
            )
        running = backend.fetch(
            conn,
            "SELECT package_id, started_at FROM sync_imports "
            "WHERE source_env = ? AND status = 'running'",
            (manifest.source_env,),
        )
        for row in running:
            other = str(row["package_id"])
            started = _read_moment(row["started_at"])
            if started is not None and started < stale_before:
                context.ledger.warn(
                    f"took over a sync_imports row for package {other} that has "
                    f"been 'running' since {started.isoformat()}; the process "
                    "that opened it is assumed gone"
                )
                continue
            if other == manifest.package_id and resume:
                context.ledger.warn(
                    "resumed a package whose previous run is still marked "
                    "'running'; --resume asserts that process is gone"
                )
                continue
            raise SyncImportError(
                f"an import from {manifest.source_env!r} is still running "
                f"(package_id {other}); refusing to interleave two imports of "
                "the same source environment. If that process is gone, re-run "
                "with resume=True (--resume)."
            )
        conn.execute(
            backend.sql(
                "INSERT INTO sync_imports "
                "(package_id, source_env, from_seq, to_seq, status, started_at, "
                "finished_at, report_json) "
                f"VALUES (?, ?, ?, ?, 'running', ?, NULL, {_json_cast(backend)}) "
                "ON CONFLICT (package_id) DO UPDATE SET "
                "source_env = excluded.source_env, from_seq = excluded.from_seq, "
                "to_seq = excluded.to_seq, status = 'running', "
                "started_at = excluded.started_at, finished_at = NULL"
            ),
            (
                manifest.package_id,
                manifest.source_env,
                manifest.from_seq,
                manifest.to_seq,
                _moment(backend, now),
                "{}",
            ),
        )


def _json_cast(backend: _Backend) -> str:
    """Placeholder for ``report_json``. PostgreSQL's column is ``jsonb`` and
    psycopg sends a ``str`` as text, so the cast is what parses it."""
    return "?::jsonb" if backend.is_postgres else "?"


# ------------------------------------------------------------ phase 3 -- rows


def _completed_steps(backend: _Backend, conn: Any, package_id: str) -> set[str]:
    return {
        str(row["table_name"])
        for row in backend.fetch(
            conn,
            "SELECT table_name FROM sync_import_progress "
            "WHERE package_id = ? AND completed_at IS NOT NULL",
            (package_id,),
        )
    }


def _record_progress(
    backend: _Backend,
    conn: Any,
    package_id: str,
    step: str,
    rows_applied: int,
    completed_at: datetime,
) -> None:
    conn.execute(
        backend.sql(
            "INSERT INTO sync_import_progress "
            "(package_id, table_name, rows_applied, completed_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (package_id, table_name) DO UPDATE SET "
            "rows_applied = excluded.rows_applied, "
            "completed_at = excluded.completed_at"
        ),
        (package_id, step, rows_applied, _moment(backend, completed_at)),
    )


def _target_owned(table: str) -> frozenset[str]:
    return frozenset(spec_for(table).target_owned_columns)


def _insert_overrides(table: str, context: _Context) -> dict[str, Any]:
    """Values a NEWLY inserted row takes from this import rather than from the
    package. Only ``notebooks`` has any, and every one of them is a
    target-owned column (§5) that must not arrive carrying the source's value:

    - ``sync_origin``: stamped at INSERT, not only in phase 5, so a crash
      between the two never leaves a row that looks like a local notebook.
    - ``status``: the in-flight marker, flipped in phase 5.
    - ``is_shared``/``share_token``: link sharing is the target's own decision
      (the mirror fence in deps.py lets ``notebook:configure`` through on a
      mirror precisely because of that), and carrying the source's token
      across would publish a link nobody at this environment created.
      ``copy_notebook`` mints a fresh token for the same reason.
    """
    if table != "notebooks":
        return {}
    return {
        "sync_origin": context.manifest.source_env,
        "status": _IMPORT_IN_FLIGHT_STATUS,
        "is_shared": 0,
        "share_token": None,
    }


def _value_encoder(backend: _Backend, conn: Any, table: str):
    """``(column, value) -> parameter`` for this table and target backend.

    SQLite takes the package's values almost as they are -- the package
    encoding IS the SQLite shape. The one adjustment is the inverse of the
    exporter's: a column in ``POSTGRES_EMPTY_TIME_SENTINELS`` is
    ``timestamptz NULL`` in PostgreSQL but ``TEXT NOT NULL DEFAULT ''`` in
    SQLite, so the package's ``null`` has to land as the empty-string sentinel
    the SQLite schema actually accepts.

    PostgreSQL routes each value through the shadow migration's strict
    SQLite->PG conversion, so jsonb/timestamptz/bytea/boolean land as the
    driver's native types rather than as text -- including that same sentinel
    pair in the other direction.
    """
    if not backend.is_postgres:
        sentinels = frozenset(
            column.split(".", 1)[1]
            for column in POSTGRES_EMPTY_TIME_SENTINELS
            if column.startswith(f"{table}.")
        )

        def encode_sqlite(column: str, value: Any) -> Any:
            if value is None and column in sentinels:
                return ""
            return value

        return encode_sqlite

    from app.migration.shadow.transform import transform_sqlite_value

    spec = _SHADOW_SPECS.get(table)
    if spec is None:
        raise SyncImportError(
            f"{table}: no same-name TableSpec in the shadow manifest, so its "
            "PostgreSQL value conversion is undefined"
        )
    columns = _postgres_columns(backend, conn, table)

    def encode(column: str, value: Any) -> Any:
        target = columns.get(column)
        if target is None:
            raise SyncImportError(f"{table}.{column} is not a target column")
        try:
            return transform_sqlite_value(spec, target, value)
        except ValueError as exc:
            raise SyncImportError(f"{table}.{column}: {exc}") from None

    return encode


def _apply_table(
    backend: _Backend, conn: Any, table: str, context: _Context
) -> TableOutcome:
    spec = spec_for(table)
    entry = context.manifest.tables[table]
    target_columns = set(backend.columns(conn, table))
    columns = [
        column
        for column in entry.columns
        # The exporter never writes ordinal (§8) -- filtered again here so a
        # package from any other producer cannot pin the target's identity
        # sequence to the source's numbering.
        if column in target_columns
        and not (column == "ordinal" and table in POSTGRES_ROWID_ORDINAL_TABLES)
    ]
    if not columns:
        raise SyncImportError(f"{table}: package carries no usable column")
    primary_key = backend.primary_key(conn, table)
    owned = _target_owned(table)
    update_columns = [
        column
        for column in columns
        if column not in primary_key and column not in owned
    ]
    statement = _upsert_statement(
        table, columns, primary_key, update_columns, seed_only=spec.seed_only
    )
    overrides = _insert_overrides(table, context)
    encode = _value_encoder(backend, conn, table)

    if not primary_key:
        _replace_scope(backend, conn, table, context)
    known = _TargetKeys(backend, conn, table, primary_key, context)

    inserted = updated = skipped = 0
    for batch in _row_batches(context.package_dir / rows_path(table)):
        prepared: list[dict[str, Any]] = []
        for raw in batch:
            decoded = decode_row(raw)
            row, reason = _map_row(table, dict(decoded), context)
            if row is None:
                skipped += 1
                context.ledger.skip(table, _row_key(decoded, primary_key), reason)
                continue
            prepared.append(row)
        kept = _apply_optional_refs(table, prepared, context, conn)
        skipped += len(prepared) - len(kept)
        if not kept:
            continue
        keys = [tuple(row.get(column) for column in primary_key) for row in kept]
        present = known.present(keys) if primary_key else set()
        params: list[tuple[Any, ...]] = []
        fresh: list[tuple[Any, ...]] = []
        for row, key in zip(kept, keys, strict=True):
            exists = bool(primary_key) and key in present
            if exists and spec.seed_only:
                skipped += 1
                continue
            values = dict(row)
            if not exists:
                values.update(overrides)
                fresh.append(key)
            params.append(tuple(encode(column, values.get(column)) for column in columns))
            if exists:
                updated += 1
            else:
                inserted += 1
        _executemany(backend, conn, statement, params)
        known.note_inserted(fresh)
    if inserted + updated + skipped != entry.rows:
        raise SyncImportError(
            f"{table}: accounted for {inserted + updated + skipped} rows but the "
            f"package manifest declares {entry.rows}"
        )
    return TableOutcome(inserted=inserted, updated=updated, skipped=skipped)


def _replace_scope(
    backend: _Backend, conn: Any, table: str, context: _Context
) -> None:
    """A table the target schema gives no primary key (knowledge_object_sources,
    community_members -- design doc §2 registers them, §7 defers adding one to
    PR-3) cannot be upserted row by row. A FULL package carries every row those
    tables have for the notebooks it covers, so the idempotent equivalent is to
    clear this package's notebooks out of the table first and insert afresh.
    Only defensible while the table is notebook-scoped, which is asserted."""
    scope = spec_for(table).scope
    if scope is None or scope.kind is not ScopeKind.NOTEBOOK:
        raise SyncImportError(
            f"{table}: the target has no primary key for it and its scope is "
            f"not notebook-local, so an idempotent import is undefined"
        )
    context.ledger.warn(
        f"{table} has no primary key at the target; this package's notebooks "
        "were replaced wholesale in it rather than upserted"
    )
    for batch in _batched(list(context.manifest.notebooks)):
        placeholders = ",".join("?" for _ in batch)
        conn.execute(
            backend.sql(
                f"DELETE FROM {_ident(table)} "
                f"WHERE {_ident(scope.column)} IN ({placeholders})"
            ),
            tuple(batch),
        )


def _row_batches(path: Path) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for row in _iter_lines(path):
        batch.append(row)
        if len(batch) >= _ROW_BATCH:
            yield batch
            batch = []
    if batch:
        yield batch


def _apply_rows(backend: _Backend, context: _Context, done: set[str]) -> None:
    package_id = context.manifest.package_id
    resumed = done & set(synced_tables())
    if resumed:
        context.ledger.warn(
            f"{len(resumed)} table(s) were already complete from an earlier run "
            "of this package and were not re-applied"
        )
    for table in synced_tables():
        if table in done:
            context.ledger.tables[table] = TableOutcome(resumed=True)
            continue
        with backend.write() as conn:
            outcome = _apply_table(backend, conn, table, context)
            _record_progress(
                backend,
                conn,
                package_id,
                table,
                outcome.inserted + outcome.updated,
                datetime.now(timezone.utc),
            )
        context.ledger.tables[table] = outcome


# ----------------------------------------------------------- phase 4 -- files


# ``package subdirectory under files/`` -> ``storage/<subdirectory>/``. The
# same pair the exporter copies and notebook delete cleans up together.
_FILE_ROOTS = (NOTEBOOK_FILES_DIR, ASSET_FILES_DIR)

_STAGED_SUFFIX = ".sync-tmp"
_RETIRED_SUFFIX = ".sync-old"


def _install_files(context: _Context, *, verify: bool) -> None:
    """Install each notebook's ``storage/`` directories from the package.

    A full package carries a notebook's whole directory, so the target's copy
    is REPLACED rather than merged. A directory the package does not carry is
    left alone: "absent from the package" is how a notebook with no uploads
    looks, and it must not be read as "delete whatever the target has".

    The directory each swap displaces is kept as ``<name>.sync-old`` and is
    only deleted once phase 5 has succeeded (``_commit_files``); until then
    ``_rollback_files`` can put it back. ``verify`` re-hashes every installed
    file against ``checksums.json`` -- off by default because preflight
    already hashed the same bytes in this same run.
    """
    storage = Path(context.settings.storage_dir)
    copied = 0
    for notebook_id in context.manifest.notebooks:
        for root in _FILE_ROOTS:
            origin = context.package_dir / FILES_DIR / root / notebook_id
            destination = storage / root / notebook_id
            _sweep_staging(destination)
            if not origin.is_dir():
                continue
            staged = destination.with_name(destination.name + _STAGED_SUFFIX)
            retired = destination.with_name(destination.name + _RETIRED_SUFFIX)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(origin, staged)
            installed = [path for path in sorted(staged.rglob("*")) if path.is_file()]
            if verify:
                _verify_installed(context, staged, root, notebook_id, installed)
            copied += len(installed)
            if destination.exists():
                destination.rename(retired)
            staged.rename(destination)
            context.installed.append((destination, retired))
    context.ledger.files_copied = copied


def _verify_installed(
    context: _Context,
    staged: Path,
    root: str,
    notebook_id: str,
    installed: Sequence[Path],
) -> None:
    for path in installed:
        relative = (
            f"{FILES_DIR}/{root}/{notebook_id}/{path.relative_to(staged).as_posix()}"
        )
        if _digest(path) != context.checksums.get(relative):
            raise SyncImportError(
                f"package file failed its checksum on install: {relative}"
            )


def _sweep_staging(destination: Path) -> None:
    """Remove a ``.sync-tmp`` left by a crashed run. A ``.sync-old`` is left
    alone: a crash between the swap and phase 5 means the destination already
    holds this package's bytes, and this run's own rollback/commit owns the
    retired copy from here on."""
    staged = destination.with_name(destination.name + _STAGED_SUFFIX)
    if staged.exists():
        shutil.rmtree(staged, ignore_errors=True)


def _rollback_files(context: _Context) -> None:
    """Put back every directory the file phase displaced. Best effort by
    construction -- it runs while an exception is already propagating -- so a
    failure here is reported, never allowed to replace the original error."""
    for destination, retired in reversed(context.installed):
        try:
            if not retired.exists():
                # Nothing was there before this run; removing what we put in
                # restores "absent", which is what the target had.
                if destination.exists():
                    shutil.rmtree(destination, ignore_errors=True)
                continue
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            retired.rename(destination)
        except OSError as exc:  # pragma: no cover - filesystem failure path
            context.ledger.warn(f"could not restore {destination}: {exc}")
    context.installed.clear()


def _commit_files(backend: _Backend, context: _Context) -> None:
    """Drop the displaced directories and record the file phase as complete.
    Both happen only after phase 5's database work has succeeded, so a crash
    before this point resumes into a file phase that runs again."""
    for _destination, retired in context.installed:
        if retired.exists():
            shutil.rmtree(retired, ignore_errors=True)
    context.installed.clear()
    with backend.write() as conn:
        _record_progress(
            backend,
            conn,
            context.manifest.package_id,
            _FILES_PHASE,
            context.ledger.files_copied,
            datetime.now(timezone.utc),
        )


# ----------------------------------------------------------- phase 5 -- finish


def _publish_notebooks(backend: _Backend, context: _Context) -> None:
    """Flip the notebooks this run inserted out of ``copying``.

    Narrow on purpose: only a row that is BOTH still in the in-flight state
    AND already stamped with this package's source environment is touched, so
    a notebook the target put into ``copying`` for its own reasons (a deep
    copy in flight) is never published by an import that happens to name the
    same id. A resumed run reaches this with the ``copying`` rows its own
    earlier attempt committed -- ``sync_import_progress`` is what says those
    rows are this package's -- and flips exactly those."""
    if not context.manifest.notebooks:
        return
    with backend.write() as conn:
        for batch in _batched(list(context.manifest.notebooks)):
            placeholders = ",".join("?" for _ in batch)
            conn.execute(
                backend.sql(
                    f"UPDATE notebooks SET status = ? "
                    f"WHERE id IN ({placeholders}) AND status = ? "
                    "AND sync_origin = ?"
                ),
                (
                    _IMPORT_FINAL_STATUS,
                    *batch,
                    _IMPORT_IN_FLIGHT_STATUS,
                    context.manifest.source_env,
                ),
            )


def _stamp_mirrors(backend: _Backend, context: _Context) -> None:
    """The SECOND leg of the mirror stamp, not the only one.

    A notebook this run INSERTS already commits with ``sync_origin`` set --
    ``_insert_overrides`` puts it in the INSERT itself, so there is no window
    in which a committed mirror row looks local. This pass exists for the
    rows that were NOT inserted: a re-sync of an existing mirror, where
    ``sync_origin`` is a target-owned column and therefore excluded from the
    UPDATE clause. It re-asserts the stamp through the one store method that
    owns that column (``sharing_store.set_notebook_sync_origin``) rather than
    a second ``UPDATE notebooks`` of this module's own."""
    if not context.manifest.notebooks:
        return
    settings = context.settings
    # Both stores take a clock and a deep-copy row writer that
    # set_notebook_sync_origin does not touch; they are supplied only because
    # the constructor requires them.
    if backend.is_postgres:
        from app.repositories.postgres.sharing_store import SharingStore

        store: Any = SharingStore(
            backend._database,
            settings,
            now=lambda: datetime.now(timezone.utc),
            insert_row=_unused_insert_row,
        )
    else:
        from app.repositories.sqlite.sharing_store import SharingStore

        store = SharingStore(
            backend._database,
            settings,
            now=lambda: datetime.now(timezone.utc).isoformat(),
            insert_row=_unused_insert_row,
        )
    for notebook_id in context.manifest.notebooks:
        store.set_notebook_sync_origin(notebook_id, context.manifest.source_env)


def _unused_insert_row(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError(
        "the importer's SharingStore is constructed only for "
        "set_notebook_sync_origin; it never copies rows"
    )


def _close_import_row(
    backend: _Backend, package_id: str, status: str, report: ImportReport
) -> None:
    with backend.write() as conn:
        conn.execute(
            backend.sql(
                "UPDATE sync_imports SET status = ?, finished_at = ?, "
                f"report_json = {_json_cast(backend)} WHERE package_id = ?"
            ),
            (
                status,
                _moment(backend, datetime.now(timezone.utc)),
                json.dumps(report.as_json(), ensure_ascii=False, sort_keys=True),
                package_id,
            ),
        )


_SAFE_FILENAME = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"


def _report_filename(settings: "Settings", manifest: _PackageManifest) -> str:
    """``import-report-<this environment>.json``.

    Named after the TARGET environment's own identifier (``SYNC_ENV``, the
    same name the other side puts in ``--target``), not after the package, so
    two environments importing the same directory each keep their own report
    instead of overwriting one another's."""
    label = (
        str(getattr(settings, "sync_env", "") or "").strip()
        or manifest.target_env
        or "target"
    )
    safe = "".join(
        character if character in _SAFE_FILENAME else "_" for character in label
    )
    return f"{_REPORT_PREFIX}{safe}.json"


def _write_report_file(context: _Context, report: ImportReport) -> None:
    """Best effort: a report that cannot be written (read-only package
    directory, full disk) degrades to a warning. The authoritative copy is
    ``sync_imports.report_json`` in the database."""
    path = context.package_dir / _report_filename(context.settings, context.manifest)
    try:
        path.write_text(json_document(report.as_json()) + "\n", encoding="utf-8")
    except OSError as exc:
        context.ledger.warn(f"could not write the import report to {path}: {exc}")


# ------------------------------------------------------------------- import


def import_package(
    settings: "Settings",
    package_dir: Path,
    *,
    create_missing_users: bool = False,
    dry_run: bool = False,
    importer_user_id: str | None = None,
    resume: bool = False,
    verify_files: bool = False,
) -> ImportReport:
    """Apply one full export package to the backend ``settings`` names.

    ``dry_run`` stops after identity mapping: it reads the target, reports what
    the run would do, and writes nothing -- not to the database, and not into
    the package directory either (a package may be shared or read-only).

    ``resume`` is the operator asserting that an earlier run of THIS package,
    whose ``sync_imports`` row is still marked 'running', is gone. Without it
    a still-running row for the same source environment refuses the run, so a
    genuinely concurrent import can never interleave. A row left 'failed' (the
    ordinary outcome of a crash this process caught) always resumes without
    the flag, from ``sync_import_progress``.

    ``verify_files`` re-hashes the installed files against ``checksums.json``.
    Preflight already hashed the same bytes in this same run, so it is off by
    default and exists for an operator who wants the copy itself checked.

    **A SQLite target must be quiesced** -- see the module docstring. The
    report carries a warning whenever the target is SQLite.

    Re-running the same package is safe. A package already recorded as ``done``
    short-circuits with ``already_applied=True``.
    """
    package_dir = Path(package_dir)
    root_dir = Path(__file__).resolve().parents[4]
    backend = _Backend(settings, root_dir)
    try:
        return _import(
            backend,
            settings=settings,
            package_dir=package_dir,
            create_missing_users=create_missing_users,
            dry_run=dry_run,
            importer_user_id=importer_user_id,
            resume=resume,
            verify_files=verify_files,
        )
    finally:
        backend.close()


def _import(
    backend: _Backend,
    *,
    settings: "Settings",
    package_dir: Path,
    create_missing_users: bool,
    dry_run: bool,
    importer_user_id: str | None,
    resume: bool,
    verify_files: bool,
) -> ImportReport:
    manifest = _read_manifest(package_dir)
    # Phase 1a: the package on its own, with no database handle open.
    checksums = _verify_checksums(package_dir, manifest)
    _reject_incremental_payload(package_dir)

    ledger = _Ledger()
    context = _Context(
        backend=backend,
        settings=settings,
        package_dir=package_dir,
        manifest=manifest,
        ledger=ledger,
        checksums=checksums,
    )
    if not backend.is_postgres:
        ledger.warn(
            "the target is SQLite, which has a single writer: stop the "
            "application before importing, or its writes will interleave with "
            "this run between tables"
        )
    if manifest.missing_files:
        ledger.warn(
            f"the exporter could not read {len(manifest.missing_files)} file(s) "
            "it meant to carry; the rows that reference them still import "
            f"(manifest.missing_files): {sorted(manifest.missing_files)[:10]}"
        )

    # Phase 1b + 2: one read snapshot, catalog and identity only.
    with backend.read() as conn:
        if _preflight(backend, conn, context):
            return _report(manifest, ledger, _EMPTY_MAPPING, already_applied=True)
        context.importer_user_id = _resolve_importer(backend, conn, importer_user_id)
        try:
            mapping = build_user_mapping(
                _package_users(package_dir), _target_users(backend, conn)
            )
        except ValueError as exc:
            raise SyncImportError(f"identity mapping is ambiguous: {exc}") from None
        groups, groups_to_create = _build_group_mapping(backend, conn, context)

    context.users = dict(mapping.matched)
    context.groups = groups
    ledger.groups_created.extend(groups_to_create)
    created: dict[str, str] = {}

    if dry_run:
        if mapping.unmatched and not create_missing_users:
            ledger.warn(
                f"{len(mapping.unmatched)} package user(s) have no target "
                "counterpart; without --create-missing-users each reference to "
                "them falls back to its table's rule"
            )
        return _report(
            manifest,
            ledger,
            UserMappingResult(
                matched=MappingProxyType(dict(mapping.matched)),
                created=MappingProxyType({}),
                unmatched=tuple(user.username for user in mapping.unmatched),
            ),
            dry_run=True,
        )

    _claim_import(backend, context, resume=resume)

    if create_missing_users:
        created = _create_missing_users(backend, mapping.unmatched)
        context.users.update(created)
    elif mapping.unmatched:
        ledger.warn(
            f"{len(mapping.unmatched)} package user(s) have no target "
            "counterpart; references to them follow their table's rule "
            "(docs/incremental-sync-design.md §3.2)"
        )
    result = UserMappingResult(
        matched=MappingProxyType(dict(mapping.matched)),
        created=MappingProxyType(created),
        unmatched=tuple(
            user.username for user in mapping.unmatched if user.id not in created
        ),
    )

    try:
        with backend.read() as conn:
            done = _completed_steps(backend, conn, manifest.package_id)
        _apply_rows(backend, context, done)          # phase 3
        if _FILES_PHASE in done:
            ledger.warn(
                "the file phase was already complete from an earlier run of "
                "this package and was not repeated"
            )
        else:
            _install_files(context, verify=verify_files)  # phase 4
        _publish_notebooks(backend, context)          # phase 5
        _stamp_mirrors(backend, context)
    except BaseException as exc:
        # BaseException, not Exception: a KeyboardInterrupt or a cancelled
        # task must still leave the run's state recorded, or the next attempt
        # sees a row stuck at 'running' and refuses without --resume.
        _rollback_files(context)
        failure = _report(manifest, ledger, result, error=str(exc) or repr(exc))
        _settle(backend, context, "failed", failure)
        if isinstance(exc, SyncImportError) or not isinstance(exc, Exception):
            raise
        raise SyncImportError(f"import failed: {exc}") from exc

    report = _report(manifest, ledger, result)
    _settle(backend, context, "done", report)
    try:
        _commit_files(backend, context)
    except OSError as exc:  # pragma: no cover - filesystem failure path
        ledger.warn(f"could not drop the replaced storage directories: {exc}")
    return report


def _settle(
    backend: _Backend, context: _Context, status: str, report: ImportReport
) -> None:
    """Record the run's outcome, on both the success and the failure path, and
    never let recording it become the failure. On the failure path the caller
    is already re-raising the original exception, and an error here would
    replace it with a less informative one."""
    try:
        _close_import_row(backend, context.manifest.package_id, status, report)
    except Exception as exc:  # noqa: BLE001 - reported, never raised over the original
        context.ledger.warn(f"could not record sync_imports.status={status}: {exc}")
    _write_report_file(context, report)


__all__ = [
    "ImportReport",
    "SkippedRow",
    "SyncImportError",
    "TableOutcome",
    "UnmappedPolicy",
    "UserMappingResult",
    "import_package",
]

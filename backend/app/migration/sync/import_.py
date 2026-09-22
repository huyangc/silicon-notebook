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
3a. **Reconcile.** A full package is a SNAPSHOT, and upsert only ever adds
   and overwrites, so a pass -- children before parents, one transaction and
   one ``sync_import_progress`` step per table -- deletes the rows the target
   holds for this package's notebooks that the package does not carry.
   Without it a mirror accumulates every chunk, element and knowhow cell the
   source ever deleted.

   It runs BEFORE the upsert, not after, because a primary key is not a
   table's only identity. ``chunk_questions`` is unique on
   ``(chunk_id, question)``, ``knowhow_cells`` on ``(row_id, column_id)``, and
   so on: a source that deleted a row and made an equivalent one gives that
   row a new primary key and the same business key, so an upsert keyed on the
   primary key inserts a SECOND row and the unique index rejects it. Sweeping
   first removes the superseded row while it is still the only one. What the
   sweep cannot remove -- a collision against a row it is not allowed to touch
   -- is caught by name before the statement runs (``_assert_no_unique_key_
   collision``) rather than surfacing as a constraint error.

   **``memory_items`` and its revision/provenance/embedding rows are exempt**:
   the target's users create memories on a mirror and §5 lets those coexist,
   but nothing on a memory row says which side made it, so a "not in the
   package" sweep cannot tell a source-side deletion from a target-side
   creation and would destroy the second. Source-side memory deletions wait
   for PR-3's change log, which replays them by key instead of inferring them.
   **The closure of a target-owned memory source is exempt too** -- confirming
   a memory on a mirror materializes a synthetic source with its own elements,
   chunks, vectors and objects (``_protected_sources``). The report says both
   in ``warnings``.
3b. **Rows.** One transaction per table, in ``synced_tables()`` order, with
   the table's ``sync_import_progress`` row written inside that same
   transaction. A crash therefore loses at most the table that was in flight,
   and a re-run re-applies only tables that never completed. A SQLite
   target's two hand-maintained FTS5 indexes are re-projected in the same
   transaction as the table they index (``_SQLITE_FTS_REBUILD``).
4. **Files.** After the rows, because a row is what makes a file meaningful
   and a crash between them must not leave installed bytes with no row. Each
   notebook directory is staged beside its destination and swapped in; the
   directory it replaced is kept as ``.sync-old`` until phase 5 has succeeded,
   and is swapped BACK if anything in between fails.
5. **Finish.** Flip the notebooks this run inserted out of their in-flight
   state, stamp them as mirrors, drop the retired file directories, close the
   ``sync_imports`` row, write the report.

**Atomicity, stated honestly.** There is no transaction spanning the run. Rows
are atomic per table; files are atomic per notebook directory and are rolled
back on failure; the run as a whole is *resumable*, not atomic. A failure
leaves the tables that completed in place, `sync_imports.status='failed'`, and
a report on disk -- and a re-run picks up from there. Failure granularity is
the whole package: a package either ends applied in full or is left resumable.
Nothing partially applies "some notebooks" and reports success.

**A notebook this run inserts is created ``status='importing'``**, a state of
its own that ``NOTEBOOK_LIVE_SQL`` hides and phase 5 flips to the ordinary
``draft``. A half-imported notebook is therefore never visible -- and never
mistaken for a half-COPY, which the deep copy's stale-copy sweeper physically
deletes (see ``_IMPORT_IN_FLIGHT_STATUS``). Re-syncing a notebook the target
ALREADY mirrors does not hide it: ``status`` is a target-owned column (§5) and
is never overwritten for an existing row, so an established mirror stays
readable while it is refreshed. That is the accepted trade-off -- the
alternative is taking a live mirror offline for every sync.

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

**``manifest.json`` is not trustworthy.** ``checksums.json`` covers every
package file EXCEPT the manifest itself (it is the package-complete marker,
written last -- see ``_verify_checksums``), so nothing in ``manifest.json``
carries any integrity guarantee beyond "this is the file the exporter last
wrote before the run either finished or was interrupted". In particular
``manifest.notebooks`` is not proof of which notebooks this package actually
carries, and a value in it is not proof it is safe to splice into a
filesystem path. Two independent checks carry that weight instead, both run
in preflight before any database handle is opened or any file is staged,
renamed, or deleted:

- **path safety** (``_verify_package_paths``): every identifier this package
  lets get joined onto a trusted base path -- ``manifest.notebooks``,
  ``manifest.package_id``, the directory names actually present under
  ``files/notebooks/`` and ``files/assets/``, and every key in
  ``checksums.json`` -- is checked against a narrow character whitelist
  (``app.migration.sync.package.is_safe_identifier``/``is_safe_relative_path``)
  and, for the two file roots, the JOINED path is resolved and asserted to
  stay inside its package/storage root. A package that fails this leaves the
  target untouched.
- **row-range validity** (``_verify_row_scopes``): since ``manifest.notebooks``
  cannot be trusted, the notebook set a package actually covers is instead
  read from ``rows/notebooks.jsonl`` -- which IS checksummed -- and every
  other synced table's rows are streamed and checked to fall inside that set
  (NOTEBOOK-scoped tables directly, PARENT-scoped tables one hop at a time
  against their already-verified parent's primary keys). This runs before the
  target-side mirror-collision guard in ``_preflight``, so that guard is
  reasoning about the package's real scope rather than what its manifest
  claims.

A row's PRIMARY KEY is not trustworthy either, and that one is not something
preflight can settle once against the package alone -- it can only be caught
against the TARGET, table by table, as each upsert is about to run. Ids are
exported verbatim and never reissued per environment, so a package can
legitimately reuse a primary key the target already holds under a completely
different notebook (or, for a PARENT-scoped table, a different parent row).
Nothing stops ``INSERT ... ON CONFLICT (pk) DO UPDATE`` from rewriting that
pre-existing row's scope column right along with the rest of it, and
``_TargetKeys.present`` cannot see the collision coming -- for a
NOTEBOOK/GLOBAL-scoped table it only preloads keys already inside THIS
package's own notebook set, so a same-PK row belonging to someone else's
notebook reads as "does not exist yet" right up until the database's own
conflict target collides with it. **row-ownership validity**
(``_assert_target_ownership``, called from ``_apply_table`` once per batch,
before the upsert, inside the same per-table write transaction) closes this:
an unscoped lookup by primary key against the target, checked against
``context.manifest.notebooks`` (NOTEBOOK scope) or ``context.parent_key_sets``
(PARENT scope, populated by ``_verify_row_scopes`` during preflight) --
refusing the whole import by name, table, key, and both scope values, rather
than silently reattributing an existing row. Not needed for a ``seed_only``
table (its upsert is a bare ``ON CONFLICT DO NOTHING``, which never touches a
pre-existing row regardless of who owns it) or a table with no primary key
(replaced wholesale by notebook in ``_replace_scope``, never upserted through
a conflict target that could straddle scopes).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from app.migration.shadow.manifest import MANIFEST as _SHADOW_MANIFEST
from app.migration.sync.export import (
    SyncExportError,
    _batched,
    _parent_join_clause,
    _quoted,
)
from app.migration.sync.export import _Source as _Backend
from app.migration.sync.identity import (
    UserMapping,
    UserProjection,
    build_user_mapping,
)
from app.migration.sync.manifest import (
    MappingKind,
    ScopeKind,
    SyncClass,
    SYNC_MANIFEST,
    TableScope,
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
    decode_value,
    is_safe_identifier,
    is_safe_relative_path,
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
# phase 5 flips it to. ``importing`` is hidden by ``NOTEBOOK_LIVE_SQL`` (both
# backends' access_sql.py), which is exactly what a notebook whose rows are
# still arriving needs to be.
#
# It is deliberately NOT the deep copy's ``copying`` marker, even though the
# two look alike on the read side. ``copying``'s WRITE-side meaning is "this
# is a half-copy, garbage, physically delete it":
# ``SharingStore.sweep_stale_copies`` reaps every ``status='copying'`` row
# whose ``created_at`` is older than ``notebook_copy_stale_seconds``, and a
# mirrored notebook's ``created_at`` comes from the SOURCE environment, so it
# is almost always already past that cutoff. Sharing the marker would let the
# sweeper delete a notebook mid-import. A half-imported notebook is not
# garbage -- it is resumable -- so it gets its own state, and the sweeper's
# predicate never matches it.
#
# It doubles as the durable "this run created this row" fact the seed_only
# gate below needs: it survives a crash, and phase 5 is the only thing that
# clears it.
_IMPORT_IN_FLIGHT_STATUS = "importing"
_IMPORT_FINAL_STATUS = "draft"

# notebook_grants.principal_type values whose principal_id is a groups.id.
# Restated from app.repositories.group_rows.GROUP_PRINCIPAL_TYPES, which this
# package's import whitelist keeps it from importing. 'group_admins' is the
# same group reached over a narrower edge (only its role='admin' members), so
# it maps through the group mapping exactly like 'group' -- anything else
# leaves every admin-only grant pointing at a source-side id.
_GROUP_PRINCIPAL_TYPES = frozenset({"group", "group_admins"})

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

# Where a running import records that it is still alive. Refreshed inside
# every transaction that writes progress, so "is that other import dead?" is
# answered by evidence of work rather than by a timeout on when it STARTED --
# a long import is not an abandoned one, and the two are indistinguishable
# from started_at alone. No duration is treated as proof of death here: the
# importer reports the other run's last heartbeat and an operator decides.
_HEARTBEAT_AT_KEY = "heartbeat_at"

# ``sync_imports.status``. 'superseded' marks a failed package that a NEWER
# package of the same source environment has since been declared over: its
# half-applied progress no longer describes anything the target should finish,
# so the progress rows are dropped and the package can never be resumed again.
_STATUS_RUNNING = "running"
_STATUS_DONE = "done"
_STATUS_FAILED = "failed"
_STATUS_SUPERSEDED = "superseded"

# Where a package's own ``manifest.created_at`` is kept inside
# ``sync_imports.report_json``, and where a supersede records who did it.
_PACKAGE_CREATED_AT_KEY = "package_created_at"
_SUPERSEDED_BY_KEY = "superseded_by"

# What an operator has to do next for every user ``--create-missing-users``
# minted. Nothing in this repository links an external identity to a local
# account by matching usernames: ``AuthStore._complete`` refuses an unmapped
# identity with ``identity_not_linked``, and the enrollment purpose refuses a
# username that is already taken (``check_name``) -- which a created user's is.
# The only way in is an administrator issuing a 'recover' grant naming that
# account (``AuthStore.issue_grant``, surfaced at ``POST
# /admin/auth/grants``), which the person then completes through the external
# provider. Until then the account exists and owns rows but nobody can sign in
# to it. Said in the report rather than implemented here: minting an identity
# binding from a username match is precisely the trust decision that flow
# exists to keep out of automation's hands.
_CREATED_USERS_NEED_A_GRANT = (
    "{count} user(s) were created for this import and have NO way to sign in "
    "yet: they carry no credentials and no external identity binding, and "
    "nothing links one by username. An administrator must issue a 'recover' "
    "grant for each (POST /admin/auth/grants, purpose='recover'), which the "
    "person completes through the external provider. See "
    "user_mapping.created for the ids."
)

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


# Every table named as a PARENT scope's ``parent_table`` somewhere in the
# manifest -- ``sources``, ``memory_items``, ``knowhow_tables``,
# ``knowhow_rows`` today. ``_verify_row_scopes`` needs to know, for each
# synced table in turn, whether it must also collect its OWN primary-key set
# (because a later table's PARENT scope will need it) -- derived from the
# manifest rather than hand-listed, so a new PARENT scope automatically
# widens this set in the same change that adds it.
_PARENT_SCOPE_TARGETS: frozenset[str] = frozenset(
    spec.scope.parent_table
    for spec in SYNC_MANIFEST
    if spec.scope is not None and spec.scope.kind is ScopeKind.PARENT
)


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
    # Required for totality, but unreachable in practice: groups is seed_only,
    # so the only write is an INSERT, and _STATIC_INSERT_OVERRIDES clears the
    # whole invitation capability on every insert. The rule is kept because a
    # mapped column without one is a hard failure by design, and because the
    # policy and the override happen to agree on the value.
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
    # The package's own manifest.created_at, carried through so
    # sync_imports.report_json records which SNAPSHOT this row applied. That
    # is what a later import compares itself against; sync_imports has no
    # column of its own for it and this change does not add one.
    package_created_at: str
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
            _PACKAGE_CREATED_AT_KEY: self.package_created_at,
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
        package_created_at=manifest.created_at,
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
    # When the SOURCE wrote this package. The ordering key for "newer" and
    # "older" between two packages of one source environment -- not the import
    # time, which only says when this side got around to it. PR-3 layers
    # ``to_seq`` on top; until there is a change log, both sequence numbers
    # are 0 and this is the only thing that orders two full snapshots.
    created_at: str
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
        created_at=str(document.get("created_at") or ""),
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
    # parent table -> primary keys THIS run inserted into it, for the
    # seed_only parent gate (``_SEED_PARENT``). Only what this process did;
    # ``_created_parents`` unions it with what an earlier attempt recorded.
    created_parents: dict[str, set[str]] = field(default_factory=dict)
    # package ids recorded 'done' at the target, read once before the file
    # phase so _reconcile_staging can tell an abandoned retired directory
    # (its package finished) from one a live run still owns.
    finished_packages: frozenset[str] = frozenset()
    # Target-owned synthetic memory sources in the package's notebooks; the
    # rows reachable from them survive the snapshot sweep. Resolved once, at
    # the start of the reconcile phase, from state the import does not change.
    protected_sources: frozenset[str] = frozenset()
    # Package-side primary-key sets for every table named as a PARENT scope's
    # ``parent_table`` (plus ``"notebooks"``), as ``_verify_row_scopes``
    # computed them from the package's own rows during preflight. Reused by
    # ``_assert_target_ownership`` as "the parent keys this package actually
    # carries" -- never re-derived from ``manifest.json``, which is not
    # trustworthy (module docstring).
    parent_key_sets: dict[str, frozenset[str]] = field(default_factory=dict)


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
            elif principal in _GROUP_PRINCIPAL_TYPES:
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


def _assert_within(base: Path, candidate: Path, *, what: str) -> Path:
    """Resolve ``candidate`` and refuse it if it does not stay inside
    ``base``. The character whitelist (``is_safe_identifier``) already
    refuses ``..`` and an absolute path lexically; this is the second,
    independent layer that catches what the whitelist cannot see -- a
    component that passes the whitelist and still resolves outside ``base``
    through a symlink planted at a trusted location. Returns ``base``'s own
    resolved form so a caller checking several candidates against the same
    base does not re-resolve it each time.
    """
    resolved_base = base.resolve()
    resolved = candidate.resolve()
    if resolved != resolved_base and resolved_base not in resolved.parents:
        raise SyncImportError(
            f"{what} resolves outside its expected root: {candidate} -> "
            f"{resolved} (expected under {resolved_base})"
        )
    return resolved_base


def _verify_package_paths(
    package_dir: Path,
    manifest: _PackageManifest,
    checksums: Mapping[str, str],
    settings: "Settings",
) -> None:
    """Refuse, before any database handle is open and before a single file is
    staged, renamed, or deleted, any identifier this package would splice
    into a filesystem path.

    ``manifest.json`` is not covered by ``checksums.json`` (see the module
    docstring), so ``manifest.notebooks`` is exactly as untrusted as an
    attacker's own text and is checked here on its own terms -- not against
    what the package's rows say (``_verify_row_scopes`` does that, and needs
    a database handle for primary-key names, so it runs later in phase 1b).

    Four things get checked, all filesystem-only:

    - every id in ``manifest.notebooks``, and ``manifest.package_id`` itself
      (both later become directory-name components -- the notebook id under
      ``files/notebooks/``/``files/assets/`` and the storage roots, the
      package id in ``.sync-old-<package_id>``/``.sync-tmp``'s sibling
      naming) must be ``is_safe_identifier``;
    - every key ``checksums.json`` carries must be ``is_safe_relative_path``
      (defence in depth: ``_verify_checksums`` already requires every key to
      equal a real on-disk relative path, which can never lexically contain
      ``..``, but this does not rely on that invariant holding);
    - every directory actually present under ``files/notebooks/`` and
      ``files/assets/`` must have an ``is_safe_identifier`` name, whether or
      not ``manifest.notebooks`` mentions it;
    - for every notebook id, the package-side and storage-side paths
      ``_install_files`` will read from and write to are resolved and
      asserted to stay inside the package's ``files/<root>/`` directory and
      the target's ``storage/<root>/`` directory respectively -- this is what
      catches a directory that passes the character whitelist but is
      actually a symlink pointing outside either root.
    """
    bad_ids = sorted(
        notebook_id
        for notebook_id in manifest.notebooks
        if not is_safe_identifier(notebook_id)
    )
    if bad_ids:
        raise SyncImportError(
            f"manifest.notebooks carries unsafe notebook id(s): {bad_ids}"
        )
    if manifest.package_id and not is_safe_identifier(manifest.package_id):
        raise SyncImportError(
            f"manifest.package_id is not a safe identifier: {manifest.package_id!r}"
        )
    bad_paths = sorted(
        relative for relative in checksums if not is_safe_relative_path(relative)
    )
    if bad_paths:
        raise SyncImportError(
            f"{CHECKSUMS_NAME} carries unsafe relative path(s): {bad_paths}"
        )

    storage_dir = Path(settings.storage_dir)
    for root in (NOTEBOOK_FILES_DIR, ASSET_FILES_DIR):
        package_root = package_dir / FILES_DIR / root
        if package_root.is_dir():
            bad_entries = sorted(
                entry.name
                for entry in package_root.iterdir()
                if not is_safe_identifier(entry.name)
            )
            if bad_entries:
                raise SyncImportError(
                    f"{FILES_DIR}/{root} carries unsafe director(ies): {bad_entries}"
                )
        storage_root = storage_dir / root
        for notebook_id in manifest.notebooks:
            _assert_within(
                package_root,
                package_root / notebook_id,
                what=f"package file path for notebook {notebook_id!r} under {root!r}",
            )
            _assert_within(
                storage_root,
                storage_root / notebook_id,
                what=(
                    f"storage destination path for notebook {notebook_id!r} "
                    f"under {root!r}"
                ),
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


def _verify_row_scopes(
    backend: _Backend, conn: Any, package_dir: Path, manifest: _PackageManifest
) -> dict[str, frozenset[str]]:
    """Refuse a package whose rows reach outside the notebook set it claims.

    ``manifest.notebooks`` is not checksummed (see the module docstring), so
    it is not used here as the definition of "this package's notebooks" --
    ``rows/notebooks.jsonl`` IS checksummed, and its primary keys are. This
    first asserts the two sets are equal (catching a manifest that claims
    fewer OR more notebooks than the package's own rows carry), then streams
    every other synced table once and requires each row's scope value to
    fall inside that set:

    - NOTEBOOK-scoped: the row's own scope column must be one of the
      notebook ids.
    - PARENT-scoped: the row's scope column must be a primary key this
      package's copy of ``scope.parent_table`` actually carries. Checked one
      hop at a time against each table's immediate parent (``TableScope``'s
      own ``parent_table``, not a hand-rolled walk of it) -- sound by
      induction, because ``synced_tables()`` orders a declared FK parent
      before its children, so a PARENT table's own scope has already been
      verified, and therefore everything it stands for, by the time a table
      scoped through it is reached.
    - GLOBAL-scoped: not checked -- a GLOBAL table's rows are not attributed
      to any one notebook (design doc §3).

    Streamed table by table, keeping only the small id sets four tables need
    as somebody else's parent (``sources``, ``memory_items``,
    ``knowhow_tables``, ``knowhow_rows``) -- never a full table's rows in
    memory, so a chunks-sized table costs O(1) extra space here.

    Runs against the target only for ``backend.primary_key`` (a PARENT
    table's key column names, read from the live catalog like every other
    schema fact in this module) -- it reads no target DATA and writes
    nothing, and it runs before ``_preflight``'s mirror-collision guard, so
    that guard sees the package's real scope rather than what its manifest
    claims.

    Returns the package-side primary-key sets it collected for every table
    named as somebody else's PARENT ``parent_table`` (``_PARENT_SCOPE_TARGETS``,
    plus ``"notebooks"``). The row-ownership guard in ``_apply_table``
    (``_assert_target_ownership``) reuses this as "the parent keys this
    package actually carries" -- the set a PARENT-scoped table's existing
    target-side rows must be attributed into -- rather than re-streaming the
    same package files a second time.
    """
    notebook_ids: set[str] = set()
    for row in _iter_lines(package_dir / rows_path("notebooks")):
        value = str(row.get("id") or "")
        if value:
            notebook_ids.add(value)
    declared = set(manifest.notebooks)
    missing = sorted(declared - notebook_ids)
    extra = sorted(notebook_ids - declared)
    if missing or extra:
        raise SyncImportError(
            "manifest.notebooks does not match rows/notebooks.jsonl's primary "
            f"keys: manifest-only={missing}, rows-only={extra}"
        )

    parent_key_sets: dict[str, set[str]] = {"notebooks": notebook_ids}
    for table in synced_tables():
        if table == "notebooks":
            continue
        spec = spec_for(table)
        scope = spec.scope
        if scope is None or scope.kind is ScopeKind.GLOBAL:
            continue
        if scope.kind is ScopeKind.NOTEBOOK:
            allowed = notebook_ids
        else:
            allowed = parent_key_sets.get(scope.parent_table)
            if allowed is None:
                # synced_tables() orders a declared FK parent before its
                # children (tests/test_sync_manifest.py asserts this for
                # every edge inside the synced set), so this table's parent
                # must already have been visited. Reaching here is a bug in
                # that ordering guarantee, not a package problem.
                raise SyncImportError(
                    f"{table}: PARENT scope into {scope.parent_table!r}, whose "
                    "own rows were not scanned before this table"
                )
        collect_own_keys = table in _PARENT_SCOPE_TARGETS
        own_key_column = ""
        own_keys: set[str] = set()
        if collect_own_keys:
            key_columns = backend.primary_key(conn, table)
            if len(key_columns) != 1:
                raise SyncImportError(
                    f"{table}: named as a PARENT scope's parent_table but its "
                    f"primary key {key_columns} is not a single column"
                )
            own_key_column = key_columns[0]

        bad: set[str] = set()
        for row in _iter_lines(package_dir / rows_path(table)):
            value = str(row.get(scope.column) or "")
            if value not in allowed:
                bad.add(value)
            if collect_own_keys:
                own_keys.add(str(row.get(own_key_column) or ""))
        if bad:
            raise SyncImportError(
                f"{table}.{scope.column} carries value(s) outside this "
                f"package's scope: {sorted(bad)[:20]}"
            )
        if collect_own_keys:
            parent_key_sets[table] = own_keys

    return {table: frozenset(keys) for table, keys in parent_key_sets.items()}


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
    # Row-range validity is checked before ANY target-side decision that
    # reasons about "this package's notebooks" -- including the
    # already-applied short-circuit right below, so a package whose manifest
    # lies about its scope is refused the same way whether or not it happens
    # to share a package_id with something already recorded.
    context.parent_key_sets = _verify_row_scopes(
        backend, conn, context.package_dir, manifest
    )

    for row in backend.fetch(
        conn,
        "SELECT status FROM sync_imports WHERE package_id = ?",
        (manifest.package_id,),
    ):
        if str(row["status"]) == _STATUS_DONE:
            return True
    _reject_backwards_snapshot(
        _prior_imports(backend, conn, manifest.source_env), context
    )

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


def _assert_required_identities_resolve(
    context: _Context,
    mapping: UserMapping,
    package_users: Sequence[UserProjection],
    *,
    create_missing_users: bool,
) -> None:
    """Refuse, before anything is written, a package whose FAIL-policy
    references cannot be resolved.

    ``_UNMAPPED_POLICY``'s FAIL rule aborts the whole import the moment a row
    carrying that column is applied -- and by then phase 3a has already
    DELETED the rows this package does not carry. The target would be left
    stripped of content by a run that was never going to finish. Every FAIL
    column is therefore resolved up front, against the same mapping the row
    phase will use, while the only thing that has happened is reading.

    A dry run raises here too rather than warning: "this package cannot be
    imported" is the answer a dry run exists to produce, and reporting it as a
    warning among others is how an operator misses it.
    """
    resolvable = set(mapping.matched)
    if create_missing_users:
        resolvable.update(user.id for user in mapping.unmatched)
    usernames = {user.id: user.username for user in package_users}
    required = sorted(
        column
        for column, policy in _UNMAPPED_POLICY.items()
        if policy is UnmappedPolicy.FAIL
    )
    problems: list[str] = []
    for table, column in required:
        unresolved: set[str] = set()
        for value in _package_column(context, table, column):
            if isinstance(value, str) and value and value not in resolvable:
                unresolved.add(value)
        for value in sorted(unresolved):
            username = usernames.get(value)
            who = f"username={username!r}" if username else "not in users.jsonl"
            problems.append(f"{table}.{column}={value!r} ({who})")
    if not problems:
        return
    raise SyncImportError(
        "this package cannot be imported: these references have no "
        "counterpart in the target environment, and their rule is 'fail' "
        "(docs/incremental-sync-design.md §3.2), so the import would abort "
        f"partway through. {problems}. Re-run with --create-missing-users, or "
        "create those users at the target first."
    )


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


@dataclass(frozen=True)
class _PriorImport:
    """One ``sync_imports`` row of this source environment, as the ordering
    rules need to see it."""

    package_id: str
    status: str
    started_at: datetime | None
    # The package's own manifest.created_at, read back out of report_json.
    # ``None`` when the row predates this bookkeeping or its JSON is
    # unreadable -- an ordering question that cannot be answered, never one
    # silently answered "yes".
    created_at: datetime | None
    # When this run last committed work, out of report_json. ``None`` for a
    # row that has not committed anything yet (or is not running).
    heartbeat_at: datetime | None
    has_progress: bool
    # This row's own manifest.notebooks, out of report_json. ``None`` when the
    # row predates this bookkeeping or its JSON is unreadable -- a coverage
    # question that cannot be answered, never one silently treated as "covers
    # everything" or "covers nothing" (see ``_supersede_outstanding``).
    notebooks: tuple[str, ...] | None


def _prior_imports(
    backend: _Backend, conn: Any, source_env: str
) -> tuple[_PriorImport, ...]:
    with_progress = {
        str(row["package_id"])
        for row in backend.stream(
            conn, "SELECT DISTINCT package_id FROM sync_import_progress"
        )
    }
    prior: list[_PriorImport] = []
    for row in backend.fetch(
        conn,
        "SELECT package_id, status, started_at, report_json FROM sync_imports "
        "WHERE source_env = ?",
        (source_env,),
    ):
        package_id = str(row["package_id"])
        prior.append(
            _PriorImport(
                package_id=package_id,
                status=str(row["status"]),
                started_at=_read_moment(row["started_at"]),
                created_at=_recorded_created_at(row["report_json"]),
                heartbeat_at=_recorded_moment(row["report_json"], _HEARTBEAT_AT_KEY),
                has_progress=package_id in with_progress,
                notebooks=_recorded_notebooks(row["report_json"]),
            )
        )
    return tuple(prior)


def _recorded_moment(report_json: Any, key: str) -> datetime | None:
    """One timestamp out of a stored report. psycopg hands back a decoded
    ``dict`` for jsonb; SQLite hands back text."""
    document = report_json
    if isinstance(document, (str, bytes)):
        try:
            document = json.loads(document)
        except ValueError:
            return None
    if not isinstance(document, dict):
        return None
    return _read_moment(document.get(key))


def _recorded_created_at(report_json: Any) -> datetime | None:
    return _recorded_moment(report_json, _PACKAGE_CREATED_AT_KEY)


def _recorded_notebooks(report_json: Any) -> tuple[str, ...] | None:
    """This row's own ``manifest.notebooks``, read back out of a stored
    report. ``None`` when the row predates this bookkeeping (written by both
    ``_running_report_json`` and the full ``ImportReport.as_json``) or its
    JSON is unreadable."""
    document = report_json
    if isinstance(document, (str, bytes)):
        try:
            document = json.loads(document)
        except ValueError:
            return None
    if not isinstance(document, dict):
        return None
    notebooks = document.get("notebooks")
    if not isinstance(notebooks, list):
        return None
    return tuple(str(item) for item in notebooks)


def _reject_backwards_snapshot(
    prior: Sequence[_PriorImport], context: _Context
) -> None:
    """Refuse a package older than the newest one already applied.

    A full package is a snapshot, and phase 3a now DELETES what a snapshot
    does not carry. Importing an older snapshot after a newer one therefore
    does not just add stale rows, it rolls the mirror back -- every row the
    source created between the two packages is swept away. The identical
    package short-circuits as ``already_applied`` long before this, so the
    only thing this can reject is a genuinely older export.
    """
    mine = _read_moment(context.manifest.created_at)
    newest: _PriorImport | None = None
    for row in prior:
        if row.status != _STATUS_DONE or row.package_id == context.manifest.package_id:
            continue
        if row.created_at is None:
            context.ledger.warn(
                f"package {row.package_id} was applied by a build that did not "
                "record its created_at, so this run cannot check that it is "
                "not importing an older snapshot over it"
            )
            continue
        if newest is None or row.created_at > (newest.created_at or row.created_at):
            newest = row
    if newest is None or newest.created_at is None:
        return
    if mine is None:
        raise SyncImportError(
            f"{MANIFEST_NAME} carries no usable created_at, so this package "
            f"cannot be ordered against {newest.package_id}, which this "
            "environment already applied. Re-export it."
        )
    if mine < newest.created_at:
        raise SyncImportError(
            f"this package was exported at {mine.isoformat()}, BEFORE the "
            f"package {newest.package_id} this environment already applied "
            f"({newest.created_at.isoformat()}). Importing it would roll the "
            "mirror back to the older snapshot and delete everything the "
            "source has produced since. Export a current package instead."
        )


def _claim_import(
    backend: _Backend, context: _Context, *, resume: bool, take_over: bool
) -> bool:
    """Take ownership of this source environment's import slot and open (or
    re-open) this package's ``sync_imports`` row, in ONE write transaction.
    Returns True when the package turns out to be already applied in full, in
    which case nothing was claimed and nothing must be written.

    The check and the claim have to be one transaction or they are not a
    check: two importers that both read "nothing is running" would both then
    write a running row. PostgreSQL takes a transaction-scoped advisory lock
    on the source environment so the read is serialized across connections;
    SQLite gets the same guarantee from ``write()``'s process-wide writer
    lock (and, across processes, from the database file's own write lock).

    Everything preflight decided about ORDER is decided again here, on the
    rows read under that lock. Preflight runs on a snapshot taken before the
    lock exists, so between the two another importer can finish a newer
    package -- and the whole point of the ordering rules is that applying an
    older snapshot after a newer one rolls the mirror back. A check made
    outside the lock that admits the package is not a check.

    A row already 'running' for this source environment blocks the run.
    ``take_over=True`` is the operator saying "that process is gone"; it is
    never inferred. There is no duration after which this module declares
    another run dead on its own -- a long import is not an abandoned one, and
    guessing wrong means two importers interleaving on the same target. The
    refusal quotes the other run's last heartbeat so the person deciding has
    the one fact that actually bears on it.

    Declaring a package also RETIRES the half-applied ones it replaces. A
    'failed' package of the same source environment that still has progress
    rows describes a partly-applied older snapshot; once a newer package is
    declared, finishing the older one would re-apply superseded content on top
    of newer, so it is marked 'superseded', its progress rows are dropped, and
    it can never be resumed again. Declaring an OLDER package while such a
    failed one is outstanding is refused outright -- there is no order in
    which applying them both is meaningful.
    """
    manifest = context.manifest
    now = datetime.now(timezone.utc)
    mine = _read_moment(manifest.created_at)
    with backend.write() as conn:
        if backend.is_postgres:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (manifest.source_env,)
            )
        prior = _prior_imports(backend, conn, manifest.source_env)
        own = next(
            (row for row in prior if row.package_id == manifest.package_id), None
        )
        if own is not None and own.status == _STATUS_DONE:
            # Unconditional, and before anything else: another importer
            # finished this very package between preflight and this lock.
            # There is nothing to claim, nothing to resume and nothing to
            # refuse -- it is simply already applied.
            return True
        if own is not None and own.status == _STATUS_SUPERSEDED:
            raise SyncImportError(
                f"package {manifest.package_id} was superseded by a newer "
                "package of the same source environment and its partial "
                "progress has been dropped; it can no longer be resumed. "
                "Export a current package instead."
            )
        _reject_backwards_snapshot(prior, context)
        taken_over: list[_PriorImport] = []
        for row in prior:
            if row.status != _STATUS_RUNNING:
                continue
            other = row.package_id
            heartbeat = (
                row.heartbeat_at.isoformat()
                if row.heartbeat_at is not None
                else "never (it has not committed any step yet)"
            )
            if take_over and other == manifest.package_id:
                # This package's own abandoned run. Taking it over is
                # continuing it, not replacing it: the progress rows are
                # exactly what makes the resume cheap, so they stay.
                context.ledger.warn(
                    "took over this package's own running sync_imports row; "
                    f"its last committed step was at {heartbeat}, and "
                    "take_over asserts that process is gone"
                )
                continue
            if take_over:
                # Somebody ELSE's run. It is retired below, after the same
                # ordering check every other retirement goes through -- taking
                # over another run is not a licence to apply an older snapshot
                # after a newer one.
                taken_over.append(row)
                continue
            raise SyncImportError(
                f"an import from {manifest.source_env!r} is still running "
                f"(package_id {other}); its last committed step was at "
                f"{heartbeat}. Refusing to interleave two imports of the same "
                "source environment. If that process is really gone, re-run "
                "with take_over=True (--take-over)."
            )
        outstanding = [
            row
            for row in prior
            if row.status == _STATUS_FAILED
            and row.has_progress
            and row.package_id != manifest.package_id
        ]
        # Declared BEFORE the supersede, not after: a retired package's
        # transferred `__created__:groups:*` progress rows are re-pointed at
        # THIS package_id (``_transfer_created_group_markers``), and
        # ``sync_import_progress.package_id`` is a foreign key into
        # ``sync_imports.package_id`` -- the row that key points at has to
        # exist before anything can be re-pointed at it. The whole thing is
        # still one transaction, so a refusal inside ``_supersede_outstanding``
        # rolls this INSERT back along with everything else.
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
                _running_report_json(manifest.created_at, now, manifest.notebooks),
            ),
        )
        _supersede_outstanding(
            backend, conn, context, [*outstanding, *taken_over], mine, now
        )
    return False


def _supersede_outstanding(
    backend: _Backend,
    conn: Any,
    context: _Context,
    outstanding: Sequence[_PriorImport],
    mine: datetime | None,
    now: datetime,
) -> None:
    """Retire the half-applied packages of OTHER ids this declaration
    replaces, or refuse if this package is not the newer one.

    Two kinds arrive here, and they get the same treatment because they are
    the same situation: a package that failed partway and still holds
    progress, and a package still marked running whose process the operator
    has just declared gone with ``--take-over``. Either way some prefix of an
    older snapshot is applied and nobody is going to finish it, so leaving the
    row alive would let a later run resume it ON TOP of this newer one. The
    ordering check is the same too -- taking over someone else's run is not a
    licence to apply an older snapshot after a newer one.

    A package is also refused as the superseder of one whose recorded
    notebooks it does not fully cover. The half-applied package's progress is
    about to be dropped -- retiring it is a promise that nothing further will
    ever apply the rest of ITS notebooks, and only a package that carries all
    of them can stand in for that promise. Without this, superseding a
    two-notebook failed package with a one-notebook package would silently
    strand the other notebook: nothing declared for this source environment
    would ever finish it, and nothing would say so.

    Runs inside the claim's transaction, so a package is never declared
    without the retirement that makes declaring it safe."""
    manifest = context.manifest
    mine_notebooks = set(manifest.notebooks)
    for row in outstanding:
        if mine is None or row.created_at is None or mine <= row.created_at:
            state = (
                "failed partway through and still holds progress"
                if row.status == _STATUS_FAILED
                else "is still marked running and you asked to take it over"
            )
            raise SyncImportError(
                f"package {row.package_id} {state} for this source "
                "environment. This package is not newer than it "
                f"({manifest.created_at or '<unknown>'} vs "
                f"{row.created_at.isoformat() if row.created_at else '<unknown>'}"
                "), so there is no order in which applying both is meaningful. "
                "Finish or export past that package first."
            )
        if row.notebooks is None:
            # Predates this bookkeeping (or its report_json is unreadable):
            # there is nothing to compare against, so the coverage question
            # cannot be answered. Warned, not refused -- refusing forever
            # would make an old row permanently unsupersedable.
            context.ledger.warn(
                f"package {row.package_id} carries no recorded notebook list "
                "to check coverage against before superseding it"
            )
            continue
        missing = sorted(set(row.notebooks) - mine_notebooks)
        if missing:
            raise SyncImportError(
                f"package {row.package_id} covered notebook(s) {missing} that "
                f"this package ({manifest.package_id}) does not carry. "
                "Superseding it would drop its progress on those notebooks "
                "with nothing left to ever finish them. Export a package "
                "that covers those notebooks too, or --resume "
                f"{row.package_id} first."
            )
    for row in outstanding:
        _transfer_created_group_markers(backend, conn, context, row.package_id)
        conn.execute(
            backend.sql(
                "DELETE FROM sync_import_progress WHERE package_id = ?"
            ),
            (row.package_id,),
        )
        conn.execute(
            backend.sql(
                "UPDATE sync_imports SET status = ?, finished_at = ?, "
                f"report_json = {_json_cast(backend)} WHERE package_id = ?"
            ),
            (
                _STATUS_SUPERSEDED,
                _moment(backend, now),
                json.dumps(
                    {
                        _PACKAGE_CREATED_AT_KEY: (
                            row.created_at.isoformat() if row.created_at else ""
                        ),
                        _SUPERSEDED_BY_KEY: manifest.package_id,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                row.package_id,
            ),
        )
        taken = (
            ""
            if row.status == _STATUS_FAILED
            else (
                " (it was still marked running; its last committed step was at "
                + (
                    row.heartbeat_at.isoformat()
                    if row.heartbeat_at is not None
                    else "never"
                )
                + ")"
            )
        )
        context.ledger.warn(
            f"package {row.package_id} was superseded by this newer one{taken}; "
            "its partial progress was dropped and it can no longer be resumed"
        )


def _transfer_created_group_markers(
    backend: _Backend, conn: Any, context: _Context, old_package_id: str
) -> None:
    """Move a retired package's ``__created__:groups:<id>`` markers onto the
    package that supersedes it, instead of dropping them with the rest of its
    progress.

    The rest of ``old_package_id``'s progress genuinely describes completed
    STEPS the new package is about to redo from scratch (phase 3a/3b start
    over for every table), so dropping it is correct. A created-group marker
    is a different kind of fact: it is not a step the new run repeats, it is
    the durable record that SOME earlier attempt of this import chain already
    inserted group ``<id>`` and its members have not been seeded yet
    (``_created_parents``, the ``_SEED_PARENT`` gate). ``groups`` is
    ``seed_only``, so once the group row exists the superseding package's own
    pass over ``groups`` finds it already there and never re-records the
    fact -- if the marker were dropped instead of moved, ``group_members``
    would find the group missing from ``_created_parents`` and skip every row
    for it as "already existed at the target", exactly as if this import
    chain had never created that group at all.

    A notebook's equivalent fact needs no such transfer: it lives in the
    notebook row's own ``status``/``sync_origin`` (``_IMPORT_IN_FLIGHT_STATUS``),
    which is read straight from ``notebooks`` and is already keyed by
    ``source_env``+id, not by ``package_id`` -- retiring the old package
    row does not touch it.

    Uses Python's ``str.startswith`` rather than SQL ``LIKE`` for the prefix
    match, same reason as ``_created_parents``: the prefix contains ``_``
    characters, which ``LIKE`` reads as single-character wildcards."""
    prefix = _CREATED_STEP_PREFIX + "groups" + ":"
    new_package_id = context.manifest.package_id
    already_theirs = {
        str(row["table_name"])
        for row in backend.fetch(
            conn,
            "SELECT table_name FROM sync_import_progress WHERE package_id = ?",
            (new_package_id,),
        )
        if str(row["table_name"]).startswith(prefix)
    }
    markers = [
        str(row["table_name"])
        for row in backend.fetch(
            conn,
            "SELECT table_name FROM sync_import_progress WHERE package_id = ?",
            (old_package_id,),
        )
        if str(row["table_name"]).startswith(prefix)
    ]
    for step in markers:
        if step in already_theirs:
            # The new package's own earlier attempt already recorded the same
            # group; nothing to move, and the caller's DELETE drops the old
            # duplicate along with the rest of the retired progress.
            continue
        conn.execute(
            backend.sql(
                "UPDATE sync_import_progress SET package_id = ? "
                "WHERE package_id = ? AND table_name = ?"
            ),
            (new_package_id, old_package_id, step),
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
    context: _Context,
    step: str,
    rows_applied: int,
    completed_at: datetime,
) -> None:
    """Record one completed step AND refresh this run's heartbeat, in the
    caller's transaction. The two belong together: the heartbeat's whole
    meaning is "this import committed work at that moment", so it must be
    written by the same commit that made the work durable, never by a
    background tick that could keep beating for a run that is wedged."""
    conn.execute(
        backend.sql(
            "INSERT INTO sync_import_progress "
            "(package_id, table_name, rows_applied, completed_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (package_id, table_name) DO UPDATE SET "
            "rows_applied = excluded.rows_applied, "
            "completed_at = excluded.completed_at"
        ),
        (context.manifest.package_id, step, rows_applied, _moment(backend, completed_at)),
    )
    conn.execute(
        backend.sql(
            f"UPDATE sync_imports SET report_json = {_json_cast(backend)} "
            "WHERE package_id = ?"
        ),
        (
            _running_report_json(
                context.manifest.created_at, completed_at, context.manifest.notebooks
            ),
            context.manifest.package_id,
        ),
    )


def _running_report_json(
    created_at: str, heartbeat: datetime, notebooks: Sequence[str]
) -> str:
    """``report_json`` for a row that is still running: which snapshot it is
    applying, when it last committed anything, and which notebooks it covers.

    ``notebooks`` is written here (not only in the final ``ImportReport``) so
    that a package taken over while still 'running' -- never reaching the
    ``except`` handler that builds the full report -- still leaves behind
    enough to answer "does the package that supersedes this one cover
    everything this one covered?" (``_supersede_outstanding``)."""
    return json.dumps(
        {
            _PACKAGE_CREATED_AT_KEY: created_at,
            _HEARTBEAT_AT_KEY: heartbeat.isoformat(),
            "notebooks": list(notebooks),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _target_owned(table: str) -> frozenset[str]:
    return frozenset(spec_for(table).target_owned_columns)


# Columns a NEWLY inserted row of this table takes from the import rather
# than from the package, for tables whose overrides do not depend on the run.
#
# ``groups``: the invitation capability (0035_group_invite.sql -- the raw
# token plus its two metadata columns, all three of which GroupStore reads
# back: ``join_by_invite`` resolves a group BY ``invite_token`` and
# ``_group_row`` renders the other two). An invitation is a live, unexpiring
# capability to join: carrying the source's token across would let anyone
# holding a link minted in the OTHER environment walk into this one's group,
# and the partial UNIQUE index on the column would additionally make two
# environments' groups collide on it. An imported group therefore arrives with
# no active link, exactly as a freshly created one does; a group admin here
# mints their own.
_STATIC_INSERT_OVERRIDES: dict[str, dict[str, Any]] = {
    "groups": {
        "invite_token": None,
        "invite_created_at": None,
        "invite_created_by": None,
    },
}


def _insert_overrides(table: str, context: _Context) -> dict[str, Any]:
    """Values a NEWLY inserted row takes from this import rather than from the
    package -- each one a decision this environment owns and the source cannot
    make for it.

    ``notebooks`` is the run-dependent one:

    - ``sync_origin``: stamped at INSERT, not only in phase 5, so a crash
      between the two never leaves a row that looks like a local notebook.
    - ``status``: the in-flight marker, flipped in phase 5.
    - ``is_shared``/``share_token``: link sharing is the target's own decision
      (the mirror fence in deps.py lets ``notebook:configure`` through on a
      mirror precisely because of that), and carrying the source's token
      across would publish a link nobody at this environment created.
      ``copy_notebook`` mints a fresh token for the same reason.

    Everything else comes from ``_STATIC_INSERT_OVERRIDES``.
    """
    if table == "notebooks":
        return {
            "sync_origin": context.manifest.source_env,
            "status": _IMPORT_IN_FLIGHT_STATUS,
            "is_shared": 0,
            "share_token": None,
        }
    return dict(_STATIC_INSERT_OVERRIDES.get(table, {}))


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


# An authorization/membership table, the column pointing at the row it
# authorizes, and the table that row lives in. These are seeded ONLY together
# with a parent this import created: once the target owns a notebook or a
# group, who may read it is the target's decision, and a re-sync must not
# resurrect a membership an administrator there revoked, nor push the source's
# member list into a group that already existed here.
#
# ``seed_only`` alone does not say this. It is row-level ("do not overwrite a
# row that exists"), which still INSERTS a row the target deleted -- exactly
# the revoked-membership case. The parent gate is the missing half.
#
# ``object_schemas`` is deliberately absent: it is an environment-level
# registry owned by no parent row, so plain row-level seed_only is the whole
# rule for it (§3.1).
_SEED_PARENT: dict[str, tuple[str, str]] = {
    "notebook_members": ("notebook_id", "notebooks"),
    "notebook_grants": ("notebook_id", "notebooks"),
    "group_members": ("group_id", "groups"),
}

# ``sync_import_progress.table_name`` prefix for "this package created parent
# row X". Not a table name and can never collide with one. Used only for
# parents that have no durable in-row marker of their own -- see
# ``_created_parents``.
_CREATED_STEP_PREFIX = "__created__:"

# Parent tables whose created-set has to be written to sync_import_progress.
# ``notebooks`` is NOT one: a notebook this run created wears
# ``_IMPORT_IN_FLIGHT_STATUS`` until phase 5, which is already a durable,
# per-row, crash-surviving marker -- and there can be thousands of notebooks
# in one package, which would be thousands of progress rows. ``groups`` has no
# such column, and a package carries only the handful of groups its notebooks'
# grants reference, so recording those explicitly is cheap.
_PERSISTED_CREATED_PARENTS = frozenset({"groups"})


def _created_step(parent_table: str, parent_id: str) -> str:
    return f"{_CREATED_STEP_PREFIX}{parent_table}:{parent_id}"


def _created_parents(
    backend: _Backend, conn: Any, parent_table: str, context: _Context
) -> set[str]:
    """Which rows of ``parent_table`` THIS package created -- including in an
    earlier, crashed attempt of the same package, because the gate has to give
    the same answer before and after a resume."""
    if parent_table == "notebooks":
        found: set[str] = set()
        for batch in _batched(list(context.manifest.notebooks)):
            placeholders = ",".join("?" for _ in batch)
            for row in backend.fetch(
                conn,
                f"SELECT id FROM notebooks WHERE id IN ({placeholders}) "
                "AND status = ? AND sync_origin = ?",
                (
                    *batch,
                    _IMPORT_IN_FLIGHT_STATUS,
                    context.manifest.source_env,
                ),
            ):
                found.add(str(row["id"]))
        return found
    # Read the package's whole progress list and filter in Python rather than
    # with LIKE: the prefix contains '_' characters, which LIKE reads as
    # single-character wildcards, and a bounded list (one row per synced table
    # plus one per created group) is not worth a pattern this module would
    # then have to escape correctly on two dialects.
    prefix = _CREATED_STEP_PREFIX + parent_table + ":"
    persisted = {
        str(row["table_name"])[len(prefix) :]
        for row in backend.fetch(
            conn,
            "SELECT table_name FROM sync_import_progress WHERE package_id = ?",
            (context.manifest.package_id,),
        )
        if str(row["table_name"]).startswith(prefix)
    }
    return persisted | context.created_parents.get(parent_table, set())


def _record_created_parents(
    backend: _Backend, conn: Any, context: _Context, table: str
) -> None:
    if table not in _PERSISTED_CREATED_PARENTS:
        return
    moment = datetime.now(timezone.utc)
    for parent_id in sorted(context.created_parents.get(table, set())):
        _record_progress(
            backend,
            conn,
            context,
            _created_step(table, parent_id),
            0,
            moment,
        )


# A child table whose rows the SOURCE owns outright (§5: "源端产生的记忆行以
# 源端为准"), as ``child -> (column pointing at the parent, parent table)``.
#
# Upsert alone is not enough for these two, and not only for policy reasons:
# both carry a UNIQUE constraint BESIDES their primary key --
# ``uq_memory_revisions_memory_id_revision (memory_id, revision)`` and
# ``uq_memory_provenance_memory_id (memory_id)``. A target-side confirmation
# or edit of a mirrored memory mints a row with a NEW id but the SAME
# (memory_id, revision), so the package's row conflicts on the id index and
# the target's row conflicts on the other one -- ``ON CONFLICT (id) DO
# UPDATE`` then raises a unique violation and fails the whole import. Deleting
# what the source no longer has, before inserting, is both the §5 semantics
# and the only way the statement can succeed at all.
#
# ``memory_embeddings`` is deliberately absent: its primary key IS
# ``memory_id`` and it has no second unique index, so an upsert already
# replaces the target's row. ``memory_items`` itself is absent too -- the
# target's own memories live in that table beside the mirrored ones and must
# not be touched, which is exactly why the prune is scoped to the parent ids
# the package carries.
_SOURCE_AUTHORITATIVE: dict[str, tuple[str, str]] = {
    "memory_provenance": ("memory_id", "memory_items"),
    "memory_revisions": ("memory_id", "memory_items"),
}

# Ceiling on one pruning DELETE's bind list (parents + retained ids together).
# Well under SQLite's SQLITE_MAX_VARIABLE_NUMBER and cheap to plan on
# PostgreSQL, while still deleting many parents per statement.
_PRUNE_BINDS = 500


def _package_column(context: _Context, table: str, column: str) -> Iterator[Any]:
    """Stream one column out of a package rows file. Read from the package
    rather than from what this run happens to have applied, so a resumed run
    and a fresh one compute the same set."""
    for row in _iter_lines(context.package_dir / rows_path(table)):
        yield decode_value(row.get(column))


def _prune_superseded(
    backend: _Backend, conn: Any, table: str, context: _Context, primary_key: Sequence[str]
) -> int:
    """Delete the target's rows for this table that the package no longer has,
    scoped to the parents the package carries.

    A parent the package does NOT carry is untouched: those are the target's
    own memories, which §5 leaves alone. A parent the package DOES carry has
    its whole child set replaced by the package's, including the case where
    the package carries none for it.
    """
    child_column, parent_table = _SOURCE_AUTHORITATIVE[table]
    if len(primary_key) != 1:
        raise SyncImportError(
            f"{table}: source-authoritative pruning needs a single-column "
            f"primary key, found {tuple(primary_key)}"
        )
    key = primary_key[0]
    retained: dict[Any, set[Any]] = {}
    for row in _iter_lines(context.package_dir / rows_path(table)):
        parent = decode_value(row.get(child_column))
        retained.setdefault(parent, set()).add(decode_value(row.get(key)))
    parents = [
        parent
        for parent in _package_column(context, parent_table, "id")
        if parent is not None and parent != ""
    ]
    deleted = 0
    for batch in _prune_batches(parents, retained):
        keep = sorted(
            {item for parent in batch for item in retained.get(parent, ())}, key=repr
        )
        parent_slots = ",".join("?" for _ in batch)
        statement = (
            f"DELETE FROM {_ident(table)} "
            f"WHERE {_ident(child_column)} IN ({parent_slots})"
        )
        params: list[Any] = list(batch)
        if keep:
            statement += f" AND {_ident(key)} NOT IN ({','.join('?' for _ in keep)})"
            params.extend(keep)
        cursor = conn.execute(backend.sql(statement), tuple(params))
        deleted += max(0, int(getattr(cursor, "rowcount", 0) or 0))
    if deleted:
        context.ledger.warn(
            f"{table}: removed {deleted} target row(s) the source no longer "
            "has; the source environment owns this table's rows for the "
            "memories it carries (docs/incremental-sync-design.md §5)"
        )
    return deleted


def _prune_batches(
    parents: Sequence[Any], retained: Mapping[Any, set[Any]]
) -> Iterator[list[Any]]:
    """Group parents so that one DELETE's parent slots plus its retained-id
    slots stay under ``_PRUNE_BINDS``. A single parent with more retained
    children than that still gets its own statement -- correctness first."""
    batch: list[Any] = []
    binds = 0
    for parent in parents:
        cost = 1 + len(retained.get(parent, ()))
        if batch and binds + cost > _PRUNE_BINDS:
            yield batch
            batch, binds = [], 0
        batch.append(parent)
        binds += cost
    if batch:
        yield batch


# SQLite's two hand-maintained FTS5 virtual tables, as
# ``base table -> (fts table, rebuild INSERT ... SELECT)``.
#
# ``chunks_fts`` and ``kg_objects_fts`` have NO triggers (only
# ``memory_items_fts`` does -- migrations.py builds three for it), so every
# writer maintains them by hand: ``ChunkStore.insert_rows`` writes chunk rows,
# ``KnowledgeLifecycle`` writes object rows, and each bulk path that bypasses
# those -- ``knowhow_transfer_store`` for a transferred table,
# ``notebook_sharing`` for a deep copy -- re-does it explicitly, in the same
# transaction, for exactly this reason. An importer is another such path: land
# the rows without these and the mirror has vector recall but is permanently
# invisible to lexical search, with no self-healing probe to notice (the
# vector side has one, FTS does not). The shadow manifest classifies both as
# REBUILT for the same reason: they are derived, and rebuilding them is always
# the right answer.
#
# Rebuild, not merge: DELETE the package's notebooks out of the index and
# re-project the base table as it stands. That makes the operation idempotent
# and makes a resumed or twice-run phase converge, where an INSERT-only patch
# would duplicate rows in a table with no unique constraint to stop it.
#
# The ``name`` projection mirrors knowledge_lifecycle.py's fts_rows exactly:
# ``payload -> 'name'``, and only when it is non-blank.
# PostgreSQL needs none of this -- its lexical indexes are GIN indexes ON the
# column, maintained by the row write itself.
_SQLITE_FTS_REBUILD: dict[str, tuple[str, str]] = {
    "chunks": (
        "chunks_fts",
        "INSERT INTO chunks_fts(chunk_id, notebook_id, text) "
        "SELECT id, notebook_id, COALESCE(text, '') FROM chunks "
        "WHERE notebook_id IN ({placeholders})",
    ),
    "knowledge_objects": (
        "kg_objects_fts",
        "INSERT INTO kg_objects_fts(object_id, notebook_id, name) "
        "SELECT id, notebook_id, json_extract(payload, '$.name') "
        "FROM knowledge_objects WHERE notebook_id IN ({placeholders}) "
        "AND TRIM(COALESCE(json_extract(payload, '$.name'), '')) <> ''",
    ),
}


def _rebuild_sqlite_fts(
    backend: _Backend, conn: Any, table: str, context: _Context
) -> None:
    """Re-project this table's SQLite lexical index for the package's
    notebooks, inside the caller's transaction. A no-op on PostgreSQL and for
    every table that has no hand-maintained index."""
    if backend.is_postgres:
        return
    entry = _SQLITE_FTS_REBUILD.get(table)
    if entry is None or not context.manifest.notebooks:
        return
    index, insert = entry
    for batch in _batched(list(context.manifest.notebooks)):
        placeholders = ",".join("?" for _ in batch)
        conn.execute(
            f"DELETE FROM {_ident(index)} WHERE notebook_id IN ({placeholders})",
            tuple(batch),
        )
        conn.execute(insert.format(placeholders=placeholders), tuple(batch))


def _unique_keys(
    backend: _Backend, conn: Any, table: str, primary_key: Sequence[str]
) -> tuple[tuple[str, ...], ...]:
    """The table's UNIQUE constraints other than its primary key, read from
    the target's catalog.

    A primary key is not a table's only identity, and an upsert only knows
    about one of them. Reading the rest from the catalog rather than listing
    them here means a constraint added by a later migration is covered the day
    it exists, with no second roster to keep in step.

    PARTIAL unique indexes are skipped on purpose: their constraint only binds
    rows satisfying a predicate this module does not evaluate (today
    ``notebooks.share_token``, ``sources.memory_id``, ``groups.invite_token``
    and ``memory_items(created_by, source_answer_id)``), so treating them as
    unconditional would reject imports over collisions the database would
    never raise.
    """
    key_set = set(primary_key)
    found: list[tuple[str, ...]] = []
    if backend.is_postgres:
        rows = backend.fetch(
            conn,
            "SELECT i.indexrelid AS oid, "
            "  (SELECT array_agg(a.attname ORDER BY k.ord) FROM "
            "     unnest(i.indkey::smallint[]) WITH ORDINALITY AS k(attnum, ord) "
            "     JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "       AND a.attnum = k.attnum) AS names "
            "FROM pg_index i WHERE i.indrelid = ?::regclass "
            "AND i.indisunique AND NOT i.indisprimary AND i.indpred IS NULL",
            (table,),
        )
        for row in rows:
            names = tuple(str(name) for name in (row["names"] or ()))
            if names and set(names) != key_set:
                found.append(names)
        return tuple(found)
    for index in backend.fetch(conn, f"PRAGMA index_list({_ident(table)})"):
        if int(index["unique"]) != 1 or int(index["partial"] or 0) == 1:
            continue
        if str(index["origin"]) == "pk":
            continue
        names = tuple(
            str(column["name"])
            for column in backend.fetch(
                conn, f"PRAGMA index_info({_ident(str(index['name']))})"
            )
        )
        if names and set(names) != key_set:
            found.append(names)
    return tuple(found)


def _assert_no_unique_key_collision(
    backend: _Backend,
    conn: Any,
    table: str,
    keys: Sequence[tuple[str, ...]],
    rows: Sequence[Mapping[str, Any]],
    primary_key: Sequence[str],
) -> None:
    """Refuse, by name, a package row that would collide with a target row on
    a UNIQUE constraint other than the primary key.

    The reconcile phase removes most of these before they can happen: a row
    the source replaced is gone by the time the replacement is inserted. What
    reaches here is a collision against a row the sweep is NOT allowed to
    touch -- something in the exempt set, or under a target-owned source's
    protection. That is a genuine conflict between the two environments, and
    the useful outcome is an error naming the table, the key and both row ids,
    not an ``IntegrityError`` from the driver with neither.
    """
    for key in keys:
        wanted: dict[tuple[Any, ...], tuple[Any, ...]] = {}
        for row in rows:
            if any(column not in row for column in key):
                break
            wanted[tuple(row[column] for column in key)] = tuple(
                row.get(column) for column in primary_key
            )
        else:
            _probe_unique_key(
                backend, conn, table, key, wanted, primary_key
            )


def _probe_unique_key(
    backend: _Backend,
    conn: Any,
    table: str,
    key: Sequence[str],
    wanted: Mapping[tuple[Any, ...], tuple[Any, ...]],
    primary_key: Sequence[str],
) -> None:
    if not wanted:
        return
    projection = ", ".join(
        _ident(column) for column in (*primary_key, *key) if column
    )
    leading = sorted({item[0] for item in wanted}, key=repr)
    for chunk in _batched(leading):
        placeholders = ",".join("?" for _ in chunk)
        for row in backend.fetch(
            conn,
            f"SELECT {projection} FROM {_ident(table)} "
            f"WHERE {_ident(key[0])} IN ({placeholders})",
            chunk,
        ):
            found = tuple(row[column] for column in key)
            mine = wanted.get(found)
            if mine is None:
                continue
            theirs = tuple(row[column] for column in primary_key)
            if theirs != mine:
                raise SyncImportError(
                    f"{table}: the package's row {mine} and the target's row "
                    f"{theirs} both claim {tuple(key)}={found}, and the "
                    "target's row is one this import must not remove "
                    "(an exempt table, or under a target-owned memory "
                    "source). Resolve it at the target before re-importing."
                )


def _assert_target_ownership(
    backend: _Backend,
    conn: Any,
    table: str,
    scope: TableScope,
    primary_key: Sequence[str],
    keys: Sequence[tuple[Any, ...]],
    context: _Context,
) -> None:
    """Refuse a package row whose primary key already belongs, at the
    target, to a notebook (or PARENT-scope owner) outside this package.

    ``manifest.json`` is not the only thing about a package that cannot be
    trusted (module docstring) -- neither can a row's PRIMARY KEY. Ids are
    exported verbatim, never reissued per environment, so a package can
    legitimately reuse a primary key the target already has under a
    completely different notebook. When it does, ``ON CONFLICT (pk) DO
    UPDATE`` in ``_upsert_statement`` rewrites that pre-existing row's scope
    column right along with everything else it touches -- silently
    reattributing someone else's row into this package's notebook (codex
    #772 round 12, reproduced against ``knowledge_objects``).

    ``_TargetKeys.present`` cannot catch this by itself: for a
    NOTEBOOK/GLOBAL-scoped table it only preloads keys already inside THIS
    package's own notebook set (``_TargetKeys.__init__``), so a same-PK row
    that belongs to a different notebook is invisible to it and reads as
    "does not exist yet" even though the database's own conflict target is
    about to collide with it. This runs a second, UNSCOPED-by-notebook
    lookup keyed only on the primary key -- ``WHERE pk[0] IN (...)``, refined
    in Python exactly like ``_probe_unique_key`` -- and checks every
    existing row's own scope value against the set this package actually
    carries:

    - NOTEBOOK scope: the row's own scope column must be one of
      ``context.manifest.notebooks``.
    - PARENT scope: the row's own scope column must be one of the primary
      keys ``context.parent_key_sets`` recorded for ``scope.parent_table`` --
      this package's own copy of the parent table, computed once by
      ``_verify_row_scopes`` during preflight.

    Never called for a ``seed_only`` table (its upsert is a bare
    ``ON CONFLICT DO NOTHING``, so a pre-existing row -- whoever it belongs
    to -- is never touched) or for a table with no primary key (replaced
    wholesale by notebook in ``_replace_scope``, never upserted through a
    conflict target that could straddle scopes) -- the caller only reaches
    here for a table where neither exemption applies.
    """
    if scope.kind is ScopeKind.NOTEBOOK:
        allowed = set(context.manifest.notebooks)
    elif scope.kind is ScopeKind.PARENT:
        allowed = context.parent_key_sets.get(scope.parent_table)
        if allowed is None:
            raise SyncImportError(
                f"{table}: no package-side primary-key set was recorded for "
                f"its PARENT scope's parent table {scope.parent_table!r} "
                "-- _verify_row_scopes must run, in synced_tables() order, "
                "before this table is applied"
            )
    else:
        # Unreachable today: every GLOBAL-scoped table in SYNC_MANIFEST is
        # seed_only, and the caller never reaches here for one. Kept as an
        # explicit refusal rather than a silent bypass, so a future GLOBAL
        # table that is NOT seed_only fails loudly instead of skipping this
        # guard by accident.
        raise SyncImportError(
            f"{table}: GLOBAL-scoped and not seed_only; row-ownership is "
            "undefined for that combination -- add a rule here in the same "
            "change that adds such a table"
        )
    wanted = set(keys)
    if not wanted:
        return
    projection = ", ".join(
        _ident(column) for column in (*primary_key, scope.column)
    )
    leading = sorted({key[0] for key in wanted}, key=repr)
    for chunk in _batched(leading):
        placeholders = ",".join("?" for _ in chunk)
        for row in backend.fetch(
            conn,
            f"SELECT {projection} FROM {_ident(table)} "
            f"WHERE {_ident(primary_key[0])} IN ({placeholders})",
            chunk,
        ):
            found = tuple(row[column] for column in primary_key)
            if found not in wanted:
                continue
            theirs = str(row[scope.column] or "")
            if theirs not in allowed:
                raise SyncImportError(
                    f"{table}: primary key {found} already exists at the "
                    f"target under {scope.column}={theirs!r}, which is "
                    "outside this package's scope. Refusing to reattribute "
                    "an existing row to this import; resolve the collision "
                    "at the target before re-importing."
                )


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

    unique_keys = (
        ()
        if spec.seed_only or not primary_key
        # A seed_only table inserts with a bare ON CONFLICT DO NOTHING, which
        # cannot raise on any index; a keyless table has no upsert at all.
        else _unique_keys(backend, conn, table, primary_key)
    )
    if not primary_key:
        _replace_scope(backend, conn, table, context)
    if table in _SOURCE_AUTHORITATIVE:
        _prune_superseded(backend, conn, table, context, primary_key)
    # A seed_only upsert is a bare ON CONFLICT DO NOTHING, so a pre-existing
    # row is never touched regardless of who it belongs to; a keyless table
    # is replaced wholesale above rather than upserted. Neither can reattribute
    # someone else's row, so neither needs the ownership recheck below.
    check_ownership = bool(primary_key) and not spec.seed_only
    scope = spec.scope
    if check_ownership and scope is None:
        raise SyncImportError(f"{table}: no import scope to verify row ownership against")
    known = _TargetKeys(backend, conn, table, primary_key, context)
    seed_parent = _SEED_PARENT.get(table)
    seedable = (
        _created_parents(backend, conn, seed_parent[1], context)
        if seed_parent is not None
        else None
    )
    records_created = table in _PERSISTED_CREATED_PARENTS

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
        if seedable is not None:
            column, parent = seed_parent  # type: ignore[misc]
            allowed: list[dict[str, Any]] = []
            for row in kept:
                if str(row.get(column) or "") in seedable:
                    allowed.append(row)
                    continue
                skipped += 1
                context.ledger.skip(
                    table,
                    _row_key(row, primary_key),
                    f"{parent} row {row.get(column)!r} already existed at the "
                    "target; authorization rows are seeded only with the "
                    "parent this import created",
                )
            kept = allowed
        if not kept:
            continue
        if unique_keys:
            _assert_no_unique_key_collision(
                backend, conn, table, unique_keys, kept, primary_key
            )
        keys = [tuple(row.get(column) for column in primary_key) for row in kept]
        if check_ownership:
            # Before the upsert, in this same write transaction: an existing
            # target row whose primary key this batch is about to write, but
            # whose own scope value falls outside this package, must not be
            # silently reattributed by ON CONFLICT DO UPDATE.
            assert scope is not None  # narrowed above alongside check_ownership
            _assert_target_ownership(
                backend, conn, table, scope, primary_key, keys, context
            )
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
                if records_created and len(primary_key) == 1:
                    context.created_parents.setdefault(table, set()).add(str(key[0]))
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
    _rebuild_sqlite_fts(backend, conn, table, context)
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
    if table in _SOURCE_LINK and context.protected_sources:
        # The wholesale clear has to spare a target-owned memory source's
        # rows for the same reason the snapshot sweep does. With no primary
        # key to delete by, the surviving rows are decided here and the rest
        # are deleted by full-row equality -- the same shape _prune_table
        # uses, and one that needs no unbounded NOT IN list.
        _delete_unprotected_rows(backend, conn, table, context)
        return
    for batch in _batched(list(context.manifest.notebooks)):
        placeholders = ",".join("?" for _ in batch)
        conn.execute(
            backend.sql(
                f"DELETE FROM {_ident(table)} "
                f"WHERE {_ident(scope.column)} IN ({placeholders})"
            ),
            tuple(batch),
        )


def _delete_unprotected_rows(
    backend: _Backend, conn: Any, table: str, context: _Context
) -> None:
    columns = [
        name
        for name in backend.columns(conn, table)
        if not (name == "ordinal" and table in POSTGRES_ROWID_ORDINAL_TABLES)
    ]
    statement = _scope_select(backend, conn, table, columns, source_link=True)
    doomed: list[tuple[Any, ...]] = []
    for batch in _batched(list(context.manifest.notebooks)):
        placeholders = ",".join("?" for _ in batch)
        for row in backend.stream(
            conn, statement.format(placeholders=placeholders), batch
        ):
            if str(row[_SOURCE_LINK_COLUMN] or "") in context.protected_sources:
                continue
            doomed.append(tuple(row[name] for name in columns))
    if not doomed:
        return
    predicate = " AND ".join(f"{_ident(name)} = ?" for name in columns)
    for start in range(0, len(doomed), _ROW_BATCH):
        _executemany(
            backend,
            conn,
            f"DELETE FROM {_ident(table)} WHERE {predicate}",
            doomed[start : start + _ROW_BATCH],
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
    resumed = done & set(synced_tables())  # bookkeeping steps are not tables
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
            # In the SAME transaction as the rows it describes: "this package
            # created group G" and the G row itself must commit together, or a
            # crash between them makes the gate lie on the way back.
            _record_created_parents(backend, conn, context, table)
            _record_progress(
                backend,
                conn,
                context,
                table,
                outcome.inserted + outcome.updated,
                datetime.now(timezone.utc),
            )
        context.ledger.tables[table] = outcome


# --------------------------------------------- phase 3b -- snapshot reconcile


# Tables the full-snapshot prune deliberately leaves alone, beyond the ones
# the filter below already excludes by shape (GLOBAL scope, seed_only, no
# primary key):
#
# - ``notebooks``: the package's notebook list IS the scope of the whole
#   import. "A notebook of this package that the package does not carry" is
#   not a thing, and retiring a mirror is an operator decision (§5), not a
#   side effect of syncing a different one.
# - ``memory_items`` and its three child tables: the target's users create
#   memories on a mirror, and §5 explicitly lets those coexist with the
#   source's. Nothing on a memory row says which side made it, so a
#   notebook-scoped "not in the package" sweep cannot tell "the source
#   deleted this" from "the target created this" -- and it would silently
#   destroy the second. Source-side deletions of memories therefore wait for
#   PR-3's change log, which replays them by key instead of inferring them.
#   ``memory_provenance``/``memory_revisions`` still get the narrower,
#   parent-scoped ``_SOURCE_AUTHORITATIVE`` prune, which only ever touches
#   memories the package actually carries.
_SNAPSHOT_PRUNE_EXEMPT = frozenset(
    {
        "notebooks",
        "memory_items",
        "memory_embeddings",
        "memory_provenance",
        "memory_revisions",
    }
)

_PRUNE_STEP_PREFIX = "__prune__:"


def _prune_step(table: str) -> str:
    return f"{_PRUNE_STEP_PREFIX}{table}"


def _prunable_tables(backend: _Backend, conn: Any) -> tuple[str, ...]:
    """Synced tables the snapshot prune covers, children before parents.

    Reverse ``synced_tables()`` order: that ordering puts a declared foreign
    key's parent before its child, so walking it backwards deletes a child
    before the parent it points at and no delete has to rely on ON DELETE
    CASCADE firing the way this module happens to expect.
    """
    chosen: list[str] = []
    for table in reversed(synced_tables()):
        spec = spec_for(table)
        if table in _SNAPSHOT_PRUNE_EXEMPT or spec.seed_only:
            continue
        scope = spec.scope
        if scope is None or scope.kind is ScopeKind.GLOBAL:
            continue
        if not backend.primary_key(conn, table):
            # No primary key: _replace_scope already cleared and re-inserted
            # this table's rows for the package's notebooks, which IS the
            # reconciliation.
            continue
        chosen.append(table)
    return tuple(chosen)


# How a row of this table reaches the ``sources`` row it was derived from, as
# a SQL expression over the alias ``t0``. Used to spare a target-owned
# synthetic memory source's closure from the snapshot sweep -- see
# ``_protected_sources``. A table absent from here has no path to a source at
# all, or has one this module does not follow; see that function's docstring
# for the boundary.
_SOURCE_LINK_COLUMN = "__sync_source__"

_DIRECT_SOURCE_TABLES = (
    "chunks",
    "chunk_questions",
    "element_embeddings",
    "knowledge_object_sources",
    "knowledge_objects",
    "knowledge_relations",
    "knowledge_source_facts",
    "knowledge_source_fact_elements",
    "kg_source_profiles",
    "notebook_assets",
    "source_authors",
    "source_elements",
    "source_paper_meta",
)

_SOURCE_LINK: dict[str, str] = {
    # The source row itself.
    "sources": 't0."id"',
    # Reached through the chunk / object / relation the row hangs off, for the
    # three vector-ish tables that carry no source_id of their own.
    "chunk_embeddings": (
        '(SELECT c."source_id" FROM "chunks" c WHERE c."id" = t0."chunk_id")'
    ),
    "chunk_elements": (
        '(SELECT c."source_id" FROM "chunks" c WHERE c."id" = t0."chunk_id")'
    ),
    "knowledge_embeddings": (
        '(SELECT o."source_id" FROM "knowledge_objects" o '
        'WHERE o."id" = t0."object_id")'
    ),
    "relation_embeddings": (
        '(SELECT r."source_id" FROM "knowledge_relations" r '
        'WHERE r."id" = t0."relation_id")'
    ),
    **{table: 't0."source_id"' for table in _DIRECT_SOURCE_TABLES},
}


def _protected_sources(
    backend: _Backend, conn: Any, context: _Context
) -> frozenset[str]:
    """Sources in the package's notebooks that the TARGET owns, and whose
    derived rows the snapshot sweep must not touch.

    Confirming a memory on a mirror runs ``ingest_memory_source``, which
    materializes a synthetic ``source_type='memory'`` row pointing at that
    memory, plus its elements, chunks, vectors and knowledge objects. Those
    rows are in the package's notebooks and are not in the package, so a
    notebook-scoped sweep would read them as "the source deleted these" and
    destroy a memory's entire retrievable form on every sync. A source is the
    target's exactly when it is synthetic AND its memory is not one the
    package carries -- a synthetic source for a MIRRORED memory does come from
    the package and is reconciled normally.

    **Known boundary**: only rows that reach a source are spared. The KG
    aggregates aggregated ACROSS sources -- communities, community_members,
    concept_clusters, canonical_relations, concept_comentions, mention_edges,
    kg_community_edges -- carry no source id, so a local memory's
    contribution to them is swept and comes back on the target's next KG
    rebuild (§6 already requires one after an import). Registered here rather
    than papered over.
    """
    package_memories = {
        value
        for value in _package_column(context, "memory_items", "id")
        if isinstance(value, str) and value
    }
    protected: set[str] = set()
    for batch in _batched(list(context.manifest.notebooks)):
        placeholders = ",".join("?" for _ in batch)
        for row in backend.stream(
            conn,
            "SELECT id, memory_id FROM sources "
            f"WHERE notebook_id IN ({placeholders}) AND source_type = 'memory'",
            batch,
        ):
            memory = str(row["memory_id"] or "")
            if memory and memory not in package_memories:
                protected.add(str(row["id"]))
    return frozenset(protected)


def _scope_select(
    backend: _Backend,
    conn: Any,
    table: str,
    columns: Sequence[str],
    *,
    source_link: bool = False,
) -> str:
    """``SELECT <columns> FROM <table>`` restricted to a bind list of notebook
    ids, following the table's scope -- its own column, or the join chain up
    to the ancestor that has one. Reuses the exporter's join builder so the
    two sides resolve a scope the same way. ``source_link`` adds the row's
    owning source id as an extra projected column."""
    projected = [f"t0.{_ident(column)}" for column in columns]
    if source_link:
        projected.append(f"{_SOURCE_LINK[table]} AS {_ident(_SOURCE_LINK_COLUMN)}")
    projection = ", ".join(projected)
    head = f"SELECT {projection} FROM {_ident(table)} t0"
    scope = spec_for(table).scope
    if scope is None:
        raise SyncImportError(f"{table}: LOCAL table has no import scope")
    if scope.kind is ScopeKind.NOTEBOOK:
        return f"{head} WHERE t0.{_ident(scope.column)} IN ({{placeholders}})"
    try:
        joins, filtered = _parent_join_clause(backend, conn, table)
    except SyncExportError as exc:
        raise SyncImportError(str(exc)) from None
    return f"{head} {joins} WHERE {filtered} IN ({{placeholders}})"


def _prune_table(
    backend: _Backend, conn: Any, table: str, context: _Context
) -> int:
    """Delete the target's rows of ``table`` that belong to this package's
    notebooks but are not in the package. Returns how many went."""
    primary_key = backend.primary_key(conn, table)
    carried = {
        tuple(decode_value(row.get(column)) for column in primary_key)
        for row in _iter_lines(context.package_dir / rows_path(table))
    }
    linked = table in _SOURCE_LINK and bool(context.protected_sources)
    statement = _scope_select(
        backend, conn, table, primary_key, source_link=linked
    )
    stale: list[tuple[Any, ...]] = []
    for batch in _batched(list(context.manifest.notebooks)):
        placeholders = ",".join("?" for _ in batch)
        for row in backend.stream(
            conn, statement.format(placeholders=placeholders), batch
        ):
            if linked and str(
                row[_SOURCE_LINK_COLUMN] or ""
            ) in context.protected_sources:
                continue
            key = tuple(row[column] for column in primary_key)
            if key not in carried:
                stale.append(key)
    if not stale:
        return 0
    predicate = " AND ".join(f"{_ident(column)} = ?" for column in primary_key)
    # Full-key equality through executemany rather than an IN list: it is the
    # one form that works for a composite primary key on both dialects, and
    # the rows reaching it are by definition only the stale ones.
    for start in range(0, len(stale), _ROW_BATCH):
        _executemany(
            backend,
            conn,
            f"DELETE FROM {_ident(table)} WHERE {predicate}",
            stale[start : start + _ROW_BATCH],
        )
    _rebuild_sqlite_fts(backend, conn, table, context)
    return len(stale)


def _prune_snapshot(backend: _Backend, context: _Context, done: set[str]) -> None:
    """Make each reconciled table hold exactly what the package says.

    A full package is a snapshot, so a row the target has for one of the
    package's notebooks and the package does not is a row the source deleted.
    Upsert alone can never notice that -- it only ever adds and overwrites --
    so a mirror would accumulate every chunk, element and knowhow cell the
    source ever removed.
    """
    total = 0
    with backend.read() as conn:
        tables = _prunable_tables(backend, conn)
        context.protected_sources = _protected_sources(backend, conn, context)
    if context.protected_sources:
        context.ledger.warn(
            f"{len(context.protected_sources)} target-owned memory source(s) "
            "and the rows derived from them were spared the reconciliation; "
            "their contribution to the cross-source KG aggregates is not "
            "(see _protected_sources)"
        )
    for table in tables:
        step = _prune_step(table)
        if step in done:
            continue
        with backend.write() as conn:
            removed = _prune_table(backend, conn, table, context)
            _record_progress(
                backend,
                conn,
                context,
                step,
                removed,
                datetime.now(timezone.utc),
            )
        total += removed
    if total:
        context.ledger.warn(
            f"removed {total} target row(s) this full package no longer "
            "carries for the notebooks it covers"
        )
    context.ledger.warn(
        "memory_items and its revision/provenance/embedding rows are NOT "
        "reconciled against the package: the target's own memories live in "
        "those tables beside the mirrored ones and carry no marker that "
        "separates them, so a source-side memory deletion is replayed by "
        "PR-3's change log rather than inferred here "
        "(docs/incremental-sync-design.md §5)"
    )


# ----------------------------------------------------------- phase 4 -- files


# ``package subdirectory under files/`` -> ``storage/<subdirectory>/``. The
# same pair the exporter copies and notebook delete cleans up together.
_FILE_ROOTS = (NOTEBOOK_FILES_DIR, ASSET_FILES_DIR)

_STAGED_SUFFIX = ".sync-tmp"
# A retired directory names the package that retired it. Without the id, a
# LATER package's reconciliation cannot tell "my own interrupted swap, adopt
# it" from "somebody else's leftovers, do not touch". The prefix is what a
# scan matches on; the id is what decides ownership.
_RETIRED_PREFIX = ".sync-old-"


def _retired_path(destination: Path, package_id: str) -> Path:
    return destination.with_name(destination.name + _RETIRED_PREFIX + package_id)


def _retired_siblings(destination: Path) -> list[tuple[Path, str]]:
    """``(path, owning package id)`` for every retired copy of this directory,
    whoever left it."""
    prefix = destination.name + _RETIRED_PREFIX
    parent = destination.parent
    if not parent.is_dir():
        return []
    return sorted(
        (path, path.name[len(prefix) :])
        for path in parent.iterdir()
        if path.is_dir() and path.name.startswith(prefix)
    )


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

    Every directory is reconciled first (``_reconcile_staging``), because a
    run killed inside the two-rename swap leaves the previous attempt's
    leftovers here and this one has to read them correctly rather than pile
    another rename on top.
    """
    storage = Path(context.settings.storage_dir)
    copied = 0
    for notebook_id in context.manifest.notebooks:
        for root in _FILE_ROOTS:
            origin = context.package_dir / FILES_DIR / root / notebook_id
            destination = storage / root / notebook_id
            # Belt and suspenders: _verify_package_paths already refused an
            # unsafe notebook id, and a symlink escape under either root,
            # before any of this ran -- re-asserted here so staging,
            # reconciliation, rename, and delete never run against a path
            # this function alone computed and trusted.
            _assert_within(
                context.package_dir / FILES_DIR / root, origin, what="package file path"
            )
            _assert_within(storage / root, destination, what="storage destination path")
            inherited = _reconcile_staging(context, destination)
            if not origin.is_dir():
                continue
            staged = destination.with_name(destination.name + _STAGED_SUFFIX)
            retired = _retired_path(destination, context.manifest.package_id)
            # staged/retired are ``destination`` with only its final name
            # segment changed (``Path.with_name``), so they share its parent
            # and this reasserts nothing new about traversal -- it does
            # confirm with_name/with_package_id did not somehow produce a
            # path separator that pathlib would have rejected already.
            _assert_within(storage / root, staged, what="staged file path")
            _assert_within(storage / root, retired, what="retired file path")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(origin, staged)
            installed = [path for path in sorted(staged.rglob("*")) if path.is_file()]
            if verify:
                _verify_installed(context, staged, root, notebook_id, installed)
            copied += len(installed)
            if inherited:
                # The retired copy is ALREADY the pre-import original, kept by
                # a previous attempt. Renaming the current destination onto it
                # would both fail (POSIX rename refuses a non-empty target
                # directory) and destroy the only copy rollback can restore,
                # so the destination is simply replaced in place.
                shutil.rmtree(destination, ignore_errors=True)
            elif destination.exists():
                destination.rename(retired)
            staged.rename(destination)
            if not inherited:
                # An inherited pair is already registered by
                # _reconcile_staging; registering it twice would make rollback
                # restore it and then delete what it just restored.
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


def _reconcile_staging(context: _Context, destination: Path) -> bool:
    """Resolve what a run killed inside the swap left behind, and report
    whether a usable retired copy of THIS package was inherited.

    The swap is: copy into ``.sync-tmp``, rename the destination to
    ``.sync-old-<package_id>``, rename ``.sync-tmp`` onto the destination,
    and (only after phase 5) delete the retired copy. A process killed at any
    point leaves one of:

    - ``.sync-tmp`` present: killed during or right after the copy, before the
      destination was touched. Discard it; nothing was displaced.
    - this package's retired copy present AND the destination present: killed
      after both renames, before the cleanup. The destination already holds
      new content and the retired copy is still the pre-import original -- the
      one thing a rollback needs. It is adopted, not overwritten, and this
      run's commit or rollback disposes of it.
    - this package's retired copy present and the destination MISSING: killed
      between the two renames. Put it back; the destination is the original
      again.

    A retired copy belonging to a DIFFERENT package is never adopted -- this
    run knows nothing about what it contains or what rolling it back would
    mean. If that package finished (``sync_imports.status='done'``) its
    retired copy is dead weight and is removed; otherwise it is left exactly
    where it is, for the run that owns it or for an operator. Either way it is
    reported.
    """
    staged = destination.with_name(destination.name + _STAGED_SUFFIX)
    if staged.exists():
        shutil.rmtree(staged, ignore_errors=True)
    mine: Path | None = None
    for path, owner in _retired_siblings(destination):
        if owner == context.manifest.package_id:
            mine = path
            continue
        if owner in context.finished_packages:
            shutil.rmtree(path, ignore_errors=True)
            context.ledger.warn(
                f"removed {path.name}: package {owner} finished without "
                "dropping the directory it replaced"
            )
            continue
        context.ledger.warn(
            f"left {path.name} alone: package {owner} has not finished, so "
            "this run does not know whether that copy is still needed"
        )
    if mine is None:
        return False
    if not destination.exists():
        mine.rename(destination)
        context.ledger.warn(
            f"restored {destination.name} from a previous run of this package "
            "that was interrupted mid-swap"
        )
        return False
    context.installed.append((destination, mine))
    context.ledger.warn(
        f"adopted the retired copy of {destination.name} left by a previous "
        "run of this package that was interrupted before it could clean up"
    )
    return True


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
            context,
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
    take_over: bool = False,
    verify_files: bool = False,
) -> ImportReport:
    """Apply one full export package to the backend ``settings`` names.

    ``dry_run`` stops after identity mapping: it reads the target, reports what
    the run would do, and writes nothing -- not to the database, and not into
    the package directory either (a package may be shared or read-only).

    A row left 'failed' (the ordinary outcome of a crash this process caught)
    resumes from ``sync_import_progress`` on its own, while it is still the
    newest package declared for its source environment; ``resume`` exists for
    the CLI to say so explicitly.

    ``take_over`` is the operator asserting that a run whose ``sync_imports``
    row is still marked 'running' is gone. Nothing infers that: a running row
    refuses this run and the refusal quotes that run's last heartbeat -- the
    timestamp of the last step it actually committed -- so the person deciding
    has the fact that bears on it. There is no timeout after which this module
    declares another import dead, because a long import and an abandoned one
    look identical from the outside and guessing wrong means two importers
    interleaving on one target.

    **``sync_imports.status`` takes four values.** ``running`` is a claim in
    flight; ``done`` is applied in full; ``failed`` is a run that broke and
    left resumable progress; ``superseded`` is a failed package that a NEWER
    package of the same source environment has since replaced -- its progress
    rows were dropped when that newer one was declared, and it can never be
    resumed again (export a current package instead).

    **Packages of one source environment are ordered by their own
    ``manifest.created_at``**, not by when this side imported them. Three
    rules follow, and all three exist because phase 3a DELETES what a snapshot
    does not carry, so applying snapshots out of order rolls the mirror
    backwards rather than merely adding stale rows:

    - a package older than one this environment has already applied ``done``
      is refused by preflight;
    - declaring a package while an OLDER failed one still holds progress
      retires that one as ``superseded``;
    - declaring one while a NEWER failed one holds progress is refused.

    ``verify_files`` re-hashes the installed files against ``checksums.json``.
    Preflight already hashed the same bytes in this same run, so it is off by
    default and exists for an operator who wants the copy itself checked.

    ``create_missing_users`` mints a local account for every package user this
    environment does not have, so their rows keep an author. **Those accounts
    cannot be signed in to until an administrator issues a 'recover' grant for
    each** (``POST /admin/auth/grants``, ``purpose='recover'``, completed by
    the person through the external provider). No code here binds an external
    identity by matching usernames -- see ``_CREATED_USERS_NEED_A_GRANT`` for
    why that is deliberate. The report says so too, in ``warnings``, and the
    ids are in ``user_mapping.created``.

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
            take_over=take_over,
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
    take_over: bool,
    verify_files: bool,
) -> ImportReport:
    manifest = _read_manifest(package_dir)
    # Phase 1a: the package on its own, with no database handle open.
    checksums = _verify_checksums(package_dir, manifest)
    _reject_incremental_payload(package_dir)
    _verify_package_paths(package_dir, manifest, checksums, settings)

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
        package_users = _package_users(package_dir)
        try:
            mapping = build_user_mapping(
                package_users, _target_users(backend, conn)
            )
        except ValueError as exc:
            raise SyncImportError(f"identity mapping is ambiguous: {exc}") from None
        groups, groups_to_create = _build_group_mapping(backend, conn, context)
        _assert_required_identities_resolve(
            context,
            mapping,
            package_users,
            create_missing_users=create_missing_users,
        )

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

    def outcome() -> UserMappingResult:
        """The mapping as it stands right now. Built on demand rather than
        once up front because creating the missing users happens INSIDE the
        protected phase below, and the failure path has to be able to report
        whatever had been created when it broke."""
        return UserMappingResult(
            matched=MappingProxyType(dict(mapping.matched)),
            created=MappingProxyType(dict(created)),
            unmatched=tuple(
                user.username for user in mapping.unmatched if user.id not in created
            ),
        )

    if _claim_import(backend, context, resume=resume, take_over=take_over):
        return _report(manifest, ledger, outcome(), already_applied=True)

    try:
        # Creating users is the first step of the PROTECTED phase, not a step
        # before it. It writes to the target and it can fail -- a package user
        # with no username is refused right here -- and anything that fails
        # after the sync_imports row is claimed must leave that row 'failed'
        # with a report, or the next run of this source environment is refused
        # as a concurrent import until somebody passes --resume.
        if create_missing_users:
            created.update(_create_missing_users(backend, mapping.unmatched))
            context.users.update(created)
            if created:
                ledger.warn(_CREATED_USERS_NEED_A_GRANT.format(count=len(created)))
        elif mapping.unmatched:
            ledger.warn(
                f"{len(mapping.unmatched)} package user(s) have no target "
                "counterpart; references to them follow their table's rule "
                "(docs/incremental-sync-design.md §3.2)"
            )
        with backend.read() as conn:
            done = _completed_steps(backend, conn, manifest.package_id)
            context.finished_packages = _finished_packages(backend, conn)
        _prune_snapshot(backend, context, done)      # phase 3a
        _apply_rows(backend, context, done)          # phase 3b
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
        # Bound to a local, not read out of the lambda: Python unbinds an
        # ``except ... as exc`` name at the end of its block, and the builder
        # must stay callable for as long as _settle needs it.
        message = str(exc) or repr(exc)
        _settle(
            backend,
            context,
            _STATUS_FAILED,
            lambda: _report(manifest, ledger, outcome(), error=message),
        )
        if isinstance(exc, SyncImportError) or not isinstance(exc, Exception):
            raise
        raise SyncImportError(f"import failed: {exc}") from exc

    # Dropping the replaced directories comes BEFORE the run is recorded done.
    # The other order leaks: a crash in between would leave a package marked
    # done -- which the next run short-circuits on as already_applied -- with
    # its retired directories still on disk and nothing left that will ever
    # come back for them. This way the crash leaves the run un-finished, the
    # rerun resumes, and reconciliation cleans up.
    try:
        _commit_files(backend, context)
    except OSError as exc:  # pragma: no cover - filesystem failure path
        ledger.warn(f"could not drop the replaced storage directories: {exc}")
    return _settle(
        backend,
        context,
        _STATUS_DONE,
        lambda: _report(manifest, ledger, outcome()),
    )


def _finished_packages(backend: _Backend, conn: Any) -> frozenset[str]:
    """Package ids this environment has finished applying. Used only to decide
    whether another package's retired storage directory is still wanted."""
    return frozenset(
        str(row["package_id"])
        for row in backend.stream(
            conn, "SELECT package_id FROM sync_imports WHERE status = 'done'"
        )
    )


def _settle(
    backend: _Backend,
    context: _Context,
    status: str,
    build: "Callable[[], ImportReport]",
) -> ImportReport:
    """Record the run's outcome and return the report that describes it.

    ``status`` is ``done`` or ``failed`` here; those are the only two outcomes
    a RUN produces. The other two values in the column are written elsewhere:
    ``running`` by ``_claim_import`` when the package is declared, and
    ``superseded`` by ``_supersede_outstanding`` when a newer package retires
    a failed one. The report goes into ``report_json`` either way, and carries
    the package's own ``created_at`` -- which is what every later import of
    this source environment orders itself against.

    **A ``done`` that cannot be recorded is a failure of the run**, not a
    footnote to it. The rows and files are applied, but ``sync_imports`` still
    says 'running' for this package -- which blocks the next import of this
    source environment until somebody passes ``--take-over``, and leaves no
    trace of why. Swallowing that would hand the caller a success report for a
    run whose outcome nobody can see, so it is raised and the CLI exits
    non-zero.

    The ``failed`` path still swallows it: the caller is already re-raising
    the exception that got us here, and replacing that with "and also the
    bookkeeping failed" loses the only diagnosis anyone wanted.

    ``build`` is a callable rather than a finished report because ``_report``
    freezes ``ledger.warnings`` into a tuple: a report built before this
    function's own warnings would not carry them, which is exactly how a
    warning about an unrecorded outcome would end up invisible.
    """
    recording: Exception | None = None
    try:
        _close_import_row(backend, context.manifest.package_id, status, build())
    except Exception as exc:  # noqa: BLE001 - reported, and re-raised below for 'done'
        recording = exc
        context.ledger.warn(f"could not record sync_imports.status={status}: {exc}")
    # Rebuilt here so it carries the warning above; _write_report_file may add
    # one of its own, which the returned report then carries in turn.
    _write_report_file(context, build())
    if recording is not None and status == _STATUS_DONE:
        raise SyncImportError(
            "the import applied cleanly but its outcome could not be recorded "
            f"({recording}). sync_imports still says "
            f"{_STATUS_RUNNING!r} for package {context.manifest.package_id}, so "
            "the next import of this source environment will refuse until this "
            "row is closed out; re-run with take_over=True (--take-over) once "
            "the database is reachable."
        ) from recording
    return build()


__all__ = [
    "ImportReport",
    "SkippedRow",
    "SyncImportError",
    "TableOutcome",
    "UnmappedPolicy",
    "UserMappingResult",
    "import_package",
]

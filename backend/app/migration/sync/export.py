"""Export packages: a FULL table scan of the selected notebooks, or the
INCREMENTAL window ``incremental.py`` reads out of the source change log
(docs/incremental-sync-design.md §7 "模式判定" and §8).

This module owns the decision between those two, the package's file layout and
the watermark; it does not own the SQL. The source database handle, the
scope/keyset SQL and the portable row encoding live in
``app.migration.sync.database``, which both this module and ``incremental.py``
build on -- see that module's docstring for why the split exists.

Layering: this module is part of ``app.migration.sync``, whose import
whitelist (guarded by ``tests/test_sync_manifest.py``) deliberately keeps the
package free of services and repository facades, so an export can run as an
offline tool against a quiesced database without composing the application.
"""

from __future__ import annotations

import hashlib
import shutil
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from app.migration.sync import incremental
from app.migration.sync.database import (
    _GLOBAL_KEY_QUERIES,
    _GROUP_PRINCIPAL_TYPES,
    _ID_BATCH,
    _Source,
    SyncExportError,
    _batched,
    _exported_columns,
    _global_scope,
    _parent_join_clause,
    _quoted,
    _row_encoder,
    _scan,
    _scope_of,
    _table_query,
)
from app.migration.sync.manifest import MappingKind, ScopeKind, spec_for, synced_tables
from app.migration.sync.package import (
    ASSET_FILES_DIR,
    CHECKSUMS_NAME,
    DELETES_NAME,
    KG_EPOCHS_NAME,
    MANIFEST_NAME,
    MODE_FULL,
    MODE_INCREMENTAL,
    NOTEBOOK_FILES_DIR,
    PACKAGE_FORMAT_VERSION,
    USERS_NAME,
    json_document,
    json_line,
    notebook_assets_dir,
    notebook_files_dir,
    package_dir_name,
    rows_path,
)
from app.repositories.postgres.schema_manifest import POSTGRES_SCHEMA_MANIFEST

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.config import Settings

# Re-exported for the callers that reach for the backend facade and the SQL
# helpers by their historical home (``import_``, ``cli``, and the export
# tests). Kept as names rather than a shim layer so there is still exactly one
# definition of each.
__all__ = [
    "ExportReport",
    "SyncExportError",
    "_Source",
    "_batched",
    "_parent_join_clause",
    "_quoted",
    "export_notebooks",
]


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


@dataclass(frozen=True)
class ExportReport:
    package_dir: Path
    package_id: str
    # Notebook ids actually written into the package, sorted.
    notebooks: tuple[str, ...]
    # notebook id -> why it was left out. Two reasons only: it is a mirror of
    # another environment, or the caller named an id this database does not
    # have. A notebook's lifecycle ``status`` is NOT one of them -- see
    # _select_notebooks.
    skipped: Mapping[str, str]
    # table name -> rows written, for every synced table (0 included, so the
    # report and the package's file set describe the same thing).
    table_counts: Mapping[str, int]
    file_count: int
    bytes_written: int
    # package.MODE_FULL or package.MODE_INCREMENTAL. Decided by _assemble from
    # the capture gate, this target's watermark row and the caller's
    # full/notebook_ids arguments -- never by the caller directly.
    mode: str
    # The change log's high-water seq visible inside THIS export's read
    # snapshot, as observed -- 0 when the capture gate was off at that moment.
    # An OBSERVATION, not the watermark: ``sync prune-log`` may legitimately
    # empty the log at or below an existing watermark, and then this reads
    # LOWER than ``to_seq``, which is clamped so a watermark never moves
    # backwards. Kept under this name because the CLI has printed it since
    # PR-3a.
    captured_through_seq: int = 0
    # The window this package covers. A full baseline: 0 .. this export's
    # captured_through_seq (the rows come from a table scan, so "from" is the
    # beginning of time). Incremental: previous watermark + 1 .. the same.
    # A SCOPED package (see ``scoped``) claims 0 .. 0: it is not a link in
    # any chain, so it must not look like one.
    from_seq: int = 0
    to_seq: int = 0
    # True when the caller named the notebooks (``--notebook``). Such a
    # package covers a chosen subset, never "everything that changed", so it
    # is always full and never touches this target's watermark -- advancing
    # it would push every OTHER notebook's changes below the next window's
    # floor and lose them permanently.
    scoped: bool = False
    # Whether this run moved sync_export_state. False for a scoped export,
    # true otherwise (a full baseline advances it too -- that is what makes
    # the NEXT export incremental).
    watermark_advanced: bool = True
    # This run's ``sync_export_runs`` lease id (the package id). Empty for a
    # scoped export, which takes no lease.
    run_id: str = ""
    # What this run actually wrote to sync_export_state.captured -- i.e.
    # whether the NEXT export may resume from the watermark this one left.
    # Normally "the gate was open", but _advance_watermark downgrades it to
    # False when the gate moved mid-export (see its docstring), so this is
    # the recorded truth rather than the gate reading. Always False for a
    # scoped export, which writes no watermark at all.
    captured: bool = False
    # Incremental only: the package_id the previous watermark named, so a
    # target can chain packages without guessing. Empty for a full package.
    base_package_id: str = ""
    # Rows written to deletes.jsonl.
    deletes: int = 0
    # Notebooks whose own ``notebooks`` row was deleted inside the window.
    # They are NOT in ``notebooks`` (there is nothing left to export), but a
    # target still has to hear about them.
    deleted_notebooks: tuple[str, ...] = ()
    # Change-log rows dropped because they belong to a mirrored notebook.
    skipped_mirror_changes: int = 0
    # An incremental package whose window held no change at all. Still a
    # complete, importable package with a package_id and an advanced
    # watermark: the export chain stays continuous, so a target never has to
    # tell "nothing changed" from "a package went missing".
    empty: bool = False
    # Non-fatal conditions an operator should see, e.g. a stale staging
    # directory left by an earlier crashed run and removed by this one.
    warnings: tuple[str, ...] = ()
    # Package-relative paths of files that vanished between listing and
    # copying. Reported rather than raised: a source file deleted by a
    # concurrent notebook edit must not throw away a whole export.
    missing_files: tuple[str, ...] = ()


# ----------------------------------------------------------- package writer


class _PackageWriter:
    """Writes into ``<final>.tmp`` and records each file's sha256 and size as
    it goes, so ``checksums.json`` never requires a second pass over the rows.
    ``write_order`` is the package's file-creation order, which is what makes
    "manifest.json is written last" checkable rather than a claim about
    timestamps."""

    def __init__(self, root: Path, on_beat: Any = None) -> None:
        self.root = root
        self.checksums: dict[str, str] = {}
        self.bytes_written = 0
        self.write_order: list[str] = []
        self._heartbeat = root / _HEARTBEAT_NAME
        # Refreshes the DATABASE lease alongside the on-disk marker, so the
        # two liveness signals a crashed export leaves behind (a staging
        # directory and a sync_export_runs row) go stale together instead of
        # one of them outliving the other. None for a scoped export, which
        # takes no lease.
        self._on_beat = on_beat
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
        if self._on_beat is not None:
            self._on_beat()

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
    make the two sides each other's upstream. That is the ONLY reason a
    notebook that exists is left behind.

    ``status`` is deliberately not consulted. It is a TARGET-owned column
    (``TableSyncSpec.target_owned_columns``): import never overwrites it and
    the publish step flips a mirror to ``draft``, so a source-side
    ``copying``/``deleting``/``importing`` says nothing about what the target
    should hold -- it is a moment in a local process, and the export's read
    snapshot is a coherent view of the rows either way. Skipping on it would
    be actively harmful now that the watermark advances: a baseline taken
    while a notebook was mid-copy would drop the whole notebook, and an
    incremental window would drop exactly the changes made during that
    window, with the watermark moving past them regardless
    (docs/incremental-sync-design.md §7 "笔记本范围与镜像").
    """
    statement = "SELECT id, sync_origin FROM notebooks"
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


def _write_changed_files(
    writer: _PackageWriter, entries: Sequence[tuple[str, Path]]
) -> tuple[int, list[str]]:
    """The incremental counterpart of ``_write_files``: copy only the files
    the package's own upserted rows point at, already resolved to
    ``(package-relative path, source path)`` by
    ``incremental.resolve_file_requests``.

    Same placement as the full path -- after the read snapshot closes -- and
    the same treatment of a file that vanished in between: reported through
    ``missing_files``, never fatal. What a package does NOT carry here is the
    bytes of a DELETED row; those are removed at the target from the target's
    own row paths when PR-3c replays ``deletes.jsonl``, because the source no
    longer has the row that would say which file to remove."""
    copied = 0
    missing: list[str] = []
    for relative, origin in entries:
        if writer.copy_file(relative, origin):
            copied += 1
        else:
            missing.append(relative)
    return copied, missing


# ----------------------------------------------------------------- watermark


def _capture_gate(source: _Source, conn: Any, *, lock: bool = False) -> tuple[bool, Any]:
    """``(is the gate open, when it was last opened)`` -- the capture gate's
    GENERATION, read once per export and passed around from there.

    The flag alone is not enough to promise a window is covered. ``sync
    capture disable`` clears the change log, and a ``disable`` + ``enable``
    pair during an export leaves the flag exactly as this read found it while
    having thrown away everything the window was going to describe.
    ``enabled_at`` moves on every ``enable``, so the pair is what distinguishes
    "still the gate I read" from "a gate that has been round the loop since".
    ``_advance_watermark`` re-reads it in its own write transaction and
    refuses to claim ``captured`` when it has moved.

    A missing control row (capture never turned on) answers ``(False, None)``,
    which compares equal to itself across the export the same way.

    ``lock=True`` is for the re-read inside the watermark's write transaction:
    on PostgreSQL it takes a ROW lock on the control row, so a concurrent
    ``sync capture enable``/``disable`` (both write that row) waits until the
    watermark is committed instead of slipping between the re-read and the
    INSERT. Not needed for the snapshot-side read, which is allowed to be a
    moment in time -- it is the value the write transaction compares against.
    A row that does not exist cannot be locked and does not need to be: no
    row means the gate is closed, and an ``enable`` that inserts one
    afterwards is simply later than this export.
    """
    statement = (
        "SELECT enabled, enabled_at FROM sync_capture_control WHERE singleton = 1"
    )
    if lock and source.is_postgres:
        statement += " FOR UPDATE"
    rows = source.fetch(conn, statement)
    if not rows:
        return False, None
    return bool(rows[0]["enabled"]), rows[0]["enabled_at"]


def _captured_through_seq(source: _Source, conn: Any, gate_open: bool) -> int:
    """The change log's high-water ``seq`` visible in THIS read snapshot, or
    0 when the capture gate is off.

    Read inside the SAME ``source.read()`` snapshot the row scan itself
    uses, via the ``conn`` that snapshot handed out -- never a later
    connection -- so this number describes exactly the data that ended up in
    the package, not anything captured after the export's own consistent
    view was taken. A closed gate means there is nothing safe to call
    "captured through": rows may have changed since the last time it was
    open with no log entry to prove it, so 0 (never a stale high-water mark)
    is the only honest answer (docs/incremental-sync-design.md §7).

    THIS NUMBER IS THE WATERMARK, AND IT IS A RESUME POINT ONLY TOGETHER WITH
    THE SNAPSHOT STORED BESIDE IT. On PostgreSQL ``seq`` is handed out by a
    sequence at INSERT time while a row becomes VISIBLE at COMMIT time, and
    those two orders are not the same: writer A can hold ``seq = 100``
    uncommitted while writer B takes ``seq = 101`` and commits first, so this
    snapshot sees 101 and not 100 and ``MAX(seq)`` is 101. Resuming from
    ``seq > 101`` alone would skip A's change forever -- silently, because
    nothing is missing from the log itself, only from the window.

    That is why ``_advance_watermark`` stores ``exported_snapshot``
    (``pg_current_snapshot()``) next to this number, and why
    ``incremental.compensation_rows`` re-reads, on the next run, exactly the
    log rows whose ``txid`` was still in flight at this snapshot -- whatever
    their ``seq``. The pair ``(exported_through_seq, exported_snapshot)`` is
    the resume point; neither half is one on its own. SQLite needs only the
    first half (one writer at a time means ``seq`` order is commit order),
    and stores NULL for the second.
    """
    if not gate_open:
        return 0
    row = source.fetch(
        conn, "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM sync_change_log"
    )[0]
    return int(row["max_seq"])


def _require_lease(source: _Source, conn: Any, target_env: str, run_id: str) -> None:
    """Refuse to publish unless this run STILL holds the target's lease.

    The hole this closes (codex #784 r5): an export that stalls past
    ``_STALE_RUN_SECONDS`` has its lease taken over, the successor finishes
    and publishes, and then the original wakes up. Its package describes an
    older snapshot, and on the full branch its sequence can be EQUAL to the
    successor's -- so the ``exported_through_seq <= excluded`` guard lets it
    through and it overwrites a newer watermark with an older
    ``exported_snapshot``. On PostgreSQL that silently drops the next
    export's compensation window for any transaction the successor had
    already accounted for: those log rows may since have been pruned, and
    nothing will look for them again.

    Read ``FOR UPDATE`` on PostgreSQL, inside the publish transaction and
    after it has already taken the gate's row lock, so the lease cannot be
    taken over between this check and the commit. A MISSING row is the same
    verdict as a different ``run_id``: the successor deletes the lease in the
    very transaction that publishes its watermark, so "no lease" means
    "somebody else already finished".

    This check is only ever "the row exists and the id matches". Losing a
    lease is made IRREVERSIBLE elsewhere: ``sync prune-log`` removes dead
    lease rows under a lock before it uses the surviving floors, and
    ``_refresh_export_lease`` is an UPDATE that can never recreate one. So a
    stalled export cannot heartbeat its way back into a lease whose
    protection has already been spent.

    This is also what keeps the full branch's ``<=`` safe rather than merely
    permissive. Equality can now only come from THIS run (nobody else holds
    the lease), which is the re-publish case a re-run of one export is
    allowed to perform; a different run reaching the same sequence is
    rejected here, before the comparison is ever reached.
    """
    statement = "SELECT run_id FROM sync_export_runs WHERE target_env = ?"
    if source.is_postgres:
        statement += " FOR UPDATE"
    rows = source.fetch(conn, statement, (target_env,))
    held = str(rows[0]["run_id"]) if rows else ""
    if held == run_id:
        return
    raise SyncExportError(
        f"export lease for target {target_env!r} was taken over by run "
        f"{held or '(none -- already published and released)'!r}; this "
        "package is not published"
    )


def _advance_watermark(
    source: _Source,
    target_env: str,
    package_id: str,
    exported_at: datetime,
    exported_through_seq: int,
    *,
    gate: tuple[bool, Any],
    snapshot: str | None,
    base_package_id: str,
    run_id: str,
) -> tuple[bool, str]:
    """Upsert this target's watermark, only once the package is complete on
    disk (§7): a failed export must leave the watermark where it was so a
    re-run is always safe.

    ``exported_through_seq`` is the caller's ``to_seq`` -- the package's own
    claim, which is this snapshot's ``MAX(seq)`` clamped so it can never fall
    below the watermark already stored (see ``_assemble``). The package and
    the watermark must name the same point, or a reader of the chain and a
    reader of this table would disagree about where the source got to.

    Three columns together are what the NEXT export resumes from:
    ``exported_through_seq``,
    ``exported_snapshot`` (this transaction's ``pg_current_snapshot()``, NULL
    on SQLite) and ``captured`` (whether the gate was open, i.e. whether
    there is a log behind that seq at all). ``captured = 0`` is what makes a
    watermark unusable as a resume point without making it a lie: it still
    records what this export saw, it just refuses to promise the log covers
    the gap. Written for a FULL export too -- that is what lets the export
    after it be incremental.

    ``captured`` is decided HERE, not by the caller, because the gate can move
    while the export runs. The control row is re-read inside this very write
    transaction and compared against the generation the read snapshot saw
    (``_capture_gate``): a ``disable`` + ``enable`` pair in between leaves the
    flag looking unchanged while having emptied the change log, so a
    watermark claiming ``captured`` would send the NEXT export resuming into
    a log that no longer holds the window. On any disagreement the watermark
    is still written -- the seq and snapshot are true facts about this run --
    but ``captured`` goes to 0, which costs one full export and loses nothing.
    Returns ``(captured, reason)``; ``reason`` is empty when the gate held.

    Before either, ``_require_lease`` confirms this run still owns the
    target's export lease -- see it for the stalled-then-superseded export
    that guard exists for.

    That comparison is only worth anything if the gate cannot move between
    the re-read and the commit, so this block TAKES THE LOCK FIRST:
    ``begin_immediate`` on SQLite (one writer at a time, across processes --
    the export and the ``sync capture`` CLI are two of them), and a ``FOR
    UPDATE`` row lock on the control row on PostgreSQL. ``capture
    enable``/``disable`` both write that row, so either they land before this
    read or they wait for this commit; there is no third case.

    The write itself is CONDITIONAL, because two exports of one target can
    overlap and the watermark is a chain, not a latest-writer-wins cell:

    - Incremental: the row must still be the package this one declares as its
      ``base_package_id``. If another export replaced it in the meantime,
      publishing over it would orphan that export's package -- the chain
      would skip it -- so this run fails instead and its package is removed.
    - Full: the stored seq may not move backwards, so the upsert carries
      ``WHERE sync_export_state.exported_through_seq <= excluded....``. A
      baseline that lost a race against a newer one is refused the same way.
      The ``<=`` (rather than ``<``) is safe because ``_require_lease`` has
      already established that nobody else holds this target's lease: an
      EQUAL sequence can only be this same run re-publishing.

    Raises ``SyncExportError`` in both cases; the caller deletes the package
    it just published, because a package no watermark points at is worse than
    no package at all.

    The in-flight lease (``sync_export_runs``) is released in THIS
    transaction, so "the watermark is published" and "this target is free
    again" become true at the same instant -- a crash between the two could
    otherwise leave a lease with no run behind it, blocking the target for an
    hour.

    See ``_captured_through_seq`` for why the seq alone is not a resume
    point on PostgreSQL, and ``incremental.compensation_rows`` for what the
    snapshot is used for."""
    moment: Any = exported_at if source.is_postgres else exported_at.isoformat()
    reason = ""
    with source.write() as conn:
        source.begin_immediate(conn)
        now = _capture_gate(source, conn, lock=True)
        _require_lease(source, conn, target_env, run_id)
        captured = bool(gate[0]) and now == gate
        if gate[0] and not captured:
            reason = (
                "the capture gate changed while this export ran (read "
                f"{gate!r}, now {now!r}); the watermark is recorded but not "
                "marked captured, so the next export will be a full one"
            )
        flag: Any = captured if source.is_postgres else int(captured)
        values = (exported_through_seq, moment, package_id, flag, snapshot)
        if base_package_id:
            cursor = conn.execute(
                source.sql(
                    "UPDATE sync_export_state SET "
                    "exported_through_seq = ?, exported_at = ?, package_id = ?, "
                    "captured = ?, exported_snapshot = ? "
                    "WHERE target_env = ? AND package_id = ?"
                ),
                (*values, target_env, base_package_id),
            )
            if int(cursor.rowcount or 0) == 0:
                raise SyncExportError(
                    f"watermark moved during export: base {base_package_id!r} "
                    f"is no longer the watermark of target {target_env!r}; "
                    "another export published over it, so this package would "
                    "leave a gap in the chain"
                )
        else:
            cursor = conn.execute(
                source.sql(
                    "INSERT INTO sync_export_state "
                    "(target_env, exported_through_seq, exported_at, package_id, "
                    "captured, exported_snapshot) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (target_env) DO UPDATE SET "
                    "exported_through_seq = excluded.exported_through_seq, "
                    "exported_at = excluded.exported_at, "
                    "package_id = excluded.package_id, "
                    "captured = excluded.captured, "
                    "exported_snapshot = excluded.exported_snapshot "
                    "WHERE sync_export_state.exported_through_seq "
                    "<= excluded.exported_through_seq"
                ),
                (target_env, *values),
            )
            if int(cursor.rowcount or 0) == 0:
                raise SyncExportError(
                    f"watermark moved during export: target {target_env!r} is "
                    f"already at a sequence above {exported_through_seq}; "
                    "another export published over it and this package would "
                    "drag the watermark backwards"
                )
        conn.execute(
            source.sql(
                "DELETE FROM sync_export_runs "
                "WHERE target_env = ? AND run_id = ?"
            ),
            (target_env, run_id),
        )
    return captured, reason


# ------------------------------------------------------------------ lease


# How long a lease row may go un-refreshed before a new export treats it as a
# DEAD run to replace rather than a live one to refuse. Deliberately the SAME
# constant as the staging directory's: one beat refreshes both the ``.tmp``
# marker and this row, so they are two views of one liveness signal and must
# not be able to disagree about which runs are alive.
_STALE_RUN_SECONDS = _STALE_STAGING_SECONDS


def _moment_for(source: _Source, value: datetime) -> Any:
    """One instant in whichever shape this backend's timestamp columns take:
    a ``datetime`` for PostgreSQL ``timestamptz``, ISO text for SQLite."""
    return value if source.is_postgres else value.isoformat()


def _age_seconds(stored: Any, now: datetime) -> float | None:
    """Seconds since a stored timestamp, or None when it cannot be read.

    Unreadable is NOT "old": a lease row whose timestamp this build cannot
    parse is treated as live, so a corrupted row makes an export refuse
    loudly rather than silently steal a lease from a run that is still
    going."""
    if isinstance(stored, datetime):
        moment = stored
    elif isinstance(stored, str) and stored:
        try:
            moment = datetime.fromisoformat(stored)
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (now - moment).total_seconds()


def _claim_export_lease(
    source: _Source, target_env: str, run_id: str, started_at: datetime
) -> list[str]:
    """Take this target's in-flight export lease, BEFORE the read snapshot.

    Two things go wrong without it, and neither is visible to the run that
    causes them (docs/incremental-sync-design.md §7):

    - ``sync prune-log`` deletes log rows at or below the smallest captured
      watermark. A running export's watermark does not exist yet, so the rows
      it is about to read still look prunable. The lease carries two floors,
      both read in THIS transaction, and prune-log takes its bounds as the
      minimum over the captured watermarks AND every live lease:

      * ``floor_seq`` -- the log's ``MAX(seq)`` at claim time, a lower bound
        on the ``exported_through_seq`` this run will publish.
      * ``floor_xmin`` -- this transaction's ``pg_snapshot_xmin``, NULL on
        SQLite (no txid there, and no in-flight window to compensate for).
        This is what protects the NEXT export's COMPENSATION window, which
        ``floor_seq`` alone cannot: that window re-reads log rows whose
        ``seq`` is BELOW the watermark, so a seq floor says nothing about
        them. The ordering is the whole argument -- this transaction runs
        strictly BEFORE the export opens its read snapshot, so
        ``floor_xmin <= xmin(export snapshot)``, and any transaction still in
        flight at that snapshot has ``txid >= xmin(export snapshot) >=
        floor_xmin``. Holding prune-log's txid bound at or below
        ``floor_xmin`` therefore keeps every row compensation will want.
    - Two unscoped exports of one target race to publish, and the loser's
      package describes a window the winner's watermark already claims.

    The claim is ONE STATEMENT, and ``rowcount == 1`` is the only thing that
    means "mine" (codex #784 r2). An earlier shape read the row ``FOR UPDATE``
    and then upserted, which is wrong on PostgreSQL in exactly the case the
    lease exists for: two exports starting when NO row exists both read
    nothing -- ``FOR UPDATE`` cannot lock a row that is not there -- and the
    second's unconditional upsert then overwrote the first's live lease, with
    both runs continuing. ``INSERT ... ON CONFLICT (target_env) DO UPDATE ...
    WHERE sync_export_runs.heartbeat_at < <stale cutoff>`` has no such gap:
    the insert wins outright when the table is empty, and a second
    transaction arriving behind it re-evaluates that ``WHERE`` against the
    row the winner just committed, sees a fresh heartbeat, and updates
    nothing. Both backends support the ``WHERE`` on ``DO UPDATE``; SQLite
    runs the same statement under ``BEGIN IMMEDIATE``.

    A row whose ``heartbeat_at`` has gone unrefreshed for
    ``_STALE_RUN_SECONDS`` belongs to a run that died; the ``WHERE`` lets it
    be taken over, with a warning, rather than blocking this target forever.
    A takeover rewrites both floors: they describe the run that now holds the
    lease, and a dead run's promises are nobody's.
    A heartbeat this build cannot even READ is treated as live and refused
    outright, before the claim is attempted: an unreadable row is a reason to
    stop, never a reason to assume the other run is dead. Scoped
    (``--notebook``) exports never call this: they are not on the chain, they
    publish no watermark, and there is nothing for them to race over.
    """
    moment = _moment_for(source, started_at)
    cutoff = _moment_for(source, started_at - timedelta(seconds=_STALE_RUN_SECONDS))
    warnings: list[str] = []
    with source.write() as conn:
        source.begin_immediate(conn)
        held = "SELECT run_id, started_at, heartbeat_at FROM sync_export_runs WHERE target_env = ?"
        # Read only to phrase the messages below; it decides nothing, which
        # is why the claim does not depend on it being atomic with anything.
        previous = source.fetch(conn, held, (target_env,))
        if previous and _age_seconds(previous[0]["heartbeat_at"], started_at) is None:
            raise SyncExportError(
                f"target {target_env!r} holds an export lease whose "
                f"heartbeat_at ({previous[0]['heartbeat_at']!r}) is not a "
                "readable timestamp; refusing to assume that run is dead -- "
                "fix or remove the sync_export_runs row by hand"
            )
        floor_seq = _captured_through_seq(source, conn, _capture_gate(source, conn)[0])
        # Read on THIS connection, in THIS transaction -- the one that runs
        # strictly before the export opens its read snapshot. See the
        # docstring for why that ordering is the whole guarantee.
        snapshot = source.current_snapshot(conn)
        floor_xmin = source.snapshot_xmin(snapshot) if snapshot else None
        cursor = conn.execute(
            source.sql(
                "INSERT INTO sync_export_runs "
                "(target_env, run_id, package_id, started_at, heartbeat_at, "
                "floor_seq, floor_xmin) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (target_env) DO UPDATE SET "
                "run_id = excluded.run_id, package_id = excluded.package_id, "
                "started_at = excluded.started_at, "
                "heartbeat_at = excluded.heartbeat_at, "
                "floor_seq = excluded.floor_seq, "
                "floor_xmin = excluded.floor_xmin "
                "WHERE sync_export_runs.heartbeat_at < ?"
            ),
            (
                target_env, run_id, run_id, moment, moment, floor_seq,
                floor_xmin, cutoff,
            ),
        )
        if int(cursor.rowcount or 0) != 1:
            holder = source.fetch(conn, held, (target_env,)) or previous
            detail = (
                f"run {str(holder[0]['run_id'])!r}, started "
                f"{holder[0]['started_at']}, last heartbeat "
                f"{holder[0]['heartbeat_at']}"
                if holder
                else "holder unknown"
            )
            raise SyncExportError(
                f"an export for target {target_env!r} is already running "
                f"({detail}); wait for it to finish, or remove its "
                "sync_export_runs row if you know it is dead"
            )
        if previous:
            age = _age_seconds(previous[0]["heartbeat_at"], started_at) or 0.0
            warnings.append(
                f"replaced a dead export lease for target {target_env!r} "
                f"(run {str(previous[0]['run_id'])!r}, last heartbeat "
                f"{previous[0]['heartbeat_at']}, {int(age)}s ago)"
            )
    return warnings


def _refresh_export_lease(
    source: _Source, target_env: str, run_id: str, warnings: list[str]
) -> None:
    """Push this run's ``heartbeat_at`` forward.

    UPDATE ONLY, matched on ``run_id``, and deliberately never an upsert.
    ``sync prune-log`` deletes DEAD lease rows under a lock before it uses
    the remaining floors; if a heartbeat could recreate the row, a stalled
    export would resurrect a lease prune-log had already decided to ignore,
    and the log rows that lease was protecting would already be gone. Losing
    a lease has to be irreversible, so a heartbeat that matches nothing
    reports it and changes nothing -- the publish then fails in
    ``_require_lease``, which is the outcome the operator needs to see.

    Best effort otherwise: a failed heartbeat must never take down an export
    that is otherwise fine. The worst a missed beat costs is that a LATER
    export judges this lease dead, and the lease check plus the conditional
    publish still keep two runs from overwriting each other's watermark.
    """
    try:
        with source.write() as conn:
            cursor = conn.execute(
                source.sql(
                    "UPDATE sync_export_runs SET heartbeat_at = ? "
                    "WHERE target_env = ? AND run_id = ?"
                ),
                (
                    _moment_for(source, datetime.now(timezone.utc)),
                    target_env,
                    run_id,
                ),
            )
            if int(cursor.rowcount or 0) == 0 and not any(
                "lease lost" in warning for warning in warnings
            ):
                warnings.append(
                    f"lease lost: target {target_env!r} no longer holds run "
                    f"{run_id!r}; this export will refuse to publish"
                )
    except Exception:  # noqa: BLE001 - see the docstring
        pass


def _release_export_lease(source: _Source, target_env: str, run_id: str) -> None:
    """Drop this run's lease. Best effort, and matched on ``run_id`` so a run
    that was already declared dead and replaced cannot delete its
    successor's row on the way out."""
    try:
        with source.write() as conn:
            conn.execute(
                source.sql(
                    "DELETE FROM sync_export_runs "
                    "WHERE target_env = ? AND run_id = ?"
                ),
                (target_env, run_id),
            )
    except Exception:  # noqa: BLE001 - an abort path must not raise again
        pass


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
    full: bool = False,
) -> ExportReport:
    """Write one export package under ``out_dir`` and return its report.

    ``notebook_ids=None`` means "every non-mirror notebook" -- a notebook's
    lifecycle ``status`` is not a filter here, see ``_select_notebooks``. An
    EMPTY
    sequence is rejected rather than treated as None: an empty package is
    never what a caller meant, and a ``--notebook`` list that silently became
    empty upstream would otherwise produce a package that imports cleanly and
    carries nothing.

    Three shapes, and the caller does not pick between them directly
    (docs/incremental-sync-design.md §7 "模式判定"):

    - ``notebook_ids`` given -> a SCOPED full package. It carries exactly the
      named notebooks and does NOT advance this target's watermark: a
      watermark earned by exporting two notebooks would put every other
      notebook's changes below the next window's floor, where nothing would
      ever pick them up again.
    - ``full=True``, or no usable watermark for this target (capture gate
      closed, no watermark row, or one whose ``captured`` is 0) -> a full
      BASELINE package over every non-mirror notebook. It advances the
      watermark,
      which is what makes the next export incremental.
    - otherwise -> an INCREMENTAL package built from the change log.

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
            "non-mirror notebook"
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
            full=full,
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
    full: bool,
) -> ExportReport:
    package_id = uuid.uuid4().hex
    exported_at = datetime.now(timezone.utc)
    scoped = notebook_ids is not None
    # Claimed BEFORE anything is read or created: a refused export must leave
    # no staging directory and must not have touched the read snapshot. A
    # scoped export takes no lease -- it is not on the chain (see
    # _claim_export_lease).
    warnings = [] if scoped else _claim_export_lease(
        source, target_env, package_id, exported_at
    )
    # The staging name is fixed before the window is known, because the
    # directory has to exist before the first byte is written; the FINAL name
    # carries the real ``from``/``to`` and is computed by _assemble. Keeping
    # the ``0-0`` spelling here also keeps the staging sweep's prefix/suffix
    # rule (``sync-<env>-...tmp``) matching what earlier versions left behind.
    staging_dir = out_dir / (package_dir_name(source_env, 0, 0, package_id) + ".tmp")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        warnings.extend(_sweep_stale_staging(out_dir, source_env))
        staging_dir.mkdir()
    except BaseException:
        if not scoped:
            _release_export_lease(source, target_env, package_id)
        raise
    # The beat appends to the SAME list ``_assemble`` was handed, and every
    # beat happens before that list is folded into the report -- so a lease
    # lost mid-run is reported by the run that lost it.
    on_beat = (
        None if scoped
        else lambda: _refresh_export_lease(
            source, target_env, package_id, warnings
        )
    )
    try:
        return _assemble(
            source,
            settings=settings,
            writer=_PackageWriter(staging_dir, on_beat),
            target_env=target_env,
            source_env=source_env,
            notebook_ids=notebook_ids,
            package_id=package_id,
            exported_at=exported_at,
            out_dir=out_dir,
            warnings=warnings,
            full=full,
        )
    except BaseException:
        # A half-written package must never be left where the next run would
        # sweep it and report it as someone else's crash -- and the lease it
        # was holding must not block the re-run either. The publish path
        # deletes the lease in the SAME transaction as the watermark, so
        # reaching here means it never published.
        shutil.rmtree(staging_dir, ignore_errors=True)
        if not scoped:
            _release_export_lease(source, target_env, package_id)
        raise


# Rows this package carries that REFERENCE a GLOBAL row which must travel
# with them, even when that row itself did not change and no exported
# notebook points at it. Without this an incremental package can carry a
# ``group_members`` row whose ``groups`` parent is nowhere -- both backends
# declare ``group_members.group_id -> groups.id`` -- because the seed set is
# resolved from the touched NOTEBOOKS, and a window that only edited a
# membership touches none.
#
# ``group_members``' parent id is in its own SYNC KEY ``(group_id, user_id)``,
# which is what lets it be collected from the compacted window without
# reading a row -- and therefore before ``groups`` is written, since
# copy_rank puts the parent first.
_GLOBAL_KEY_PARENTS: dict[str, tuple[str, str]] = {
    "group_members": ("group_id", "granted_group_ids"),
}

# The same gap reached from a NOTEBOOK-scoped table: a grant whose principal
# is a group names a ``groups`` row that the notebook-driven seed query would
# only find if that grant were already committed when the seed ran. Collected
# from the rows this package actually writes, which is why _RowSink takes an
# observer.
_PRINCIPAL_SEED_TABLE = "notebook_grants"
_PRINCIPAL_SEED_KEY_SET = "granted_group_ids"


@dataclass
class _Rows:
    """What one mode's row phase produced. The two paths (full table scan,
    change-log window) differ only in how they fill this in; everything after
    it -- users, checksums, manifest, rename, watermark -- is shared."""

    notebooks: tuple[str, ...]
    skipped: dict[str, str]
    table_counts: dict[str, int]
    table_entries: dict[str, dict[str, Any]]
    deletes: int
    deleted_notebooks: tuple[str, ...]
    skipped_mirror_changes: int
    warnings: list[str]
    # Full: the notebook ids whose storage roots are copied wholesale.
    # Incremental: the ``incremental.FileRequest`` list for the changed rows.
    # Resolved in the file phase, after the read snapshot closes.
    file_plan: Any


def _table_entry(writer: _PackageWriter, table: str, columns, written: int) -> dict:
    return {
        "columns": list(columns),
        "rows": written,
        "sha256": writer.checksums[rows_path(table)],
    }


def _full_rows(
    source: _Source,
    conn: Any,
    writer: _PackageWriter,
    users: _UserIds,
    notebook_ids: Sequence[str] | None,
) -> _Rows:
    """The table-scan path: every row of every synced table that belongs to
    the selected notebooks. ``deletes.jsonl`` is empty by construction -- a
    full package states what EXISTS, and the importer reconciles by deleting
    whatever it holds that the package does not carry."""
    selection = _select_notebooks(source, conn, notebook_ids)
    global_keys = _global_keys(source, conn, selection.exported)
    table_counts: dict[str, int] = {}
    table_entries: dict[str, dict[str, Any]] = {}
    for table in synced_tables():
        columns, written = _write_table(
            source, conn, writer, table, selection.exported, global_keys, users
        )
        table_counts[table] = written
        table_entries[table] = _table_entry(writer, table, columns, written)
    with writer.lines(DELETES_NAME):
        pass
    return _Rows(
        notebooks=selection.exported,
        skipped=dict(selection.skipped),
        table_counts=table_counts,
        table_entries=table_entries,
        deletes=0,
        deleted_notebooks=(),
        skipped_mirror_changes=0,
        warnings=[],
        file_plan=list(selection.exported),
    )


class _RowSink:
    """One incremental table's row writer: the SAME encoder a full export
    uses, plus the ``users.jsonl`` closure and an optional record of which
    keys were written.

    Each row is projected down to the package's column set -- the read may
    have carried a key column the package does not export -- and handed to
    the open ``writer.lines`` handle immediately, so no table's rows are ever
    all in memory at once. ``key_columns`` is passed only for the two table
    groups whose extra rows are computed FROM what the window already wrote
    (``notebooks``, and the GLOBAL seed); everywhere else tracking keys would
    be exactly the accumulation this design avoids.
    """

    def __init__(
        self,
        write: Any,
        users: _UserIds,
        table: str,
        columns: Sequence[str],
        key_columns: Sequence[str] | None = None,
        observe: Any = None,
    ) -> None:
        self._write = write
        self._users = users
        self._columns = tuple(columns)
        self._encode = _row_encoder(table, columns)
        self._identity = _identity_columns(table)
        self._key_columns = key_columns
        # Called with each row as it is written, for the one table whose rows
        # name a GLOBAL row that has to travel with them (see
        # _PRINCIPAL_SEED_TABLE). Streaming, like everything else here.
        self._observe = observe
        self.rows = 0
        self.keys: set[str] = set()

    def __call__(self, row: Mapping[str, Any]) -> None:
        projected = {name: row[name] for name in self._columns}
        self._users.observe(projected, *self._identity)
        if self._observe is not None:
            self._observe(projected)
        self._write(json_line(self._encode(projected)))
        self.rows += 1
        if self._key_columns is not None:
            self.keys.add(
                incremental.key_text(incremental.row_key(row, self._key_columns))
            )


def _incremental_rows(
    source: _Source,
    conn: Any,
    writer: _PackageWriter,
    users: _UserIds,
    watermark: Any,
) -> _Rows:
    """The change-log path (docs/incremental-sync-design.md §7).

    Table order is copy_rank, with two deliberate deferrals, because two
    table groups cannot be written until the package's notebook set is known:

    - ``notebooks`` last-but-one, so every notebook carrying ANY row or
      delete in this package gets a ``notebooks`` row even when the notebook
      itself did not change. That keeps ``manifest.notebooks`` equal to
      ``rows/notebooks.jsonl``, the invariant the importer's preflight has
      always checked, instead of making an incremental package the one shape
      where the two legitimately disagree.
    - the GLOBAL tables last, because their seed set (``_global_keys``) is
      resolved FROM the exported notebooks.

    Only the file-creation order inside the package moves; the manifest's
    table map is sorted and the importer walks tables by copy_rank out of the
    manifest, never by directory listing.

    ``deletes.jsonl`` is opened ONCE around the whole loop and appended to as
    each table produces its entries, so the run never holds the window's
    delete list either. Its line order is therefore the loop's: non-GLOBAL
    tables by copy_rank, then ``notebooks``, then the GLOBAL tables. That is
    deterministic but NOT foreign-key-safe ordering; replaying deletes in a
    safe order is PR-3c's problem, which has the target's catalog to do it
    with.
    """
    compensation = incremental.compensation_rows(source, conn, watermark)
    notebook_state = incremental.NotebookState(source, conn)
    table_counts: dict[str, int] = {}
    table_entries: dict[str, dict[str, Any]] = {}
    notebooks: set[str] = set()
    deleted_notebooks: set[str] = set()
    skipped: dict[str, str] = {}
    files: list[Any] = []
    warnings: list[str] = []
    counters = {"deletes": 0, "mirror": 0}
    deferred: list[str] = []
    # GLOBAL key sets this package needs seeded BEYOND what the touched
    # notebooks resolve to -- see _GLOBAL_KEY_PARENTS.
    extra_global: dict[str, set[str]] = {}
    # Compactions taken early (for the pass above) and reused when their
    # table is actually written, so the change log is read once per table.
    cached_changes: dict[str, Any] = {}

    def absorb(table: str, delta: Any, sink: _RowSink, columns) -> None:
        table_counts[table] = sink.rows
        table_entries[table] = _table_entry(writer, table, columns, sink.rows)
        notebooks.update(delta.notebooks)
        deleted_notebooks.update(delta.deleted_notebooks)
        files.extend(delta.files)
        warnings.extend(delta.warnings)
        counters["deletes"] += delta.deletes
        counters["mirror"] += delta.skipped_mirror
        for notebook_id, reason in delta.skipped.items():
            skipped.setdefault(notebook_id, reason)

    with writer.lines(DELETES_NAME) as write_delete:

        def emit_delete(entry: Mapping[str, Any]) -> None:
            write_delete(json_line(entry))

        def process(
            table: str,
            *,
            track_keys: bool = False,
            extra: Any = None,
            observe: Any = None,
        ):
            columns = _exported_columns(source, source.columns(conn, table), table)
            key_columns = source.sync_key(conn, table)
            # A key column the package does not export still has to be READ,
            # or the row could not be matched back to the log entry that
            # named it.
            read_columns = columns + tuple(
                name for name in key_columns if name not in columns
            )
            changes = cached_changes.pop(table, None)
            if changes is None:
                changes = incremental.compact(
                    source, conn, table, watermark, compensation
                )
            with writer.lines(rows_path(table)) as write:
                sink = _RowSink(
                    write, users, table, columns,
                    key_columns if track_keys else None,
                    observe,
                )
                delta = incremental.table_delta(
                    source,
                    conn,
                    table=table,
                    columns=read_columns,
                    key_columns=key_columns,
                    changes=changes,
                    notebooks=notebook_state,
                    emit_row=sink,
                    emit_delete=emit_delete,
                )
                del changes
                if extra is not None:
                    for row in extra(delta, sink, columns, key_columns):
                        sink(row)
            absorb(table, delta, sink, columns)

        def seed_group(value: Any) -> None:
            if value:
                extra_global.setdefault(_PRINCIPAL_SEED_KEY_SET, set()).add(
                    str(value)
                )

        def observe_grant(row: Mapping[str, Any]) -> None:
            if str(row.get("principal_type") or "") in _GROUP_PRINCIPAL_TYPES:
                seed_group(row.get("principal_id"))

        for table in synced_tables():
            if table == "notebooks" or _scope_of(table).kind is ScopeKind.GLOBAL:
                deferred.append(table)
                continue
            process(
                table,
                observe=observe_grant if table == _PRINCIPAL_SEED_TABLE else None,
            )

        # Before any GLOBAL table is written: pull the parent ids out of the
        # deferred tables' own keys, so the parent (written first, by
        # copy_rank) can carry them. The compaction is kept and reused rather
        # than the log being read twice.
        for table, (column, key_set) in _GLOBAL_KEY_PARENTS.items():
            if table not in deferred:
                continue
            cached_changes[table] = incremental.compact(
                source, conn, table, watermark, compensation
            )
            for change in cached_changes[table].values():
                if change.operation == incremental.OPERATION_UPSERT:
                    if key_set == _PRINCIPAL_SEED_KEY_SET:
                        seed_group(change.key.get(column))
                    else:
                        extra_global.setdefault(key_set, set()).add(
                            str(change.key[column])
                        )

        process(
            "notebooks",
            track_keys=True,
            extra=lambda delta, sink, columns, key_columns: _backfilled_notebooks(
                source, conn, delta, columns, key_columns, notebooks, sink.keys
            ),
        )

        global_keys = _global_keys(source, conn, sorted(notebooks))
        for key_set, values in extra_global.items():
            global_keys[key_set] = tuple(
                sorted(set(global_keys.get(key_set, ())) | values)
            )
        for table in deferred:
            if table == "notebooks":
                continue
            process(
                table,
                track_keys=True,
                extra=lambda delta, sink, columns, key_columns, name=table: (
                    _global_seed_rows(
                        source, conn, name, columns, key_columns,
                        sink.keys, global_keys,
                    )
                ),
            )

    return _Rows(
        notebooks=tuple(sorted(notebooks)),
        skipped=skipped,
        table_counts=table_counts,
        table_entries=table_entries,
        deletes=counters["deletes"],
        deleted_notebooks=tuple(sorted(deleted_notebooks)),
        skipped_mirror_changes=counters["mirror"],
        warnings=warnings,
        file_plan=files,
    )


def _backfilled_notebooks(
    source: _Source,
    conn: Any,
    delta: Any,
    columns: Sequence[str],
    key_columns: Sequence[str],
    notebooks: set[str],
    present: set[str],
) -> list[Mapping[str, Any]]:
    """The ``notebooks`` rows this package owes for notebooks it carries rows
    or deletes for but whose own row did not change inside the window.

    Without them ``manifest.notebooks`` (every notebook the package touches)
    and ``rows/notebooks.jsonl`` (only the ones that changed) would disagree,
    and that equality is a security check, not bookkeeping: it is what stops a
    package from carrying rows for a notebook it never declared.

    Folds this table's OWN notebooks into ``notebooks`` first -- it runs
    before the caller absorbs the delta, and a notebook that changed only in
    the ``notebooks`` table has to end up declared too. ``present`` is what
    the window already wrote to this file, so a notebook is never sent twice."""
    notebooks.update(delta.notebooks)
    wanted = [
        {key_columns[0]: notebook_id}
        for notebook_id in sorted(notebooks)
        if incremental.key_text({key_columns[0]: notebook_id}) not in present
    ]
    found = incremental.read_rows_by_key(
        source, conn, "notebooks", columns, key_columns, wanted
    )
    return [found[text] for text in sorted(found)]


def _global_seed_rows(
    source: _Source,
    conn: Any,
    table: str,
    columns: Sequence[str],
    key_columns: Sequence[str],
    present: set[str],
    global_keys: Mapping[str, tuple[str, ...]],
) -> list[Mapping[str, Any]]:
    """A GLOBAL table's SEED rows: the groups/object types the package's
    notebooks reference, exactly as a full export resolves them.

    An incremental package carries these even when they did not change, for
    the same reason a full one does -- a notebook arriving at the target for
    the first time needs the group its grants point at to exist. Every GLOBAL
    table is ``seed_only``, so re-sending an unchanged row is a no-op there
    (§3), and the window's own upserts are preferred over the seed scan for
    any key both produce."""
    query = _table_query(source, conn, table, columns)
    extra: dict[str, Mapping[str, Any]] = {}
    for batch in _batched(list(global_keys[_global_scope(table)[1]]), _ID_BATCH):
        for row in _scan(source, conn, query, batch):
            text = incremental.key_text(incremental.row_key(row, key_columns))
            if text not in present:
                extra[text] = row
    return [extra[text] for text in sorted(extra)]


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
    out_dir: Path,
    warnings: Sequence[str],
    full: bool,
) -> ExportReport:
    users = _UserIds()
    scoped = notebook_ids is not None

    with source.read() as conn:
        # FIRST statement of the transaction that needs a snapshot, so on
        # PostgreSQL this both reads and PINS the visibility snapshot every
        # later statement here shares -- including the change-log window. A
        # snapshot taken after the log had been read would describe a
        # different instant than the package's own contents.
        snapshot = source.current_snapshot(conn)
        gate = _capture_gate(source, conn)
        gate_open = gate[0]
        # A scoped export is never a link in the chain, so it neither reads
        # the watermark nor writes one.
        watermark = (
            None if scoped else incremental.read_watermark(source, conn, target_env)
        )
        resumable = (
            not full
            and gate_open
            and watermark is not None
            and watermark.captured
        )
        if resumable:
            produced = _incremental_rows(source, conn, writer, users, watermark)
        else:
            produced = _full_rows(source, conn, writer, users, notebook_ids)
        user_count = _write_users(source, conn, writer, sorted(users.ids))
        with writer.lines(KG_EPOCHS_NAME):
            pass  # PR-3b does not fold KG resets; see incremental.OPERATION_KG_EPOCH.
        captured_through_seq = _captured_through_seq(source, conn, gate_open)

    mode = MODE_INCREMENTAL if resumable else MODE_FULL
    if scoped:
        # A subset package makes no claim about a sequence range: it is not on
        # the chain, so a reader must not be able to mistake it for one.
        from_seq, to_seq, base_package_id = 0, 0, ""
    elif resumable:
        assert watermark is not None
        from_seq = watermark.exported_through_seq + 1
        # max(): ``sync prune-log`` deletes log rows at or below every
        # captured watermark, so a quiet environment's log can end up EMPTY
        # and MAX(seq) reads 0 -- lower than the watermark it was pruned
        # against. Clamping here (and storing this, not the raw observation,
        # in _advance_watermark below) is what keeps the watermark monotonic:
        # letting it fall back would re-open a window over seq values that
        # have already been exported AND no longer exist. An empty window then
        # spells from_seq = W + 1 > to_seq = W, which is the honest way to say
        # "this package covers no sequence range at all".
        to_seq = max(captured_through_seq, watermark.exported_through_seq)
        base_package_id = watermark.package_id
    else:
        # A full re-baseline (``--full``, a closed gate, or a watermark that
        # predates capture) is still a link in the chain and is subject to
        # the same clamp: after ``sync prune-log`` emptied the log, MAX(seq)
        # reads 0 and must not drag an existing watermark back down.
        from_seq, base_package_id = 0, ""
        to_seq = max(
            captured_through_seq,
            watermark.exported_through_seq if watermark is not None else 0,
        )

    # Outside the row snapshot: rows first, then the files they point at (see
    # _write_files).
    run_warnings = list(warnings) + produced.warnings
    storage_dir = Path(settings.storage_dir)
    if resumable:
        requests, unresolved, file_warnings = incremental.resolve_file_requests(
            storage_dir, source.root_dir, produced.file_plan
        )
        run_warnings.extend(file_warnings)
        file_count, missing_files = _write_changed_files(writer, requests)
        # Files the phase could not even attempt are the same kind of gap as
        # one that vanished mid-copy, and the manifest reports them together.
        missing_files = sorted(set(missing_files) | set(unresolved))
    else:
        file_count, missing_files = _write_files(
            writer, storage_dir, produced.file_plan
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
            "mode": mode,
            "from_seq": from_seq,
            "to_seq": to_seq,
            "base_package_id": base_package_id,
            # Whether ``--notebook`` narrowed this export. A scoped package is
            # a one-off copy of the notebooks an operator named: it takes no
            # lease, writes no watermark and is not a link in the chain, so an
            # importer must not let it displace the baseline a window
            # continues from. The importer cannot infer that -- a scoped full
            # package and a first unscoped baseline over an empty log are
            # otherwise identical on the wire (both ``0 .. 0``, both without a
            # base) -- so the exporter, which is the only side that knows,
            # says so. An additive field: format 2 readers that predate it see
            # nothing new, and this build reads its absence as "unknown"
            # rather than as either answer (codex #788 r2 P1).
            "scoped": scoped,
            "notebooks": list(produced.notebooks),
            "deleted_notebooks": list(produced.deleted_notebooks),
            "tables": produced.table_entries,
            "deletes": produced.deletes,
            "kg_epochs": 0,
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
    final_dir = out_dir / package_dir_name(source_env, from_seq, to_seq, package_id)
    writer.root.rename(final_dir)
    captured = False
    if not scoped:
        # ``to_seq``, not the raw observation: the package and the watermark
        # must name the same point, and that point may never move backwards.
        try:
            captured, gate_warning = _advance_watermark(
                source,
                target_env,
                package_id,
                exported_at,
                to_seq,
                gate=gate,
                snapshot=snapshot,
                base_package_id=base_package_id,
                run_id=package_id,
            )
        except SyncExportError:
            # The package is on disk under its final name but no watermark
            # names it, and nothing ever will -- a reader walking the chain
            # by base_package_id would step straight past it. Remove it
            # rather than leave a package that looks complete and is not
            # reachable.
            shutil.rmtree(final_dir, ignore_errors=True)
            raise
        if gate_warning:
            run_warnings.append(gate_warning)
    return ExportReport(
        package_dir=final_dir,
        package_id=package_id,
        notebooks=produced.notebooks,
        skipped=MappingProxyType(dict(sorted(produced.skipped.items()))),
        table_counts=MappingProxyType(dict(sorted(produced.table_counts.items()))),
        file_count=file_count,
        bytes_written=writer.bytes_written,
        mode=mode,
        captured_through_seq=captured_through_seq,
        from_seq=from_seq,
        to_seq=to_seq,
        base_package_id=base_package_id,
        deletes=produced.deletes,
        deleted_notebooks=produced.deleted_notebooks,
        skipped_mirror_changes=produced.skipped_mirror_changes,
        captured=captured,
        empty=resumable and produced.deletes == 0
        and not any(produced.table_counts.values()),
        scoped=scoped,
        watermark_advanced=not scoped,
        run_id="" if scoped else package_id,
        warnings=tuple(run_warnings),
        missing_files=tuple(missing_files),
    )

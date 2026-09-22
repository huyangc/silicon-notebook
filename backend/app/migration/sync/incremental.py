"""Reading ``sync_change_log`` back as ONE export window
(docs/incremental-sync-design.md §7 "增量窗口").

This module answers a single question for the exporter: *given the watermark
the last export left behind, which rows does this package have to carry, and
which keys did the source delete?* It never decides WHETHER the export is
incremental (``export._assemble`` does that from the capture gate, the
watermark row and the caller's ``--full``/``--notebook``), and it never writes
a package file -- it hands back per-table deltas and lets the exporter's
existing writer/encoder put them on disk, so an incremental package and a full
one are produced by exactly one set of formatting code.

The window has two halves, and the second one is the whole reason
``sync_change_log.txid`` exists:

1. The MAIN window, per table: ``table_name = ? AND seq > W ORDER BY seq``,
   which rides the existing ``idx_sync_change_log_table_seq`` index.
2. The COMPENSATION window, PostgreSQL only, one global statement for the
   whole export: the log rows written by transactions that were still IN
   FLIGHT when the previous export took its snapshot and committed only
   afterwards. Their ``seq`` is at or below ``W`` (they took their sequence
   value before that export finished), so the main window will never reach
   them again, and the previous export could not see them either -- without
   this half they are skipped forever, silently, because nothing is missing
   from the log, only from the windows we chose.

   The predicate that DECIDES membership is the visibility test,
   ``NOT pg_visible_in_snapshot(txid, S_prev)``: a row the previous export's
   snapshot could not see is a row it did not export, whatever its ``seq``.
   ``txid >= pg_snapshot_xmin(S_prev)`` adds nothing to that decision -- it
   is an ACCESS PATH, the index range over ``idx_sync_change_log_txid``
   (v85/0065) that cannot exclude any qualifying row, since everything below
   a snapshot's ``xmin`` is by definition already visible in it. Dropping the
   bound would still be correct and would only cost a full scan of the log.

   SQLite has no second half and needs none: one writer at a time means there
   is no "assigned but not yet committed" window for another reader to miss,
   and no REPEATABLE READ snapshot to miss it in.

Everything here runs on the connection ``export._Source.read()`` yielded, i.e.
inside the one snapshot the row scan itself uses. Reading the log on a second
connection would describe a different instant than the rows the package
carries, which is the same mistake the compensation window exists to undo.

Memory: compaction is per table and the dict is dropped before the next table
is touched (``export`` calls ``compact``/``table_delta`` in one loop body), so
the peak is the number of DISTINCT KEYS ONE table saw in this window -- not
the number of log rows, and not the whole window. For a daily export that is
the day's edited rows of the single busiest table; for the first export after
a long gap it can approach that table's row count, which is the same order of
magnitude a full export of the table already materializes one page at a time.
An export is an offline tool and this is an accepted ceiling (§11).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.migration.sync.database import (
    _ID_BATCH,
    SyncExportError,
    _batched,
    _quoted,
)
from app.migration.sync.manifest import ScopeKind, scope_chain, spec_for
from app.migration.sync.package import (
    ASSET_FILES_DIR,
    NOTEBOOK_FILES_DIR,
    delete_entry,
    is_safe_identifier,
    notebook_assets_dir,
    notebook_files_dir,
)


LOG_TABLE = "sync_change_log"
STATE_TABLE = "sync_export_state"

OPERATION_UPSERT = "upsert"
OPERATION_DELETE = "delete"
# Ignored during compaction: PR-3b does not fold a KG reset into a single
# "replace the whole graph" instruction. That fold is an optimization, never a
# correctness premise -- the selective row-level deletes a reset performs are
# each logged in full anyway (§7 "压缩", §11 for when it becomes worth doing).
OPERATION_KG_EPOCH = "kg_epoch"

_LOG_COLUMNS = "seq, table_name, key_json, operation, parent_key, notebook_id"

_MAIN_WINDOW_SQL = (
    f"SELECT {_LOG_COLUMNS} FROM {LOG_TABLE} "
    "WHERE table_name = ? AND seq > ? ORDER BY seq"
)

# One statement for the whole export (see the module docstring). ``txid`` is
# bound as a plain INTEGER, not as a function call over the stored snapshot
# text, so the planner can use it as an index range at plan time; ``seq`` is
# bound the same way and written next to it so the pair is usable as a leading
# range on ``idx_sync_change_log_txid`` whether that index is ``(txid)`` or
# ``(txid, seq)``. Neither bound DECIDES membership -- the visibility test
# does; they only narrow what has to be read (module docstring).
_COMPENSATION_WINDOW_SQL = (
    f"SELECT {_LOG_COLUMNS} FROM {LOG_TABLE} "
    "WHERE txid >= ? AND seq <= ? "
    "AND NOT pg_visible_in_snapshot(txid::text::xid8, ?::pg_snapshot) "
    "ORDER BY seq"
)

# Lifecycle states in which a notebook's rows are mid-copy/mid-delete/mid-import
# and no snapshot of them is a coherent notebook. Same value list as
# ``export._NOT_LIVE_STATUSES``; kept as its own name here so a reader of this
# module can see the predicate it applies without chasing the import.
_NOT_LIVE_STATUSES = ("copying", "deleting", "importing")

LIVE = "live"
MIRROR = "mirror"
NOT_LIVE = "not_live"
MISSING = "missing"


@dataclass(frozen=True)
class Watermark:
    """One row of ``sync_export_state`` -- where the last export of this
    target environment stopped, and whether that stopping point is one an
    incremental window may resume from."""

    target_env: str
    # MAX(seq) visible inside the PREVIOUS export's read snapshot.
    exported_through_seq: int
    # That export's ``pg_current_snapshot()::text``; None on SQLite (and on a
    # watermark row written before v85/0065).
    exported_snapshot: str | None
    package_id: str
    # v85/0065. True only when the capture gate was open for that export, i.e.
    # there is a change log behind this seq. A watermark carried over from
    # before v85/0065 reads False, so the first export after the upgrade is
    # necessarily full -- correct, because that row has no snapshot to
    # compensate against.
    captured: bool


@dataclass(frozen=True)
class Change:
    """One ``(table, key)``'s FINAL state inside the window."""

    operation: str
    key: dict[str, Any]
    # Straight off the log row: set for NOTEBOOK-scoped tables, NULL for
    # PARENT and GLOBAL ones.
    notebook_id: str | None
    # Straight off the log row: set for PARENT-scoped tables only.
    parent_key: str | None


@dataclass(frozen=True)
class FileRequest:
    """One file an incremental package has to carry, named the way the FILE
    PHASE can resolve it -- after the read snapshot has closed, like
    ``export._write_files``. Either ``path`` (an absolute path read out of a
    row's path column) or ``stem`` (an id whose ``<stem>.*`` files under the
    notebook's asset root belong to that row) is set, never both."""

    root: str
    notebook_id: str
    path: str = ""
    stem: str = ""


@dataclass
class TableDelta:
    """What one table contributes to an incremental package."""

    # Raw source rows (still in the database's own shape -- the caller runs
    # them through ``_row_encoder``), ordered by canonical key text.
    rows: list[dict[str, Any]] = field(default_factory=list)
    # ``deletes.jsonl`` entries, same ordering.
    deletes: list[dict[str, Any]] = field(default_factory=list)
    # Live, non-mirror notebooks this table's contribution belongs to.
    notebooks: set[str] = field(default_factory=set)
    # Only ever non-empty for the ``notebooks`` table itself.
    deleted_notebooks: set[str] = field(default_factory=set)
    files: list[FileRequest] = field(default_factory=list)
    # Log rows dropped because they belong to a mirrored notebook.
    skipped_mirror: int = 0
    # notebook id -> why its changes were left out (not-live lifecycle state).
    skipped: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------- watermark row


def read_watermark(source: Any, conn: Any, target_env: str) -> Watermark | None:
    """This target's watermark row, or None when it has never been exported to.

    Read on the export's OWN read connection, so the mode decision and the
    window it leads to are taken against one instant."""
    rows = source.fetch(
        conn,
        "SELECT exported_through_seq, exported_snapshot, package_id, captured "
        f"FROM {STATE_TABLE} WHERE target_env = ?",
        (target_env,),
    )
    if not rows:
        return None
    row = rows[0]
    snapshot = row["exported_snapshot"]
    return Watermark(
        target_env=target_env,
        exported_through_seq=int(row["exported_through_seq"] or 0),
        exported_snapshot=str(snapshot) if snapshot else None,
        package_id=str(row["package_id"] or ""),
        captured=bool(row["captured"]),
    )


# ------------------------------------------------------------------ the window


def key_text(key: Mapping[str, Any]) -> str:
    """The canonical text of one row key, used as the compaction dict's key
    and as the package's row ordering.

    Built from the PARSED object, never from ``key_json``'s bytes: SQLite's
    ``json_object`` keeps the trigger's column order while PostgreSQL's
    ``jsonb`` normalizes it, so the same row produces different TEXT on the
    two backends. Sorting the keys here is what makes the two comparable --
    and what makes one export of one window byte-reproducible."""
    return json.dumps(dict(key), ensure_ascii=False, sort_keys=True)


def row_key(row: Mapping[str, Any], key_columns: Sequence[str]) -> dict[str, Any]:
    """A live row's key as the same column -> value mapping ``key_json``
    carries, so a row read back from the table and a key read out of the log
    compare as equals."""
    return {name: row[name] for name in key_columns}


def _parse_key(value: Any, table: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise SyncExportError(
            f"{LOG_TABLE}: {table!r} has a change-log row whose key_json is "
            f"not JSON: {value!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise SyncExportError(
            f"{LOG_TABLE}: {table!r} has a change-log row whose key_json is "
            f"not a JSON object: {value!r}"
        )
    return parsed


def compensation_rows(
    source: Any, conn: Any, watermark: Watermark
) -> dict[str, list[dict[str, Any]]]:
    """The whole compensation window (see the module docstring), grouped by
    table and left in ``seq`` order inside each group.

    Materialized once for the entire export rather than per table: it is ONE
    index range over the transactions that were in flight at the previous
    export's snapshot, and splitting it per table would turn one small scan
    into 46 of them. Empty on SQLite, and empty for a watermark with no stored
    snapshot (a pre-v85/0065 row -- which ``export`` never treats as
    resumable anyway, since ``captured`` is 0 there)."""
    if not source.is_postgres or not watermark.exported_snapshot:
        return {}
    xmin = source.snapshot_xmin(watermark.exported_snapshot)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in source.stream(
        conn,
        _COMPENSATION_WINDOW_SQL,
        (xmin, watermark.exported_through_seq, watermark.exported_snapshot),
    ):
        grouped.setdefault(str(row["table_name"]), []).append(row)
    return grouped


def compact(
    source: Any,
    conn: Any,
    table: str,
    watermark: Watermark,
    compensation: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Change]:
    """Fold this table's window into one final operation per ``(table, key)``.

    The compensation rows go first and the main window second, which is
    already global ``seq`` order: every compensation row has ``seq <= W`` by
    construction and every main-window row has ``seq > W``. Within each half
    the statement's own ``ORDER BY seq`` holds. So a plain last-writer-wins
    assignment yields the window's FINAL state -- insert-then-delete collapses
    to a delete, delete-then-reinsert collapses to an upsert, and a key
    touched fifty times costs one entry.

    ``kg_epoch`` rows are dropped here; they are events, not row states."""
    final: dict[str, Change] = {}
    for row in compensation.get(table, ()):
        _fold(final, table, row)
    for row in source.stream(
        conn, _MAIN_WINDOW_SQL, (table, watermark.exported_through_seq)
    ):
        _fold(final, table, row)
    return final


def _fold(
    final: dict[str, Change], table: str, row: Mapping[str, Any]
) -> None:
    operation = str(row["operation"])
    if operation == OPERATION_KG_EPOCH:
        return
    if operation not in (OPERATION_UPSERT, OPERATION_DELETE):
        raise SyncExportError(
            f"{LOG_TABLE}: {table!r} has a change-log row with the unknown "
            f"operation {operation!r}"
        )
    key = _parse_key(row["key_json"], table)
    notebook_id = row["notebook_id"]
    parent_key = row["parent_key"]
    final[key_text(key)] = Change(
        operation=operation,
        key=key,
        notebook_id=str(notebook_id) if notebook_id else None,
        parent_key=str(parent_key) if parent_key else None,
    )


# --------------------------------------------------------------- keyed reads


def read_rows_by_key(
    source: Any,
    conn: Any,
    table: str,
    columns: Sequence[str],
    key_columns: Sequence[str],
    keys: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Read the CURRENT rows for a batch of keys, returned by canonical key
    text.

    A composite key is matched with an SQL row-value ``IN`` list --
    ``(t0."a", t0."b") IN ((?,?), (?,?))`` -- which SQLite (>= 3.15) and
    PostgreSQL both support; a single-column key uses a plain ``IN`` list
    instead, because a one-element row value is not a row value in either
    dialect (``(x) IN ((1))`` parses as ordinary parentheses) and spelling it
    that way would only obscure what runs.

    The batch size divides the placeholder budget by the key's width, so a
    two-column key sends half as many rows per statement rather than twice as
    many placeholders (SQLITE_MAX_VARIABLE_NUMBER is 999 on older builds).
    """
    if not keys:
        return {}
    tuples = [tuple(key[name] for name in key_columns) for key in _checked(
        keys, key_columns, table
    )]
    projection = ", ".join(f"t0.{_quoted(name)}" for name in columns)
    prefix = f"SELECT {projection} FROM {_quoted(table)} t0 WHERE "
    width = len(key_columns)
    found: dict[str, dict[str, Any]] = {}
    for batch in _batched(tuples, max(1, _ID_BATCH // width)):
        if width == 1:
            placeholders = ",".join("?" for _ in batch)
            where = f"t0.{_quoted(key_columns[0])} IN ({placeholders})"
            params: list[Any] = [item[0] for item in batch]
        else:
            row_value = ",".join(f"t0.{_quoted(name)}" for name in key_columns)
            one = "(" + ",".join("?" for _ in key_columns) + ")"
            where = f"({row_value}) IN ({','.join(one for _ in batch)})"
            params = [value for item in batch for value in item]
        for row in source.fetch(conn, prefix + where, params):
            found[key_text(row_key(row, key_columns))] = row
    return found


def _checked(
    keys: Sequence[Mapping[str, Any]], key_columns: Sequence[str], table: str
) -> Iterator[Mapping[str, Any]]:
    for key in keys:
        missing = [name for name in key_columns if name not in key]
        if missing:
            raise SyncExportError(
                f"{table}: a change-log key is missing the column(s) {missing}; "
                f"the log was written under a different key than {tuple(key_columns)} "
                "(capture triggers and the manifest key have drifted)"
            )
        yield key


# ------------------------------------------------------------- attribution


def resolve_parent_notebooks(
    source: Any, conn: Any, table: str, values: Sequence[Any]
) -> dict[Any, str | None]:
    """Resolve a PARENT-scoped table's parent keys to notebook ids, one
    batched statement per hop of ``scope_chain`` (not one per row).

    A parent row that is already gone resolves to ``None`` and stays ``None``
    for the rest of the walk: its child's delete is an orphan whose notebook
    the SOURCE can no longer name, and this module records that rather than
    guessing (§7 "归属" -- PR-3c resolves it against the target's own parent
    chain, where a truly orphaned delete is a no-op)."""
    if not values:
        return {}
    resolved: dict[Any, Any] = {value: value for value in dict.fromkeys(values)}
    for _child, _child_column, parent in scope_chain(table):
        parent_key = source.sync_key(conn, parent)
        if len(parent_key) != 1:
            raise SyncExportError(
                f"{table}: parent {parent!r} has the key {parent_key}; a scope "
                "chain can only follow a single-column key"
            )
        scope = spec_for(parent).scope
        if scope is None:
            raise SyncExportError(f"{table}: scope chain reaches LOCAL {parent!r}")
        hop: dict[Any, Any] = {}
        wanted = sorted(
            {value for value in resolved.values() if value is not None},
            key=str,
        )
        for batch in _batched(wanted):
            placeholders = ",".join("?" for _ in batch)
            rows = source.fetch(
                conn,
                f"SELECT {_quoted(parent_key[0])} AS parent_key, "
                f"{_quoted(scope.column)} AS scope_value "
                f"FROM {_quoted(parent)} "
                f"WHERE {_quoted(parent_key[0])} IN ({placeholders})",
                batch,
            )
            for row in rows:
                hop[row["parent_key"]] = row["scope_value"]
        resolved = {
            value: (hop.get(current) if current is not None else None)
            for value, current in resolved.items()
        }
    return {
        value: (str(current) if current else None)
        for value, current in resolved.items()
    }


class NotebookState:
    """Current lifecycle/mirror state of the notebooks an incremental window
    touches, resolved in batches and cached for the whole export.

    Cached rather than re-queried per table because the same handful of
    notebooks is touched by table after table, and the answer must not move
    inside one export anyway (it is read inside the export's own snapshot).
    A notebook that no longer exists answers ``MISSING``, which is not an
    error: a window that deletes a notebook necessarily has log rows whose
    notebook is gone by the time the export reads it."""

    def __init__(self, source: Any, conn: Any) -> None:
        self._source = source
        self._conn = conn
        self._known: dict[str, tuple[str, str]] = {}

    def prime(self, notebook_ids: Sequence[str]) -> None:
        wanted = sorted(
            {value for value in notebook_ids if value and value not in self._known}
        )
        for batch in _batched(wanted):
            placeholders = ",".join("?" for _ in batch)
            rows = self._source.fetch(
                self._conn,
                "SELECT id, status, sync_origin FROM notebooks "
                f"WHERE id IN ({placeholders})",
                batch,
            )
            for row in rows:
                origin = str(row["sync_origin"] or "")
                status = str(row["status"] or "")
                if origin:
                    state = (MIRROR, f"mirror of {origin!r}; a mirror cannot be a source")
                elif status in _NOT_LIVE_STATUSES:
                    state = (NOT_LIVE, f"status={status!r}")
                else:
                    state = (LIVE, "")
                self._known[str(row["id"])] = state
        for notebook_id in wanted:
            self._known.setdefault(notebook_id, (MISSING, "no such notebook"))

    def state(self, notebook_id: str) -> tuple[str, str]:
        if notebook_id not in self._known:
            self.prime([notebook_id])
        return self._known[notebook_id]


# ------------------------------------------------------------------- deltas


def table_delta(
    source: Any,
    conn: Any,
    *,
    table: str,
    columns: Sequence[str],
    key_columns: Sequence[str],
    changes: Mapping[str, Change],
    notebooks: NotebookState,
) -> TableDelta:
    """Turn one table's compacted window into the rows, deletes, notebook
    attribution and file requests the package needs.

    Four rules this function is the single home of:

    - A final ``upsert`` whose row cannot be read back in this snapshot is
      DOWNGRADED to a delete with a warning. It should not happen (the delete
      that removed the row would have been logged too, and would have won the
      compaction); if it does, shipping a delete is the conservative answer --
      shipping nothing would leave the target holding a row the source no
      longer has.
    - A ``seed_only`` table's deletes are NOT exported at all (§5 "授权只播种"):
      the target owns membership and sharing decisions made after the first
      import, so a source-side revocation must not travel. The same rule
      already keeps import from overwriting those rows.
    - A change attributed to a MIRRORED notebook is dropped and counted: a
      mirror's content came from some other source environment, and
      re-exporting it would launder that environment's rows into this one's
      name and make two environments each other's upstream.
    - A change attributed to a notebook in a mid-copy/mid-delete/mid-import
      state is dropped with a reason, the same predicate a full export's
      notebook selection applies.
    """
    delta = TableDelta()
    if not changes:
        return delta
    scope = spec_for(table).scope
    if scope is None:
        raise SyncExportError(f"{table}: LOCAL table has no export scope")
    seed_only = spec_for(table).seed_only

    upserts = {text: change for text, change in changes.items()
               if change.operation == OPERATION_UPSERT}
    deletes = {text: change for text, change in changes.items()
               if change.operation == OPERATION_DELETE}
    rows = read_rows_by_key(
        source, conn, table, columns, key_columns,
        [change.key for change in upserts.values()],
    )
    for text in sorted(set(upserts) - set(rows)):
        delta.warnings.append(
            f"{table}: {text} was logged as an upsert but no longer exists in "
            "the export snapshot; exported as a delete instead"
        )
        deletes[text] = upserts.pop(text)

    attribution = _attribute(
        source, conn, table, scope, upserts, deletes, rows, key_columns
    )
    notebooks.prime([value for value in attribution.values() if value])

    for text in sorted(upserts):
        notebook_id = attribution.get(text)
        state = _classify(notebook_id, notebooks, delta)
        if state in (MIRROR, NOT_LIVE):
            continue
        row = rows[text]
        delta.rows.append(row)
        if notebook_id and state == LIVE:
            delta.notebooks.add(notebook_id)
            delta.files.extend(_file_requests(table, row, notebook_id))
    if seed_only:
        return delta
    for text in sorted(deletes):
        change = deletes[text]
        notebook_id = attribution.get(text)
        if _classify(notebook_id, notebooks, delta) in (MIRROR, NOT_LIVE):
            continue
        delta.deletes.append(
            delete_entry(table, change.key, notebook_id, change.parent_key)
        )
        if table == "notebooks":
            delta.deleted_notebooks.add(str(change.key[key_columns[0]]))
    return delta


def _classify(
    notebook_id: str | None, notebooks: NotebookState, delta: TableDelta
) -> str:
    """The state that decides whether one attributed change travels, recording
    the reason on the delta when it does not.

    ``MIRROR`` and ``NOT_LIVE`` are dropped. ``MISSING`` travels -- a window
    that deleted a notebook, or a row whose notebook was deleted with it, must
    still be able to tell the target so -- but it is NOT counted as a notebook
    this package carries: there is nothing left to carry. A row with no
    notebook at all (GLOBAL scope, or a PARENT orphan whose chain is gone)
    reports ``MISSING`` for the same reason."""
    if not notebook_id:
        return MISSING
    state, reason = notebooks.state(notebook_id)
    if state == MIRROR:
        delta.skipped_mirror += 1
        delta.skipped[notebook_id] = reason
        return state
    if state == NOT_LIVE:
        delta.skipped[notebook_id] = reason
    return state


def _attribute(
    source: Any,
    conn: Any,
    table: str,
    scope: Any,
    upserts: Mapping[str, Change],
    deletes: Mapping[str, Change],
    rows: Mapping[str, dict[str, Any]],
    key_columns: Sequence[str],
) -> dict[str, str | None]:
    """``key text -> notebook id`` for one table's window.

    NOTEBOOK scope reads the notebook off the live row when there is one and
    off the log row otherwise (a delete has no row left to read). PARENT
    scope resolves the row's own parent pointer for an upsert and the log's
    ``parent_key`` for a delete, both through one batched ``scope_chain``
    walk. GLOBAL scope has no notebook by definition."""
    if scope.kind is ScopeKind.GLOBAL:
        return {text: None for text in (*upserts, *deletes)}
    if scope.kind is ScopeKind.NOTEBOOK:
        attribution: dict[str, str | None] = {}
        for text, change in upserts.items():
            value = rows[text].get(scope.column)
            attribution[text] = str(value) if value else change.notebook_id
        for text, change in deletes.items():
            # ``notebooks``' own scope column IS its key, so a delete of the
            # notebook row can name its notebook even if the log row did not.
            fallback = (
                str(change.key[key_columns[0]]) if table == "notebooks" else None
            )
            attribution[text] = change.notebook_id or fallback
        return attribution
    pointers: dict[str, Any] = {}
    for text in upserts:
        pointers[text] = rows[text].get(scope.column)
    for text, change in deletes.items():
        pointers[text] = change.parent_key
    resolved = resolve_parent_notebooks(
        source, conn, table, [value for value in pointers.values() if value]
    )
    return {
        text: (resolved.get(value) if value else None)
        for text, value in pointers.items()
    }


# --------------------------------------------------------------------- files


def _file_requests(
    table: str, row: Mapping[str, Any], notebook_id: str
) -> list[FileRequest]:
    """The on-disk files ONE upserted row points at.

    ``sources.file_path`` is the only path column the shadow manifest declares
    on a synced table, and it holds the source host's absolute path -- which
    is this host, since we are the source, so it resolves directly.
    ``notebook_assets`` declares none: its bytes are named
    ``<asset id>.<extension>`` under ``storage/assets/<notebook>/`` by
    ``AssetService.path_for``, and the extension comes from a mime table this
    package may not import (and must not duplicate, or the two copies drift).
    So an asset row asks for its id as a STEM and the file phase takes
    whatever ``<id>.*`` is actually on disk -- which is also the honest
    answer if a deployment ever stores a second derivative beside it.

    Resolution is deferred to the file phase on purpose: it touches the
    filesystem, and the filesystem is not part of the read snapshot (see
    ``export._write_files`` for the same reasoning)."""
    if table == "sources":
        value = row.get("file_path")
        if isinstance(value, str) and value:
            return [FileRequest(NOTEBOOK_FILES_DIR, notebook_id, path=value)]
        return []
    if table == "notebook_assets":
        asset_id = str(row.get("id") or "")
        if asset_id:
            return [FileRequest(ASSET_FILES_DIR, notebook_id, stem=asset_id)]
    return []


def resolve_file_requests(
    storage_dir: Path, requests: Sequence[FileRequest]
) -> tuple[list[tuple[str, Path]], list[str]]:
    """``([(package-relative path, source path)], warnings)`` for the file
    phase, de-duplicated and ordered by the package path.

    Runs AFTER the read snapshot closes, so the answers describe the disk as
    the copy will find it. Two refusals rather than silent surprises: a path
    column pointing outside its own notebook's storage root is reported and
    skipped (this environment's rows should never say that, and splicing such
    a path into a package path would be a traversal), and an id that is not a
    safe single path segment is never globbed with."""
    package_dir_for = {
        NOTEBOOK_FILES_DIR: notebook_files_dir,
        ASSET_FILES_DIR: notebook_assets_dir,
    }
    found: dict[str, Path] = {}
    warnings: list[str] = []
    for request in requests:
        base = Path(storage_dir) / request.root / request.notebook_id
        prefix = package_dir_for[request.root](request.notebook_id)
        if request.path:
            origin = Path(request.path)
            try:
                relative = origin.relative_to(base)
            except ValueError:
                warnings.append(
                    f"{request.root}/{request.notebook_id}: {request.path!r} is "
                    "not under this notebook's storage root and was not carried"
                )
                continue
            found[f"{prefix}/{relative.as_posix()}"] = origin
            continue
        if not is_safe_identifier(request.stem):
            warnings.append(
                f"{request.root}/{request.notebook_id}: {request.stem!r} is not "
                "a safe file name and its bytes were not carried"
            )
            continue
        for origin in sorted(base.glob(f"{request.stem}.*")):
            if origin.is_file():
                found[f"{prefix}/{origin.name}"] = origin
    return sorted(found.items()), warnings

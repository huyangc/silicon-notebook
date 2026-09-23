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

Memory, by what is actually held at once:

- The compaction dict for ONE table: key text -> final operation, dropped
  before the next table is touched. This is the peak, and it is the number of
  DISTINCT KEYS that table saw in the window (not log rows, not the window).
  For a daily export that is the day's edited rows of the single busiest
  table; after a long gap it can approach that table's row count.
- ONE BATCH of read-back rows (``_ID_BATCH`` divided by the key's width).
  Rows are written to the package as they are read and dropped with the
  batch -- ``table_delta`` never accumulates a table's rows, which is the
  whole point of it taking writer callbacks instead of returning lists.
- The compensation window, whole, for the export's duration -- key identities
  only, one entry per log row a transaction in flight at the previous
  snapshot wrote (see ``compensation_rows`` for when that stops being small).
- One ``FileRequest`` per changed ``sources``/``notebook_assets`` row: the
  file phase runs after the read snapshot closes and cannot ask again.

An export is an offline tool and the first of these is an accepted ceiling
(§11).
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
from app.migration.shadow.manifest import MANIFEST as _SHADOW_MANIFEST
from app.migration.sync.manifest import (
    ScopeKind,
    scope_chain,
    spec_for,
    synced_tables,
)
from app.migration.sync.package import (
    ASSET_FILES_DIR,
    NOTEBOOK_FILES_DIR,
    delete_entry,
    is_safe_identifier,
    is_safe_relative_path,
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

# What a touched notebook can be, as far as this window is concerned. There is
# deliberately no "mid-copy / mid-delete / mid-import" state: ``notebooks.
# status`` is a TARGET-owned column and a source-side lifecycle state says
# nothing about what the target should hold. Skipping on it would drop exactly
# the changes made during that window while the watermark moved past them --
# see ``export._select_notebooks`` for the full argument.
# This environment's own notebook, still here. Deliberately NOT "live": the
# lifecycle ``status`` column plays no part in the answer (see NotebookState).
PRESENT = "present"
MIRROR = "mirror"
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


# How many downgraded keys one table names in its warning before the message
# switches to a count. A window that downgrades thousands of keys has one
# problem, not thousands, and an operator report has to stay readable.
_DOWNGRADE_SAMPLES = 5


@dataclass
class TableDelta:
    """What one table contributed to an incremental package.

    COUNTS AND SETS ONLY -- the rows and delete entries themselves went
    straight to the package writer as they were produced (see
    ``table_delta``). The one list still held here is ``files``: one entry per
    changed ``sources``/``notebook_assets`` row, because the file phase runs
    after the read snapshot closes and cannot ask again."""

    rows: int = 0
    deletes: int = 0
    # Live, non-mirror notebooks this table's contribution belongs to -- rows
    # AND deletes, so the caller re-sends a ``notebooks`` row for each.
    notebooks: set[str] = field(default_factory=set)
    # Only ever non-empty for the ``notebooks`` table itself.
    deleted_notebooks: set[str] = field(default_factory=set)
    files: list[FileRequest] = field(default_factory=list)
    # Log rows dropped because they belong to a mirrored notebook.
    skipped_mirror: int = 0
    # notebook id -> why its changes were left out (mirror, today the only
    # reason a notebook that exists is skipped).
    skipped: dict[str, str] = field(default_factory=dict)
    # Upserts whose row was gone at read-back time, and a bounded sample of
    # their keys. Counted rather than one warning per key.
    downgraded: int = 0
    _samples: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def note_downgrade(self, texts: Sequence[str]) -> None:
        self.downgraded += len(texts)
        for text in texts:
            if len(self._samples) < _DOWNGRADE_SAMPLES:
                self._samples.append(text)

    def sealed(self, table: str) -> "TableDelta":
        """Fold the bounded counters into the warning text. Called once per
        table, after the last batch, so the message can say how many keys it
        is really talking about."""
        if self.downgraded:
            more = self.downgraded - len(self._samples)
            tail = f" (+{more} more)" if more > 0 else ""
            self.warnings.append(
                f"{table}: {self.downgraded} key(s) logged as upserts no longer "
                f"exist in the export snapshot and were exported as deletes: "
                f"{', '.join(self._samples)}{tail}"
            )
        return self


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

    Materialized once for the entire export rather than per table, and held
    whole: these rows carry key identities only (no payload), and their count
    is the number of log rows written by transactions that were still in
    flight at the previous export's snapshot -- normally a handful. Splitting
    the read per table would turn one scan into 46 of them.

    The cost is NOT unconditionally small. ``txid >= xmin(S_prev)`` is a cheap
    index range only while that ``xmin`` is recent; a long-running or
    ``idle in transaction`` session on the source holds the snapshot's ``xmin``
    down, and the range then widens towards the whole log, with one
    ``pg_visible_in_snapshot`` call per row scanned. The rows RETURNED stay
    few either way (only genuinely invisible ones qualify), so this degrades
    into a slow export rather than a large one -- and the fix is on the
    database side (do not leave transactions open), not here.

    Empty on SQLite, and empty for a watermark with no stored snapshot (a
    pre-v85/0065 row -- which ``export`` never treats as resumable anyway,
    since ``captured`` is 0 there)."""
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

    ``keys`` is ONE batch: callers that walk a whole window size their
    batches with ``key_batches`` first, because the batch is also the unit of
    memory -- its rows are written out and dropped before the next batch is
    read. The loop below still chunks, as a backstop for a caller (the
    ``notebooks`` re-send) whose key list is small and bounded by the number
    of notebooks rather than by the window.
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

    Every hop compares parent keys as the database hands them back, and the
    de-duplication/ordering here sorts them with ``str`` as the key. That is
    sound only because every PARENT chain's parent key is a single TEXT column
    today (uuid-shaped ids on ``sources``, ``knowhow_tables``,
    ``knowhow_rows``, ``memory_items``); ``sync_key`` already refuses a
    multi-column parent key, and a future numeric one would need this
    comparison revisited rather than left to ``str``.

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
    """Whether each notebook an incremental window touches is this
    environment's own, a mirror of another one, or already gone -- resolved in
    batches and cached for the whole export.

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
                "SELECT id, sync_origin FROM notebooks "
                f"WHERE id IN ({placeholders})",
                batch,
            )
            for row in rows:
                origin = str(row["sync_origin"] or "")
                self._known[str(row["id"])] = (
                    (MIRROR, f"mirror of {origin!r}; a mirror cannot be a source")
                    if origin
                    else (PRESENT, "")
                )
        for notebook_id in wanted:
            self._known.setdefault(notebook_id, (MISSING, "no such notebook"))

    def state(self, notebook_id: str) -> tuple[str, str]:
        if notebook_id not in self._known:
            self.prime([notebook_id])
        return self._known[notebook_id]


# ------------------------------------------------------------------- deltas


def key_batches(texts: Sequence[str], width: int) -> Iterator[tuple[str, ...]]:
    """Slice an ordered list of canonical key texts into statement-sized
    chunks, dividing the placeholder budget by the key's WIDTH so a
    two-column key sends half as many rows per statement rather than twice as
    many placeholders (SQLITE_MAX_VARIABLE_NUMBER is 999 on older builds).

    The batch is also the unit of memory: one batch's rows are read, written
    and dropped before the next is read (see ``table_delta``)."""
    return _batched(tuple(texts), max(1, _ID_BATCH // max(1, width)))


def table_delta(
    source: Any,
    conn: Any,
    *,
    table: str,
    columns: Sequence[str],
    key_columns: Sequence[str],
    changes: Mapping[str, Change],
    notebooks: NotebookState,
    emit_row: Any,
    emit_delete: Any,
) -> TableDelta:
    """Turn one table's compacted window into rows, deletes, notebook
    attribution and file requests, HANDING EACH ONE STRAIGHT TO THE WRITER.

    Streaming rather than returning lists: the package's whole point is that
    one table's window can be large, and buffering its rows would put the
    entire changed set of the busiest table in memory on top of the
    compaction dict. Rows go out in canonical key order -- the keys are sorted
    once, batched in that order, and each batch is read, emitted and dropped
    -- so the file is byte-reproducible without a global sort.

    Five rules this function is the single home of:

    - A final ``upsert`` whose row cannot be read back in this snapshot is
      DOWNGRADED to a delete. It should not happen (the delete that removed
      the row would have been logged too, and would have won the compaction);
      if it does, shipping a delete is the conservative answer -- shipping
      nothing would leave the target holding a row the source no longer has.
      Counted, with a bounded sample of keys, rather than one warning per key.
    - A ``seed_only`` table's deletes are NOT exported at all (§5 "授权只播种"):
      the target owns membership and sharing decisions made after the first
      import, so a source-side revocation must not travel. The same rule
      already keeps import from overwriting those rows.
    - A change attributed to a MIRRORED notebook is dropped and counted: a
      mirror's content came from some other source environment, and
      re-exporting it would launder that environment's rows into this one's
      name and make two environments each other's upstream.
    - Every notebook this table contributes a row OR a delete for is a
      notebook the package DECLARES (``delta.notebooks``), so the caller
      re-sends its ``notebooks`` row. A package that says "delete this chunk"
      without declaring the notebook it belongs to would be describing a
      notebook outside its own scope.
    - The notebook of a deleted ``notebooks`` row goes to
      ``deleted_notebooks`` instead: it is gone, so there is no current state
      to re-send, and it is not a notebook this package carries.
    """
    delta = TableDelta()
    if not changes:
        return delta
    spec = spec_for(table)
    scope = spec.scope
    if scope is None:
        raise SyncExportError(f"{table}: LOCAL table has no export scope")

    # One table's parent-key -> notebook memo, shared by every batch of this
    # table (upserts AND deletes). Without it a table whose rows fan in to a
    # handful of parents -- 100k chunks under 10 sources -- would re-ask the
    # parent chain once per batch, which is two statements per hop per 500
    # rows for an answer that cannot change inside the read snapshot. Scoped
    # to the table and dropped with it: the keyspace is this table's distinct
    # parents, not the database's. Same shape as ``NotebookState``.
    parents: dict[Any, str | None] = {}
    upsert_texts = sorted(
        text for text, change in changes.items()
        if change.operation == OPERATION_UPSERT
    )
    delete_texts = [
        text for text, change in changes.items()
        if change.operation == OPERATION_DELETE
    ]
    for batch in key_batches(upsert_texts, len(key_columns)):
        rows = read_rows_by_key(
            source, conn, table, columns, key_columns,
            [changes[text].key for text in batch],
        )
        gone = [text for text in batch if text not in rows]
        if gone:
            delta.note_downgrade(gone)
            delete_texts.extend(gone)
        present = [text for text in batch if text in rows]
        attribution = _attribute(
            source, conn, table, scope, present, changes, rows, key_columns,
            parents,
        )
        notebooks.prime([value for value in attribution.values() if value])
        for text in present:
            notebook_id = attribution.get(text)
            state = _classify(notebook_id, notebooks, delta)
            if state == MIRROR:
                continue
            row = rows[text]
            emit_row(row)
            delta.rows += 1
            if notebook_id and state == PRESENT:
                delta.notebooks.add(notebook_id)
                delta.files.extend(_file_requests(table, row, notebook_id))
        del rows

    if spec.seed_only:
        return delta.sealed(table)
    for batch in key_batches(sorted(delete_texts), len(key_columns)):
        attribution = _attribute(
            source, conn, table, scope, batch, changes, {}, key_columns, parents
        )
        notebooks.prime([value for value in attribution.values() if value])
        for text in batch:
            change = changes[text]
            notebook_id = attribution.get(text)
            state = _classify(notebook_id, notebooks, delta)
            if state == MIRROR:
                continue
            emit_delete(
                delete_entry(table, change.key, notebook_id, change.parent_key)
            )
            delta.deletes += 1
            if table == "notebooks":
                delta.deleted_notebooks.add(str(change.key[key_columns[0]]))
            elif notebook_id and state == PRESENT:
                delta.notebooks.add(notebook_id)
    return delta.sealed(table)


def _classify(
    notebook_id: str | None, notebooks: NotebookState, delta: TableDelta
) -> str:
    """The state that decides whether one attributed change travels, recording
    the reason on the delta when it does not.

    ``MIRROR`` is the only drop. ``MISSING`` travels -- a window that deleted
    a notebook, or a row whose notebook was deleted with it, must still be
    able to tell the target so -- but it is NOT counted as a notebook this
    package carries: there is nothing left to carry. A row with no notebook at
    all (GLOBAL scope, or a PARENT orphan whose chain is gone) reports
    ``MISSING`` for the same reason."""
    if not notebook_id:
        return MISSING
    state, reason = notebooks.state(notebook_id)
    if state == MIRROR:
        delta.skipped_mirror += 1
        delta.skipped[notebook_id] = reason
    return state


def _attribute(
    source: Any,
    conn: Any,
    table: str,
    scope: Any,
    texts: Sequence[str],
    changes: Mapping[str, Change],
    rows: Mapping[str, Mapping[str, Any]],
    key_columns: Sequence[str],
    parents: dict[Any, str | None],
) -> dict[str, str | None]:
    """``key text -> notebook id`` for ONE batch of this table's window.

    ``rows`` carries the rows read back for an upsert batch and is empty for
    a delete batch -- a delete has no row left to read, so everything has to
    come off the log entry. NOTEBOOK scope reads the notebook off the live row
    when there is one and off the log row otherwise; PARENT scope resolves the
    row's own parent pointer for an upsert and the log's ``parent_key`` for a
    delete, both through one batched ``scope_chain`` walk -- but only for the
    parent keys ``parents`` has not already answered. That memo is the
    table's, not the batch's, so a table whose rows share a few parents pays
    for the chain once rather than once per batch; a parent that resolved to
    ``None`` is remembered as ``None`` and not re-asked either. GLOBAL scope
    has no notebook by definition."""
    if scope.kind is ScopeKind.GLOBAL:
        return {text: None for text in texts}
    if scope.kind is ScopeKind.NOTEBOOK:
        attribution: dict[str, str | None] = {}
        for text in texts:
            change = changes[text]
            row = rows.get(text)
            if row is not None:
                value = row.get(scope.column)
                attribution[text] = str(value) if value else change.notebook_id
                continue
            # ``notebooks``' own scope column IS its key, so a delete of the
            # notebook row can name its notebook even if the log row did not.
            attribution[text] = change.notebook_id or (
                str(change.key[key_columns[0]]) if table == "notebooks" else None
            )
        return attribution
    pointers: dict[str, Any] = {}
    for text in texts:
        row = rows.get(text)
        pointers[text] = (
            row.get(scope.column) if row is not None else changes[text].parent_key
        )
    unknown = [
        value for value in pointers.values() if value and value not in parents
    ]
    if unknown:
        parents.update(resolve_parent_notebooks(source, conn, table, unknown))
    return {
        text: (parents.get(value) if value else None)
        for text, value in pointers.items()
    }


# --------------------------------------------------------------------- files


# The file columns the export's file phase knows how to carry, as
# ``{(table, column): storage subdirectory}``. Cross-checked below against the
# shadow manifest, exactly the way ``import_._STORAGE_PATH_COLUMNS`` is, so a
# newly declared path column on a synced table cannot slip through un-exported
# -- the failure it would otherwise cause is silent and one-sided: the rows
# travel, the bytes they point at do not.
_EXPORTED_PATH_COLUMNS: dict[tuple[str, str], str] = {
    ("sources", "file_path"): NOTEBOOK_FILES_DIR,
}

# The one synced table whose bytes are NOT reachable through a path column.
# ``notebook_assets`` stores them as ``<asset id>.<extension>`` under
# ``storage/assets/<notebook>/`` (``AssetService.path_for``), and the
# extension comes from a mime table this package may not import and must not
# duplicate. So an asset row asks for its id as a STEM and the file phase
# carries whatever ``<id>.*`` is on disk. Registered here, next to the path
# columns, so the two mechanisms are visible in one place rather than one of
# them being an unexplained special case inside a function.
# See export._IMPORT_STAGING_SUFFIX: the suffix an interrupted import leaves
# behind, which neither file phase may treat as notebook content.
_IMPORT_STAGING_SUFFIX = ".sync-tmp"

_STEM_TABLES: dict[str, tuple[str, str]] = {
    "notebook_assets": ("id", ASSET_FILES_DIR),
}


def _check_path_columns_are_covered() -> None:
    declared = {
        (spec.name, column)
        for spec in _SHADOW_MANIFEST.tables
        if spec.name in set(synced_tables())
        for column in spec.path_columns
    }
    missing = sorted(declared - set(_EXPORTED_PATH_COLUMNS))
    extra = sorted(set(_EXPORTED_PATH_COLUMNS) - declared)
    if missing or extra:
        raise RuntimeError(
            "_EXPORTED_PATH_COLUMNS must cover exactly the shadow manifest's "
            f"path columns on synced tables: missing={missing}, stale={extra}. "
            "An incremental package carries only the files its changed rows "
            "point at, so an uncovered path column means those rows travel "
            "without their bytes."
        )


_check_path_columns_are_covered()


def _file_requests(
    table: str, row: Mapping[str, Any], notebook_id: str
) -> list[FileRequest]:
    """The on-disk files ONE upserted row points at.

    Resolution is deferred to the file phase on purpose: it touches the
    filesystem, and the filesystem is not part of the read snapshot (see
    ``export._write_files`` for the same reasoning)."""
    requests: list[FileRequest] = []
    for (owner, column), root in _EXPORTED_PATH_COLUMNS.items():
        if owner != table:
            continue
        value = row.get(column)
        if isinstance(value, str) and value:
            requests.append(FileRequest(root, notebook_id, path=value))
    stem_rule = _STEM_TABLES.get(table)
    if stem_rule is not None:
        column, root = stem_rule
        stem = str(row.get(column) or "")
        if stem:
            requests.append(FileRequest(root, notebook_id, stem=stem))
    return requests


_PACKAGE_DIR_FOR = {
    NOTEBOOK_FILES_DIR: notebook_files_dir,
    ASSET_FILES_DIR: notebook_assets_dir,
}


def resolve_file_requests(
    storage_dir: Path, root_dir: Path, requests: Sequence[FileRequest]
) -> tuple[list[tuple[str, Path]], list[str], list[str]]:
    """``([(package path, source path)], missing, warnings)`` for the file
    phase, de-duplicated and ordered by the package path.

    Runs AFTER the read snapshot closes, so the answers describe the disk as
    the copy will find it.

    A path column's value is normalized the way both backends normalize it --
    ``resolve_path``: absolute as given, relative against ``root_dir`` -- and
    only then asked whether it belongs to its own notebook. Legacy rows
    predating the absolute-path convention are relative, and skipping the
    normalization would send every one of them to ``missing`` while their
    bytes sat right there on disk.

    Anything the phase cannot carry lands in MISSING, not merely in a warning:
    ``missing`` is the manifest field an importer reads to know the package is
    knowingly incomplete, and a row whose bytes did not travel is exactly that
    -- whether the file was absent, the path pointed outside the notebook, or
    an asset id matched nothing. The package-relative name recorded for it is
    the one the IMPORTER will look for (``files/<root>/<notebook>/<basename>``
    -- ``import_._rebase_storage_paths`` re-anchors on the basename), so the
    two sides name the same gap. A name that is not a safe package-relative
    path is not recorded at all, only warned about: it must never reach
    ``checksums.json`` or the manifest.
    """
    found: dict[str, Path] = {}
    missing: list[str] = []
    warnings: list[str] = []
    for request in requests:
        base = Path(storage_dir) / request.root / request.notebook_id
        prefix = _PACKAGE_DIR_FOR[request.root](request.notebook_id)
        if request.path:
            origin = Path(request.path)
            if not origin.is_absolute():
                origin = Path(root_dir) / origin
            try:
                relative = origin.relative_to(base)
            except ValueError:
                _record_gap(
                    missing, warnings, f"{prefix}/{Path(request.path).name}",
                    f"{request.root}/{request.notebook_id}: {request.path!r} does "
                    "not resolve under this notebook's storage root; its bytes "
                    "were not carried",
                )
                continue
            package_path = f"{prefix}/{relative.as_posix()}"
            if not is_safe_relative_path(package_path):
                # ``relative_to`` is LEXICAL: a value like
                # "<base>/../../etc/passwd" is "under" base as text. The
                # package-relative path is what would be written to disk and
                # into checksums.json, so it gets the real check.
                warnings.append(
                    f"{request.root}/{request.notebook_id}: {request.path!r} "
                    "would land outside the package and was not carried"
                )
                continue
            found[package_path] = origin
            continue
        if not is_safe_identifier(request.stem):
            warnings.append(
                f"{request.root}/{request.notebook_id}: {request.stem!r} is not "
                "a safe file name and its bytes were not carried"
            )
            continue
        matched = [
            path
            for path in sorted(base.glob(f"{request.stem}.*"))
            # ``<id>.png.sync-tmp`` matches ``<id>.*`` too. It is an
            # interrupted IMPORT's staging file, not this asset's bytes, and
            # carrying it would ship one environment's crash debris onward
            # (codex T1 review P3).
            if path.is_file() and not path.name.endswith(_IMPORT_STAGING_SUFFIX)
        ]
        if not matched:
            _record_gap(
                missing, warnings, f"{prefix}/{request.stem}",
                f"{request.root}/{request.notebook_id}: no file named "
                f"{request.stem}.* is on disk for this row",
            )
            continue
        for origin in matched:
            package_path = f"{prefix}/{origin.name}"
            if is_safe_relative_path(package_path):
                found[package_path] = origin
            else:
                warnings.append(
                    f"{request.root}/{request.notebook_id}: {origin.name!r} is "
                    "not a safe package path and was not carried"
                )
    return sorted(found.items()), missing, warnings


def _record_gap(
    missing: list[str], warnings: list[str], package_path: str, reason: str
) -> None:
    warnings.append(reason)
    if is_safe_relative_path(package_path):
        missing.append(package_path)

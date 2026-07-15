"""Knowhow-tables PR-1 Task 6 + PR-2+3 Task 3: import/table HTTP orchestration,
the editing API's orchestration layer, and the projection scheduler.

Framework-agnostic like its siblings ``grid_parser.py``/``assets.py`` — every
validation failure raises ``ValueError`` with a friendly Chinese message
(``GridParseError`` is itself a ``ValueError`` subclass, so it flows through
unchanged) rather than ``fastapi.HTTPException``; ``routes.py`` owns
translating that into the file's existing
``except ValueError as exc: raise HTTPException(400, str(exc))`` idiom.

``KnowhowProjector`` (Task 5) is a plain service, deliberately NOT wired onto
the facade/``RepositoryRuntime`` composition root (out of this task's scope —
see the task brief). ``build_projector`` constructs one directly from the
handful of named collaborators its constructor wants, mirroring TWO existing
precedents exactly: Task 5's own test fixture
(``test_knowhow_projection.py::projector``) and ``app/api/deps.py``'s
established "extract a narrow runtime port" pattern
(``repository()._runtime.identity`` etc.) — not a new pattern.

PR-2+3 Task 3 adds three things to this module:
  - the wire-shaping helpers (``to_wire_table``/``to_wire_column``) that
    rename the store's ``role`` field to the wire's ``kind`` field and derive
    the table-level ``anchor_column_id`` — the ONE place this reshaping
    happens, so every route (existing GET/import, new PATCH/POST/DELETE)
    shares it rather than five copies of the same dict-munging;
  - the import/create-table column+anchor_index merging
    (``_columns_with_anchor``), replacing PR-1's per-column ``role`` wire
    with PR-2+3's ``kind`` + a separate ``anchor_index``;
  - ``ProjectionScheduler`` — the debounced, single-flight background
    reprojection scheduler every mutating knowhow endpoint now goes through
    (see its own docstring for the coalescing state machine).
"""
from __future__ import annotations

import json
import threading
import weakref
from typing import Any, Callable

from app.repositories.sqlite.knowhow_store import VALID_KINDS as _STORE_KINDS
from app.services import background_jobs
from app.services.knowhow.grid_parser import ParsedGrid, guess_kinds, parse_grid
from app.services.knowhow.projection import KnowhowProjector

# Legal column KINDS a client may name directly on the wire (PR-2+3 Task 3):
# every store-level kind EXCEPT 'anchor' — the row-title designation is never
# a per-column kind value on the wire (import's anchor_index / PATCH table's
# anchor_column_id are the only ways to name it; create_knowhow_table's own
# "anchor inline" exception is reached only through _columns_with_anchor
# below, never directly from client input). Derived from knowhow_store's own
# VALID_KINDS (not hand-typed) so the two vocabularies cannot drift apart.
VALID_KINDS = _STORE_KINDS - {"anchor"}


def preview_import(filename: str, data: bytes) -> dict:
    """Parse the uploaded grid and return the wire-shaped preview: each
    column's name + guessed kind, the anchor (row-title) suggestion index
    (None when no column name suggests one), the first 5 data rows, and the
    total row count. Never writes anything. Raises GridParseError (a
    ValueError subclass) unchanged on a structurally invalid file."""
    grid = parse_grid(filename, data)
    kinds, anchor_index = guess_kinds(grid.columns)
    return {
        "columns": [
            {"name": name, "guessed_kind": kind}
            for name, kind in zip(grid.columns, kinds)
        ],
        "anchor_suggestion": anchor_index,
        "rows_preview": grid.rows[:5],
        "total_rows": len(grid.rows),
    }


def _columns_with_anchor(columns: list[dict], anchor_index: "int | None") -> list[dict]:
    """Merge a client-supplied ``[{name, kind}]`` list with a separate
    ``anchor_index`` into the store's internal ``[{name, role}]`` shape (the
    ONE place a column's role may be 'anchor' inline — create_knowhow_table's
    own documented exception; see knowhow_store.py). An entry that omits
    ``kind`` entirely defaults to 'attribute' (mirrors PR-1's own
    ``role = column.get("role") or "plain"`` leniency — a wizard/import
    column with no explicit type is a plain content column, not an error).
    Raises ValueError (friendly Chinese) for an explicit-but-illegal kind
    value (this also rejects a client sending ``kind: "anchor"`` directly —
    'anchor' is excluded from VALID_KINDS) or an out-of-range anchor_index.
    Does NOT validate name emptiness/uniqueness or the at-most-one-anchor
    invariant — create_knowhow_table itself does that (and the wire's own
    shape makes ">1 anchor" structurally inexpressible here: anchor_index is
    a single value, never a per-column list)."""
    kinds: list[str] = []
    for column in columns:
        kind = column.get("kind") or "attribute"
        if kind not in VALID_KINDS:
            raise ValueError(f"非法的列类型：{kind!r}")
        kinds.append(kind)
    if anchor_index is not None and not (0 <= anchor_index < len(columns)):
        raise ValueError("行标题列索引超出范围")
    result = [
        {"name": column["name"], "role": kind} for column, kind in zip(columns, kinds)
    ]
    if anchor_index is not None:
        result[anchor_index]["role"] = "anchor"
    return result


def parse_import_columns(
    columns_json: str, grid: ParsedGrid, anchor_index: "int | None" = None
) -> list[dict]:
    """Validate + parse the user-confirmed column/kind mapping (PR-2+3 wire:
    ``columns_json=[{name,kind}]`` + a separate ``anchor_index`` — task
    brief: "columns_json 列数与文件一致...kind 值合法 —— 违规 400 友好中文").
    Raises ValueError (friendly Chinese) for: invalid JSON, an empty/non-list
    payload, a column missing its name, an illegal kind value, an
    out-of-range anchor_index, or a column count that doesn't match the
    file. Duplicate/empty names and the store's own at-most-one-anchor
    invariant are validated downstream by ``KnowhowStore.create_knowhow_table``
    itself (same ValueError contract) — not duplicated here."""
    try:
        columns = json.loads(columns_json)
    except (TypeError, ValueError) as exc:
        raise ValueError("列定义不是合法的 JSON") from exc
    if not isinstance(columns, list) or not columns:
        raise ValueError("列定义不能为空")
    if len(columns) != len(grid.columns):
        raise ValueError(
            f"列定义数量（{len(columns)}）与文件列数（{len(grid.columns)}）不一致"
        )
    for column in columns:
        if not isinstance(column, dict) or not str(column.get("name", "")).strip():
            raise ValueError("列定义缺少列名")
    return _columns_with_anchor(columns, anchor_index)


def build_projector(repo: Any) -> KnowhowProjector:
    """Construct a fresh KnowhowProjector from the facade's runtime. Cheap
    (every collaborator is an already-constructed, process-wide singleton) —
    safe to call once per request/job, mirroring routes.py's own
    ``_asset_service()`` helper for Task 4's AssetService."""
    rt = repo._runtime  # type: ignore[attr-defined]
    return KnowhowProjector(
        settings=repo.settings,
        database=rt.database,
        knowhow=rt.knowhow_store,
        sources=rt.source_store,
        chunks=rt.chunk_store,
        knowledge=rt.knowledge,
        embedding=rt.source_embedding,
        note_model_error=rt.models.note_model_error,
        invalidate_unified_cache=rt.kg_mutations.invalidate_unified_cache,
        mark_unified_dirty=rt.kg_mutations.mark_unified_kg_dirty,
        new_id=rt.seams.new_id,
        now=rt.seams.now,
    )


def get_table_in_notebook(repo: Any, notebook_id: str, table_id: str) -> dict:
    """Fetch a table detail, enforce it belongs to ``notebook_id`` (the
    request's read/write guard only proves the caller can access the
    notebook named in the URL, not that this table_id belongs to it — mirrors
    Task 4's identical ``asset["notebook_id"] != notebook_id`` check for
    notebook_assets), and reshape it to the wire format (``to_wire_table`` —
    every HTTP caller wants the wire shape; internal callers that need the
    raw store dict, like the delete route's ``hidden_source_id`` read, still
    work unchanged since reshaping only touches ``columns``/adds
    ``anchor_column_id``). Raises KeyError uniformly for "never existed" and
    "exists under a different notebook" — the route 404s either way without
    leaking which case it was."""
    table = repo.get_knowhow_table(table_id)
    if table["notebook_id"] != notebook_id:
        raise KeyError(table_id)
    return to_wire_table(table)


def import_table(
    repo: Any,
    notebook_id: str,
    filename: str,
    data: bytes,
    title: str,
    columns_json: str,
    anchor_index: "int | None" = None,
) -> str:
    """Full import orchestration (task brief step 2): parse -> validate ->
    create the table -> insert every row+cell -> bump mutation_seq once for
    the whole batch. Returns the new table_id; the caller (routes.py) is
    responsible for scheduling the background projection job and re-fetching
    the full detail for the response — this function does no HTTP/job
    concerns, only data orchestration, so it stays trivially testable and
    reusable.

    Raises ValueError for: GridParseError (bad file), column-validation
    failures (parse_import_columns), or the store's own name-uniqueness /
    at-most-one-anchor checks (create_knowhow_table) — routes.py's existing
    400 idiom catches all of these uniformly."""
    grid = parse_grid(filename, data)
    columns = parse_import_columns(columns_json, grid, anchor_index)
    table_id = repo.create_knowhow_table(notebook_id, title, "", columns)
    column_ids = [c["id"] for c in repo.get_knowhow_table(table_id)["columns"]]
    for row in grid.rows:
        cells = {column_ids[i]: value for i, value in enumerate(row) if value}
        repo.add_knowhow_row(table_id, cells)
    repo.bump_knowhow_mutation_seq(table_id)
    return table_id


def create_table(
    repo: Any,
    notebook_id: str,
    title: str,
    columns: list[dict],
    anchor_index: "int | None",
) -> str:
    """Wizard backend (PR-2+3 Task 3): create an EMPTY table (no grid/rows) —
    mirrors ``import_table``'s create step minus parsing a file. ``columns``
    is the wire-shaped ``[{name, kind}]`` list; this function merges in
    ``anchor_index`` and validates kind legality (``_columns_with_anchor``)
    before delegating to the store, which itself validates name
    emptiness/uniqueness, the at-most-one-anchor rule, and the table title.
    Deliberately does NOT schedule a reprojection — a brand-new table has
    zero rows/cells, so there is nothing to project yet; the first row/cell
    mutation schedules the table's first real run."""
    merged = _columns_with_anchor(columns, anchor_index)
    return repo.create_knowhow_table(notebook_id, title, "", merged)


# --- wire shaping: store `role` <-> wire `kind`, table-level anchor_column_id
# ---------------------------------------------------------------------------


def to_wire_column(column: dict) -> dict:
    """One column dict (store shape: id/name/role/position) -> wire shape
    (id/name/kind/position). 'anchor' is a perfectly normal OUTPUT value here
    (whichever column currently carries the row-title designation) — it is
    only excluded as an INPUT kind value (see VALID_KINDS/_columns_with_anchor
    above)."""
    return {
        "id": column["id"],
        "name": column["name"],
        "kind": column["role"],
        "position": column["position"],
    }


def _anchor_column_id(columns: list[dict]) -> "str | None":
    return next((column["id"] for column in columns if column["role"] == "anchor"), None)


def to_wire_table(table: dict) -> dict:
    """Reshape a store ``get_knowhow_table``-shaped dict into the
    ``KnowhowTableDetail`` wire shape: renames every column's ``role`` to
    ``kind`` and adds the table-level ``anchor_column_id`` (derived from
    whichever column, if any, carries ``role == 'anchor'``). Rows pass
    through unchanged (they carry no role/kind field of their own); every
    other table-level key (hidden_source_id, mutation_seq, timestamps, ...)
    passes through unchanged too."""
    columns = table.get("columns", [])
    return {
        **table,
        "columns": [to_wire_column(column) for column in columns],
        "anchor_column_id": _anchor_column_id(columns),
    }


# --- PR-2+3 Task 3: debounced single-flight projection scheduler ------------


class ProjectionScheduler:
    """Per-table debounced, single-flight background reprojection scheduler
    (task brief: "per-table pending/running/rerun 三态...0.5s 防抖合并...
    发现绝不 seq 门控"). Every editing/import/reproject endpoint calls
    ``schedule(table_id)`` instead of launching ``background_jobs.submit``
    directly, so rapid successive edits to the same table collapse into as
    few actual ``project_table`` runs as this state machine guarantees —
    ``project_table`` itself is ALWAYS the full deterministic pass (never a
    seq-gated/partial variant) whenever it does run; the scheduler only
    decides WHEN/HOW OFTEN to call it, never gates on mutation_seq itself.

    Per-table state:
      - a pending ``threading.Timer`` debouncing rapid ``schedule()`` calls
        (``_debounce`` seconds of quiet before the timer actually fires and
        attempts to submit a run — repeated calls keep resetting it, so N
        rapid calls submit at most once, shortly after the LAST one);
      - ``_running``: a ``project_table`` call is in flight for this table
        right now (guards against launching a second, overlapping run);
      - ``_rerun``: ``schedule()`` was called again WHILE a run was already
        in flight — that edit may not be reflected in the run already under
        way, so ``_run``'s ``finally`` immediately fires once more after the
        in-flight run completes.
    """

    DEBOUNCE_SECONDS = 0.5

    def __init__(
        self,
        project_fn: Callable[[str], None],
        *,
        debounce_seconds: "float | None" = None,
        submit: Callable[..., Any] = background_jobs.submit,
    ) -> None:
        self._project_fn = project_fn
        self._debounce = (
            self.DEBOUNCE_SECONDS if debounce_seconds is None else debounce_seconds
        )
        self._submit = submit
        self._lock = threading.Lock()
        self._timers: dict[str, threading.Timer] = {}
        self._running: set[str] = set()
        self._rerun: set[str] = set()

    def schedule(self, table_id: str) -> None:
        """(Re)start the debounce timer for this table, cancelling whatever
        timer was already pending. Only once ``_debounce`` seconds elapse
        with no further ``schedule()`` call does the timer fire (``_fire``)
        and attempt to submit a run."""
        with self._lock:
            pending = self._timers.get(table_id)
            if pending is not None:
                pending.cancel()
            timer = threading.Timer(self._debounce, self._fire, args=(table_id,))
            timer.daemon = True
            self._timers[table_id] = timer
            timer.start()

    def _fire(self, table_id: str) -> None:
        with self._lock:
            self._timers.pop(table_id, None)
            if table_id in self._running:
                # Already projecting this table: don't start a concurrent
                # second run, just remember to run once more once this one
                # finishes (covers whatever changed during the in-flight
                # run).
                self._rerun.add(table_id)
                return
            self._running.add(table_id)
        self._submit(
            self._run,
            table_id,
            name=f"knowhow-project-{table_id}",
            notify_pending=True,
        )

    def _run(self, table_id: str) -> None:
        try:
            self._project_fn(table_id)
        finally:
            rerun = False
            with self._lock:
                self._running.discard(table_id)
                if table_id in self._rerun:
                    self._rerun.discard(table_id)
                    rerun = True
            if rerun:
                self._fire(table_id)


# One scheduler per repository INSTANCE, not a single bare module global:
# tests get a fresh SQLiteRepository every test (backend/tests/conftest.py's
# autouse ``_reset_singleton_caches`` fixture clears ``app.api.deps.repository``'s
# ``lru_cache`` before/after each test), so a scheduler tied to a stale
# repo/db from a PREVIOUS test must never leak into the next one. Keying the
# cache by the repo object's own identity — a ``WeakKeyDictionary``, so a
# garbage-collected repo doesn't pin its scheduler forever either — gives
# each fresh repo instance its own scheduler with a clean per-table state
# machine: the same "process-level singleton" the task brief describes,
# scoped correctly for test isolation (the production process only ever
# constructs ONE repo, via ``app.api.deps.repository``'s own ``lru_cache``,
# so in production this is indistinguishable from a bare module-global
# singleton).
_SCHEDULERS: "weakref.WeakKeyDictionary[Any, ProjectionScheduler]" = weakref.WeakKeyDictionary()
_SCHEDULERS_LOCK = threading.Lock()


def get_scheduler(repo: Any) -> ProjectionScheduler:
    scheduler = _SCHEDULERS.get(repo)
    if scheduler is not None:
        return scheduler
    with _SCHEDULERS_LOCK:
        scheduler = _SCHEDULERS.get(repo)
        if scheduler is None:
            scheduler = ProjectionScheduler(
                lambda table_id: build_projector(repo).project_table(table_id)
            )
            _SCHEDULERS[repo] = scheduler
        return scheduler


__all__ = [
    "VALID_KINDS",
    "preview_import",
    "parse_import_columns",
    "build_projector",
    "get_table_in_notebook",
    "import_table",
    "create_table",
    "to_wire_column",
    "to_wire_table",
    "ProjectionScheduler",
    "get_scheduler",
]

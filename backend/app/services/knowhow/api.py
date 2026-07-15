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
# singleton).  For the WeakKeyDictionary to actually deliver that promise,
# the VALUE must not strongly reference the KEY: the scheduler's project_fn
# holds only a ``weakref.ref(repo)`` (resolved per run, no-op once the repo
# is collected) — a plain ``lambda: build_projector(repo)...`` closure would
# pin the key alive through its own value and the entry would never die
# (see test_knowhow_projection.py::test_get_scheduler_entry_does_not_pin_repo).
_SCHEDULERS: "weakref.WeakKeyDictionary[Any, ProjectionScheduler]" = weakref.WeakKeyDictionary()
_SCHEDULERS_LOCK = threading.Lock()


def get_scheduler(repo: Any) -> ProjectionScheduler:
    scheduler = _SCHEDULERS.get(repo)
    if scheduler is not None:
        return scheduler
    with _SCHEDULERS_LOCK:
        scheduler = _SCHEDULERS.get(repo)
        if scheduler is None:
            repo_ref = weakref.ref(repo)

            def _project(table_id: str) -> None:
                target = repo_ref()
                if target is None:
                    return  # repo already collected — nothing to project into
                build_projector(target).project_table(table_id)

            scheduler = ProjectionScheduler(_project)
            _SCHEDULERS[repo] = scheduler
        return scheduler


# --- PR-2+3 Task 6: Excel template round-trip (design doc §② 路B) ----------
#
# Template download (build_template_xlsx) and append import (preview_append/
# commit_append) share one alignment core (_align_rows_to_table_columns):
# match the uploaded grid's header BY COLUMN NAME against the TARGET table's
# own (already-created) columns — never by position, unlike import_table's
# brand-new-table wire (which has no pre-existing schema to align against).
# Both raise ValueError (GridParseError, a subclass, included) on a
# structurally bad file, flowing through routes.py's existing 400 idiom
# unchanged, exactly like every other function in this module.
#
# Deliberately appended here, AFTER build_projector/ProjectionScheduler/
# get_scheduler rather than interleaved earlier in the file: this file's
# lines 133-152 (build_projector's body) are EXACT-LINE pinned by
# test_repository_callers_static.py's INDEPENDENT_PRIVATE_SITES (`_runtime`
# at line 138) and test_repository_surface_manifest.py's
# TASK6_KNOWHOW_ALLOWED_CONSUMERS (`_runtime`/`settings` at lines 138/140 —
# from the PRE-EXISTING PR-1 Task 6, a different task under the same number;
# see that constant's own docstring). Inserting anything ABOVE those lines
# would shift them and break both guards; appending at EOF is the zero-risk
# option (task-3-report-pr23.md documents the identical reasoning for why
# ITS OWN test-file registration went to EOF instead of interleaved).


_TEMPLATE_KIND_HINTS: dict[str, str] = {
    # Copied verbatim from the frontend's single source of truth
    # (frontend/app/knowhow-manage-logic.ts KIND_HINTS/ANCHOR_SET_HINT,
    # frontend/app/knowhow-model.ts ROLE_LABELS) so the xlsx template and the
    # in-app column-kind legend never drift apart in wording.
    "anchor": "行标题：用作每行的标题；设置后每行作为一个节点进入知识图谱，节点名取自该列",
    "procedure": "方法步骤：写做法/流程的列，自动识别有序步骤",
    "entity": "工具/事物：列出的名称自动归并：工具、命令、文档等",
    "attribute": "普通：仅作为内容参与检索",
}


def build_template_xlsx(table: dict) -> bytes:
    """Build the downloadable xlsx template for filling in new rows offline
    (design doc §② 路B "按当前表头生成 xlsx 模板下载"): row 1 = the table's
    current column names (bold + light fill — a visual "this is the header,
    not data" cue), row 2 = a one-line kind hint per column — including the
    row-title annotation for whichever column is currently the table's
    anchor column, since ``to_wire_table`` already surfaces THAT column's own
    ``kind`` as the literal string ``"anchor"`` (see ``to_wire_column`` — no
    separate anchor_column_id lookup needed here). Both rows are frozen
    (``freeze_panes = "A3"``) so they stay visible while the user scrolls
    through however many rows they fill in below.

    Deliberately does NOT enable xlsx cell/sheet protection: openpyxl's
    ``SheetProtection`` flags mirror OOXML's own inverted-boolean attributes
    (e.g. ``formatCells=True`` means "protected", i.e. disallowed) — easy to
    get backwards, not meaningfully verifiable through an openpyxl-only test
    (enforcement is entirely up to the Excel client, never the file's own
    metadata), and a wrong guess risks locking the user out of legitimate
    operations (inserting an extra row, widening a column). The task brief's
    "锁定" is delivered as a visual affordance only — a deliberate scope cut,
    not an oversight (see the task report).

    ``table`` is the wire-shaped detail (``to_wire_table`` output — columns
    carry ``kind``, never the store's ``role``); column order follows
    ``table["columns"]`` (already position-ordered by ``get_knowhow_table``).
    Round-trips losslessly through ``grid_parser.parse_grid`` (same column
    names, same order) — which is exactly why ``preview_append``/
    ``commit_append`` below match the re-uploaded file BY COLUMN NAME rather
    than assuming a fixed row/column offset: the hint row and however much
    data the user fills in both come back to that parser as ordinary data
    rows, no different from any other grid upload."""
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    columns = table.get("columns", [])
    wb = Workbook()
    ws = wb.active

    header_font = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="DDEBF7")
    hint_font = Font(italic=True, size=9, color="FF666666")
    hint_alignment = Alignment(wrap_text=True, vertical="top")

    for index, column in enumerate(columns, start=1):
        name_cell = ws.cell(row=1, column=index, value=column["name"])
        name_cell.font = header_font
        name_cell.fill = header_fill

        hint_cell = ws.cell(
            row=2, column=index, value=_TEMPLATE_KIND_HINTS.get(column.get("kind"), "")
        )
        hint_cell.font = hint_font
        hint_cell.alignment = hint_alignment

        letter = get_column_letter(index)
        ws.column_dimensions[letter].width = min(40, max(16, len(column["name"]) * 2 + 4))

    ws.row_dimensions[2].height = 32
    ws.freeze_panes = "A3"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _align_rows_to_table_columns(
    table_columns: list[dict], grid: ParsedGrid
) -> "tuple[list[list[str]], list[str]]":
    """Shared alignment core for both append preview and commit (design doc
    §② 路B: "按表头名匹配列...未识别/缺失列报告"): reshapes the uploaded
    grid's rows into the TARGET TABLE's own column order, matching BY NAME.
    A table column absent from the file contributes ``""`` for every row
    (missing = blank, never an error); a file column absent from the table is
    dropped from the aligned rows and surfaced separately as
    ``unmatched_columns`` (in the file's own column order) so nothing is ever
    silently discarded without being reported (design doc §⑦: "模板上传列不
    匹配给差异报告而非静默丢弃").

    Returns ``(aligned_rows, unmatched_columns)`` where ``aligned_rows[i][j]``
    is the value for ``table_columns[j]`` in the file's data row ``i``."""
    file_index = {name: position for position, name in enumerate(grid.columns)}
    table_names = {column["name"] for column in table_columns}
    unmatched_columns = [name for name in grid.columns if name not in table_names]
    aligned_rows = [
        [
            row[file_index[column["name"]]] if column["name"] in file_index else ""
            for column in table_columns
        ]
        for row in grid.rows
    ]
    return aligned_rows, unmatched_columns


def preview_append(table: dict, filename: str, data: bytes) -> dict:
    """Append preview (design doc §② 路B + task brief): parses the uploaded
    file and aligns it to the TABLE's existing columns by name (see
    ``_align_rows_to_table_columns``) — never writes anything, only reports
    what a subsequent ``commit_append`` call WOULD do.

    ``duplicate_titles`` is populated ONLY when the table has a row-title
    (anchor) column (design doc: "仅设行标题列时按其值对比现有行标") — an
    anchor-less ("record-shaped") table has no notion of a row "title" to
    compare, so it is unconditionally ``[]`` there. When there IS an anchor
    column, EVERY incoming row (the full file, not just the 5-row preview
    window below — a naming collision past row 5 is exactly as worth
    surfacing) whose aligned, stripped anchor-column value is non-empty AND
    matches an EXISTING row's own (stripped) anchor value is flagged — a
    blank title never counts as a "duplicate" of another blank title (that
    would just be preview noise, not a real naming collision). Deliberately
    compares only against EXISTING rows, never row-vs-row WITHIN the same
    incoming batch — the design doc's own wording ("对比现有行标") scopes it
    that way. ``row_index`` is the 0-based position within the file's OWN
    data rows (``grid.rows``), the same "index is 0-based" convention this
    module already uses for ``anchor_index`` elsewhere."""
    grid = parse_grid(filename, data)
    table_columns = table.get("columns", [])
    aligned_rows, unmatched_columns = _align_rows_to_table_columns(table_columns, grid)

    duplicate_titles: list[dict] = []
    anchor_position = next(
        (i for i, column in enumerate(table_columns) if column["kind"] == "anchor"), None
    )
    if anchor_position is not None:
        anchor_column_id = table_columns[anchor_position]["id"]
        existing_titles = {
            (row["cells"].get(anchor_column_id, "") or "").strip()
            for row in table.get("rows", [])
        }
        existing_titles.discard("")
        for row_index, aligned_row in enumerate(aligned_rows):
            title = aligned_row[anchor_position].strip()
            if title and title in existing_titles:
                duplicate_titles.append({"row_index": row_index, "title": title})

    return {
        "rows_preview": aligned_rows[:5],
        "total_rows": len(grid.rows),
        "unmatched_columns": unmatched_columns,
        "duplicate_titles": duplicate_titles,
    }


def commit_append(
    repo: Any, table_id: str, table: dict, filename: str, data: bytes
) -> int:
    """Append commit (design doc §② 路B "确认后追加导入"): re-parses and
    re-aligns the file (deliberately not trusting a client-supplied preview
    payload — the file itself stays the single source of truth, and this
    keeps commit callable on its own without ever having called preview
    first), then inserts one row per file data row via the store's existing
    ``add_knowhow_row`` (position omitted -> always appends, mirroring
    ``import_table``'s own per-row loop), skipping empty cells (mirrors
    ``import_table``'s ``if value`` filter — a blank/missing-column cell is
    simply absent, never an empty-string placeholder, matching
    ``get_knowhow_table``'s "no cell row = never edited" contract) — then
    bumps ``mutation_seq`` ONCE for the whole batch (not per row), exactly
    like ``import_table``. Does NOT itself call the scheduler — routes.py
    does that after this returns, exactly like every other mutating knowhow
    endpoint. Returns the number of rows added (never raises for duplicate
    titles or unmatched columns — those are ``preview_append``'s advisory-
    only warnings; by the time a client calls commit, the human has already
    decided to proceed, design doc: "确认后追加导入")."""
    grid = parse_grid(filename, data)
    table_columns = table.get("columns", [])
    aligned_rows, _unmatched_columns = _align_rows_to_table_columns(table_columns, grid)
    column_ids = [column["id"] for column in table_columns]
    for aligned_row in aligned_rows:
        cells = {column_ids[i]: value for i, value in enumerate(aligned_row) if value}
        repo.add_knowhow_row(table_id, cells)
    repo.bump_knowhow_mutation_seq(table_id)
    return len(aligned_rows)


# --- PR-2+3 Task 8: LLM cell rewrite (design doc §③, explicit trigger only) -
#
# This is the feature's ONLY new LLM call (design doc: "绝不自动触发" — no
# automatic projection/import/reproject path ever calls this). Suggestion-
# only: optimize_cell never writes to the store itself and never schedules a
# reprojection — the caller (routes.py) hands the suggestion back for an
# original/suggested side-by-side preview, and the user's own explicit
# accept re-uses the EXISTING PATCH cell endpoint to write it (that endpoint,
# not this one, is what schedules reprojection).
#
# Appended here, after commit_append/before __all__, for the identical zero-
# line-shift reason documented in the Task 6 section above: build_projector's
# `_runtime`/`settings` reaches are pinned at their own exact lines in
# test_repository_callers_static.py/test_repository_surface_manifest.py;
# inserting anything ABOVE them would shift those pins. This section needs
# its OWN, SECOND `_runtime` reach (repo._runtime.models, to resolve the
# per-user rewrite LLM client + note_model_error) — registered as a new,
# separate entry in both guards (see task-8-report-pr23.md), not folded into
# the existing build_projector registration.


class KnowhowOptimizeUnavailable(RuntimeError):
    """Raised when the rewrite LLM is reached but the call itself fails
    (network/timeout/malformed response) — as opposed to simply not being
    configured (``ModelNotConfiguredError``, mapped to 400 by routes.py just
    like every other validation ``ValueError``) or the cell being empty
    (plain ``ValueError``, also 400). routes.py maps THIS one to 502."""


_OPTIMIZE_SCHEMA_HINT = '{"suggestion_md": ""}'


def _optimize_cell_prompt(content_md: str, column_name: str, kind: str) -> str:
    """Fixed prompt template (design doc §③): preserve meaning/language, tidy
    structure/wording, keep ``asset://`` image refs verbatim, reply with ONLY
    the rewritten markdown body — no explanation, no fences. The ordered-
    list-of-steps clause is included iff this column's kind is ``procedure``
    (design doc: "markdown 列表化步骤"); the caller resolves ``kind`` from the
    store (``to_wire_column``'s own value) before calling this."""
    procedure_clause = (
        "- 这一列的内容类型是「方法步骤」，请将改写结果整理为有序 markdown 列表"
        "（1. 2. 3. ...），每个步骤单独一行。\n"
        if kind == "procedure"
        else ""
    )
    return (
        f"你是表格型知识库的编辑助手。下面是「{column_name}」列某个格子的当前内容，"
        "请在完全保持原意和原语言的前提下，规整表达的结构与措辞，使其更清晰、更专业。\n"
        "规则：\n"
        "- 保持原意与原语言，不得翻译成其他语言，不得编造原文没有的信息。\n"
        f"{procedure_clause}"
        "- 若原文包含形如 `![说明](asset://...)` 的图片引用，必须原样保留，不得改写、"
        "删除或挪动位置。\n"
        "- 只输出重写后的 markdown 正文本身，不要添加任何解释、前后缀、标题或代码"
        "围栏。\n\n"
        f"当前内容：\n{content_md}\n\n"
        '严格按此 JSON 格式返回：{"suggestion_md": "<重写后的 markdown 正文>"}'
    )


def optimize_cell(repo: Any, content_md: str, column_name: str, kind: str) -> str:
    """LLM 表达优化（design doc §③）：格子浮窗「优化表达」/行详情抽屉「优化整行」
    的共享后端。**显式触发、suggestion-only、绝不写库**——回填走既有 PATCH cell
    端点（那才触发重投影）。是本特性唯一的新增 LLM 调用。

    走 notebook 既有 LLM 配置（``repo._runtime.models.rewrite_llm_client``，
    已含 per-user 模型配置解析——不同用户可各自配置改写模型），``cap_kwargs``
    复用全局生成 max_tokens 上限（不新增专属预算旋钮——效率一等约束）。失败经
    ``repo._runtime.models.note_model_error`` 走既有 model_error 可观测链路
    （events.jsonl；本调用不在 ask 上下文内，L2 sink 不适用，仅 L1 emit）。

    Raises ``ValueError`` for an empty cell (400, "格子为空，无需优化") and,
    from inside the try below, for a well-formed-but-empty LLM reply (folds
    into the generic-failure path since ``except Exception`` below re-wraps
    it); ``ModelNotConfiguredError`` when the resolved client isn't configured
    (400, same friendly message the ``.configured`` pre-check itself raises);
    ``KnowhowOptimizeUnavailable`` for every other failure (network/timeout/
    bad response) AFTER logging it via ``note_model_error`` (502) — routes.py
    maps each to its own status code."""
    from app.core.llm import cap_kwargs
    from app.services.model_config import ModelNotConfiguredError

    if not content_md.strip():
        raise ValueError("格子为空，无需优化")
    models = repo._runtime.models  # type: ignore[attr-defined]
    client = models.rewrite_llm_client
    if not client.configured:
        raise ModelNotConfiguredError("尚未配置模型，无法优化表达")
    model_label = getattr(client, "model", "") or ""
    try:
        raw = client.chat_json(
            [{"role": "user", "content": _optimize_cell_prompt(content_md, column_name, kind)}],
            _OPTIMIZE_SCHEMA_HINT,
            **cap_kwargs(client, "openai_compat_max_tokens"),
        )
        data = json.loads(raw)
        suggestion = str(data.get("suggestion_md", "")).strip() if isinstance(data, dict) else ""
        if not suggestion:
            raise ValueError("模型未返回有效的重写结果")
        return suggestion
    except ModelNotConfiguredError:
        raise
    except Exception as exc:
        models.note_model_error("knowhow_optimize", model_label, exc)
        raise KnowhowOptimizeUnavailable("优化服务暂时不可用，请稍后再试") from exc


# --- PR-2+3 Task 10: Agent surface (HTTP+MCP shared core, design doc §⑥) ----
#
# Every function below is a PURE transform over an already-fetched wire-shaped
# table dict (``to_wire_table`` output) plus, where needed, an already-fetched
# code-attachment list — the ROUTE (knowhow_agent_routes.py) and the MCP tools
# (mcp_server.py) each do their OWN repo reads (needed anyway for their own
# notebook-membership auth check) and hand the result in here, so HTTP and MCP
# share 100% of this module's logic and never re-diverge on shape/rules. The
# three cell-code CRUD functions are the one exception (they DO take ``repo``
# directly) — a PUT must read the CURRENT cell text and write a new hash in
# the same call, which cannot be expressed as a pure pre-fetched-dict
# transform.
#
# ``hashlib``/``textops`` are imported HERE (not hoisted to the file's own
# top-of-module import block) for the same zero-line-shift reason documented
# above build_template_xlsx/optimize_cell: build_projector's body is EXACT-
# LINE pinned (lines 133-152) by test_repository_callers_static.py's
# INDEPENDENT_PRIVATE_SITES and test_repository_surface_manifest.py's
# TASK6_KNOWHOW_ALLOWED_CONSUMERS; inserting anything above those lines
# (including a top-of-file import) would shift them and break both guards.
import hashlib

from app.services.knowhow import textops


def cell_net_text(content_md: "str | None") -> str:
    """A cell's net (markup-stripped) display text. MUST mirror
    ``KnowhowProjector._write_elements``'s own ``cell_nets`` formula EXACTLY
    (``textops.strip_images(cells.get(column['id'], '')).strip()`` — see
    ``projection.py``'s ``project_table`` loop) so the agent surface's
    displayed text and ``cell_content_hash``'s basis never diverge from what
    the projector itself computed for the SAME cell. Divergence here would
    silently corrupt every code-attachment freshness derivation (design doc
    §⑥-4) into false-stale or false-implemented."""
    return textops.strip_images(content_md or "").strip()


def cell_content_hash(content_md: "str | None") -> str:
    """sha256 hex of a cell's net text — MUST mirror
    ``KnowhowProjector._write_elements``'s own ``content_hash`` formula
    exactly (``hashlib.sha256(text.encode('utf-8')).hexdigest()`` where
    ``text`` is ``cell_net_text``'s own output), so a code attachment's
    stored ``cell_content_hash`` (computed via THIS function at PUT time)
    compares correctly against the CURRENT cell hash at read time."""
    return hashlib.sha256(cell_net_text(content_md).encode("utf-8")).hexdigest()


def _cell_nets(columns: list[dict], cells: dict[str, str]) -> dict[str, str]:
    return {column["id"]: cell_net_text(cells.get(column["id"], "")) for column in columns}


def _row_title(
    anchor_column_id: "str | None", columns: list[dict],
    cell_nets: dict[str, str], position: int,
) -> str:
    """Mirrors ``projection.py``'s private ``_resolve_row_title`` (design doc
    §① "行标题自动合成") as a deliberate, documented TWIN rather than a
    cross-module reach-in onto that function's own leading-underscore name —
    projection.py is not in this task's file list, and both twins are built
    from the exact same public ``textops`` primitives (``node_name``/
    ``compose_row_title``), so there is nothing project-specific left to
    import: the anchor cell's own ``node_name`` when the table has an anchor
    column AND this row's anchor cell has content; otherwise a synthesized
    ``textops.compose_row_title``; ``"行{position+1}"`` when every cell is
    empty."""
    if anchor_column_id:
        anchor_text = cell_nets.get(anchor_column_id, "")
        if anchor_text:
            return textops.node_name(anchor_text)
    composed = textops.compose_row_title([cell_nets[c["id"]] for c in columns])
    return composed or f"行{position + 1}"


def _code_status(
    cells: dict[str, str], column_id: str, attachment: "dict | None",
) -> str:
    """Design doc §⑥-4's three-state freshness derivation: no attachment ->
    ``none``; attachment's stored hash matches the cell's CURRENT hash ->
    ``implemented``; anything else (the cell changed since the attachment was
    saved) -> ``stale``. Never persisted — always recomputed at read time."""
    if attachment is None:
        return "none"
    current_hash = cell_content_hash(cells.get(column_id, ""))
    return "implemented" if attachment["cell_content_hash"] == current_hash else "stale"


def _find_by_id(items: list[dict], item_id: str) -> "dict | None":
    return next((item for item in items if item["id"] == item_id), None)


def agent_table_summary(table: dict) -> dict:
    """One ``to_wire_table``-shaped table -> the agent tables-list shape
    (task brief: "概要+列(kind)+行数+anchor_column_id")."""
    return {
        "id": table["id"],
        "title": table["title"],
        "description": table.get("description") or "",
        "row_count": len(table.get("rows", [])),
        "anchor_column_id": table.get("anchor_column_id"),
        "columns": [
            {"id": column["id"], "name": column["name"], "kind": column["kind"]}
            for column in table["columns"]
        ],
    }


def list_tables_for_agent(repo: Any, notebook_id: str) -> list[dict]:
    """Agent-surface tables-list (design doc §⑥-1 / task brief): every
    column's kind + row_count + anchor_column_id per table — richer than
    ``list_knowhow_tables``'s own cheap batched-count summary (title/
    description/row_count only), so this re-fetches each table's FULL detail
    (``to_wire_table``) rather than reusing that summary. A notebook's
    knowhow table COUNT is small (a handful, not hundreds — design doc's
    "百行内" scale ceiling is per-TABLE row count, not table count), so one
    extra SELECT per table here is not a real cost."""
    summaries = repo.list_knowhow_tables(notebook_id)
    return [
        agent_table_summary(to_wire_table(repo.get_knowhow_table(summary["id"])))
        for summary in summaries
    ]


def build_discrimination_set(table: dict, code_attachments: list[dict]) -> dict:
    """Pure transform: ``table`` is a ``to_wire_table``-shaped detail already
    scoped to the right notebook by the caller; ``code_attachments`` is that
    table's full ``list_knowhow_cell_code`` result. Design doc §⑥-2/§⑥-4 +
    task brief: every row's title + its procedure-kind columns' net text +
    per-method code_status, in column order; raises ``ValueError`` (friendly
    Chinese, routes.py's existing 400 idiom) for a table with no anchor
    (row-title) column — a "记录型" table has no row identity to discriminate
    by."""
    anchor_column_id = table.get("anchor_column_id")
    if not anchor_column_id:
        raise ValueError("该表未设置行标题列，不支持判别集")
    columns = table["columns"]
    procedure_columns = [column for column in columns if column["kind"] == "procedure"]
    code_by_row_column = {
        (attachment["row_id"], attachment["column_id"]): attachment
        for attachment in code_attachments
    }
    rows_out = []
    for row in table["rows"]:
        cells = row["cells"]
        cell_nets = _cell_nets(columns, cells)
        title = _row_title(anchor_column_id, columns, cell_nets, row["position"])
        methods = [
            {
                "column_id": column["id"],
                "column_name": column["name"],
                "text": cell_nets[column["id"]],
                "code_status": _code_status(
                    cells, column["id"],
                    code_by_row_column.get((row["id"], column["id"])),
                ),
            }
            for column in procedure_columns
        ]
        rows_out.append({"row_id": row["id"], "title": title, "methods": methods})
    return {"rows": rows_out}


def build_row_detail(table: dict, row_id: str, code_attachments: list[dict]) -> dict:
    """Pure transform: ``table`` is a ``to_wire_table``-shaped detail already
    scoped to the right notebook; ``code_attachments`` is that table's full
    ``list_knowhow_cell_code`` result (filtered to this row below). Design doc
    §⑥-3/§⑥-4 + task brief: every column's kind/net-text (+ ``steps`` for a
    procedure column, ``items`` for an entity column), plus this row's
    existing code attachments (never a synthetic 'none' placeholder entry —
    absence from ``code`` IS the 'none' state). Raises ``KeyError`` if
    ``row_id`` is not one of ``table``'s rows."""
    row = _find_by_id(table["rows"], row_id)
    if row is None:
        raise KeyError(row_id)
    columns = table["columns"]
    cells_raw = row["cells"]
    cell_nets = _cell_nets(columns, cells_raw)
    title = _row_title(table.get("anchor_column_id"), columns, cell_nets, row["position"])
    cells = []
    for column in columns:
        text = cell_nets[column["id"]]
        entry = {
            "column_id": column["id"], "column_name": column["name"],
            "kind": column["kind"], "text": text,
        }
        if column["kind"] == "procedure":
            entry["steps"] = textops.parse_steps(text)
        elif column["kind"] == "entity":
            entry["items"] = textops.split_tools(text)
        cells.append(entry)
    code = [
        {
            "column_id": attachment["column_id"],
            "language": attachment["language"],
            "code_text": attachment["code_text"],
            "status": _code_status(cells_raw, attachment["column_id"], attachment),
            "updated_at": attachment["updated_at"],
            "updated_by": attachment["updated_by"],
        }
        for attachment in code_attachments
        if attachment["row_id"] == row_id
    ]
    return {"title": title, "cells": cells, "code": code}


def cell_code_view(
    cells: dict[str, str], column_id: str, attachment: "dict | None",
) -> dict:
    """Shared response shape for GET/PUT's single-cell code endpoint (design
    doc §⑥-4): the three-state freshness derivation plus the attachment's own
    fields, or an all-``None``/``'none'`` shape when no attachment exists yet
    (a legitimate 200, never a 404 — "no code yet" is a normal state, not an
    error)."""
    if attachment is None:
        return {"code_text": None, "language": None, "status": "none", "updated_at": None}
    return {
        "code_text": attachment["code_text"],
        "language": attachment["language"],
        "status": _code_status(cells, column_id, attachment),
        "updated_at": attachment["updated_at"],
    }


def get_cell_code(repo: Any, row_id: str, column_id: str) -> dict:
    """GET .../cells/{col}/code service core. Raises ``KeyError`` for an
    unknown row, ``ValueError`` ("格子定位不合法") for a column that does not
    belong to this row's table (``validate_cell_target``'s own contract) —
    routes.py's existing 400 idiom for the latter, matching every other
    knowhow cell-scoped endpoint (PATCH cell, optimize)."""
    location = repo.get_knowhow_row_location(row_id)
    if location is None:
        raise KeyError(row_id)
    repo.validate_cell_target(row_id, column_id)
    table = repo.get_knowhow_table(location["table_id"])
    row = _find_by_id(table["rows"], row_id)
    if row is None:
        raise KeyError(row_id)
    attachment = repo.get_knowhow_cell_code(row_id, column_id)
    return cell_code_view(row["cells"], column_id, attachment)


def put_cell_code(
    repo: Any, row_id: str, column_id: str, code_text: str, language: str,
    updated_by: str,
) -> dict:
    """PUT .../cells/{col}/code service core (design doc §⑥-4): computes and
    stores ``cell_content_hash`` at save time from the CURRENT cell's net
    text (never the possibly-stale last-projected element hash — the
    projector's background debounce means the two can transiently differ
    right after an edit). Raises ``ValueError`` for blank ``code_text``
    ("代码内容不能为空") or a cross-table (row, column) pair
    ("格子定位不合法"), ``KeyError`` for an unknown row."""
    if not str(code_text or "").strip():
        raise ValueError("代码内容不能为空")
    location = repo.get_knowhow_row_location(row_id)
    if location is None:
        raise KeyError(row_id)
    repo.validate_cell_target(row_id, column_id)
    table = repo.get_knowhow_table(location["table_id"])
    row = _find_by_id(table["rows"], row_id)
    if row is None:
        raise KeyError(row_id)
    content_hash = cell_content_hash(row["cells"].get(column_id, ""))
    repo.upsert_knowhow_cell_code(
        row_id, column_id, code_text, language or "", updated_by, content_hash,
    )
    attachment = repo.get_knowhow_cell_code(row_id, column_id)
    return cell_code_view(row["cells"], column_id, attachment)


def delete_cell_code(repo: Any, row_id: str, column_id: str) -> None:
    """DELETE .../cells/{col}/code service core. Idempotent (the store's own
    delete is a silent no-op when nothing exists to delete) — but an unknown
    ROW or a cross-table (row, column) pair still fails loud (``KeyError``/
    ``ValueError``), consistent with every other cell-scoped endpoint's
    validation, rather than silently no-op-ing an address that was never
    valid in the first place."""
    location = repo.get_knowhow_row_location(row_id)
    if location is None:
        raise KeyError(row_id)
    repo.validate_cell_target(row_id, column_id)
    repo.delete_knowhow_cell_code(row_id, column_id)


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
    "build_template_xlsx",
    "preview_append",
    "commit_append",
    "KnowhowOptimizeUnavailable",
    "optimize_cell",
    "cell_net_text",
    "cell_content_hash",
    "agent_table_summary",
    "list_tables_for_agent",
    "build_discrimination_set",
    "build_row_detail",
    "cell_code_view",
    "get_cell_code",
    "put_cell_code",
    "delete_cell_code",
]

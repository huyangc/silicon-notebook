"""Knowhow-tables PR-1 Task 6: import/table HTTP orchestration.

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
"""
from __future__ import annotations

import json
from typing import Any

from app.services.knowhow.grid_parser import ParsedGrid, guess_roles, parse_grid
from app.services.knowhow.projection import KnowhowProjector

# Legal column kinds (PR-2+3 Task 1): the post-migration-17 behavior-kind
# vocabulary (anchor allowed at most once — enforced downstream by
# create_knowhow_table itself). Task 3 rewires the wire to kind+anchor_index.
VALID_ROLES = {"anchor", "procedure", "entity", "attribute"}


def preview_import(filename: str, data: bytes) -> dict:
    """Parse the uploaded grid and return the wire-shaped preview: each
    column's name + guessed role, the first 5 data rows, and the total row
    count. Never writes anything. Raises GridParseError (a ValueError
    subclass) unchanged on a structurally invalid file."""
    grid = parse_grid(filename, data)
    roles = guess_roles(grid.columns)
    return {
        "columns": [
            {"name": name, "guessed_role": role}
            for name, role in zip(grid.columns, roles)
        ],
        "rows_preview": grid.rows[:5],
        "total_rows": len(grid.rows),
    }


def parse_import_columns(columns_json: str, grid: ParsedGrid) -> list[dict]:
    """Validate + parse the user-confirmed column/role mapping against the
    parsed grid (task brief: "columns_json 列数与文件一致、concept 恰一列、
    role 值合法 —— 违规 400 友好中文"). Raises ValueError (friendly Chinese)
    for: invalid JSON, an empty/non-list payload, a column missing its name,
    an illegal role value, or a column count that doesn't match the file.
    Duplicate/empty names and "exactly one concept column" are validated
    downstream by ``KnowhowStore.create_knowhow_table`` itself (same
    ValueError contract) — not duplicated here."""
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
    parsed: list[dict] = []
    for column in columns:
        if not isinstance(column, dict) or not str(column.get("name", "")).strip():
            raise ValueError("列定义缺少列名")
        role = column.get("role") or "plain"
        if role not in VALID_ROLES:
            raise ValueError(f"不支持的列角色：{role!r}")
        parsed.append({"name": column["name"], "role": role})
    return parsed


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
    """Fetch a table detail and enforce it belongs to ``notebook_id``: the
    request's read/write guard only proves the caller can access the
    notebook named in the URL, not that this table_id belongs to it (mirrors
    Task 4's identical ``asset["notebook_id"] != notebook_id`` check for
    notebook_assets). Raises KeyError uniformly for "never existed" and
    "exists under a different notebook" — the route 404s either way without
    leaking which case it was."""
    table = repo.get_knowhow_table(table_id)
    if table["notebook_id"] != notebook_id:
        raise KeyError(table_id)
    return table


def import_table(
    repo: Any, notebook_id: str, filename: str, data: bytes, title: str, columns_json: str
) -> str:
    """Full import orchestration (task brief step 2): parse -> validate ->
    create the table -> insert every row+cell -> bump mutation_seq once for
    the whole batch. Returns the new table_id; the caller (routes.py) is
    responsible for launching the background projection job and re-fetching
    the full detail for the response — this function does no HTTP/job
    concerns, only data orchestration, so it stays trivially testable and
    reusable.

    Raises ValueError for: GridParseError (bad file), column-validation
    failures (parse_import_columns), or the store's own name-uniqueness /
    concept-count-exactly-one checks (create_knowhow_table) — routes.py's
    existing 400 idiom catches all of these uniformly."""
    grid = parse_grid(filename, data)
    columns = parse_import_columns(columns_json, grid)
    table_id = repo.create_knowhow_table(notebook_id, title, "", columns)
    column_ids = [c["id"] for c in repo.get_knowhow_table(table_id)["columns"]]
    for row in grid.rows:
        cells = {column_ids[i]: value for i, value in enumerate(row) if value}
        repo.add_knowhow_row(table_id, cells)
    repo.bump_knowhow_mutation_seq(table_id)
    return table_id


__all__ = [
    "VALID_ROLES",
    "preview_import",
    "parse_import_columns",
    "build_projector",
    "get_table_in_notebook",
    "import_table",
]

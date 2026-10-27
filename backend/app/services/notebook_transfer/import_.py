"""Import one ``.snbk`` archive as a new, independent notebook.

The user's rulings (docs/superpowers/plans/2026-10-22-notebook-export-import.md
§0, §9): an import is a fresh notebook like a deep copy -- new ids, the
importer owns it, the same file may be imported any number of times, and it is
never a ``sync_origin`` mirror. Memory never travels and no mount edge is
carried. Vectors are compared per embedding workload by ``(model, dim)``: kept
byte for byte when this deployment binds the same model, dropped (and embedded
again after publish) otherwise. The knowledge graph travels as it is.

One run (``NotebookImportService.import_notebook``):

1. **Preflight**, before any write: the archive's directory, manifest, every
   sha256 and row count (``archive_reader.inspect_open_archive``); the column
   names against this database's tables (``check_columns``); the carried file
   paths against case-insensitive / Unicode-normalising collisions
   (``check_file_paths``); exactly one notebook row; the concept seed space of
   the carried clusters. The vector verdicts and the deployment-wide object types
   are decided here too.
2. **Write.** First the hidden sentinel alone -- ``status='importing'``,
   ``sync_origin=''``, ``updated_at`` its heartbeat -- committed before anything
   else, so every file and row the import writes afterwards belongs to a row the
   stale-import sweep finds. Then the notebook's files, then the rest of
   ``NotebookCopyWriter`` with the import profile (``notebook_copy_writer.
   import_profile``: strict references, the importer as owner and editor,
   unfinished sources failed, per-batch FTS) reading ``ArchiveRowSource`` --
   each table streamed page by page from the zip and re-hashed on every pass --
   then the custom object types this deployment lacks, as the notebook's own.
   The heartbeat is beaten (at most every ``NOTEBOOK_IMPORT_STALE_SECONDS / 4``,
   capped at 30 s) on every page read, every file written, between question
   re-embedding batches and around validation.
3. **Validate** (``validate_import``: the rows written, table by table, and no
   reference leaving the notebook) and **publish** (the sentinel flips to the
   exported status).
4. **After publish**: every Knowhow table is projected again synchronously
   (``project_table``), so the structural graph exists before anything embeds.
   The report names the re-embedding legs the caller runs next
   (``notebook_reembed``); the scale index is built after those (the CLI:
   ``scale_rebuild.rebuild_after_import``; a web job: ``maybe_auto_index`` once
   its re-embedding run has finished).

Any failure before publish compensates the target only: a notebook within the
copy limits is deleted in one transaction (its children cascade); a larger one
is handed to the batched delete job (``request_delete_from_import``), and both
directories this run created are removed. A process that dies mid-import leaves
its sentinel, whose heartbeat stops; ``sweep_stale_imports`` (server startup)
hands every sentinel quiet for ``NOTEBOOK_IMPORT_STALE_SECONDS`` to the delete
job.

**Object types.** The source's *effective* definition of each type its graph
uses wins wherever this deployment has none: a deployment-wide custom type the
manifest lists (``global_object_types``) and this deployment lacks becomes the
notebook's own ``source='custom'`` schema row; a local ``proposed`` row of that
type (review-only: the source kept resolving the deployment-wide definition) is
replaced by it (``proposals_superseded``). A type this deployment already
defines keeps this deployment's definition; an active or disabled local row
shadows the deployment-wide one and is kept as it is; a deployment-wide type
whose own status was ``proposed`` was never effective in the source and is not
created (``skipped_proposed``).

**Concept seed space.** The clusters keep placing new concepts correctly only
in the seed space they were built in: the manifest's ``kg.concept_seed_version``
is recorded on the notebook's KG state row (``_COPY_SEED_SPACE``, as a deep copy
does). An archive from before that key existed carries clusters of an unknown
space: the notebook starts marked for a rebuild and the report says so
(``kg_notes``).

``import_notebook`` is safe to call on a background thread: it holds no global
state, reports progress through its ``progress`` callback and beats the
heartbeat while it writes.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import shutil
import struct
import time
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from app.core.config import Settings
from app.core.database_url import database_identity
from app.domain.kg_merge_seed import CONCEPT_SEED_VERSION, check_concept_seed_version
from app.domain.notebook_archive import (
    ARCHIVE_TABLES,
    EMBEDDING_WORKLOADS,
    VECTOR_TABLES,
    ArchiveManifest,
    EmbeddingIdentity,
)
from app.domain.portable_rows import is_safe_identifier
from app.domain.repository import RepositoryCompatibilitySeams
from app.domain.vector_index import encode_vector
from app.services.knowhow.assets import ALLOWED_MIME_EXTENSIONS
from app.services.notebook_copy_writer import (
    CopyRowRejected,
    NotebookCopyRun,
    NotebookCopyWriter,
    VectorPolicy,
    import_profile,
)
from app.services.notebook_reembed import (
    LEG_CHUNKS,
    LEG_ELEMENTS,
    LEG_KG_OBJECTS,
    LEG_KG_RELATIONS,
    LEGS,
)
from app.services.notebook_transfer.archive_reader import (
    ASSET_FILES_PREFIX,
    NOTEBOOK_FILES_PREFIX,
    ArchiveError,
    ArchiveRowSource,
    check_columns,
    check_file_paths,
    copy_file_entry,
    inspect_open_archive,
    open_archive,
)

_log = logging.getLogger("silicon_notebook.notebook_import")

#: Statuses an imported notebook is never published with (they hide a row).
_HIDDEN_STATUSES = frozenset({"copying", "deleting", "importing", "pending_delete"})
#: A schema's status an imported custom object type keeps; anything else is active.
_SCHEMA_STATUSES = frozenset({"active", "disabled"})
#: Where a workload's vectors live (``chunk_embeddings`` holds two workloads).
_WORKLOAD_TABLE = {
    "chunk_embedding": "chunk_embeddings",
    "knowhow_embedding": "chunk_embeddings",
    "source_element_embedding": "element_embeddings",
    "knowledge_object_embedding": "knowledge_embeddings",
    "relation_embedding": "relation_embeddings",
}
_VECTOR_COLUMN_TABLES = frozenset(VECTOR_TABLES) | {"chunk_questions"}
#: Write-open of an extracted file: a new file only, never through a link.
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
#: Opaque, so a file's content never reaches an operator's terminal.
_INCOMPLETE = "文件内容不完整：引用了文件中不存在的行，或某行缺少必需的列"


class NotebookImportError(RuntimeError):
    """The import did not happen and nothing of it remains in this deployment.
    The message is for the operator and never quotes archive content."""


@dataclasses.dataclass(frozen=True)
class ImportProgress:
    """One progress event: ``stage`` is ``preflight``, ``files``, ``rows``,
    ``validate``, ``publish``, ``project`` or ``done``; ``table`` and ``rows``
    (rows written to it so far) accompany ``rows`` events, ``rows_total`` is
    the archive's row count over every table."""

    stage: str
    table: str = ""
    rows: int = 0
    rows_total: int = 0


@dataclasses.dataclass(frozen=True)
class VectorVerdict:
    """One embedding workload: ``kept`` when the archive's vectors were made by
    the model this deployment binds (same model, same dimension). ``reason``:
    ``same_model``, ``not_included`` (exported without vectors),
    ``unbound_in_archive``, ``unbound_here``, ``model_differs``, ``dim_differs``."""

    kept: bool
    reason: str
    archive: "EmbeddingIdentity | None"
    target: "EmbeddingIdentity | None"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kept": self.kept,
            "reason": self.reason,
            "archive": None if self.archive is None else dataclasses.asdict(self.archive),
            "target": None if self.target is None else dataclasses.asdict(self.target),
        }


@dataclasses.dataclass(frozen=True)
class ImportReport:
    #: The new notebook's id; ``""`` on a dry run, which writes nothing.
    notebook_id: str
    name: str
    owner_id: str
    dry_run: bool
    #: The exported notebook's id and name (reference only).
    source_notebook: Mapping[str, str]
    #: Rows written per table (a dry run: rows that would be).
    rows: Mapping[str, int]
    #: Files written into the notebook's two trees (a dry run: files carried).
    files: int
    mount_edges_dropped: int
    #: ``<table>.<column>[.<key>]`` -> ids inside stored values that named no
    #: row of the archive, cleared.
    cleared: Mapping[str, int]
    #: ``<table>.<reason>`` -> rows not written.
    dropped: Mapping[str, int]
    unresolved_asset_refs: int
    sources_reset: int
    vectors_included: bool
    vectors: Mapping[str, VectorVerdict]
    #: Legs of ``notebook_reembed`` this notebook needs, in run order.
    reembed_legs: tuple[str, ...]
    chunk_questions: Mapping[str, Any]
    #: ``added``: deployment-wide custom types created as this notebook's own
    #: (``proposals_superseded``: of those, the ones replacing a local proposal);
    #: ``existing``: already defined here (this deployment's definition wins);
    #: ``local``: an active/disabled local row defines the type itself;
    #: ``skipped_proposed``: deployment-wide types that were only proposed.
    global_object_types: Mapping[str, tuple[str, ...]]
    kg_starts_dirty: bool
    #: The manifest's ``kg.concept_seed_version`` (``None``: absent).
    kg_concept_seed_version: "int | None"
    #: ``seed_space_unknown``: clusters from an archive without a seed space;
    #: the notebook starts marked for a rebuild.
    kg_notes: tuple[str, ...]
    knowhow_tables: tuple[str, ...]
    #: Knowhow tables whose post-publish projection failed (logged; the table
    #: has a manual rebuild).
    projection_failures: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = dataclasses.asdict(self)
        result["vectors"] = {key: verdict.to_dict() for key, verdict in self.vectors.items()}
        result["reembed_legs"] = list(self.reembed_legs)
        result["knowhow_tables"] = list(self.knowhow_tables)
        result["projection_failures"] = list(self.projection_failures)
        result["kg_notes"] = list(self.kg_notes)
        result["global_object_types"] = {
            key: list(value) for key, value in self.global_object_types.items()
        }
        return result


def vector_verdicts(
    manifest: ArchiveManifest,
    target: Mapping[str, "EmbeddingIdentity | None"],
    *,
    dim: int,
) -> dict[str, VectorVerdict]:
    """Per workload, whether the archive's vectors can be kept here (module
    docstring). ``dim`` is this deployment's ``EMBED_DIM``."""
    verdicts: dict[str, VectorVerdict] = {}
    for workload in EMBEDDING_WORKLOADS:
        archived, here = manifest.embeddings.get(workload), target.get(workload)
        if not manifest.vectors_included:
            reason = "not_included"
        elif archived is None:
            reason = "unbound_in_archive"
        elif here is None:
            reason = "unbound_here"
        elif archived.model != here.model:
            reason = "model_differs"
        elif archived.dim != here.dim or archived.dim != dim:
            reason = "dim_differs"
        else:
            reason = "same_model"
        verdicts[workload] = VectorVerdict(
            kept=reason == "same_model", reason=reason, archive=archived, target=here
        )
    return verdicts


# ------------------------------------------------------------ value shapes


def _legacy_vector(value: Any) -> Any:
    """A SQLite legacy vector (JSON text of numbers) as float32 bytes, the
    shape every vector is written in now; anything else as it is."""
    if not isinstance(value, str):
        return value
    try:
        numbers = json.loads(value)
    except (ValueError, RecursionError):
        return value
    if not isinstance(numbers, list) or not all(
        isinstance(item, (int, float)) and not isinstance(item, bool)
        and math.isfinite(float(item))
        for item in numbers
    ):
        return value
    return struct.pack(f"<{len(numbers)}f", *numbers)


#: Deepest nesting a stored JSON value may have (real payloads nest a few levels).
MAX_JSON_DEPTH = 64


def _check_json_text(text: str, column: str) -> None:
    """``text`` is JSON nesting at most ``MAX_JSON_DEPTH`` levels, or
    ``ArchiveError``. Parsed once by ``json`` (linear, and its recursion is
    guarded: a value nested past the interpreter's limit raises
    ``RecursionError`` instead of exhausting the stack), then measured without
    recursion -- so no later parser (the writer's, the driver's, PostgreSQL's
    ``jsonb``) ever sees a value nested deeper than the limit. A text-level
    bracket count would need a string-literal regex, and every such regex
    backtracks per character in CPython's engine (an 8 MB string cost ~1 GB)."""
    try:
        value = json.loads(text)
    except RecursionError:
        raise ArchiveError(f"{column} 的 JSON 嵌套过深") from None
    except ValueError:
        raise ArchiveError(f"{column} 不是合法的 JSON") from None
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, dict):
            children: Iterable[Any] = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        if depth > MAX_JSON_DEPTH:
            raise ArchiveError(f"{column} 的 JSON 嵌套过深")
        pending.extend((child, depth + 1) for child in children)


class _TargetValues:
    """An archive row (SQLite shape) in the target backend's value shapes,
    decided per column from the PostgreSQL DDL contract (the export's own
    classification). Both backends: a legacy JSON vector becomes float32
    bytes. PostgreSQL: timestamps go through the shadow migration's SQLite ->
    PostgreSQL conversion (``app.migration.shadow.transform``: a naive value is
    UTC by convention, ``""`` lands as NULL where it is a sentinel), and the
    ``""`` empty-list sentinel of a JSON column becomes ``[]``. SQLite: a NULL
    timestamp that is ``TEXT NOT NULL DEFAULT ''`` here lands as ``""``.

    Every JSON column's text is parsed once here, before any writer or driver
    parses it: a value that is not text (the exporter writes text or NULL),
    text that is not JSON, or JSON nesting deeper than ``MAX_JSON_DEPTH`` (a
    crafted value that would exhaust the recursion of every later parser)
    refuses the file (``ArchiveError``)."""

    def __init__(self, backend: str) -> None:
        from app.migration.shadow.postgres_catalog import EXPECTED_COLUMNS
        from app.repositories.postgres.schema_manifest import (
            POSTGRES_EMPTY_TIME_SENTINELS,
        )

        self._postgres = backend == "postgresql"
        self._json_list_sentinels = frozenset({
            "notebooks.expected_questions", "notebooks.source_types", "notebooks.taxonomy",
        })
        stamps = {
            table: {
                name: column
                for name, column in (EXPECTED_COLUMNS.get(table) or {}).items()
                if column.data_type == "timestamp with time zone"
            }
            for table in ARCHIVE_TABLES
        }
        self._json_columns = {
            table: frozenset(
                name for name, column in (EXPECTED_COLUMNS.get(table) or {}).items()
                if column.data_type == "jsonb"
            )
            for table in ARCHIVE_TABLES
        }
        #: SQLite: the timestamp columns that are ``TEXT NOT NULL DEFAULT ''``.
        self._empty_text = {
            table: frozenset(
                name for name in columns if f"{table}.{name}" in POSTGRES_EMPTY_TIME_SENTINELS
            )
            for table, columns in stamps.items()
        }
        #: PostgreSQL: ``(table spec, column)`` per timestamp column, for the shadow
        #: conversion.
        self._postgres_stamps: dict[str, tuple[Any, tuple[Any, ...]]] = {}
        if self._postgres:
            from app.migration.shadow.manifest import MANIFEST
            from app.migration.shadow.transform import PostgresColumn, transform_sqlite_value

            specs = {spec.name: spec for spec in MANIFEST.tables}
            self._transform = transform_sqlite_value
            self._postgres_stamps = {
                table: (specs[table], tuple(
                    PostgresColumn(name=name, data_type=column.data_type,
                                   nullable=column.nullable)
                    for name, column in columns.items()
                ))
                for table, columns in stamps.items()
            }

    def __call__(self, table: str, row: dict) -> dict:
        for name in self._json_columns.get(table, ()):
            value = row.get(name)
            if value is None:
                continue
            if not isinstance(value, str):
                # The exporter writes every JSON column as text (or NULL); an
                # object or array in its place was put there by hand, and would
                # reach a driver unchecked.
                raise ArchiveError(f"{table}.{name} 不是 JSON 文本")
            if value:
                _check_json_text(value, f"{table}.{name}")
        if table in _VECTOR_COLUMN_TABLES and "vector" in row:
            row["vector"] = _legacy_vector(row["vector"])
        if self._postgres:
            return self._postgres_row(table, row)
        for name in self._empty_text.get(table, ()):
            if name in row and row[name] is None:
                row[name] = ""
        return row

    def _postgres_row(self, table: str, row: dict) -> dict:
        spec, columns = self._postgres_stamps[table]
        for column in columns:
            if column.name in row:
                row[column.name] = self._transform(spec, column, row[column.name])
        if table == "notebooks":
            for name, value in row.items():
                if value == "" and f"{table}.{name}" in self._json_list_sentinels:
                    row[name] = "[]"
        return row


# ----------------------------------------------------------- store wrappers


class _ImportStore:
    """The store as the writer sees it during an import: every call reaches the
    real store; rows written are counted per table (``validate_import`` checks
    against them), progress is reported, and the heartbeat is beaten -- from
    the moment the sentinel is committed (before it there is no row to beat).
    ``clock`` is a monotonic seconds reading."""

    def __init__(
        self,
        inner: Any,
        *,
        notebook_id: str,
        heartbeat_seconds: float,
        progress: Callable[[ImportProgress], None],
        rows_total: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._notebook_id = notebook_id
        self._interval = heartbeat_seconds
        self._progress = progress
        self._rows_total = rows_total
        self._clock = clock
        self._armed = False
        self._last_beat = clock()
        self.written: dict[str, int] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def insert_import_sentinel(
        self, row: dict, *, starts_dirty: bool, concept_seed_version: "int | None" = None
    ) -> None:
        self._inner.insert_import_sentinel(
            row, starts_dirty=starts_dirty, concept_seed_version=concept_seed_version
        )
        self.written["notebooks"] = 1
        self._armed = True
        self._last_beat = self._clock()

    def insert_copy_rows(self, table: str, rows, *, chunk_size: int) -> None:
        self._inner.insert_copy_rows(table, rows, chunk_size=chunk_size)
        self.written[table] = self.written.get(table, 0) + len(rows)
        self._progress(ImportProgress(
            stage="rows", table=table, rows=self.written[table], rows_total=self._rows_total,
        ))
        self.beat()

    def beat(self) -> None:
        """Move the sentinel's heartbeat when ``heartbeat_seconds`` have passed
        since the last one; an import whose sentinel was reaped stops here."""
        if not self._armed or self._clock() - self._last_beat < self._interval:
            return
        if not self._inner.touch_import(self._notebook_id):
            raise NotebookImportError("导入中途被收割（心跳超时），本次导入已作废")
        self._last_beat = self._clock()


class _DryRunStore:
    """A dry run's store: counts the rows the writer would insert and writes
    nothing (the writer's import path calls only these four methods)."""

    def __init__(self) -> None:
        self.written: dict[str, int] = {}

    def insert_import_sentinel(
        self, row: dict, *, starts_dirty: bool, concept_seed_version: "int | None" = None
    ) -> None:
        del row, starts_dirty, concept_seed_version
        self.written["notebooks"] = 1

    def insert_copy_rows(self, table: str, rows, *, chunk_size: int) -> None:
        del chunk_size
        self.written[table] = self.written.get(table, 0) + len(rows)

    def insert_fts_rows(self, sql_text: str, rows, *, chunk_size: int) -> None:
        del sql_text, rows, chunk_size

    def seed_copied_knowhow_genesis(self, table_ids, **kwargs) -> None:
        del table_ids, kwargs

    def beat(self) -> None:
        return None


@dataclasses.dataclass
class _ImportTrees:
    """The two directories an import creates; each flag is set the moment its
    directory exists, so the compensation removes exactly those."""

    notebook_dir: Path
    assets_dir: Path
    notebook_created: bool = False
    assets_created: bool = False
    files_written: int = 0

    def remove(self) -> None:
        if self.notebook_created:
            shutil.rmtree(self.notebook_dir, ignore_errors=True)
        if self.assets_created:
            shutil.rmtree(self.assets_dir, ignore_errors=True)


@dataclasses.dataclass(frozen=True)
class _Preflight:
    manifest: ArchiveManifest
    files: Mapping[str, tuple[int, str]]
    notebook_row: Mapping[str, Any]
    verdicts: Mapping[str, VectorVerdict]
    new_object_types: tuple[Mapping[str, Any], ...]
    existing_object_types: tuple[str, ...]
    local_object_types: tuple[str, ...]
    superseded_proposals: tuple[str, ...]
    skipped_proposed_types: tuple[str, ...]
    #: The seed space to record (``_seed_space``) and the report's KG notes.
    concept_seed_version: "int | None"
    kg_notes: tuple[str, ...]

    @property
    def starts_dirty(self) -> bool:
        return self.manifest.kg_starts_dirty or "seed_space_unknown" in self.kg_notes


def _require_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ArchiveError("global_object_types 的字段类型不符")
    return value


def _check_served_values(source: ArchiveRowSource) -> None:
    """Values a serving path trusts because this deployment only ever persists
    them in one shape, refused (``ArchiveError``) when an archive carries any
    other: an attachment's MIME type is one of the image types the asset routes
    serve inline (``ALLOWED_MIME_EXTENSIONS``) -- a hand-made archive could
    otherwise have this origin serve HTML or SVG -- and a source's link, which
    the source list renders as a link and a re-parse downloads, is an http(s)
    URL (what URL import accepts)."""
    for batch in source.batches("notebook_assets"):
        for row in batch:
            mime = row.get("mime")
            if not isinstance(mime, str) or mime not in ALLOWED_MIME_EXTENSIONS:
                raise ArchiveError("附件的 MIME 类型不在允许的图片类型之内")
    for batch in source.batches("sources"):
        for row in batch:
            url = row.get("source_url")
            if url not in (None, "") and not _is_web_url(url):
                raise ArchiveError("来源链接不是 http(s) 地址")


def _is_web_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)


def _require_text_list(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ArchiveError("global_object_types 的字段类型不符")
    return value


class NotebookImportService:
    """Imports ``.snbk`` archives into this deployment (module docstring).

    Collaborators are late-bound callables where the facade owns them:
    ``target_embeddings`` (this deployment's embedding identity per workload),
    ``question_embedder`` (the ``chunk_embedding`` embedder, or ``None`` when
    unbound), ``object_types`` (the deployment-wide registry's type names),
    ``project_table(table_id, embed)`` (one synchronous Knowhow projection)
    and ``delete_runner`` (the delete job runner, ``None`` before it is wired)."""

    def __init__(
        self,
        *,
        store: Any,
        chunks: Any,
        delete_jobs: Any,
        seams: RepositoryCompatibilitySeams,
        settings: Settings,
        storage_dir: Callable[[], Path],
        target_embeddings: Callable[[], Mapping[str, "EmbeddingIdentity | None"]],
        question_embedder: Callable[[], Any],
        object_types: Callable[[], Iterable[str]],
        project_table: Callable[[str, bool], None],
        delete_runner: Callable[[], Any],
    ) -> None:
        self._store = store
        self._chunks = chunks
        self._delete_jobs = delete_jobs
        self._seams = seams
        self._settings = settings
        self._storage_dir = storage_dir
        self._target_embeddings = target_embeddings
        self._question_embedder = question_embedder
        self._object_types = object_types
        self._project_table = project_table
        self._delete_runner = delete_runner
        #: Monotonic seconds, for the heartbeat (tests advance it).
        self._clock: Callable[[], float] = time.monotonic

    # ------------------------------------------------------------- the run
    def import_notebook(
        self,
        path: Path,
        *,
        owner_id: str,
        name: "str | None" = None,
        progress: "Callable[[ImportProgress], None] | None" = None,
        dry_run: bool = False,
    ) -> ImportReport:
        """Import ``path`` for ``owner_id`` (module docstring). ``name`` replaces
        the exported name; ``dry_run`` runs the preflight and the whole row
        mapping (every strict check, every verdict) and writes nothing.

        Raises ``NotebookImportError`` for an archive this deployment refuses
        (invalid, incomplete, written by a newer build) or an import that was
        reaped mid-way; any other exception is a failure of this deployment
        (the database, the disk). Either way nothing of the import remains."""
        report = progress or (lambda event: None)
        new_name = (name or "").strip() or None
        try:
            archive = open_archive(Path(path))
        except ArchiveError as exc:
            raise NotebookImportError(str(exc)) from None
        with archive:
            report(ImportProgress(stage="preflight"))
            try:
                checked = self._preflight(Path(path), archive)
            except ArchiveError as exc:
                raise NotebookImportError(str(exc)) from None
            if dry_run:
                return self._rehearse(archive, checked, owner_id=owner_id, name=new_name)
            return self._run(archive, checked, owner_id=owner_id, name=new_name, report=report)

    def _preflight(self, path: Path, archive: zipfile.ZipFile) -> _Preflight:
        inspection = inspect_open_archive(path, archive)
        manifest = inspection.manifest
        if manifest.tables["notebooks"].rows != 1:
            raise ArchiveError("导出文件必须恰好包含一本笔记本")
        check_columns(manifest, self._store.import_table_columns(ARCHIVE_TABLES))
        check_file_paths(inspection.files)
        source = ArchiveRowSource(archive, manifest, adapt=lambda table, row: row, page_rows=1000)
        _check_served_values(source)
        (notebook_row,) = [row for batch in source.batches("notebooks") for row in batch]
        local: dict[str, set[str]] = {}
        for batch in source.batches("notebook_object_schemas"):
            for row in batch:
                local.setdefault(str(row.get("object_type") or ""), set()).add(
                    str(row.get("status") or "")
                )
        registry = frozenset(self._object_types())
        added, existing, local_types, superseded, skipped = [], [], [], [], []
        listed: set[str] = set()
        for item in manifest.global_object_types:
            object_type = _require_text(item.get("object_type"))
            if object_type in listed:
                raise ArchiveError(f"global_object_types 重复列出了类型 {object_type!r}")
            listed.add(object_type)
            statuses = local.get(object_type, set())
            if statuses - {"proposed"}:
                local_types.append(object_type)  # an active/disabled local row shadows it
            elif object_type in registry:
                existing.append(object_type)  # this deployment's definition wins
            elif item.get("status") == "proposed":
                skipped.append(object_type)  # never effective in the source either
            else:
                added.append(item)
                if statuses:
                    superseded.append(object_type)  # only a proposal: replaced
        seed, notes = self._seed_space(manifest)
        return _Preflight(
            manifest=manifest,
            files=inspection.files,
            notebook_row=notebook_row,
            verdicts=vector_verdicts(
                manifest, self._target_embeddings(), dim=int(self._settings.embed_dim)
            ),
            new_object_types=tuple(added),
            existing_object_types=tuple(existing),
            local_object_types=tuple(local_types),
            superseded_proposals=tuple(superseded),
            skipped_proposed_types=tuple(skipped),
            concept_seed_version=seed,
            kg_notes=notes,
        )

    @staticmethod
    def _seed_space(manifest: ArchiveManifest) -> tuple["int | None", tuple[str, ...]]:
        """The seed space to record for the carried clusters (``None``: the
        default one, or no cluster travels) and the report's KG notes. A forged
        version is refused here; an archive without the key (written before it
        existed) holds clusters of an unknown space: ``seed_space_unknown``,
        and the notebook starts marked for a rebuild."""
        version = manifest.kg_concept_seed_version
        if version is not None:
            try:
                check_concept_seed_version(version)
            except ValueError:
                raise ArchiveError("kg.concept_seed_version 不是已知的概念种子空间") from None
        if not manifest.tables["concept_clusters"].rows or manifest.kg_clusters_dropped:
            return None, ()
        if version is None:
            return None, ("seed_space_unknown",)
        return (None if version == CONCEPT_SEED_VERSION else version), ()

    def _vector_policy(
        self, checked: _Preflight, *, dry_run: bool, beat: Callable[[], None]
    ) -> VectorPolicy:
        dim = int(self._settings.embed_dim)
        batch = max(1, int(self._settings.embed_batch_size))
        reembed = None
        embedder = self._question_embedder()
        if embedder is not None and dry_run:
            # Rehearsal: what would be embedded, without a model call.
            def reembed(texts: list[str]) -> list[bytes]:
                return [bytes(4 * dim) for _ in texts]
        elif embedder is not None:
            def reembed(texts: list[str]) -> list[bytes]:
                # One model call per embed batch, the heartbeat between them.
                vectors: list[bytes] = []
                for start in range(0, len(texts), batch):
                    beat()
                    vectors.extend(
                        encode_vector(vector)
                        for vector in embedder.embed_texts(texts[start:start + batch])
                    )
                beat()
                return vectors
        return VectorPolicy(
            dim=dim,
            kept_workloads=frozenset(
                workload for workload, verdict in checked.verdicts.items() if verdict.kept
            ),
            reembed_questions=reembed,
        )

    def _new_run(self, checked: _Preflight, *, owner_id: str, new_id: str) -> NotebookCopyRun:
        return NotebookCopyRun(
            source_notebook_id=str(checked.manifest.notebook.get("id") or ""),
            source_name=str(checked.notebook_row.get("name") or ""),
            new_owner_id=owner_id,
            new_id=new_id,
            now=self._seams.now(),
            chunk_size=self._seams.copy_chunk_size(),
            destination_dir=self._storage_dir() / "notebooks" / new_id,
        )

    def _writer(
        self, archive, checked: _Preflight, run: NotebookCopyRun,
        store: "_ImportStore | _DryRunStore",
        materialize: Callable[[list[tuple[str, str, str]]], None], *, dry_run: bool,
    ) -> NotebookCopyWriter:
        backend = database_identity(self._settings.database_url).scheme
        beat = store.beat
        return NotebookCopyWriter(
            store=store,
            seams=self._seams,
            rows=ArchiveRowSource(
                archive, checked.manifest, adapt=_TargetValues(backend),
                page_rows=run.chunk_size, on_page=beat,
            ),
            run=run,
            materialize_assets=materialize,
            profile=import_profile(
                starts_dirty=checked.starts_dirty,
                concept_seed_version=checked.concept_seed_version,
                superseded_proposals=frozenset(checked.superseded_proposals),
                vectors=self._vector_policy(checked, dry_run=dry_run, beat=beat),
            ),
        )

    def _rehearse(
        self, archive, checked: _Preflight, *, owner_id: str, name: "str | None"
    ) -> ImportReport:
        store = _DryRunStore()
        run = self._new_run(checked, owner_id=owner_id, new_id="nb-dry-run")
        try:
            self._writer(archive, checked, run, store, lambda files: None, dry_run=True).write(
                new_name=name, actor_label=None
            )
            schema_rows = self._object_type_rows(checked, run)
        except Exception as exc:
            raise _rejection(exc) from None
        store.written["notebook_object_schemas"] = (
            store.written.get("notebook_object_schemas", 0) + len(schema_rows)
        )
        return self._report(
            checked, run, store.written, notebook_id="", name=name, dry_run=True,
            files=len(checked.files),
        )

    def _run(
        self, archive, checked: _Preflight, *, owner_id: str, name: "str | None",
        report: Callable[[ImportProgress], None],
    ) -> ImportReport:
        new_id = self._seams.new_id("nb")
        run = self._new_run(checked, owner_id=owner_id, new_id=new_id)
        trees = _ImportTrees(run.destination_dir, self._storage_dir() / "assets" / new_id)
        store = _ImportStore(
            self._store, notebook_id=new_id,
            heartbeat_seconds=max(5.0, min(30.0, self._settings.notebook_import_stale_seconds / 4)),
            progress=report,
            rows_total=sum(entry.rows for entry in checked.manifest.tables.values()),
            clock=self._clock,
        )
        try:
            writer = self._writer(
                archive, checked, run, store,
                lambda files: self._extract_assets(archive, checked, trees, files, store),
                dry_run=False,
            )
            # The sentinel first, committed on its own: every file below then
            # belongs to a row the stale-import sweep can find.
            writer.write_sentinel(new_name=name)
            report(ImportProgress(stage="files"))
            self._extract_notebook_files(archive, checked, trees, store)
            writer.write_rows(actor_label=None)
            schema_rows = self._object_type_rows(checked, run)
            if schema_rows:
                store.insert_copy_rows(
                    "notebook_object_schemas", schema_rows, chunk_size=run.chunk_size
                )
            report(ImportProgress(stage="validate"))
            store.beat()
            self._store.validate_import(new_id, store.written)
            store.beat()
            if run.question_dropped_chunks:
                self._chunks.clear_question_index_marks(new_id, sorted(run.question_dropped_chunks))
            report(ImportProgress(stage="publish"))
            if not self._store.publish_import(new_id, _published_status(checked)):
                raise NotebookImportError("导入中途被收割（心跳超时），本次导入已作废")
        except BaseException as exc:
            self._compensate(new_id, store.written, trees)
            if isinstance(exc, Exception) and _is_rejection(exc):
                raise _rejection(exc) from None
            raise
        # Past publish nothing may compensate (the copy's rule): a projection
        # failure is logged and reported, the notebook stays.
        report(ImportProgress(stage="project"))
        failures = self._project_knowhow(run, checked)
        result = self._report(
            checked, run, store.written, notebook_id=new_id, name=name, dry_run=False,
            files=trees.files_written, projection_failures=failures,
        )
        report(ImportProgress(stage="done"))
        return result

    # ------------------------------------------------------------- files
    def _extract_notebook_files(
        self, archive, checked: _Preflight, trees: _ImportTrees, store: Any
    ) -> None:
        """``files/notebooks/**`` into the new notebook's tree, each file created
        ``O_EXCL | O_NOFOLLOW`` and re-hashed; every directory is proven to stay
        inside the tree after it is resolved."""
        wanted = sorted(name for name in checked.files if name.startswith(NOTEBOOK_FILES_PREFIX))
        if not wanted:
            return
        trees.notebook_dir.mkdir(parents=True, exist_ok=False)
        trees.notebook_created = True
        root = trees.notebook_dir.resolve()
        for name in wanted:
            target = trees.notebook_dir / name[len(NOTEBOOK_FILES_PREFIX):]
            target.parent.mkdir(parents=True, exist_ok=True)
            parent = target.parent.resolve()
            if parent != root and root not in parent.parents:
                raise ArchiveError("导出文件的文件路径越出了笔记本目录")
            size, sha256 = checked.files[name]
            _write_entry(archive, name, size, sha256, target)
            trees.files_written += 1
            store.beat()

    def _extract_assets(
        self, archive, checked: _Preflight, trees: _ImportTrees,
        batch: list[tuple[str, str, str]], store: Any,
    ) -> None:
        """The writer's ``materialize_assets``: one batch of attachment rows'
        files, ``files/assets/<old id>.<ext>`` -> ``storage/assets/<new
        notebook>/<new id>.<ext>``. An attachment whose file the archive does not
        carry stays without one, as in a copy."""
        trees.assets_dir.mkdir(parents=True, exist_ok=True)
        trees.assets_created = True
        for old_id, new_id, mime in batch:
            ext = ALLOWED_MIME_EXTENSIONS.get(mime, "bin")
            name = f"{ASSET_FILES_PREFIX}{old_id}.{ext}"
            entry = checked.files.get(name)
            if entry is None:
                continue
            leaf = f"{new_id}.{ext}"
            if not is_safe_identifier(leaf):
                raise ArchiveError("附件 id 不合法")
            _write_entry(archive, name, entry[0], entry[1], trees.assets_dir / leaf)
            trees.files_written += 1
            store.beat()

    # ----------------------------------------------------- object types
    def _object_type_rows(self, checked: _Preflight, run: NotebookCopyRun) -> list[dict]:
        """The deployment-wide custom object types the notebook's graph uses and
        this deployment does not define: written as the notebook's own schema
        rows (``source='custom'``)."""
        rows = []
        for item in checked.new_object_types:
            status = item.get("status")
            rows.append({
                "notebook_id": run.new_id,
                "object_type": _require_text(item.get("object_type")),
                "plural": _require_text(item.get("plural", "")),
                "fields": json.dumps(_require_text_list(item.get("fields", [])), ensure_ascii=False),
                "primary_field": _require_text(item.get("primary_field", "")),
                "description": _require_text(item.get("description", "")),
                "label": _require_text(item.get("label", "")),
                "list_fields": json.dumps(
                    _require_text_list(item.get("list_fields", [])), ensure_ascii=False
                ),
                "source": "custom",
                "status": status if status in _SCHEMA_STATUSES else "active",
                "rationale": "",
                "created_by": run.new_owner_id,
                "created_at": run.now,
                "updated_at": run.now,
            })
        return rows

    # ----------------------------------------------------- after publish
    def _project_knowhow(self, run: NotebookCopyRun, checked: _Preflight) -> tuple[str, ...]:
        """Project every imported Knowhow table again, now and in order, so its
        structural graph is rebuilt before the re-embedding legs run (they
        must follow it: the projection rewrites the table's edges, and their
        vectors with them). When the archive's Knowhow vectors were kept the
        pass makes no model call (every chunk is already in place); otherwise
        it embeds nothing (``embed=False``) and the ``chunks`` leg fills them."""
        embed = checked.verdicts["knowhow_embedding"].kept
        failures = []
        for table_id in run.khtbl_map.values():
            try:
                self._project_table(table_id, embed)
            except Exception as exc:  # noqa: BLE001 -- the published notebook must survive it
                # Ids and the exception class only: a traceback or message can
                # carry cell content, database values or private paths.
                _log.warning(
                    "notebook import: %s 的 knowhow 表 %s 投影失败（%s；笔记本已导入，"
                    "可在表内手动重建投影）",
                    run.new_id, table_id, type(exc).__name__,
                )
                failures.append(table_id)
        return tuple(failures)

    # ------------------------------------------------------ compensation
    def _compensate(self, notebook_id: str, written: Mapping[str, int], trees: _ImportTrees) -> None:
        """Remove what this import wrote (module docstring). Never raises: the
        import's own error is what the caller must see; a sentinel left behind
        is reaped by ``sweep_stale_imports``."""
        settings = self._settings
        large = sum(written.values()) > settings.notebook_copy_max_snapshot_rows or (
            written.get("chunks", 0) + written.get("knowledge_objects", 0)
            > settings.notebook_copy_max_rows
        )
        try:
            if large:
                job = self._delete_jobs.request_delete_from_import(notebook_id)
                runner = self._delete_runner()
                if job is not None and runner is not None:
                    runner.run(job["id"])
            else:
                self._store.compensate_import(notebook_id)
        except Exception as exc:  # noqa: BLE001 -- the import's own failure is re-raised
            _log.error(
                "notebook import: 补偿 %s 失败（%s）；残留的导入哨兵会在启动时被收割",
                notebook_id, type(exc).__name__,
            )
        finally:
            trees.remove()

    def sweep_stale_imports(self) -> int:
        """Hand every import sentinel whose heartbeat stopped
        ``NOTEBOOK_IMPORT_STALE_SECONDS`` ago (its process died) to the delete
        job, which removes its rows and both of its directories. Returns how
        many were handed over. Server startup runs it; a sync mirror is never
        among them (``sync_origin`` is not empty).

        The race it accepts: a live import beats at most every
        ``min(30 s, NOTEBOOK_IMPORT_STALE_SECONDS / 4)`` -- on every page it
        reads, every file it writes, between question-embedding batches and
        around validation -- so it is reaped only if ONE of those steps (a
        single statement, a single file, a single model call) runs longer than
        ``NOTEBOOK_IMPORT_STALE_SECONDS``. Then its next beat finds the sentinel
        gone (``touch_import`` answers ``False``) and it stops with a
        ``NotebookImportError``; the delete job removes what it had written. The
        CAS in ``request_delete_from_import`` re-checks staleness, so a
        sentinel whose heartbeat moved after the listing is left alone."""
        seconds = float(self._settings.notebook_import_stale_seconds)
        runner = self._delete_runner()
        reaped = 0
        for notebook_id in self._store.stale_import_ids(older_than_seconds=seconds):
            job = self._delete_jobs.request_delete_from_import(notebook_id, stale_seconds=seconds)
            if job is None:
                continue
            reaped += 1
            if runner is not None:
                runner.submit_job(job["id"], notebook_id)
        return reaped

    # ------------------------------------------------------------ report
    def _report(
        self, checked: _Preflight, run: NotebookCopyRun, written: Mapping[str, int], *,
        notebook_id: str, name: "str | None", dry_run: bool, files: int,
        projection_failures: tuple[str, ...] = (),
    ) -> ImportReport:
        return ImportReport(
            notebook_id=notebook_id,
            name=name or run.source_name,
            owner_id=run.new_owner_id,
            dry_run=dry_run,
            source_notebook={
                "id": str(checked.manifest.notebook.get("id") or ""),
                "name": run.source_name,
            },
            rows=dict(sorted(written.items())),
            files=files,
            mount_edges_dropped=run.mount_edges_dropped + checked.manifest.excluded_mount_edges,
            cleared=dict(sorted(run.cleared.items())),
            dropped=dict(sorted(run.dropped.items())),
            unresolved_asset_refs=run.unresolved_asset_refs,
            sources_reset=run.sources_reset,
            vectors_included=checked.manifest.vectors_included,
            vectors=dict(checked.verdicts),
            reembed_legs=reembed_legs(checked.verdicts, run, written),
            chunk_questions={
                **run.questions,
                "marks_cleared": len(run.question_dropped_chunks),
            },
            global_object_types={
                "added": tuple(str(item["object_type"]) for item in checked.new_object_types),
                "existing": checked.existing_object_types,
                "local": checked.local_object_types,
                "proposals_superseded": checked.superseded_proposals,
                "skipped_proposed": checked.skipped_proposed_types,
            },
            kg_starts_dirty=checked.starts_dirty,
            kg_concept_seed_version=checked.manifest.kg_concept_seed_version,
            kg_notes=checked.kg_notes,
            knowhow_tables=tuple(run.khtbl_map.values()),
            projection_failures=projection_failures,
        )


def reembed_legs(
    verdicts: Mapping[str, VectorVerdict], run: NotebookCopyRun, written: Mapping[str, int]
) -> tuple[str, ...]:
    """The ``notebook_reembed`` legs an imported notebook needs (PR3 handoff):
    a workload whose vectors were not kept, or whose rows were dropped for a
    wrong length, needs its leg when the notebook has rows of that kind
    (``knowhow_embedding`` is the Knowhow half of ``chunks``). A notebook with a
    Knowhow table always needs ``chunks``, ``kg_objects`` and ``kg_relations``:
    the post-publish projection tears down and rebuilds the table's objects and
    edges, which carry no vector afterwards (a Knowhow-projected object is
    never copied), and it creates the chunks of every cell the archive carries
    none for (a table never projected at the source) -- without vectors when
    the Knowhow vectors were not kept (``embed=False``). The archive's chunk
    count cannot tell; each leg reads what is still missing from storage, so
    naming it is free when nothing is."""

    def missing(workload: str) -> bool:
        return not verdicts[workload].kept or bool(
            run.dropped.get(f"{_WORKLOAD_TABLE[workload]}.{workload}")
        )

    knowhow_chunks = len(run.knowhow_chunk_ids_old)
    document_chunks = written.get("chunks", 0) - knowhow_chunks
    legs = set()
    if (document_chunks > 0 and missing("chunk_embedding")) or (
        knowhow_chunks > 0 and missing("knowhow_embedding")
    ) or written.get("knowhow_tables", 0):
        legs.add(LEG_CHUNKS)
    if written.get("source_elements", 0) and missing("source_element_embedding"):
        legs.add(LEG_ELEMENTS)
    if (written.get("knowledge_objects", 0) and missing("knowledge_object_embedding")) or (
        written.get("knowhow_tables", 0)
    ):
        legs.add(LEG_KG_OBJECTS)
    if (written.get("knowledge_relations", 0) and missing("relation_embedding")) or (
        written.get("knowhow_tables", 0)
    ):
        legs.add(LEG_KG_RELATIONS)
    return tuple(leg for leg in LEGS if leg in legs)


def _published_status(checked: _Preflight) -> str:
    status = str(checked.notebook_row.get("status") or "")
    return status if status and status not in _HIDDEN_STATUSES else "draft"


def _write_entry(archive, name: str, size: int, sha256: str, target: Path) -> None:
    fd = os.open(target, _CREATE_FLAGS, 0o644)
    try:
        copy_file_entry(archive, name, size, sha256, fd)
    finally:
        os.close(fd)


def _is_rejection(exc: BaseException) -> bool:
    return isinstance(exc, (NotebookImportError, ArchiveError, CopyRowRejected)) or (
        type(exc) in (
            KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError, RecursionError,
        )
    )


def _rejection(exc: Exception) -> Exception:
    """What the caller sees for a failed run: a refusal of the file as a
    ``NotebookImportError`` (the writer's ``KeyError`` on a reference with no
    remap entry, a malformed value), anything else unchanged."""
    if isinstance(exc, NotebookImportError):
        return exc
    if isinstance(exc, ArchiveError):
        return NotebookImportError(str(exc))
    if isinstance(exc, CopyRowRejected):
        return NotebookImportError(f"文件内容不完整：{exc}")
    if _is_rejection(exc):
        return NotebookImportError(_INCOMPLETE)
    return exc

"""Export one notebook to a single ``.snbk`` archive (format:
``app.domain.notebook_archive``).

Read-only against the database: the rows come from the store's streaming copy
read (``SharingStorePort.iter_copy_rows``) -- the deep copy's own closure,
statements and Memory rules (ruling M2: no Memory, nothing derived from one, no
Memory evidence inside kept rows), paged inside one read transaction and NOT
bounded by the copy-size limits, so a notebook too large to copy can still be
exported. Each table goes into its zip entry as it streams; nothing holds more
than one page of rows.

What the export changes on the way out (``_Normalizer``), so that a file this
build wrote passes the importer's strict reference check:

* a notebook row never carries its share link (``share_token``/``is_shared``);
* ``sources.file_path`` becomes the path relative to the notebook's file tree
  (the archive's ``files/notebooks/`` entries), never a host path;
* soft pointers the deep copy passes through unmapped (``map.get(x, x)``) are
  cleared when they point outside the exported closure; an asset row whose
  source is gone is dropped together with its file; a cluster row whose member
  object is not exported is dropped;
* mount edges are not carried at all (an import never mounts); their number is
  recorded in ``manifest.excluded``.

Files are the notebook's ``storage/notebooks/<id>/`` and ``storage/assets/<id>/``
trees, read after the row transaction (as the deep copy does): regular files
only, symbolic links and special files skipped and counted. The archive is
written to ``<out>.part`` and renamed into place only when complete, the
manifest being its last entry; any failure removes the part file.
"""

from __future__ import annotations

import dataclasses
import functools
import os
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol

from app.core.config import Settings
from app.core.database_url import database_identity
from app.domain.notebook_archive import (
    ARCHIVE_TABLES,
    COMPRESSION_RATIO_FLOOR_BYTES,
    EMBEDDING_WORKLOADS,
    FILES_INDEX_NAME,
    FORMAT_VERSION,
    MANIFEST_NAME,
    MAX_ENTRIES,
    MAX_ENTRY_COMPRESSION_RATIO,
    MAX_JSONL_LINE_BYTES,
    MAX_TOTAL_UNCOMPRESSED_BYTES,
    PART_SUFFIX,
    VECTOR_TABLES,
    ArchiveManifest,
    EmbeddingIdentity,
    FilesSummary,
    TableEntry,
    asset_file_entry,
    notebook_file_entry,
    rows_entry,
)
from app.domain.portable_rows import (
    DigestCounter,
    column_encoder,
    is_safe_relative_path,
    json_document,
    json_line,
)
from app.models.knowledge import ObjectSchemaModel
from app.repositories.ports import CopyRowsOutcome, CopyTableConsumer
from app.services.knowhow.assets import ALLOWED_MIME_EXTENSIONS

#: Bytes per read when copying a file into its entry while hashing it.
_COPY_BLOCK = 1 << 20
#: Every entry's timestamp: an archive's bytes depend on its content, not on
#: when its files were last touched (zip cannot store dates before 1980).
_ENTRY_DATE = (1980, 1, 1, 0, 0, 0)
#: Regular file, rw-r--r--, in the zip external attributes' high word.
_ENTRY_MODE = (stat.S_IFREG | 0o644) << 16


class NotebookExportError(RuntimeError):
    """The export could not produce a valid archive. The message is for the
    operator and never carries row content."""


class CopyRowsReader(Protocol):
    """The one store capability an export needs."""

    def iter_copy_rows(
        self,
        notebook_id: str,
        *,
        on_table: CopyTableConsumer,
        skip_tables: frozenset[str] = frozenset(),
    ) -> CopyRowsOutcome: ...


@dataclasses.dataclass(frozen=True)
class ExportReport:
    notebook_id: str
    path: Path
    archive_bytes: int
    rows: Mapping[str, int]
    files: int
    file_bytes: int
    skipped_links: int
    vectors_included: bool
    #: ``<table>.<column>`` -> how many values the export cleared or rows it
    #: dropped because they pointed outside the exported closure.
    normalized: Mapping[str, int]
    #: Sources whose recorded file is not in the notebook's file tree.
    missing_source_files: int

    def to_dict(self) -> dict[str, Any]:
        result = dataclasses.asdict(self)
        result["path"] = str(self.path)
        return result


def embedding_identities(
    settings: Settings, service_for: "Callable[[str], Any] | None" = None
) -> dict[str, "EmbeddingIdentity | None"]:
    """Which model each embedding workload is bound to, as the manifest
    records it: ``{protocol, model, dim: EMBED_DIM, encoding}``, or ``None`` for
    an unbound workload. ``service_for`` resolves a workload to its
    ``ModelServiceDefinition`` (default: the deployment's model registry as
    configured in ``settings``)."""
    if service_for is None:
        from app.services.model_registry import SystemModelServiceRegistry

        service_for = SystemModelServiceRegistry.load(settings).service_for
    identities: dict[str, EmbeddingIdentity | None] = {}
    for workload in EMBEDDING_WORKLOADS:
        service = service_for(workload)
        identities[workload] = (
            None
            if service is None
            else EmbeddingIdentity(
                protocol=str(service.protocol),
                model=str(service.model),
                dim=int(settings.embed_dim),
            )
        )
    return identities


def default_archive_name(notebook_id: str) -> str:
    return f"{notebook_id}.snbk"


def export_notebook(
    rows: CopyRowsReader,
    notebook_id: str,
    out_path: Path,
    *,
    storage_dir: Path,
    settings: Settings,
    embeddings: Mapping[str, "EmbeddingIdentity | None"],
    global_object_schemas: Iterable[ObjectSchemaModel] = (),
    include_vectors: bool = True,
    now: "Callable[[], datetime] | None" = None,
) -> ExportReport:
    """Write ``notebook_id`` to ``out_path`` (see the module docstring).

    Raises ``KeyError`` when the notebook does not exist or is being deleted,
    ``NotebookExportError`` when the output already exists or the archive would
    break a reader limit. The output's directory must exist."""
    out_path = Path(out_path)
    if out_path.exists():
        raise NotebookExportError(f"输出文件已存在: {out_path}")
    if not out_path.parent.is_dir():
        raise NotebookExportError(f"输出目录不存在: {out_path.parent}")
    part = out_path.with_name(out_path.name + PART_SUFFIX)
    writer = _ArchiveWriter(part)
    try:
        report = _write_archive(
            writer, rows, notebook_id, out_path,
            storage_dir=Path(storage_dir), settings=settings, embeddings=embeddings,
            global_object_schemas=tuple(global_object_schemas),
            include_vectors=include_vectors,
            exported_at=(now or _utc_now)().astimezone(timezone.utc).isoformat(),
        )
        writer.close()
        os.replace(part, out_path)
    except BaseException:
        writer.abort()
        raise
    return dataclasses.replace(report, archive_bytes=out_path.stat().st_size)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _write_archive(
    writer: "_ArchiveWriter",
    rows: CopyRowsReader,
    notebook_id: str,
    out_path: Path,
    *,
    storage_dir: Path,
    settings: Settings,
    embeddings: Mapping[str, "EmbeddingIdentity | None"],
    global_object_schemas: tuple[ObjectSchemaModel, ...],
    include_vectors: bool,
    exported_at: str,
) -> ExportReport:
    notebook_root = storage_dir / "notebooks" / notebook_id
    assets_root = storage_dir / "assets" / notebook_id
    normalizer = _Normalizer(notebook_root)
    tables: dict[str, TableEntry] = {}

    def on_table(table: str, pages: Iterator[list[dict]]) -> None:
        tables[table] = writer.write_rows(table, pages, normalizer)

    outcome = rows.iter_copy_rows(
        notebook_id,
        on_table=on_table,
        skip_tables=frozenset() if include_vectors else frozenset(VECTOR_TABLES),
    )
    if tuple(tables) != ARCHIVE_TABLES:
        raise NotebookExportError("复制闭包的表集合与导出格式不符，请升级导出格式版本")
    files = _FileIndex(writer)
    try:
        files.add_tree(notebook_root, notebook_file_entry)
        files.add_tree(assets_root, asset_file_entry, skip=normalizer.dropped_asset_files())
        summary = files.finish()
    finally:
        files.close()
    notebook = normalizer.notebook
    manifest = ArchiveManifest(
        format_version=FORMAT_VERSION,
        exported_at=exported_at,
        schema=_schema_diagnostics(settings),
        notebook={
            "id": str(notebook.get("id") or notebook_id),
            "name": str(notebook.get("name") or ""),
            "purpose": str(notebook.get("purpose") or ""),
            "primary_domain": str(notebook.get("primary_domain") or ""),
        },
        kg_starts_dirty=outcome.starts_dirty,
        kg_clusters_dropped=outcome.clusters_dropped,
        vectors_included=include_vectors,
        embeddings={workload: embeddings.get(workload) for workload in EMBEDDING_WORKLOADS},
        tables=tables,
        files=summary,
        global_object_types=normalizer.global_object_types(global_object_schemas),
        excluded_mount_edges=normalizer.mount_edges,
    )
    writer.write_document(MANIFEST_NAME, json_document(manifest.to_dict()).encode("utf-8"))
    missing = sum(
        1 for relative in normalizer.source_files if relative not in files.notebook_files
    )
    return ExportReport(
        notebook_id=notebook_id,
        path=out_path,
        archive_bytes=0,
        rows={table: entry.rows for table, entry in tables.items()},
        files=summary.count,
        file_bytes=summary.bytes,
        skipped_links=files.skipped,
        vectors_included=include_vectors,
        normalized=dict(sorted(normalizer.counts.items())),
        missing_source_files=missing,
    )


def _schema_diagnostics(settings: Settings) -> dict[str, Any]:
    from app.repositories.postgres.schema_manifest import POSTGRES_SCHEMA_MANIFEST

    return {
        "backend": database_identity(settings.database_url).scheme,
        "sqlite_version": POSTGRES_SCHEMA_MANIFEST.sqlite_version,
        "postgres_version": POSTGRES_SCHEMA_MANIFEST.postgres_version,
    }


@functools.lru_cache(maxsize=None)
def _row_encoder(table: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """The portable encoder for one table, its JSON and timestamp columns read
    from the PostgreSQL migration DDL contract (the canonical schema) -- the
    same column classes the sync package uses, so both formats carry one row
    shape."""
    from app.migration.shadow.postgres_catalog import EXPECTED_COLUMNS

    contracts = EXPECTED_COLUMNS.get(table) or {}
    return column_encoder(
        [name for name, column in contracts.items() if column.data_type == "jsonb"],
        [
            name
            for name, column in contracts.items()
            if column.data_type == "timestamp with time zone"
        ],
    )


class _Normalizer:
    """The export-side rewrite of each row (see the module docstring). Tables
    arrive in closure order, so every id set a later table is checked against
    (sources, objects) is complete before that table streams."""

    def __init__(self, notebook_root: Path) -> None:
        self._notebook_root = notebook_root
        self.notebook: dict[str, Any] = {}
        self.source_ids: set[str] = set()
        self.object_ids: set[str] = set()
        self.object_types: set[str] = set()
        self.schema_types: set[str] = set()
        #: archive-relative paths of the sources' files
        self.source_files: set[str] = set()
        self.mount_edges = 0
        self.counts: dict[str, int] = {}
        self._dropped_assets: set[str] = set()
        self._handlers: dict[str, Callable[[dict], "dict | None"]] = {
            "notebooks": self._row_notebooks,
            "notebook_bases": self._row_notebook_bases,
            "sources": self._row_sources,
            "knowledge_objects": self._row_knowledge_objects,
            "knowledge_source_facts": self._row_knowledge_source_facts,
            "knowledge_source_fact_backfills": self._row_knowledge_source_fact_backfills,
            "knowledge_relations": self._row_knowledge_relations,
            "concept_clusters": self._row_concept_clusters,
            "notebook_object_schemas": self._row_notebook_object_schemas,
            "knowhow_tables": self._row_knowhow_tables,
            "notebook_assets": self._row_notebook_assets,
        }

    def _note(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    def _clear(self, row: dict, table: str, column: str, known: set, empty: Any) -> None:
        value = row.get(column)
        if value and value not in known:
            row[column] = empty
            self._note(f"{table}.{column}")

    def __call__(self, table: str, row: dict) -> "dict | None":
        handler = self._handlers.get(table)
        return row if handler is None else handler(row)

    def _row_notebooks(self, row: dict) -> dict:
        # A share link is a live read capability of THIS deployment's notebook.
        row["share_token"] = None
        row["is_shared"] = 0
        self.notebook = dict(row)
        return row

    def _row_notebook_bases(self, row: dict) -> None:
        self.mount_edges += 1
        return None

    def _row_sources(self, row: dict) -> dict:
        self.source_ids.add(str(row["id"]))
        recorded = row.get("file_path")
        if recorded:
            relative = self._relative_file_path(str(recorded))
            row["file_path"] = relative
            self.source_files.add(relative)
        return row

    def _relative_file_path(self, recorded: str) -> str:
        """The recorded file as a path relative to the notebook's file tree;
        a path outside it keeps only its name (the deep copy's rule:
        ``destination_dir / Path(file_path).name``)."""
        path = Path(recorded)
        for root in (self._notebook_root, self._notebook_root.resolve()):
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if is_safe_relative_path(relative):
                return relative
        return path.name

    def _row_knowledge_objects(self, row: dict) -> dict:
        self.object_ids.add(str(row["id"]))
        if row.get("object_type"):
            self.object_types.add(str(row["object_type"]))
        self._clear(row, "knowledge_objects", "source_id", self.source_ids, "")
        self._clear(row, "knowledge_objects", "source_candidate_id", self.source_ids, None)
        return row

    def _row_knowledge_source_facts(self, row: dict) -> dict:
        if row.get("object_type"):
            self.object_types.add(str(row["object_type"]))
        self._clear(row, "knowledge_source_facts", "global_object_id", self.object_ids, "")
        return row

    def _row_knowledge_source_fact_backfills(self, row: dict) -> dict:
        self._clear(
            row, "knowledge_source_fact_backfills", "after_object_id", self.object_ids, ""
        )
        return row

    def _row_knowledge_relations(self, row: dict) -> dict:
        self._clear(row, "knowledge_relations", "source_id", self.source_ids, None)
        return row

    def _row_concept_clusters(self, row: dict) -> "dict | None":
        if str(row.get("member_object_id") or "") not in self.object_ids:
            self._note("concept_clusters.member_object_id")
            return None
        return row

    def _row_notebook_object_schemas(self, row: dict) -> dict:
        self.schema_types.add(str(row["object_type"]))
        return row

    def _row_knowhow_tables(self, row: dict) -> dict:
        self._clear(row, "knowhow_tables", "hidden_source_id", self.source_ids, None)
        return row

    def _row_notebook_assets(self, row: dict) -> "dict | None":
        source_id = row.get("source_id")
        if source_id is not None and str(source_id) not in self.source_ids:
            # The asset of a source that is gone: nothing can display it and
            # its source can never delete it. Dropped with its file.
            ext = ALLOWED_MIME_EXTENSIONS.get(str(row.get("mime") or ""), "bin")
            self._dropped_assets.add(f"{row['id']}.{ext}")
            self._note("notebook_assets.source_id")
            return None
        return row

    def dropped_asset_files(self) -> frozenset[str]:
        return frozenset(self._dropped_assets)

    def global_object_types(
        self, schemas: tuple[ObjectSchemaModel, ...]
    ) -> tuple[dict[str, Any], ...]:
        """The deployment-wide custom object types this notebook's KG uses but
        does not define itself (built-in types exist everywhere)."""
        wanted = self.object_types - self.schema_types
        return tuple(
            {
                "object_type": schema.object_type,
                "plural": schema.plural,
                "fields": list(schema.fields),
                "primary_field": schema.primary,
                "description": schema.description,
                "label": schema.label,
                "list_fields": list(schema.list_fields),
                "status": schema.status,
            }
            for schema in sorted(schemas, key=lambda item: item.object_type)
            if schema.object_type in wanted and schema.source != "builtin"
        )


class _ArchiveWriter:
    """The zip being written: forced ZIP64, deflate, one entry open at a time,
    every entry counted against the reader limits as it closes."""

    def __init__(self, part: Path) -> None:
        self._part = part
        self._zip = zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True)
        self._entries = 0
        self._uncompressed = 0

    def _info(self, name: str) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(name, date_time=_ENTRY_DATE)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = _ENTRY_MODE
        return info

    def _open(self, name: str):
        self._entries += 1
        if self._entries > MAX_ENTRIES:
            raise NotebookExportError("导出文件的条目数超过读取端上限")
        return self._zip.open(self._info(name), "w", force_zip64=True)

    def _closed(self, name: str) -> None:
        info = self._zip.getinfo(name)
        self._uncompressed += info.file_size
        if self._uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise NotebookExportError("导出内容总量超过读取端上限")
        if (
            info.file_size > COMPRESSION_RATIO_FLOOR_BYTES
            and info.file_size > MAX_ENTRY_COMPRESSION_RATIO * max(info.compress_size, 1)
        ):
            raise NotebookExportError(f"条目 {name} 的压缩比超过读取端上限")

    def write_rows(
        self, table: str, pages: Iterator[list[dict]], normalize: _Normalizer
    ) -> TableEntry:
        name = rows_entry(table)
        encode = _row_encoder(table)
        digest = DigestCounter()
        columns: set[str] = set()
        count = 0
        with self._open(name) as entry:
            for page in pages:
                for row in page:
                    kept = normalize(table, row)
                    if kept is None:
                        continue
                    encoded = encode(kept)
                    line = (json_line(encoded) + "\n").encode("utf-8")
                    if len(line) > MAX_JSONL_LINE_BYTES:
                        raise NotebookExportError(f"{table} 有一行超过读取端的单行上限")
                    entry.write(line)
                    digest.update(line)
                    columns.update(encoded)
                    count += 1
        self._closed(name)
        return TableEntry(
            columns=tuple(sorted(columns)), rows=count, bytes=digest.size,
            sha256=digest.hexdigest(),
        )

    def write_file(self, name: str, path: Path) -> DigestCounter:
        digest = DigestCounter()
        with path.open("rb") as source, self._open(name) as entry:
            while True:
                block = source.read(_COPY_BLOCK)
                if not block:
                    break
                entry.write(block)
                digest.update(block)
        self._closed(name)
        return digest

    def write_stream(self, name: str, source) -> DigestCounter:
        digest = DigestCounter()
        with self._open(name) as entry:
            while True:
                block = source.read(_COPY_BLOCK)
                if not block:
                    break
                entry.write(block)
                digest.update(block)
        self._closed(name)
        return digest

    def write_document(self, name: str, data: bytes) -> None:
        with self._open(name) as entry:
            entry.write(data)
        self._closed(name)

    def close(self) -> None:
        self._zip.close()

    def abort(self) -> None:
        try:
            self._zip.close()
        except Exception:  # noqa: BLE001 -- the part file is removed either way
            pass
        try:
            self._part.unlink()
        except FileNotFoundError:
            pass


class _FileIndex:
    """``files/**`` entries and their ``files.jsonl`` index, spooled to a
    temporary file (an index line per file, written after the last file entry
    because a zip has one entry open at a time)."""

    def __init__(self, writer: _ArchiveWriter) -> None:
        self._writer = writer
        self._index = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
        self.count = 0
        self.bytes = 0
        self.skipped = 0
        #: archive-relative paths (``files/notebooks/<this>``) of the source tree
        self.notebook_files: set[str] = set()

    def add_tree(
        self, root: Path, entry_name: Callable[[str], str], *, skip: frozenset[str] = frozenset()
    ) -> None:
        if not root.is_dir() or root.is_symlink():
            return
        for directory, subdirs, names in os.walk(root, followlinks=False):
            base = Path(directory)
            for subdir in list(subdirs):
                if (base / subdir).is_symlink():
                    subdirs.remove(subdir)
                    self.skipped += 1
            subdirs.sort()
            for name in sorted(names):
                path = base / name
                relative = path.relative_to(root).as_posix()
                if base == root and name in skip:
                    continue
                mode = os.lstat(path).st_mode
                if not stat.S_ISREG(mode) or not is_safe_relative_path(relative):
                    self.skipped += 1
                    continue
                self._add(entry_name(relative), path)
                if entry_name is notebook_file_entry:
                    self.notebook_files.add(relative)

    def _add(self, name: str, path: Path) -> None:
        digest = self._writer.write_file(name, path)
        line = json_line({"path": name, "sha256": digest.hexdigest(), "size": digest.size})
        self._index.write((line + "\n").encode("utf-8"))
        self.count += 1
        self.bytes += digest.size

    def finish(self) -> FilesSummary:
        self._index.seek(0)
        digest = self._writer.write_stream(FILES_INDEX_NAME, self._index)
        return FilesSummary(
            count=self.count, bytes=self.bytes,
            index_bytes=digest.size, index_sha256=digest.hexdigest(),
        )

    def close(self) -> None:
        self._index.close()

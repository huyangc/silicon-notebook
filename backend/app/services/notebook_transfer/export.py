"""Export one notebook to a single ``.snbk`` archive (format:
``app.domain.notebook_archive``).

Read-only against the database: the rows come from the store's streaming copy
read (``SharingStorePort.iter_copy_rows``) -- the deep copy's own closure,
statements and Memory rules (ruling M2: no Memory, nothing derived from one, no
Memory evidence inside kept rows), paged inside one read transaction and NOT
bounded by the copy-size limits, so a notebook too large to copy can still be
exported. Each table goes into its zip entry as it streams, one page of rows in
memory at a time. What does grow with the notebook: the normaliser's id sets
(every exported source and knowledge object id, the referenced object types),
the zip's in-memory directory (one ``ZipInfo`` per entry) and the
``files.jsonl`` index, which spills to a temporary file past 8 MiB.

What the export changes on the way out (``_Normalizer``), so that a file this
build wrote passes the importer's strict reference check:

* a notebook row never carries its share link (``share_token``/``is_shared``);
* ``sources.file_path`` becomes the path relative to the notebook's file tree
  (the archive's ``files/notebooks/`` entries), never a host path;
* soft pointers the deep copy passes through unmapped (``map.get(x, x)``) are
  cleared when they point outside the exported closure; an asset row whose
  source is gone is dropped together with its file; a cluster row whose member
  object, or the object its canonical id was minted from, is not exported is
  dropped;
* mount edges are not carried at all (an import never mounts); their number is
  recorded in ``manifest.excluded``.

Files are the notebook's ``storage/notebooks/<id>/`` and ``storage/assets/<id>/``
trees, read after the row transaction (as the deep copy does). The trees are
walked through directory descriptors (``os.fwalk``) and each file is opened
relative to its directory's descriptor, ``O_NOFOLLOW | O_NONBLOCK``, and checked
to be a regular file on that same descriptor, so a symbolic link, FIFO or device
-- including one swapped in after the directory listing, at the file itself or
at any directory above it -- is never followed: a file-level one is skipped and
counted, a swapped directory is not entered. A file that vanished since
the listing is counted as missing; either way the export continues. A large
file that would compress beyond the reader's ratio limit is stored uncompressed.

The archive is written to a uniquely named ``<out>.<random>.part`` (created
``O_EXCL``), fsynced, and published with a hard link, which fails if ``<out>``
exists, so an existing file is never overwritten and two exports to one path
cannot corrupt each other; the directory is fsynced after the link. The
manifest is the last entry; any failure removes the part file. A part file left
by a killed export is reported (``ExportReport.stale_part_files``), never
deleted automatically. A file the export cannot read for any reason other than
"gone" or "not a regular file" (EACCES, EIO...) fails the export.
"""

from __future__ import annotations

import dataclasses
import errno
import functools
import glob
import os
import secrets
import stat
import tempfile
import time
import zipfile
import zlib
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
    MAX_MANIFEST_BYTES,
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
from app.services.notebook_copy_writer import cluster_seed_object_id

#: Bytes per read when copying a file into its entry while hashing it.
_COPY_BLOCK = 1 << 20
#: Bytes per read of the compression-ratio probe (``_deflate_within_ratio``):
#: small, because ordinary content proves its ratio after a few reads.
_PROBE_BLOCK = 64 * 1024
#: Every entry's timestamp: an archive's bytes depend on its content, not on
#: when its files were last touched (zip cannot store dates before 1980).
_ENTRY_DATE = (1980, 1, 1, 0, 0, 0)
#: Regular file, rw-r--r--, in the zip external attributes' high word.
_ENTRY_MODE = (stat.S_IFREG | 0o644) << 16


class NotebookExportError(RuntimeError):
    """The export could not produce a valid archive. The message is for the
    operator and never carries row content."""


class NotebookNotFoundError(KeyError):
    """The notebook does not exist or is being deleted (the store's
    liveness-filtered root read found no row)."""


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
    #: Files listed in the tree that were gone by the time they were opened.
    missing_files: int
    vectors_included: bool
    #: ``<table>.<column>`` -> how many values the export cleared or rows it
    #: dropped because they pointed outside the exported closure.
    normalized: Mapping[str, int]
    #: Sources whose recorded file is not in the notebook's file tree.
    missing_source_files: int
    #: Leftover ``<out>.<random>.part`` files of killed earlier exports to the
    #: same output, older than ``STALE_PART_SECONDS`` (reported, not deleted).
    stale_part_files: tuple[str, ...] = ()

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
    checkpoint: "Callable[[int], None] | None" = None,
) -> ExportReport:
    """Write ``notebook_id`` to ``out_path`` (see the module docstring).

    ``checkpoint(written)`` runs before every page of rows and every block of a
    file goes into the archive, with the bytes the part file holds so far; what
    it raises stops the export (the part file is removed) and reaches the caller
    unchanged -- the web job's cancellation and its archive-size cap.

    Raises ``NotebookNotFoundError`` when the notebook does not exist or is being
    deleted, ``NotebookExportError`` when the output already exists (checked
    before and again at publication) or the archive would break a reader limit.
    The output's directory must exist."""
    out_path = Path(out_path)
    if out_path.exists():
        raise NotebookExportError(f"输出文件已存在: {out_path}")
    if not out_path.parent.is_dir():
        raise NotebookExportError(f"输出目录不存在: {out_path.parent}")
    _refuse_output_inside_exported_trees(out_path, Path(storage_dir), notebook_id)
    stale = stale_part_files(out_path)
    writer = _ArchiveWriter.create(out_path, checkpoint=checkpoint)
    try:
        report = _write_archive(
            writer, rows, notebook_id, out_path,
            storage_dir=Path(storage_dir), settings=settings, embeddings=embeddings,
            global_object_schemas=tuple(global_object_schemas),
            include_vectors=include_vectors,
            exported_at=(now or _utc_now)().astimezone(timezone.utc).isoformat(),
        )
        writer.close()
        _publish(writer.part, out_path)
    except BaseException:
        writer.abort()
        raise
    return dataclasses.replace(
        report, archive_bytes=out_path.stat().st_size, stale_part_files=stale
    )


def _refuse_output_inside_exported_trees(
    out_path: Path, storage_dir: Path, notebook_id: str
) -> None:
    """The archive's own ``.part`` file must never be one of the files it
    copies: written inside the notebook's file or asset tree, the walk would
    reach the part file and append its compressed bytes to itself until the
    disk fills (codex #974 r1). Compared on resolved paths, so a symlinked
    output directory that lands inside a tree is refused too."""
    destination = out_path.parent.resolve()
    for tree in (storage_dir / "notebooks" / notebook_id, storage_dir / "assets" / notebook_id):
        root = tree.resolve()
        if destination == root or root in destination.parents:
            raise NotebookExportError(f"输出路径不能位于被导出笔记本的文件目录内: {out_path}")


def _publish(part: Path, out_path: Path) -> None:
    """Give the finished (already fsynced) part file its final name without
    ever replacing an existing file: a hard link fails with ``EEXIST`` when
    ``out_path`` exists. On a filesystem without hard links, ``out_path`` is
    first claimed with an empty ``O_EXCL`` placeholder (same refusal) and the
    part file renamed over that claim; if the rename fails, the placeholder --
    recognised by the device/inode recorded when it was created, still empty --
    is removed before the error propagates. The directory is fsynced once the
    new name exists, so the publication survives a power loss."""
    try:
        os.link(part, out_path)
    except FileExistsError:
        raise NotebookExportError(f"输出文件已存在: {out_path}") from None
    except OSError as exc:
        if exc.errno not in _NO_HARD_LINKS:
            raise
        _publish_by_rename(part, out_path)
    else:
        part.unlink()
    _fsync_directory(out_path.parent)


def _publish_by_rename(part: Path, out_path: Path) -> None:
    try:
        fd = os.open(out_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise NotebookExportError(f"输出文件已存在: {out_path}") from None
    try:
        claim = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        os.replace(part, out_path)
    except BaseException:
        try:
            current = os.lstat(out_path)
        except OSError:
            current = None
        if (
            current is not None
            and (current.st_dev, current.st_ino) == (claim.st_dev, claim.st_ino)
            and current.st_size == 0
        ):
            out_path.unlink()
        raise


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


#: A ``<out>.<random>.part`` sibling untouched this long is left over from an
#: export that was killed (SIGKILL, OOM) before it could remove it.
STALE_PART_SECONDS = 3600


def stale_part_files(out_path: Path, *, now: "float | None" = None) -> tuple[str, ...]:
    """Names of leftover part files of earlier exports to ``out_path`` (never
    deleted here: one of them may belong to a running export)."""
    limit = (time.time() if now is None else now) - STALE_PART_SECONDS
    found = []
    for candidate in out_path.parent.glob(f"{glob.escape(out_path.name)}.*{PART_SUFFIX}"):
        try:
            if candidate.stat().st_mtime < limit:
                found.append(candidate.name)
        except FileNotFoundError:
            continue
    return tuple(sorted(found))


#: ``os.link`` errors that mean "this filesystem has no hard links".
_NO_HARD_LINKS = frozenset(
    code for code in (
        errno.EPERM, errno.EXDEV, errno.EMLINK,
        getattr(errno, "ENOTSUP", None), getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "ENOSYS", None),
    ) if code is not None
)


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

    handed: list[str] = []

    def on_table(table: str, pages: Iterator[list[dict]]) -> None:
        handed.append(table)
        tables[table] = writer.write_rows(table, pages, normalizer)

    try:
        outcome = rows.iter_copy_rows(
            notebook_id,
            on_table=on_table,
            skip_tables=frozenset() if include_vectors else frozenset(VECTOR_TABLES),
        )
    except KeyError as exc:
        # Only the root read's own refusal, raised before any table is handed out.
        if not handed and exc.args == (notebook_id,):
            raise NotebookNotFoundError(notebook_id) from None
        raise
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
        kg_concept_seed_version=outcome.concept_seed_version,
        vectors_included=include_vectors,
        embeddings={workload: embeddings.get(workload) for workload in EMBEDDING_WORKLOADS},
        tables=tables,
        files=summary,
        global_object_types=normalizer.global_object_types(global_object_schemas),
        excluded_mount_edges=normalizer.mount_edges,
    )
    encoded = json_document(manifest.to_dict()).encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        # codex #974 r3: the reader refuses a manifest beyond this size, so an
        # archive carrying one would be published only to be unreadable. The
        # unbounded part is global_object_types (custom schema descriptions).
        raise NotebookExportError(
            f"导出文件的清单过大（{len(encoded)} 字节，上限 {MAX_MANIFEST_BYTES} 字节），"
            "通常是引用的自定义对象类型说明过长"
        )
    writer.write_document(MANIFEST_NAME, encoded)
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
        missing_files=files.missing,
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
        seed = cluster_seed_object_id(str(row.get("canonical_id") or ""))
        if seed is not None and seed not in self.object_ids:
            # Minted from an object this export does not carry (gone, or not in
            # the closure): the same "cannot prove it is not stale" rule as the
            # copy's ``_visible_cluster``.
            self._note("concept_clusters.canonical_id")
            return None
        return row

    def _row_notebook_object_schemas(self, row: dict) -> dict:
        # A local ``proposed`` row is review-only: the registry keeps resolving
        # the global definition for that type (SchemaRegistryService.
        # effective_schemas), so it must not stop the global one travelling
        # (codex #974 r4). Active and disabled local rows do shadow it.
        if str(row.get("status") or "") != "proposed":
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
    """The zip being written: forced ZIP64, deflate (stored for a file that
    would inflate beyond the ratio limit), one entry open at a time, every entry
    counted against the reader limits as it closes."""

    def __init__(
        self, part: Path, handle, *, checkpoint: "Callable[[int], None] | None" = None
    ) -> None:
        self.part = part
        self._handle = handle
        self._zip = zipfile.ZipFile(
            handle, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
        )
        self._entries = 0
        self._uncompressed = 0
        self._checkpoint = checkpoint

    @classmethod
    def create(
        cls, out_path: Path, *, checkpoint: "Callable[[int], None] | None" = None
    ) -> "_ArchiveWriter":
        """A fresh, uniquely named part file beside ``out_path`` (``O_EXCL``)."""
        while True:
            part = out_path.with_name(f"{out_path.name}.{secrets.token_hex(6)}{PART_SUFFIX}")
            try:
                fd = os.open(part, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
            except FileExistsError:
                continue
            return cls(part, os.fdopen(fd, "w+b"), checkpoint=checkpoint)

    def _check(self) -> None:
        """Hand the caller's checkpoint the bytes written so far (the position
        of the part file: the zip writes it sequentially, compressed)."""
        if self._checkpoint is not None:
            self._checkpoint(self._handle.tell())

    def _info(self, name: str, compress_type: int) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(name, date_time=_ENTRY_DATE)
        info.compress_type = compress_type
        info.external_attr = _ENTRY_MODE
        return info

    def _open(self, name: str, compress_type: int = zipfile.ZIP_DEFLATED):
        self._entries += 1
        if self._entries > MAX_ENTRIES:
            raise NotebookExportError("导出文件的条目数超过读取端上限")
        return self._zip.open(self._info(name, compress_type), "w", force_zip64=True)

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
                self._check()
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

    def write_file(self, name: str, fd: int, size: int) -> DigestCounter:
        """One regular file, read from an already-verified descriptor."""
        compress_type = (
            zipfile.ZIP_DEFLATED if _deflate_within_ratio(fd, size) else zipfile.ZIP_STORED
        )
        os.lseek(fd, 0, os.SEEK_SET)
        digest = DigestCounter()
        with self._open(name, compress_type) as entry:
            while True:
                self._check()
                block = os.read(fd, _COPY_BLOCK)
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
                self._check()
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
        """Finish the zip and make its bytes durable before it is published."""
        self._zip.close()
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()

    def abort(self) -> None:
        for close in (self._zip.close, self._handle.close):
            try:
                close()
            except Exception:  # noqa: BLE001 -- the part file is removed either way
                pass
        try:
            self.part.unlink()
        except FileNotFoundError:
            pass


def _deflate_within_ratio(fd: int, size: int) -> bool:
    """Whether deflating this file keeps its entry within the reader's ratio
    limit. Exact: the same raw deflate stream ``zipfile`` writes, measured
    without keeping it, and stopped as soon as the output is large enough --
    for ordinary content within the first few ``_PROBE_BLOCK`` reads, so a large
    ordinary file is not compressed twice."""
    if size <= COMPRESSION_RATIO_FLOOR_BYTES:
        return True
    compressor = zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, -15)
    produced = 0
    while True:
        block = os.read(fd, _PROBE_BLOCK)
        if not block:
            break
        produced += len(compressor.compress(block))
        if produced * MAX_ENTRY_COMPRESSION_RATIO >= size:
            return True
    produced += len(compressor.flush())
    return produced * MAX_ENTRY_COMPRESSION_RATIO >= size


#: Read-only, never through a final symbolic link, never blocking on a FIFO.
_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
#: ``open`` errors that mean "this is not a regular file": a final symbolic
#: link under ``O_NOFOLLOW`` (ELOOP; EMLINK on some BSDs), a socket or a device
#: without a driver (ENXIO, EOPNOTSUPP/ENOTSUP, ENODEV). Skipped and counted;
#: every other error fails the export.
_NOT_A_REGULAR_FILE = frozenset(
    code for code in (
        errno.ELOOP, errno.EMLINK, errno.ENXIO, errno.ENODEV,
        getattr(errno, "EOPNOTSUPP", None), getattr(errno, "ENOTSUP", None),
    ) if code is not None
)


class _FileIndex:
    """``files/**`` entries and their ``files.jsonl`` index, spooled to a
    temporary file (an index line per file, written after the last file entry
    because a zip has one entry open at a time)."""

    def __init__(self, writer: _ArchiveWriter) -> None:
        self._writer = writer
        self._index = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
        self.count = 0
        self.bytes = 0
        #: symbolic links, special files and unsafe names left out
        self.skipped = 0
        #: listed files that were gone when opened
        self.missing = 0
        #: archive-relative paths (``files/notebooks/<this>``) of the source tree
        self.notebook_files: set[str] = set()

    def add_tree(
        self, root: Path, entry_name: Callable[[str], str], *, skip: frozenset[str] = frozenset()
    ) -> None:
        if root.is_symlink():
            # The tree root itself is a link: nothing under it is the
            # notebook's own storage. Counted, never followed.
            self.skipped += 1
            return
        if not root.is_dir():
            return
        # codex #974 r2 (P1): every open is anchored to a directory descriptor.
        # ``O_NOFOLLOW`` guards only the last path component, so opening
        # ``<root>/sub/file`` by name follows ``sub`` if it was swapped for a
        # link after the listing. ``os.fwalk`` without ``follow_symlinks`` opens
        # each directory relative to its parent's descriptor and descends only
        # when the opened descriptor is the directory it listed (a swapped-in
        # link is not entered). A link to a directory is listed among the
        # subdirectories but never entered; it is pruned here so it is counted.
        for directory, subdirs, names, dir_fd in os.fwalk(
            root, follow_symlinks=False, onerror=self._walk_error
        ):
            base = Path(directory)
            for subdir in list(subdirs):
                try:
                    mode = os.stat(subdir, dir_fd=dir_fd, follow_symlinks=False).st_mode
                except FileNotFoundError:
                    continue  # gone: fwalk's own open reports it to _walk_error
                if stat.S_ISLNK(mode):
                    subdirs.remove(subdir)
                    self.skipped += 1
            subdirs.sort()
            for name in sorted(names):
                if base == root and name in skip:
                    continue
                relative = (base / name).relative_to(root).as_posix()
                if not is_safe_relative_path(relative):
                    self.skipped += 1
                    continue
                added = self._add_one(entry_name(relative), name, dir_fd)
                if added and entry_name is notebook_file_entry:
                    self.notebook_files.add(relative)

    def _walk_error(self, exc: OSError) -> None:
        """A directory ``os.fwalk`` could not list or open: gone since its
        parent was listed counts as missing; anything else fails the export,
        never drops the subtree silently."""
        if isinstance(exc, FileNotFoundError):
            self.missing += 1
            return
        raise NotebookExportError(
            f"无法读取笔记本目录（{errno.errorcode.get(exc.errno, exc.errno)}）"
        ) from exc

    def _add_one(self, name: str, leaf: str, dir_fd: int) -> bool:
        """Open ``leaf`` relative to its verified directory descriptor, verify
        on the same file descriptor, then copy (module docstring). Returns
        whether the file was carried."""
        try:
            fd = os.open(leaf, _OPEN_FLAGS, dir_fd=dir_fd)
        except FileNotFoundError:
            self.missing += 1
            return False
        except OSError as exc:
            if exc.errno in _NOT_A_REGULAR_FILE:
                self.skipped += 1
                return False
            # EACCES, EIO, EMFILE...: a file of the notebook that cannot be
            # read. Never silently left out of the archive.
            raise NotebookExportError(
                f"无法读取笔记本文件 {name}（{errno.errorcode.get(exc.errno, exc.errno)}）"
            ) from exc
        try:
            status = os.fstat(fd)
            if not stat.S_ISREG(status.st_mode):
                self.skipped += 1
                return False
            digest = self._writer.write_file(name, fd, status.st_size)
        finally:
            os.close(fd)
        line = json_line({"path": name, "sha256": digest.hexdigest(), "size": digest.size})
        self._index.write((line + "\n").encode("utf-8"))
        self.count += 1
        self.bytes += digest.size
        return True

    def finish(self) -> FilesSummary:
        self._index.seek(0)
        digest = self._writer.write_stream(FILES_INDEX_NAME, self._index)
        return FilesSummary(
            count=self.count, bytes=self.bytes,
            index_bytes=digest.size, index_sha256=digest.hexdigest(),
        )

    def close(self) -> None:
        self._index.close()

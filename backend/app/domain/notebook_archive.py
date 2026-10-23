"""The single-file notebook archive (``.snbk``): layout, manifest and limits.

One notebook's copy closure -- its materials, original files and attachments,
elements, chunks, vectors, knowledge graph and Knowhow tables -- in one zip
file, so it can be carried to another deployment and imported there as an
independent new notebook (docs/superpowers/plans/2026-10-22-notebook-export-import.md
§3). The writer is ``app.services.notebook_transfer.export``; the reader is
``app.services.notebook_transfer.archive_reader``. Both import their names from
here, so the layout has one definition.

Zip entries (the archive is always written with ZIP64 records)::

    manifest.json                     written LAST; a file without it is unfinished
    files.jsonl                       one line per carried file: path, size, sha256
    rows/<table>.jsonl                one line per row, for every table of ARCHIVE_TABLES
    files/notebooks/<relative path>   the notebook's storage/notebooks/<id>/ tree
    files/assets/<relative path>      the notebook's storage/assets/<id>/ tree

Rows use the portable encoding of ``app.domain.portable_rows`` (SQLite shape,
``{"$bytes": b64}`` for byte columns, ``sort_keys`` JSONL). Timestamp columns
(classified from the PostgreSQL DDL contract): a zone-aware value -- every
PostgreSQL ``timestamptz``, SQLite text with an offset -- is written as UTC ISO
text; a naive SQLite value carries no zone and travels exactly as stored, and a
reader treats it as UTC by convention, as the sync importer does
(``app.migration.shadow.transform``); SQLite's ``""`` "no time" sentinel
travels unchanged.
``format_version`` is independent of the database schema version: the real
compatibility gate on import is "every column a file carries exists in the
target", which the reader checks table by table. A change to the closure's
table set changes what a file means, so it moves ``FORMAT_VERSION``
(``test_notebook_archive_format`` pins the closure to ``ARCHIVE_TABLES``).

Pure standard library: the domain layer imports nothing from the application.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from app.domain.portable_rows import is_safe_relative_path

FORMAT = "silicon-notebook.notebook-archive"
#: Bumped whenever the layout, the table set or the meaning of a field changes
#: incompatibly; a reader refuses a version it does not list as readable.
FORMAT_VERSION = 1
READABLE_FORMAT_VERSIONS = (1,)
FILE_EXTENSION = ".snbk"
#: Suffix of the file a writer fills before the atomic rename to the final name.
PART_SUFFIX = ".part"

MANIFEST_NAME = "manifest.json"
FILES_INDEX_NAME = "files.jsonl"
ROWS_DIR = "rows"
FILES_DIR = "files"
NOTEBOOK_FILES_DIR = "notebooks"
ASSET_FILES_DIR = "assets"

#: The copy closure, in the order the copy snapshot reads it (both backends'
#: ``sharing_store._COPY_SNAPSHOT_QUERIES``). Every table has an entry in every
#: archive, empty or not.
ARCHIVE_TABLES: tuple[str, ...] = (
    "notebooks",
    "notebook_bases",
    "sources",
    "source_paper_meta",
    "source_authors",
    "source_elements",
    "chunks",
    "knowledge_objects",
    "knowledge_source_facts",
    "knowledge_source_fact_elements",
    "knowledge_source_fact_backfills",
    "knowledge_relations",
    "chunk_embeddings",
    "chunk_questions",
    "element_embeddings",
    "knowledge_embeddings",
    "relation_embeddings",
    "concept_clusters",
    "notebook_object_schemas",
    "knowhow_tables",
    "knowhow_columns",
    "knowhow_rows",
    "knowhow_cells",
    "knowhow_cell_code",
    "notebook_assets",
)
#: The tables an export without vectors leaves empty. ``chunk_questions.vector``
#: is NOT NULL and stays: the importer re-embeds question text when its model
#: differs.
VECTOR_TABLES: tuple[str, ...] = (
    "chunk_embeddings",
    "element_embeddings",
    "knowledge_embeddings",
    "relation_embeddings",
)
#: The embedding workloads whose vectors an archive may carry, each recorded
#: with the model identity that produced them (or null when unbound).
EMBEDDING_WORKLOADS: tuple[str, ...] = (
    "chunk_embedding",
    "source_element_embedding",
    "knowledge_object_embedding",
    "relation_embedding",
    "knowhow_embedding",
)
#: How vector bytes are laid out: little-endian IEEE 754 float32, ``dim`` of them.
VECTOR_ENCODING = "float32-le"
#: ``excluded.memory``: Memory never travels (ruling M2). Deliberately a fixed
#: word and not a count: the size of a member's Memory is not the file
#: holder's to learn.
MEMORY_NOT_CARRIED = "not_carried"

# ---------------------------------------------------------- reader limits
# Protocol constants every reader enforces before it trusts a byte of an
# archive (zip-bomb and resource guards). The writer refuses to produce a file
# that would break them, so an archive this build exported always reads back.

#: Most zip entries an archive may hold (rows + index + manifest + files).
MAX_ENTRIES = 2_000_000
#: Most bytes the entries may decompress to, summed (2 TiB).
MAX_TOTAL_UNCOMPRESSED_BYTES = 2 * 1024**4
#: Highest uncompressed/compressed ratio of one entry. Deflate's own ceiling is
#: about 1032:1, so ordinary content never comes near it.
MAX_ENTRY_COMPRESSION_RATIO = 1000
#: Entries at most this large are exempt from the ratio check (a tiny entry of
#: repeated bytes is harmless and legitimately compresses very well).
COMPRESSION_RATIO_FLOOR_BYTES = 1024 * 1024
#: Longest single JSONL line (one row, one file index line) a reader accepts.
MAX_JSONL_LINE_BYTES = 64 * 1024 * 1024
#: Largest ``manifest.json`` a reader parses.
MAX_MANIFEST_BYTES = 16 * 1024 * 1024


class ManifestError(ValueError):
    """``manifest.json`` is missing a field, has a wrong type, or names a
    different format. The message is for the operator; it never echoes file
    content beyond a field name."""


class ArchiveVersionTooNewError(ManifestError):
    """The archive was written by a newer build than this one can read."""


def rows_entry(table: str) -> str:
    """``rows/<table>.jsonl``."""
    return f"{ROWS_DIR}/{table}.jsonl"


def notebook_file_entry(relative: str) -> str:
    """``files/notebooks/<relative>``: a file of the notebook's source tree."""
    return f"{FILES_DIR}/{NOTEBOOK_FILES_DIR}/{relative}"


def asset_file_entry(relative: str) -> str:
    """``files/assets/<relative>``: a file of the notebook's attachment tree."""
    return f"{FILES_DIR}/{ASSET_FILES_DIR}/{relative}"


_ROWS_ENTRIES = {rows_entry(table): table for table in ARCHIVE_TABLES}
_FILE_PREFIXES = (
    f"{FILES_DIR}/{NOTEBOOK_FILES_DIR}/",
    f"{FILES_DIR}/{ASSET_FILES_DIR}/",
)


def entry_kind(name: str) -> "str | None":
    """What a zip entry name is, or None when it may not appear in an archive:
    ``"manifest"``, ``"files_index"``, ``"rows"`` (one of ``ARCHIVE_TABLES``)
    or ``"file"`` (a safe relative path under one of the two file roots)."""
    if name == MANIFEST_NAME:
        return "manifest"
    if name == FILES_INDEX_NAME:
        return "files_index"
    if name in _ROWS_ENTRIES:
        return "rows"
    for prefix in _FILE_PREFIXES:
        if name.startswith(prefix) and is_safe_relative_path(name[len(prefix):]):
            return "file"
    return None


# ------------------------------------------------------------------ manifest


@dataclasses.dataclass(frozen=True)
class TableEntry:
    """One ``rows/<table>.jsonl``: the column names its rows carry (sorted
    union), how many rows, and the entry's exact uncompressed size and sha256."""

    columns: tuple[str, ...]
    rows: int
    bytes: int
    sha256: str


@dataclasses.dataclass(frozen=True)
class FilesSummary:
    """The carried files (``files.jsonl`` lines) and the index entry itself."""

    count: int
    bytes: int
    index_bytes: int
    index_sha256: str


@dataclasses.dataclass(frozen=True)
class EmbeddingIdentity:
    """Which model produced one workload's vectors."""

    protocol: str
    model: str
    dim: int
    encoding: str = VECTOR_ENCODING


@dataclasses.dataclass(frozen=True)
class ArchiveManifest:
    format_version: int
    exported_at: str
    #: Diagnostics only (the schema pair and backend that wrote the file);
    #: never a compatibility gate.
    schema: Mapping[str, Any]
    #: Display fields of the exported notebook; ``id`` is the SOURCE
    #: deployment's id, for reference only (an import mints a new one).
    notebook: Mapping[str, Any]
    #: ``starts_dirty``: the export left Memory-derived rows out or read a
    #: dirty notebook, so an imported copy starts marked for a KG rebuild;
    #: ``clusters_dropped``: the source's clustering was out of date and no
    #: cluster row travels.
    kg_starts_dirty: bool
    kg_clusters_dropped: bool
    vectors_included: bool
    embeddings: Mapping[str, "EmbeddingIdentity | None"]
    tables: Mapping[str, TableEntry]
    files: FilesSummary
    global_object_types: tuple[Mapping[str, Any], ...]
    #: Mount edges the export did not carry (an import never mounts).
    excluded_mount_edges: int
    #: ``kg.concept_seed_version``: the concept seed space the carried cluster
    #: rows were built in (the source's published merge), written whenever the
    #: clusters were not dropped. Optional within format version 1: an archive
    #: written before the key existed lacks it, and an importer then cannot
    #: place new concepts next to the carried clusters and starts the notebook
    #: marked for a rebuild.
    kg_concept_seed_version: "int | None" = None
    format: str = FORMAT

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "format_version": self.format_version,
            "exported_at": self.exported_at,
            "schema": dict(self.schema),
            "notebook": dict(self.notebook),
            "kg": {
                "starts_dirty": self.kg_starts_dirty,
                "clusters_dropped": self.kg_clusters_dropped,
                **(
                    {}
                    if self.kg_concept_seed_version is None
                    else {"concept_seed_version": self.kg_concept_seed_version}
                ),
            },
            "embeddings": {
                "vectors_included": self.vectors_included,
                "workloads": {
                    workload: (
                        None if identity is None else dataclasses.asdict(identity)
                    )
                    for workload, identity in self.embeddings.items()
                },
            },
            "tables": {
                table: {
                    "columns": list(entry.columns),
                    "rows": entry.rows,
                    "bytes": entry.bytes,
                    "sha256": entry.sha256,
                }
                for table, entry in self.tables.items()
            },
            "files": dataclasses.asdict(self.files),
            "global_object_types": [dict(item) for item in self.global_object_types],
            "excluded": {
                "memory": MEMORY_NOT_CARRIED,
                "mount_edges": self.excluded_mount_edges,
            },
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "ArchiveManifest":
        """Validate a parsed ``manifest.json`` and build the manifest, or raise
        ``ManifestError`` naming the first field that is wrong. Unknown keys
        are ignored: they cannot make the reader do anything."""
        doc = _mapping(raw, "manifest")
        if doc.get("format") != FORMAT:
            raise ManifestError("这不是笔记本导出文件（format 字段不符）")
        version = _integer(doc.get("format_version"), "format_version")
        if version > max(READABLE_FORMAT_VERSIONS):
            raise ArchiveVersionTooNewError(
                f"导出文件的格式版本 {version} 比本部署支持的版本"
                f" {max(READABLE_FORMAT_VERSIONS)} 更新；请先升级本部署再导入"
            )
        if version not in READABLE_FORMAT_VERSIONS:
            raise ManifestError(f"不支持的导出文件格式版本 {version}")
        kg = _mapping(doc.get("kg"), "kg")
        embeddings = _mapping(doc.get("embeddings"), "embeddings")
        workloads = _mapping(embeddings.get("workloads"), "embeddings.workloads")
        if set(workloads) != set(EMBEDDING_WORKLOADS):
            raise ManifestError("embeddings.workloads 的工作负载集合不符")
        tables = _mapping(doc.get("tables"), "tables")
        if set(tables) != set(ARCHIVE_TABLES):
            raise ManifestError("tables 的表集合与本格式版本不符")
        files = _mapping(doc.get("files"), "files")
        excluded = _mapping(doc.get("excluded"), "excluded")
        if excluded.get("memory") != MEMORY_NOT_CARRIED:
            raise ManifestError("excluded.memory 字段不符")
        global_types = doc.get("global_object_types")
        if not isinstance(global_types, list):
            raise ManifestError("global_object_types 必须是列表")
        for index, item in enumerate(global_types):
            entry = _mapping(item, f"global_object_types[{index}]")
            _text(entry.get("object_type"), f"global_object_types[{index}].object_type")
        notebook = _mapping(doc.get("notebook"), "notebook")
        _text(notebook.get("name"), "notebook.name")
        return cls(
            format_version=version,
            exported_at=_text(doc.get("exported_at"), "exported_at"),
            schema=_mapping(doc.get("schema"), "schema"),
            notebook=notebook,
            kg_starts_dirty=_flag(kg.get("starts_dirty"), "kg.starts_dirty"),
            kg_clusters_dropped=_flag(kg.get("clusters_dropped"), "kg.clusters_dropped"),
            vectors_included=_flag(
                embeddings.get("vectors_included"), "embeddings.vectors_included"
            ),
            embeddings={
                workload: _identity(workloads[workload], workload)
                for workload in EMBEDDING_WORKLOADS
            },
            tables={
                table: _table_entry(tables[table], table) for table in ARCHIVE_TABLES
            },
            files=FilesSummary(
                count=_count(files.get("count"), "files.count"),
                bytes=_count(files.get("bytes"), "files.bytes"),
                index_bytes=_count(files.get("index_bytes"), "files.index_bytes"),
                index_sha256=_digest(files.get("index_sha256"), "files.index_sha256"),
            ),
            global_object_types=tuple(dict(item) for item in global_types),
            excluded_mount_edges=_count(excluded.get("mount_edges"), "excluded.mount_edges"),
            kg_concept_seed_version=(
                None if kg.get("concept_seed_version") is None
                else _count(kg.get("concept_seed_version"), "kg.concept_seed_version")
            ),
        )


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{field} 必须是对象")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ManifestError(f"{field} 必须是字符串")
    return value


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError(f"{field} 必须是整数")
    return value


def _count(value: Any, field: str) -> int:
    number = _integer(value, field)
    if number < 0:
        raise ManifestError(f"{field} 不能为负数")
    return number


def _flag(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestError(f"{field} 必须是布尔值")
    return value


def _digest(value: Any, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ManifestError(f"{field} 不是 sha256 十六进制摘要")
    return text


def _identity(value: Any, workload: str) -> "EmbeddingIdentity | None":
    if value is None:
        return None
    field = f"embeddings.workloads.{workload}"
    entry = _mapping(value, field)
    identity = EmbeddingIdentity(
        protocol=_text(entry.get("protocol"), f"{field}.protocol"),
        model=_text(entry.get("model"), f"{field}.model"),
        dim=_count(entry.get("dim"), f"{field}.dim"),
        encoding=_text(entry.get("encoding"), f"{field}.encoding"),
    )
    if identity.encoding != VECTOR_ENCODING:
        raise ManifestError(f"{field}.encoding 不受支持")
    return identity


def _table_entry(value: Any, table: str) -> TableEntry:
    field = f"tables.{table}"
    entry = _mapping(value, field)
    columns = entry.get("columns")
    if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
        raise ManifestError(f"{field}.columns 必须是字符串列表")
    return TableEntry(
        columns=tuple(columns),
        rows=_count(entry.get("rows"), f"{field}.rows"),
        bytes=_count(entry.get("bytes"), f"{field}.bytes"),
        sha256=_digest(entry.get("sha256"), f"{field}.sha256"),
    )

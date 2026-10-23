"""Read and verify a ``.snbk`` notebook archive before anything trusts it.

The preflight every reader runs first (``notebook inspect`` and the importer):
the zip's directory is checked against the protocol limits and the entry name
whitelist of ``app.domain.notebook_archive``, ``manifest.json`` is validated
field by field, and every entry the manifest and ``files.jsonl`` declare is
re-read and re-hashed -- sizes, sha256, row counts and the column names each
row carries. Nothing is extracted to disk here and nothing is written
anywhere; an archive that fails any check raises ``ArchiveError`` with an
operator-facing message.

The importer then reads the verified archive a second time, through
``ArchiveRowSource`` (the rows, page by page, each pass re-hashed against the
manifest) and ``copy_file_entry`` (one file into an already-open descriptor,
re-hashed against ``files.jsonl``), so a file changed on disk after the
preflight fails the import instead of slipping past it. Two import-only checks
live here too: ``check_columns`` (the column names are plain identifiers the
target has) and ``check_file_paths`` (no two ``files/**`` paths collide on a
case-insensitive or Unicode-normalising file system).

What is checked on the zip directory, before a single entry is decompressed:
entry count and total uncompressed size within the limits; every name is
``manifest.json``, ``files.jsonl``, ``rows/<archive table>.jsonl`` or a safe
relative path under ``files/notebooks/`` / ``files/assets/`` (no absolute path,
backslash, drive letter, ``..`` or empty segment, hence no directory entry);
no name appears twice; no entry is encrypted, compressed with anything but
stored/deflate, or marked as a symbolic link or other non-regular file; no
large entry inflates beyond the per-entry compression ratio.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import stat
import unicodedata
import zipfile
import zlib
from pathlib import Path
from typing import IO, Any, Callable, Iterable, Iterator, Mapping

from app.domain.notebook_archive import (
    ARCHIVE_TABLES,
    ASSET_FILES_DIR,
    COMPRESSION_RATIO_FLOOR_BYTES,
    FILES_DIR,
    FILES_INDEX_NAME,
    MANIFEST_NAME,
    MAX_ENTRIES,
    MAX_ENTRY_COMPRESSION_RATIO,
    MAX_JSONL_LINE_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_TOTAL_UNCOMPRESSED_BYTES,
    NOTEBOOK_FILES_DIR,
    ArchiveManifest,
    ManifestError,
    entry_kind,
    rows_entry,
)
from app.domain.portable_rows import DigestCounter, decode_row, is_safe_identifier

_READ_BLOCK = 1 << 20
#: The default line-byte budget of one ``ArchiveRowSource`` page (32 MiB).
PAGE_BYTES = 32 * 1024 * 1024
_ALLOWED_COMPRESSION = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
#: ``ZipInfo.create_system`` value of an archive written on a Unix host, the
#: only one whose external attributes carry a file mode.
_UNIX = 3


class ArchiveError(ValueError):
    """The archive is not a valid notebook archive (or not one this build can
    read). The message says what is wrong without quoting archive content."""


@dataclasses.dataclass(frozen=True)
class ArchiveInspection:
    path: Path
    manifest: ArchiveManifest
    entries: int
    uncompressed_bytes: int
    #: ``files.jsonl``: carried file entry name -> (size, sha256).
    files: Mapping[str, tuple[int, str]] = dataclasses.field(
        default_factory=dict, repr=False
    )

    def summary(self) -> dict[str, Any]:
        manifest = self.manifest
        return {
            "path": str(self.path),
            "format": manifest.format,
            "format_version": manifest.format_version,
            "exported_at": manifest.exported_at,
            "notebook": dict(manifest.notebook),
            "schema": dict(manifest.schema),
            "kg": {
                "starts_dirty": manifest.kg_starts_dirty,
                "clusters_dropped": manifest.kg_clusters_dropped,
                "concept_seed_version": manifest.kg_concept_seed_version,
            },
            "vectors_included": manifest.vectors_included,
            "embeddings": {
                workload: None if identity is None else dataclasses.asdict(identity)
                for workload, identity in manifest.embeddings.items()
            },
            "rows": {table: entry.rows for table, entry in manifest.tables.items()},
            "files": {"count": manifest.files.count, "bytes": manifest.files.bytes},
            "global_object_types": [item["object_type"] for item in manifest.global_object_types],
            "excluded_mount_edges": manifest.excluded_mount_edges,
            "entries": self.entries,
            "uncompressed_bytes": self.uncompressed_bytes,
        }


def open_archive(path: Path) -> zipfile.ZipFile:
    """Open ``path`` as a zip and check its directory (module docstring). The
    caller closes the returned file."""
    try:
        size = os.path.getsize(path)
        archive = zipfile.ZipFile(path, "r")
    except Exception as exc:  # noqa: BLE001 -- every parse failure is "not an archive"
        raise ArchiveError(f"无法作为导出文件打开: {type(exc).__name__}") from exc
    checked = False
    try:
        check_directory(archive, size)
        checked = True
    except ArchiveError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ArchiveError(f"导出文件目录无法解析: {type(exc).__name__}") from exc
    finally:
        if not checked:
            archive.close()
    return archive


def check_directory(archive: zipfile.ZipFile, archive_bytes: int) -> None:
    """The directory checks (module docstring). ``archive_bytes`` is the file's
    size on disk: the entries' compressed sizes cannot add up to more than the
    file holds unless entries overlap (the overlapping-entry zip bomb), whatever
    the interpreter's own zip reader would tolerate."""
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ArchiveError("导出文件的条目数超过上限")
    seen: set[str] = set()
    total = 0
    for info in infos:
        name = info.filename
        if name in seen:
            raise ArchiveError("导出文件含重复的条目名")
        seen.add(name)
        if entry_kind(name) is None:
            raise ArchiveError("导出文件含不允许的条目名")
        if info.flag_bits & 0x1:
            raise ArchiveError("导出文件含加密条目")
        if info.compress_type not in _ALLOWED_COMPRESSION:
            raise ArchiveError("导出文件含不支持的压缩方式")
        mode = info.external_attr >> 16
        if info.create_system == _UNIX and stat.S_IFMT(mode) not in (0, stat.S_IFREG):
            raise ArchiveError("导出文件含符号链接或特殊文件")
        total += info.file_size
        if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise ArchiveError("导出文件解压后的总量超过上限")
        if (
            info.file_size > COMPRESSION_RATIO_FLOOR_BYTES
            and info.file_size > MAX_ENTRY_COMPRESSION_RATIO * max(info.compress_size, 1)
        ):
            raise ArchiveError("导出文件含压缩比异常的条目")
    if sum(info.compress_size for info in infos) > archive_bytes:
        raise ArchiveError("导出文件的条目相互重叠或声明的大小超出文件本身")
    if MANIFEST_NAME not in seen:
        raise ArchiveError("导出文件缺少 manifest.json（导出未完成或文件已损坏）")


def read_manifest(archive: zipfile.ZipFile) -> ArchiveManifest:
    info = archive.getinfo(MANIFEST_NAME)
    if info.file_size > MAX_MANIFEST_BYTES:
        raise ArchiveError("manifest.json 超过大小上限")
    with archive.open(info) as handle:
        data = handle.read(MAX_MANIFEST_BYTES + 1)
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ArchiveError("manifest.json 不是合法的 JSON") from None
    try:
        return ArchiveManifest.from_dict(raw)
    except ManifestError as exc:
        raise ArchiveError(str(exc)) from None


def inspect_archive(path: Path) -> ArchiveInspection:
    """Every check of this module, on the whole archive (module docstring)."""
    path = Path(path)
    with open_archive(path) as archive:
        return inspect_open_archive(path, archive)


def inspect_open_archive(path: Path, archive: zipfile.ZipFile) -> ArchiveInspection:
    """``inspect_archive`` on a zip the caller keeps open (the importer reads
    the same open file again for the rows and files it writes)."""
    with archive_errors():
        return _inspect_open(path, archive)


class archive_errors:
    """Context manager: what the zip layer or a parser raises while reading a
    crafted or damaged archive becomes ``ArchiveError`` (never a traceback)."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, kind, exc, traceback) -> bool:
        if exc is None or isinstance(exc, ArchiveError):
            return False
        if isinstance(exc, (zipfile.BadZipFile, zlib.error, EOFError, OSError)):
            # A flipped byte inside a compressed entry surfaces as a CRC or
            # inflate error from the zip layer, never as a checksum mismatch.
            raise ArchiveError(f"导出文件已损坏: {type(exc).__name__}") from None
        if isinstance(exc, Exception):
            # NotImplementedError, RuntimeError, ValueError... from the zip
            # layer or a parser: the file is not one this build can read.
            raise ArchiveError(f"导出文件无法解析: {type(exc).__name__}") from exc
        return False


def _inspect_open(path: Path, archive: zipfile.ZipFile) -> ArchiveInspection:
    manifest = read_manifest(archive)
    declared = {MANIFEST_NAME, FILES_INDEX_NAME}
    for table in ARCHIVE_TABLES:
        name = rows_entry(table)
        declared.add(name)
        _verify_rows(archive, name, manifest.tables[table])
    files = _verify_files(archive, manifest)
    declared.update(files)
    extra = [info.filename for info in archive.infolist() if info.filename not in declared]
    if extra:
        raise ArchiveError(f"导出文件含 {len(extra)} 个未在清单中声明的条目")
    infos = archive.infolist()
    return ArchiveInspection(
        path=path,
        manifest=manifest,
        entries=len(infos),
        uncompressed_bytes=sum(info.file_size for info in infos),
        files=files,
    )


def _entry(archive: zipfile.ZipFile, name: str) -> zipfile.ZipInfo:
    try:
        return archive.getinfo(name)
    except KeyError:
        raise ArchiveError(f"导出文件缺少条目 {name}") from None


def _lines(handle: IO[bytes], name: str, digest: DigestCounter):
    """The entry's lines, each at most ``MAX_JSONL_LINE_BYTES``; every byte read
    feeds ``digest``."""
    while True:
        line = handle.readline(MAX_JSONL_LINE_BYTES + 1)
        if not line:
            return
        digest.update(line)
        if len(line) > MAX_JSONL_LINE_BYTES:
            raise ArchiveError(f"{name} 有一行超过单行上限")
        if not line.endswith(b"\n"):
            raise ArchiveError(f"{name} 的最后一行没有换行符（文件被截断）")
        yield line


def _json_object(line: bytes, name: str) -> dict:
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ArchiveError(f"{name} 含不合法的 JSON 行") from None
    if not isinstance(value, dict):
        raise ArchiveError(f"{name} 的每一行必须是 JSON 对象")
    return value


def _verify_rows(archive: zipfile.ZipFile, name: str, declared) -> None:
    info = _entry(archive, name)
    if info.file_size != declared.bytes:
        raise ArchiveError(f"{name} 的大小与清单声明不符")
    columns = frozenset(declared.columns)
    digest = DigestCounter()
    rows = 0
    with archive.open(info) as handle:
        for line in _lines(handle, name, digest):
            row = _json_object(line, name)
            if not set(row) <= columns:
                raise ArchiveError(f"{name} 有行携带清单未声明的列")
            rows += 1
    if digest.size != declared.bytes or digest.hexdigest() != declared.sha256:
        raise ArchiveError(f"{name} 的校验和与清单声明不符")
    if rows != declared.rows:
        raise ArchiveError(f"{name} 的行数与清单声明不符")


def _verify_files(
    archive: zipfile.ZipFile, manifest: ArchiveManifest
) -> dict[str, tuple[int, str]]:
    info = _entry(archive, FILES_INDEX_NAME)
    if info.file_size != manifest.files.index_bytes:
        raise ArchiveError("files.jsonl 的大小与清单声明不符")
    digest = DigestCounter()
    files: dict[str, tuple[int, str]] = {}
    with archive.open(info) as handle:
        for line in _lines(handle, FILES_INDEX_NAME, digest):
            entry = _json_object(line, FILES_INDEX_NAME)
            path, size, sha256 = entry.get("path"), entry.get("size"), entry.get("sha256")
            if (
                not isinstance(path, str) or entry_kind(path) != "file"
                or isinstance(size, bool) or not isinstance(size, int) or size < 0
                or not isinstance(sha256, str)
            ):
                raise ArchiveError("files.jsonl 含不合法的条目")
            if path in files:
                raise ArchiveError("files.jsonl 含重复的路径")
            files[path] = (size, sha256)
    if (
        digest.size != manifest.files.index_bytes
        or digest.hexdigest() != manifest.files.index_sha256
    ):
        raise ArchiveError("files.jsonl 的校验和与清单声明不符")
    if len(files) != manifest.files.count:
        raise ArchiveError("文件数与清单声明不符")
    if sum(size for size, _sha in files.values()) != manifest.files.bytes:
        raise ArchiveError("文件总大小与清单声明不符")
    for path, (size, sha256) in files.items():
        file_info = _entry(archive, path)
        if file_info.file_size != size:
            raise ArchiveError(f"{path} 的大小与文件清单不符")
        file_digest = DigestCounter()
        with archive.open(file_info) as handle:
            while True:
                block = handle.read(_READ_BLOCK)
                if not block:
                    break
                file_digest.update(block)
        if file_digest.size != size or file_digest.hexdigest() != sha256:
            raise ArchiveError(f"{path} 的校验和与文件清单不符")
    return files


# ------------------------------------------------------------------ import


def check_columns(
    manifest: ArchiveManifest, target_columns: Mapping[str, frozenset[str]]
) -> None:
    """Every column name the archive declares is a plain identifier (it becomes
    part of an INSERT statement) and exists in the target's table. An empty
    table declares ``[]``, which says nothing about the table's columns. A
    column the target lacks means the archive came from a newer build."""
    for table in ARCHIVE_TABLES:
        declared = manifest.tables[table].columns
        if not all(is_safe_identifier(column) for column in declared):
            raise ArchiveError(f"{rows_entry(table)} 的列名不合法")
        missing = sorted(set(declared) - set(target_columns.get(table, ())))
        if missing:
            raise ArchiveError(
                f"导出文件的 {table} 表含本部署没有的列（{len(missing)} 个）："
                "导出端版本较新，请先升级本部署再导入"
            )


def check_file_paths(paths: "Iterable[str]") -> None:
    """No two carried files may land on the same path of a case-insensitive or
    Unicode-normalising file system (macOS, Windows volumes): each path is
    compared NFC-normalised and lower-cased, and a file may not also be the
    directory of another file.

    ``str.lower()`` (simple, one-to-one case mapping), not ``str.casefold()``:
    APFS and NTFS compare names under a simple case mapping, so ``ß`` and
    ``ss`` are two different files there, while full case folding would refuse
    an archive that extracts fine."""
    seen: set[str] = set()
    parents: set[str] = set()
    for path in paths:
        key = unicodedata.normalize("NFC", path).lower()
        if key in seen or key in parents:
            raise ArchiveError("导出文件含大小写或 Unicode 归一化后重名的文件")
        seen.add(key)
        parts = key.split("/")
        for depth in range(1, len(parts)):
            parent = "/".join(parts[:depth])
            if parent in seen:
                raise ArchiveError("导出文件含大小写或 Unicode 归一化后重名的文件")
            parents.add(parent)


class ArchiveRowSource:
    """``CopyRowSource`` (``notebook_copy_writer``) over a verified archive.

    ``batches(table)`` opens the table's ``rows/<table>.jsonl`` entry again on
    every call (a zip entry can be re-read from its start) and yields its rows
    a page at a time, decoded (``{"$bytes": ...}`` back to bytes) and passed
    through ``adapt(table, row)`` (the target backend's value shapes). A page
    closes at ``page_rows`` rows or once its lines reach ``page_bytes``, so a
    page of very long rows (each up to ``MAX_JSONL_LINE_BYTES``) never holds
    more than about ``page_bytes`` plus one row; ``on_page()`` runs before
    each page is handed out (the importer beats its heartbeat there, also
    during the writer's read-only pre-passes) -- outside ``archive_errors``,
    so what it raises (a reaped import, a database error) reaches the caller
    as itself, never as a damaged file.
    Only one page is held at a time; the writer's ``CopyRowSource`` contract --
    a table's write pass is its last read -- holds because every read starts
    over from the file. Each pass re-hashes the entry and counts its rows, and
    a pass that does not end exactly at the manifest's sha256 and row count
    raises ``ArchiveError`` (the file changed since the preflight)."""

    def __init__(
        self,
        archive: zipfile.ZipFile,
        manifest: ArchiveManifest,
        *,
        adapt: "Callable[[str, dict], dict]",
        page_rows: int,
        page_bytes: int = PAGE_BYTES,
        on_page: "Callable[[], None]" = lambda: None,
    ) -> None:
        self._archive = archive
        self._manifest = manifest
        self._adapt = adapt
        self._page_rows = max(1, int(page_rows))
        self._page_bytes = max(1, int(page_bytes))
        self._on_page = on_page

    def batches(self, table: str) -> Iterator[list[dict]]:
        with contextlib.closing(self._pages(table)) as pages:
            for page in pages:
                self._on_page()
                yield page

    def _pages(self, table: str) -> Iterator[list[dict]]:
        """The pages of ``batches``, read (and re-verified) under ``archive_errors``."""
        declared = self._manifest.tables[table]
        name = rows_entry(table)
        columns = frozenset(declared.columns)
        with archive_errors():
            info = _entry(self._archive, name)
            digest = DigestCounter()
            count = page_size = 0
            page: list[dict] = []
            with self._archive.open(info) as handle:
                for line in _lines(handle, name, digest):
                    row = _json_object(line, name)
                    if not set(row) <= columns:
                        raise ArchiveError(f"{name} 有行携带清单未声明的列")
                    page.append(self._adapt(table, decode_row(row)))
                    count += 1
                    page_size += len(line)
                    if len(page) >= self._page_rows or page_size >= self._page_bytes:
                        yield page
                        page, page_size = [], 0
            if digest.size != declared.bytes or digest.hexdigest() != declared.sha256:
                raise ArchiveError(f"{name} 自预检后已被改动（校验和不符）")
            if count != declared.rows:
                raise ArchiveError(f"{name} 自预检后已被改动（行数不符）")
        if page or count == 0:
            yield page


def copy_file_entry(
    archive: zipfile.ZipFile, name: str, size: int, sha256: str, fd: int
) -> None:
    """Copy one ``files/**`` entry into ``fd`` (opened by the caller, written
    from its start), re-hashing it: a size or sha256 other than ``files.jsonl``
    declared raises ``ArchiveError``."""
    with archive_errors():
        info = _entry(archive, name)
        digest = DigestCounter()
        with archive.open(info) as handle:
            while True:
                block = handle.read(_READ_BLOCK)
                if not block:
                    break
                digest.update(block)
                _write_all(fd, block)
        if digest.size != size or digest.hexdigest() != sha256:
            raise ArchiveError(f"{name} 自预检后已被改动（校验和不符）")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


#: ``files/notebooks/`` and ``files/assets/``: where an import extracts from.
NOTEBOOK_FILES_PREFIX = f"{FILES_DIR}/{NOTEBOOK_FILES_DIR}/"
ASSET_FILES_PREFIX = f"{FILES_DIR}/{ASSET_FILES_DIR}/"

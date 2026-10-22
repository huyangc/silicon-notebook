"""Read and verify a ``.snbk`` notebook archive before anything trusts it.

The preflight every reader runs first (``notebook inspect`` today, the importer
next): the zip's directory is checked against the protocol limits and the entry
name whitelist of ``app.domain.notebook_archive``, ``manifest.json`` is
validated field by field, and every entry the manifest and ``files.jsonl``
declare is re-read and re-hashed -- sizes, sha256, row counts and the column
names each row carries. Nothing is extracted to disk here and nothing is
written anywhere; an archive that fails any check raises ``ArchiveError`` with
an operator-facing message.

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

import dataclasses
import json
import os
import stat
import zipfile
import zlib
from pathlib import Path
from typing import IO, Any

from app.domain.notebook_archive import (
    ARCHIVE_TABLES,
    COMPRESSION_RATIO_FLOOR_BYTES,
    FILES_INDEX_NAME,
    MANIFEST_NAME,
    MAX_ENTRIES,
    MAX_ENTRY_COMPRESSION_RATIO,
    MAX_JSONL_LINE_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_TOTAL_UNCOMPRESSED_BYTES,
    ArchiveManifest,
    ManifestError,
    entry_kind,
    rows_entry,
)
from app.domain.portable_rows import DigestCounter

_READ_BLOCK = 1 << 20
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
        try:
            return _inspect_open(path, archive)
        except ArchiveError:
            raise
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError) as exc:
            # A flipped byte inside a compressed entry surfaces as a CRC or
            # inflate error from the zip layer, never as a checksum mismatch.
            raise ArchiveError(f"导出文件已损坏: {type(exc).__name__}") from None
        except Exception as exc:  # noqa: BLE001 -- a crafted file never yields a traceback
            # NotImplementedError, RuntimeError, ValueError... from the zip
            # layer or a parser: the file is not one this build can read.
            raise ArchiveError(f"导出文件无法解析: {type(exc).__name__}") from exc


def _inspect_open(path: Path, archive: zipfile.ZipFile) -> ArchiveInspection:
    manifest = read_manifest(archive)
    declared = {MANIFEST_NAME, FILES_INDEX_NAME}
    for table in ARCHIVE_TABLES:
        name = rows_entry(table)
        declared.add(name)
        _verify_rows(archive, name, manifest.tables[table])
    declared.update(_verify_files(archive, manifest))
    extra = [info.filename for info in archive.infolist() if info.filename not in declared]
    if extra:
        raise ArchiveError(f"导出文件含 {len(extra)} 个未在清单中声明的条目")
    infos = archive.infolist()
    return ArchiveInspection(
        path=path,
        manifest=manifest,
        entries=len(infos),
        uncompressed_bytes=sum(info.file_size for info in infos),
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


def _verify_files(archive: zipfile.ZipFile, manifest: ArchiveManifest) -> set[str]:
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
    return set(files)

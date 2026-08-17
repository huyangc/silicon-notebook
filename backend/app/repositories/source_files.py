from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Iterable


def safe_filename(file_name: str) -> str:
    """Defuse directory traversal / separator smuggling in client-supplied
    names: keep only the final path component and flatten any remaining
    separators. Empty results fall back to ``source.bin``."""
    cleaned = Path(file_name).name.replace("/", "_").replace("\\", "_").strip()
    return cleaned or "source.bin"


# ext4/XFS/NTFS cap one path component at 255 BYTES. APFS/HFS+ count 255
# UTF-16 units instead, so an over-long name writes fine on a dev Mac and only
# fails in Linux production — as an OSError whose message carries the storage
# absolute path. Filesystem protocol boundary, not a tunable budget.
FILESYSTEM_NAME_MAX_BYTES = 255


def stored_upload_name(source_id: str, file_name: str) -> str:
    """The on-disk component for one uploaded source:
    ``{source_id}_{safe_filename(file_name)}``, kept within
    ``FILESYSTEM_NAME_MAX_BYTES`` by clipping the name's stem on ENCODED bytes
    (never mid-character). Browsers accept client file names up to 255 bytes
    of their own, so the composed component can reach 255 + prefix bytes.

    Only this DERIVED disk name is shortened; ``sources.file_name`` and
    ``sources.title`` keep the client's value whole, so nothing user-visible
    is truncated. The extension survives clipping because consumers dispatch
    on the STORED path's suffix (``read_source_text`` recognises
    .md/.markdown/.txt); an extension that cannot fit the budget by itself is
    garbage, treated as part of the stem and clipped with it.
    """
    name = safe_filename(file_name)
    prefix = f"{source_id}_"
    budget = FILESYSTEM_NAME_MAX_BYTES - len(prefix.encode("utf-8"))
    if len(name.encode("utf-8")) <= budget:
        return prefix + name
    suffix = Path(name).suffix
    stem_budget = budget - len(suffix.encode("utf-8"))
    if not suffix or stem_budget <= 0:
        suffix, stem_budget = "", budget
    stem = name[: len(name) - len(suffix)]
    clipped = stem.encode("utf-8")[:stem_budget].decode("utf-8", "ignore")
    # Byte clipping can strip a short stem to nothing (a lone multi-byte
    # character cut mid-sequence). The fallback is itself sliced to the stem
    # budget so the guarantee stays arithmetic rather than resting on ids
    # being fixed-width (`src-` + 32 hex leaves a ~218-byte budget today).
    return prefix + (clipped.strip() or "source"[:stem_budget]) + suffix


def delete_source_file(file_path: str) -> None:
    """Remove one stored source file, then its per-notebook directory when the
    file was the last one in it.  Shared by SourceFileStore.delete (the
    facade's `_delete_file` compatibility wrapper) and the notebook catalog's
    delete_notebook cleanup so source deletion keeps one implementation."""
    if not file_path:
        return
    path = Path(file_path)
    if path.exists() and path.is_file():
        path.unlink()
    notebook_dir = path.parent
    if notebook_dir.exists() and not any(notebook_dir.iterdir()):
        shutil.rmtree(notebook_dir, ignore_errors=True)


class SourceFileStore:
    """Filesystem persistence for uploaded source documents under
    ``storage_dir/notebooks/<notebook_id>/``.

    ``resolve_path`` is the database boundary's path resolver (Task 5): raw
    text reads accept both absolute stored paths and legacy repo-root-relative
    ones. Construction performs no I/O — directories are created per write,
    exactly as the facade's inline upload path always did."""

    def __init__(self, storage_dir: Path, *, resolve_path: Callable[[str], Path]) -> None:
        self.storage_dir = storage_dir
        self.resolve_path = resolve_path

    def write_upload(
        self,
        notebook_id: str,
        source_id: str,
        file_name: str,
        content: bytes,
    ) -> Path:
        source_dir = self.storage_dir / "notebooks" / notebook_id
        source_dir.mkdir(parents=True, exist_ok=True)
        stored_path = source_dir / stored_upload_name(source_id, file_name)
        stored_path.write_bytes(content)
        return stored_path

    def delete(self, file_path: str) -> None:
        delete_source_file(file_path)

    def read_source_text(
        self,
        file_path: str,
        fallback_elements: Iterable,
    ) -> str:
        """Raw document text for windowing: read the stored .md/.txt file when
        present, else reconstruct from element texts."""
        path = file_path or ""
        if path and (path.endswith(".md") or path.endswith(".markdown") or path.endswith(".txt")):
            try:
                resolved = self.resolve_path(path)
                return Path(resolved).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        return "\n\n".join(e.text for e in fallback_elements)

    def read_source(self, source, fallback_elements: Iterable) -> str:
        return self.read_source_text(
            getattr(source, "file_path", "") or "", fallback_elements
        )

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Iterable, Optional


def safe_filename(file_name: str) -> str:
    """Defuse directory traversal / separator smuggling in client-supplied
    names: keep only the final path component and flatten any remaining
    separators. Empty results fall back to ``source.bin``."""
    cleaned = Path(file_name).name.replace("/", "_").replace("\\", "_").strip()
    return cleaned or "source.bin"


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
        stored_path = source_dir / f"{source_id}_{safe_filename(file_name)}"
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

    def read_bytes(self, file_path: str) -> Optional[bytes]:
        """Raw bytes of a stored source file, or None when the path is empty or
        the file is gone. Used by the in-flight suffix-correction reconcile
        (SourceIngestionService._reconcile_pending_suffix): once the pipeline that
        was READING this file has settled, the file is repointed to the corrected
        name — and since the content is byte-identical (that is what dedup keys
        on), the corrected file is written from THESE bytes. A missing file
        degrades to None so the caller falls back to a name-only repoint rather
        than raising (same resolve_path boundary as read_source_text)."""
        path = file_path or ""
        if not path:
            return None
        try:
            return Path(self.resolve_path(path)).read_bytes()
        except OSError:
            return None

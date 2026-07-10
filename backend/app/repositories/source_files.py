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

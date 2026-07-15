"""Disk persistence + validation for notebook-scoped pasted-image assets
(knowhow-tables PR-1 Task 4: table cells embed images by reference to these).

Framework-agnostic like this package's sibling ``grid_parser.py``: validation
failures raise ``AssetValidationError`` (a ``ValueError`` subclass carrying a
Chinese-friendly message) rather than ``fastapi.HTTPException`` directly —
``routes.py`` owns the ``except ValueError`` -> HTTP 400 translation (the
same idiom already used throughout ``app/api/routes.py``) plus the
notebook-scoped auth guards. This module never touches SQL directly; it
calls the two Task 2 facade one-hop delegates (``insert_notebook_asset`` /
``get_notebook_asset``) for metadata and only does filesystem I/O itself.

Disk layout mirrors ``SourceFileStore`` (``storage_dir/notebooks/<id>/...``,
see ``app/repositories/source_files.py``) with a sibling
``storage_dir/assets/<notebook_id>/<asset_id>.<ext>`` — one file per asset,
named by the store-generated id (never the client-supplied filename), so
repeated uploads of the same name can't collide and a hostile filename can't
smuggle a path traversal into the on-disk location.
"""
from __future__ import annotations

from pathlib import Path

from app.repositories.source_files import safe_filename

MAX_ASSET_BYTES = 10 * 1024 * 1024  # 10MB

# The only mime types accepted for upload, mapped to their on-disk extension.
# `path_for` looks a stored asset's mime up here too, so any row that made it
# past `AssetService.save`'s validation always resolves to a real extension.
ALLOWED_MIME_EXTENSIONS: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    # image/svg+xml intentionally excluded: unsanitized SVG served same-origin is a stored-XSS vector (inline <script>/event-handler attrs); pasted screenshots are always raster.
}


class AssetValidationError(ValueError):
    """A user-visible upload rejection (unsupported mime / too large).

    Carries a Chinese-friendly message; ``routes.py``'s existing
    ``except ValueError as exc: raise HTTPException(400, str(exc))`` idiom
    maps this straight to a friendly HTTP 400 with no extra wiring needed.
    """


def validate_asset(mime: str, size: int) -> None:
    """Raise ``AssetValidationError`` if `mime`/`size` fail upload rules.

    Called before anything is written to the store or disk, so a rejected
    upload leaves zero trace (no orphan DB row, no orphan file).
    """
    if mime not in ALLOWED_MIME_EXTENSIONS:
        raise AssetValidationError(
            "图片类型不支持，仅支持 PNG/JPEG/GIF/WebP 格式"
        )
    if size > MAX_ASSET_BYTES:
        raise AssetValidationError("图片过大，最大支持 10MB")


class AssetService:
    """Validates, persists to disk, and records ``notebook_assets`` metadata
    for pasted knowhow-table-cell images."""

    def __init__(self, repo) -> None:
        """``repo`` is the SQLiteRepository facade (or anything duck-typed
        the same way): needs its public ``storage_dir`` attribute plus the
        ``insert_notebook_asset``/``get_notebook_asset`` one-hop delegates
        Task 2 already exposes."""
        self._repo = repo
        self._assets_root = Path(repo.storage_dir) / "assets"

    def path_for(self, asset: dict) -> Path:
        """The on-disk path for a stored asset row (does not check existence)."""
        ext = ALLOWED_MIME_EXTENSIONS.get(asset["mime"], "bin")
        return self._assets_root / asset["notebook_id"] / f"{asset['id']}.{ext}"

    def save(
        self, notebook_id: str, filename: str, mime: str, data: bytes, created_by: str
    ) -> dict:
        """Validate, record metadata, write bytes to disk.

        Raises ``AssetValidationError`` (mime/size) before anything is
        written. After the write, independently re-stats the file and raises
        ``RuntimeError`` if it didn't land correctly — a silent storage
        failure must never look like a successful upload.
        """
        validate_asset(mime, len(data))
        asset_id = self._repo.insert_notebook_asset(
            notebook_id, safe_filename(filename or "asset"), mime, len(data), created_by
        )
        asset = self._repo.get_notebook_asset(asset_id)
        if asset is None:  # pragma: no cover - insert_notebook_asset just returned this id
            raise RuntimeError(
                f"notebook asset {asset_id} missing immediately after insert_notebook_asset"
            )
        path = self.path_for(asset)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if not path.is_file() or path.stat().st_size != len(data):
            raise RuntimeError(
                f"asset {asset_id} did not persist correctly to disk at {path} "
                "(post-write existence check failed)"
            )
        return asset


__all__ = [
    "AssetService",
    "AssetValidationError",
    "ALLOWED_MIME_EXTENSIONS",
    "MAX_ASSET_BYTES",
    "validate_asset",
]

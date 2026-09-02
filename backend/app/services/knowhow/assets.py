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
from typing import Callable

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


def validate_asset(mime: str, size: int, max_bytes: int | None = None) -> None:
    """Raise ``AssetValidationError`` if `mime`/`size` fail upload rules.

    Called before anything is written to the store or disk, so a rejected
    upload leaves zero trace (no orphan DB row, no orphan file).

    ``max_bytes`` overrides the default pasted-image ceiling: source-image
    persistence is governed by the deployment's ``MINERU_MAX_IMAGE_BYTES``
    (which may legitimately exceed 10MB), and a fixed lower cap here would
    silently drop assets the documented setting promises to allow.
    """
    if mime not in ALLOWED_MIME_EXTENSIONS:
        raise AssetValidationError(
            "图片类型不支持，仅支持 PNG/JPEG/GIF/WebP 格式"
        )
    limit = MAX_ASSET_BYTES if max_bytes is None else max_bytes
    if size > limit:
        raise AssetValidationError(
            f"图片过大，最大支持 {max(1, limit // (1024 * 1024))}MB"
        )


class AssetService:
    """Validates, persists to disk, and records ``notebook_assets`` metadata
    for pasted knowhow-table-cell images."""

    def __init__(self, repo, *, notebook_exists: Callable[[str], bool] | None = None) -> None:
        """``repo`` is the SQLiteRepository facade (or anything duck-typed
        the same way): needs its public ``storage_dir`` attribute plus the
        ``insert_notebook_asset``/``get_notebook_asset`` one-hop delegates
        Task 2 already exposes.

        ``notebook_exists`` (codex #659 R6 P1) — optional injection point for
        the write-after-race recheck ``save``/``save_source_image`` do after
        writing bytes to disk (see those methods' docstrings for the race).
        Defaults to probing ``repo.get_notebook`` (the real facade's existing
        seam — raises ``KeyError`` for a missing/copying/deleting notebook,
        same as every other "notebook not found" call site in this
        codebase); when ``repo`` has no such seam (this module's own narrow
        unit tests' ``_FakeRepo``-shaped stand-ins), the recheck is skipped
        (always "still exists") rather than raising — those tests exercise
        disk I/O in isolation and were never wired to a real notebook
        lifecycle, so failing them closed here would be a false regression,
        not a caught bug."""
        self._repo = repo
        self._assets_root = Path(repo.storage_dir) / "assets"
        self._notebook_exists = notebook_exists or self._default_notebook_exists

    def _default_notebook_exists(self, notebook_id: str) -> bool:
        probe = getattr(self._repo, "get_notebook", None)
        if probe is None:
            return True
        try:
            probe(notebook_id)
        except KeyError:
            return False
        return True

    @staticmethod
    def _compensate_orphaned_write(path: Path) -> None:
        """codex #659 R6 P1: unlink a just-written file whose post-write
        liveness recheck found the notebook already gone/deleting, then
        remove the per-notebook asset directory if that was its last file
        (mirrors ``_delete_notebook_asset_dir``'s tolerance — best-effort,
        never raises on its own)."""
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass
        parent = path.parent
        try:
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass

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

        codex #659 R6 P1: after that, re-checks the notebook is still
        live. The race this closes: an upload that already passed its
        route's ``require_notebook_capability`` guard, then inserted the
        ``notebook_assets`` row, can still be mid-flight when a delete job's
        phase 4/finalize sweep runs its one-time asset-directory ``rmtree``
        (see ``notebook_delete.py``'s ``_sweep_ingestion_stragglers``) — this
        write's own ``mkdir(parents=True, exist_ok=True)`` a few lines up
        then silently RECREATES the directory that sweep just removed,
        landing a file with no cleanup path left (its row is either already
        cascaded away, or about to be with no further disk sweep coming).
        Covers both orderings: if this recheck passes, the delete job's
        tombstone has not landed yet, so the eventual phase-4/finalize sweep
        will still find and remove this file normally; if it fails (notebook
        gone or ``status='deleting'``), THIS call compensates immediately —
        unlinking the file it just wrote (and the now-empty per-notebook
        asset directory) before raising ``KeyError`` — the same "notebook
        not found" shape every other call site in this codebase raises,
        which ``routes.py`` already maps to a 404."""
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
        if not self._notebook_exists(notebook_id):
            self._compensate_orphaned_write(path)
            raise KeyError(notebook_id)
        return asset

    def save_source_image(
        self, notebook_id: str, source_id: str, filename: str,
        mime: str, data: bytes, created_by: str,
        max_bytes: int | None = None,
        *,
        on_row_created: Callable[[str], None] | None = None,
    ) -> dict:
        """存 MinerU 从来源抽出的内嵌图片：与 save() 同款校验+落盘，但带 source_id
        关联，供来源视图渲染与按源级联清理。护栏(大小/张数)由调用方(persist_image
        工厂)先行把关，这里仍做 mime/尺寸兜底校验，绝不放行不合规写盘；
        ``max_bytes`` 由调用方传部署的来源图片上限(codex R6：否则配置超过
        10MB 时被默认粘贴图上限静默压住)。

        ``on_row_created`` 在 ``notebook_assets`` 行**已经提交、文件还没写**的那一刻
        被调用一次，参数是新 asset id。行提交与写盘之间存在一个失败窗口：那之后抛出
        的异常会让调用方**永远拿不到这个 id**，而 `sweep_orphan_assets` 明确不回收带
        ``source_id`` 的行，于是它成为一条谁也够不着的孤儿。在线路径不传它（行为逐字
        不变）；离线的 `backfill-images` 用它把 id 记进自己的回滚名单，从而只删自己
        铸出来的资产——按"这一趟新出现的行"之类的差集去猜，会连并发的在线重解析刚建
        的合法资产一起删掉（那条路径的文件名同样是 ``<sha>.jpg``，形状上区分不开）。

        codex #659 R6 P1：写盘之后同样复核笔记本仍在——理由与 ``save()`` 的
        docstring 一字不差（同一个 ``mkdir(parents=True, exist_ok=True)`` +
        写后复核的形状），只是这次是来源解析产的内嵌图片而不是用户手动粘贴的
        单元格图片。复核不过时补偿删除刚写的文件（目录空则收尾）再抛
        ``KeyError``。"""
        validate_asset(mime, len(data), max_bytes=max_bytes)
        asset_id = self._repo.insert_notebook_asset(
            notebook_id, safe_filename(filename or "image"), mime, len(data),
            created_by, source_id=source_id,
        )
        if on_row_created is not None:
            on_row_created(asset_id)
        asset = self._repo.get_notebook_asset(asset_id)
        if asset is None:  # pragma: no cover
            raise RuntimeError(f"source image asset {asset_id} missing after insert")
        path = self.path_for(asset)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if not path.is_file() or path.stat().st_size != len(data):
            raise RuntimeError(f"source image asset {asset_id} did not persist to {path}")
        if not self._notebook_exists(notebook_id):
            self._compensate_orphaned_write(path)
            raise KeyError(notebook_id)
        return asset

    def delete_source_images(self, source_id: str) -> None:
        """删一个来源的全部内嵌图片：单次删除并返回 asset 元数据算盘上路径，
        再 unlink 文件。删盘 best-effort（文件先没了不阻塞行删除）。"""
        assets = self._repo.delete_source_asset_rows(source_id)
        for asset in assets:
            path = self.path_for(asset)
            try:
                if path.is_file():
                    path.unlink()
            except OSError:
                pass


__all__ = [
    "AssetService",
    "AssetValidationError",
    "ALLOWED_MIME_EXTENSIONS",
    "MAX_ASSET_BYTES",
    "validate_asset",
]

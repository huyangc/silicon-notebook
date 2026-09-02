"""来源内嵌图片持久化工厂（纯逻辑，便于单测）。

facade 的 _make_persist_image 绑定它到实例：per-source 一个闭包，带张数计数
与单图尺寸/mime 护栏；任何一步不合规返回 None（元素退化为 caption/占位文本，
绝不因图片问题阻塞文本解析）。"""
from __future__ import annotations

import mimetypes
from typing import Callable, Optional

from app.core.config import Settings
from app.services.knowhow.assets import ALLOWED_MIME_EXTENSIONS, AssetValidationError


def make_persist_image_factory(
    settings: Settings,
    asset_service_provider: Callable[[], object],
) -> Callable[[str, str, str], Optional[Callable[[bytes, str], Optional[str]]]]:
    def factory(notebook_id: str, source_id: str, created_by: str):
        if not settings.mineru_return_images:
            return None
        state = {"n": 0}

        def persist(img_bytes: bytes, img_name: str) -> Optional[str]:
            if len(img_bytes) > settings.mineru_max_image_bytes:
                return None
            if state["n"] >= settings.mineru_max_images_per_source:
                return None
            mime = mimetypes.guess_type(img_name)[0] or "image/jpeg"
            if mime not in ALLOWED_MIME_EXTENSIONS:
                return None
            try:
                asset = asset_service_provider().save_source_image(
                    notebook_id, source_id, img_name, mime, img_bytes, created_by,
                    max_bytes=settings.mineru_max_image_bytes)
            except (AssetValidationError, RuntimeError, OSError, KeyError):
                # KeyError (codex #659 R6 P1): the notebook was deleted/
                # deleting by the time the write-after-write recheck ran —
                # AssetService already compensated (unlinked the file,
                # cleaned the now-empty directory). Same "never block text
                # parsing over an image problem" degrade as every other
                # failure mode here.
                return None
            state["n"] += 1
            return asset["id"]

        return persist

    return factory

"""缓存模块的唯一公开面。

消费者只允许 `from app.core.cache import make_cache_backend, CacheBackend`。
具体实现类（SqliteCacheBackend 等）不对外导出——替换组件时只改本模块内部，
调用方零改动。该约束由 tests/test_cache_cohesion_guard.py 强制。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.cache.backend import CacheAdmin, CacheBackend, NoCacheBackend
from app.core.cache.policy import embed_key, llm_key

__all__ = [
    "CacheBackend", "CacheAdmin", "NoCacheBackend",
    "make_cache_backend", "llm_key", "embed_key",
]


def make_cache_backend(settings: Any) -> CacheBackend:
    """缓存的唯一诞生处：读开关、解析路径、选实现。

    换缓存组件只需改本函数——所有消费者都从这里取，不自行构造、不解析路径、
    不读配置项。
    """
    if not getattr(settings, "llm_cache_enabled", False):
        return NoCacheBackend()
    from app.core.cache.sqlite_backend import SqliteCacheBackend

    raw = getattr(settings, "llm_cache_path", "") or ".local/llm_cache_v2.db"
    path = Path(raw)
    if not path.is_absolute():
        # 相对路径锚定到仓库根。此规则归缓存模块所有，调用方不该知道。
        path = Path(__file__).resolve().parents[4] / raw
    return SqliteCacheBackend(
        str(path),
        size_limit=int(getattr(settings, "llm_cache_size_limit", 2 * 2**30)),
        ttl_seconds=float(getattr(settings, "llm_cache_ttl_days", 90)) * 86400.0,
    )

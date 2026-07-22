"""缓存模块的唯一公开面。

消费者只允许 `from app.core.cache import make_cache_backend, CacheBackend`。
具体实现类（SqliteCacheBackend 等）不对外导出——替换组件时只改本模块内部，
调用方零改动。该约束由 tests/test_cache_cohesion_guard.py 强制。
"""
from __future__ import annotations

from app.core.cache.backend import CacheAdmin, CacheBackend, NoCacheBackend

__all__ = ["CacheBackend", "CacheAdmin", "NoCacheBackend"]

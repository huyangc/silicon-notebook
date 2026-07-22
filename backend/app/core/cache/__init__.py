"""缓存模块的唯一公开面。

消费者只允许 `from app.core.cache import make_cache_backend, CacheBackend`。
具体实现类（SqliteCacheBackend 等）不对外导出——替换组件时只改本模块内部，
调用方零改动。该约束由 tests/test_cache_cohesion_guard.py 强制。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.cache.backend import CacheAdmin, CacheBackend, NoCacheBackend
from app.core.cache.policy import embed_key, is_cacheable_llm_response, llm_key
# 仓库根锚点的**唯一定义点**。不在这里重新 `parents[N]` 数一遍目录层数：那是第
# 二套锚定机制，与 config 的口径会各自漂移（「双 .local」事故正是同一个病）。
# 同款复用见 llm_logging.py 从 event_logging 取 _ROOT_DIR。
from app.core.config import _ROOT_DIR

__all__ = [
    "CacheBackend", "CacheAdmin", "NoCacheBackend",
    "make_cache_backend", "llm_key", "embed_key", "is_cacheable_llm_response",
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
        # 相对路径锚定到**仓库根**（不是 CWD）：`npm run dev` 会 cd 进 backend/
        # 再起 uvicorn，离线 CLI 从仓库根跑——按 CWD 解析会分裂成两个 .local，
        # 服务端与 CLI 各写各的缓存（本仓库栽过三次的「双 .local」事故）。
        # 此规则归缓存模块所有（调用方不该知道），但锚点复用 config._ROOT_DIR。
        path = _ROOT_DIR / raw
    return SqliteCacheBackend(
        str(path),
        size_limit=int(getattr(settings, "llm_cache_size_limit", 2 * 2**30)),
        ttl_seconds=float(getattr(settings, "llm_cache_ttl_days", 90)) * 86400.0,
    )

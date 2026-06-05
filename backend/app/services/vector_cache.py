"""进程内每-notebook 向量字典缓存（单用户单进程足够）。
版本键变化（向量行数/最新时间戳）即自动重载；删除时可显式 invalidate。"""
from __future__ import annotations

from typing import Callable, Dict, Hashable, Tuple


class VectorCache:
    def __init__(self) -> None:
        self._store: Dict[str, Tuple[Hashable, dict]] = {}

    def get(self, key: str, version: Hashable, loader: Callable[[], dict]) -> dict:
        cached = self._store.get(key)
        if cached is not None and cached[0] == version:
            return cached[1]
        value = loader()
        self._store[key] = (version, value)
        return value

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)

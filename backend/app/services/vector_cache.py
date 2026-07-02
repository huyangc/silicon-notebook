"""进程内每-notebook 向量字典缓存（单用户单进程足够）。
版本键变化（向量行数/最新时间戳）即自动重载；删除时可显式 invalidate。

single-flight: 同 key 并发 miss 只有一个线程跑 loader()，其余线程排队等待
同一个结果，避免分钟级 / GB 级构建被并行触发 K 次（瞬时内存 ×K）。

LRU: OrderedDict 保序，命中 move_to_end 刷新新鲜度，超过 max_entries 淘汰
最旧条目——只影响性能（淘汰后下次重新构建），不影响正确性。

锁次序（避免死锁）: 任何时候都不持有 per-key 锁去申请全局锁；全局锁只保护
锁表 / _store 的结构性操作（增删条目、移动顺序），从不在全局锁内执行 loader。
拿全局锁 -> 释放全局锁 -> 拿 per-key 锁 -> 跑 loader（不持全局锁）-> 拿全局锁
写回结果，这样全局锁与 per-key 锁不会同时被同一线程持有去等待对方，不构成
环路等待。

锁表回收: per-key 锁条目在使用计数归零（没有线程在用它）时立即从锁表中
pop，不无界增长；仍在使用（in-flight load 或排队等待者）时保留。
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Callable, Dict, Hashable, Tuple


class _KeyLockEntry:
    __slots__ = ("lock", "refcount")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.refcount = 0


class VectorCache:
    def __init__(self, max_entries: int = 32) -> None:
        self._store: "OrderedDict[str, Tuple[Hashable, dict]]" = OrderedDict()
        self._max_entries = max_entries
        self._global_lock = threading.Lock()
        self._key_locks: Dict[str, _KeyLockEntry] = {}

    def _acquire_key_lock(self, key: str) -> _KeyLockEntry:
        with self._global_lock:
            entry = self._key_locks.get(key)
            if entry is None:
                entry = _KeyLockEntry()
                self._key_locks[key] = entry
            entry.refcount += 1
        return entry

    def _release_key_lock(self, key: str, entry: _KeyLockEntry) -> None:
        with self._global_lock:
            entry.refcount -= 1
            if entry.refcount <= 0 and self._key_locks.get(key) is entry:
                del self._key_locks[key]

    def get(self, key: str, version: Hashable, loader: Callable[[], dict]) -> dict:
        with self._global_lock:
            cached = self._store.get(key)
            if cached is not None and cached[0] == version:
                self._store.move_to_end(key)
                return cached[1]

        entry = self._acquire_key_lock(key)
        try:
            with entry.lock:
                # double-check：等待锁期间可能已被别的线程构建好了。
                with self._global_lock:
                    cached = self._store.get(key)
                    if cached is not None and cached[0] == version:
                        self._store.move_to_end(key)
                        return cached[1]

                # loader 绝不在全局锁内运行。
                value = loader()

                with self._global_lock:
                    self._store[key] = (version, value)
                    self._store.move_to_end(key)
                    while len(self._store) > self._max_entries:
                        self._store.popitem(last=False)
                return value
        finally:
            self._release_key_lock(key, entry)

    def invalidate(self, key: str) -> None:
        with self._global_lock:
            self._store.pop(key, None)

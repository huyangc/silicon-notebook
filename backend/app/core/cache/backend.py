"""缓存后端的接口契约。

分两层是刻意的：CacheBackend 只有 get/put 两个必需方法，任何简单 KV 组件都能
实现；运维能力归入可选的 CacheAdmin，由调用方用 isinstance 探测。若把 evict_tag/
stats 并入必需接口，将来换任何 KV 组件都得先补齐管理方法，可替换性即告失效。
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class CacheBackend(Protocol):
    """内容寻址缓存的最小契约：key -> 字符串值。

    `tag` 是可选参数，不支持分组的实现直接忽略即可——降级为"无法按 tag 清空",
    不影响正确性。
    """

    def get(self, key: str) -> Optional[str]: ...

    def put(self, key: str, value: str, tag: str = "") -> None: ...


@runtime_checkable
class CacheAdmin(Protocol):
    """可选的运维能力。后端未实现时，管理入口如实降级提示。"""

    def evict_tag(self, tag: str) -> int: ...

    def clear(self) -> int: ...

    def stats(self) -> dict: ...


class NoCacheBackend:
    """永远 miss。用于测试隔离与显式关闭缓存的场合。"""

    def get(self, key: str) -> Optional[str]:
        return None

    def put(self, key: str, value: str, tag: str = "") -> None:
        pass

    # NoCacheBackend 顺带实现 CacheAdmin（零成本）。但这**不意味着** stats/
    # evict_tag 是必需能力：只实现 CacheBackend 两个方法的后端（例如把 TTL 与
    # LRU 交给服务端配置的 Redis 后端）是完全合法的。消费侧必须
    # isinstance(backend, CacheAdmin) 探测后再调用，见 test_cache_admin_is_optional。
    def evict_tag(self, tag: str) -> int:
        return 0

    def clear(self) -> int:
        return 0

    def stats(self) -> dict:
        return {"entries": 0, "bytes": 0, "by_tag": {},
                "hits": 0, "misses": 0, "hit_rate": 0.0}

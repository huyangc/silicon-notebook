"""缓存模块的唯一公开面。

消费者只允许 `from app.core.cache import make_cache_backend, CacheBackend`。
具体实现类（SqliteCacheBackend 等）不对外导出——替换组件时只改本模块内部，
调用方零改动。该约束由 tests/test_cache_cohesion_guard.py 强制。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict

from app.core.cache.backend import CacheAdmin, CacheBackend, NoCacheBackend
from app.core.cache.policy import (
    embed_key,
    embedding_batch_dim,
    is_cacheable_embedding,
    is_cacheable_llm_response,
    llm_key,
)
# 仓库根锚点的**唯一定义点**。不在这里重新 `parents[N]` 数一遍目录层数：那是第
# 二套锚定机制，与 config 的口径会各自漂移（「双 .local」事故正是同一个病）。
# 同款复用见 llm_logging.py 从 event_logging 取 _ROOT_DIR。
from app.core.config import _ROOT_DIR

__all__ = [
    "CacheBackend", "CacheAdmin", "NoCacheBackend",
    "make_cache_backend", "llm_key", "embed_key", "is_cacheable_llm_response",
    "embedding_batch_dim", "is_cacheable_embedding",
]


# 同一个缓存文件在本进程内只允许存在**一个** backend 实例，按解析后的绝对路径
# 记忆化。锁保护「查-建-存」这一组，让并发的两个消费者不会各自建出一个实例。
_INSTANCES: Dict[str, CacheBackend] = {}
_INSTANCES_LOCK = threading.Lock()


def make_cache_backend(settings: Any) -> CacheBackend:
    """缓存的唯一诞生处：读开关、解析路径、选实现、**按路径复用实例**。

    换缓存组件只需改本函数——所有消费者都从这里取，不自行构造、不解析路径、
    不读配置项。

    **为什么必须记忆化**：生产上的消费者是「每个 OpenAICompatibleClient
    （system llm / kg_llm / rewrite / reasoning + per-user）各一个 + embedder 一个」。
    每次调用都 new 一个实例的话，它们共享同一个文件，而 SqliteCacheBackend 的锁是
    **实例私有**的——`put()` 里「SELECT size → INSERT → UPDATE meta」这组读改写在
    实例之间没有互斥。实测三个后果：

    1. **计量漂移**：4 实例 × 4 线程 × 400 次同 key put → meta=54000 real=50000
       （8%）。且漂移只累积不自愈。
    2. **漂移驱动淘汰**：meta 虚高越过 size_limit 后，下一次 put 会为了满足幻影
       字节把健康条目**全部**淘汰（实测 entries 8 → 0），而 meta 仍虚高，缓存就
       此永久停在 0 条。
    3. **fd 放大**：连接是 per-实例 per-线程的。4 实例 × 4 线程 = 16 连接 = 37 个
       fd（db+wal+shm）。生产形态 ~5 实例 × 请求线程池 + embed/KG worker 会到
       100+ 连接 / 200+ fd，而本仓库有 EMFILE 事故史。

    记忆化一处同时解决三件事：单锁（无漂移）、fd 降到 O(线程数)、hits/misses 变成
    全进程口径而不是被切成 N 份。

    记忆化的键**只有路径**，刻意不含 size_limit/ttl：那两个是全局配置（不随用户
    变化），而「同一个文件上有两个各持不同上限的实例」比「后一次配置被忽略」危险
    得多——它正是上面第 2 条的成因。

    构造失败不入表：坏文件下次调用仍会照常抛（见
    test_make_embedder_survives_unusable_cache_file 依赖的行为）。
    """
    if not getattr(settings, "llm_cache_enabled", False):
        # NoCacheBackend 无状态、无 fd，不必记忆化。
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
    # 记忆化的键必须是**规范化后**的绝对路径：同一个文件既可能以相对路径（经上面
    # 锚定）也可能以绝对路径传进来，不归一化就会各记一份，等于没记忆化。
    key = str(path.resolve())
    with _INSTANCES_LOCK:
        existing = _INSTANCES.get(key)
        if existing is not None:
            return existing
        # 构造放在锁内：放到锁外并发两个线程会各建一个实例，其中一个被丢弃前
        # 已经被那个线程拿去用了——正是记忆化要消除的双实例形态。
        backend = SqliteCacheBackend(
            str(path),
            size_limit=int(getattr(settings, "llm_cache_size_limit", 2 * 2**30)),
            ttl_seconds=float(getattr(settings, "llm_cache_ttl_days", 90)) * 86400.0,
        )
        _INSTANCES[key] = backend
        return backend

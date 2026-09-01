"""Notebook 规模判定(copyable / 是否该建索引)与它的进程内 copy-stats memo。

热路径修复批 2 · R2-2(审计 ASK-4)——``copy_stats`` 的缓存从共享的
``VectorCache`` 搬到本模块自己的有界 memo:

现场:``copy_stats`` 的冷载是 ``load_notebook_scale_facts`` 的**五条整表聚合**
(sources 的 ``SUM(file_size)`` 与 ``COUNT``、chunks / knowledge_objects /
knowledge_relations 各一条 ``COUNT``,见两个 QueryStore 的同名方法),而它此前
存在全进程共用、**32 条**上限的 ``VectorCache`` 里(``VECTOR_CACHE_MAX_ENTRIES``)。
一次提问要问它 5–10 次(``_federated_graph_is_large`` 每参与库一次、
``_lexical_knn_allowed``、chunk 暴力守卫、``requires_index`` 等),而单个大库
自己就要占掉那 32 条里的十几条——两个活跃库互相挤兑时,被淘汰的恰好是这类
「冷载最贵」的条目,于是每次提问都重付一遍五条整表聚合。

改法:per-notebook 有界 memo(默认 512 个 notebook,每条值是一个几个整数的小
dict),形态照 ``postgres/knowledge_counts_cache.py``:命中判据是**版本键相等**,
写回前用 ``(全局 epoch, 该 notebook 的 epoch)`` 二元组复核这期间没被 invalidate。
本模块与方言无关(facts 由两个后端各自的 store 提供),所以 sqlite / postgres
两侧天然是同一份实现,不存在孪生分叉。

⚠ 这份状态是 **runtime-owned,不是模块级全局**(codex PR#634 R2 P2-2)。判据是
``docs/development.md`` 的组合契约:「``RepositoryRuntime`` owns or references
composed runtime state;``REPORT_CANCELLATIONS`` remains the intentionally
process-global canonical owner……Other mutable operational state (…and artifact
caches) is runtime-owned」—— 唯一被特许的进程级可变工件是
``REPORT_CANCELLATIONS``,这个不是。第一版把 memo 写成模块级 dict,同进程里两个
runtime 实例(shadow 迁移、``PostgresRepository`` 与 ``SQLiteRepository`` 并存的
维护流程、测试)会在同一个 ``(notebook_id, version)`` 键上互读缓存、互等
``_pending``——跨 runtime 串味。现在整份状态收进 :class:`CopyStatsMemo`,由
``_build_retrieval_domain`` 每个 runtime 建一个,经 ``RetrievalSnapshotCache``
持有(它本来就是「冻结键族及其失效」的所有者,copy-stats 曾经就是它的一个键族)。

⚠ 版本键**刻意保持不变**:仍是 ``(version_for(nb), notebook_copy_max_bytes,
notebook_copy_max_rows)``。任务简报建议改成 ``kg_mutation_seq`` + 两个阈值,
核实后没有采用——那会**放宽**失效口径,是一处静默的语义变化:
``version_for`` 是 ``ScaleArtifactRuntime.version(nb)``,它自身按
``version_signal`` = ``(kg_mutation_seq, cluster_mutation_seq, settings_tail +
(mention_seq, indexing_pipeline_id, indexing_pipeline_version), kg_reset_epoch)``
记忆化(批 3·W1 PR-2 新增末位的 ``kg_reset_epoch``,见
``services/scale_artifact_runtime.py`` 的 ``version()``),再展开成五张表的
``COUNT``/``MAX``,epoch>0 时在返回的 list 末尾条件式追加
``["kg_reset_epoch", N]``。也就是说今天的失效信号里除了 kg_mutation_seq,
还有簇代次、mention 代次、管线身份、若干 settings 与清图代次;只用
kg_mutation_seq 当键,一次 rebuild(只 bump ``cluster_mutation_seq``)之后
copy_stats 会继续返回旧值,一次 delete_notebook_kg(只 bump
``kg_reset_epoch``、把 kg_mutation_seq 重置为 0)更是如此——这正是本模块沿用
``version_for(nb)`` 整体作键、不摘出 kg_mutation_seq 单独用的理由:凡是
``version()`` 判定为「变了」的,这里也天然判定为「变了」,不需要跟着每一次
version_signal 的分量变化单独维护。
本批的红线是「同值少扫」,所以这里只换存储、不换判据:失效口径逐字不变,省下的
纯粹是跨键族挤兑造成的冷载。``version_for`` 每次调用仍是那一条 ``version_signal``
主键读,与改造前完全一样,热路径没有新增查询。

失效链同样复用既有调用点:``RetrievalSnapshotCache.invalidate_kg``(每一次在线
KG 变更都汇流到它)原来 evict ``{nb}:copystats`` 这个 VectorCache 键,现在改调
本模块的 ``invalidate_copy_stats``。没有新增调用点,也没有新增契约。

⚠ 一处如实登记的取舍(评审 P2-1):**条目的驻留时长变长了**。版本键
``version_for`` 展开成 ``version_facts``,里面**没有 sources 表的信号**——一次
上传/删除只在它顺带 bump 了 ``kg_mutation_seq`` 时才让键变化。也就是说
``size.bytes`` / ``size.sources`` 本来就有一个已登记的陈旧窗口(深拷贝那道总量
闸因此由 ``NotebookSharingService.notebook_copy_stats`` 在上一层现查兜住,见
``copy_stats`` 处的注释)。改造前那个窗口有一部分是被 32 条共享 LRU 的**挤兑**
白捡回来的新鲜度——不是设计,是副作用;搬进专池之后这份副作用没有了。所以摄取
路径上补了一处显式失效:``SourceIngestionService._invalidate_corpus_scale_memos``
在既有的 ``invalidate_knowledge_counts`` 调用点上把 copy-stats 一并失效(服务层、
与后端无关、零新增查询、零新增调用点;走 runtime 注入的 ``invalidate_copy_stats``
回调,与 ``invalidate_knowledge_counts`` 同一注入形态,不再调模块函数)。它覆盖的是「上传/删除改了语料规模」这条
主路径;不经该路径的 sources 行改动(如改名)仍落在上述已登记窗口内,判据未变。
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Callable, Dict, Hashable, Optional, Protocol, Tuple

from app.core.config import Settings
from app.domain.notebook_scale import NotebookScaleFacts

# 有界 LRU 的默认上限。每条值是 {"copyable": bool, "size": {5 个 int}} —— 几百
# 字节,512 个 notebook 也就几十 KB,与它替换掉的 GB 级键族完全不是一个量级,
# 所以按 notebook 数量封顶就够,不需要字节预算(那是 R2-4 给 VectorCache 做的)。
_MAX_NOTEBOOKS = 512


class _PendingCopyStats:
    """一次在途冷载:值(或异常)算出来之后唤醒所有等待者。"""

    __slots__ = ("ready", "value", "error", "epoch")

    def __init__(self, epoch: Tuple[int, int]) -> None:
        self.ready = threading.Event()
        self.value: "Optional[dict]" = None
        self.error: "Optional[BaseException]" = None
        # 采样自 leader 进入时,写回守卫用它 —— 等待者不重复写回(见下)。
        self.epoch = epoch


class CopyStatsMemo:
    """``copy_stats`` 的 runtime-owned 有界 memo(版本键 + single-flight)。

    每个 ``RepositoryRuntime`` 一个实例(见模块 docstring 的所有权论证)。实例之间
    完全隔离:两个 runtime 同时活在一个进程里(shadow 迁移、并存的维护流程、测试)
    不会互读缓存,也不会互等对方的在途冷载。

    写回守卫语义与 ``knowledge_counts_cache`` 逐条对齐:``_global_epoch`` 只被
    ``invalidate(None)`` 与 ``_epochs`` 的淘汰推进;``_epochs`` 是 per-notebook
    代次,只被该 notebook 自己的 invalidate 推进,所以 A 库一次几秒级的冷聚合不会
    被 B 库的摄取误伤。``_epochs`` 淘汰时必须 fail closed(推进全局代次),否则被
    挤出去的 notebook 的代次会静默退回默认值 0,让它自己在途的写回被误判成「没被
    invalidate」、把 invalidate 之前的快照重新钉回来。
    """

    def __init__(self, max_notebooks: int = _MAX_NOTEBOOKS) -> None:
        self._lock = threading.Lock()
        self._max_notebooks = max_notebooks
        self._store: "OrderedDict[str, Tuple[Hashable, dict]]" = OrderedDict()
        self._global_epoch = 0
        self._epochs: "OrderedDict[str, int]" = OrderedDict()
        # single-flight(codex PR#634 R1 P2):同一个 ``(notebook_id, version)`` 的
        # 并发冷 miss 只跑一次五条整表聚合,其余线程等同一个结果。
        #
        # 这一半是 R2-2 搬家时**丢掉**的能力:旧的 ``VectorCache.get`` 有 per-key
        # single-flight,而 ``knowledge_counts_cache`` 那个被照抄的形态没有(它的
        # 冷查询是单条 GROUP BY,重复跑一次不致命)。copy-stats 的冷载是**五条**
        # 整表聚合,而它一次提问要被问 5–10 次(每参与库一次的
        # ``_federated_graph_is_large``、``_lexical_knn_allowed``、chunk 暴力守卫、
        # ``requires_index`` …),这些调用点在 reasoning/report 的并发扇出里是真
        # 并发的 —— 一次冷启动因此可能同时打出十几组五条整表聚合。
        #
        # 键是 ``(notebook_id, version)`` 而不是只有 notebook:版本不同就是两次
        # 不同的计算,不该互相等待、更不该互相复用结果。
        self._pending: "Dict[Tuple[str, Hashable], _PendingCopyStats]" = {}

    def _epoch_of(self, notebook_id: str) -> Tuple[int, int]:
        """采样 ``(全局 epoch, 该 notebook 的 epoch)``。调用方必须已持有锁。"""
        return (self._global_epoch, self._epochs.get(notebook_id, 0))

    def get(
        self, notebook_id: str, version: Hashable, compute: Callable[[], dict]
    ) -> dict:
        key = (notebook_id, version)
        while True:
            with self._lock:
                hit = self._store.get(notebook_id)
                if hit is not None and hit[0] == version:
                    self._store.move_to_end(notebook_id)
                    return hit[1]
                pending = self._pending.get(key)
                if pending is None:
                    pending = _PendingCopyStats(self._epoch_of(notebook_id))
                    self._pending[key] = pending
                    leader = True
                else:
                    leader = False

            if not leader:
                # 等待者:不跑聚合,拿 leader 的结果。leader 失败时同样醒来,并按
                # 「不缓存毒值」的约定各自重试(回到循环顶部,可能成为新的 leader)
                # —— 与 ``RetrievalRunState.memoized_embedding`` 的既有约定一致。
                pending.ready.wait()
                if pending.error is None and pending.value is not None:
                    return pending.value
                continue

            # 聚合绝不在锁内跑(冷载在生产大库上是秒级)。
            try:
                value = compute()
            except BaseException as exc:                 # noqa: BLE001
                with self._lock:
                    self._pending.pop(key, None)         # 有界:完成即清理
                pending.error = exc
                pending.ready.set()                      # 唤醒等待者后再抛
                raise

            with self._lock:
                # 写回守卫只由 **leader** 执行一次,用它自己进入时采样的 epoch:
                # 等待者根本没跑 compute,没有「读之后到写之前」这个窗口需要守,
                # 让它们再核一遍 epoch 只会在 leader 写回后又写一遍同一个值。
                if pending.epoch == self._epoch_of(notebook_id):
                    self._store[notebook_id] = (version, value)
                    self._store.move_to_end(notebook_id)
                    while len(self._store) > self._max_notebooks:
                        self._store.popitem(last=False)
                self._pending.pop(key, None)             # 有界:完成即清理
            pending.value = value
            pending.ready.set()
            return value

    def invalidate(self, notebook_id: "Optional[str]" = None) -> None:
        """清 memo(单 notebook 或全部)。

        非正确性必需——版本键本身已自失效——是与 ``{nb}:copystats`` 时代同款的
        安全阀:挡住「同一秒内的原地改动恰好让版本元组不变」这种边缘。
        ``notebook_id`` 为 ``None`` 时清空全部(运维与测试用)。
        """
        with self._lock:
            if notebook_id is None:
                self._global_epoch += 1
                self._store.clear()
                self._epochs.clear()
                # 在途 leader 的写回由它自己的 epoch 守卫拒掉;这里只清 memo,
                # 不动 ``_pending``——强行清掉会让等待者永远等不到 ``ready``。
                return
            self._epochs[notebook_id] = self._epochs.get(notebook_id, 0) + 1
            self._epochs.move_to_end(notebook_id)
            while len(self._epochs) > self._max_notebooks:
                self._epochs.popitem(last=False)
                self._global_epoch += 1   # fail closed,理由见类 docstring
            self._store.pop(notebook_id, None)

    def cached_version(self, notebook_id: str) -> Any:
        """当前驻留的版本键(没有则 ``None``)。只给测试与运维观察用。"""
        with self._lock:
            hit = self._store.get(notebook_id)
            return hit[0] if hit is not None else None


class NotebookScaleFactsRepository(Protocol):
    def load_notebook_scale_facts(self, notebook_id: str) -> NotebookScaleFacts: ...
    def is_mounted_by_anyone(self, notebook_id: str) -> bool: ...

class NotebookScaleProfile:
    def __init__(self, settings: Settings, facts: NotebookScaleFactsRepository, version_for: Callable[[str], Hashable], memo: CopyStatsMemo) -> None:
        # ``memo`` 是必填依赖,刻意不给「没传就自己新建一个」的默认值:Profile 在
        # 生产上是**每次调用现构造**的(见 ScaleArtifactRuntime.notebook_copy_stats
        # 与 repository_runtime 的 scale_profiles),一个私建的 memo 等于完全没有
        # 缓存 —— 那种默认值只会静默地把这项优化整个废掉。
        self.settings, self.facts_repo, self.version_for, self.memo = settings, facts, version_for, memo
    def facts(self, notebook_id: str) -> NotebookScaleFacts:
        return self.facts_repo.load_notebook_scale_facts(notebook_id)
    def copy_stats(self, notebook_id: str) -> dict:
        # bytes + chunks+nodes only — the cheap, KG-version-cached copyability
        # verdict retrieval reads on the hot path. The deep-copy total-
        # materialisation bound (which also depends on assets/sources that do NOT
        # bump this cache's version) is enforced FRESH one layer up, in the
        # share-routing service (NotebookSharingService.notebook_copy_stats), so it
        # can never go stale here (codex PR#354 r2 P2).
        #
        # 存储从 VectorCache 换成本模块的 per-notebook memo(R2-2),版本键与
        # 判据逐字不变——理由与取舍见模块 docstring。
        version = (self.version_for(notebook_id), self.settings.notebook_copy_max_bytes, self.settings.notebook_copy_max_rows)
        def load():
            f = self.facts(notebook_id)
            return {"copyable": f.bytes <= self.settings.notebook_copy_max_bytes and f.chunks + f.nodes <= self.settings.notebook_copy_max_rows, "size": f.as_size_dict()}
        return self.memo.get(notebook_id, version, load)
    def is_copyable(self, notebook_id: str) -> bool:
        return bool(self.copy_stats(notebook_id)["copyable"])
    def requires_index(self, notebook_id: str, *, has_disk_index: bool) -> bool:
        if self.is_copyable(notebook_id): return False
        return not has_disk_index
    def index_eligible(self, notebook_id: str, *, tier: str, has_disk_index: bool, total_chunks: int) -> bool:
        # 被任何笔记本挂载即构成建索引资格(Task 6)——镜像 ScaleArtifactRuntime.eligible
        # 的同一分支,两处必须保持一致(否则建索引与用索引的判定会分叉)。
        if tier == "base" or has_disk_index or self.facts_repo.is_mounted_by_anyone(notebook_id): return True
        if total_chunks > self.settings.index_suggest_chunk_threshold: return True
        return not self.is_copyable(notebook_id)

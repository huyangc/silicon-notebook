"""审核队列排名的 runtime-owned 有界 memo(R3 T-A2,热路径修复批 2;T-A3 v4 加入
``total``,codex #638 R1)。

现场(审计 KG-2,P0):``KnowledgeGovernanceService.review_queue`` 每一次请求都
把该 notebook 的**全部非 rejected 关系**取回(生产最大库 8.35M 行),在 Python 里
算 corroboration / trust / priority,再 ``heapq.nlargest(limit)`` 只交出 ~100 条。
更糟的是审核循环本身:每按一次「通过/存疑」都 bump ``kg_mutation_seq``,把
``_edge_centrality_map`` 的 version-cache 一并打失效——于是下一次取队列连
betweenness 都要重算。

T-A3 v4:队列的真实总量(``review_status != 'rejected'`` 的关系数)现在也存进
本 memo 的条目里,与排名 items 绑在同一个 seq 标签上——``compute`` 回调返回
``(items, total)``,``top()`` 也返回 ``(items, total)``。总量的初版曾经是
``knowledge_counts_cache`` 的第 5 个 module-global memo,codex #638 R1 指出那
会导致端点两次独立读产生 items/total 跨版本不一致的自相矛盾响应;v4 把 total
挪进这里,与 items 天然同一把锁、同一个 seq、同一次 compute,不可能跨版本。
carry 只 retag 标签,不碰 total(verified/pending 翻转不改变集合大小);
invalidate 连 items 带 total 一并清空(reject 类迁移可能真的改变了集合)。

本 memo 承担 KG-2 的**读侧主修复**,口径要如实说清楚:**降频不降幅**。冷算的
量级一点没变(仍是全量扫描 + 全量打分 + 一次 betweenness);变的是它从「每请求
一次」降到「每个拓扑版本一次」,而审核循环里最常见的 verified/pending 翻转
连版本都不算变(见下面的 carry)。设计评审 B1 实测否掉了 v1 设想的「ec>0 缩窄
候选」:rustworkx 的 ``digraph_edge_betweenness_centrality`` 对图内每条边恒正,
候选集合 = 全部边,``id=ANY`` 分批严格劣于现在的单次扫描。

## 失效完备性论证(键 = ``kg_mutation_seq``,别的都不看)

``review_queue`` 的输入**恰好**是三样:

1. 该 notebook 的非 rejected ``knowledge_relations`` 行;
2. 这些关系端点对象的 ``payload.name`` 与 ``object_type``;
3. edge betweenness centrality map。

``total`` 不是第四样输入——它就是 (1) 的 ``len()``,同一次扫描顺带算出来,不
额外读任何东西,因此失效完备性论证覆盖 items 也就覆盖了 total。

(1)(2) 的每一条**生产**写路径都汇流到 ``mark_unified_kg_dirty`` —— 它是
``kg_mutation_seq`` 在本仓库里唯一的前进点(``store_kg`` / 关系补全 /
KG job publish / ``set_edge_review`` / 删除路径,逐条抽查过)。(3) 是 (1)(2) 的
**纯函数**:``_edge_centrality_map`` 读关系行,也读端点的
``knowledge_objects(id, object_type)``(见 ``edge_centrality_source_rows``——
度数排名与有界子图的建图都要靠 ``object_type``),但连 ``payload`` 都不读(见
``repository_facade._edge_centrality_map`` 的 docstring);``knowledge_objects``
的写路径与(1)(2)走的是同一条 ``mark_unified_kg_dirty`` 前进点,所以仍在 seq
覆盖面内。它自己的 vector-cache 键另有 settings 维度,但 settings 变化伴随
进程重启,而本 memo 是 process-local,重启即空。

与 ``notebook_scale.py`` 拒绝「单用 ``kg_mutation_seq`` 当键」的先例**不冲突**:
那里的产物(copy_stats)还依赖 embedding 表、簇代次、mention 代次与若干 settings,
所以它保留了展开五张表的 ``version_for``;这里的产物不依赖那些,三样输入全部
落在 ``kg_mutation_seq`` 的覆盖面内。

**已知豁口(三处,都已堵)**——三处都不是「漏了一条 bump」,而是那条路径的 seq
**根本不前进**,所以 seq 闸对它们天然无效,必须显式失效:

1. ``RepositoryFacade.add_relations``(``repository_facade.py``)是 fixture/测试
   专用的裸插入,直接走 ``KnowledgeStorePort.add_relations_current``,**不** bump。
   照 ``knowledge_query.insert_test_object`` 的先例,该方法内显式调用本 memo 的
   ``invalidate``(与它已有的 ``invalidate_knowledge_counts`` 并排)。
2. ``KnowledgeLifecycleService.delete_notebook_kg`` 删掉 ``unified_kg_state``
   整行,于是 seq **归零重爬**——不是单调前进,而是**别名**:一次 delete + 重抽
   之后 seq 会重新爬回它离开时的那些值,与那时**完全不同**的图内容撞上同一个
   标签。这正是那里已经显式 ``invalidate_knowledge_counts`` 的原因(见该方法的
   注释),本 memo 在同一处并排失效。它比计数更需要这一手:计数在重爬途中被
   任何一次读覆盖,而排名条目只在真的有人取队列时才被覆盖,一次「删完直接重抽
   到底、中途没人看队列」就能撞上。
3. ``SourceIngestionService.run_extraction`` 在 ``preserve_existing=False`` 时
   走 ``begin_extraction_run`` → ``clear_source_graph_state``,删掉该源全部
   ``knowledge_relations``/``knowledge_objects``,同样**不** bump——它只清旧图,
   ``mark_unified_kg_dirty`` 要等后面 ``store_kg`` 真落新对象/关系才会被调用。
   与前两处**不同**:这不是 fixture 专用的裸写,也不是删库重建的运维路径,而是
   **每一次重新抽取**都会走到的常规用户操作(手动重解析/失败重试/批量摄取
   补跑皆同此)。清空旧图之后若抽取本身失败退出(最常见:没配模型),
   ``kg_mutation_seq`` 原地不动,暖过的排名 memo 会继续端出已经被删除的边——
   下一次对它 ``set_edge_review`` 必得 404。在
   ``SourceIngestionService._invalidate_corpus_scale_memos``(与 copy-stats/
   开路计数失效同一处调用点、同一注入形态)里显式 ``invalidate``。见
   ``backend/tests/test_source_ingestion_service.py::
   test_extraction_reset_without_seq_bump_invalidates_the_review_queue_memo``。

## 读序契约(硬)

冷算必须**先点读 seq、再取数据**。``top()`` 因此自己按这个顺序调用注入的
``read_seq`` 与 ``compute``,而不是让调用方递一个已经算好的 seq 进来——顺序是
本模块的不变量,不是调用方的自觉。

理由:这样得到的条目永远满足「**内容 ≥ 标签**」。取数期间若有写入提交,内容
可能新于标签,下一次读会因为 seq 不等而 miss、重算——多付一次冷算,方向保守。
反序(先取数后读 seq)会得到「陈旧内容 + 新鲜标签」:下一次读命中它,而且
``carry`` 还会把这份陈旧内容一路续成更新的版本,直到某次 rejected 迁移才被清掉。
变异锚点见 ``backend/tests/test_review_queue_memo.py::
test_cold_compute_must_read_the_seq_before_the_data``。

## carry-forward

``set_edge_review`` 允许任意迁移。**verified/pending 之间的翻转不改变任何排序
输入**(trust / corroboration / centrality 都不读 ``review_status``,逐链核实过),
所以这类迁移由 ``carry`` 处置:锁内比对 ``entry.seq == expected_seq``
(调用方传自己 bump 前观察到的 seq,即 ``new_seq - 1``),相等则 **copy-on-write**
—— 新 list、被改那一条换成新 dict —— 更新 ``review_status`` 并把标签挪到
``new_seq``;**该 rel 不在 top-M 内时同样 retag**(它的落榜与否只由 priority 决定,
而 priority 不含 ``review_status``);seq 不符则整条丢弃,绝不猜。

迁移**任一侧涉及 'rejected'**(含 rejected→rejected 的幂等写、以及
rejected→pending 这种「撤销拒绝」)会改变集合与拓扑,一律 ``invalidate``。

copy-on-write 是两件事的基础:``top()`` 的命中拷贝可以在**锁外**做(拿到的
list 引用不会被就地改写),而 ``carry`` 也不会改到任何已经交出去的引用。未被
改动的 item dict 在新旧两个 list 之间共享——安全,因为本模块从不就地改 item。

## epoch / LRU

形态照 ``postgres/knowledge_counts_cache.py`` 中 epoch 保护**最严**的那两个 memo
(pending / visible_pending),不照无 epoch 的 ``type_status_counts``:per-notebook
epoch 让 A 库的一次秒级冷算不被 B 库的摄取误伤;全局 epoch 只被 ``invalidate(None)``
与 ``_epochs`` 的淘汰推进。有界 LRU(≤512 本),**淘汰必须 fail closed**——被挤出
去的 notebook 的代次不能静默退回默认值 0,否则它自己在途的写回会被误判成「没被
invalidate」、把 invalidate 之前的快照重新钉回来。

本模块是 services 层的独立类,**不 import repositories 内部**:seq 由 runtime
注入的闭包点读(与 ``KnowledgeGovernanceService`` 的 ``kg_mutation_seq`` 是同一个
闭包)。实例 runtime-owned(``docs/development.md`` 的组合契约:除
``REPORT_CANCELLATIONS`` 外的可变运行状态一律 runtime-owned),由
``_build_knowledge_domain`` 每个 runtime 建一个。
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Callable, Dict, List, Optional, Tuple

# 缓存的排名深度。``review_queue(nb, limit)`` 在 ``0 <= limit <= M`` 时走 memo;
# 更大或负的 limit 直通冷路径(语义 = 现状)。M 必须覆盖两处未传 limit 的默认值——
# ``KnowledgeGovernanceService.review_queue``/``RepositoryFacade.review_queue``
# 的默认 ``limit=200`` 与路由 ``edge_review_queue`` 的默认 ``limit=100``——否则
# 最常见的「不传 limit」调用直接绕开 memo。切片等价性:``nlargest(M)`` 的前缀
# 就是 ``nlargest(limit)``——nlargest 的装饰键带一个严格递减的计数器,并列因此按
# 输入序解决,而输入序在前缀上不变。
REVIEW_QUEUE_MEMO_ITEMS = 200

# 有界 LRU 的默认上限。每条值是 ≤200(REVIEW_QUEUE_MEMO_ITEMS)个小 dict(十来个
# 标量字段)。诚实的量级(P1,codex 复审——「几十 MB」此前是猜的):深度遍历
# 实测——对满载 200 条 item 递归 ``sys.getsizeof`` 每个 dict/key/value 求和,
# 不是只对外层 list 调一次——单本约 0.34MB,按 128 本满载外推 ≈44MB;这是量级
# 上界,不是精确值,但足以确认它与它替换掉的「每请求一次 8.35M 行扫描」不是
# 一个量级。
_MAX_NOTEBOOKS = 128


class _PendingRanking:
    """一次在途冷算:值(或异常)算出来之后唤醒所有等待者。"""

    __slots__ = ("ready", "value", "error", "epoch")

    def __init__(self, epoch: Tuple[int, int]) -> None:
        self.ready = threading.Event()
        self.value: "Optional[Tuple[List[dict], int]]" = None
        self.error: "Optional[BaseException]" = None
        # 采样自 leader 进入时,写回守卫用它——等待者不重复写回(见 ``top``)。
        self.epoch = epoch


def _slice_copy(items: List[dict], limit: int) -> List[dict]:
    """交出去的永远是新 list + 逐 item 新 dict:调用方(乃至 API 序列化层)对返回
    值的任何改动都碰不到 memo 里的那份。item 的值都是标量,浅拷贝足够。"""
    return [dict(item) for item in items[:limit]]


class ReviewQueueMemo:
    """``review_queue`` 排名结果的 runtime-owned 有界 memo(seq 键 + single-flight)。

    每个 ``RepositoryRuntime`` 一个实例:两个 runtime 同时活在一个进程里(shadow
    迁移、并存的维护流程、测试)不会互读缓存,也不会互等对方的在途冷算。
    """

    def __init__(self, max_notebooks: int = _MAX_NOTEBOOKS) -> None:
        self._lock = threading.Lock()
        self._max_notebooks = max_notebooks
        self._store: "OrderedDict[str, Tuple[int, List[dict], int]]" = OrderedDict()
        self._global_epoch = 0
        self._epochs: "OrderedDict[str, int]" = OrderedDict()
        # single-flight:同一个 ``(notebook_id, seq)`` 的并发冷 miss 只跑一次全量
        # 扫描 + betweenness,其余线程等同一个结果。键含 seq:版本不同就是两次不同
        # 的计算,不该互相等待、更不该互相复用结果。
        self._pending: "Dict[Tuple[str, int], _PendingRanking]" = {}

    def _epoch_of(self, notebook_id: str) -> Tuple[int, int]:
        """采样 ``(全局 epoch, 该 notebook 的 epoch)``。调用方必须已持有锁。"""
        return (self._global_epoch, self._epochs.get(notebook_id, 0))

    def top(
        self,
        notebook_id: str,
        limit: int,
        read_seq: Callable[[], int],
        compute: Callable[[], Tuple[List[dict], int]],
    ) -> Tuple[List[dict], int]:
        """该 notebook 排名前 ``limit`` 条 + 队列真实总量(``limit <=
        REVIEW_QUEUE_MEMO_ITEMS``)。

        ``read_seq`` 点读 ``kg_mutation_seq``,``compute`` 算出**完整的**
        ``(top-M 列表, 总量)``(调用方负责让列表就是 ``REVIEW_QUEUE_MEMO_ITEMS``
        深,总量是同一次扫描里 ``len(非 rejected 关系行)``)。两者的调用顺序是本
        模块的读序契约,见模块 docstring。返回值的 total 与 items 永远同一个 seq
        版本——它们来自同一次 ``compute()``、存在同一个 store 条目里,不存在
        「items 命中缓存、total 另外算」这种会跨版本的路径。
        """
        seq = int(read_seq())        # ← 读序契约:seq 先于数据,不得交换
        key = (notebook_id, seq)
        with self._lock:
            hit = self._store.get(notebook_id)
            if hit is not None and hit[0] == seq:
                self._store.move_to_end(notebook_id)
                cached: "Optional[Tuple[List[dict], int]]" = (hit[1], hit[2])
                pending: "Optional[_PendingRanking]" = None
                leader = False
            else:
                cached = None
                pending = self._pending.get(key)
                leader = pending is None
                if leader:
                    pending = _PendingRanking(self._epoch_of(notebook_id))
                    self._pending[key] = pending

        if cached is not None:
            # 锁外拷贝:carry 是 copy-on-write,这份引用不会被就地改写。
            items, total = cached
            return _slice_copy(items, limit), total
        # 到这里 ``pending`` 必非 None:上面的 else 支要么取到在途的那个,
        # 要么自己建了一个。
        if not leader:
            # 等待者不跑冷算,拿 leader 的结果——包括失败:leader 抛出的异常
            # 原样继承给每一个在等的 follower(P2-4,codex 复审),不再各自转正、
            # 串行重跑同一次注定会失败的冷算——N 个并发 follower 曾经会把墙钟
            # 拉长到 N 倍单次冷算耗时,现在是 1 倍。失败之后 leader 已经把
            # ``_pending`` 条目摘掉(见下面 except 分支),所以下一次(非并发的)
            # 请求会重新从头当 leader——不做负缓存,不会把这次失败钉死。
            pending.ready.wait()
            if pending.error is not None:
                raise pending.error
            items, total = pending.value
            return _slice_copy(items, limit), total

        try:
            value = compute()    # 冷算绝不在锁内跑(大库上是秒级)
        except BaseException as exc:                     # noqa: BLE001
            with self._lock:
                self._pending.pop(key, None)             # 有界:完成即清理
            pending.error = exc
            pending.ready.set()                          # 唤醒等待者后再抛
            raise

        items, total = value
        with self._lock:
            # 写回守卫只由 **leader** 执行一次,用它自己进入时采样的 epoch:
            # 等待者没跑 compute,没有「读之后到写之前」这个窗口需要守。
            #
            # 单调守卫(P2-3,codex 复审):epoch 守卫只挡 invalidate() 打的失效,
            # 挡不住「慢 leader 用旧 seq 算出来的值,在一个更新的 seq 已经写回
            # 之后才姗姗来迟」——两次冷算可以并发在飞(seq 在第一次冷算跑到一半
            # 时前进,催生了第二个 ``(nb, new_seq)`` 的独立 single-flight,它跑
            # 得更快先写回)。旧 seq 的写回绝不能覆盖新 seq 已经落的条目。
            if pending.epoch == self._epoch_of(notebook_id):
                existing = self._store.get(notebook_id)
                if existing is None or existing[0] <= seq:
                    self._store[notebook_id] = (seq, items, total)
                    self._store.move_to_end(notebook_id)
                    while len(self._store) > self._max_notebooks:
                        self._store.popitem(last=False)
            self._pending.pop(key, None)                 # 有界:完成即清理
        pending.value = value
        pending.ready.set()
        return _slice_copy(items, limit), total

    def carry(
        self,
        notebook_id: str,
        expected_seq: int,
        new_seq: int,
        rel_id: str,
        status: str,
    ) -> None:
        """把该 notebook 的排名从 ``expected_seq`` 续到 ``new_seq``,顺带把
        ``rel_id`` 的 ``review_status`` 改成 ``status``。

        只有 verified<->pending 这类**不改变任何排序输入**的迁移可以调它(判据在
        ``KnowledgeGovernanceService.set_edge_review``);任一侧涉及 'rejected' 的
        迁移必须走 ``invalidate``。

        全程在锁内,所以不像 ``top`` 的写回那样存在「解锁—重算」的窗口:条目的
        seq 必须**严格等于** ``expected_seq``(调用方传自己 bump 前观察到的
        ``new_seq - 1``),任何不符——别的写者插了队,或这本从来没暖过——都 pop 掉
        而不是猜。下一次读多付一次冷算永远是安全的,续一个陈旧标签不是。

        ``rel_id`` 不在 top-M 内时**照样 retag**:它是否落榜只由 ``review_priority``
        决定,而 priority 不含 ``review_status``,所以这次迁移对榜单的内容与顺序
        都没有影响。此时唯一的改动就是标签。

        ``total``(T-A3 v4)**原样不变**——verified/pending 之间的翻转不改变
        ``review_status != 'rejected'`` 集合的大小,只有条目本身的字段变了,所以
        这里只搬运 ``hit[2]``,不重新计数。
        """
        with self._lock:
            hit = self._store.get(notebook_id)
            if hit is None:
                return
            if hit[0] != expected_seq:
                self._store.pop(notebook_id, None)
                return
            # copy-on-write:新 list + 被改那一条的新 dict。未被改动的 item 在新旧
            # 两个 list 之间共享引用——安全,本模块从不就地改 item。
            items = [
                {**item, "review_status": status}
                if item.get("rel_id") == rel_id else item
                for item in hit[1]
            ]
            self._store[notebook_id] = (new_seq, items, hit[2])
            self._store.move_to_end(notebook_id)

    def invalidate(self, notebook_id: "Optional[str]" = None) -> None:
        """清 memo(单 notebook 或全部)。

        seq 闸本身已经自失效,这里是安全阀:挡住「写已落、但它的 seq bump 尚未
        提交」这个边缘,以及三处 seq **根本不前进**的路径(fixture 裸插入 /
        notebook 删库重爬 / 抽取重置删图,见模块 docstring 的豁口一节)。

        不打断在途的 follower(P2-5,codex 复审):它们已经拿着这次
        ``top()`` 调用之前采样的 ``pending`` 引用等在 ``ready`` 上,本方法只清
        ``_store``/推进 epoch,不碰 ``_pending``——它们会等到 leader 算完、
        拿到**失效前**的那份值(memo 的读序契约本就保证这份值不会比它的标签更
        新,只是变旧了),而不是白等一个永远不会被 set 的 ``Event``。这个「失效
        与在途读交错」的窗口只存在于 seq **不动**的删除路径上:所有生产失效点
        都是「先推进 seq、再落新内容」同时完成的,窗口天然不存在;那些 seq 不动
        的路径已经在各自的调用点显式调了这里。``notebook_id`` 为 ``None`` 时
        清空全部(运维与测试用)。
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
                self._global_epoch += 1   # fail closed,理由见模块 docstring
            self._store.pop(notebook_id, None)

    def cached_seq(self, notebook_id: str) -> "Optional[int]":
        """当前驻留条目的 seq 标签(没有则 ``None``)。只给测试与运维观察用。"""
        with self._lock:
            hit = self._store.get(notebook_id)
            return hit[0] if hit is not None else None


__all__ = ["REVIEW_QUEUE_MEMO_ITEMS", "ReviewQueueMemo"]

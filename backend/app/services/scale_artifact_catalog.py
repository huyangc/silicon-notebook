"""Scale-index read catalog (Task 18).

Applies the exact / allow_stale version semantics over the on-disk scale
artifacts and lazily opens + memoizes the hnswlib ANN handles. READ-ONLY BY
CONSTRUCTION: the catalog holds no builder and never schedules a rebuild
merely because it reads — an active query can therefore never trigger a base
index rebuild (the base-offline-ANN / active-brute cost-separation
invariant). Serving a stale instance keeps being keyed on the DISK identity
(manifest.json version), so ingestion churn (kg_mutation_seq drift) never
forces a multi-GB ANN handle reload.

Interim composition (Task 18 → Task 20): the LRU cache and the cold-load
single-flight lock table resolve facade-late per call (tests reassign
``repo._scale_idx_cache``; Task 20 transfers that state into
ScaleArtifactRuntime by identity), the version key resolves the facade's
memoized ``_scale_index_version`` per call, and ``open_ann`` keeps the
manifest ``dim`` probe as the ANN dimension truth (漏一处消费点 = 静默零召回)
plus the fail-open None → caller-fallback semantics.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Callable

# ``_stale_manifest_admissible`` 的解析 memo 上限(每条只有两个字段,几十字节)。
_MANIFEST_IDENTITY_MEMO_MAX = 512


class _AnnLoadState:
    """One in-flight generation and its shared outcome for an ANN artifact."""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.loading = False
        self.generation = 0


class ScaleArtifactCatalog:
    def __init__(
        self,
        *,
        artifacts,
        settings,
        version: Callable,
        scale_cache: Callable,
        load_lock: Callable,
        load_locks: Callable,
        note_model_error: Callable,
        pipeline_identity: "Callable | None" = None,
    ) -> None:
        self.artifacts = artifacts
        self.settings = settings
        self.version = version
        self.scale_cache = scale_cache
        self.load_lock = load_lock
        self.load_locks = load_locks
        self.note_model_error = note_model_error
        self.pipeline_identity = pipeline_identity
        self._ann_lock_guard = threading.Lock()
        # R2-5:磁盘 manifest 的**身份投影** memo,键是文件的 stat 签名。
        self._manifest_identity_lock = threading.Lock()
        self._manifest_identity_memo: "OrderedDict[str, tuple]" = OrderedDict()

    def _manifest_identity(self, notebook_id: str) -> "dict | None":
        """``{"version": ..., "pipeline_identity": ...}`` —— 身份比对真正需要的
        那两个字段,按磁盘 stat 签名 memo(热路径修复批 2 · R2-5,审计 P1-15)。

        为什么是投影 + memo,而不是「轻读 manifest 头部」:JSON 没有部分解析,
        读出 ``version`` 就必然把整份(含 48k 元素、≈2MB 的 ``watermark_sources``)
        解析一遍。既有的 ``read_manifest_version`` 也不例外——它省的是**返回**
        整份 dict,不是解析。把 ``watermark_sources`` 拆到 manifest 旁的独立文件
        才能真正做到轻读,那是工件格式 + 迁移 + 读写两侧的改动,本批不做(登记为
        残余)。所以这里退而求其次:同一份磁盘工件只解析一次,之后按签名命中。

        签名与 fail-soft 语义见 ``ScaleArtifactStore.manifest_stat_signature``。
        解析失败(corrupt manifest)刻意**不进** memo:与仓库既有约定一致
        (``scale_manifest_identity`` 的 docstring:损坏结论不缓存),用户修好
        工件之后不必等任何东西过期。
        """
        directory = self.artifacts.scale_dir(notebook_id)
        signature = None
        probe = getattr(self.artifacts, "manifest_stat_signature", None)
        if callable(probe):
            signature = probe(directory)
            if signature is None:
                # 文件不在 = 无工件,与 read_manifest 的缺失分支同款早退。
                return None
            with self._manifest_identity_lock:
                cached = self._manifest_identity_memo.get(notebook_id)
                if cached is not None and cached[0] == signature:
                    self._manifest_identity_memo.move_to_end(notebook_id)
                    return cached[1]
        try:
            manifest = self.artifacts.read_manifest(directory)
        except (OSError, ValueError):
            return None
        if manifest is None or manifest.get("version") is None:
            return None
        identity = {
            "version": manifest.get("version"),
            "pipeline_identity": manifest.get("pipeline_identity"),
        }
        if signature is not None:
            with self._manifest_identity_lock:
                self._manifest_identity_memo[notebook_id] = (signature, identity)
                self._manifest_identity_memo.move_to_end(notebook_id)
                while len(self._manifest_identity_memo) > _MANIFEST_IDENTITY_MEMO_MAX:
                    self._manifest_identity_memo.popitem(last=False)
        return identity

    def _stale_manifest_admissible(self, notebook_id: str) -> "dict | None":
        """allow_stale 的磁盘 manifest 读取 + 管线身份闸(codex #602 R8 P1)。

        普通 stale(摄取造成的 kg_mutation_seq 漂移)刻意可服务——ANN 核=磁盘已
        索引部分,delta 由检索侧补。但**管线切换发布**不是普通 stale:全库 chunk id
        已重铸,旧工件的 ANN 命中指向已删行,喂它等于把检索打残到异步重建完成(或
        它失败后永远)。工件 manifest 的 `pipeline_identity` ≠ 当前已发布身份时按
        「无工件」处理,调用方回落 live 检索路径。legacy 工件缺该键——它们必然
        建于插件管线存在之前——按内建身份放行;corrupt manifest 保持 fail-soft。

        R2-5:返回的是 manifest 的**身份投影**(``version`` + ``pipeline_identity``)
        而不是整份 manifest —— 这两个字段就是本方法与它唯一的调用方 ``load()``
        (只读 ``.get("version")``)消费的全部。解析按磁盘签名 memo,见
        ``_manifest_identity``。管线身份的**数据库**一侧仍然每次现读(它是一次
        主键行读,而且发布切换必须立刻可见),只有磁盘那一侧被 memo。
        """
        identity = self._manifest_identity(notebook_id)
        if identity is None:
            return None
        if self.pipeline_identity is not None:
            from app.domain.indexing_pipeline import (
                BUILTIN_INDEXING_PIPELINE_VERSION,
            )
            artifact_identity = list(
                identity.get("pipeline_identity")
                or ["", BUILTIN_INDEXING_PIPELINE_VERSION]
            )
            if artifact_identity != list(self.pipeline_identity(notebook_id)):
                return None
        return identity

    def load(self, notebook_id: str, allow_stale: bool = False):
        """Return a valid ScaleIndex or None.

        exact(allow_stale=False):manifest.version == 当前 DB 版本 cur 才算有效,
        否则 None——viz/status 等要求与 DB 强一致的调用方语义不变。

        allow_stale=True(检索热路径「取磁盘已索引部分」):按**磁盘索引身份**
        (manifest.json 的 version)缓存复用。磁盘索引只在 rebuild/fold 时换,与
        kg_mutation_seq(每写 bump)无关——所以摄取造成 cur 漂移时,不再每查询重建
        stale 实例 + 重载 ~10GB ANN handle,而是复用同一进程缓存实例(handle memoize
        存活)。cold-load 走 per-nb 单飞锁,防 N 个并发查询各载 8GB 造成内存尖峰。
        stale-serve 与 scale_search_include_delta 无关地正确:ANN 核=磁盘已索引部分,
        flag=ON 时 delta 新鲜度来自检索侧 ⊕delta 暴力块,不来自这个核。"""
        cur = self.version(notebook_id)
        cached = self.scale_cache().get(notebook_id)
        if cached is not None and cached.manifest.get("version") == cur:
            return cached
        if not allow_stale:
            # version-exact:字节不变——load,manifest==cur 才 cache 并返回,否则 None。
            idx = self.artifacts.load_scale(notebook_id)
            if idx is None:
                return None
            if idx.manifest.get("version") == cur:
                self.scale_cache()[notebook_id] = idx
                return idx
            return None
        # allow_stale:按磁盘身份复用。cached 若仍是当前磁盘索引(其 version == 磁盘
        # manifest version)→ 直接返回(handle 存活,零重载)。管线身份闸见
        # _stale_manifest_admissible。
        disk_manifest = self._stale_manifest_admissible(notebook_id)
        if disk_manifest is None:
            return None   # 无索引 / 工件与已发布管线不同代
        disk_ver = disk_manifest.get("version")
        if cached is not None and cached.manifest.get("version") == disk_ver:
            return cached
        # cold:单飞加载。全局锁只护锁表,load 在 per-nb 锁内、不持全局锁。
        with self.load_lock():
            locks = self.load_locks()
            nb_lock = locks.get(notebook_id)
            if nb_lock is None:
                nb_lock = threading.Lock()
                locks[notebook_id] = nb_lock
        with nb_lock:
            # double-check:等锁期间别的线程可能已加载好当前磁盘索引。
            cached = self.scale_cache().get(notebook_id)
            disk_manifest = self._stale_manifest_admissible(notebook_id)
            if disk_manifest is None:
                return None
            disk_ver = disk_manifest.get("version")
            if cached is not None and cached.manifest.get("version") == disk_ver:
                return cached
            idx = self.artifacts.load_scale(notebook_id)
            if idx is None:
                return None
            self.scale_cache()[notebook_id] = idx
            return idx

    def open_ann(self, index, kind: str):
        """惰性 open + memoize hnswlib handle 到 ScaleIndex 实例(进程缓存,版本变→新实例→重开)。
        kind='kg'→ann.bin/ann_labels;'chunk'→chunk_ann.bin/chunk_ann_labels;
        'relation'→relation_ann.bin/relation_ann_labels。失败/无工件→None。"""
        import hnswlib
        _attr_by_kind = {"kg": "ann_handle", "chunk": "chunk_ann_handle",
                         "relation": "relation_ann_handle"}
        _path_by_kind = {"kg": "ann_path", "chunk": "chunk_ann_path",
                         "relation": "relation_ann_path"}
        _labels_by_kind = {"kg": "ann_labels", "chunk": "chunk_ann_labels",
                           "relation": "relation_ann_labels"}
        attr = _attr_by_kind[kind]
        cached = getattr(index, attr, None)
        if cached is not None:
            return cached
        path = getattr(index, _path_by_kind[kind], None)
        labels = getattr(index, _labels_by_kind[kind], None)
        if not path or not labels:
            return None
        # ScaleIndex is the cache identity. Keep one state per artifact kind on
        # that instance so concurrent reasoning subqueries share both a loaded
        # handle and a failed in-flight generation. A request arriving after a
        # failed generation may retry; its already-waiting followers reuse its
        # None outcome instead of serially reopening the same multi-GB file.
        with self._ann_lock_guard:
            ann_states = getattr(index, "_ann_load_states", None)
            if ann_states is None:
                ann_states = {}
                setattr(index, "_ann_load_states", ann_states)
            state = ann_states.get(kind)
            if state is None:
                state = _AnnLoadState()
                ann_states[kind] = state
        with state.condition:
            cached = getattr(index, attr, None)
            if cached is not None:
                return cached
            if state.loading:
                followed_generation = state.generation
                while state.loading and state.generation == followed_generation:
                    state.condition.wait()
                cached = getattr(index, attr, None)
                if cached is not None:
                    return cached
                # This caller joined an already-running generation. If that
                # generation (or a newer retry that overtook our wake-up) did
                # not publish a handle, share its fail-open None outcome.
                return None
            state.generation += 1
            state.loading = True
        try:
            from app.services.vector_index import resolve_runtime_dim as _rrd
            dim = int(index.manifest.get("dim", _rrd(self.settings) or self.settings.embed_dim))
            h = hnswlib.Index(space="cosine", dim=dim)
            h.load_index(path, max_elements=len(labels))
        except Exception as exc:  # noqa: BLE001 — fail-open
            with state.condition:
                state.loading = False
                state.condition.notify_all()
            self.note_model_error(f"scale_ann_open_{kind}", "", exc)
            return None
        with state.condition:
            setattr(index, attr, h)
            state.loading = False
            state.condition.notify_all()
        return h

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
from typing import Callable


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
    ) -> None:
        self.artifacts = artifacts
        self.settings = settings
        self.version = version
        self.scale_cache = scale_cache
        self.load_lock = load_lock
        self.load_locks = load_locks
        self.note_model_error = note_model_error
        self._ann_lock_guard = threading.Lock()

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
        # manifest version)→ 直接返回(handle 存活,零重载)。
        disk_ver = self.artifacts.read_manifest_version(
            self.artifacts.scale_dir(notebook_id))
        if disk_ver is None:
            return None   # 无索引
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
            disk_ver = self.artifacts.read_manifest_version(
                self.artifacts.scale_dir(notebook_id))
            if disk_ver is None:
                return None
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
        # ScaleIndex is the cache identity.  Keep one lock per artifact kind on
        # that instance so concurrent reasoning subqueries cannot each load the
        # same multi-GB hnsw file before any of them publishes the memoized
        # handle.  The second check is required after waiting for the winner.
        with self._ann_lock_guard:
            ann_locks = getattr(index, "_ann_load_locks", None)
            if ann_locks is None:
                ann_locks = {}
                setattr(index, "_ann_load_locks", ann_locks)
            kind_lock = ann_locks.get(kind)
            if kind_lock is None:
                kind_lock = threading.Lock()
                ann_locks[kind] = kind_lock
        with kind_lock:
            cached = getattr(index, attr, None)
            if cached is not None:
                return cached
            from app.services.vector_index import resolve_runtime_dim as _rrd
            dim = int(index.manifest.get("dim", _rrd(self.settings) or self.settings.embed_dim))
            try:
                h = hnswlib.Index(space="cosine", dim=dim)
                h.load_index(path, max_elements=len(labels))
            except Exception as exc:  # noqa: BLE001 — fail-open
                self.note_model_error(f"scale_ann_open_{kind}", "", exc)
                return None
            setattr(index, attr, h)
            return h

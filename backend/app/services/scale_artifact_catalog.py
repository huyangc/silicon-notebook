"""Scale-index read catalog (Task 18).

Applies the exact / allow_stale version semantics over the on-disk scale
artifacts and lazily opens + memoizes the hnswlib ANN handles. READ-ONLY BY
CONSTRUCTION: the catalog holds no builder and never schedules a rebuild
merely because it reads — an active query can therefore never trigger a base
index rebuild (the base-offline-ANN / active-brute cost-separation
invariant). Serving a stale instance keeps being keyed on the DISK identity
(manifest.json version), so ingestion churn (kg_mutation_seq drift) never
forces a multi-GB ANN handle reload.

W-CLI T-W3: that disk identity is (version value, manifest stat signature),
not the version value alone — an offline/off-machine rebuild can publish a
new artifact under an unchanged version, and a value-only comparison would
serve the superseded in-process instance forever. See ``load``.

Interim composition (Task 18 → Task 20): the LRU cache and the cold-load
single-flight lock table resolve facade-late per call (tests reassign
``repo._scale_idx_cache``; Task 20 transfers that state into
ScaleArtifactRuntime by identity), the version key resolves the facade's
memoized ``_scale_index_version`` per call, and ``open_ann`` keeps the
manifest ``dim`` probe as the ANN dimension truth (漏一处消费点 = 静默零召回)
plus the fail-open None → caller-fallback semantics.
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Callable

from app.repositories.filesystem.scale_artifact_store import MANIFEST_ABSENT

_logger = logging.getLogger(__name__)

# ``_stale_manifest_admissible`` 的解析 memo 上限(每条只有两个字段,几十字节)。
_MANIFEST_IDENTITY_MEMO_MAX = 512

# 缓存 ScaleIndex 上记录「它是从哪一代磁盘工件加载出来的」的属性名。照
# ``open_ann`` 的 ``_ann_load_states`` setattr 先例:ScaleIndex 是进程缓存的身份,
# 把加载时的签名挂在实例上,后续判等就能问「你还是盘上那一份吗」。
_DISK_SIGNATURE_ATTR = "_scale_disk_signature"

# ``artifacts`` 适配器没有 ``manifest_stat_signature``(老测试替身)时的签名值,
# 与「文件不在」的 ``None`` 严格区分:前者是「探测不了」(一切照旧),后者是
# 「探测到没有工件」。
_SIGNATURE_UNSUPPORTED = object()

# 「调用方没带签名,自己 stat 一次」——只给直接调这两个方法的既有测试用;
# ``load()`` 永远显式传,一次 load 只 stat 一次。
_COMPUTE_SIGNATURE = object()


def _signature_superseded(cached, signature) -> bool:
    """缓存实例是否已被磁盘上的**新一代产物**取代(W-CLI T-W3)。

    ``signature is None``(manifest 此刻读不到——例如 swap 的两次 rename 之间
    那一瞬)刻意不算失配:那不是「换代了」,是「暂时看不见」,继续服务手上的
    实例是既有的 fail-soft 语义。

    **主根为什么不需要伴生那条「确认缺失即失效」分支**(codex #643 R12 P1)。
    伴生侧(``source_partitioned_ppr``)把 ``MANIFEST_ABSENT``——探到根**确实**
    不在——当作立即逐出的信号,因为伴生根会被同版本 ``import`` 合法地**退休**
    (包里省略该可选根 → ``retire_live_directory`` 把它改名走),那之后「无
    manifest」是一个稳定的新事实,继续服务旧 CSR 永远不会自愈。主根没有这条
    路径:``validate_import_package`` 硬拒缺 ``kg_index`` 的包,``PUBLISH_ORDER``
    里主根永远是被替换而不是被退休,``retiring`` 集合按构造排除 ``MAIN_ROOT``。
    所以主根上的「manifest 不在」只有两种来源——swap 两次 rename 之间那一瞬,
    或运维手工删除——两者都是「暂时看不见」而不是「这一代已退役」,维持
    fail-soft 是对的:换代的正常形态是**换成另一个 inode**,由下面的值比较
    抓到。因此 ``MANIFEST_ABSENT`` 在这里与 ``None`` 同款早退,主根行为零变化。

    ``recorded is None``(codex #643 R5 P2)**不再**同等对待。它曾经被当成
    「这个实例理论上不该存在」的死角,按「不知道」放行——但它其实是一条真实
    可复现的窗口:``load()`` 顶部那次 ``_manifest_signature`` 恰好落在
    live→``.old`` 的 rename 缝隙里读到 ``None``,而同一次调用里随后的
    ``load_scale`` 已经晚了一步、读到的是 rename 完成后的新产物——于是一个
    **合法** 的新索引被 ``_adopt`` 记成「签名未知」。旧谓词从此对它永远判
    False:同 version 之后再换代(离线 CLI/import 反复发布)一律判不出来,直到
    进程重启。方向只在这一格反过来:``signature`` **这次**是可读的、
    ``recorded`` 却是「不知道」→ 判 True,补记一次真实签名(此后又是正常的
    值比较,不会反复重载)。``signature is None``(现在也读不到)仍然维持
    fail-soft,不因为「recorded 也是 None」被误判——上面的早退已经处理了。
    """
    if (
        signature is None
        or signature is MANIFEST_ABSENT
        or signature is _SIGNATURE_UNSUPPORTED
    ):
        return False
    recorded = getattr(cached, _DISK_SIGNATURE_ATTR, None)
    return recorded != signature


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

    def _manifest_signature(self, notebook_id: str):
        """磁盘 manifest 的 stat 签名 —— **一次 ``load()`` 只调一次**(T-W3)。

        签名有三个消费者:身份投影 memo 的键、缓存实例的换代判据、以及新加载实例
        身上要记的那一代。它们必须共用同一次 ``os.stat``,否则同一次 load 会在
        热路径上 stat 好几遍(计数器测试钉住「每 load 一次」)。

        返回 ``MANIFEST_ABSENT`` = 探测到没有 manifest;``None`` = 这一次
        ``stat`` 没问出来(权限/IO,codex #643 R12 起与前者分开);
        ``_SIGNATURE_UNSUPPORTED`` = 这个 ``artifacts`` 适配器根本没有探针
        (老测试替身),调用方一切照旧。主根把前两者一视同仁(见
        ``_signature_superseded`` 里为什么主根不需要伴生那条失效分支);伴生
        侧不是这样。
        """
        probe = getattr(self.artifacts, "manifest_stat_signature", None)
        if not callable(probe):
            return _SIGNATURE_UNSUPPORTED
        return probe(self.artifacts.scale_dir(notebook_id))

    def _manifest_identity(
        self, notebook_id: str, signature=_COMPUTE_SIGNATURE,
    ) -> "dict | None":
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

        ``signature`` 由调用方给(见 ``_manifest_signature``);缺省值只服务直接
        调用本方法的测试,``load()`` 永远显式传自己那一次 stat 的结果。
        """
        directory = self.artifacts.scale_dir(notebook_id)
        if signature is _COMPUTE_SIGNATURE:
            signature = self._manifest_signature(notebook_id)
        if signature is not _SIGNATURE_UNSUPPORTED:
            if signature is None or signature is MANIFEST_ABSENT:
                # 文件不在(``MANIFEST_ABSENT``)或这次 stat 问不出来(``None``)
                # = 无工件,与 read_manifest 的缺失分支同款早退。两者在**这里**
                # 仍然同款是刻意的:调用方 ``load`` 对二者的结论都是「回落 live
                # 检索路径」,分开只会在探针失灵时多解析一份 2MB manifest。
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
        if signature is not _SIGNATURE_UNSUPPORTED:
            with self._manifest_identity_lock:
                self._manifest_identity_memo[notebook_id] = (signature, identity)
                self._manifest_identity_memo.move_to_end(notebook_id)
                while len(self._manifest_identity_memo) > _MANIFEST_IDENTITY_MEMO_MAX:
                    self._manifest_identity_memo.popitem(last=False)
        return identity

    def _stale_manifest_admissible(
        self, notebook_id: str, signature=_COMPUTE_SIGNATURE,
    ) -> "dict | None":
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
        identity = self._manifest_identity(notebook_id, signature)
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
        flag=ON 时 delta 新鲜度来自检索侧 ⊕delta 暴力块,不来自这个核。

        **W-CLI T-W3:version 值判等之外再比磁盘签名。** 换代读取本来是逐请求探测
        的(version_signal + manifest 磁盘签名),盲区只有一个:「数据不变、产物变化」
        —— 离线 CLI 或异机 import 原子换上新工件,而 DB 侧 version 值一模一样。那时
        上面两处 ``version`` 判等都会命中旧对象,进程缓存里那个几 GB 的 ScaleIndex
        (含已打开的 hnswlib handle)会一直服务已被替换掉的产物。所以每处 version
        判等都跟一次签名比对,签名变 → 当作新一代重新加载。

        **成本如实登记(规格评审 9)**:这多出来的一次 ``os.stat`` 落在两条路径
        **最热的共用分支**上 —— 不是「本就每次 stat」。改动前,静态大库(不再摄取,
        ``cur`` 与 manifest version 恒等)每次 ``load`` 都在第一处判等直接返回,
        **零 stat**;一次提问要 5–10 次 ``_scale_index(allow_stale=True)``,所以这是
        每次提问多 5–10 次 stat。本机实测(APFS、暖 dentry;方法见
        ``tests/test_scale_generation_switch.py`` 的 characterization 用例):
        单次 ``manifest_stat_signature`` ≈1.4µs,``load`` 的缓存命中分支
        1.5µs→2.9µs,一次提问合计 +7–14µs。参照物有两个:同一次 ``load`` 里
        ``version()`` 无条件先做的那次 ``version_signal`` 查询 ≈7.4µs(sqlite
        进程内;Postgres 是一次网络往返,只会更大),以及这个 memo 已经在替本路径
        省掉的一次 1.9MB manifest 解析 ≈1.9ms —— 新增的 stat 比前者小一个量级、
        比后者小三个量级,更远低于它在盲区里换回来的那次多 GB 重载。
        """
        signature = self._manifest_signature(notebook_id)
        cur = self.version(notebook_id)
        cached = self.scale_cache().get(notebook_id)
        if self._still_current(cached, cur, signature):
            return cached
        if not allow_stale:
            # version-exact:字节不变——load,manifest==cur 才 cache 并返回,否则 None。
            idx = self.artifacts.load_scale(notebook_id)
            if idx is None:
                return None
            if idx.manifest.get("version") == cur:
                return self._adopt(notebook_id, idx, signature)
            return None
        # allow_stale:按磁盘身份复用。cached 若仍是当前磁盘索引(其 version == 磁盘
        # manifest version **且**签名同代)→ 直接返回(handle 存活,零重载)。管线
        # 身份闸见 _stale_manifest_admissible;签名共用上面那一次 stat。
        disk_manifest = self._stale_manifest_admissible(notebook_id, signature)
        if disk_manifest is None:
            return None   # 无索引 / 工件与已发布管线不同代
        disk_ver = disk_manifest.get("version")
        if self._still_current(cached, disk_ver, signature):
            return cached
        # cold:单飞加载。全局锁只护锁表,load 在 per-nb 锁内、不持全局锁。
        with self.load_lock():
            locks = self.load_locks()
            nb_lock = locks.get(notebook_id)
            if nb_lock is None:
                nb_lock = threading.Lock()
                locks[notebook_id] = nb_lock
        with nb_lock:
            # double-check:等锁期间别的线程可能已加载好当前磁盘索引。身份仍走
            # 本次 load 的那一个签名(memo 命中,不再 stat)。
            cached = self.scale_cache().get(notebook_id)
            disk_manifest = self._stale_manifest_admissible(
                notebook_id, signature
            )
            if disk_manifest is None:
                return None
            disk_ver = disk_manifest.get("version")
            if self._still_current(cached, disk_ver, signature):
                return cached
            idx = self.artifacts.load_scale(notebook_id)
            if idx is None:
                return None
            return self._adopt(notebook_id, idx, signature)

    def _still_current(self, cached, version, signature) -> bool:
        """进程缓存里这一个实例,现在还能直接交出去吗?

        两个条件:``version`` 值判等(既有语义,调用点各自给 DB 版本或磁盘版本)
        **且**它不是被新一代产物取代的旧对象(T-W3)。
        """
        return (
            cached is not None
            and cached.manifest.get("version") == version
            and not _signature_superseded(cached, signature)
        )

    def _adopt(self, notebook_id: str, idx, signature):
        """把刚加载出来的实例记上「它是哪一代」并放进进程缓存。

        签名是在 ``load_scale`` **之前**取的,方向是安全的:它只可能比手上这份
        字节**旧**,不可能更新。所以最坏情况是「盘上刚换代、我们记了上一代的签名」
        →下一次 load 多重载一次(自愈);绝不会出现「记了更新的签名、把旧产物一直
        当新的服务」那一侧的错。
        """
        setattr(idx, _DISK_SIGNATURE_ATTR, signature)
        self._warn_on_library_drift(notebook_id, idx.manifest)
        self.scale_cache()[notebook_id] = idx
        return idx

    def _warn_on_library_drift(self, notebook_id: str, manifest) -> None:
        """工件的 hnswlib 版本与本进程不同时记一条 warning(W-CLI T-W3)。

        为什么单挑 hnswlib:``.bin`` **没有格式版本头**,版本不符时 ``load_index``
        未必报错,而 ``open_ann`` 的 fail-open 会把任何异常吞成 None → 静默零召回。
        numpy/scipy 的 npy/npz 有格式版本、失配会响亮失败,所以只记进 manifest 供
        运维比对,不在这里告警。**硬拒**(异机 import 时版本不等就拒绝落地)属于
        离线 CLI 的 ``import`` 校验,不在读侧——读侧已经有这份工件了,拒绝服务它
        只会把检索打成零召回,比带警告服务更糟。

        缺 ``library_versions`` 键 = 本特性之前构建的工件 → 沉默(未知不是失配,
        older-index-stays-valid)。只在冷加载后走一次,不在缓存命中路径上。

        刻意不用 ``note_model_error``:那条通道会进 ask 响应的 ``model_errors``,
        把一条运维侧的库漂移警告变成用户看见的「上游模型错误」。这里要的是
        运维可见,不是把噪声塞进问答结果。
        """
        from app.services.kg.scale_index import (
            MANIFEST_LIBRARY_KEY,
            runtime_library_versions,
        )
        recorded = manifest.get(MANIFEST_LIBRARY_KEY)
        built_with = (
            str(recorded.get("hnswlib") or "")
            if isinstance(recorded, dict)
            else ""   # 缺键 / 结构性损坏都按「未知」处理,绝不在读侧抛。
        )
        if not built_with:
            return
        running = str(runtime_library_versions().get("hnswlib") or "")
        if not running or running == built_with:
            return
        _logger.warning(
            "scale index for notebook %s was built with hnswlib %s but this "
            "process runs %s; the .bin carries no format version header, so a "
            "mismatched read can fail open to zero recall — rebuild locally or "
            "re-import from a machine with matching libraries",
            notebook_id,
            built_with,
            running,
        )

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

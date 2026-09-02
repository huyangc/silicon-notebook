"""Knowledge lifecycle and unified-KG orchestration (Task 15).

Owns the KG lifecycle surface the facade previously carried inline:
deletion / full-notebook build / rebuild (with the ``kg_building`` in-flight
flag), store/relink of extracted graphs, cluster writes, incremental source
fusion, the unified-KG reads (status / graph / neighbors) and the streamed
unified rebuild plus its derived layers (canonical relations, mention
bridge, communities and community reports).

Composition rules (Gate 5): no facade import. Persistence goes through the
injected Task-13 stores; transactions ride the injected ``write``/``connect``
seats (the facade's ``_write``/``_connect`` compatibility seams, resolved at
call time so transaction-counting / failure-injection monkeypatches keep
observing every commit boundary). Post-commit KG side effects keep funnelling
through the facade's ``_invalidate_unified_cache`` / ``_mark_unified_kg_dirty``
/ ``_bump_cluster_mutation_seq`` wrappers (injected late) — the Task-14
coordinator stays the single dirty entry and the frozen mutation phase matrix
is unchanged. Until Gate 6 the scale/viz domain is reached through callable
adapters (scale-index load / ANN open / viz index+probe / build-viz /
auto-index / copy-stats), all late-bound to the facade so frozen repo-level
patch seats keep working.

Red lines preserved verbatim from the facade:
- ``rebuild_unified_kg``'s resume/cache semantics: the ``cluster_input_version``
  (v2) O(1) skip gate, ``force = force or fresh`` boundary self-defense,
  checkpoint GC AFTER the skip gate, the ``exclude_emb_count`` checkpoint
  version namespace and the merge-review / concept-desc checkpoint resume.
- ``_stream_seed_reps`` / ``_write_cluster_map_streamed`` ORDER BY rowid
  anchoring (PR#136: a covering index must never perturb canonical order).
- Concept-description concurrency: the kg LLM client is resolved ONCE in the
  main thread (worker threads don't inherit the request ContextVar).
- Merge-review batching + fail-open (the try wraps the whole adjudication
  block, never just json.loads).
- The ``kg_building`` single-flight guard covers the delete phase of a full
  rebuild.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import re
import threading
import time
from types import SimpleNamespace
from typing import (
    Any, Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple,
)

from app.core.config import Settings
from app.core.event_logging import EventLogger
from app.domain.indexing_pipeline import (
    IndexingPipelineKgExtractionFailedError,
    IndexingPipelineStalePlanError,
)
from app.models.sources import kg_analyzed_without_objects
from app.repositories.ports import (
    GovernanceStorePort,
    KgBuildAlreadyRunning,
    KgBuildJobStorePort,
    KgMaintenanceAlreadyRunning,
    KnowledgeStorePort,
    NotebookDeletingAbortsMaintenanceError,
    UnifiedKgStorePort,
)
from app.services.kg_analysis_precompute import (
    ARTIFACT_CLUSTER_HISTOGRAM,
    ARTIFACT_COMMUNITY_EDGES,
    ARTIFACT_LARGEST_CLUSTERS,
    ARTIFACT_RELATION_PROVENANCE,
    ARTIFACT_SOURCE_PROFILES,
    MAINSTREAM_COVERAGE,
    MAX_PERSISTED_COMMUNITY_EDGES,
    PRECOMPUTED_LARGEST_CLUSTERS,
    CanonicalEdgeTable,
    CanonicalEdgeTableBuilder,
    SourceBoardCounter,
    SourceProfileFolder,
    analysis_ledger_is_current,
    fold_cross_community_edges,
    head_community_ids,
    reusable_artifact_payloads,
    stamp_cluster_seq,
)
from app.services.model_work import notebook_model_artifact_scope
from app.services.knowledge_governance import KnowledgeGovernanceService
from app.services.kg.run_control import (
    KgBuildAborted,
    KgBuildFailure,
    KgExtractionRunControl,
    TaskScopedKgClients,
    probe_kg_model,
)
from app.services.kg.maintenance_jobs import KgMaintenanceJobs


INTERNAL_KG_BUILD_ERROR_MESSAGE = (
    "知识图谱分析意外中断；已完成内容已保留，可继续分析未完成内容。"
)

# Ctrl-C / SIGTERM 收尾用。措辞与启动期崩溃兜底
# (SqliteMigrator._recover_interrupted_jobs) 保持同一口径,用户看到的都是
# 「被中断 + 已完成内容保留 + 可继续」。
INTERRUPTED_KG_BUILD_ERROR_MESSAGE = (
    "本次分析被中断；已完成内容已保留，可继续分析未完成内容。"
)

_SOURCE_BUILD_PAGE_SIZE = 500

# batch-3-W1 T-5a (codex #663 R13 P2): the drain's two safety limits, NAMED so
# the loop conditions and their diagnostic messages cannot silently drift.
# _GRAPH_RESET_ATTEMPTS bounds final-reset retries (each retry only fires
# after an in-transaction bound-probe failure or a 40001 serialization
# abort); _GRAPH_DRAIN_STALL_LIMIT bounds consecutive zero-delete pages
# before the drain declares predicate drift. Both fail LOUDLY at the limit.
_GRAPH_RESET_ATTEMPTS = 3
_GRAPH_DRAIN_STALL_LIMIT = 3

# relink 的两个有界化常数。页大小只影响「一次问数据库要多少个来源 id」,与
# `_SOURCE_BUILD_PAGE_SIZE` 取同一个值:两者是同一张表、同一条 `(created_at, id)`
# keyset、同一条 `idx_sources_notebook_created`。刻意不取更小值——SQLite 把行值
# 比较当残余过滤(EXPLAIN:`SEARCH sources USING INDEX idx_sources_notebook_created
# (notebook_id=?)`,游标不是索引下界),所以每页都从该 notebook 的索引段头重扫一遍,
# 页数越多重扫越多。来源数是千级而不是对象的百万级,这点代价可忽略,但没有理由白付。
_RELINK_SOURCE_PAGE_SIZE = _SOURCE_BUILD_PAGE_SIZE
# 每条 IN 语句的对象 id 上限。relink 的关系读取按端点方向拆成两条语句,每条各带一份
# id 列表,故单条语句的参数数就是这个值 +1,离 SQLite 的变量上限还很远。
_RELINK_ID_BATCH_SIZE = 500


def _in_relink_batches(ids: List[str]) -> Iterator[List[str]]:
    """Split object ids into bounded IN batches (dedup, order preserved)."""
    values = list(dict.fromkeys(ids))
    for index in range(0, len(values), _RELINK_ID_BATCH_SIZE):
        yield values[index:index + _RELINK_ID_BATCH_SIZE]


# 增量融合(PR-C 有界化)每条 IN 语句的 id 上限。两个值不同,因为两条查询的形状不同:
#
# · 桥接候选排除集把同一份 id 列表**用两次**(canonical_a IN … OR canonical_b IN …),
#   单条语句的参数数是这个值的两倍 +1;PG 的 IN 列表规划耗时对 N 超线性,故取 300 ——
#   与 knowledge_context 有界化(PR#476)同一条保守惯例。
# · canonical 折叠走既有的 `cluster_fold_rows`,单列 IN,沿用仓库既有的 900
#   (`_IN_CHUNK` / `_candidate_cluster_map` 已按这个值发同一条语句)。在这条查询上
#   **批越大越好**:SQLite 不 ANALYZE 时 planner 选 `idx_clusters_nb` 而不是
#   `idx_clusters_member`,每条语句都要扫一遍该 notebook 的索引段(实测 40 万行库
#   42.7ms/条,加 `INDEXED BY idx_clusters_member` 则 0.52ms/条),所以分得越碎越亏;
#   PG 侧两种批量都走 idx_clusters_member 的 bitmap 扫(实测 0.44ms/300 id)。
#
# ⚠权衡的**另一侧**(评审 P2,与上面那条互为反面,别只读一半):既然每批 = 一次
# 该 notebook 的索引段扫描,定点折叠的总成本就随**批数**线性涨,只在「要折叠的
# id 数 ≪ 库规模」时才赢过一次范围读。ANN 分支永远在这一侧(每次融合只折叠几十个
# 命中 = 1 批);而无 ANN 的暴力分支要折叠的是被 `kg_incremental_tier2_max_entities`
# (默认 5 万)界住的**整批既有 concept**,拆成 56 批比一次范围读慢约 20×,方向
# 是反的。所以那一支已撤回范围读 —— 实测数字与完整论证登记在
# `incremental_fuse_source` 里 ex_cmap 处,此处不重复。
#
# PG 侧还有一条与批量大小无关、但同属这个权衡的观察:当**单个 notebook 在
# concept_clusters 里占主导份额**时,planner 对 IN 列表的选择率估计会接近全表,
# 于是把这条查询翻成 Seq Scan;这也是 canonical id 那一档取 300 而非 900 的另一半
# 理由(列表越短,估出来的选择率越低,越留在索引侧)。
_FUSE_ID_BATCH_SIZE = 300
_FUSE_FOLD_BATCH_SIZE = 900


def _in_fuse_batches(ids: Iterable[str],
                     size: int = _FUSE_ID_BATCH_SIZE) -> Iterator[List[str]]:
    """Split ids into bounded IN batches (dedup, order preserved)."""
    values = list(dict.fromkeys(ids))
    for index in range(0, len(values), size):
        yield values[index:index + size]


# 单条 claim 参与 concept_comentions 两两组合的 canonical 数上限;**超过即整条
# claim 放弃组合**(不截断)。协议边界性质的稀疏化参数,与 `_MAX_GROUP_REPS` 同类:
# 具名常量,不进 Settings。
#
# 为什么需要它:`rebuild_mention_bridge` 的别名 DF 双门(`mention_alias_df_floor` /
# `mention_alias_df_cap`)约束的是「单个**别名**能命中多少 claim」,**不**约束
# 「单条 **claim** 能命中多少 canonical」——后者恰是组合爆炸那一维:一条枚举式
# claim("A、B、C … 均可用于 X")命中 h 个 canonical 就产出 C(h,2) 对,h 无上限时
# 这是审计点名的 O(h²)。16 给出 C(16,2)=120 对/claim 的硬顶。
#
# 为什么是「整条跳过」而不是「按 canonical id 字典序取前 16」(评审 P2):字典序
# 前缀对命名系统有偏置 —— canonical id 由 `_norm(概念名)` 派生,中文概念在
# ASCII 之后,一条中英混排的巨型 claim 会系统性地只保留英文那一半的桥接对。而
# 「同时提到 17 个以上概念」的枚举式 claim 本身就是**弱**桥接信号(它没说明任何
# 两个概念的具体关系),整条放弃既无偏置又更简单,还省掉一次排序。
#
# 只作用于**组合**这一步:`mention_edges`(每 claim×命中 canonical 一行,线性)
# 仍逐条保留 —— 那是检索侧真正消费的产物,而共提对只是桥接信号。跳过条数经
# 结构化事件上报(对齐隔壁 DF 门的 `mention_alias_df_dropped`)。
_MENTION_MAX_CANONS_PER_CLAIM = 16

# 增量融合开头那次 orphan 簇 anti-join 的**进程内**兜底闸(完整论证见
# `incremental_fuse_source`):本进程已经为哪些 notebook 扫过一次历史残渣。
#
# 语义刻意是「每进程每 notebook 至多一次」而不是任何跨进程的变更信号 —— 全仓已无
# orphan 生产者(四条删除路径都在自己的写事务里清簇行:三条既有路径 + T-5a 预排水
# 的 knowledge_objects 页,见 drain_notebook_graph_rows_page),所以这里要发现的
# 只有改造之前留下的静态残渣,扫一次即净。多 worker 部署下每个 worker 各扫一次,不存在
# 「A 产生、B 收不到信号因而被永久压制」的路径(那正是 tick 版本被 codex 判 P1 的
# 原因)。进程重启后集合为空 → 再扫一次,兜底不失效。
_ORPHAN_SWEEP_DONE: Set[str] = set()
_ORPHAN_SWEEP_DONE_LOCK = threading.Lock()
# 有界:多租户进程里 notebook 数无上限,记满就整体丢弃重来(下一轮各扫一次,
# 与冷启动同代价),不引入 LRU 复杂度。
_ORPHAN_SWEEP_DONE_MAX = 512
# Z6:每批 DELETE 的行数上限 —— 与 keyset 游标一起把清扫拆成多条独立提交的写事务,
# 使单条语句在 9M 行级别的库上仍能稳稳落在 statement_timeout(30s)以内。
_ORPHAN_SWEEP_BATCH_SIZE = 5000


class ModelSkipPolicy:
    """离线跳过模式(`batch_ingest kg --skip-model-failures`)的**任务级**账目。

    默认行为不变:模型不可用熔断整个任务。显式开启后,单个来源的模型失败只作用于
    它自己的子控制器(见 ``KgExtractionRunControl.child``),该来源退回 ``parsed``
    并记入 ``skipped``,任务继续跑后面的来源——偶发波动不再让上千个源的批量全灭。

    连续失败阈值是**保留的兜底**:服务真的挂了时,继续跑就是每个源白烧
    ``KG_LLM_MAX_RETRIES + 1`` 次超时。连续 ``max_consecutive`` 个来源都因模型问题
    失败、中间没有任何一个成功时升级为原来的任务级熔断。

    账目必须**跨页存活**:目标按 500 行原始来源分页,稀疏库每页可能只有一两个目标,
    计数若按页重置,阈值永远达不到,兜底就形同虚设。故由 ``_run_notebook_kg_job``
    建一份、各页 ``_extract_targets`` 共享。
    """

    def __init__(self, max_consecutive: int):
        self.max_consecutive = max(1, int(max_consecutive))
        self._lock = threading.Lock()
        self._consecutive = 0
        self.skipped: List[str] = []

    def record_success(self) -> None:
        with self._lock:
            self._consecutive = 0

    def record_skip(self, source_id: str) -> bool:
        """记一次来源级模型失败;返回 True 表示应升级为任务级熔断。"""
        with self._lock:
            self.skipped.append(source_id)
            self._consecutive += 1
            return self._consecutive >= self.max_consecutive

# `rebuild_communities` 的默认层,也是仓库里**唯一**被写过的层(所有调用点都传 0)。
# 它单独取个名字是因为那份**不分 level** 的共享账目整体归它独有:
# `unified_kg_state.community_seq`、`kg_analysis_artifacts`、以及两张依赖板块划分的
# 产物表 —— 只有这一层的构建写它们,也只有这一层的构建读它们(见闸里的方向四/方向五)。
# ⚠ 与 `rebuild_communities(level=...)` 的签名默认值必须一致,由
# `test_kg_analysis_precompute` 的方向四守卫钉住。
DEFAULT_COMMUNITY_LEVEL = 0


try:
    import orjson as _orjson
    def _fast_loads(s):
        """orjson for speed (5-10x on big float arrays); fall back to stdlib json
        for any value orjson rejects (e.g. NaN/Infinity in legacy vectors)."""
        try:
            return _orjson.loads(s)
        except Exception:
            return json.loads(s)
except ImportError:  # pragma: no cover
    _fast_loads = json.loads


def _concept_desc_sig(name: str, quotes: List[str]) -> str:
    """Deterministic signature of the concept-description LLM input. Same
    (name, quote-set) => same sig => cached description reused across rebuilds.
    Quotes are sorted here so the sig is order-insensitive on the set (the
    caller also sorts, so the prompt text stays byte-stable regardless)."""
    import hashlib
    payload = (name or "") + "\x00" + "\x00".join(sorted(quotes))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _pair_key(a: str, b: str) -> str:
    """canonical id 对的稳定归一键:排序后用 \x1f 连接,(a,b)/(b,a) 同键。"""
    x, y = sorted((a, b))
    return f"{x}\x1f{y}"


_DESC_CKPT_FLUSH = 16   # 概念描述每完成多少个 flush 一次 checkpoint(被杀最多丢这么多)
# 概念描述取证据时,一次 IN 查询覆盖多少个 canonical(审计批4)。原来每个多成员 canonical
# 一次 cluster_evidence_rows 往返,10⁵–10⁶ 次全部串行堵在 LLM 阶段之前。
_DESC_EVIDENCE_CID_BATCH = 300
# 同一批的 seed 参数上限。SQLite 老构建的 SQLITE_MAX_VARIABLE_NUMBER 是 999,
# 900 + (notebook_id, run_id) 两个参数仍在界内(与仓库 in_batches 的默认 900 同源)。
# 单个 canonical 自己的 seed 数就超过它时**独占一批**——即逐条时代对该 canonical 的
# 原样行为,不构成回退。
_DESC_EVIDENCE_SEED_BATCH = 900
# 同一批**取回的证据行数**上限(评审:上面两条都只约束**参数个数**,管不住载荷)。
# cluster_evidence_rows 每个 scratch 成员返一行、行里带整份 evidence JSON,所以一批的驻留
# 由 Σ 成员数决定而不是 Σ seed 数:300 个 hub canonical 各带上千成员就是几十万行 evidence
# JSON 同时在内存里(实测量级几百 MB)。20000 行按每行 ~1–2KB evidence 估约 20–40MB,与
# 「一次只驻留一批」的承诺相称。取值只是内存预算,不影响产物。
_DESC_EVIDENCE_ROW_BATCH = 20000


def _desc_evidence_batches(
    cids: List[str],
    seeds_by_cid: Dict[str, List[str]],
    total_by_cid: Dict[str, int],
):
    """把有序的 canonical id 列表切成批,**保序**、不跨批重排。

    三条上限,任一触顶即切批:
    - 每批至多 ``_DESC_EVIDENCE_CID_BATCH`` 个 canonical(往返数);
    - seed 参数至多 ``_DESC_EVIDENCE_SEED_BATCH`` 个(**绑定变量**上限);
    - 预计取回的证据行至多 ``_DESC_EVIDENCE_ROW_BATCH`` 行(**载荷**上限——前两条都只数
      参数,数不出「一个 hub canonical 有上千成员」这件事)。行数用 ``total_by_cid``
      (Σ members_count,正是 cluster_evidence_rows 会返的行数)估。

    单个 canonical 自己就超过某条上限时**独占一批**——即逐条时代对它的原样行为,不构成
    回退(三条上限的处理一致)。保序是硬要求:下游 ``work`` 列表(喂给 LLM 阶段的输入)
    必须与逐条时代逐位一致,故批按输入序切、批内按输入序遍历。

    前提(故两张表都**直取**、不用 ``.get`` 兜底):``seeds_by_cid`` 与 ``total_by_cid`` 由
    同一个 ``seed_to_canonical`` 循环建起,键集合恒等;``cids`` 是它们键集合的子集。缺键
    说明上游算错了,应当响亮失败——兜底成 0 会被当作「0 个 seed / 0 行」悄悄放大批容量,
    而调用方下一行就是 ``seeds_by_cid[cid]``,兜底也只是把 KeyError 推迟三行。"""
    batch: List[str] = []
    seeds = 0
    rows = 0
    for cid in cids:
        n = len(seeds_by_cid[cid])
        r = int(total_by_cid[cid])
        if batch and (
            len(batch) >= _DESC_EVIDENCE_CID_BATCH
            or seeds + n > _DESC_EVIDENCE_SEED_BATCH
            or rows + r > _DESC_EVIDENCE_ROW_BATCH
        ):
            yield batch
            batch, seeds, rows = [], 0, 0
        batch.append(cid)
        seeds += n
        rows += r
    if batch:
        yield batch


def _canonical_scratch_row(
    notebook_id: str, run_id: str, seed: str, canonical_id: str,
    canonical_names: Dict[str, str], desc_by_cid: Dict[str, str],
    desc_sig_by_cid: Dict[str, str],
) -> tuple:
    """Build one kg_canonical_scratch row for the write-lock-slimming
    preparation segment (_write_cluster_map_streamed). Pure dict lookups —
    no I/O — factored out to its own name so it's a stable point to assert
    against "runs outside the write lock" (mirrors kg_merge.seed_or_unique,
    the equivalent per-object hook for the sibling kg_cluster_scratch
    preparation loop in _stream_seed_reps)."""
    return (
        notebook_id, run_id, seed, canonical_id,
        canonical_names.get(canonical_id, ""),
        desc_by_cid.get(canonical_id, ""),
        desc_sig_by_cid.get(canonical_id, ""),
    )


class KnowledgeLifecycleService:
    def __init__(
        self,
        *,
        settings: Settings,
        event_log: EventLogger,
        knowledge: KnowledgeStorePort,
        governance_store: GovernanceStorePort,
        unified_kg: UnifiedKgStorePort,
        governance: KnowledgeGovernanceService,
        kg_build_jobs: KgBuildJobStorePort,
        kg_building: set,
        unified_cache: Dict[Any, Any],
        scale_artifacts: Any,
        new_id: Callable[[str], str],
        now: Callable[[], str],
        connect: Callable[[], object],
        write: Callable[[], Any],
        bulk_write: Callable[..., int],
        get_notebook: Callable[[str], Any],
        current_user_id: Callable[[], str],
        invalidate_unified_cache: Callable[[str], None],
        mark_unified_kg_dirty: Callable[[str], None],
        mark_unified_kg_dirty_in_tx: Callable[[object, str], None],
        bump_cluster_mutation_seq: Callable[[object, str], None],
        embed_objects_batch: Callable[..., None],
        embed_relations_batch: Callable[[str, List[dict]], None],
        source_ids_from_evidence: Callable[[Optional[str | list]], set],
        set_source_status: Callable[..., None],
        run_extraction: Callable[..., None],
        model_clients: Any,
        reconcile_extracted_terminal: Callable[
            [str, Callable[[str], None]], None
        ],
        wait_ingestion_drain: Callable[[str, float], bool] = (
            lambda _notebook_id, _timeout: True
        ),
        cluster_map: Callable[[str], Dict[str, str]],
        annotate_edge_support: Callable[[str, List[dict]], List[dict]],
        decided_seed_pairs: Callable[[str], Dict[frozenset, str]],
        relations_for_notebook: Callable[[str], List[dict]],
        notebook_copy_stats: Callable[[str], dict],
        note_model_error: Callable[..., None],
        participant_notebook_ids: Callable[[str], List[str]],
        invalidate_knowledge_counts: Callable[[str], None] = lambda _notebook_id: None,
        # 批 3·W1 PR-3 §4.2 的三处在途重建检查点(选项 A)共用这一个callable:
        # 读 notebooks.status 是否为 'deleting'。默认 `lambda _nid: False`
        # (从不中止)保持每一个直接构造本服务的既有测试字节不变——只有生产
        # wiring(RepositoryRuntime.wire_knowledge_lifecycle)传真实实现。
        notebook_deleting: Callable[[str], bool] = lambda _notebook_id: False,
    ) -> None:
        self.settings = settings
        # batch-3-W1 T-5a (codex #663 R3 P2): the drain's row budget — one
        # value serves as both the per-batch page size and the per-table
        # residue threshold deliberately LEFT for the final single
        # transaction (a table already under one page's worth costs the
        # final pass no more than one drained page would have, so draining
        # it buys nothing and the small-graph path stays write-free; the
        # P0-1 pin depends on that). Deployment-configurable via
        # KG_GRAPH_DRAIN_PAGE_ROWS, sized against statement_timeout — see
        # the Settings field's comment.
        self._graph_drain_budget_rows = int(
            getattr(settings, "kg_graph_drain_page_rows", 2000)
        )
        self.event_log = event_log
        self.knowledge = knowledge
        self.governance_store = governance_store
        self.unified_kg = unified_kg
        self.governance = governance
        self.kg_build_jobs = kg_build_jobs
        self._notebook_deleting = notebook_deleting
        # KG build/rebuild 的进行中标志(进程内;重启后天然为空=未构建,无需 reconcile)。
        # 集合本体归 NotebookCatalogService 所有(get_notebook 在那读成员资格);
        # 本服务持同一个 set 对象,build/rebuild 路径照旧 add/discard;facade 的
        # `_kg_building`/`_kg_building_lock` 属性别名这里的同一对象。
        self.kg_building = kg_building
        self.kg_building_lock = threading.Lock()
        # The facade's EXISTING cache objects, held BY IDENTITY (never
        # replacement copies) — the Task-14 coordinator evicts the same dict.
        self.unified_cache = unified_cache
        self.scale_artifacts = scale_artifacts
        self._new_id = new_id
        self._now = now
        self._connect = connect
        self._write = write
        self._bulk_write = bulk_write
        self.get_notebook = get_notebook
        self._current_user_id = current_user_id
        self._invalidate_unified_cache = invalidate_unified_cache
        self._mark_unified_kg_dirty = mark_unified_kg_dirty
        self._mark_unified_kg_dirty_in_tx = mark_unified_kg_dirty_in_tx
        self._bump_cluster_mutation_seq = bump_cluster_mutation_seq
        self._embed_objects_batch = embed_objects_batch
        self._embed_relations_batch = embed_relations_batch
        self._source_ids_from_evidence = source_ids_from_evidence
        self._set_source_status = set_source_status
        self._run_extraction = run_extraction
        self.model_clients = model_clients
        # doc_type 终态收口（与上传流水线 process_source 共用
        # SourceIngestionService._extract_reconciling_doc_type）：跑 extract → 守卫落
        # 'extracted'（WHERE doc_type=本轮值）→ 不一致带新类型重跑（轮数上限）→ 原子补发
        # 'extracted' 事件。晚绑定到 source_ingestion（wire 顺序上它在本服务之后建，故
        # 只能 call-time 解析），与 run_extraction 同款。
        self._reconcile_extracted_terminal = reconcile_extracted_terminal
        self._wait_ingestion_drain = wait_ingestion_drain
        self.cluster_map = cluster_map
        self._annotate_edge_support = annotate_edge_support
        self.decided_seed_pairs = decided_seed_pairs
        self.relations_for_notebook = relations_for_notebook
        self.notebook_copy_stats = notebook_copy_stats
        self._note_model_error = note_model_error
        self.participant_notebook_ids = participant_notebook_ids
        self._invalidate_knowledge_counts = invalidate_knowledge_counts
        # Keep algorithm callbacks late-bound through ``self`` so established
        # lifecycle monkeypatch seams and the caller's ContextVar context remain
        # authoritative when a background worker enters the collaborator.
        self.kg_maintenance = KgMaintenanceJobs(
            event_log=event_log,
            get_notebook=get_notebook,
            new_id=new_id,
            relink_notebook_kg=lambda notebook_id: self.relink_notebook_kg(
                notebook_id
            ),
            rebuild_unified_kg=lambda notebook_id: self.rebuild_unified_kg(
                notebook_id, force=False
            ),
        )
        # Private aliases remain for compatibility with characterization and
        # operational probes that inspect the shared registry by identity.
        self.kg_maintenance_jobs = self.kg_maintenance.jobs
        self.kg_maintenance_lock = self.kg_maintenance.lock
        # Conflict detection gets its OWN slot rather than joining relink/rebuild:
        # those two share one because they rewrite the same derived cluster and
        # community products, which conflict detection does not touch — it writes
        # the conflict review queue.  Sharing would only invent a wait.
        self.kg_conflict_jobs = KgMaintenanceJobs(
            event_log=event_log,
            get_notebook=get_notebook,
            new_id=new_id,
            resolve_notebook_conflicts=lambda notebook_id: (
                self.governance.resolve_notebook_conflicts(notebook_id)
            ),
        )

    # ------------------------------------------------------------------
    # KG deletion / store / relink / cluster writes / incremental fusion
    # ------------------------------------------------------------------

    def delete_notebook_kg(
        self, notebook_id: str, *, held_by_kg_job: bool = False
    ) -> dict:
        """Delete all KG artifacts for a notebook (objects, relations, clusters,
        merge candidates, embeddings, extraction runs) and RESET unified state
        (batch-3-W1 PR-2, design doc Sec 3.3 option C) while KEEPING sources
        and source_elements so it can be re-extracted from already-parsed
        elements. Returns {table: rows_deleted} — ``unified_kg_state`` itself
        contributes a ``"unified_kg_state"`` key holding the RESET's rowcount
        (normally 1, an UPSERT since R1 P0-2 — see the store's own docstring
        for why a bare UPDATE is not enough), not a delete count;
        ``"kg_analysis_artifacts"`` holds this notebook's ledger rows removed
        (Sec 3.2 table #15); and — R1 (P2-1, post-review) —
        ``"kg_community_edges"`` / ``"kg_source_profiles"`` hold the ledger's
        DETAIL rows removed alongside it (the two are a documented single
        unit; see ``discard_board_dependent_kg_analysis_artifacts``'s
        docstring in either backend's ``unified_kg_store.py``).

        batch-3-W1 PR-2 R1 (P0-1, post-review): only TWO of the three
        pre-PR-2 explicit post-commit invalidations were removable, not
        three. ``_invalidate_knowledge_counts`` / ``_invalidate_review_
        queue_memo`` existed for the SAME reason: the old
        ``delete_notebook_graph_rows`` DROPPED the ``unified_kg_state`` row,
        so every seq-keyed process cache (both of those included) could
        re-climb to a version it had already cached under after a
        delete+reingest — review_queue_memo's module docstring calls this
        out as one of its registered "seq does not advance" gaps. PR-2
        closes THAT hazard structurally: the row is reset in place rather
        than dropped, and ``kg_reset_epoch`` — folded into both memo keys as
        (epoch, seq) — only increases, so a cache entry keyed under an
        older epoch can never again compare equal to a live read. Those two
        calls are gone for good.

        ``_invalidate_unified_cache`` is DIFFERENT and stays. It evicts
        ``self.unified_cache`` (``_unified_graph_full``'s
        ``(notebook_id, level)``-keyed dict, no version component of ANY
        kind — not kg_mutation_seq, not kg_reset_epoch). ``invalidate_kg``
        (what this call delegates to) is the ONLY eviction path for that
        cache — it is not a seq-aliasing patch alongside the other two, it
        is the single mechanism. Dropping it would leave a warm
        ``/unified-kg`` response serving the pre-delete graph indefinitely
        (until some unrelated write on the same notebook happens to evict
        it) — an outright wrong answer, not merely a version-aliasing risk
        epoch could ever close. See
        ``test_knowledge_lifecycle_delegation.py::
        test_delete_notebook_kg_evicts_the_warm_unified_graph_cache`` for
        the behavioural proof and kg_mutation.py's FULL CENSUS entry for
        delete_notebook_kg for the full seq-vs-epoch writeup.
        """
        self.get_notebook(notebook_id)
        # codex #663 R7/R8 P1 — the fence, stated precisely:
        # ``kg_building`` gates NEW buildkg-/rebuildkg- job admission
        # (``prepare_notebook_kg_job`` refuses while this notebook is
        # registered — R8 made that check real), and the production rebuild
        # path registers here BEFORE entering its delete phase. Standalone
        # callers (maintenance facade, offline scripts) register for the
        # span of this call when nobody has. What the fence deliberately
        # does NOT cover, with the boundedness argument for each:
        # per-source upload extraction (``process_source`` → ``store_kg``)
        # commits ONE bounded batch per source and is converged by the
        # rebuild's own subsequent re-extraction — its residue is exactly
        # what the final transaction's in-tx revalidation loop absorbs (over
        # threshold → re-drain, three failed rounds → loud failure);
        # hidden-source writers (memory confirm / knowhow projector) never
        # match a drain predicate; leg-B maintenance (relink/unified) writes
        # bounded batches, same revalidation argument. Rows a concurrent
        # writer commits after a table's DELETE inside the final pass are
        # PRE-EXISTING PostgreSQL READ COMMITTED semantics of that pass —
        # T-5a changed none of its statements — and are converged by the
        # rebuild re-extraction that follows every production delete.
        # codex #663 R9 P1b: ownership is explicit, never inferred from set
        # emptiness. The rebuild job's nested call passes
        # ``held_by_kg_job=True`` (it reserved the marker at preparation);
        # every other caller must CLAIM the marker atomically here — and a
        # marker already held by someone else is a refusal
        # (KgBuildAlreadyRunning), not a licence to drain alongside the
        # actual owner.
        if held_by_kg_job:
            return self._delete_notebook_kg_fenced(notebook_id)
        with self.kg_building_lock:
            if notebook_id in self.kg_building:
                raise KgBuildAlreadyRunning(notebook_id)
            self.kg_building.add(notebook_id)
        try:
            return self._delete_notebook_kg_fenced(notebook_id)
        finally:
            with self.kg_building_lock:
                self.kg_building.discard(notebook_id)

    def _delete_notebook_kg_fenced(self, notebook_id: str) -> dict:
        """``delete_notebook_kg``'s body, run under the kg_building fence
        (its docstring carries the full contract)."""
        # codex #663 R16 P1a: snapshot the reverse-index completeness
        # certificate BEFORE the drain. The drain's first page revokes it
        # (in that page's own transaction — see
        # ``clear_source_index_backfilled``'s docstring for the
        # interrupted-drain hazard it closes), and a successful final reset
        # re-asserts it from THIS pre-drain value; an interrupted drain
        # leaves the certificate revoked, so reparse/delete falls back to
        # the evidence scan instead of trusting a half-drained reverse
        # index.
        with self._connect() as db:
            certified_before = self.knowledge.source_index_backfilled(
                db, notebook_id
            )
        drained = self._drain_graph_rows_before_reset(notebook_id)
        totals: dict[str, int] = {}
        for attempt in range(_GRAPH_RESET_ATTEMPTS):
            # codex #663 R11/R12 P2: the tombstone checkpoint guards EVERY
            # final-reset attempt — a small graph never enters the drain
            # loop's per-batch checkpoint, and a tombstone can land during a
            # retry round's re-drain just as well.
            if self._notebook_deleting(notebook_id):
                raise NotebookDeletingAbortsMaintenanceError(notebook_id)
            try:
                with self._write() as db:
                    # codex #663 R12 P1: FIRST statement — pin the
                    # transaction snapshot (PostgreSQL REPEATABLE READ;
                    # SQLite no-op, its global write lock is already
                    # atomic). The bound probe below and every DELETE in
                    # delete_notebook_graph_rows then see the SAME row set,
                    # so the probe atomically bounds the whole final pass on
                    # BOTH backends — a concurrent store_kg committing
                    # mid-transaction can no longer inflate a DELETE past
                    # what the probe admitted (its rows survive the reset,
                    # the final pass's pre-existing semantics, converged by
                    # the rebuild's re-extraction).
                    self.knowledge.begin_graph_reset_isolation(db)
                    # codex #663 R6/R7: UNCONDITIONAL in-transaction bound
                    # validation — read probes outside this transaction are
                    # stale-able regardless of whether a drain ran. Over the
                    # threshold -> commit nothing, drain again; three failed
                    # attempts fail loudly instead of looping forever. One
                    # bounded read inside the final tx; the write-event
                    # shape the P0-1 pin traces is untouched.
                    if self.knowledge.graph_drain_backlog(
                        db, notebook_id, self._graph_drain_budget_rows, 0
                    ) is not None:
                        counts = None
                    else:
                        counts = self.knowledge.delete_notebook_graph_rows(
                            db, notebook_id, self._now()
                        )
                        # delete_notebook_graph_rows RESETS unified_kg_state
                        # in place (PR-2) while keeping Memory/Knowhow
                        # objects and their forward-maintained provenance.
                        # Preserve an existing completeness certificate, but
                        # never promote a legacy/unknown notebook merely
                        # because its user-document graph was cleared:
                        # historical hidden projections may also predate
                        # forward maintenance. The reset UPDATE deliberately
                        # does not touch source_index_backfilled itself (see
                        # its own comment). codex #663 R16 P1a: the value
                        # re-asserted is the PRE-DRAIN snapshot — the drain
                        # revoked the certificate on its first page, so an
                        # in-transaction read here would always be False
                        # after a drain and the certificate would be lost on
                        # every big-graph delete.
                        if certified_before:
                            self.knowledge.mark_source_index_backfilled(
                                db, notebook_id
                            )
            except Exception as exc:
                # codex #663 R12 P1: under REPEATABLE READ a write-write
                # conflict (the unified_kg_state upsert racing a concurrent
                # mark_dirty) surfaces as SQLSTATE 40001 — the transaction
                # rolled back cleanly, so treat it exactly like a failed
                # bound probe: another attempt. Anything else re-raises
                # untouched. sqlite3 exceptions carry no sqlstate attribute,
                # so this branch is PostgreSQL-only by construction.
                if getattr(exc, "sqlstate", None) != "40001":
                    raise
                counts = None
            if counts is not None:
                for table, deleted in counts.items():
                    totals[table] = totals.get(table, 0) + deleted
                # codex #663 R16 P2: the REPEATABLE READ snapshot makes
                # commits that land DURING the final transaction invisible
                # to its DELETEs — under the old READ COMMITTED they might
                # have been swept. The rebuild path re-extracts and
                # converges those survivors, but standalone maintenance/
                # offline callers have no such follow-up — so validate
                # AFTER the commit on a fresh read connection (which sees
                # every later commit) and, when doc-graph rows survived,
                # spend a remaining attempt on another drain+reset round.
                with self._connect() as db:
                    survivors = self.knowledge.graph_drain_backlog(
                        db, notebook_id, 0, 0
                    )
                if survivors is None:
                    break
                counts = None
            # In-transaction bound validation failed (or the transaction was
            # serialization-aborted, or post-reset validation found
            # survivors): a concurrent writer moved the graph under us —
            # drain the new backlog and try the final pass again. codex
            # #663 R13 P2: SKIP the re-drain when no reset attempt remains —
            # the loop would exit straight into the loud failure, and a
            # full destructive drain nobody will follow up on is pure
            # wasted latency.
            if attempt + 1 >= _GRAPH_RESET_ATTEMPTS:
                continue
            for table, deleted in self._drain_graph_rows_before_reset(
                notebook_id
            ).items():
                drained[table] = drained.get(table, 0) + deleted
        else:
            raise RuntimeError(
                f"delete_notebook_kg for notebook {notebook_id}: the final "
                "reset's in-transaction bound validation kept failing "
                f"after {_GRAPH_RESET_ATTEMPTS} attempts — a concurrent "
                "writer is refilling the graph faster than the drain "
                "empties it"
            )
        # P0-1 (post-review): unified_cache has NO version component of its
        # own — invalidate_kg is its only eviction path, not a redundant
        # seq-aliasing patch. See this method's own docstring.
        self._invalidate_unified_cache(notebook_id)
        # T-5a: rows removed by the pre-reset drain belong to this call's
        # deletion just as much as the final pass's (R16 P2: `totals` may
        # aggregate more than one final round) — merge them so callers
        # reading counts (rebuild logs, tests) keep seeing the total.
        for table, deleted in drained.items():
            totals[table] = totals.get(table, 0) + deleted
        return totals

    def _drain_graph_rows_before_reset(self, notebook_id: str) -> dict[str, int]:
        """batch-3-W1 T-5a: bound ``delete_notebook_kg``'s final single
        transaction by PRE-draining the bulk of the graph in small
        per-transaction pages, without touching the P0-1 invariant the final
        pass carries ("all 13 DELETEs + the unified_kg_state reset commit in
        ONE ``write()``, cache invalidation strictly after that commit" —
        pinned by ``test_kg_mutation_phase_matrix.py::test_delete_delegates_
        in_write_then_commits_before_cache_invalidation``). The invariant
        survives verbatim because the final DELETEs are idempotent
        set-clears: after draining they match only the ≤threshold remainder
        (plus whatever landed between drain end and the final transaction),
        so the committed END state — and its atomic flip to readers — is
        byte-identical to the undrained shape.

        Small-graph fast path: the FIRST probe runs on a read connection; a
        graph already at or under the threshold performs ZERO extra write
        transactions, keeping the legacy path (and the pinned test's exact
        write-event sequence) untouched.

        Per batch, in the SAME transaction as the page delete, ``mark_dirty``
        bumps ``kg_mutation_seq`` — kg_mutation.py's FULL CENSUS discipline
        (a graph-row mutation on a live notebook never commits without its
        seq bump; without it two mid-drain reads could cache DIFFERENT
        partial graphs under the SAME (epoch, seq) memo key). The final
        pass's upsert then resets seq to 0 under epoch+1, so drain-era keys
        can never alias post-reset content.

        Mid-drain visibility: readers see a shrinking graph before the final
        atomic flip — the same degraded window that already exists AFTER the
        flip (empty graph until the rebuild refills it), extended backwards
        by the drain duration. ``rebuildkg-``'s dirty flag is set from the
        first batch on.

        §4.2 checkpoint: a delete-job tombstone landing mid-drain aborts the
        pass exactly like the relink/rebuild checkpoints do — the delete
        job's own phase 3 owns those rows now; keeping on draining would
        only contend with it.

        Loud stall guard: a page that deletes 0 rows while the backlog probe
        still names its table means the predicate and the pager disagree —
        after 3 consecutive stalls this raises instead of spinning."""
        drained: dict[str, int] = {}
        stalls = 0
        cursor = 0
        certificate_revoked = False
        while True:
            with self._connect() as db:
                backlog = self.knowledge.graph_drain_backlog(
                    db, notebook_id, self._graph_drain_budget_rows, cursor
                )
            if backlog is None:
                if cursor == 0:
                    return drained
                # codex #663 R2 P1: the cursor is a fast-forward, not a
                # promise — concurrent ingestion can refill a table the
                # cursor already passed, and returning here would hand that
                # unbounded backlog straight to the "bounded" final
                # transaction. Only a CLEAN full pass from index 0 may end
                # the drain; anything that reappeared re-drains from its own
                # index. Convergence: each verification round only fires
                # after a complete forward pass, drain throughput dwarfs
                # ingestion throughput, and the per-batch deleting
                # checkpoint still exits on a tombstone.
                cursor = 0
                continue
            table, cursor = backlog
            if self._notebook_deleting(notebook_id):
                raise NotebookDeletingAbortsMaintenanceError(notebook_id)
            with self._write() as db:
                deleted = self.knowledge.drain_notebook_graph_rows_page(
                    db, notebook_id, cursor, self._graph_drain_budget_rows
                )
                if deleted:
                    # codex #663 R16 P1a: the FIRST destructive page also
                    # revokes the reverse-index completeness certificate in
                    # its own transaction — an interrupted drain must never
                    # leave a certified-but-incomplete reverse index behind
                    # (the successful final reset re-asserts it from the
                    # pre-drain snapshot).
                    if not certificate_revoked:
                        self.knowledge.clear_source_index_backfilled(
                            db, notebook_id
                        )
                        certificate_revoked = True
                    # T-5a review P0: the seq bump goes through the ONE
                    # online choke point (mark_unified_kg_dirty_in_tx), never
                    # a bare store ``mark_dirty`` — the choke point also
                    # re-arms maybe_auto_index's once-set and drops the
                    # corpus-language memo, both of which must not keep
                    # serving pre-drain answers (kg_mutation.py's red line).
                    self._mark_unified_kg_dirty_in_tx(db, notebook_id)
            if deleted:
                # codex #663 R1 P1: evict the warm ``/unified-kg`` graph
                # cache AFTER each page's commit (same commit-then-evict
                # ordering as the final pass). ``unified_cache`` carries NO
                # version component — invalidate_kg is its only eviction
                # path — so without this a warm entry keeps serving the
                # pre-drain graph for the whole drain, and indefinitely if
                # the drain aborts before the final pass's own eviction.
                self._invalidate_unified_cache(notebook_id)
                for name, n in deleted.items():
                    drained[name] = drained.get(name, 0) + n
                stalls = 0
                continue
            stalls += 1
            if stalls >= _GRAPH_DRAIN_STALL_LIMIT:
                raise RuntimeError(
                    f"graph drain stalled on {table} for notebook "
                    f"{notebook_id}: backlog probe keeps naming it but "
                    f"{_GRAPH_DRAIN_STALL_LIMIT} consecutive page deletes "
                    "removed 0 rows (predicate drift?)"
                )

    def prepare_indexing_pipeline_kg(
        self,
        notebook_id: str,
        source_id: str,
        objects: List[dict],
        relations: List[dict],
        *,
        source_generation: str,
    ) -> tuple[dict, List[dict], List[dict]]:
        """Map one extracted graph to unpublished core-schema payload rows.

        This is the preparation half of ``store_kg``: ids, relation endpoints,
        evidence reverse rows and source-local facts are all minted by core,
        but no live table is mutated.  The caller computes embeddings from the
        returned inputs and persists the resulting payload in the durable
        notebook stage.
        """
        now = self._now()
        local_to_id: Dict[str, str] = {}
        for obj in objects:
            local_to_id[obj["local_id"]] = self._new_id("ko")
            obj["_oid"] = local_to_id[obj["local_id"]]
        from app.services.retrieval import (
            RELATION_NAME_EMBED_CHARS,
            relation_embed_text,
            _payload_text,
        )

        local_to_name = {
            obj["local_id"]: _payload_text(obj["payload"])[:RELATION_NAME_EMBED_CHARS]
            for obj in objects
        }
        relation_inputs: List[dict] = []
        relation_rows: List[dict] = []
        for relation in relations:
            source_object_id = local_to_id.get(relation["source_local_id"])
            target_object_id = local_to_id.get(relation["target_local_id"])
            if not source_object_id or not target_object_id:
                continue
            relation_id = self._new_id("rel")
            spans = [
                evidence.get("quoted_span", "")
                for evidence in relation.get("evidence", [])
                if isinstance(evidence, dict)
            ]
            relation_inputs.append(
                {
                    "_rid": relation_id,
                    "text": relation_embed_text(
                        local_to_name.get(relation["source_local_id"], "?"),
                        relation["edge_type"],
                        local_to_name.get(relation["target_local_id"], "?"),
                        spans,
                    ),
                }
            )
            relation_rows.append(
                {
                    "id": relation_id,
                    "source_object_id": source_object_id,
                    "target_object_id": target_object_id,
                    "edge_type": relation["edge_type"],
                    "evidence": relation.get("evidence", []),
                    "created_at": now,
                }
            )

        with self._connect() as db:
            notebook = self.knowledge.notebook_tier_row(db, notebook_id)
            self.knowledge.validate_stage_source_elements(
                db,
                notebook_id,
                source_id,
                [
                    str(evidence.get("element_id") or "")
                    for obj in objects
                    for evidence in (obj.get("evidence") or ())
                    if isinstance(evidence, dict)
                    and str(evidence.get("element_id") or "")
                ],
            )
        status = "reviewed" if notebook and notebook["tier"] == "base" else "approved"
        object_rows: List[dict] = []
        object_sources: List[dict] = []
        facts: List[dict] = []
        fact_elements: List[dict] = []
        for obj in objects:
            object_id = obj["_oid"]
            evidence = obj.get("evidence") or []
            object_rows.append(
                {
                    "id": object_id,
                    "object_type": obj["object_type"],
                    "status": status,
                    "payload": obj["payload"],
                    "evidence": evidence,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            object_sources.extend(
                {"object_id": object_id, "source_id": evidence_source_id}
                for evidence_source_id in sorted(
                    self._source_ids_from_evidence(evidence)
                )
            )
            element_ids: List[str] = []
            for evidence_item in evidence:
                if not isinstance(evidence_item, dict):
                    continue
                evidence_source_id = str(evidence_item.get("source_id") or "")
                if evidence_source_id and evidence_source_id != source_id:
                    raise ValueError(
                        "source-local fact evidence must belong to its source"
                    )
                element_id = str(evidence_item.get("element_id") or "")
                if element_id and element_id not in element_ids:
                    element_ids.append(element_id)
            if not element_ids:
                continue
            fact_id = "ksf-" + hashlib.sha256(
                f"{source_id}\0{source_generation}\0{obj['local_id']}".encode("utf-8")
            ).hexdigest()[:32]
            facts.append(
                {
                    "id": fact_id,
                    "source_generation": source_generation,
                    "local_object_id": obj["local_id"],
                    "global_object_id": object_id,
                    "object_type": obj["object_type"],
                    "payload": obj["payload"],
                    "evidence": evidence,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            fact_elements.extend(
                {
                    "fact_id": fact_id,
                    "source_generation": source_generation,
                    "element_id": element_id,
                    "created_at": now,
                }
                for element_id in element_ids
            )
        return (
            {
                "mode": "replace",
                "objects": object_rows,
                "relations": relation_rows,
                "object_sources": object_sources,
                "facts": facts,
                "fact_elements": fact_elements,
                "object_vectors": [],
                "relation_vectors": [],
            },
            objects,
            relation_inputs,
        )

    def store_kg(
        self,
        notebook_id: str,
        source_id: Optional[str],
        objects: List[dict],
        relations: List[dict],
        *,
        replace_source: bool = False,
        source_generation: str | None = None,
    ) -> Tuple[int, int]:
        """Insert KG nodes/edges (remapping local ids to DB ids), embeds payload.

        分块执行批量 INSERT，但所有 object/relation 块共享一个事务：source 是
        KG 的持久化边界，任一后续块失败都回滚整源。本地 id->DB id 在分块前一次性
        预分配，跨块关系仍能正确 remap。Relations 引用不到的 local id 静默跳过。
        ``replace_source`` 把旧图清理并入同一事务；新图写入失败时旧图保持不变。
        ``source_generation`` 仅由 ingestion 的 extraction run 传入；存在时
        同事务发布独立的 source-local facts，普通治理/测试写入保持原行为。
        ``kg_mutation_seq`` 的 bump 也在这**同一个**事务里（codex #638 R5 P1，
        见循环尾部的注释）：图行与它的世代号一起提交，embedding 只在之后跑，
        既不能推迟这次 bump，也不能因为失败而把它整个吞掉。"""
        CHUNK = 1000
        now = self._now()
        local_to_id: Dict[str, str] = {}
        for obj in objects:
            local_to_id[obj["local_id"]] = self._new_id("ko")
            obj["_oid"] = local_to_id[obj["local_id"]]   # _embed_objects_batch 依赖
        from app.services.retrieval import (
            RELATION_NAME_EMBED_CHARS,
            relation_embed_text,
            _payload_text,
        )
        local_to_name = {o["local_id"]: _payload_text(o["payload"])[:RELATION_NAME_EMBED_CHARS] for o in objects}
        db_relations = []
        for rel in relations:
            s = local_to_id.get(rel["source_local_id"])
            t = local_to_id.get(rel["target_local_id"])
            if not s or not t:
                continue
            spans = [e.get("quoted_span", "") for e in rel.get("evidence", [])
                     if isinstance(e, dict)]
            db_relations.append({
                "_rid": self._new_id("rel"),
                "source_object_id": s, "target_object_id": t,
                "edge_type": rel["edge_type"], "evidence": rel.get("evidence", []),
                "text": relation_embed_text(
                    local_to_name.get(rel["source_local_id"], "?"), rel["edge_type"],
                    local_to_name.get(rel["target_local_id"], "?"), spans),
            })

        fact_element_ids: dict[str, list[str]] = {}
        fact_ids: dict[str, str] = {}
        all_fact_element_ids: list[str] = []
        if source_generation:
            if not source_id:
                raise ValueError("source_generation requires source_id")
            for obj in objects:
                element_ids: list[str] = []
                for evidence in obj.get("evidence") or ():
                    if not isinstance(evidence, dict):
                        continue
                    evidence_source_id = str(evidence.get("source_id") or "")
                    if evidence_source_id and evidence_source_id != source_id:
                        raise ValueError("source-local fact evidence must belong to its source")
                    element_id = str(evidence.get("element_id") or "")
                    if element_id:
                        element_ids.append(element_id)
                element_ids = list(dict.fromkeys(element_ids))
                if not element_ids:
                    # An ungrounded object may remain on the legacy global KG
                    # path, but it is not safe source-local fact material.
                    continue
                fact_ids[obj["_oid"]] = "ksf-" + hashlib.sha256(
                    f"{source_id}\0{source_generation}\0{obj['local_id']}".encode("utf-8")
                ).hexdigest()[:32]
                fact_element_ids[obj["_oid"]] = element_ids
                all_fact_element_ids.extend(element_ids)

        # Base strong-review gate (Track F): objects written directly to a base
        # notebook land as 'reviewed' (still in USABLE_STATUSES, so retrievable)
        # rather than 'approved' — the curator confirms via update_knowledge
        # before they are treated as canonical. Personal notebooks keep
        # 'approved' (no behavior change).
        with self._connect() as db:
            nb_row = self.knowledge.notebook_tier_row(db, notebook_id)
        auto_status = 'reviewed' if (nb_row and nb_row["tier"] == 'base') else 'approved'

        with self._write() as db:
            if source_generation:
                self.knowledge.validate_source_fact_publish(
                    db,
                    notebook_id,
                    source_id or "",
                    source_generation,
                    all_fact_element_ids,
                )
            if replace_source:
                if not source_id:
                    raise ValueError("replace_source requires source_id")
                self.knowledge.clear_source_graph_state(
                    db, source_id, notebook_id
                )
            for i in range(0, len(objects), CHUNK):
                chunk = objects[i:i + CHUNK]
                # payload/evidence serialized once per object per chunk and shared
                # by the object-insert row and (when this generation also
                # publishes source-local facts) the fact row for the same
                # object — never re-dumped for the second consumer. The reverse-
                # index lookup below reads source ids off the still-live Python
                # evidence list directly, no dumps-then-loads round trip.
                serialized = [
                    (json.dumps(o["payload"], ensure_ascii=False),
                     json.dumps(o["evidence"], ensure_ascii=False))
                    for o in chunk
                ]
                self.knowledge.insert_object_chunk(
                    db,
                    [(o["_oid"], notebook_id, o["object_type"], auto_status,
                      payload_json, evidence_json,
                      source_id or '', now, now)
                     for o, (payload_json, evidence_json) in zip(chunk, serialized)],
                )
                fts_rows = [
                    (o["_oid"], notebook_id, o["payload"].get("name", ""))
                    for o in chunk
                    if (o["payload"].get("name") or "").strip()
                ]
                if fts_rows:
                    self.knowledge.insert_kg_fts_rows(db, fts_rows)
                # Forward maintenance (P0-4 reverse index): fresh inserts never had
                # prior rows, so a plain batched INSERT suffices (no DELETE-first).
                kos_rows = [
                    (o["_oid"], sid, notebook_id)
                    for o in chunk
                    for sid in self._source_ids_from_evidence(o["evidence"])
                ]
                if kos_rows:
                    self.knowledge.insert_object_source_rows(db, kos_rows)
                if source_generation:
                    fact_rows = [
                        (
                            fact_ids[o["_oid"]], notebook_id, source_id or "", source_generation,
                            o["local_id"], o["_oid"], o["object_type"],
                            payload_json, evidence_json,
                            1, now, now,
                        )
                        for o, (payload_json, evidence_json) in zip(chunk, serialized)
                        if o["_oid"] in fact_ids
                    ]
                    fact_element_rows = [
                        (
                            fact_ids[o["_oid"]], notebook_id, source_id or "", source_generation,
                            element_id, now,
                        )
                        for o in chunk
                        if o["_oid"] in fact_ids
                        for element_id in fact_element_ids[o["_oid"]]
                    ]
                    self.knowledge.insert_source_fact_rows(
                        db, fact_rows, fact_element_rows
                    )
            for i in range(0, len(db_relations), CHUNK):
                chunk = db_relations[i:i + CHUNK]
                self.knowledge.insert_relation_chunk(
                    db,
                    [(r["_rid"], notebook_id, source_id,
                      r["source_object_id"], r["target_object_id"], r["edge_type"],
                      json.dumps(r["evidence"], ensure_ascii=False), now) for r in chunk],
                )
            # codex #638 R5 P1: the seq bump commits WITH the graph rows, not
            # after the embeddings below. Every object/relation chunk shares
            # this one transaction, so this is simultaneously "the last graph
            # write's transaction" and "the only one" — the bump can neither
            # be seen without the rows nor the rows without the bump. Before
            # this it ran post-embed, which opened two windows: (a) for the
            # WHOLE embedding pass (minutes on a large source) the rows were
            # committed and visible while kg_mutation_seq had not moved, so a
            # seq-keyed reader — ReviewQueueMemo above all, whose entry the
            # re-extraction clear (R4) had just legitimately re-tagged — kept
            # serving its stale ranking AND total; and (b) on the
            # non-replacement path an embedding failure re-raises, skipping
            # the bump entirely, so that staleness lasted until some unrelated
            # KG write happened to bump the notebook again. Embedding failure
            # can no longer strand the seq: the rows and their generation
            # number are already durable together, and a reader that misses
            # the vectors sees a consistent committed state, never a stale
            # one. Ordering note: no seq consumer wants the bump to trail the
            # embeddings — the embedding COUNT is its OWN term in
            # _cluster_input_version (emb_c), checkup keys vectors on explicit
            # hooks (it states outright that a successful embed does not bump
            # this seq), and notebook_scale expands five tables including the
            # embedding one.
            self._mark_unified_kg_dirty_in_tx(db, notebook_id)
        try:
            self._embed_objects_batch(notebook_id, objects)
        except Exception:
            if not replace_source:
                raise
            self.event_log.logger.exception(
                "replacement KG object embedding failed for %s; "
                "the complete graph is retained for backfill",
                source_id,
            )
        try:
            self._embed_relations_batch(notebook_id, db_relations)
        except Exception:
            if not replace_source:
                raise
            self.event_log.logger.exception(
                "replacement KG relation embedding failed for %s; "
                "the complete graph is retained for backfill",
                source_id,
            )
        # Cache eviction stays post-commit (cheap, process-local, and safe to
        # run late — the seq gate above is what protects sibling processes).
        self._invalidate_unified_cache(notebook_id)
        return len(objects), len(db_relations)

    def complete_relations_for_source(
        self,
        notebook_id: str,
        source_id: str,
        source_title: str,
        run_id: str,
        objects: List[dict],
        relations: List[dict],
        elements: List[Any],
        *,
        expected_mode: str | None = None,
        cancel_check: Callable[[], None] = lambda: None,
    ) -> dict:
        """Run resumable, bounded completion pages for one source generation."""
        from app.services.kg.edge_schema import EDGE_SPECS, canonical_edge_endpoints
        from app.services.kg.relation_completion import (
            COMPLETION_EVIDENCE_PER_OBJECT,
            COMPLETION_SCHEMA_VERSION,
            candidate_batches,
            completion_mode_for_notebook,
            generate_candidates,
            propose_batch,
            stable_relation_id,
            verify_batch,
        )
        from app.services.retrieval import relation_embed_text

        mode = completion_mode_for_notebook(self.settings, notebook_id)
        if expected_mode is not None and expected_mode != mode:
            if mode in {"shadow", "write"}:
                self.transition_relation_completion_mode(
                    notebook_id, source_id, run_id, expected_mode, mode
                )
            else:
                self.mark_relation_completion_stale(
                    notebook_id, source_id, run_id, expected_mode
                )
            return {
                "mode": expected_mode,
                "current_mode": mode,
                "skip_reason": "mode_changed",
                "candidates": 0,
                "proposed": 0,
                "verified": 0,
                "inserted": 0,
            }
        if mode == "off":
            return {"mode": "off", "candidates": 0, "proposed": 0, "verified": 0, "inserted": 0}
        batch_size = max(1, self.settings.kg_relation_completion_batch_pairs)
        max_batches = max(0, self.settings.kg_relation_completion_max_batches)
        max_pages = max(1, self.settings.kg_relation_completion_max_pages_per_run)
        page_size = max(1, self.settings.kg_relation_completion_max_objects)
        max_pairs = max(0, self.settings.kg_relation_completion_max_pairs)
        overfetch = max(1, self.settings.kg_relation_completion_candidate_overfetch)
        neighbor_top_k = max(1, self.settings.kg_relation_completion_neighbor_top_k)
        batch_chars = max(512, self.settings.kg_relation_completion_batch_chars)
        proposer = self.model_clients.chat("kg_extract")
        verifier = self.model_clients.chat("kg_refine")
        stats = {
            "mode": mode,
            "pages": 0, "candidates": 0, "proposed": 0, "verified": 0,
            "inserted": 0,
            "model_batches": 0, "lexical_candidates": 0,
            "ann_candidates": 0, "exhausted": False,
        }
        # These positional arguments remain only for source-compatible callers.
        # Durable, bounded repository rows are the sole completion authority so
        # restart behavior cannot differ from the initial ingestion process.
        del objects, relations, elements
        all_relation_docs = []
        reasoning_edge_types = tuple(
            edge_type for edge_type, spec in EDGE_SPECS.items()
            if spec.category == "reasoning"
        )
        edge_contract_rows = tuple(
            (edge_type, source_type, target_type)
            for edge_type, spec in EDGE_SPECS.items()
            for source_type, target_type in sorted(spec.allowed_pairs)
        )
        known_edge_types = tuple(EDGE_SPECS)
        core_node_types = ("concept", "claim", "formula", "procedure")

        def emit_stats() -> None:
            self.event_log.emit({
                "kind": "kg_relation_completion_done", "notebook_id": notebook_id,
                "source_id": source_id, **stats,
            })

        if max_batches <= 0 or max_pairs <= 0:
            stats["skip_reason"] = "invalid_limits"
            emit_stats()
            return stats
        if not getattr(proposer, "configured", False) or not getattr(
            verifier, "configured", False
        ):
            stats["skip_reason"] = "model_unavailable"
            emit_stats()
            return stats

        def decoded_object(row: Any, score: float = 0.0) -> dict:
            oid = str(row["id"])
            payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"] or "{}")
            evidence = row["evidence"] if isinstance(row["evidence"], list) else json.loads(row["evidence"] or "[]")
            item = {"local_id": oid, "_oid": oid, "object_type": row["object_type"],
                    "payload": payload, "evidence": evidence}
            item["_completion_score"] = max(float(item.get("_completion_score") or 0.0), score)
            return item

        # Reuse the published KG ANN when available. Missing/stale artifacts are
        # fail-closed for this path; lexical and explicit page signals remain.
        ann_index = None
        ann = None
        try:
            ann_index = self.scale_artifacts.load(notebook_id, allow_stale=True)
            if ann_index is not None and getattr(ann_index, "ann_labels", None):
                ann = self.scale_artifacts.open_ann(ann_index, "kg")
        except Exception:
            ann_index = ann = None

        for _page_number in range(max_pages):
            if stats["model_batches"] >= max_batches and max_batches > 0:
                break
            cancel_check()
            now = self._now()
            with self._write() as db:
                page = self.knowledge.completion_page(
                    db, notebook_id, source_id, run_id, mode,
                    COMPLETION_SCHEMA_VERSION, reasoning_edge_types,
                    edge_contract_rows, known_edge_types, core_node_types,
                    page_size, now,
                )
            if page.get("generation_conflict"):
                stats["generation_conflict"] = True
                break
            if page.get("already_completed"):
                stats["exhausted"] = True
                break
            page_rows = list(page.get("rows") or ())
            if not page_rows:
                with self._write() as db:
                    advanced = self.knowledge.completion_advance_state(
                        db, notebook_id, source_id, run_id, mode,
                        COMPLETION_SCHEMA_VERSION, page["cursor"], page["next_cursor"],
                        "completed", self._now(),
                    )
                stats["exhausted"] = bool(advanced)
                break

            anchors = [decoded_object(row) for row in page_rows]
            for anchor, row in zip(anchors, page_rows):
                anchor["_completion_priority"] = (
                    2 if not bool(row["has_relation"]) else
                    1 if not bool(row["has_reasoning_relation"]) else 0
                )
            anchor_ids = [str(item["_oid"]) for item in anchors]
            candidate_scores: dict[str, float] = {}
            lexical_available = False
            lexical_failed = False

            # Bounded lexical over-fetch, then indexed source-id hydration.
            for anchor in anchors:
                if len(candidate_scores) >= overfetch:
                    break
                name = str((anchor.get("payload") or {}).get("name") or "").strip()
                if not name:
                    continue
                try:
                    with self._connect() as db:
                        hits = self.knowledge.fts_search(
                            db, notebook_id, name, k=neighbor_top_k
                        )
                    lexical_available = True
                except Exception:
                    lexical_failed = True
                    hits = []
                for hit in hits:
                    oid = str(hit.get("object_id") or "")
                    if oid and oid not in anchor_ids:
                        candidate_scores[oid] = max(
                            candidate_scores.get(oid, 0.0), 0.25
                        )
                    if len(candidate_scores) >= overfetch:
                        break
            stats["lexical_candidates"] += len(candidate_scores)

            # Bounded ANN over-fetch. Only anchor vectors and the bounded hit ids
            # are hydrated; no source/notebook vector scan is permitted.
            if ann is not None and ann_index is not None and len(candidate_scores) < overfetch:
                try:
                    from app.services.vector_index import decode_vector, resolve_runtime_dim, truncate_vec
                    with self._connect() as db:
                        vector_rows = self.knowledge.embedding_rows_for_objects(
                            db, notebook_id, anchor_ids
                        )
                    runtime_dim = resolve_runtime_dim(self.settings)
                    for vector_row in vector_rows:
                        arr = decode_vector(vector_row["vector"])
                        if arr is None or arr.size != self.settings.embed_dim:
                            continue
                        if runtime_dim:
                            arr = truncate_vec(arr, runtime_dim)
                        k = min(neighbor_top_k, len(ann_index.ann_labels))
                        if k <= 0:
                            break
                        ann.set_ef(max(k + 1, 50))
                        labels, distances = ann.knn_query(arr, k=k)
                        for label, distance in zip(labels[0], distances[0]):
                            oid = str(ann_index.ann_labels[int(label)])
                            if oid not in anchor_ids and not oid.startswith("cluster:"):
                                candidate_scores[oid] = max(
                                    candidate_scores.get(oid, 0.0),
                                    max(0.0, 1.0 - float(distance)),
                                )
                            if len(candidate_scores) >= overfetch:
                                break
                        if len(candidate_scores) >= overfetch:
                            break
                except Exception as exc:
                    self._note_model_error("relation_completion_ann", "", exc)

            if ann is None and lexical_failed and not lexical_available:
                stats["skip_reason"] = "ann_fts_unavailable"
                break

            candidate_ids = list(candidate_scores)[:overfetch]
            with self._connect() as db:
                neighbor_rows = self.knowledge.completion_candidate_rows(
                    db, notebook_id, source_id, candidate_ids
                )
            stats["ann_candidates"] += sum(
                1 for row in neighbor_rows if candidate_scores.get(str(row["id"]), 0.0) > 0.25
            )
            combined = list(anchors)
            seen_ids = set(anchor_ids)
            for row in neighbor_rows:
                oid = str(row["id"])
                if oid not in seen_ids:
                    seen_ids.add(oid)
                    combined.append(decoded_object(row, candidate_scores.get(oid, 0.0)))

            # Only hydrate evidence referenced by this bounded object window.
            # The original parse may contain an entire book; it must not be kept
            # as completion authority or required for a later resume worker.
            page_element_ids: list[str] = []
            for item in combined:
                bounded_evidence = []
                seen_element_ids: set[str] = set()
                for evidence in item.get("evidence") or ():
                    if not isinstance(evidence, dict):
                        continue
                    element_id = str(evidence.get("element_id") or "")
                    if not element_id or element_id in seen_element_ids:
                        continue
                    seen_element_ids.add(element_id)
                    bounded_evidence.append({**evidence, "element_id": element_id})
                    page_element_ids.append(element_id)
                    if len(bounded_evidence) >= COMPLETION_EVIDENCE_PER_OBJECT:
                        break
                item["evidence"] = bounded_evidence
            combined_ids = [str(item["_oid"]) for item in combined]
            with self._connect() as db:
                element_rows = self.knowledge.completion_element_rows(
                    db, source_id, page_element_ids
                )
                page_existing = self.knowledge.completion_existing_keys(
                    db, notebook_id, combined_ids
                )
            page_elements = [
                SimpleNamespace(
                    id=str(row["id"]), source_id=str(row["source_id"]),
                    element_type=str(row["element_type"] or ""),
                    location_label=str(row["location_label"] or ""),
                    text=str(row["text"] or ""),
                )
                for row in element_rows
            ]
            element_map = {element.id: element for element in page_elements}
            page_relations = [
                {
                    "source_object_id": source_object_id,
                    "target_object_id": target_object_id,
                    "edge_type": edge_type,
                }
                for source_object_id, target_object_id, edge_type in page_existing
            ]

            remaining_batches = max(0, max_batches - stats["model_batches"])
            pair_cap = min(max_pairs, remaining_batches * batch_size) if max_batches else 0
            candidates = generate_candidates(
                combined, page_relations, page_elements, run_id=run_id,
                max_objects=len(combined), max_pairs=pair_cap,
                excerpt_chars=max(1, self.settings.kg_relation_completion_excerpt_chars),
                anchor_ids=anchor_ids,
                section_quota=max(1, self.settings.kg_relation_completion_section_quota),
            ) if pair_cap > 0 else []
            stats["pages"] += 1
            stats["candidates"] += len(candidates)
            verified = []
            for batch in candidate_batches(
                candidates, pair_limit=batch_size, char_limit=batch_chars,
                max_batches=remaining_batches,
            ):
                cancel_check()
                proposals = propose_batch(proposer, batch)
                stats["proposed"] += len(proposals)
                cancel_check()
                checked = verify_batch(verifier, proposals)
                verified.extend(checked)
                stats["verified"] += len(checked)
                stats["model_batches"] += 1

            endpoint_ids = sorted({
                oid for proposal in verified for oid in (
                    proposal.candidate.source_object_id,
                    proposal.candidate.target_object_id,
                )
            })
            evidence_ids = sorted({
                eid for proposal in verified for eid in proposal.evidence_element_ids
            })
            scope_ids = endpoint_ids or anchor_ids
            relation_rows = []
            relation_docs = []
            with self._write() as db:
                if not self.knowledge.completion_validate_scope(
                    db, notebook_id, source_id, run_id, scope_ids, evidence_ids
                ):
                    stats["generation_conflict"] = True
                    break
                existing = self.knowledge.completion_existing_keys(
                    db, notebook_id, endpoint_ids
                )
                if mode == "write":
                    names = {
                        str(item["_oid"]): str((item.get("payload") or {}).get("name") or "")
                        for item in combined
                    }
                    for proposal in verified:
                        source_object_id, target_object_id = canonical_edge_endpoints(
                            proposal.edge_type, proposal.candidate.source_object_id,
                            proposal.candidate.target_object_id,
                        )
                        key = (source_object_id, target_object_id, proposal.edge_type)
                        reverse = (target_object_id, source_object_id, proposal.edge_type)
                        if key in existing or (EDGE_SPECS[proposal.edge_type].symmetric and reverse in existing):
                            continue
                        evidence = []
                        excerpt_by_id = {
                            str(item.get("element_id") or ""): str(item.get("text") or "")
                            for item in proposal.candidate.excerpts
                            if isinstance(item, dict)
                        }
                        for element_id in proposal.evidence_element_ids:
                            element = element_map.get(element_id)
                            if element is None:
                                continue
                            # Persist the exact server-issued excerpt that the
                            # verifier approved.  The hydrated element remains
                            # the ownership/existence authority, never model text.
                            quote = excerpt_by_id.get(element_id, "")
                            if not quote:
                                continue
                            evidence.append({
                                "source_id": source_id, "source_title": source_title,
                                "element_id": element_id,
                                "element_type": str(element.element_type or ""),
                                "location_label": str(element.location_label or ""),
                                "quote": quote, "quoted_span": quote,
                                "confidence": proposal.confidence,
                                "basis": "completion:bounded-llm",
                                "completion_schema_version": COMPLETION_SCHEMA_VERSION,
                                "verifier": "kg_refine",
                            })
                        if not evidence:
                            continue
                        relation_id = stable_relation_id(notebook_id, run_id, proposal)
                        relation_rows.append((
                            relation_id, notebook_id, source_id, source_object_id,
                            target_object_id, proposal.edge_type,
                            json.dumps(evidence, ensure_ascii=False), self._now(),
                        ))
                        relation_docs.append({
                            "_rid": relation_id, "source_object_id": source_object_id,
                            "target_object_id": target_object_id,
                            "edge_type": proposal.edge_type, "evidence": evidence,
                            "text": relation_embed_text(
                                names.get(source_object_id, "?"), proposal.edge_type,
                                names.get(target_object_id, "?"),
                                [item["quoted_span"] for item in evidence],
                            ),
                        })
                        existing.add(key)
                    inserted = self.knowledge.insert_completion_relations(db, relation_rows)
                    if inserted:
                        # codex #638 R5: one transaction PER PAGE, so the bump
                        # rides each page's own insert (relink_notebook_kg's
                        # shape) instead of a run tail that was stale for every
                        # page but the last and lost if a later page raised.
                        self._mark_unified_kg_dirty_in_tx(db, notebook_id)
                else:
                    inserted = 0
                status = "completed" if page["exhausted"] else "pending"
                if not self.knowledge.completion_advance_state(
                    db, notebook_id, source_id, run_id, mode,
                    COMPLETION_SCHEMA_VERSION, page["cursor"], page["next_cursor"],
                    status, self._now(),
                ):
                    raise RuntimeError("relation completion watermark CAS failed")
            stats["inserted"] += inserted
            all_relation_docs.extend(relation_docs)
            if page["exhausted"]:
                stats["exhausted"] = True
                break

        if stats["inserted"]:
            try:
                self._embed_relations_batch(notebook_id, all_relation_docs)
            except Exception:
                self.event_log.logger.exception(
                    "completion relation embedding failed for %s", source_id
                )
            # Bump already rode each page's own insert; only eviction is left.
            self._invalidate_unified_cache(notebook_id)
        emit_stats()
        return stats

    def pending_relation_completions(
        self, after_source_id: str = "", after_mode: str = "", limit: int = 100
    ) -> list[dict]:
        """Return a bounded startup-resume page without hydrating KG content."""
        with self._connect() as db:
            rows = self.knowledge.completion_pending_states(
                db, after_source_id, after_mode, limit
            )
        return [dict(row) for row in rows]

    def mark_relation_completion_stale(
        self, notebook_id: str, source_id: str, run_id: str, mode: str
    ) -> bool:
        """Retire one pending mode-specific cursor without touching other modes."""
        with self._write() as db:
            return self.knowledge.completion_mark_state_stale(
                db, notebook_id, source_id, run_id, mode, self._now()
            )

    def transition_relation_completion_mode(
        self, notebook_id: str, source_id: str, run_id: str,
        old_mode: str, new_mode: str,
    ) -> bool:
        """Atomically retain restart recovery across an active-mode change."""
        from app.services.kg.relation_completion import COMPLETION_SCHEMA_VERSION

        with self._write() as db:
            return self.knowledge.completion_transition_mode_state(
                db, notebook_id, source_id, run_id, old_mode, new_mode,
                COMPLETION_SCHEMA_VERSION, self._now(),
            )

    def relink_notebook_kg(self, notebook_id: str) -> dict:
        """Backfill: reconnect degree-0 KG nodes in an EXISTING notebook.

        For legacy / pre-relink graphs (the inline path handles new extractions).
        Reads non-deprecated objects (same node filter as knowledge_graph) with
        their evidence element_ids, asks the deterministic relink core for new
        INTRA-SOURCE edges (nodes carry real source_id ⇒ cross-source never
        linked), and inserts them into knowledge_relations EXACTLY like store_kg
        does (review_status defaults to 'pending', the same value LLM edges get).
        source_id of each new row = the SOURCE object's source_id. Idempotent:
        an edge is skipped if a row with the same
        (source_object_id, target_object_id, edge_type) already exists.

        **Driven one source at a time.** Peak memory is one source, not one
        notebook: the historical whole-graph shape hydrated every object's payload
        and evidence JSON plus every relation into the calling thread (~6.8 GB on
        the 9.1M-object base — PostgreSQL's statement timeout tripped first,
        SQLite ground into the OOM killer). Why the split is answer-preserving,
        edge for edge:

        · ``complete_isolated_edges`` only ever proposes within one source
          (``by_source`` buckets the candidates), so every structure it consults —
          the emitted-key set, the per-node degree cap, the undirected
          already-linked pair set — is confined to a single source. Partitioning on
          exactly that key cannot make two partitions interact.
        · Isolation is NOT partition-local, so the per-source relation read
          deliberately fetches every edge ADJACENT to the partition's objects,
          cross-source ones included. A node whose only edge leaves the source is
          connected and must be skipped; a partition blind to that edge would
          re-link it. This is the one way the decomposition could have diverged.
        · ``isolated_before`` / ``isolated_after`` are plain counts over disjoint,
          exhaustive partitions, so summing them reproduces the notebook totals.

        One registered, covered exception to "edge for edge": the per-source read
        pins ``ORDER BY rowid`` / ``ORDER BY ordinal``, whereas the historical query
        carried no ``ORDER BY`` and got whatever the planner's index gave it (in
        practice ``updated_at`` on SQLite). Where a notebook's insertion order and
        ``updated_at`` order disagree AND the per-node cap binds, an isolated node
        can end up bound to a different — equally valid — same-source partner. Edge
        counts and isolation counts are identical either way; the differential
        fixture pins exactly that.

        Returns {"isolated_before", "edges_added", "isolated_after"}."""
        self.get_notebook(notebook_id)  # KeyError if missing
        isolated_before = isolated_after = 0
        # 「至今写过任何边」的账目,刻意是可变的、写完一个来源就立刻记账,供下面
        # `finally` 判断要不要驱逐进程内缓存。**持久**的变更信号
        # (`kg_mutation_seq`)不在这里推——`finally` 兜不住 kill -9:进程可能在某
        # 来源的写事务提交后、`finally` 语句真正执行前就被 SIGKILL,那样已经落
        # 库的边会永久躲开 `kg_mutation_seq`(`_cluster_input_version` 的兜底
        # COUNT 不数 relations,漏推这一下让「刷新图谱」对这本库永久短路)。因此
        # 持久信号改在 `_relink_one_source` 里、与该来源的边插入同一个写事务原子
        # 提交(镜像 `write_clusters` 的 `_bump_cluster_mutation_seq(db, ...)`)。
        # 这里的 `finally` 只负责进程内缓存驱逐——那是廉价、幂等的,晚一点/多做
        # 一次都无所谓,不需要事务级原子性。
        written = {"edges": 0}
        try:
            for source_id in self._relink_source_partitions(notebook_id):
                # 批 3·W1 PR-3 §4.2 选项 A 的检查点(relinkkg-,逐来源边界)。
                # 相位 2 quiesce 闸的腿 B 依赖这个检查点存在(design doc §T-3.3
                # 时序步骤 3)——没有它,quiesce 会永远等不到这个 pass 自己停下,
                # 删除只能靠超时收场(安全但永远删不掉)。一次 notebooks 主键
                # 点查,噪声级成本。
                if self._notebook_deleting(notebook_id):
                    raise NotebookDeletingAbortsMaintenanceError(notebook_id)
                before, after = self._relink_one_source(
                    notebook_id, source_id, written
                )
                isolated_before += before
                isolated_after += after
        finally:
            if written["edges"]:
                # Match store_kg / delete_notebook_kg so the in-memory rustworkx
                # graph (and PPR/federated caches) pick up the new edges. Once for
                # the run — the per-source writes are additive and no reader can
                # observe a partially relinked graph as anything other than a graph
                # with fewer relink edges. The kg_mutation_seq bump already
                # happened per-source (durably, in-transaction) — this is
                # process-local cache eviction only.
                self._invalidate_unified_cache(notebook_id)
        return {
            "isolated_before": isolated_before,
            "edges_added": written["edges"],
            "isolated_after": isolated_after,
        }

    def _relink_source_partitions(self, notebook_id: str) -> Iterator[str]:
        """Every partition key of the notebook's objects, each yielded exactly once.

        Two readers, because objects have no foreign key to ``sources``:

        · the notebook's own sources, walked with the repository's ordinary
          ``(created_at, id)`` keyset — bounded, notebook-local, index-backed;
        · the orphan keys (``''`` and ids of deleted sources), discovered by ONE
          query at the start of the run.

        Their union is exactly the set of distinct ``source_id`` values in
        ``knowledge_objects``, and they cannot overlap (a key either names a source
        of this notebook or it does not). Sources with no objects yield empty
        partitions, which ``_relink_one_source`` short-circuits.

        Why not the obvious ``SELECT DISTINCT source_id FROM knowledge_objects``
        keyset: no index leads with ``(notebook_id, source_id)``, so on PostgreSQL
        every page walked and discarded a neighbouring notebook's objects (measured
        on the shared base: ~1.2M rows removed on the tail page, per page). The
        orphan scan pays a notebook-local pass ONCE per run instead — see the store
        docstrings for the full cost note.

        Orphans are discovered first but processed LAST, so the enumeration reflects
        the state at the start of the run while the common case (real sources) starts
        immediately.
        """
        with self._connect() as db:
            orphans = [
                str(row["source_id"] or "")
                for row in self.knowledge.relink_orphan_source_ids(db, notebook_id)
            ]
        after_created_at = None
        after_id = ""
        while True:
            with self._connect() as db:
                page = self.knowledge.relink_source_page(
                    db, notebook_id, after_created_at, after_id,
                    _RELINK_SOURCE_PAGE_SIZE,
                )
            if not page:
                break
            for row in page:
                yield str(row["id"])
            after_created_at = page[-1]["created_at"]
            after_id = str(page[-1]["id"])
            if len(page) < _RELINK_SOURCE_PAGE_SIZE:
                break
        yield from orphans

    def _relink_one_source(
        self, notebook_id: str, source_id: str, written: dict
    ) -> Tuple[int, int]:
        """One bounded relink work unit: read this source, write its new edges.

        Returns ``(isolated_before, isolated_after)`` for THIS source; the caller
        sums them into the notebook totals. Edges written are reported through
        ``written`` — a mutable ledger rather than a return value — because the
        caller has to know "did the graph already change" even on the path where
        this call raises after its transaction committed.
        """
        from app.services.kg.relink import complete_isolated_edges

        with self._connect() as db:
            obj_rows = self.knowledge.relink_object_rows_for_source(
                db, notebook_id, source_id
            )
        if not obj_rows:
            return (0, 0)

        nodes = []
        for r in obj_rows:
            payload = json.loads(r["payload"] or "{}")
            evidence = json.loads(r["evidence"] or "[]")
            element_ids = {
                ev.get("element_id")
                for ev in evidence
                if isinstance(ev, dict) and ev.get("element_id")
            }
            nodes.append({
                "id": r["id"],
                "object_type": r["object_type"],
                "name": payload.get("name", ""),
                "source_id": source_id,
                "element_ids": element_ids,
            })
        # The raw rows still hold this source's payload+evidence TEXT documents;
        # `nodes` kept only the parsed name and element ids. Dropping the last
        # reference here releases those documents before the relation reads and the
        # relink core run, so the work unit's peak is the derived structures rather
        # than derived + raw. (Only the largest source matters; the whole point of
        # paging is that this is the peak.)
        del obj_rows

        edges: List[Tuple[str, str]] = []
        existing_triples: Set[Tuple[str, str, str]] = set()
        object_ids = [n["id"] for n in nodes]
        with self._connect() as db:
            for batch in _in_relink_batches(object_ids):
                for r in self.knowledge.relink_relation_rows_for_objects(
                    db, notebook_id, batch
                ):
                    edges.append((r["source_object_id"], r["target_object_id"]))
                    existing_triples.add((
                        r["source_object_id"],
                        r["target_object_id"],
                        r["edge_type"],
                    ))

        connected = {oid for pair in edges for oid in pair}
        isolated_before = sum(1 for n in nodes if n["id"] not in connected)

        pending: List[Tuple[Tuple[str, str, str], str]] = []
        for e in complete_isolated_edges(nodes, edges):
            triple = (e["source_object_id"], e["target_object_id"], e["edge_type"])
            if triple in existing_triples:
                continue
            existing_triples.add(triple)
            pending.append((triple, e["basis"]))

        new_rows = []
        if pending:
            # Only now ask whether the partition key still names a live source. It
            # decides one column of the rows about to be written, so in the steady
            # state (nothing left to relink) this query never runs at all.
            with self._connect() as db:
                source_is_live = self.knowledge.relink_source_is_live(
                    db, notebook_id, source_id
                )
            now = self._now()
            new_rows = [
                (
                    self._new_id("rel"), notebook_id,
                    # NULL if the source row is gone or objects carry '' (FK-safe).
                    source_id if source_is_live else None,
                    src, tgt, edge_type,
                    json.dumps([{"basis": basis, "quote": ""}], ensure_ascii=False),
                    now,
                )
                for (src, tgt, edge_type), basis in pending
            ]
            with self._write() as db:
                self.knowledge.insert_relation_chunk(db, new_rows)
                # Durable change signal, atomically with THIS source's edges —
                # not deferred to the run's finally. A kill -9 landing right
                # after this transaction's commit (before the run even reaches
                # its finally) must not leave these edges invisible to
                # kg_mutation_seq (see relink_notebook_kg's comment on the same
                # hazard). Only reached when `pending` is non-empty, so a
                # source with zero new edges never advances the seq — mirrors
                # append_clusters' "no-op call manufactures no signal" rule.
                self._mark_unified_kg_dirty_in_tx(db, notebook_id)
            # Committed — record it BEFORE anything else can raise, so the run's
            # cache-invalidation `finally` sees it even on a later failure.
            written["edges"] += len(new_rows)

        now_connected = connected | {
            oid for r in new_rows for oid in (r[3], r[4])
        }
        isolated_after = sum(1 for n in nodes if n["id"] not in now_connected)
        return (isolated_before, isolated_after)

    # --- zero-model KG maintenance compatibility delegates -------------------
    #
    # KgMaintenanceJobs owns the shared per-notebook slot and worker lifecycle.
    # These methods remain as one-hop compatibility seams for the facade, routes,
    # post-build relink tail, and established tests/operational probes.

    def _settle_kg_maintenance(
        self, notebook_id: str, job_id: str, status: str, stats: dict | None = None
    ) -> None:
        return self.kg_maintenance.settle(
            notebook_id, job_id, status, stats
        )

    def start_notebook_relink(self, notebook_id: str) -> dict:
        return self.kg_maintenance.start_notebook_relink(notebook_id)

    def notebook_relink_status(self, notebook_id: str) -> dict:
        return self.kg_maintenance.notebook_relink_status(notebook_id)

    def _settle_notebook_relink(
        self, notebook_id: str, job_id: str, status: str, stats: dict | None = None
    ) -> None:
        return self.kg_maintenance.settle(
            notebook_id, job_id, status, stats
        )

    def run_notebook_relink_job(self, notebook_id: str, job_id: str) -> dict:
        return self.kg_maintenance.run_notebook_relink_job(
            notebook_id, job_id
        )

    def fail_notebook_relink_submission(
        self, notebook_id: str, job_id: str
    ) -> None:
        return self.kg_maintenance.fail_notebook_relink_submission(
            notebook_id, job_id
        )

    # --- unified rebuild background job (same slot as relink) ----------------

    def start_unified_kg_rebuild(self, notebook_id: str) -> dict:
        return self.kg_maintenance.start_unified_kg_rebuild(notebook_id)

    def unified_kg_rebuild_status(self, notebook_id: str) -> dict:
        return self.kg_maintenance.unified_kg_rebuild_status(notebook_id)

    def run_unified_kg_rebuild_job(self, notebook_id: str, job_id: str) -> int:
        return self.kg_maintenance.run_unified_kg_rebuild_job(
            notebook_id, job_id
        )

    def fail_unified_kg_rebuild_submission(
        self, notebook_id: str, job_id: str
    ) -> None:
        return self.kg_maintenance.fail_unified_kg_rebuild_submission(
            notebook_id, job_id
        )

    # --- conflict detection background job (its own slot) -------------------

    def conflict_resolution_admitted(self, notebook_id: str) -> bool:
        """Size admission for conflict detection — ONE predicate, both entries.

        The endpoint and the post-build tail must agree: a notebook the endpoint
        refuses as too large cannot be admitted through the build tail instead,
        or the gate is decoration. Objects and relations have independent
        ceilings because a modest node set may still form a dense edge graph.
        """
        return not self._conflict_resolution_admission_reason(notebook_id)

    def _conflict_resolution_admission_reason(self, notebook_id: str) -> str:
        with self._connect() as db:
            object_count = self.knowledge.active_object_count(db, notebook_id)
            if object_count > self.settings.kg_conflict_max_objects:
                return "too_many_objects"
            relation_count = (
                self.governance.governance_store.conflict_relation_count(
                    db,
                    notebook_id,
                    max_rows=self.settings.kg_conflict_max_relations + 1,
                )
            )
        if relation_count > self.settings.kg_conflict_max_relations:
            return "too_many_relations"
        return ""

    def _resolve_conflicts_after_build(self, notebook_id: str) -> None:
        """Conflict-detection tail of a successful build — same gates as the endpoint.

        Calling the governance service directly here used to bypass BOTH the
        single-flight slot and the size admission: a build finishing while a
        user-triggered detection was running started a second full pass over the
        same graph, and a notebook the endpoint refuses ran anyway. Fail-open in
        every direction — a build must not fail because its optional conflict
        tail was skipped.
        """
        reason = self._conflict_resolution_admission_reason(notebook_id)
        if reason:
            self.event_log.emit({
                "kind": "kg_conflict_resolution_skipped",
                "notebook_id": notebook_id,
                "reason": reason,
            })
            return
        try:
            job = self.kg_conflict_jobs.start_conflict_resolution(notebook_id)
        except KgMaintenanceAlreadyRunning:
            self.event_log.logger.debug(
                "build_notebook_kg: conflict resolution already running for %s",
                notebook_id,
            )
            self.event_log.emit({
                "kind": "kg_conflict_resolution_skipped",
                "notebook_id": notebook_id,
                "reason": "already_running",
            })
            return
        try:
            # Settlement, the failure event and the exception log all live in
            # run_conflict_resolution_job; this only keeps the build alive.
            self.kg_conflict_jobs.run_conflict_resolution_job(
                notebook_id, job["job_id"]
            )
        except Exception:  # noqa: BLE001 - the conflict tail is fail-open
            self.event_log.logger.debug(
                "build_notebook_kg: conflict resolution failed for %s",
                notebook_id,
            )

    def start_conflict_resolution(self, notebook_id: str) -> dict:
        return self.kg_conflict_jobs.start_conflict_resolution(notebook_id)

    def run_conflict_resolution_job(self, notebook_id: str, job_id: str) -> dict:
        return self.kg_conflict_jobs.run_conflict_resolution_job(
            notebook_id, job_id
        )

    def fail_conflict_resolution_submission(
        self, notebook_id: str, job_id: str
    ) -> None:
        return self.kg_conflict_jobs.fail_conflict_resolution_submission(
            notebook_id, job_id
        )

    # --- Concept-cluster writes ---------------------------------------------

    def write_clusters(self, notebook_id: str, rows: List[dict],
                       object_type: str = "concept") -> None:
        now = self._now()
        with self._write() as db:
            self.governance_store.delete_clusters(db, notebook_id, object_type)
            self.governance_store.insert_clusters(
                db, notebook_id, object_type, rows, now
            )
            # P0-A: bump the cluster-write change signal in the SAME commit as the
            # DELETE+INSERT above — _scale_index_version's memo relies on this
            # being atomic with the content change (no window where clusters moved
            # but cseq didn't).
            self._bump_cluster_mutation_seq(db, notebook_id)
        # P1-2: this DELETE+INSERT can land on the SAME second as a prior write to
        # this notebook (COUNT/MAX(created_at) unchanged for that (notebook_id,
        # object_type) slice, or even notebook-wide if this is the only cluster
        # write) — a version-keyed cache would then serve a stale cluster_map.
        # Explicit invalidation, not the version tuple, is what's load-bearing here.
        self._invalidate_unified_cache(notebook_id)

    def append_clusters(self, notebook_id: str, rows: list, object_type: str = "concept") -> int:
        """追加写 concept_clusters(不 DELETE);member_object_id 幂等(已在则跳过)。返回新增数。"""
        now = self._now()
        with self._write() as db:
            added = self.governance_store.insert_clusters(
                db, notebook_id, object_type, rows, now
            )
            # P0-A: only bump when a row actually landed (a no-op call — every
            # member already present — must not manufacture a fake change signal).
            if added:
                self._bump_cluster_mutation_seq(db, notebook_id)
        # P1-2: self-invalidate rather than rely on every caller remembering to.
        # incremental_fuse_source (the only production caller) already invalidates
        # at its own end too — invalidation is idempotent, so this is pure defense
        # against a future caller that forgets, and against the same-second
        # INSERT-only-COUNT-moves-not-MAX hazard (append adds a row with `now`,
        # which CAN still tie MAX(created_at) to an existing row's timestamp when
        # called twice within the same second, e.g. via incremental_fuse_source's
        # own two append_clusters calls).
        if added:
            self._invalidate_unified_cache(notebook_id)
        return added

    def _sweep_orphan_clusters_page_loop(self, notebook_id: str) -> int:
        """Z6: keyset-batched anti-join delete of legacy orphan
        ``concept_clusters`` rows (``member_object_id`` pointing at a
        since-deleted knowledge object) for ``notebook_id``.

        The batch bound is on **scanned** rows, not deleted ones. Each
        iteration asks the store for one keyset page of ``≤
        _ORPHAN_SWEEP_BATCH_SIZE`` cluster rows ordered by ``(object_type,
        member_object_id)`` — exactly the column order of the existing
        ``uq_clusters_notebook_type_member`` unique index, so both backends
        drive the scan off it without an extra sort and ties are impossible
        (that triple is UNIQUE with ``notebook_id`` fixed by the WHERE
        clause) — and the store deletes whichever rows of THAT page are
        orphans. Cost per batch is O(page) for both statements; a full sweep of
        a notebook holding N cluster rows is ``ceil(N / page)`` batches, O(N)
        overall, and its FREQUENCY is what the once-per-process-per-notebook
        accounting in ``incremental_fuse_source`` bounds — not the size of any
        single statement.

        ⚠ The cursor therefore advances to the **last scanned** key, and the
        loop stops on a SHORT PAGE — never on "this batch deleted nothing".
        Advancing by deleted rows instead is exactly the P1 this shape fixes:
        with the `LIMIT` sitting behind the orphan filter, a notebook with zero
        orphans (the normal state — the producers are zeroed out, see
        ``incremental_fuse_source``) made every batch scan the entire cluster
        slice before coming back empty, i.e. the same ``statement_timeout``
        hazard the batching was introduced to remove; and with many orphans
        every batch re-scanned the whole remaining range.

        Each batch is its own committed write transaction. Returns the total
        number of rows deleted (0 in the common case)."""
        total = 0
        after_object_type = ""
        after_member_object_id = ""
        while True:
            with self._write() as db:
                page, deleted = self.governance_store.sweep_orphan_clusters_page(
                    db, notebook_id, after_object_type, after_member_object_id,
                    _ORPHAN_SWEEP_BATCH_SIZE,
                )
                # P0-A: only bump when this batch actually deleted rows — the
                # later append_clusters calls in incremental_fuse_source
                # self-bump on their own additions, so this guards just the
                # DELETE branch (no double-count, no fake signal on a scanned
                # -but-deleted-nothing batch, which is now the common one).
                if deleted:
                    self._bump_cluster_mutation_seq(db, notebook_id)
            if not page:
                break
            total += deleted
            last = page[-1]
            after_object_type = str(last["object_type"])
            after_member_object_id = str(last["member_object_id"])
            if len(page) < _ORPHAN_SWEEP_BATCH_SIZE:
                break
        return total

    def incremental_fuse_source(self, notebook_id: str, source_id: str) -> None:
        """上传后增量融合该源 concept 进 concept_clusters。Tier1 名种子 append(无 LLM)。"""
        if not self.settings.kg_incremental_fusion_enabled:
            return
        # 清理 re-extraction 留下的 orphan 簇行(member 指向已删 knowledge_objects):重抽取删旧
        # ko- 以新 id 重建,旧簇成员行悬空。消费方(build_ppr_graph/unified_graph)虽已过滤,
        # 仍清以防表无界增长 + unified_kg_status 的 canonical 计数虚高。id 是 PK,子查询走索引。
        #
        # ⚠这条 anti-join 是**全库**扫描,而本方法**每成功抽取一个来源就跑一次**
        # (source_ingestion 的抽取收尾 + scale_index_builder 的增量 fold 逐源调用),
        # 千万级对象的库上等于每次上传白付一次全表反连接。P1 轮把它降为**纯遗留残渣
        # 兜底**,靠的是先把 orphan 的**生产者**清零(而不是给消费端加聪明的闸):
        #
        #   ① `KnowledgeStore.clear_source_graph_state` 的每个删除批次在**同一写
        #      事务**里按 `(notebook_id, member_object_id)`(走 `idx_clusters_member`)
        #      删掉对应簇行 —— 来源删除、重新解析、`store_kg(replace_source=True)`。
        #   ② knowhow 投影的 delete-and-reinsert 走
        #      `prune_cluster_rows_for_source(keep_object_ids=本次要重插的 id)`:
        #      只清**差集**(被这次重投影真正丢掉的对象),活对象一行不碰。
        #   ③ `delete_notebook_graph_rows` 本来就整表清空 `concept_clusters`。
        #
        # 于是全仓**零 orphan 生产者**(由 `test_orphan_producing_paths_are_
        # exhaustively_registered` 的枚举守卫钉住,豁免清单为空),这里剩下的唯一
        # 职责是清掉**改造之前**留在库里的历史残渣 —— 每进程每 notebook 扫一次就够。
        #
        # ⚠ 闸不能是任何「跨进程的变更信号」。走过两个被否的版本,都登记在这里:
        #   · `kg_mutation_seq` 相等闸:`store_kg` 收尾无条件 bump 它,而本方法恰跟在
        #     抽取之后跑,上传主热路径上闸恒开(评审实测 3 次上传 3 次全库反连接)。
        #   · 进程内「疑似 orphan」tick:多 worker 部署下,worker A 的 knowhow 投影
        #     只推 A 的本地信号,worker B 的记账永远停在旧世代 → B 的清扫被**永久
        #     压制**,劣于改造前的无条件扫(codex P1)。
        # 现在的闸是纯进程本地的「至多一次」,而它之所以无害,正是因为生产者已清零:
        # 每个 worker 各扫一次历史残渣,之后没有任何新残渣需要谁去发现。
        #
        # 判据是**纯内存**的,所以跳过时既不读库也不开写事务(fold 循环 D 次白拿
        # 进程写锁是 PR#320 写锁饿死的同一形状);只有真要扫时才 `_write()`。
        #
        # ⚠ Z6(P0,行为恢复):记账在**try/finally**里,语义是「每进程每库尝试过一次」
        # ——不再是「成功过一次」。原因是清扫本身已经是 keyset 分批(见
        # `_sweep_orphan_clusters_page_loop` 与 store 侧「先取页、再删页内孤儿」的两步
        # 重写:批大小界住的是**扫描行数**),单批有界、正常
        # 不该再撞 30s statement_timeout;但历史残渣的分布无法在事前证明,一次异常
        # (连接抖动、极端倾斜分布撑爆某一批)如果不记账,会让**下一次上传**在同一个
        # 注定失败(或注定慢)的清扫上重开一遍、每次都白烧时间,还连带打断整个
        # `incremental_fuse_source`(Tier2 桥接与 claim/formula/procedure 聚类因此对
        # 这本库永久不生效)——这个代价远大于「漏扫一次历史残渣」。生产者早已清零
        # (见上),所以漏掉的只是**静态残渣**,不是持续增长的活伤口:下一次进程重启
        # (`_ORPHAN_SWEEP_DONE` 清空)或一次 `rebuild_unified_kg` 都能把它兜住。
        with _ORPHAN_SWEEP_DONE_LOCK:
            already_swept = notebook_id in _ORPHAN_SWEEP_DONE
        if not already_swept:
            try:
                self._sweep_orphan_clusters_page_loop(notebook_id)
            finally:
                with _ORPHAN_SWEEP_DONE_LOCK:
                    if len(_ORPHAN_SWEEP_DONE) >= _ORPHAN_SWEEP_DONE_MAX:
                        _ORPHAN_SWEEP_DONE.clear()
                    _ORPHAN_SWEEP_DONE.add(notebook_id)
        from app.services.kg_merge import place_new_concepts, _norm
        # PR-C 有界化:这里曾经 `cmap = self.cluster_map(notebook_id)` —— 整表物化
        # {member_object_id: canonical_id}(生产 9.1M 行 ≈2GB dict),而且每次融合的
        # append 都推进 cluster_mutation_seq、当场作废版本缓存,于是下一个来源再付
        # 一遍(fold 路径按 delta 来源循环调本方法 = O(D×全库))。三个消费方各自的
        # 结论**不同**,逐个登记:
        #   · place_new_concepts:cmap 参数**可证冗余**,已删(证明在它的 docstring)。
        #   · _tier2_bridge_candidates_ann:ANN 命中 id → `cluster_fold_rows` 定点,
        #     每次融合只折叠几十个命中。这是唯一真正有界化的一处,也是生产大库
        #     实际走的那一支。
        #   · 无 ANN 的暴力分支:**撤回**有界化,仍读一次整表范围(见下面 ex_cmap
        #     处的登记)—— 它要折叠的是被 max_entities 界住的整批既有 concept,
        #     定点反而慢 20×。
        # 版本缓存 `cluster_map()` 三处都不再用:它会把这份 dict 留在进程里,而这里
        # 用完即弃;何况每次 append 都会当场作废它,缓存本就不成立。
        with self._connect() as db:
            new = self.knowledge.incremental_object_rows(
                db, notebook_id, source_id, "concept"
            )
            new_objs = [{"object_id": r["id"],
                         "name": json.loads(r["payload"] or "{}").get("name", "")}
                        for r in new]
            # 本源没有新 concept 时,canon 名录一个字都用不上 —— 旧代码无条件先扫
            # 一遍再丢掉。读仍留在**同一个连接**里,快照口径与旧实现逐位一致。
            cn = (
                self.governance_store.incremental_cluster_rows(
                    db, notebook_id, "concept"
                )
                if new_objs else []
            )
        if new_objs:
            # ⚠登记:这一读**无法**安全收窄,刻意保留该 (notebook, object_type)
            # 切片的 DISTINCT 扫描。place_new_concepts 对它有两个消费:①按候选 cid
            # 定点取簇名(有界);②整份 values() 喂 build_acronym_alias_map —— 后者
            # 要在既有簇名里找出形如「Full (ACR)」的定义,把新来的裸缩写 "ACR" 并进
            # Full 的簇。
            # 不能收窄的理由是**别名表的键不可点查**:它是 _norm(括号内 token),既不是
            # canonical_id 也不是 canonical_name 的任何前缀/子串位置固定的形态,反查
            # 等于枚举「首字母缩写等于 s」的所有 Full。真要下推,只能对每个新概念种子
            # 发一条 `canonical_name LIKE '%(csa)%'` —— canonical_name 上没有可用索引,
            # 那是**每来源 N 条**无索引扫描去换掉**一条**切片扫描,方向反了。
            # (曾经这里写的理由是「LIKE 看原始列值会漏掉全角括号（ＣＳＡ）」,那是错的:
            #  build_acronym_alias_map 的正则跑在**原始** name 上、不经 NFKC,实测
            #  'Compressed Sparse Attention（ＣＳＡ）' 今天就产出空别名表,压根进不来。
            #  收窄的真实障碍是上面那条,与全角无关。)
            # 已收窄的是**触发次数**:仅在本源确有该类型新对象时才读(上面的条件),
            # 非 concept 类型同理(见下)。
            canon_names = {r["canonical_id"]: r["canonical_name"] for r in cn}
            rows = place_new_concepts(new_objs, canon_names,
                                      seed_fn=lambda o: _norm(o["name"]), id_prefix="K-")
            # ⚠折叠时刻必须冻结在 append **之前**(双评审同条 P1)。master 在方法
            # 开头读一次整表 cluster_map,所以本源刚抽出的新 concept 在那份快照里
            # **没有簇行** → Tier2 的 ANN 命中里凡是同源新对象都被
            # `if not other_cid: continue` 跳掉 = 「新↔新」桥接推迟到下一次索引
            # 重建(_tier2_bridge_candidates_ann docstring 的原话)。若改成 append
            # 之后再定点查,新对象已经有簇行了,就会凭空多产一批「新↔新」候选灌进
            # 「待确认合并」人审队列 —— 那正是仓库专门治理过的方向。
            # 这里在 append 前把这批 id 折叠一次并预置进 Tier2 的 memo:append 只
            # **新增**本源的 member 行、不改任何既有行(insert_clusters 跳过已存在
            # 的 member),所以「预置这批 + 之后只对既有对象定点查」与 master 的
            # pre-append 快照逐位一致。re-fuse(同一来源第二次融合)时这批 id 已经
            # 有簇行,冻结下来的是**命中值**而不是缺失 —— master 第二次融合确实会
            # 看见它们并产出这类对,所以不能一律记成 miss。
            frozen_canonical = self._freeze_new_object_canonicals(notebook_id, new_objs)
            self.append_clusters(notebook_id, rows, object_type="concept")
            # Tier2 桥接候选来源三分支(P1-3,perf audit):
            #   1) 有可用 kg ANN(即使版本漂移/stale,advisory 桥接可接受)→ ANN 近邻查询,
            #      任意规模可用,恢复大库(> max_entities)上一直被静默跳过的跨文档桥接。
            #      stale 索引只缺"新↔新"对象自身(下轮重建后补),"新↔存量"这一主场景
            #      不受影响(见 _tier2_bridge_candidates_ann 文档)。
            #   2) 无索引且已有 concept 数 ≤ max_entities → 原暴力 O(new×existing) 余弦(不动)。
            #   3) 无索引且已有 concept 数 > max_entities → 跳过,但显式发 tier2_skipped 事件
            #      (P1-3 修复点:旧代码这里静默跳过,大库上 Tier2 从未真正跑过)。
            idx = self.scale_artifacts.load(notebook_id, allow_stale=True)
            ann = self.scale_artifacts.open_ann(idx, "kg") if (idx is not None and idx.ann_labels) else None
            cands: list = []
            if ann is not None:
                cands = self._tier2_bridge_candidates_ann(
                    notebook_id, idx, ann, new_objs, frozen_canonical)
            else:
                # The ANN branch never consumes the existing concept payloads:
                # its labels/vectors already come from the persisted scale index.
                # Keep this notebook-wide read behind the no-ANN branch so every
                # extracted source in a large indexed library does not hydrate the
                # same millions of concept rows before immediately discarding them.
                # This only moves I/O; the brute-force/skipped branches below still
                # receive the exact same ordered rows and make the same decision.
                with self._connect() as db:
                    ex = self.knowledge.incremental_object_rows(
                        db, notebook_id, source_id, "concept", exclude_source=True
                    )
            if ann is None and len(ex) <= self.settings.kg_incremental_tier2_max_entities:
                # PR-C:桥接候选对恒含本次新对象的某个 canonical id(见
                # _bridge_canonical_ids),故 pending/已决两份排除集都按这些 id 有界取。
                my_cids = self._bridge_canonical_ids(new_objs)
                with self._connect() as db:
                    # 只读 concept 向量:本分支唯一的向量消费方
                    # `detect_bridge_candidates` 只按 `new_objs` / `existing_items`
                    # 的 object_id 取值,两者都是 `incremental_object_rows(...,
                    # 'concept')` 的结果,所以 claim/formula/procedure 的向量从来
                    # 只是被解码、验维、截断然后丢掉(claim 占对象数的大头)。
                    # 谓词与那两条读逐字相同 → 存活的键、进而本分支的输出不变。
                    vrows = self.knowledge.concept_embedding_rows(db, notebook_id)
                    pend = self._bridge_exclude_rows(
                        db, notebook_id, ("pending",), my_cids
                    )
                # 按 settings.embed_dim 过滤(同 rebuild_unified_kg:丢弃旧 embedder 的异维向量)。
                # 运行时截断旁路(计划 §1.2 旁路 2):两步分离、顺序不可反 ——
                # ①先按存储原生维过滤(既有语义,异维残留出局);②通过后 truncate_vec
                # 截断到运行时空间(聚类/融合空间拍板统一 1024,与 ANN 分支同空间,
                # 否则同功能两分支 lo=0.82 语义分裂)。new_vecs 派生自 vecs,一并覆盖。
                from app.services.vector_index import decode_vector, resolve_runtime_dim, truncate_vec
                dim = self.settings.embed_dim
                rd = resolve_runtime_dim(self.settings)
                vecs = {}
                for r in vrows:
                    arr = decode_vector(r["vector"])
                    if arr is not None and arr.size == dim:
                        if rd:
                            arr = truncate_vec(arr, rd)
                        vecs[r["object_id"]] = arr.tolist()
                existing_items = [{"object_id": r["id"],
                                   "name": json.loads(r["payload"] or "{}").get("name", "")} for r in ex]
                new_vecs = {o["object_id"]: vecs[o["object_id"]] for o in new_objs if o["object_id"] in vecs}
                # 排除全部已决(confirmed+rejected+deferred)+ 已 pending,避免重复入队。
                # 直接查(含 deferred),而非 decided_pairs()(仅 confirmed/rejected)——否则
                # deferred 概念对会在新源桥接时被重新入队,违背「deferred 不回流」。
                with self._connect() as _db:
                    _decided = self._bridge_exclude_rows(
                        _db, notebook_id, ("confirmed", "rejected", "deferred"),
                        my_cids,
                    )
                    # ⚠这一支刻意**不**做有界折叠(评审 P2,已撤回)。这是本 PR 里
                    # 「有界化」唯一一处被实测否掉的消费方,数字登记在这里,别处只引用:
                    # 本分支的准入闸 `kg_incremental_tier2_max_entities`(默认 5 万)
                    # 已经把 existing_items 的规模界住,而 SQLite 无 ANALYZE 时
                    # `member_object_id IN (…)` 退化成 idx_clusters_nb 上的残余过滤
                    # (登记在 _fold_canonical_ids),于是 5 万 id 拆 900/批 = 56 次
                    # 索引段扫描。两次独立实测同向:评审树 301.5ms vs 一次范围读
                    # 14.8ms(20.4×);本机 35 万行 concept_clusters 上 266ms vs
                    # 15.1ms(17.6×)。有界化在这里是净负。同一支本来就要
                    # `concept_embedding_rows(notebook)` 范围读向量,这条范围读没有
                    # 引入新的数量级。
                    # 走 store 原语而**不**走版本缓存的 `self.cluster_map()`:后者会把
                    # 这份 dict 留在进程里(9.1M 行 ≈2GB),这里用完即弃;顺带也收窄了
                    # P2-4 的竞态窗口(与 _decided 同一个连接、同一时刻取)。
                    # 时刻:这一读在 append 之后,但 existing_items 来自
                    # `exclude_source=True`,恒不含本源新对象,而 append 只新增本源
                    # member 行 —— 被 `.get()` 的每个键的值都与 append 前相同。
                    ex_cmap = self.unified_kg.cluster_map_rows(_db, notebook_id)
                exclude = {frozenset((r["canonical_a"], r["canonical_b"])) for r in _decided}
                exclude |= {frozenset((r["canonical_a"], r["canonical_b"])) for r in pend}
                from app.services.kg_merge import detect_bridge_candidates
                cands = detect_bridge_candidates(new_objs, new_vecs, existing_items, vecs, ex_cmap, exclude)
            elif ann is None:
                self.event_log.emit({
                    "kind": "tier2_skipped", "notebook_id": notebook_id,
                    "entities": len(ex), "reason": "no_index_over_threshold",
                })
            if cands:
                now = self._now()
                with self._write() as db:
                    for c in cands:
                        self.governance_store.insert_merge_candidate(
                            db, notebook_id, c["canonical_a"], c["canonical_b"],
                            c["score"], now, id_prefix="cm")
        from app.services.kg_merge import seed_claim, seed_formula, seed_procedure
        _TYPES = {"claim": (seed_claim, "KL-"), "formula": (seed_formula, "KF-"),
                  "procedure": (seed_procedure, "KP-")}
        for t, (sfn, prefix) in _TYPES.items():
            with self._connect() as db:
                trows = self.knowledge.incremental_object_rows(
                    db, notebook_id, source_id, t
                )
                tnew = [{"object_id": r["id"], "payload": json.loads(r["payload"] or "{}"),
                         "name": json.loads(r["payload"] or "{}").get("name", "")}
                        for r in trows]
                # 本源没有该类型的新对象时(常态:大多数来源只产 concept+claim),
                # 旧代码仍会为 claim/formula/procedure 各扫一遍整个类型切片再丢掉。
                # 读仍在同一连接内,快照口径与旧实现逐位一致。
                tcn = (
                    self.governance_store.incremental_cluster_rows(db, notebook_id, t)
                    if tnew else []
                )
            if not tnew:
                continue
            # ⚠同上登记:acronym 别名表要整份既有簇名,不可点查(见 concept 分支注释)。
            tcanon = {r["canonical_id"]: r["canonical_name"] for r in tcn}
            trows_w = place_new_concepts(tnew, tcanon,
                                         seed_fn=lambda o, _s=sfn: _s(o), id_prefix=prefix)
            self.append_clusters(notebook_id, trows_w, object_type=t)
        self._invalidate_unified_cache(notebook_id)

    @staticmethod
    def _bridge_canonical_ids(new_objs: list) -> List[str]:
        """本次新对象在 Tier2 桥接里用的 canonical id(``my_cid``)。

        必须与 ``kg_merge.detect_bridge_candidates`` 和
        ``_tier2_bridge_candidates_ann`` 内联的那条式子**逐字**同构 ——
        ``"K-" + seed_or_unique(_norm(name), object_id)``。注意它刻意**不**过
        ``_seed_with_alias``:桥接侧从来不做 acronym 别名重定向(与
        ``place_new_concepts`` 的既有差异,本 PR 不改)。这些 id 是排除集查询的
        有界键——凡是会被判 ``frozenset((a, b)) in exclude`` 的对,``a``/``b``
        必有一个出自这里。"""
        from app.services.kg_merge import _norm, seed_or_unique

        return list(dict.fromkeys(
            "K-" + seed_or_unique(_norm(o.get("name", "")), o["object_id"])
            for o in new_objs
        ))

    def _freeze_new_object_canonicals(
        self, notebook_id: str, new_objs: list
    ) -> Dict[str, str]:
        """本源新对象在 **append 之前**各自的 canonical(没有簇行的记 ``""``)。

        这是 Tier2 折叠的时刻锚点,合同见 ``incremental_fuse_source`` 里的登记:
        master 的整表 ``cluster_map`` 读在任何 append 之前,所以这批 id 在 Tier2
        眼里要么缺失(首次融合)、要么是上一轮融合留下的值(re-fuse)。调用方必须在
        ``append_clusters`` **之前**调本方法,并把结果整份交给
        ``_tier2_bridge_candidates_ann`` 当 memo 种子 —— 那之后的定点查询就只会落在
        既有对象上,而 append 不改既有对象的任何一行。

        ``""`` 是刻意的负记录而不是不放键:消费侧判的是 ``if not other_cid``,
        对 ``None``/``""`` 结果相同,而放键能保证 memo 认为它「已解出」、不会在
        append 之后再去查一次(查到的就是错的那一份)。

        成本:``ceil(len(new_objs) / _FUSE_FOLD_BATCH_SIZE)`` 条语句,单来源的新
        concept 数常态是几十到几百,即 1 条。"""
        frozen: Dict[str, str] = {o["object_id"]: "" for o in new_objs}
        if not frozen:
            return frozen
        with self._connect() as db:
            frozen.update(self._fold_canonical_ids(db, notebook_id, list(frozen)))
        return frozen

    def _bridge_exclude_rows(self, db, notebook_id: str, statuses, canonical_ids):
        """有界读取排除用的候选对(至少一端命中 ``canonical_ids``),分批。"""
        out: list = []
        for batch in _in_fuse_batches(canonical_ids):
            out.extend(self.governance_store.merge_candidate_pairs_for_canonicals(
                db, notebook_id, statuses, batch
            ))
        return out

    def _fold_canonical_ids(
        self, db, notebook_id: str, object_ids: Iterable[str]
    ) -> Dict[str, str]:
        """``{member_object_id: canonical_id}``,**只**为传进来的这些 id(分批)。

        与整表 ``cluster_map`` 在每个被查到的键上逐位相同(同一张表、同一个
        notebook 谓词、同一列),差别只在没被查的键不返回——而调用方对那些键本来
        就只会 ``.get()`` 出 ``None``。

        ⚠已登记的残余代价(不在本 PR 修):``cluster_fold_rows`` 的
        ``WHERE notebook_id=? AND member_object_id IN (…)`` 在**没有 ANALYZE 的
        SQLite** 上被 planner 选进 ``idx_clusters_nb``,member 谓词退化成残余过滤,
        于是每条语句仍要扫一遍该 notebook 的索引段(实测 40 万行单库:42.7ms/条;
        改用 ``INDEXED BY idx_clusters_member`` 是 0.52ms/条,82×)。PostgreSQL 有
        统计信息,同一条语句走 ``idx_clusters_member`` 的 bitmap 扫(实测
        0.44ms/300 id),不受影响。这里刻意**不**顺手加 ``INDEXED BY``:那会改掉
        一条被检索侧共用的原语的**返回行序**,而 ``retrieval_candidates`` 的
        ``cluster_names.setdefault(canonical_id, name)`` 是按行序取首个名字的
        (同一 canonical_id 下 canonical_name 并不保证一致),属本 PR 之外的可观察
        变化 —— 也不加索引迁移。即便如此,本方法相对旧的整表 ``cluster_map`` 仍是
        净胜:不再把 900 万行搬进 Python、不再建 2GB dict、不再每次融合作废版本缓存。
        """
        folded: Dict[str, str] = {}
        for batch in _in_fuse_batches(object_ids, _FUSE_FOLD_BATCH_SIZE):
            for row in self.unified_kg.cluster_fold_rows(db, notebook_id, batch):
                folded[row["member_object_id"]] = row["canonical_id"]
        return folded

    def _tier2_bridge_candidates_ann(self, notebook_id: str, idx, ann,
                                     new_objs: list,
                                     frozen_canonical: Dict[str, str]) -> list:
        """ANN-backed Tier2 bridge candidate detection (P1-3, perf audit).

        Same candidate-set semantics as `kg_merge.detect_bridge_candidates`
        (lo=0.82 similarity floor, top_k=5 per new object, exclude same-canonical
        hits and already-decided/pending pairs) but sourced from the notebook's
        persisted kg hnsw ANN instead of a brute-force cosine over every existing
        concept embedding — the only way Tier2 stays usable once a library's
        concept count exceeds `kg_incremental_tier2_max_entities` (production:
        490k+ entities, where the brute-force path silently no-ops today).

        `idx` may be STALE (`_scale_index(nb, allow_stale=True)` returned a disk
        index whose version predates this source's own new objects/embeddings).
        This is acceptable: bridging is advisory (candidates only ever land in
        concept_merge_candidates as 'pending', reviewed by a human — never
        auto-merged), and a stale ANN is only missing objects newer than its
        build watermark. The query side (this source's newly-fused concepts) is
        always fresh — it's read straight from knowledge_embeddings, not from
        the index. So "new↔existing" bridges (the main incremental-upload
        scenario: a freshly uploaded concept syncing against the library's prior
        knowledge) work correctly even on a stale index; only "new↔new" bridges
        between two concepts uploaded in the same still-unindexed window are
        deferred to the next scale-index rebuild. That gap already exists today
        for anything beyond a single upload cycle (the index only refreshes on
        rebuild), so this doesn't regress the status quo.

        Thread-safety: `_open_scale_ann`'s hnswlib handle is memoized on the
        ScaleIndex instance (PR#147) and hnswlib's `knn_query` is safe for
        concurrent read-only queries against one index handle, so calling this
        from extraction job worker threads (where incremental_fuse_source runs)
        needs no extra locking.

        PR-C 有界化:此方法曾收一份整表 ``cluster_map``,只为 ``.get(nid)`` 折叠
        ANN 命中的那几十个节点。现在按命中 id 走既有有界原语 ``cluster_fold_rows``
        并在本次调用内 memo(同一份命中在 k 翻倍重查、以及跨新对象之间会重复出现,
        memo 让每个 id 至多查一次;存活位与折叠共用同一个连接块)。排除集同理:
        每一对被判的候选都含本次的 ``my_cid``,故按 ``_bridge_canonical_ids``
        有界取。

        ``frozen_canonical`` 是**调用合同**,不是可选优化:它必须是
        ``_freeze_new_object_canonicals`` 在 ``append_clusters`` **之前**为本批
        ``new_objs`` 的每个 id 取到的 canonical(缺失记 ``""``),整份作为 memo 的
        初值。上面那段说的「新↔新推迟到下一次重建」只有在这个前提下才成立 ——
        没有它,append 刚写进去的簇行会让同源新对象在这里彼此配对,凭空产出一批
        人审候选。
        """
        import numpy as np
        from app.services.vector_index import decode_vector, resolve_runtime_dim, truncate_vec

        topk = 5     # mirrors detect_bridge_candidates' top_k default
        lo = 0.82    # mirrors detect_bridge_candidates' lo default
        dim = self.settings.embed_dim
        # 运行时截断旁路(计划 §1.2 旁路 3):查询侧向量读自 knowledge_embeddings
        # (存储原生维),须截断到运行时空间才能进(切换后同为运行时维的)持久 kg ANN。
        rd = resolve_runtime_dim(self.settings)
        eff_dim = rd or dim   # 运行时生效维:守卫比它而非 embed_dim(否则截断后恒 [] 静默跳过)

        with self._connect() as db:
            new_rows = self.knowledge.embedding_rows_for_objects(
                db, notebook_id, [o["object_id"] for o in new_objs]
            )
            _decided = self._bridge_exclude_rows(
                db, notebook_id, ("confirmed", "rejected", "deferred", "pending"),
                self._bridge_canonical_ids(new_objs),
            )
        exclude = {frozenset((r["canonical_a"], r["canonical_b"])) for r in _decided}
        new_vecs = {}
        for r in new_rows:
            arr = decode_vector(r["vector"])
            if arr is not None and arr.size == dim:   # ①先按存储维过滤(既有语义)
                if rd:
                    arr = truncate_vec(arr, rd)       # ②通过后截断(运行时空间)
                new_vecs[r["object_id"]] = arr

        idx_dim = int(idx.manifest.get("dim", eff_dim))
        if idx_dim != eff_dim or not new_vecs:
            return []

        name_by_obj = {o["object_id"]: o.get("name", "") for o in new_objs}
        out: list = []
        seen: set = set()
        n_labels = len(idx.ann_labels)
        # Legacy semantics (kg_merge.detect_bridge_candidates): rank the top_k
        # NEAREST existing concepts by raw similarity, THEN apply the lo floor /
        # same-canonical / decided-pair filters — filtering never backfills more
        # candidates to make up for a dropped one. The kg ANN index spans EVERY
        # object type (production ratio: ~310k claims vs ~70k concepts of 470k
        # vectors), so a FIXED over-fetch window can come back mostly claims —
        # squeezing concepts out entirely and systematically under-bridging.
        # Type-aware iterative over-fetch instead: start at k = top_k *
        # pad_factor; while fewer than top_k concept hits survive the filters
        # AND the raw neighbor tail is still >= the lo threshold (anything past
        # a below-lo tail is even further away and would be threshold-filtered
        # regardless — expanding cannot help) AND k < min(n_labels, hard cap),
        # double k and re-query (hnsw knn_query is cheap; a fresh query is
        # simpler and safer than incremental cursors).
        pad_factor = max(1, int(self.settings.kg_tier2_ann_pad_factor))
        hard_cap = min(n_labels, 4096)
        # Deprecated alignment: the legacy path's existing_items query filters
        # status!='deprecated'; ann_labels carries no status, so raw hits are
        # batch-validated per knn round (one small IN query, cached across new
        # objects) and deprecated/vanished objects are skipped BEFORE they can
        # consume an eligible slot — matching the legacy pool, where deprecated
        # rows never entered the ranking at all.
        status_alive: Dict[str, bool] = {}
        # PR-C:替代整表 cluster_map 的按命中折叠。负结果也 memo("" 表示这个 id
        # 没有簇行),因为 `if not other_cid` 对 None 和 "" 判定相同 —— 与旧的
        # `cluster_map_.get(nid)` 逐位等价,且不会对同一个缺失 id 反复发查询。
        # 初值 = 调用方在 append **之前**冻结的那一份(见 docstring 的调用合同):
        # 本源新对象的取值就此定死,后面的定点查询只会落在既有对象上。
        canonical_of: Dict[str, str] = dict(frozen_canonical)

        def _resolve_hits(raw_ids: list, self_oid: str) -> None:
            """补齐本轮命中的存活位与 canonical 折叠 —— 共用**一个**连接块。

            两者是同一批命中的两个属性,顺序不可换(要先知道谁活着才知道折叠谁),
            但没有理由各开一次连接:每轮 knn 都要多付一次连接获取(评审 P3)。
            折叠只问「能走到那一步」的命中:下面的循环在 ``cluster:``/自身/非存活
            三道过滤之后才读 ``canonical_of``,先过一遍免得为已经出局的 id 白发
            查询(有专门的守卫盯这条纯成本性质)。"""
            probes = [i for i in raw_ids if not i.startswith("cluster:")]
            unknown_alive = [i for i in probes if i not in status_alive]

            def _pending_fold() -> List[str]:
                return [i for i in probes
                        if i != self_oid and i not in canonical_of
                        and status_alive.get(i, False)]

            if not unknown_alive and not _pending_fold():
                return
            with self._connect() as db:
                if unknown_alive:
                    alive = self.governance_store.valid_object_ids(db, unknown_alive)
                    for i in unknown_alive:
                        status_alive[i] = i in alive
                need = _pending_fold()
                if need:
                    folded = self._fold_canonical_ids(db, notebook_id, need)
                    for i in need:
                        canonical_of[i] = folded.get(i, "")

        for oid, qvec in new_vecs.items():
            from app.services.kg_merge import _norm, seed_or_unique
            # 与 kg_merge.detect_bridge_candidates 一致的空 seed 守卫:符号-only 名
            # (_norm→"")绝不塌缩成裸 "K-"——否则该退化 canonical 会被写进
            # concept_merge_candidates,与真实簇(K-~oid)错位且互相污染。
            my_cid = "K-" + seed_or_unique(_norm(name_by_obj.get(oid, "")), oid)
            q = np.asarray(qvec, dtype=np.float32)
            k = min(max(topk * pad_factor, topk + 1), n_labels)
            eligible: list = []  # [(node_id, canonical_id, sim)] — alive concepts, not self
            query_failed = False
            while True:
                try:
                    ann.set_ef(max(k + 1, 50))
                    labels, distances = ann.knn_query(q, k=k)
                except Exception as exc:  # noqa: BLE001 — fail-open, mirrors other ANN call sites
                    self._note_model_error("tier2_bridge_ann_query", "", exc)
                    query_failed = True
                    break
                hits = sorted(zip(labels[0], distances[0]), key=lambda ld: ld[1])
                raw_ids = [idx.ann_labels[int(lab)] for lab, _ in hits]
                _resolve_hits(raw_ids, oid)
                eligible = []
                for nid, (_lab, dist) in zip(raw_ids, hits):
                    if len(eligible) >= topk:
                        break
                    # Defensive invariant guard: cluster hub nodes have no
                    # knowledge_embeddings row, so by construction they never
                    # appear in ann_labels — this filter only protects against
                    # a future index-format change; it is not load-bearing.
                    if nid.startswith("cluster:") or nid == oid:
                        continue
                    if not status_alive.get(nid, False):
                        continue  # deprecated or vanished object (legacy pool parity)
                    # Type filter (mirrors legacy path, which only ever loads
                    # object_type='concept' rows into existing_items): concept
                    # canonical ids are always "K-"-prefixed (never "KL-"/"KF-"/
                    # "KP-" — claim/formula/procedure), so this string check is
                    # exact and needs no extra DB lookup per hit.
                    other_cid = canonical_of.get(nid)
                    if not other_cid or not other_cid.startswith("K-"):
                        continue
                    eligible.append((nid, other_cid, max(0.0, 1.0 - float(dist))))
                if len(eligible) >= topk:
                    break
                if k >= hard_cap:
                    break
                tail_sim = max(0.0, 1.0 - float(hits[-1][1])) if hits else 0.0
                if tail_sim < lo:
                    break  # everything beyond the tail is below threshold anyway
                k = min(k * 2, hard_cap)
            if query_failed:
                continue
            for node_id, other_cid, sim in eligible:
                if sim < lo:
                    break  # eligible is distance-sorted ascending -> sim descending
                if other_cid == my_cid:
                    continue
                a, b = sorted((my_cid, other_cid))
                if frozenset((a, b)) in exclude or (a, b) in seen:
                    continue
                seen.add((a, b))
                out.append({"canonical_a": a, "canonical_b": b, "score": sim})
        return out

    # ------------------------------------------------------------------
    # Full-notebook build / rebuild (kg_building single-flight guard)
    # ------------------------------------------------------------------

    def _publish_pending_started(self) -> None:
        """kg_building 已登记后推一次待办快照,「知识图谱构建中」项才会立刻出现
        在已连接的铃铛里(此前只有 job 结束的 notify_pending 会刷新,运行期间要
        重连才看得到)。归属取请求用户 ContextVar(background_jobs 已 copy_context
        传播);CLI/离线路径解析不出 uid 时 publish_snapshot 自然 no-op。"""
        from app.core.request_context import request_user_id
        from app.services.pending_bus import publish_snapshot

        try:
            publish_snapshot(request_user_id())
        except Exception:  # noqa: BLE001 - notification is fail-open
            pass

    def _emit_kg_build_event(
        self,
        kind: str,
        job: dict,
        *,
        latency_ms: int = 0,
    ) -> None:
        """Emit only the reviewed task metadata; never prompts or diagnostics."""
        self.event_log.emit(
            {
                "kind": kind,
                "job_id": job["id"],
                "notebook_id": job["notebook_id"],
                "mode": job["mode"],
                "status": job["status"],
                "stage": job["stage"],
                "total_sources": int(job["total_sources"]),
                "completed_sources": int(job["completed_sources"]),
                "failed_sources": int(job["failed_sources"]),
                "error_code": job["error_code"],
                "latency_ms": max(0, int(latency_ms)),
            }
        )

    def _absorbing_repeated_termination(self, job_id: str, step) -> None:
        """反复执行 step,直到它不再被终止信号打断。

        中断收尾的**每一步**都必须顶住重复信号,不只是最长的那次等待:运维最自然的
        动作是「没反应,再按一次 Ctrl-C」,而第二个信号若落在 abort()(它会在 Condition
        上等)、cancel 循环、公布 stopping 或 finish() 期间并逃出去,结果就是守卫在排空
        前被放开、或那行永久留在 running——两者都是这次修复要消灭的状态。

        重跑是安全的:abort() 对已有失败是幂等的,cancel()/wait() 幂等,set_stage 与
        finish 都带 ``WHERE status='running'``(第二次自然是 0 行)。

        不用 ``signal.pthread_sigmask`` 屏蔽信号,因为信号掩码是**按线程**的:worker
        线程在设掩码之前就已创建、并未屏蔽,内核会把信号投给它们,而 CPython 的 C 层
        handler 仍会置标志、由主线程执行 Python handler——KeyboardInterrupt 照样会在
        掩码块内抛出。所以只能捕获并重试。

        要立刻结束仍可 kill -9:那属于不可捕获的终止,残留行由服务端启动兜底清理。
        """
        while True:
            try:
                step()
                return
            except (KeyboardInterrupt, SystemExit):
                self.event_log.logger.warning(
                    "KG job %s: ignoring repeated termination signal during "
                    "interrupt cleanup; the notebook guard is only released "
                    "after in-flight work stops and the job is settled",
                    job_id,
                )

    @staticmethod
    def _is_partial_kg_error(error_message: str) -> bool:
        if "retry_incomplete=1" in error_message:
            return True
        match = re.search(r"windows_failed=(\d+)/(\d+)", error_message)
        return bool(match and int(match.group(1)) > 0)

    def _kg_target_batches(
        self,
        notebook_id: str,
        mode: str,
        *,
        target_limit: int | None = None,
        retry_partial: bool = False,
    ) -> Iterator[Tuple[List[Tuple[str, bool]], List[str], List[str]]]:
        """Yield bounded durable-job targets as ``(source_id, preserve_old)``.

        The keyset advances over raw source rows, not over eligible targets.  A
        sparse notebook therefore never asks PostgreSQL to scan an unbounded
        prefix merely to find one page of missing/partial KG sources.
        """
        remaining = None if target_limit is None else max(0, int(target_limit))
        if remaining == 0:
            return
        after_created_at = None
        after_id = ""
        while True:
            with self._connect() as db:
                rows = self.knowledge.source_build_state_page(
                    db,
                    notebook_id,
                    after_created_at,
                    after_id,
                    _SOURCE_BUILD_PAGE_SIZE,
                )
            if not rows:
                return
            targets: List[Tuple[str, bool]] = []
            skipped: List[str] = []
            skipped_no_elements: List[str] = []
            for row in rows:
                source_id = str(row["id"])
                has_kg = bool(row["has_kg"])
                has_graph = bool(row["has_graph"])
                has_elements = bool(row["has_elements"])
                is_partial = has_graph and self._is_partial_kg_error(
                    str(row["latest_kg_error"] or "")
                )
                # 「跑完了、这篇里确实没有可整理的知识」不是待分析目标。没有这一条,
                # 零对象来源(正文极少 / 几乎全是图片的扫描件)会在**每一次**「分析新增」
                # 里被重新选中,重付一遍窗口的模型钱,而且抽完还是零、下一轮照样被选中
                # ——用户看到的就是「一直分析不完」。判据在 models.sources,与计数、
                # 来源徽标共用同一份表述。
                # 刻意只作用于增量模式:`mode == "rebuild"` 仍然重抽全部有元素的来源,
                # 换了模型或重新解析(带 OCR)之后要把这批来源捡回来,走的就是那条路。
                analyzed_empty = kg_analyzed_without_objects(
                    row["latest_kg_status"], row["latest_kg_error"]
                )
                if mode == "rebuild":
                    if has_elements:
                        targets.append((source_id, False))
                    else:
                        skipped_no_elements.append(source_id)
                elif retry_partial and is_partial:
                    targets.append((source_id, True))
                elif has_kg or is_partial or analyzed_empty:
                    skipped.append(source_id)
                elif has_elements:
                    targets.append((source_id, False))
                else:
                    skipped_no_elements.append(source_id)
                if remaining is not None and len(targets) >= remaining:
                    break
            yield targets, skipped, skipped_no_elements
            if remaining is not None:
                remaining -= len(targets)
                if remaining <= 0:
                    return
            after_created_at = rows[-1]["created_at"]
            after_id = str(rows[-1]["id"])
            if len(rows) < _SOURCE_BUILD_PAGE_SIZE:
                return

    def _kg_target_pages(
        self, notebook_id: str, mode: str
    ) -> Iterator[Tuple[List[str], List[str], List[str]]]:
        """Compatibility projection used by existing diagnostics/tests."""
        for targets, skipped, missing in self._kg_target_batches(
            notebook_id, mode
        ):
            yield [source_id for source_id, _preserve in targets], skipped, missing

    def _kg_target_count(
        self,
        notebook_id: str,
        mode: str,
        *,
        target_limit: int | None = None,
        retry_partial: bool = False,
    ) -> int:
        return sum(
            len(targets)
            for targets, _skipped, _missing in self._kg_target_batches(
                notebook_id,
                mode,
                target_limit=target_limit,
                retry_partial=retry_partial,
            )
        )

    def prepare_notebook_kg_job(
        self,
        notebook_id: str,
        mode: str,
        *,
        target_limit: int | None = None,
        retry_partial: bool = False,
        allow_without_model: bool = False,
    ) -> dict:
        if mode not in {"incremental", "rebuild"}:
            raise ValueError("unsupported KG build mode")
        self.get_notebook(notebook_id)
        if (
            not allow_without_model
            and not self.model_clients.configured("kg_extract")
        ):
            raise RuntimeError("LLM not configured; cannot build KG")
        # codex #663 R8/R9 P1: ``kg_building`` is a REAL admission gate for
        # build jobs (the durable single-flight is ``kg_build_jobs``'s
        # unique-active row, which a STANDALONE ``delete_notebook_kg``
        # deliberately does not create), and the reservation is ATOMIC —
        # check-and-add under one lock hold, so a standalone delete can no
        # longer slip in between an emptiness check here and the marker
        # landing after ``create_job``. Every failure path below releases
        # the reservation; on success the marker stays and the job run's
        # own finally releases it (``_run_notebook_kg_job``'s re-add is an
        # idempotent set.add covering runs resumed after a process
        # restart). Same single-worker deployment contract as quiesce leg B
        # (design doc §T-3.3 / 残余债 #10).
        with self.kg_building_lock:
            if notebook_id in self.kg_building:
                raise KgBuildAlreadyRunning(notebook_id)
            self.kg_building.add(notebook_id)
        try:
            return self._prepare_notebook_kg_job_reserved(
                notebook_id, mode,
                target_limit=target_limit, retry_partial=retry_partial,
            )
        except BaseException:
            with self.kg_building_lock:
                self.kg_building.discard(notebook_id)
            raise

    def _prepare_notebook_kg_job_reserved(
        self,
        notebook_id: str,
        mode: str,
        *,
        target_limit: int | None,
        retry_partial: bool,
    ) -> dict:
        """``prepare_notebook_kg_job``'s body, run with the ``kg_building``
        reservation already held (its docstring/comment carries the
        contract)."""
        target_count = self._kg_target_count(
            notebook_id,
            mode,
            target_limit=target_limit,
            retry_partial=retry_partial,
        )
        # create_job 先 INSERT 再 get(job_id) 返回。信号落在「已提交、尚未返回」之间时
        # job 根本没被赋值,连下面那层守卫都进不去,行就永久留在 running。这里按
        # notebook 找回它——但只在**新出现**且仍 running 的行上动手:调用前后比对最新行
        # id,变了才说明这次的 INSERT 已提交。若没变(中断落在 INSERT 之前),那行属于
        # 别人,离线进程无权裁决,绝不能碰。
        previous = self.kg_build_jobs.latest(notebook_id)
        previous_id = previous["id"] if previous is not None else ""
        try:
            job = self.kg_build_jobs.create_job(
                notebook_id,
                self._current_user_id(),
                mode,
                target_count,
            )
        except (KeyboardInterrupt, SystemExit):
            stranded = self.kg_build_jobs.latest(notebook_id)
            if (
                stranded is not None
                and stranded["id"] != previous_id
                and stranded["status"] == "running"
            ):
                self._settle_unentered_job(stranded["id"], notebook_id)
            raise
        # create_job 已经提交了持久 running 行,而下面还有三步(登记进程内标志、写事件
        # 文件、推送待办)。信号落在这里会直接逃出本方法,连调用方的兜底都进不去,行就
        # 永久留在 running。守卫必须从「行已存在」这一刻起生效。
        try:
            self._emit_kg_build_event("kg_build_started", job)
            self._publish_pending_started()
        except (KeyboardInterrupt, SystemExit):
            self._settle_unentered_job(job["id"], notebook_id)
            raise
        return job

    def _ingestion_drained_for_publish(self, notebook_id: str) -> bool:
        """发布事务之前等在飞已准入写收敛(codex #602 R13 P1);纯等待不 settle。

        写入闸(require_write_admission)自切换 pending 起就拦住新的 process_source
        准入;存量在飞写持有冻结旧代身份,若在发布之后落地,会用旧代 chunk/KG 覆盖
        刚发布的新代产物——source snapshot CAS 看不见它们(元素替换先于快照)。
        超时返回 False,由各发布点按本地失败约定 fail-closed(弃 stage、job 落
        failed 可重试),绝不带着未收敛的在飞写发布。"""
        return self._wait_ingestion_drain(
            notebook_id,
            float(self.settings.indexing_pipeline_publish_drain_timeout_seconds),
        )

    def finish_indexing_pipeline_job(
        self,
        notebook_id: str,
        job_id: str,
        *,
        succeeded: bool,
        pipeline_identity: tuple[str, str, str] | None = None,
    ) -> None:
        """Settle a reused durable rebuild job when KG extraction was skipped.

        A deployment without ``kg_extract`` can still finalize a pipeline that
        does not override KG extraction. The same durable single-flight row
        remains the authority/status surface; identity publication and the job
        terminal are committed together here.
        """
        job = self.kg_build_jobs.get(job_id)
        if job["notebook_id"] != notebook_id or job["mode"] != "rebuild":
            raise RuntimeError("KG build job does not match indexing rebuild")
        if succeeded:
            if pipeline_identity is None:
                raise ValueError("successful indexing rebuild requires identity")
            pipeline_id, pipeline_version, pipeline_generation = pipeline_identity
            if not self._ingestion_drained_for_publish(notebook_id):
                self.kg_build_jobs.discard_indexing_pipeline_stage(job_id)
                self.kg_build_jobs.finish(
                    job_id,
                    "failed",
                    error_code="indexing_pipeline_ingestion_active",
                    error_message="重建完成前仍有资料在导入；请等它们完成后重试重建。",
                )
                self._emit_kg_build_event(
                    "kg_build_failed", self.kg_build_jobs.get(job_id)
                )
                with self.kg_building_lock:
                    self.kg_building.discard(notebook_id)
                raise IndexingPipelineStalePlanError(notebook_id)
            if not self.kg_build_jobs.complete_indexing_pipeline_stage_without_kg(
                job_id
            ):
                self.kg_build_jobs.discard_indexing_pipeline_stage(job_id)
                raise IndexingPipelineStalePlanError(notebook_id)
            changed = self.kg_build_jobs.publish_indexing_pipeline_success(
                job_id,
                notebook_id,
                pipeline_id,
                pipeline_version,
                pipeline_generation,
            )
            if not changed:
                self.kg_build_jobs.discard_indexing_pipeline_stage(job_id)
                self.kg_build_jobs.finish(
                    job_id,
                    "failed",
                    error_code="indexing_pipeline_stale",
                    error_message="索引管线重建已被更新的选择替代。",
                )
                self._emit_kg_build_event(
                    "kg_build_failed", self.kg_build_jobs.get(job_id)
                )
                with self.kg_building_lock:
                    self.kg_building.discard(notebook_id)
                raise IndexingPipelineStalePlanError(notebook_id)
            event_kind = "kg_build_succeeded"
        else:
            self.kg_build_jobs.discard_indexing_pipeline_stage(job_id)
            changed = self.kg_build_jobs.finish(
                job_id,
                "failed",
                error_code="indexing_pipeline_rebuild_failed",
                error_message="索引管线重建未完成，请稍后重试。",
            )
            event_kind = "kg_build_failed"
        if changed:
            self._emit_kg_build_event(event_kind, self.kg_build_jobs.get(job_id))
            if succeeded:
                self._invalidate_unified_cache(notebook_id)
                self._invalidate_knowledge_counts(notebook_id)
        with self.kg_building_lock:
            self.kg_building.discard(notebook_id)

    def fail_notebook_kg_job_submission(self, job_id: str) -> bool:
        try:
            job = self.kg_build_jobs.get(job_id)
        except KeyError:
            return False
        failed = self.kg_build_jobs.fail_submission(job_id)
        if failed:
            self._emit_kg_build_event(
                "kg_build_failed",
                self.kg_build_jobs.get(job_id),
            )
            with self.kg_building_lock:
                self.kg_building.discard(job["notebook_id"])
        return failed

    def _warn_skipped_sources(
        self, skipped_no_elements: List[str]
    ) -> None:
        if skipped_no_elements:
            self.event_log.logger.warning(
                "build_notebook_kg: %d source(s) missing source_elements "
                "(parse not landed) — skipped extraction to avoid empty KG; "
                "run `batch_ingest reparse` to backfill: %s",
                len(skipped_no_elements),
                skipped_no_elements[:10],
            )

    def _extract_targets(
        self,
        notebook_id: str,
        targets: List[Tuple[str, bool]],
        skipped: List[str],
        skipped_no_elements: List[str],
        job_id: str,
        control: KgExtractionRunControl,
        controlled_client: TaskScopedKgClients,
        progress=None,
        on_abort=None,
        skip_policy: "ModelSkipPolicy | None" = None,
        indexing_pipeline_identity: tuple[str, str, str] | None = None,
    ) -> dict:
        import concurrent.futures as _cf
        from app.services.kg import scheduler as _kg_scheduler

        done: List[str] = []
        failed: List[str] = []

        partial_retried: List[str] = []
        partial_failed_preserved: List[str] = []
        model_skipped: List[str] = []

        def _extract_one(source_id: str, preserve_existing: bool) -> bool:
            control.raise_if_aborted()
            staged_indexing = indexing_pipeline_identity is not None
            if not staged_indexing:
                self._set_source_status(source_id, "extracting")
            # 跳过模式:本来源用自己的子控制器,模型不可用只掐这一路的在飞窗口。
            # 关闭时逐位复用任务级控制器与它那份已解析的 client 缓存(零行为差异)。
            source_control = (
                control.child() if skip_policy is not None else control
            )
            source_clients = (
                TaskScopedKgClients(
                    self.model_clients, self.settings, source_control
                )
                if skip_policy is not None
                else controlled_client
            )
            try:
                control.raise_if_aborted()
                # doc_type 终态收口（与上传流水线 process_source 同一套）：run_extraction
                # 开头就读走 doc_type 快照（它选 profile、进抽取 prompt，因而进 LLM 缓存
                # 键）。抽取期间并发重传若改了 doc_type，无条件落 'extracted' 会把新类型
                # 配上旧 profile 抽的 KG，且没有任何东西回来纠正。改为守卫落终态 + 不一致
                # 带新类型重跑（轮数上限），并原子补发 'extracted' 事件——终态与事件都由
                # 收口负责，这里不再单独 _set_source_status('extracted')（那次无条件 DB 写
                # 会在守卫落终态后的窗口里把并发 retype 翻起的 'extracting' 冲回旧类型）。
                extraction_kwargs: dict[str, object] = {
                    "kg_client": source_clients,
                    "preserve_existing_until_complete": preserve_existing,
                }
                if indexing_pipeline_identity is not None:
                    (
                        pipeline_id,
                        pipeline_version,
                        pipeline_generation,
                    ) = indexing_pipeline_identity
                    extraction_kwargs.update(
                        indexing_pipeline_id=pipeline_id,
                        indexing_pipeline_version=pipeline_version,
                        indexing_pipeline_generation=pipeline_generation,
                        indexing_stage_job_id=job_id,
                    )
                if staged_indexing:
                    self._run_extraction(source_id, **extraction_kwargs)
                else:
                    self._reconcile_extracted_terminal(
                        source_id,
                        # **_kw 收下 reconciling 透传的 frozen_pipeline_identity:
                        # 这条批处理路自己带 authorized extraction_kwargs,不用冻结
                        # 口径(两口径互斥,见 _kg_strategy_for_notebook)。
                        lambda sid, **_kw: self._run_extraction(
                            sid, **extraction_kwargs,
                        ),
                    )
                if preserve_existing:
                    partial_retried.append(source_id)
                if skip_policy is not None:
                    skip_policy.record_success()
                return True
            except KgBuildAborted as exc:
                if not staged_indexing:
                    self._set_source_status(
                        source_id,
                        "extracted" if preserve_existing else "parsed",
                        error_message="",
                    )
                if preserve_existing:
                    partial_failed_preserved.append(source_id)
                # 任务级熔断(Ctrl-C、探测失败、别的线程已升级)一律原样上抛;
                # 只有本来源自己的子控制器发起的中止才允许降级成"跳过这一个"。
                if skip_policy is None or control.aborted:
                    raise
                model_skipped.append(source_id)
                if skip_policy.record_skip(source_id):
                    # 连续失败到顶=服务真挂了,升级为原来的任务级熔断
                    # (由它公布 stopping/circuit_opened,与关闭跳过模式时同一条路)。
                    control.abort(exc.failure)
                    raise
                return False
            except (KeyboardInterrupt, SystemExit):
                # 与上一支同一件事,只是中断继承 BaseException 接不到:不退回 'parsed'
                # 这一篇来源就会一直显示「分析中」,直到后端重启才被兜底清理。
                if not staged_indexing:
                    self._set_source_status(
                        source_id,
                        "extracted" if preserve_existing else "parsed",
                        error_message="",
                    )
                if preserve_existing:
                    partial_failed_preserved.append(source_id)
                raise
            except Exception:  # noqa: BLE001 - isolate non-model source failure
                if not staged_indexing:
                    self._set_source_status(
                        source_id,
                        "extracted" if preserve_existing else "parsed",
                        error_message="",
                    )
                if preserve_existing:
                    partial_failed_preserved.append(source_id)
                self.event_log.logger.exception(
                    "build_notebook_kg failed for %s", source_id
                )
                return False
            finally:
                if skip_policy is not None:
                    # 注册表只为「父熔断要唤醒谁」存在,来源收尾即摘除,
                    # 规模因此有界于来源级并发度而不是本次任务的来源总数。
                    control.release_child(source_control)

        # 提交本身也在中断保护范围内(见下面的 try):大库要提交几十万个 job,这段窗口
        # 不短,中断落在这里时已提交的 worker 同样必须被取消并排空。故用增量填充的
        # dict 而非推导式——清理路径要能看到「提交到一半」的那部分。
        futures: dict = {}
        accepted: set = set()
        processed = set()
        progress_index = 0

        def _cancel_and_drain_accepted() -> None:
            # A completion token is registered before submit_job accepts its
            # wrapper.  A token cancelled in that pre-accept window is a bare
            # Future in CANCELLED (not CANCELLED_AND_NOTIFIED), and
            # concurrent.futures.wait() would block on it forever.  Only a
            # token whose cancel() fails can already be running/finished and
            # therefore needs draining.
            running_or_done = [
                pending for pending in accepted if not pending.cancel()
            ]
            if running_or_done:
                _cf.wait(running_or_done)

        def _record_result(future) -> KgBuildAborted | None:
            nonlocal progress_index
            if future in processed or future.cancelled():
                return None
            processed.add(future)
            source_id, _preserve_existing = futures[future]
            try:
                succeeded = bool(future.result())
            except KgBuildAborted as exc:
                return exc
            (done if succeeded else failed).append(source_id)
            self.kg_build_jobs.record_source_result(
                job_id, succeeded=succeeded
            )
            self._emit_kg_build_event(
                "kg_build_progress",
                self.kg_build_jobs.get(job_id),
            )
            progress_index += 1
            if progress is not None:
                try:
                    progress(
                        progress_index,
                        len(targets),
                        source_id,
                        succeeded,
                    )
                except Exception:  # noqa: BLE001 - progress is fail-open
                    pass
            return None

        try:
            for source_id, preserve_existing in targets:
                future = _kg_scheduler.submit_tracked_job(
                    accepted, _extract_one, source_id, preserve_existing
                )
                futures[future] = (source_id, preserve_existing)
            for future in _cf.as_completed(futures):
                abort = _record_result(future)
                if abort is None:
                    continue
                if on_abort is not None:
                    on_abort(abort)
                _cancel_and_drain_accepted()
                for drained in futures:
                    if drained is future:
                        continue
                    _record_result(drained)
                raise abort
        except (KeyboardInterrupt, SystemExit):
            # 中断只在主线程抛出,worker 还在飞——必须在这里就唤醒并**排空**它们,
            # 之后才允许上层落终态。单飞守卫是跨进程的(kg_build_jobs 上的条件唯一
            # 索引):一旦那行落了终态,活着的后端就能为同一 notebook 起新分析,而本
            # 进程尚未退出的 worker 会在那之后继续写图、改来源状态,把新任务的状态
            # 冲掉。与上面的熔断分支同形,只是失败分类换成人工中断,且用
            # notify=False——状态由外层收尾统一公布,不记模型侧的 circuit_opened。
            # 唤醒 + 取消 + 排空整段都要顶住重复中断(见
            # _absorbing_repeated_termination):等待按设计可能长到一次模型超时,而第二个
            # 信号无论落在 abort()、cancel 循环还是等待里,只要逃出去,外层就会在 future
            # 尚未排空时落终态、放开跨进程守卫,旧 worker 于是与新构建并发写入,这段修复
            # 就白做了。
            def _stop_and_drain() -> None:
                control.abort(
                    KgBuildFailure(
                        "worker_interrupted",
                        INTERRUPTED_KG_BUILD_ERROR_MESSAGE,
                    ),
                    notify=False,
                )
                _cancel_and_drain_accepted()

            self._absorbing_repeated_termination(job_id, _stop_and_drain)
            # 刻意不像熔断分支那样 _record_result(排空结果):中断是不可恢复的收尾,
            # 正在展开的栈上应尽量少写库。排空期间跑完的来源其图行本身已提交,下一次
            # 增量运行按覆盖度重算目标,不依赖这里的计数。
            raise

        done.sort()
        failed.sort()
        return {
            "built": done,
            "failed": failed,
            "skipped": skipped,
            "skipped_no_elements": skipped_no_elements,
            "partial_retried": sorted(partial_retried),
            "partial_failed_preserved": sorted(partial_failed_preserved),
            # `failed` 的子集:因模型不可用被跳过(而非来源自身出错)的那些。
            # 两份都给,调用方才分得清"这批到底是数据坏了还是服务抖了"。
            "model_skipped": sorted(model_skipped),
        }

    def _run_success_side_effects(
        self, notebook_id: str, result: dict, *, enqueue_fold: bool = True
    ) -> None:
        try:
            self._mark_unified_kg_dirty(notebook_id)
        except Exception:
            self.event_log.logger.exception(
                "unified-KG dirty mark failed for %s", notebook_id
            )
        if self.settings.kg_conflict_resolution_enabled:
            self._resolve_conflicts_after_build(notebook_id)
        if getattr(self.settings, "kg_relink_enabled", True):
            self._relink_after_build(notebook_id, result)
        if enqueue_fold:
            self.scale_artifacts.maybe_enqueue_fold(notebook_id)

    def _relink_after_build(self, notebook_id: str, result: dict) -> None:
        """Deterministic relink tail of a successful build — fail-open, observable,
        and under the SAME per-notebook single flight the endpoint claims.

        The claim is not decoration, but what it protects against differs by who
        else holds it:

        · A CONCURRENT RELINK (`POST /kg/relink`, or another build's tail) runs the
          SAME pass over the same notebook; without a shared claim a click landing
          while a build is finishing would start a second full read of every
          source, and only the idempotency triple would stop it from writing the
          edges twice — after both have already paid. Losing the claim here is a
          true SKIP: whoever holds it is doing exactly this work, nothing is lost.
        · A CONCURRENT 「重新合并」 REBUILD does DIFFERENT work — it rewrites
          `concept_clusters` and the community partition, it does not append the
          edges this tail exists to add. Losing the claim here is a deliberately
          accepted fail-open DROP, not "someone else is already doing this for
          me": this build's isolated-node connections simply do not happen this
          round and stay unconnected until a manual relink (or a later build's
          tail, if it wins the slot) runs. The two passes share the slot anyway
          because a concurrent write would have one publish over inputs the other
          is still consuming (the same reason `KgMaintenanceAlreadyRunning` exists
          at all) — sharing the slot is about write safety, not about the two
          passes being interchangeable.

        Fail-open is kept (a successful extraction must not be reported as failed
        because the deterministic tail could not run) but no longer SILENT — the
        whole-notebook shape used to raise MemoryError here and be swallowed whole,
        so the build reported success while the process was already dying.

        Single flight is per PROCESS. Production pins `--workers 1`, so it is the
        deployment-wide guard there; under multiple workers the endpoint and this
        tail only coordinate within one worker.
        """
        try:
            job = self.start_notebook_relink(notebook_id)
        except KgMaintenanceAlreadyRunning as exc:
            # `exc.holder` is "relink" or "rebuild" — see the two cases in the
            # docstring above. Surfaced on the event so an operator (or a future
            # debugging session) does not have to guess which of the two
            # fundamentally different skip reasons actually fired.
            self.event_log.logger.info(
                "build_notebook_kg: KG maintenance already running (holder=%s) for %s, skipped",
                exc.holder, notebook_id,
            )
            # Body-free by contract: kind + notebook id + a stable enum string,
            # no free text.
            self.event_log.emit({
                "kind": "kg_relink_skipped",
                "notebook_id": notebook_id,
                "holder": exc.holder,
            })
            return
        except Exception:  # noqa: BLE001 - relink is fail-open here
            self.event_log.logger.exception(
                "build_notebook_kg: relink claim failed for %s", notebook_id
            )
            return
        try:
            result["relink"] = self.run_notebook_relink_job(
                notebook_id, job["job_id"]
            )
        except Exception:  # noqa: BLE001 - relink is fail-open here
            # `run_notebook_relink_job` already released the claim, logged the
            # traceback and emitted the body-free failure event — both entry points
            # report through that one place, so nothing is emitted a second time.
            pass

    def _run_notebook_kg_job(
        self,
        notebook_id: str,
        job_id: str,
        mode: str,
        progress=None,
        *,
        target_limit: int | None = None,
        retry_partial: bool = False,
        finalize: Callable[[dict], dict | None] | None = None,
        skip_policy: "ModelSkipPolicy | None" = None,
        preserve_existing_rebuild: bool = False,
        indexing_pipeline_identity: tuple[str, str, str] | None = None,
    ) -> dict:
        job = self.kg_build_jobs.get(job_id)
        if (
            job["notebook_id"] != notebook_id
            or job["mode"] != mode
            or job["status"] != "running"
        ):
            raise RuntimeError("KG build job does not match this request")
        with self.kg_building_lock:
            self.kg_building.add(notebook_id)
        started = time.perf_counter()
        stopping_marked = False

        def _latency_ms() -> int:
            return round((time.perf_counter() - started) * 1000)

        def _mark_stopping(
            exc: KgBuildAborted, *, circuit: bool = True
        ) -> None:
            """circuit=False:操作者主动中断(Ctrl-C/SIGTERM)也要把在飞窗口停下并
            公布 'stopping',但它不是模型侧的熔断——不能记 kg_build_circuit_opened,
            否则事件日志里一次人工中断会被当成模型服务故障来统计。"""
            nonlocal stopping_marked
            if stopping_marked:
                return
            try:
                changed = self.kg_build_jobs.set_stage(
                    job_id,
                    "stopping",
                    error_code=exc.failure.code,
                    error_message=exc.failure.user_message,
                )
            except Exception:
                self.event_log.logger.exception(
                    "failed to publish stopping state for KG job %s",
                    job_id,
                )
                return
            if not changed:
                return
            stopping_marked = True
            stopping = self.kg_build_jobs.get(job_id)
            if circuit:
                self._emit_kg_build_event(
                    "kg_build_circuit_opened",
                    stopping,
                    latency_ms=_latency_ms(),
                )
            self._emit_kg_build_event(
                "kg_build_stopping",
                stopping,
                latency_ms=_latency_ms(),
            )

        control = KgExtractionRunControl(
            job_id,
            on_abort=lambda failure: _mark_stopping(
                KgBuildAborted(failure)
            ),
        )
        controlled_clients = TaskScopedKgClients(
            self.model_clients, self.settings, control
        )
        controlled_client = controlled_clients.chat("kg_extract")

        try:
            if mode == "rebuild":
                probe_kg_model(controlled_client)
                if not preserve_existing_rebuild:
                    try:
                        self.delete_notebook_kg(
                            notebook_id, held_by_kg_job=True
                        )
                    except NotebookDeletingAbortsMaintenanceError:
                        # T-5a review P2: the pre-reset drain's §4.2
                        # checkpoint fires on THIS job path too. Translate to
                        # the same abort shape the batch-loop checkpoint
                        # below uses — otherwise the maintenance-flavored
                        # exception falls through to ``except Exception`` and
                        # the user sees internal_error ("已完成内容已保留,
                        # 可继续分析") for a notebook that is being DELETED,
                        # while the identical action a few seconds later gets
                        # the truthful notebook_deleting copy.
                        deleting_failure = KgBuildFailure(
                            "notebook_deleting",
                            "该笔记本正在被删除，本次知识图谱构建已停止。",
                        )
                        control.abort(deleting_failure)
                        raise KgBuildAborted(deleting_failure)
            total_targets = int(job["total_sources"])
            if mode != "rebuild" and total_targets:
                # 起始探测**刻意不受跳过模式影响**(codex 第 1 轮 P2,驳回)。它是"服务
                # 现在活着吗"这一个问题的答案:探测失败即此刻服务不可用,而此时用户
                # 还没有任何投入,停下来的代价是零、重跑的代价也是零。放行反而更坏——
                # 跳过模式会对着一个已知挂掉的服务逐个来源白烧
                # (KG_LLM_MAX_RETRIES+1)×超时,凑满连续阈值后照样死,只是多留下几十个
                # 被标记跳过的来源。跳过模式要救的是"跑到一半的偶发波动",不是"开跑
                # 时服务就是死的"。反向护栏:
                # test_skip_mode_still_fails_fast_when_the_startup_probe_fails。
                probe_kg_model(controlled_client)
            self.kg_build_jobs.set_stage(job_id, "extracting")
            result = {
                "built": [],
                "failed": [],
                "skipped": [],
                "skipped_no_elements": [],
                "partial_retried": [],
                "partial_failed_preserved": [],
                "model_skipped": [],
            }
            processed = 0
            target_mode = (
                "rebuild"
                if mode == "rebuild" and preserve_existing_rebuild
                else "incremental"
            )
            for targets, skipped, skipped_no_elements in self._kg_target_batches(
                notebook_id,
                target_mode,
                target_limit=target_limit,
                retry_partial=retry_partial,
            ):
                # 批 3·W1 PR-3 §4.2 选项 A 的检查点(buildkg-/rebuildkg-,批
                # 边界)——走既有的 abort 通道,不新开一条。相位 2 的 quiesce
                # 闸腿 A 依赖它存在(design doc §T-3.3):没有它,quiesce 只能
                # 靠超时收场。一次 notebooks 主键点查,噪声级成本(批 = 多个
                # 来源,不是逐来源)。
                if self._notebook_deleting(notebook_id):
                    deleting_failure = KgBuildFailure(
                        "notebook_deleting",
                        "该笔记本正在被删除，本次知识图谱构建已停止。",
                    )
                    control.abort(deleting_failure)
                    raise KgBuildAborted(deleting_failure)
                if mode == "rebuild" and preserve_existing_rebuild:
                    targets = [(source_id, True) for source_id, _preserve in targets]
                self._warn_skipped_sources(skipped_no_elements)
                if indexing_pipeline_identity is not None:
                    empty_kg = {
                        "mode": "replace",
                        "objects": [],
                        "relations": [],
                        "object_sources": [],
                        "facts": [],
                        "fact_elements": [],
                        "object_vectors": [],
                        "relation_vectors": [],
                    }
                    for source_id in skipped_no_elements:
                        if not self.kg_build_jobs.stage_indexing_pipeline_kg(
                            job_id, source_id, empty_kg
                        ):
                            raise IndexingPipelineStalePlanError(notebook_id)

                def _page_progress(
                    index: int,
                    _page_total: int,
                    source_id: str,
                    succeeded: bool,
                    *,
                    _offset: int = processed,
                ) -> None:
                    if progress is not None:
                        progress(
                            _offset + index,
                            total_targets,
                            source_id,
                            succeeded,
                        )

                page_result = self._extract_targets(
                    notebook_id,
                    targets,
                    skipped,
                    skipped_no_elements,
                    job_id,
                    control,
                    controlled_clients,
                    _page_progress if progress is not None else None,
                    _mark_stopping,
                    skip_policy=skip_policy,
                    indexing_pipeline_identity=indexing_pipeline_identity,
                )
                for key in result:
                    result[key].extend(page_result[key])
                processed += len(targets)
            if indexing_pipeline_identity is not None and result["failed"]:
                raise IndexingPipelineKgExtractionFailedError()
            if indexing_pipeline_identity is None:
                self._run_success_side_effects(
                    notebook_id, result, enqueue_fold=finalize is None
                )
            if finalize is not None:
                finalized = finalize(result)
                if finalized:
                    result.update(finalized)
            if indexing_pipeline_identity is None:
                self.kg_build_jobs.finish(job_id, "succeeded")
            else:
                pipeline_id, pipeline_version, pipeline_generation = (
                    indexing_pipeline_identity
                )
                if not self._ingestion_drained_for_publish(notebook_id):
                    self.kg_build_jobs.discard_indexing_pipeline_stage(job_id)
                    raise IndexingPipelineStalePlanError(notebook_id)
                if not self.kg_build_jobs.publish_indexing_pipeline_success(
                    job_id,
                    notebook_id,
                    pipeline_id,
                    pipeline_version,
                    pipeline_generation,
                ):
                    raise IndexingPipelineStalePlanError(notebook_id)
                # Publication already dirtied and advanced the persistent
                # generation. Drop only process-local read caches before the
                # ordinary fail-open derived-product tail runs.
                self._invalidate_unified_cache(notebook_id)
                self._invalidate_knowledge_counts(notebook_id)
                self._run_success_side_effects(
                    notebook_id, result, enqueue_fold=finalize is None
                )
            self._emit_kg_build_event(
                "kg_build_succeeded",
                self.kg_build_jobs.get(job_id),
                latency_ms=_latency_ms(),
            )
            return {**result, "job_id": job_id}
        except KgBuildAborted as exc:
            _mark_stopping(exc)
            self.kg_build_jobs.finish(
                job_id,
                "failed",
                error_code=exc.failure.code,
                error_message=exc.failure.user_message,
            )
            self._emit_kg_build_event(
                "kg_build_failed",
                self.kg_build_jobs.get(job_id),
                latency_ms=_latency_ms(),
            )
            raise
        except (KeyboardInterrupt, SystemExit):
            # KeyboardInterrupt/SystemExit 继承 BaseException,走不到下面的
            # `except Exception`,而 finally 只清进程内的 kg_building 集合——不在这里
            # 收尾,kg_build_jobs 那行就永久停在 'running',
            # idx_kg_build_jobs_one_running(notebook 级条件唯一索引)会把该 notebook
            # 之后的每一次构建都挡成 KgBuildAlreadyRunning。对离线 CLI 尤其致命:
            # 启动期的崩溃兜底 _recover_interrupted_jobs 只由服务端 lifespan 调用
            # (离线进程无权裁决可能属于活跃后端的 job),所以 CLI 自己修不了,
            # 只能等后端重启。Ctrl-C 是 batch_ingest 长跑最常见的结束方式,
            # 必须在这里落终态。SIGKILL/掉电仍只能靠启动兜底。
            #
            # 同时要 abort 掉运行控制:中断只在主线程抛出,窗口/文档线程池还在跑,
            # 若不合作式停下,它们会在 job 已落终态之后继续调模型、继续写图,
            # 而 ThreadPoolExecutor 的 atexit join 又会把进程按在原地等它们跑完
            # ——用户看到的就是「Ctrl-C 没反应」,进而 kill -9,重新制造残留行。
            # 已有模型侧失败时沿用它的分类,不要用中断把真实故障码盖掉。
            def _settle() -> None:
                failure = control.failure or KgBuildFailure(
                    "worker_interrupted", INTERRUPTED_KG_BUILD_ERROR_MESSAGE
                )
                _mark_stopping(KgBuildAborted(failure), circuit=False)
                # notify=False:状态已由上一行按人工中断的口径公布过了。若那次写库
                # 撞上瞬时错误,stopping_marked 仍是 False,默认的 notify=True 会经
                # on_abort 再试一次——而那条回调是 circuit=True 的,重试一旦成功就会
                # 给一次人工中断记上 kg_build_circuit_opened,自相矛盾。宁可不重试那条
                # 瞬时的中间态(终态由下面的 finish 负责),也不能记错熔断。
                control.abort(failure, notify=False)
                # 信号可能落在成功路径 finish(succeeded) 之后、本函数返回之前:那时
                # 三次写都带 WHERE status='running',全部命中 0 行,任务其实已经成功
                # 提交。此时**不能**再拿那行已成功的记录发 kg_build_failed——事件里会
                # 带着 status="succeeded" 自相矛盾。只有真的把 running 落成 failed
                # (rowcount==1)才发失败事件。中断本身仍照原样抛出:分析虽已提交,
                # 本次运行确实被中断了(后续 rebuild/补向量都没跑)。
                settled = self.kg_build_jobs.finish(
                    job_id,
                    "failed",
                    error_code=failure.code,
                    error_message=failure.user_message,
                )
                if settled:
                    self._emit_kg_build_event(
                        "kg_build_failed",
                        self.kg_build_jobs.get(job_id),
                        latency_ms=_latency_ms(),
                    )

            def _settle_reporting_failures() -> None:
                try:
                    _settle()
                except Exception:  # noqa: BLE001 - 收尾失败不得吞掉中断本身
                    # 收尾自身失败(例如与工作线程抢写锁超时)绝不能把中断替换成另一个
                    # 异常:那样操作者拿到的是无关 traceback,而这行仍留在 'running'。
                    # 记下原因后按原样把中断抛出去(CLI 仍给「已中断」+130),这一行退回
                    # 由服务端启动兜底清理——与 SIGKILL 残留同一条出路。
                    self.event_log.logger.exception(
                        "failed to settle interrupted KG job %s", job_id
                    )

            # 落终态这一段同样要顶住重复中断:第二个信号若落在公布 stopping 或 finish()
            # 期间并逃出去,那行就永久留在 running(离线 CLI 无权自清)。
            self._absorbing_repeated_termination(
                job_id, _settle_reporting_failures
            )
            raise
        except IndexingPipelineKgExtractionFailedError:
            self.kg_build_jobs.finish(
                job_id,
                "failed",
                error_code="indexing_pipeline_kg_failed",
                error_message="索引管线知识抽取未通过校验，旧知识仍保留。",
            )
            self._emit_kg_build_event(
                "kg_build_failed",
                self.kg_build_jobs.get(job_id),
                latency_ms=_latency_ms(),
            )
            raise
        except Exception:
            self.kg_build_jobs.finish(
                job_id,
                "failed",
                error_code="internal_error",
                error_message=INTERNAL_KG_BUILD_ERROR_MESSAGE,
            )
            self._emit_kg_build_event(
                "kg_build_failed",
                self.kg_build_jobs.get(job_id),
                latency_ms=_latency_ms(),
            )
            raise
        finally:
            if indexing_pipeline_identity is not None:
                try:
                    self.kg_build_jobs.discard_indexing_pipeline_stage(job_id)
                except Exception:  # noqa: BLE001 - terminal cleanup is retryable
                    self.event_log.logger.exception(
                        "failed to discard indexing pipeline stage %s", job_id
                    )
            with self.kg_building_lock:
                self.kg_building.discard(notebook_id)

    def _settle_unentered_job(self, job_id: str, notebook_id: str) -> None:
        """兜住「行已建、但 _run_notebook_kg_job 还没进到自己的 try/finally」这个窗口。

        create_job 提交 running 行之后,到那个 try 之间还有几步(登记进程内标志、事件
        落盘、待办推送、一次 DB 读),信号落在这里时没有任何人收尾,行就永久留在
        running——正是本改动要消灭的状态。这里的收尾是幂等的:finish 带
        ``WHERE status='running'``,若 _run 内部已经落过终态,这次就是 0 行、什么也不做。

        进程内的 kg_building 标志也必须一并清掉:它由 prepare 登记、正常由
        _run_notebook_kg_job 的 finally 移除。走不到那个 finally 时若不在这里清,
        get_notebook()/索引状态会在**该进程余生**都报「构建中」,界面上的分析入口一直
        禁用,尽管数据库层面已经允许新任务。"""

        def _settle() -> None:
            try:
                if self.kg_build_jobs.finish(
                    job_id,
                    "failed",
                    error_code="worker_interrupted",
                    error_message=INTERRUPTED_KG_BUILD_ERROR_MESSAGE,
                ):
                    self._emit_kg_build_event(
                        "kg_build_failed", self.kg_build_jobs.get(job_id)
                    )
            except Exception:  # noqa: BLE001 - 收尾失败不得顶替中断本身
                self.event_log.logger.exception(
                    "failed to settle interrupted KG job %s", job_id
                )
            finally:
                with self.kg_building_lock:
                    self.kg_building.discard(notebook_id)

        self._absorbing_repeated_termination(job_id, _settle)

    def build_notebook_kg(
        self,
        notebook_id: str,
        *,
        progress=None,
        job_id: str | None = None,
        target_limit: int | None = None,
        retry_partial: bool = False,
        finalize: Callable[[dict], dict | None] | None = None,
        skip_policy: "ModelSkipPolicy | None" = None,
    ) -> dict:
        if job_id is None:
            job_id = self.prepare_notebook_kg_job(
                notebook_id,
                "incremental",
                target_limit=target_limit,
                retry_partial=retry_partial,
            )["id"]
            try:
                return self._run_notebook_kg_job(
                    notebook_id,
                    job_id,
                    "incremental",
                    progress,
                    target_limit=target_limit,
                    retry_partial=retry_partial,
                    finalize=finalize,
                    skip_policy=skip_policy,
                )
            except (KeyboardInterrupt, SystemExit):
                self._settle_unentered_job(job_id, notebook_id)
                raise
        return self._run_notebook_kg_job(
            notebook_id,
            job_id,
            "incremental",
            progress,
            target_limit=target_limit,
            retry_partial=retry_partial,
            finalize=finalize,
            skip_policy=skip_policy,
        )

    def execute_notebook_kg_job(
        self,
        notebook_id: str,
        job_id: str,
        mode: str,
        *,
        progress=None,
        target_limit: int | None = None,
        retry_partial: bool = False,
        finalize: Callable[[dict], dict | None] | None = None,
        preserve_existing_rebuild: bool = False,
        indexing_pipeline_identity: tuple[str, str, str] | None = None,
    ) -> dict:
        if mode == "incremental":
            return self.build_notebook_kg(
                notebook_id,
                progress=progress,
                job_id=job_id,
                target_limit=target_limit,
                retry_partial=retry_partial,
                finalize=finalize,
            )
        if mode == "rebuild":
            return self.rebuild_notebook_kg(
                notebook_id,
                job_id=job_id,
                finalize=finalize,
                preserve_existing=preserve_existing_rebuild,
                indexing_pipeline_identity=indexing_pipeline_identity,
            )
        raise ValueError("unsupported KG build mode")

    def rebuild_notebook_kg(
        self,
        notebook_id: str,
        *,
        job_id: str | None = None,
        finalize: Callable[[dict], dict | None] | None = None,
        preserve_existing: bool = False,
        indexing_pipeline_identity: tuple[str, str, str] | None = None,
    ) -> dict:
        if job_id is None:
            job_id = self.prepare_notebook_kg_job(
                notebook_id, "rebuild"
            )["id"]
            try:
                return self._run_notebook_kg_job(
                    notebook_id,
                    job_id,
                    "rebuild",
                    finalize=finalize,
                    preserve_existing_rebuild=preserve_existing,
                    indexing_pipeline_identity=indexing_pipeline_identity,
                )
            except (KeyboardInterrupt, SystemExit):
                self._settle_unentered_job(job_id, notebook_id)
                raise
        return self._run_notebook_kg_job(
            notebook_id,
            job_id,
            "rebuild",
            finalize=finalize,
            preserve_existing_rebuild=preserve_existing,
            indexing_pipeline_identity=indexing_pipeline_identity,
        )

    # ------------------------------------------------------------------
    # Unified-KG reads (status / graph / neighbors)
    # ------------------------------------------------------------------

    def unified_kg_status(self, notebook_id: str) -> dict:
        self.get_notebook(notebook_id)
        with self._connect() as db:
            row = self.unified_kg.state_row(db, notebook_id)
            # cluster_count is persisted at rebuild end (finish_rebuild_state); the
            # live COUNT(DISTINCT canonical_id) is only a null-fallback (see the
            # `row["cluster_count"] or clusters` below). Compute it ONLY when the
            # persisted value is absent — otherwise we scanned all member rows
            # (temp b-tree over concept_clusters, ~1 row/member) on every status
            # poll for a value that gets thrown away. Byte-identical result.
            clusters = 0
            if row is None or not row["cluster_count"]:
                clusters = self.unified_kg.distinct_cluster_count(db, notebook_id)
        viz = self.scale_artifacts.viz_probe(notebook_id)
        viz_building = notebook_id in self.scale_artifacts.viz_building
        if row is None:
            return {"dirty": False, "last_rebuild_at": "", "objects": 0, "relations": 0,
                    "clusters": int(clusters), **viz, "viz_building": viz_building}
        return {
            "dirty": bool(row["dirty"]),
            "last_rebuild_at": row["last_rebuild_at"],
            "objects": int(row["object_count"]),
            "relations": int(row["relation_count"]),
            "clusters": int(row["cluster_count"] or clusters),
            **viz,
            "viz_building": viz_building,
        }

    def unified_graph(self, notebook_id: str, level: str = "concept",
                      limit: Optional[int] = None) -> dict:
        """Graph data for the KG view. When `limit` is set and the full graph has
        more nodes, return only the `limit` most-connected ones (core subgraph) so
        the UI doesn't choke; `total_nodes`/`total_edges`/`truncated` let the
        frontend offer "widen range". The full graph is still derived + cached;
        the limit is a cheap slice applied after the cache.

        Large-notebook guard (checked FIRST, before any other branching): for a
        notebook whose non-deprecated object count exceeds
        settings.viz_sync_build_max_objects, _unified_graph_full must NEVER be
        called — it pulls every knowledge_objects row (full payloads) into
        Python dicts AND caches the multi-GB result in self.unified_cache,
        which is how a 490k-object production library fills 64GB RAM. This
        applies regardless of `limit`/`level`: a missing `limit` (old frontend,
        bare API calls, curl/tests) gets a server-side default cap
        (settings.viz_default_limit), and level='concept' is treated like
        'object' (the persisted folded viz graph is object-level only — the
        frontend always sends level=object, but we still defend the API for
        level=concept / no-level callers that would otherwise slip through)."""
        with self._connect() as db:
            nb_count = self.knowledge.count_active_objects(db, notebook_id)
        if int(nb_count) > self.settings.viz_sync_build_max_objects:
            effective_limit = limit if limit is not None else self.settings.viz_default_limit
            idx = self.scale_artifacts.viz_index(notebook_id)
            if idx is not None and getattr(idx, "viz_ids", None) is not None:
                return self._unified_graph_bounded(notebook_id, idx, effective_limit)
            # No index available yet: either a background build was just
            # spawned inside _viz_index, or (rare race) it isn't tracked as
            # building anymore. Either way, large notebooks never fall
            # through to _unified_graph_full below.
            return {"nodes": [], "edges": [], "total_nodes": 0,
                    "total_edges": 0, "truncated": False, "viz_building": True}

        # Bounded fast-path (small notebooks only, from here down): when a
        # limit is requested AND a valid scale index with a persisted folded
        # viz graph exists, serve the degree-top-N core straight from the
        # compact arrays (no full re-fold). EQUIVALENT to the legacy slice
        # below (same node-id set / totals / shape). Small notebooks with no
        # index fall through unchanged.
        if limit is not None and level != "concept":
            idx = self.scale_artifacts.viz_index(notebook_id)
            if idx is not None and getattr(idx, "viz_ids", None) is not None:
                return self._unified_graph_bounded(notebook_id, idx, limit)
            if idx is None and notebook_id in self.scale_artifacts.viz_building:
                # A background build was spawned inside _viz_index instead of
                # blocking this request on a minutes-long full-graph fold.
                # Surface a placeholder the frontend can poll on instead of
                # the (also expensive) _unified_graph_full fallback below.
                return {"nodes": [], "edges": [], "total_nodes": 0,
                        "total_edges": 0, "truncated": False, "viz_building": True}
        full = self._unified_graph_full(notebook_id, level)
        total_nodes, total_edges = len(full["nodes"]), len(full["edges"])
        from app.services.kg_merge import limit_graph_by_degree
        sliced = limit_graph_by_degree(full, limit) if limit is not None else full
        return {
            "nodes": sliced["nodes"],
            "edges": self._annotate_edge_support(notebook_id, sliced["edges"]),
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "truncated": len(sliced["nodes"]) < total_nodes,
        }

    def _unified_graph_full(self, notebook_id: str, level: str = "concept") -> dict:
        self.get_notebook(notebook_id)
        cached = self.unified_cache.get((notebook_id, level))
        if cached is not None:
            return cached
        from app.services.kg_merge import derive_unified_graph
        with self._connect() as db:
            nrows = self.knowledge.unified_graph_rows(db, notebook_id)
        nodes = [{"id": r["id"], "object_type": r["object_type"], "payload": json.loads(r["payload"] or "{}")} for r in nrows]
        edges = [{"source_object_id": r["source_object_id"], "target_object_id": r["target_object_id"], "edge_type": r["edge_type"]}
                 for r in self.relations_for_notebook(notebook_id)]
        g = derive_unified_graph(nodes, edges, self.cluster_map(notebook_id))
        if level == "concept":
            cids = {n["id"] for n in g["nodes"] if n["object_type"] == "concept"}
            g = {"nodes": [n for n in g["nodes"] if n["object_type"] == "concept"],
                 "edges": [e for e in g["edges"] if e["source_object_id"] in cids and e["target_object_id"] in cids]}
        self.unified_cache[(notebook_id, level)] = g
        return g

    def _viz_dict(self, idx):
        """Pack a ScaleIndex's persisted viz arrays into the dict shape the pure
        viz_neighbors function expects (1-hop topology over the folded graph).

        The id→position map rides along from the index's own lazy cache so a
        neighbour click stops rebuilding it over every folded node."""
        return {"viz_ids": idx.viz_ids, "viz_adj": idx.viz_adj,
                "viz_deg": idx.viz_deg, "viz_types": idx.viz_types,
                "viz_node_index": idx.viz_node_index()}

    def _viz_label_maps(self, idx, node_positions):
        """(name_by_id, type_by_id) restricted to the folded nodes at
        `node_positions` — the ones this response will actually render.

        Semantics are identical to the previous full `zip(viz_ids, viz_names)`
        dicts: a position past the end of a short names/types array contributes
        no entry, so `_viz_node`'s `.get(fid, "")` still yields "". Only the
        width changed (kept nodes instead of every node).

        Addressed by POSITION, not id, on purpose: the bounded core already
        holds the kept positions, and resolving them through `viz_node_index()`
        would build — and then permanently cache on an LRU-resident index — a
        full id→position dict for every folded node just to re-find rows we had
        already located. The neighbour path passes positions it looked up from
        that map for other reasons (edge orientation), so it pays nothing extra."""
        ids = idx.viz_ids or []
        names = idx.viz_names or []
        types = idx.viz_types or []
        name_by_id = {}
        type_by_id = {}
        for position in node_positions:
            position = int(position)
            if not (0 <= position < len(ids)):
                continue
            node_id = ids[position]
            if position < len(names):
                name_by_id[node_id] = names[position]
            if position < len(types):
                type_by_id[node_id] = types[position]
        return name_by_id, type_by_id

    def _viz_node(self, idx, fid, name_by_id, type_by_id):
        """One folded node in the unified_graph shape {id,object_type,payload:{name}}.
        Names/types come from the persisted viz arrays (no DB round-trip)."""
        return {"id": fid, "object_type": type_by_id.get(fid, ""),
                "payload": {"name": name_by_id.get(fid, "")}}

    def _unified_graph_bounded(self, notebook_id: str, idx, limit: int) -> dict:
        """Degree-top-N core from the persisted folded viz graph — EQUIVALENT to
        limit_graph_by_degree(_unified_graph_full(nb,'object'), limit): same node-id
        set (incl. degree-tie order), same kept edges, same totals/shape.

        Selection mirrors limit_graph_by_degree EXACTLY: stable sort by degree desc
        preserving viz_ids order (which equals _unified_graph_full's node order),
        keep the first `limit`, then keep edges whose both endpoints survive.

        New artifacts carry the stable degree order and a (src,dst,original)
        edge order. A bounded request uses pair-range binary searches for kept
        source×target pairs, so even a kept high-degree hub does not cause a full
        adjacency-segment scan. Old artifacts lazily derive the same helpers
        once. Returned nodes and edges remain in legacy original order."""
        import numpy as np
        ids = idx.viz_ids or []
        total_nodes = len(ids)
        edge_set = idx.viz_edges
        total_edges = len(edge_set) if edge_set is not None else 0

        if limit is None or limit >= total_nodes:
            keep_positions = np.arange(total_nodes)
        else:
            # Same slice semantics as the old `order[:limit]` over a Python list,
            # including a negative `limit` (drops from the tail).
            keep_positions = idx.viz_deg_order()[:limit]

        # Sorting at most the requested positions restores viz_ids order without
        # allocating/scanning a total_nodes-wide membership vector.
        kept_order = np.sort(np.asarray(keep_positions, dtype=np.int64))
        keep_ids = [ids[int(i)] for i in kept_order]
        # Positions, not ids: this path must never build the index-wide
        # id→position map (see _viz_label_maps).
        name_by_id, type_by_id = self._viz_label_maps(idx, kept_order)

        nodes = [self._viz_node(idx, fid, name_by_id, type_by_id) for fid in keep_ids]
        kept_edges = []
        if total_edges:
            selected = edge_set.induced_edge_indices(kept_order)
            table = edge_set.type_table
            src, dst, code = edge_set.src, edge_set.dst, edge_set.code
            kept_edges = [
                {
                    "source_object_id": ids[int(src[i])],
                    "target_object_id": ids[int(dst[i])],
                    "edge_type": table[int(code[i])],
                }
                for i in selected.tolist()
            ]
        kept_edges = self._annotate_edge_support(notebook_id, kept_edges)
        return {
            "nodes": nodes,
            "edges": kept_edges,
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "truncated": len(nodes) < total_nodes,
        }

    def _participant_source_notebook(
        self,
        active_notebook_id: str,
        object_id: str,
        source_notebook_id: str,
    ) -> str:
        """Resolve one globally unique raw object inside the active federation.

        The request is authorized against ``active_notebook_id`` by the route;
        mounted public bases are data participants, not notebook members, so the
        browser must not access them directly. Every probe is an indexed lookup
        for exactly one object id and the participant list is already bounded by
        the notebook's explicit mounts.
        """
        participants = self.participant_notebook_ids(active_notebook_id)
        if not participants:
            return active_notebook_id
        hint = source_notebook_id if source_notebook_id in participants else ""
        ordered = ([hint] if hint else []) + [
            notebook_id for notebook_id in participants if notebook_id != hint
        ]
        with self._connect() as db:
            for notebook_id in ordered:
                if self.knowledge.object_meta_rows_for_notebook(
                    db, notebook_id, [object_id]
                ):
                    return notebook_id
                if self.unified_kg.cluster_fold_rows(
                    db, notebook_id, [object_id]
                ):
                    return notebook_id
        # A canonical K-* id is synthetic and therefore has no object row. Its
        # source hint came from a previously resolved neighborhood response; use
        # it only after all raw-id probes miss.
        return hint or active_notebook_id

    def kg_neighbors(
        self,
        notebook_id: str,
        object_id: str,
        cap: int = 50,
        *,
        source_notebook_id: str = "",
    ) -> dict:
        """1-hop neighborhood of `object_id` (≤cap) in the folded concept graph.

        Fast path: persisted viz graph → viz_neighbors → hydrate names/edge_types
        (same node/edge shape as unified_graph). Fallback (no index): a bounded
        1-hop query over knowledge_relations, folding endpoints via cluster_map so
        the shape matches the unified graph the frontend already renders.

        Ask anchors carry raw knowledge-object ids, while the visual graph folds
        Concepts to canonical K-* ids. Resolve just this one requested id through
        the indexed cluster table before either path; never load the full cluster
        map on the indexed path."""
        self.get_notebook(notebook_id)
        source_id = notebook_id
        if source_notebook_id:
            source_id = self._participant_source_notebook(
                notebook_id, object_id, source_notebook_id
            )
        result = self._kg_neighbors_unchecked(source_id, object_id, cap)
        result["source_notebook_id"] = source_id
        return result

    def _kg_neighbors_unchecked(
        self, notebook_id: str, object_id: str, cap: int
    ) -> dict:
        """Participant-authorized neighbor read; caller already checked scope."""
        focus_id = object_id
        with self._connect() as db:
            fold_rows = self.unified_kg.cluster_fold_rows(
                db, notebook_id, [object_id]
            )
        if fold_rows:
            focus_id = fold_rows[0]["canonical_id"]
        idx = self.scale_artifacts.viz_index(notebook_id)
        if idx is not None and getattr(idx, "viz_ids", None) is not None:
            from app.services.kg.scale_index import viz_neighbors
            nb = viz_neighbors(self._viz_dict(idx), focus_id, cap)
            nbr_ids = {n["id"] for n in nb["nodes"]}
            # This path needs the id→position map anyway (edge orientation
            # below), so resolving ≤cap+1 labels through it costs nothing extra.
            positions = idx.viz_node_index()
            name_by_id, type_by_id = self._viz_label_maps(
                idx, [positions[fid] for fid in nbr_ids if fid in positions]
            )
            nodes = [self._viz_node(idx, fid, name_by_id, type_by_id) for fid in nbr_ids]
            # viz_neighbors exposes an undirected adjacency as focus -> neighbour,
            # but the rendered relation is directed. Recover the persisted
            # orientation so opening a target reached by an incoming edge does
            # not manufacture a second, reversed relation in the overlay.
            # The grouped lookup binary-searches each distinct directed pair in
            # the (src,dst,original) auxiliary order; it used to rebuild a full
            # (s,t)→edge_type dict per click, then briefly scanned the focus
            # segment once per neighbour. Multi-edge (s,t) still resolves to the
            # LAST persisted edge. `positions` was resolved above.
            edge_set = idx.viz_edges
            # Sentinel, not None: "no such edge" must stay distinguishable from
            # "edge exists and its edge_type is falsy", which `(s,t) in et_map`
            # gave for free.
            absent = object()
            neighbor_pairs = [(e["source"], e["target"]) for e in nb["edges"]]
            directed_pairs = [
                (positions.get(source_id), positions.get(target_id))
                for source_id, target_id in neighbor_pairs
            ]
            # Ask both orientations in one grouped pass. Duplicate pairs share
            # one pair-range lookup and no high-degree segment is scanned.
            queried = (
                edge_set.edge_types_at_many(
                    directed_pairs
                    + [(target, source) for source, target in directed_pairs],
                    default=absent,
                )
                if edge_set is not None
                else [absent] * (2 * len(directed_pairs))
            )
            split = len(directed_pairs)
            edges = []
            for edge_index, (s, t) in enumerate(neighbor_pairs):
                et = queried[edge_index]
                if et is not absent:
                    edge_s, edge_t = s, t
                else:
                    reversed_et = queried[split + edge_index]
                    if reversed_et is not absent:
                        edge_s, edge_t, et = t, s, reversed_et
                    else:
                        edge_s, edge_t, et = s, t, "related"
                edges.append({"source_object_id": edge_s, "target_object_id": edge_t,
                              "edge_type": et})
            edges = self._annotate_edge_support(notebook_id, edges)
            return {
                "nodes": nodes,
                "edges": edges,
                "focus_id": focus_id,
                # The graph renders the folded canonical id, while object
                # context is stored on the cited raw knowledge-object row.
                "focus_object_id": object_id,
            }
        # The legacy DB fallback materialises the complete concept-cluster map.
        # It remains acceptable for a small notebook, but must never run while a
        # large notebook's compact viz artifact is being built or repaired.
        with self._connect() as db:
            object_count = self.knowledge.count_active_objects(db, notebook_id)
        if int(object_count) > self.settings.viz_sync_build_max_objects:
            return {
                "nodes": [],
                "edges": [],
                "focus_id": focus_id,
                "focus_object_id": object_id,
                "locating_unavailable": True,
            }
        result = self._kg_neighbors_db(notebook_id, focus_id, cap)
        result["focus_id"] = focus_id
        result["focus_object_id"] = object_id
        return result

    def _kg_neighbors_db(self, notebook_id: str, object_id: str, cap: int) -> dict:
        """DB fallback for kg_neighbors: bounded 1-hop over knowledge_relations,
        folding concept endpoints to canonical ids (cluster_map) so the result
        matches the folded unified-graph view. `object_id` is interpreted as a
        folded id (canonical_id or raw object_id)."""
        cmap = self.cluster_map(notebook_id)
        # member -> canonical for matching folded id back to raw endpoints
        members_of = {}
        for m, c in cmap.items():
            members_of.setdefault(c, []).append(m)
        raw_ids = set(members_of.get(object_id, [object_id]))
        with self._connect() as db:
            rows = self.knowledge.neighbor_relation_rows(db, notebook_id, raw_ids)
        # object_type + name lookup for the folded nodes we touch
        def canon(oid):
            return cmap.get(oid, oid)
        edges, nbr_ids, seen = [], set(), set()
        for r in rows:
            s, t = canon(r["source_object_id"]), canon(r["target_object_id"])
            if s == t:
                continue
            if s == object_id:
                other = t
            elif t == object_id:
                other = s
            else:
                continue
            if other in seen or len(seen) >= cap:
                continue
            seen.add(other)
            nbr_ids.add(other)
            edges.append({"source_object_id": s, "target_object_id": t,
                          "edge_type": r["edge_type"]})
        # Include the queried node itself only when it's a known canonical id or
        # has neighbours; unknown / non-canonical ids that produced no edges are
        # dropped so that both the viz fast-path and the DB path return equivalent
        # empty results for unrecognised lookups.
        canonical_ids = set(cmap.values())
        include_self = bool(nbr_ids) or object_id in canonical_ids
        all_ids = (nbr_ids | {object_id}) if include_self else nbr_ids
        meta = self._object_meta(notebook_id, all_ids, cmap)
        nodes = [{"id": oid, "object_type": meta.get(oid, ("", ""))[0],
                  "payload": {"name": meta.get(oid, ("", ""))[1]}} for oid in all_ids]
        edges = self._annotate_edge_support(notebook_id, edges)
        return {"nodes": nodes, "edges": edges}

    def _object_meta(self, notebook_id: str, folded_ids, cmap):
        """{folded_id: (object_type, name)} for a set of folded ids. A folded
        concept id is either a canonical_id (resolve via a member object) or a raw
        object id. Used by the DB neighbors fallback to hydrate node display."""
        members_of = {}
        for m, c in cmap.items():
            members_of.setdefault(c, []).append(m)
        # representative raw object id per folded id
        rep = {fid: (members_of.get(fid, [fid])[0]) for fid in folded_ids}
        rep_ids = set(rep.values())
        if not rep_ids:
            return {}
        with self._connect() as db:
            rows = self.knowledge.object_meta_rows_for_notebook(
                db, notebook_id, rep_ids
            )
        by_raw = {r["id"]: (r["object_type"], json.loads(r["payload"] or "{}").get("name", ""))
                  for r in rows}
        return {fid: by_raw.get(rep[fid], ("", "")) for fid in folded_ids}

    # ------------------------------------------------------------------
    # Streamed unified rebuild
    # ------------------------------------------------------------------

    def _cluster_input_version(self, notebook_id: str, *, exclude_emb_count: bool = False) -> str:
        """O(1) content-hash gating rebuild_unified_kg's skip-when-unchanged path.

        PRIMARY change signal = the monotonic kg_mutation_seq, bumped by
        _mark_unified_kg_dirty on EVERY KG write (the single choke point all
        mutations funnel through). It advances deterministically on ANY edit —
        adds, deletes, AND in-place edits (concept rename, confirmed<->rejected
        decision flip, re-embed) — with NO dependence on timestamp granularity. An
        earlier version used COUNT+MAX(updated_at/created_at); at _now()'s 1-second
        resolution that MISSED same-second in-place edits at fixed cardinality
        (COUNT unchanged, MAX pinned by another row) and could serve a stale
        clustering. The seq closes that hole by construction.

        BACKSTOP = three COUNTs (objects status!='deprecated'; confirmed/rejected
        decided pairs — WHERE mirrors decided_pairs() EXACTLY, pending excluded so
        rebuild's own pending-refresh doesn't move the version; embeddings) plus
        settings.embed_dim. These are pure belt-and-suspenders: they catch an
        add/delete that somehow bypassed _mark_unified_kg_dirty. No timestamps.

        Clustering SETTINGS (thresholds, rep_ann_max) are intentionally NOT here;
        the explicit 刷新图谱 / recluster paths pass force=True to pick those up.

        Stable across a rebuild: rebuild writes only concept_clusters + pending
        concept_merge_candidates and preserves kg_mutation_seq (its end-write omits
        the seq column), so this value is identical before and after a rebuild.

        exclude_emb_count: when True, OMIT the embeddings COUNT term (emb_c) from
        the hash — the result is intentionally DIFFERENT from the default (False)
        call for the same notebook/state; it is NOT a drop-in replacement, it's a
        separate "backfill-stable" version namespace. Used by rebuild_unified_kg's
        LLM-stage checkpoints (merge_review, concept_desc): the node-vector
        backfill commits INCREMENTALLY, so emb_c climbs mid-backfill while seq/
        obj_c/dec_c stay put. Keying those checkpoints on the default (emb-
        inclusive) version would make a crash-then-resume during node-vector
        backfill look like a version change and GC away a possibly hours-long
        adjudication that is still perfectly valid. item_key already provides
        fine-grained correctness within a checkpoint, so dropping emb_c here is
        safe. The skip-gate itself must keep using the DEFAULT (emb-inclusive)
        version unchanged — a new/changed embedding is a real reason to recluster.
        """
        with self._connect() as db:
            seq, obj_c, dec_c, emb_c = self.unified_kg.cluster_input_facts(
                db, notebook_id, exclude_emb_count=exclude_emb_count
            )
        # runtime_dim 追加(非替换 embed_dim;T3/R4):聚类/融合空间统一到运行时
        # 空间,切 EMBED_RUNTIME_DIM 后版本闸不得跳过重聚(旧空间的簇不再有效)。
        from app.services.vector_index import resolve_runtime_dim
        # 算法版本(纯代码分量):归一化/哨兵/护栏等 seed·聚类语义的代码改动不动任何
        # 数据派生分量(seq/COUNT/dim 全不变),但会改变聚类结果。折进版本串后,已部署
        # 库改代码 → 下次「刷新图谱」(force=False)因版本失配真重算一次,不再被闸静默
        # 跳过。bump 约定见 kg_merge.CLUSTER_ALGO_VERSION。函数内 import:测试可 patch
        # kg_merge 模块属性,调用时按 patch 后的值读取。
        from app.services.kg_merge import CLUSTER_ALGO_VERSION
        parts = [
            "v2", notebook_id,
            int(seq),
            int(obj_c),
        ]
        if not exclude_emb_count:
            parts.append(int(emb_c))     # 默认路径:与旧版位次/取值一字不差,skip-gate 哈希不变
        parts += [
            int(dec_c),
            int(self.settings.embed_dim),
            resolve_runtime_dim(self.settings),
            int(CLUSTER_ALGO_VERSION),
        ]
        return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()

    def _stream_seed_reps(self, notebook_id: str, object_type: str, seed_fn,
                          run_id: str = "", compute_reps: bool = True):
        """Stream knowledge_objects of one type → populate kg_cluster_scratch
        (object_id → seed) and accumulate seed-level aggregates, memory-bounded
        by #unique seeds (NOT #objects). Returns (reps, members_count,
        seed_first_name):
          - reps[seed]          = mean of member vectors (dim-filtered); empty
                                  dict when compute_reps=False (skips Pass B).
          - members_count[seed] = #objects for that seed
          - seed_first_name[s]  = first-seen object name for that seed
        kg_cluster_scratch rows are scoped by (notebook_id, run_id) so concurrent
        rebuilds of the same notebook never wipe each other's scratch. The caller
        is responsible for clearing rows with the same (notebook_id, run_id) before
        calling this and after all types are processed.

        compute_reps=False skips Pass B (embeddings join) entirely and returns
        reps={}. Use for non-concept types (claim/formula/procedure) where
        cluster_seeds receives {} vectors anyway — avoids a large ANN over
        non-concept seeds."""
        import numpy as np
        from app.services.kg_merge import (build_acronym_alias_map, _seed_with_alias,
                                           _norm, seed_or_unique)

        embed_dim = self.settings.embed_dim
        # Scratch is scoped to (notebook_id, run_id); clear only this run's rows
        # for the current type before repopulating (types are processed serially
        # within a run, so a simple delete by run_id is safe).
        with self._write() as db:
            self.unified_kg.clear_scratch_run(db, notebook_id, run_id)

        # Pass A1: stream names once to build the acronym alias map.
        def _name_gen():
            with self._connect() as db:
                cur = self.unified_kg.seed_payload_rows(db, notebook_id, object_type)
                for r in cur:
                    yield _fast_loads(r["payload"] or "{}").get("name", "")
        alias_map = build_acronym_alias_map(_name_gen())

        # Pass A2: stream (id, name) → seed; accumulate counts/first-name; buffer
        # (notebook_id, id, seed) into scratch in batches.
        members_count: Dict[str, int] = {}
        seed_first_name: Dict[str, str] = {}
        with self._connect() as rdb:
            # ORDER BY rowid: canonical-name selection here is first-seen per seed
            # (seed_first_name), so the stream order must be deterministic and
            # independent of which index the planner happens to pick — otherwise
            # adding/removing an index silently changes canonical names + desc-cache
            # keys. rowid = insertion order, matching the historical behaviour.
            cur = self.unified_kg.stream_seed_rows(rdb, notebook_id, object_type)
            # 写锁瘦身:Python 计算(_fast_loads / seed_or_unique / alias 匹配)留在
            # 事务外,只有 executemany 进写锁。kg_cluster_scratch 有三个读者——
            # scratch_vector_rows(Pass B)、stream_scratch_rows(_write_cluster_map_
            # streamed)、cluster_evidence_rows(concept_desc 阶段)——但三者都只在
            # 本 Pass A2 循环*之后*才被调用;循环运行期间没有并发读者,分批提交不
            # 产生任何可见中间态。三者都按 run_id 过滤,这保证了并发 rebuild 之间
            # 不会串(见下面的不变量注释:这一保证要求本循环跑到耗尽)。
            #
            # ⚠ 不变量(勿改行为,仅记录):这个生成器必须跑到耗尽,不得 break/
            # 提前 return。cur 是 self._connect() 返回的线程本地复用读连接上的一
            # 个步进游标;它没耗尽之前,这条连接就钉在游标启动时的读快照上。下面
            # Pass B 的游标在同一条复用连接上打开,能看到本循环写入的 scratch 行,
            # 前提正是这个循环已经耗尽、旧快照已经释放——谁在这里加 break/提前
            # return,Pass B 就会静默读到更早的快照(零 scratch 行),聚类结果严重
            # 错误且不会抛出任何异常。_bulk_write 的 `for batch in batches:` 天然
            # 跑到 StopIteration 才停(没有 break),这个不变量继续成立。
            #
            # 写锁瘦身 + 公平性(Task 7):批提交改走 _bulk_write——它在自己的
            # for 循环里逐批 `with self.write(): apply(...)`,每批独立提交、
            # 批间完整释放写锁,不做任何 sleep 或"看 waiters 决定要不要让路"的
            # 判断(那段机制曾经存在过,已删除——仪器开着时它测不出锁的公平性、
            # 纯属多余,仪器关着时 stats is None 让它永远不会执行,即在唯一
            # 需要它的配置里反而是死代码;详见 bulk_write() 自己的 docstring 和
            # test_bulk_write_never_sleeps)。公平性现在完全由写锁本身
            # (threading.Lock,靠 PyMutex 的
            # eventual-fairness handoff 直接交接给排队者)保证,与批数、批间
            # 是否 sleep、DB_WRITE_LOCK_STATS 开关都无关。_batches() 是生成器,
            # 惰性求值:_bulk_write 每次 next() 只会推进到下一个 yield,期间跑的
            # Python 计算(_fast_loads/seed_or_unique/alias 匹配)天然发生在**上一批
            # write() 块退出之后、下一批 write() 块打开之前**——即仍在写事务外,
            # 与改造前语义等价,只是提交点从"每 1000 行"变成了"每 1000 行,
            # 批间不做任何额外判断"。
            def _batches():
                local: List[tuple] = []
                for r in cur:
                    pay = _fast_loads(r["payload"] or "{}")
                    name = pay.get("name", "")
                    seed = seed_or_unique(
                        _seed_with_alias({"name": name, "payload": pay}, seed_fn, alias_map),
                        r["id"])
                    members_count[seed] = members_count.get(seed, 0) + 1
                    seed_first_name.setdefault(seed, name)
                    local.append((notebook_id, run_id, r["id"], seed))
                    if len(local) >= 1000:
                        yield list(local)
                        local.clear()
                if local:
                    yield list(local)

            self._bulk_write(
                _batches(),
                lambda wdb, rows: self.unified_kg.insert_scratch_rows(wdb, rows),
            )

        # Pass B: stream vectors joined to seeds → rep mean per seed. Bounded by
        # #unique seeds (rep_sum/rep_cnt), NOT #objects. Dim-mismatched legacy
        # vectors are skipped (mirrors the old length filter).
        # Skipped entirely when compute_reps=False (non-concept types where
        # cluster_seeds receives {} vectors anyway — avoids an ANN over
        # millions of claim/formula/procedure seeds at scale).
        if not compute_reps:
            return {}, members_count, seed_first_name

        from app.services.vector_index import decode_vector, resolve_runtime_dim, truncate_vec

        # 运行时截断旁路(计划 §1.2 旁路 4):Pass B 的 legacy-JSON 分支绕过
        # decode_vector,是点名的已知漏点 —— 两分支在共同的存储维过滤(既有语义,
        # 先过滤)之后、rep 累加之前统一截断到运行时空间(seed rep 内存亦 ÷4)。
        rd = resolve_runtime_dim(self.settings)
        rep_dim = rd if (rd and rd < embed_dim) else embed_dim   # 截断后的确定维(R9)
        rep_sum: Dict[str, "np.ndarray"] = {}
        rep_cnt: Dict[str, int] = {}
        with self._connect() as db:
            # 依赖 Pass A2 的 for 循环已经跑到耗尽(见那里标注的不变量):Pass B
            # 与 Pass A2 共用同一条 self._connect() 线程本地读连接,只有旧读快照
            # 已经释放,这里才能看到 Pass A2 刚提交的 scratch 行,而不是一个更早
            # 、看不见任何行的快照。
            cur = self.unified_kg.scratch_vector_rows(db, notebook_id, run_id)
            for r in cur:
                # decode_vector: bytes(BLOB)->frombuffer zero-parse, str(legacy
                # JSON)->_fast_loads-equivalent json path. Mirrors build_matrix.
                raw = r["vector"]
                if isinstance(raw, (bytes, bytearray, memoryview)):
                    arr = decode_vector(raw)
                else:
                    v = _fast_loads(raw)
                    arr = np.asarray(v, dtype=np.float32)
                if arr is None or arr.size != embed_dim:
                    continue
                if rd:
                    arr = truncate_vec(arr, rd)
                # R9 守卫:BLOB/legacy-JSON 双分支统一维后才允许累加 ——
                # 混维 += 在 numpy 下是硬错,某分支漏截时在此现形而非静默。
                assert arr.size == rep_dim, \
                    f"seed rep dim {arr.size} != {rep_dim} (branch missed truncation?)"
                seed = r["seed"]
                if seed in rep_sum:
                    rep_sum[seed] += arr
                    rep_cnt[seed] += 1
                else:
                    rep_sum[seed] = arr.copy()
                    rep_cnt[seed] = 1
        reps: Dict[str, "np.ndarray"] = {s: rep_sum[s] / rep_cnt[s] for s in rep_sum}
        return reps, members_count, seed_first_name

    def _write_cluster_map_streamed(self, notebook_id: str, object_type: str,
                                    seed_to_canonical: Dict[str, str],
                                    canonical_names: Dict[str, str],
                                    desc_by_cid: Optional[Dict[str, str]] = None,
                                    desc_sig_by_cid: Optional[Dict[str, str]] = None,
                                    run_id: str = "") -> None:
        """Persist concept_clusters rows for one type: matches write_clusters'
        columns and DELETE scope EXACTLY (clear-by-(notebook_id, object_type),
        then insert one row per member object). Rows whose seed has no
        canonical are skipped. Only reads scratch rows matching run_id so
        concurrent rebuilds don't cross.

        写锁瘦身改造点 2(design doc §5.5):拆成【预备段 + 切换段】,不再是单个
        write() 块里"边持锁边流式构造 N 个成员行"(647-1240ms@300-500k,曾是
        全部持锁时间的 73%)。

        预备段(本函数的前半段):seed_to_canonical/canonical_names/
        desc_by_cid/desc_sig_by_cid 此时早已是完整的 Python dict(由上面
        cluster_seeds()/concept_desc 阶段算出,不再变化)——没有跨连接游标要
        流,只是把这些 dict 逐 seed 打包成行,经 _bulk_write 分批落进
        kg_canonical_scratch,每批独立提交、批间完整释放写锁(同
        _stream_seed_reps Pass A2 的模式)。写入前先清空本 run 的行:
        _write_cluster_map_streamed 每个 object_type 各调一次,不清会让上一个
        type 的行经 swap 的 (notebook_id, run_id, seed) 连接谓词泄漏进这个
        type(两种类型的 seed 字符串恰好相同时)——镜像 _stream_seed_reps 对
        kg_cluster_scratch 的 clear-at-start。

        切换段(本函数的后半段):一个 write() 块,纯 SQL DELETE+INSERT...
        SELECT(swap_cluster_map_from_scratch)连接 kg_cluster_scratch 与
        kg_canonical_scratch,不出现任何 Python 逐行构造或跨连接游标步进。
        cluster_mutation_seq 的 bump 必须落在同一个 write() 块(wdb)里,与
        DELETE+INSERT 同一次提交——见下面调用处的注释。

        两张 scratch 表在 rebuild 末尾的 finally 里统一清理(run_id 隔离并发
        rebuild),镜像 clear_scratch_run 已有的处理方式。"""
        now = self._now()
        desc_by_cid = desc_by_cid or {}
        desc_sig_by_cid = desc_sig_by_cid or {}

        # --- Preparation segment ------------------------------------------
        # Clear THIS run's canonical-scratch rows before repopulating (see
        # docstring above) — its own short write(), not folded into the
        # batch loop below.
        with self._write() as db:
            self.unified_kg.clear_canonical_scratch_run(db, notebook_id, run_id)

        def _batches():
            local: List[tuple] = []
            for seed, cid in seed_to_canonical.items():
                local.append(_canonical_scratch_row(
                    notebook_id, run_id, seed, cid,
                    canonical_names, desc_by_cid, desc_sig_by_cid,
                ))
                if len(local) >= 1000:
                    yield list(local)
                    local.clear()
            if local:
                yield list(local)

        self._bulk_write(
            _batches(),
            lambda wdb, rows: self.unified_kg.insert_canonical_scratch_rows(wdb, rows),
        )

        # --- Swap segment ---------------------------------------------------
        # ONE short write transaction, pure SQL: DELETE the type's old rows,
        # INSERT...SELECT the new ones by joining the two scratch tables (see
        # swap_cluster_map_from_scratch's docstring for the exact invariants
        # it preserves — inner join / no object_type column needed / ORDER BY
        # rowid / empty-clears / SQL-minted id).
        with self._write() as wdb:
            self.unified_kg.swap_cluster_map_from_scratch(
                wdb, notebook_id, object_type, run_id, now
            )
            # P0-A: same commit as the DELETE+INSERT above (wdb, not rdb) —
            # every rebuild pass through this streamed writer rewrites
            # concept_clusters for `object_type`, so it must bump every time
            # (unconditional, unlike append_clusters' added>0 guard: a
            # rebuild that clusters down to zero rows for this type is still
            # a real content change from whatever was there before).
            self._bump_cluster_mutation_seq(wdb, notebook_id)

    @notebook_model_artifact_scope
    def rebuild_unified_kg(self, notebook_id: str,
                           progress: Optional[Callable[[str, int, int], None]] = None,
                           force: bool = False, fresh: bool = False) -> int:
        """Cluster the notebook's Concepts; persist concept_clusters + refresh
        pending candidates (preserving confirmed/rejected). Returns #clusters.

        Skip-when-unchanged gate: a content-hash of the clustering inputs
        (_cluster_input_version) is stored at each rebuild. When force=False and
        that version still matches AND clusters already exist, the whole recompute
        is skipped and the cached cluster count returned — nothing is deleted or
        rewritten. The 刷新图谱 button goes through this force=False path (see
        api/routes.py); the version now folds in kg_merge.CLUSTER_ALGO_VERSION, so a
        code-only change to seed/clustering SEMANTICS bumps the version and the next
        automatic rebuild truly recomputes rather than being silently skipped.
        force=True is used only by the explicit recluster CLI paths
        (scripts/recluster_kg.py, batch_ingest rebuild_only): it bypasses the gate
        and additionally picks up clustering-SETTINGS changes (thresholds,
        rep_ann_max) the data-version can't see.

        Memory-bounded (streamed): object→seed mappings live in the
        kg_cluster_scratch table; only seed-level aggregates (reps, counts,
        names) are held in RAM, so peak memory scales with #unique name-seeds,
        not #objects. Clustering semantics are identical to the legacy
        all-objects-in-memory path (guarded by tests/test_unified_kg_repository,
        test_cross_doc_merge, test_kg_merge, test_rebuild_streaming)."""
        self.get_notebook(notebook_id)
        # fresh 隐含强制重建:fresh 要清 checkpoint 并重裁,只有真正走到 rebuild 分支才有意义。
        # 两个 CLI caller 已保证 force=(rebuild_only or fresh)/force=fresh;这里在函数边界再兜一层,
        # 防将来新增 caller 传 force=False+fresh=True 时被 skip-gate 静默跳过、clear 落空(defense-in-depth)。
        force = force or fresh
        # Content-version of the clustering inputs, captured ONCE at entry. Reused
        # both for the skip gate below and for the END-write (so the stored version
        # reflects the inputs this rebuild actually consumed).
        _ver = self._cluster_input_version(notebook_id)
        if not force:
            with self._connect() as db:
                row = self.unified_kg.state_row(db, notebook_id)
                cc = self.unified_kg.concept_clusters_count(db, notebook_id)
            if row and row["cluster_input_version"] and row["cluster_input_version"] == _ver and cc > 0:
                cached = int(row["cluster_count"] or 0)
                self.event_log.logger.info(
                    "kg-rebuild[%s] skipped — inputs unchanged since last rebuild (%s clusters)",
                    notebook_id, cached)
                if progress is not None:
                    try:
                        progress(f"跳过:自上次 rebuild 起输入未变化({cached} clusters)", 0, 0)
                    except Exception:
                        pass
                # canonical 关系层(派生)同款增量:自带 seq 闸,KG 未变即秒级 no-op。
                try:
                    self.rebuild_canonical_relations(notebook_id)
                except Exception as exc:  # noqa: BLE001
                    self.event_log.emit({"kind": "canonical_relations_rebuild_failed",
                                         "notebook_id": notebook_id, "error": str(exc)[:200]})
                # 共提桥接层(派生)同款增量:自带 mention_seq 闸,KG 未变即秒级 no-op。
                try:
                    self.rebuild_mention_bridge(notebook_id)
                except Exception as exc:  # noqa: BLE001
                    self.event_log.emit({"kind": "mention_bridge_rebuild_failed",
                                         "notebook_id": notebook_id, "error": str(exc)[:200]})
                # 增量:聚类被跳过(输入未变),但社区可能未建/过期 → rebuild_communities
                # 自带版本闸,只在需要时建(纯图、无 LLM、秒级)。这让「只需重建社区」的
                # 场景在原有刷新流程里零额外成本达成;fail-open,不拖垮跳过路径。
                try:
                    self.rebuild_communities(notebook_id, level=0)
                except Exception as exc:  # noqa: BLE001
                    self.event_log.emit({"kind": "communities_rebuild_failed",
                                         "notebook_id": notebook_id, "error": str(exc)[:200]})
                return cached
        # 断点续跑:进到这里=已确定真正要重算(force=True,或 force=False 因版本
        # 失配落空)——checkpoint GC/clear 移到这里(而非版本闸之前)才对:早前放在
        # 闸前时,force=False 且判定跳过的路径也会顺带做一次 GC 写,破坏了「跳过=零
        # 写入」的不变量。checkpoint 版本刻意排除 emb_c(节点向量增量 backfill 期间
        # emb_c 单调爬升但 seq/obj_c/dec_c 不变):这样节点向量阶段中途崩溃后,下次
        # --rebuild-only 续跑不会因 emb_c 变了就把 merge_review/concept_desc
        # checkpoint 当作「输入变了」GC 掉、被迫重新走一遍可能数小时的 LLM 裁决;
        # item_key 已经提供细粒度正确性。fresh 仍清全部 checkpoint(强制两个 LLM
        # 阶段重跑)。fail-open。
        _ck_ver = self._cluster_input_version(notebook_id, exclude_emb_count=True)
        try:
            if fresh:
                self.unified_kg.checkpoint_clear(notebook_id)
            else:
                self.unified_kg.checkpoint_gc(notebook_id, _ck_ver)
        except Exception:  # noqa: BLE001 — checkpoint 维护失败不能打断 rebuild
            self.event_log.logger.warning("rebuild checkpoint GC/clear 失败 for %s", notebook_id, exc_info=True)
        from uuid import uuid4 as _uuid4
        from app.services.kg_merge import (cluster_seeds, filter_pending_seeds_by_decisions,
                                           _norm, _discriminative_conflict,
                                           seed_claim, seed_formula, seed_procedure)
        # Each rebuild gets a unique run_id so concurrent rebuilds of the SAME
        # notebook never wipe or read each other's scratch rows.
        run_id = _uuid4().hex

        # The whole rebuild body below runs under try/finally so a crash,
        # exception, or cancellation ANYWHERE in it (not just during Pass A2
        # of _stream_seed_reps) still clears this run's kg_cluster_scratch
        # rows — see the finally at the bottom of this function.
        try:
            # Sub-stage instrumentation: log each stage's name+counts+elapsed on two
            # channels (event_log INFO + progress banner). Pure logging — no effect on
            # clustering results or the progress=None data path.
            import time as _time
            _t_total = _time.perf_counter()
            def _stage(msg: str) -> None:
                # 批 3·W1 PR-3 §4.2 选项 A 的检查点(unifiedkg-)——十个阶段
                # 边界共用这一处改动(design doc 明文点名这是全篇成本最低的
                # 插入点)。理由与 relink 检查点相同,见那条注释。
                if self._notebook_deleting(notebook_id):
                    raise NotebookDeletingAbortsMaintenanceError(notebook_id)
                self.event_log.logger.info("kg-rebuild[%s] %s", notebook_id, msg)
                if progress is not None:
                    try:
                        progress(msg, 0, 0)
                    except Exception:
                        pass

            # --- Concepts (vector + name-seed clustering) ----------------------
            # _stream_seed_reps re-populates kg_cluster_scratch for object_type=concept
            # (object_id -> seed) and returns seed-level aggregates only.
            _t = _time.perf_counter()
            reps, members_count, seed_first_name = self._stream_seed_reps(
                notebook_id, "concept", lambda o: _norm(o["name"]), run_id=run_id)
            # IMPORTANT: include seeds with NO vector (name-only) — use members_count
            # keys, not reps keys, to match the legacy all-objects path.
            seeds = sorted(members_count)
            _stage(f"concept: streamed {sum(members_count.values())} objs → "
                   f"{len(seeds)} seeds, {len(reps)} vecs "
                   f"({_time.perf_counter() - _t:.1f}s)")
            decided = self.decided_seed_pairs(notebook_id)
            confirmed = {p for p, s in decided.items() if s == "confirmed"}
            rejected = {p for p, s in decided.items() if s in ("rejected", "deferred")}
            _t_cluster = _time.perf_counter()
            sd = cluster_seeds(seeds, reps, members_count, seed_first_name, confirmed, rejected,
                               conflict_fn=_discriminative_conflict, id_prefix="K-",
                               rep_ann_max=self.settings.kg_cluster_rep_ann_max,
                               ann_threads=self.settings.kg_cluster_ann_threads)
            # LLM 兜底: ≥hi 的 auto_candidates 经复核确认后并入 confirmed, 重聚一次
            from app.services.concept_merge_review import review_merge_candidates
            autoc = sd.get("auto_candidates", [])
            _t_mr = _time.perf_counter()
            if autoc and self.model_clients.configured("kg_merge_review"):
                # Optional LLM adjudication is an enhancement — it must NEVER be able to
                # crash the rebuild. review_merge_candidates is already fail-open (chunked +
                # defensive parse); this outer guard covers any other unexpected error in the
                # block so the rebuild always proceeds to write the cluster map.
                try:
                    cand_dicts = [{"id": f"ac{i}", "canonical_a": a, "canonical_b": b, "score": s}
                                  for i, (a, b, s) in enumerate(autoc)]
                    # ac{i} → canonical id 对的稳定键(续跑复用的锚)。
                    id_to_key = {f"ac{i}": _pair_key(a, b) for i, (a, b, s) in enumerate(autoc)}
                    # 已决(同 input_version)命中即跳过 LLM;只把未决候选发出去。
                    cached = self.unified_kg.checkpoint_load(
                        notebook_id, _ck_ver, "merge_review")
                    todo = [c for c in cand_dicts if id_to_key[c["id"]] not in cached]

                    def _persist(chunk_decisions):
                        rows = [(id_to_key[d["candidate_id"]],
                                 {"decision": d["decision"], "confidence": d["confidence"],
                                  "canonical_name": d.get("canonical_name", "")})
                                for d in chunk_decisions if d.get("candidate_id") in id_to_key]
                        if rows:
                            self.unified_kg.checkpoint_put(
                                notebook_id, _ck_ver, "merge_review", rows, self._now())

                    new = review_merge_candidates(
                        self.model_clients.chat("kg_merge_review"), todo,
                        batch_size=self.settings.kg_merge_review_batch_size,
                        max_workers=self.model_clients.parallelism("kg_merge_review"),
                        on_chunk=_persist,
                    )
                    # 合并 缓存 ∪ 新决策,按 pair_key 索引。
                    decided = dict(cached)
                    for d in new:
                        k = id_to_key.get(d.get("candidate_id"))
                        if k:
                            decided[k] = {"decision": d["decision"], "confidence": d["confidence"]}
                    extra = set()
                    for i, (a, b, s) in enumerate(autoc):
                        dec = decided.get(_pair_key(a, b))
                        if dec and dec.get("decision") == "merge" and \
                                float(dec.get("confidence", 0)) >= self.settings.kg_merge_confirm_threshold:
                            extra.add(frozenset((a[2:] if a.startswith("K-") else a,
                                                b[2:] if b.startswith("K-") else b)))
                except Exception:
                    self.event_log.logger.exception(
                        "unified-KG merge-review adjudication failed for %s; proceeding without it",
                        notebook_id,
                    )
                    extra = set()
                if extra:
                    confirmed = set(confirmed) | extra
                    sd = cluster_seeds(seeds, reps, members_count, seed_first_name, confirmed, rejected,
                                       conflict_fn=_discriminative_conflict, id_prefix="K-",
                                       rep_ann_max=self.settings.kg_cluster_rep_ann_max,
                                       ann_threads=self.settings.kg_cluster_ann_threads)
                _stage(f"concept: merge-review {len(autoc)} candidates → "
                       f"{len(extra)} merged ({_time.perf_counter() - _t_mr:.1f}s)")
            _stage(f"concept: clustered {len(seeds)} seeds → "
                   f"{len(set(sd['seed_to_canonical'].values()))} canonicals, "
                   f"{len(sd.get('auto_candidates', []))} auto-cand "
                   f"({_time.perf_counter() - _t_cluster:.1f}s)")
            # reps (concept mean-vectors, ~18GB at 4.4M seeds) is dead after
            # clustering — its only consumers are the cluster_seeds calls above.
            # Drop it BEFORE the derivation tail (canonical relations / mention
            # bridge / build_viz / communities) so its ~18GB doesn't ride
            # resident and stack on top of those stages (OOM audit P0-2). dict of
            # numpy arrays → freed immediately by refcount, no gc needed.
            del reps
            seed_to_canonical = sd["seed_to_canonical"]
            desc_by_cid: Dict[str, str] = {}
            desc_sig_by_cid: Dict[str, str] = {}
            _t_desc = _time.perf_counter()
            _desc_ran = (
                self.settings.kg_concept_desc_enabled
                and self.model_clients.configured("kg_concept_description")
            )
            if _desc_ran:
                from app.services.prompts import concept_description_prompt, CONCEPT_DESC_SCHEMA_HINT
                # Previous descriptions + their input sigs, keyed by canonical id. DISTINCT
                # so this is bounded by #canonicals (not #members). Reuse fires only on an
                # exact sig match with a non-empty stored description → fail-safe: any
                # miss/mismatch just regenerates (worst case = old behavior).
                old_desc: Dict[str, tuple] = {}
                with self._connect() as db:
                    for r in self.unified_kg.cluster_description_rows(db, notebook_id):
                        old_desc[r["canonical_id"]] = (r["canonical_description"] or "", r["canonical_desc_sig"] or "")
                # 同 input_version 的 checkpoint(写簇前被杀留下的已完成描述)作第一优先复用源。
                try:
                    desc_ckpt = self.unified_kg.checkpoint_load(
                        notebook_id, _ck_ver, "concept_desc")
                except Exception:  # noqa: BLE001 — checkpoint 读失败退化为全量重跑,绝不打断 rebuild
                    self.event_log.logger.warning(
                        "concept_desc checkpoint load 失败 for %s;本轮全量重跑描述", notebook_id, exc_info=True)
                    desc_ckpt = {}
                # Total members per canonical = Σ members_count over its seeds. Keep
                # only multi-member (cross-doc merged) canonicals — same cost bound as
                # the legacy `len(mids) < 2` gate, but computed from seed aggregates so
                # no 5M-row member list is materialized.
                total_by_cid: Dict[str, int] = {}
                seeds_by_cid: Dict[str, List[str]] = {}
                for s, cid in seed_to_canonical.items():
                    total_by_cid[cid] = total_by_cid.get(cid, 0) + members_count.get(s, 0)
                    seeds_by_cid.setdefault(cid, []).append(s)
                # PHASE 1 (serial, cheap DB): fetch quotes per multi-member canonical,
                # compute its input sig, and either reuse the cached description or
                # queue an LLM job. DB access stays in the main thread.
                work: List[tuple] = []
                # 只保留多成员(跨文档合并)的 canonical——与逐条时代的 `total < 2`
                # 门同一个成本界,顺序沿 total_by_cid 的迭代序原样保留。
                eligible = [
                    cid for cid, total in total_by_cid.items() if total >= 2
                ]
                # ⚠ 证据取数**按批**(审计批4):逐条时代是「每个多成员 canonical 一次
                # cluster_evidence_rows 往返」,10⁵–10⁶ 次串行 DB 往返全部堵在 LLM 阶段
                # 之前。现在一次 IN 查回一整批 canonical 的 seed 证据行,按行上的 seed
                # 分组回内存。
                #
                # 产物逐位不变的论证(两条):
                #  ① 每个 canonical 的 quotes 经 `sorted(set(...))[:8]` 归一,与行的
                #     返回顺序无关——批量查询把多个 canonical 的行混在一起返回也无所谓,
                #     分组后各自排序去重的结果与逐条查询逐字相同。seed→canonical 是
                #     函数关系(seeds_by_cid 由 seed_to_canonical 反转而来,一个 seed
                #     只属于一个 canonical),故分组无歧义。
                #  ② `work`(LLM 阶段的输入)的顺序沿 eligible 逐位保持:批按序切、
                #     批内按序遍历,checkpoint / 跨 rebuild 复用的判定与写入顺序也因此
                #     与逐条时代一字不差。LLM 调用本身一次没动。
                #
                # 内存仍有界:一次只驻留**一批**的证据行,不是全库。三条上限里
                # ≤300 canonical / ≤900 seed 管的是**参数个数**(往返数与绑定变量上限),
                # 真正管住**载荷**的是第三条 ≤``_DESC_EVIDENCE_ROW_BATCH`` 行——
                # cluster_evidence_rows 每个成员返一行整份 evidence JSON,300 个 hub
                # canonical 各带上千成员照样能拉回几十万行。见 _desc_evidence_batches。
                for cid_batch in _desc_evidence_batches(
                    eligible, seeds_by_cid, total_by_cid
                ):
                    seed_owner: Dict[str, str] = {}
                    batch_seeds: List[str] = []
                    for cid in cid_batch:
                        for s in seeds_by_cid[cid]:
                            seed_owner[s] = cid
                            batch_seeds.append(s)
                    quotes_by_cid: Dict[str, List[str]] = {c: [] for c in cid_batch}
                    with self._connect() as db:
                        erows = self.unified_kg.cluster_evidence_rows(
                            db, notebook_id, run_id, batch_seeds
                        )
                    for er in erows:
                        owner = seed_owner.get(er["seed"])
                        if owner is None:
                            continue   # 本批没问过的 seed(理论上不会出现),不静默串组
                        bucket = quotes_by_cid[owner]
                        for ev in json.loads(er["evidence"] or "[]"):
                            q = (ev.get("quoted_span") or "").strip()
                            if q:
                                bucket.append(q)
                    del erows
                    for cid in cid_batch:
                        # DETERMINISTIC dedup+order so the sig (and prompt) is stable across
                        # rebuilds — scratch row order is otherwise unsorted.
                        quotes = sorted(set(quotes_by_cid[cid]))[:8]
                        if not quotes:
                            continue
                        name = sd["canonical_names"].get(cid, "")
                        sig = _concept_desc_sig(name, quotes)
                        ck = desc_ckpt.get(cid)
                        if ck and ck.get("sig") == sig and ck.get("description"):
                            desc_by_cid[cid] = ck["description"]     # checkpoint 命中:复用,跳过 LLM
                            desc_sig_by_cid[cid] = sig
                            continue
                        prev = old_desc.get(cid)
                        if prev and prev[0] and prev[1] == sig:
                            desc_by_cid[cid] = prev[0]               # 跨 rebuild 缓存命中:复用
                            desc_sig_by_cid[cid] = sig
                            continue
                        work.append((cid, name, quotes, sig))
                    del quotes_by_cid
                # PHASE 2 (parallel LLM): resolve the workload-bound scheduled
                # client once before the raw worker pool. The pool width mirrors
                # the physical service cap and the scheduler remains authoritative.
                import concurrent.futures as _cf
                desc_client = self.model_clients.chat("kg_concept_description")
                def _gen(item):
                    cid, name, quotes, sig = item
                    block = "\n".join(f"- {q}" for q in quotes)
                    try:
                        raw = desc_client.chat_json(
                            [{"role": "user", "content": concept_description_prompt(name, block)}],
                            CONCEPT_DESC_SCHEMA_HINT)
                        desc = (json.loads(raw).get("description") or "").strip()
                    except Exception:
                        desc = ""
                    return cid, desc, sig
                if work:
                    workers = max(
                        1,
                        min(
                            self.model_clients.parallelism("kg_concept_description"),
                            len(work),
                        ),
                    )
                    done_n = 0
                    with _cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kg-desc") as pool:
                        _ck_buf: List[Tuple[str, dict]] = []
                        for fut in _cf.as_completed([pool.submit(contextvars.copy_context().run, _gen, it) for it in work]):
                            cid, desc, sig = fut.result()
                            done_n += 1
                            if desc:
                                desc_by_cid[cid] = desc
                                desc_sig_by_cid[cid] = sig
                                _ck_buf.append((cid, {"description": desc, "sig": sig}))
                                if len(_ck_buf) >= _DESC_CKPT_FLUSH:
                                    try:
                                        self.unified_kg.checkpoint_put(
                                            notebook_id, _ck_ver, "concept_desc", _ck_buf, self._now())
                                    except Exception:  # noqa: BLE001 — checkpoint 写失败不打断 rebuild
                                        self.event_log.logger.warning(
                                            "concept_desc checkpoint put 失败 for %s", notebook_id, exc_info=True)
                                    _ck_buf = []
                            if progress is not None:
                                try:
                                    progress("concept_desc", done_n, len(work))
                                except Exception:
                                    pass
                        if _ck_buf:
                            try:
                                self.unified_kg.checkpoint_put(
                                    notebook_id, _ck_ver, "concept_desc", _ck_buf, self._now())
                            except Exception:  # noqa: BLE001 — checkpoint 写失败不打断 rebuild
                                self.event_log.logger.warning(
                                    "concept_desc checkpoint put 失败 for %s", notebook_id, exc_info=True)
            if _desc_ran:
                _stage(f"concept: descriptions {len(desc_by_cid)} "
                       f"({_time.perf_counter() - _t_desc:.1f}s)")
            else:
                _stage("concept: descriptions skipped")
            _t = _time.perf_counter()
            self._write_cluster_map_streamed(notebook_id, "concept", seed_to_canonical,
                                             sd["canonical_names"], desc_by_cid,
                                             desc_sig_by_cid, run_id=run_id)
            _stage(f"concept: wrote clusters ({_time.perf_counter() - _t:.1f}s)")
            # Cross-doc exact-normalized merge for non-concept types (v1: seed-based;
            # vector near-dup remains concept-only). Each type isolated by id prefix.
            # These types carry no vectors → reps_t is empty → cluster_seeds does only
            # exact-seed grouping, matching the legacy cluster_objects(tobjs, {}, ...).
            # compute_reps=False skips Pass B entirely (no embeddings join) since
            # cluster_seeds receives {} anyway — avoids wasted ANN at scale.
            _TYPE_MERGE = {"claim": (seed_claim, "KL-"),
                           "formula": (seed_formula, "KF-"),
                           "procedure": (seed_procedure, "KP-")}
            for t, (sfn, prefix) in _TYPE_MERGE.items():
                _t = _time.perf_counter()
                reps_t, mc_t, sfn_t = self._stream_seed_reps(notebook_id, t, sfn,
                                                              run_id=run_id,
                                                              compute_reps=False)
                # Empty type → scratch empty → _write_cluster_map_streamed clears rows
                # (same as legacy write_clusters([], object_type=t)).
                sd_t = cluster_seeds(sorted(mc_t), reps_t, mc_t, sfn_t, set(), set(),
                                     conflict_fn=None, id_prefix=prefix,
                                     rep_ann_max=self.settings.kg_cluster_rep_ann_max,
                                     ann_threads=self.settings.kg_cluster_ann_threads)
                self._write_cluster_map_streamed(notebook_id, t, sd_t["seed_to_canonical"],
                                                 sd_t["canonical_names"], run_id=run_id)
                _stage(f"{t}: streamed+clustered+wrote ({_time.perf_counter() - _t:.1f}s)")
            # Refresh pending candidates in ONE transaction (per-candidate inserts
            # were the rebuild hotspot at scale). confirmed/rejected rows untouched.
            now = self._now()
            _t = _time.perf_counter()
            with self._write() as db:
                self.governance_store.delete_pending_merges(db, notebook_id)
                # Decisions may land after the earlier cluster_seeds snapshot.
                # DELETE first so PostgreSQL serializes against updates of the
                # old pending rows; then read decisions and filter in this same
                # transaction before publishing replacement rows.
                live_decided = self.governance_store.decided_seed_pairs_from(
                    db, notebook_id
                )
                live_confirmed = {
                    pair for pair, status in live_decided.items()
                    if status == "confirmed"
                }
                live_rejected = {
                    pair for pair, status in live_decided.items()
                    if status in ("rejected", "deferred")
                }
                pending_seeds = filter_pending_seeds_by_decisions(
                    sd["pending_seeds"], seeds, live_confirmed, live_rejected
                )
                self.governance_store.insert_pending_merge_rows(
                    db,
                    [(self._new_id("mc"), notebook_id, ca, cb, sa, sb, score, now, now)
                     for sa, sb, ca, cb, score in pending_seeds])
            _stage(f"pending refresh ({_time.perf_counter() - _t:.1f}s)")
            self._invalidate_unified_cache(notebook_id)
            # #distinct concept canonicals (== set of cluster_map values in the legacy
            # path: every canonical has ≥1 seed and every concept seed has a canonical).
            cluster_count = len(set(seed_to_canonical.values()))
            with self._write() as db:
                # CRITICAL: the store's finish_rebuild_state UPSERT stores
                # cluster_input_version=_ver (captured at ENTRY, reflecting the seq
                # this rebuild consumed) and clears dirty=0, but MUST NOT touch
                # kg_mutation_seq — the column is omitted from both the column list
                # and the SET so an existing row's counter is PRESERVED. Bumping it
                # here would advance the version past what was just stored (gate
                # never skips); resetting it would lose mutations that arrived
                # mid-rebuild.
                self.unified_kg.finish_rebuild_state(
                    db, notebook_id, _ver, cluster_count, now
                )
        finally:
            # Final cleanup: drop only THIS run's scratch rows (run_id-scoped
            # so a concurrent rebuild with a different run_id is unaffected).
            # MUST be unconditional (finally, not straight-line): Pass A2 above
            # now commits each insert_scratch_rows batch independently (see the
            # comment at its call sites), so a crash/exception/cancellation
            # anywhere in the try above — not just during Pass A2 — would
            # otherwise strand this run_id's rows forever: kg_cluster_scratch has
            # no timestamp column, and every DELETE against it in the codebase is
            # run_id-scoped, so nothing could ever reclaim them. Before batching,
            # an interrupted Pass A2 rolled its scratch rows back atomically (one
            # txn); batching traded that for per-batch durability, so cleanup must
            # now be explicit here instead. Swallow+log so a cleanup failure never
            # masks whatever exception is already propagating out of the try.
            #
            # kg_canonical_scratch (write-lock slimming improvement point 2's
            # preparation-segment table) gets the SAME treatment, in the SAME
            # write() block: _write_cluster_map_streamed only clears its OWN
            # run's rows at the START of each per-type call (to keep two
            # back-to-back types in the same run from cross-contaminating —
            # see its docstring), so nothing clears the LAST type's rows once
            # the whole rebuild finishes, and a crash inside
            # _write_cluster_map_streamed (prep or swap) would otherwise
            # strand them forever for the same reason kg_cluster_scratch rows
            # would.
            try:
                with self._write() as db:
                    self.unified_kg.clear_scratch_run(db, notebook_id, run_id)
                    self.unified_kg.clear_canonical_scratch_run(db, notebook_id, run_id)
            except Exception:  # noqa: BLE001
                self.event_log.logger.warning(
                    "rebuild scratch cleanup 失败 for %s run_id=%s",
                    notebook_id, run_id, exc_info=True)
        _stage(f"DONE {cluster_count} canonicals, total {_time.perf_counter() - _t_total:.1f}s")
        # canonical 关系层(派生):聚类刚重算 → force=True(闸可能误跳)。fail-open。
        try:
            self.rebuild_canonical_relations(notebook_id, force=True)
        except Exception as exc:  # noqa: BLE001
            self.event_log.emit({"kind": "canonical_relations_rebuild_failed",
                                 "notebook_id": notebook_id, "error": str(exc)[:200]})
        # 共提桥接层(派生):聚类刚重算 → force=True(闸可能误跳)。fail-open。
        try:
            self.rebuild_mention_bridge(notebook_id, force=True)
        except Exception as exc:  # noqa: BLE001
            self.event_log.emit({"kind": "mention_bridge_rebuild_failed",
                                 "notebook_id": notebook_id, "error": str(exc)[:200]})
        # Proactively refresh the viz-only index so the next KG-view open doesn't
        # pay a lazy build. This bumped cluster_mutation_seq (in the cluster write
        # above), and build_viz now stamps its artifact with that cseq while
        # viz_index()/viz_probe() compare it — so a cluster-only rebuild reliably
        # marks the persisted viz STALE and the lazy path refreshes it on the next
        # open (serving the old folding meanwhile). That is why this proactive
        # build is no longer needed for large notebooks and MUST NOT run for them:
        #
        # build_viz's _derive_object_graph_lite materialises EVERY object + all
        # relations + the full cluster_map into one ~12-20GB graph dict. Doing that
        # HERE — synchronously stacked on the memory the rebuild just used, or via a
        # daemon that starts before this frame frees seed_to_canonical/sd/… — is a
        # reliable OOM at multi-million-object scale that kills the whole rebuild
        # (audit P0-2 / codex PR#356 r1 P1b). So large notebooks skip the tail build
        # entirely; their viz is refreshed lazily off the rebuild thread (via the
        # cseq-driven staleness above) and, off-peak, by the scale index build that
        # maybe_auto_index queues below. Small notebooks build synchronously — cheap.
        #
        # The WHOLE block (size probe included) is fail-open (codex PR#356 r1 P2):
        # an auxiliary viz decision must never turn an already-committed KG rebuild
        # into a reported failure.
        try:
            with self._connect() as db:
                viz_obj_count = self.knowledge.active_object_count(db, notebook_id)
            if int(viz_obj_count) <= self.settings.viz_sync_build_max_objects:
                self.scale_artifacts.build_viz(notebook_id)          # small: sync, cheap
            # large: skip — the cseq-marked stale viz is refreshed lazily/off-peak,
            # never materialised on or alongside the rebuild frame.
        except Exception:
            self.event_log.logger.warning(
                "viz refresh after rebuild failed for %s", notebook_id, exc_info=True)
        # rebuild 后检索索引必然 stale(clusters/objects 已变)—— 大库自动重建/入队。
        # maybe_auto_index 自身 fail-open,这里再包一层只是双保险。
        try:
            self.scale_artifacts.maybe_auto_index(notebook_id)
        except Exception:
            self.event_log.logger.exception("maybe_auto_index failed after rebuild for %s", notebook_id)
        # 社区层:聚类刚重算 → force=True 强制重建社区(clustering 未必 bump
        # kg_mutation_seq,版本闸可能误跳)。纯图、无 LLM、fail-open——绝不拖垮 KG 重建。
        try:
            self.rebuild_communities(notebook_id, level=0, force=True)
        except Exception as exc:  # noqa: BLE001
            self.event_log.emit({"kind": "communities_rebuild_failed",
                                 "notebook_id": notebook_id, "error": str(exc)[:200]})
        return cluster_count

    # ------------------------------------------------------------------
    # Derived layers: canonical relations / mention bridge / communities
    # ------------------------------------------------------------------

    def rebuild_canonical_relations(self, notebook_id: str, force: bool = False) -> int:
        """把 knowledge_relations 端点经 concept_clusters 折叠到 canonical 空间,
        按 (canonical_src, edge_type, canonical_tgt) 聚合 support_count(原始行数)/
        source_count(非NULL source_id 去重,下限1)/sample_relation_ids(cap 5),全量
        重写 canonical_relations。方向保留;rejected 边排除;折叠后自环丢弃。

        seq 闸(同 rebuild_communities):canonical_rel_seq==kg_mutation_seq 且表非空
        → 跳过(force 绕过);重写后把入口捕获的 seq 写回。返回写入行数(跳过时返回
        现有行数)。派生数据,fail-open 由调用方负责。"""
        from app.services.kg.edge_schema import is_queryable_edge_pair

        self.get_notebook(notebook_id)
        with self._connect() as db:
            st = self.unified_kg.state_row(db, notebook_id)
            cnt = self.unified_kg.canonical_relations_count(db, notebook_id)
        seq = int(st["kg_mutation_seq"]) if st else 0
        if (not force and st is not None and st["canonical_rel_seq"] == seq and cnt > 0):
            return int(cnt)
        agg: Dict[tuple, dict] = {}
        with self._connect() as db:
            cur = self.unified_kg.canonical_relation_seed_rows(db, notebook_id)
            for r in cur:
                if not is_queryable_edge_pair(r["et"], r["st"], r["tt"]):
                    continue
                s, t = r["s"], r["t"]
                if not s or not t or s == t:
                    continue
                key = (s, r["et"], t)
                ent = agg.get(key)
                if ent is None:
                    ent = agg[key] = {"n": 0, "docs": set(), "samples": []}
                ent["n"] += 1
                if r["src_doc"]:
                    ent["docs"].add(r["src_doc"])
                if len(ent["samples"]) < 5:
                    ent["samples"].append(r["rid"])
        now = self._now()
        rows = [(notebook_id, s, et, t, ent["n"], max(1, len(ent["docs"])),
                 json.dumps(ent["samples"]), now)
                for (s, et, t), ent in agg.items()]
        with self._write() as db:
            self.unified_kg.replace_canonical_relations(
                db, notebook_id, rows, seq
            )
        return len(rows)

    def rebuild_mention_bridge(self, notebook_id: str, force: bool = False) -> int:
        """从 claim 文本确定性提取「claim→跨源概念」mention 边与概念共提对(零 LLM)。

        流程:跨 >=2 源的 concept 簇 → 别名表(mention_scan.build_alias_table)→
        连接私有 TEMP trigram FTS(claim 折叠名;纯内存零 WAL,不占全局写锁)→
        每别名 phrase MATCH 召回
        候选 → 统一 alnum-lookaround 边界(boundary_hit)后校验 → DF 双门(命中
        claim 数超 max(floor, cap×claims)的泛词整体丢弃 + 事件计数)→ 聚合
        claim→{canonical} 全量重写 mention_edges + 两两组合(a<b)concept_comentions
        (单 claim 命中的 canonical 数超过 `_MENTION_MAX_CANONS_PER_CLAIM` 时**整条
        跳过组合**并计入 `mention_comention_claim_skipped` 事件;edges 不受影响)。

        seq 闸同 rebuild_canonical_relations(mention_seq==kg_mutation_seq 且表非空
        → 跳过,force 绕过;重写后写回入口捕获的 seq)。flag 关时清表返回 0。
        文本/别名一律 NFKC 折叠 + lower(与 build_alias_table 的别名折叠对齐,全/半角
        互通)。派生数据,fail-open 由调用方负责。返回写入的 mention_edges 行数。"""
        self.get_notebook(notebook_id)
        if not self.settings.mention_bridge_enabled:
            with self._write() as db:
                self.unified_kg.clear_mention_bridge(db, notebook_id)
            return 0
        # seq 闸(照抄 rebuild_canonical_relations,列名换 mention_seq)。
        with self._connect() as db:
            st = self.unified_kg.state_row(db, notebook_id)
            cnt = self.unified_kg.mention_edges_count(db, notebook_id)
        seq = int(st["kg_mutation_seq"]) if st else 0
        if (not force and st is not None and st["mention_seq"] == seq and cnt > 0):
            return int(cnt)

        import unicodedata as _ud
        from itertools import combinations as _combinations
        from app.services.kg.mention_scan import build_alias_table, boundary_hit

        def _fold(t: str) -> str:
            return _ud.normalize("NFKC", t or "").lower()

        # 1) 跨源 concept 簇(成员横跨 >=2 个非空 source_id)。claim 簇(KL- 前缀)
        #    是被扫描的文本、不作别名目标,故按 object_type='concept' 过滤。
        clusters: Dict[str, dict] = {}
        with self._connect() as db:
            cur, claim_rows = self.unified_kg.mention_seed_rows(db, notebook_id)
            for r in cur:
                ent = clusters.setdefault(r["cid"], {"name": r["cname"], "srcs": set()})
                if r["src"]:
                    ent["srcs"].add(r["src"])
        cross = [(cid, ent["name"]) for cid, ent in clusters.items() if len(ent["srcs"]) >= 2]
        claims: List[Tuple[str, str]] = []
        for r in claim_rows:
            folded = _fold(r["nm"])
            if len(folded) < 10:
                continue
            claims.append((r["id"], folded))
        alias_table = build_alias_table(cross)   # {canonical_id: {alias,...}}(已 NFKC+lower)
        # alias -> {canonical_id,...}(同一 alias 理论上可属多个 canonical)。
        alias_to_canons: Dict[str, Set[str]] = {}
        for cid, aliases in alias_table.items():
            for a in aliases:
                alias_to_canons.setdefault(a, set()).add(cid)

        claim_hits: Dict[str, Dict[str, str]] = {}   # claim_id -> {canonical_id: matched_alias}
        dropped = 0
        n_claims = len(claims)
        if cross and claims and alias_to_canons:
            df_gate = max(self.settings.mention_alias_df_floor,
                          self.settings.mention_alias_df_cap * n_claims)
            # 3) 连接私有 TEMP trigram FTS(建+填+查同一 _connect 连接):temp schema
            #    按连接隔离——并发 rebuild 同名不相撞,无需串行化;temp_store=MEMORY
            #    (见 _connect)→ 全程纯内存,零 WAL 写入、不占 _write_lock(效率约束:
            #    部署规模 ~40万 claims 的插入+扫描绝不能挡住 ingest/其它 rebuild)。
            #    连接关闭即整表蒸发(无需 DELETE/DROP;finally close 兼释放内存)。
            #    trigram=子串语义,故每候选仍须过 boundary_hit 后校验。
            with self.unified_kg.mention_alias_candidate_batches(
                claims, sorted(alias_to_canons)
            ) as batches:
                # 4) 每别名 phrase 候选 → boundary_hit 校验 → DF 双门。当前
                # alias 的 hits 最多保留 df_gate+1；一旦确认泛词就立即前进，
                # 不累计该 alias 的其余候选，更不累计后续 alias。
                for alias, candidates in batches:
                    if len(alias) < 3:  # trigram 最短查询=3;别名门已保证,双保险
                        continue
                    canons = alias_to_canons[alias]
                    hits = []
                    for claim_id, folded in candidates:
                        if not boundary_hit(alias, folded):
                            continue
                        hits.append(claim_id)
                        if len(hits) > df_gate:
                            break
                    if len(hits) > df_gate:    # 泛词:整体丢弃 + 计数
                        dropped += 1
                        continue
                    for claim_id in hits:
                        d = claim_hits.setdefault(claim_id, {})
                        for c in canons:
                            d.setdefault(c, alias)  # 同 canonical 多别名命中只记首个
        if dropped > 0:
            self.event_log.emit({"kind": "mention_alias_df_dropped",
                                 "notebook_id": notebook_id, "dropped": dropped})

        # 5) 聚合:mention_edges(每 claim×命中 canonical 一行);concept_comentions
        #    按 claim 去重后两两组合(a<b)累计 bridge_claims。全量重写 + seq 写回。
        edges = [(notebook_id, claim_id, canon, alias)
                 for claim_id, canon_map in claim_hits.items()
                 for canon, alias in canon_map.items()]
        comention: Dict[Tuple[str, str], int] = {}
        skipped_claims = 0
        for canon_map in claim_hits.values():
            # ⚠ 组合上限(`_MENTION_MAX_CANONS_PER_CLAIM`,理由见常量处):DF 双门只
            # 界住「单别名命中多少 claim」,不界住「单 claim 命中多少 canonical」,
            # 而后者才是这里的 O(h²)。超限**整条跳过**(不截断——字典序前缀对中文
            # 概念有系统性偏置),且只作用于**组合**这一步:上面的 edges 已逐条建完、
            # 不受影响。
            if len(canon_map) > _MENTION_MAX_CANONS_PER_CLAIM:
                skipped_claims += 1
                continue
            for a, b in _combinations(sorted(canon_map), 2):
                comention[(a, b)] = comention.get((a, b), 0) + 1
        if skipped_claims > 0:
            # 无正文:只有条数与上限(对齐上面的 mention_alias_df_dropped)。
            self.event_log.emit({"kind": "mention_comention_claim_skipped",
                                 "notebook_id": notebook_id,
                                 "skipped": skipped_claims,
                                 "max_canonicals": _MENTION_MAX_CANONS_PER_CLAIM})
        cm_rows = [(notebook_id, a, b, n) for (a, b), n in comention.items()]
        with self._write() as db:
            self.unified_kg.replace_mention_bridge(
                db, notebook_id, edges, cm_rows, seq
            )
        return len(edges)

    def rebuild_communities(self, notebook_id: str, level: int = 0, force: bool = False) -> int:
        """在 canonical 实体图(关系两端经 cluster_map 映射)上跑 Louvain 社区检测,
        持久化到 communities + community_members(反向索引,存 canonical_name/centrality)。
        无 LLM、确定性(seed=42)。后端:igraph(装了即用,整数边表+C 核,10^6–10^7 边
        内存/耗时有界)优先,缺失回退 networkx;仅 networkx 回退且大库(scale-tier)无 CSR
        时拒(emit community_build_refused 返回 0,避免 OOM)。返回入库社区数。

        为何喂 canonical 图:裸 knowledge_relations 逐篇封闭(每篇的同名实体是不同
        object_id)→ 社区不跨文档。经 cluster_map 把两端映射到 canonical_id 后,不同
        文档里同一 canonical 天然合并,社区才跨文档(对比/广度题需要的"兄弟"结构)。

        ⚠ **两个独立的新鲜度闸,别把它们合成一个。** 社区图与 KG 分析产物是两份产物,
        可以各自陈旧:

        · 社区图闸(历史行为):`community_seq == kg_mutation_seq` → 不重跑 Louvain、
          不重写成员表。
        · 分析账本闸:`kg_analysis_artifacts` 齐全,且每一行都建在**当前的两个世代**
          上 —— KG 世代 `kg_mutation_seq`,以及依赖合并结果的那四份还要对齐簇世代
          `cluster_mutation_seq`(判据见 `kg_analysis_precompute.artifact_is_current`)。

        只有**两个闸都过**才是真正的 no-op。图新鲜而账本不新鲜时走「只补账本」路径:
        重建 canonical 边图并沿用库里**已有**的板块划分,跳过 Louvain、跳过
        `replace_communities`(那是 171 万成员行的整表重写)、也不重新铸板块 id。

        为什么必须这样(评审 B1):生产 base 库在本特性上线**之前**就已经建好社区且
        `community_seq == kg_mutation_seq`。如果分析产物跟着社区图闸走,那么部署之后
        任何一次非 force 的 `rebuild_communities` 都会在闸上直接返回,
        `kg_analysis_artifacts` **永远是空的** —— 用户打开视图只看得到「产物缺失」,
        唯一的恢复手段是 `force=True`,而那是 836 万边全量 join + Louvain + 重写 171 万
        成员行,正是设计 §3.2 说本工具不该让用户付的那笔钱。同一个机制还会让「预计算
        失败」变成**永久**失败:失败后账本是空的,而社区图闸照样短路。

        ⚠ **「建过」的标记是 seq 对齐,不是「板块数 > 0」。** 全部连通块都小于
        `community_min_size` 的库**合法地**就是零板块 —— 那不是「没建成」。拿板块数当
        建成标记会让这类库的两个闸都永远过不去:每一次「刷新图谱」都要重跑 canonical
        边图 + 三条全表统计快照(生产量级见 `_compute_kg_analysis`),而结果永远是
        同一个零。`community_seq` 默认 -1、`kg_mutation_seq` 恒 >= 0,所以「从没建过」
        照样判得出来;而 `communities` 只被 `replace_communities` 删改、它又与
        `set_community_seq` **同事务**提交,所以 seq 对齐 ⟺ 板块确实按那个 seq 写过。

        ⚠ **那份不分 level 的账目整体归默认层独有**(codex 第 11/15 轮 P2)。
        `community_seq`、`kg_analysis_artifacts`、以及两张依赖板块划分的产物表都没有
        level 维度(第 7 轮刻意去掉的,理由见设计 §3.4 订正 2)。第 11 轮先补了**读侧**:
        level 0 对齐之后一次非 force 的 `rebuild_communities(..., level=1)`——库里一行
        level-1 都没有——会满足闸、直接返回 0 而根本不建那一层。但**写侧**照旧对所有层
        推进那份共享的 seq,于是反向的洞还在(第 15 轮 P2-1):先建 level 1 把 seq 推到
        当前,随后 `rebuild_communities(level=0)` 在 level 0 一行都没有时仍然「seq 对齐」,
        拿别的层的账目短路返回 0 —— 而默认层那一支是**无条件**信 seq 的(零板块是合法
        终态),它信的正是这个被顶掉的数。

        补法不加列,把那句蕴含关系变成写侧的不变式:**只有默认层的构建碰共享账目**。
          · `set_community_seq` 只在 `level == DEFAULT_COMMUNITY_LEVEL` 时调 → 「seq 对齐
            ⟺ 默认层的板块按那个 seq 写过」重新成立(它与 `replace_communities(level=0)`
            同事务,别处没有第二个写点);
          · 预计算与 `discard_board_dependent_...` 同样只在默认层跑 → 非默认层的构建
            既不会连坐作废默认层**有效**的两份产物(它的 `replace_communities` 按 level
            删,默认层的板块 id 一个都没变、根本没悬空),也不会把指向另一层板块 id 的
            产物写进默认层的读侧;
          · 闸本身也收进默认层:非默认层没有任何账本,所以它**永不短路**。保留「其它层
            退回 `communities_count(level) > 0`」那一支会把陈旧的划分当成新鲜的 —— 共享
            的 seq 只证明**默认层**建于当前 KG 世代,而那一层的行可能建在好几代以前。

        代价是显式的、也是刻意选的,全部落在休眠路径上:非默认层每次都全量重建(只会
        **多**建、绝不会漏建),而且它不产出分析报告 —— `/kg-analysis` 因此永远描述默认
        层,不会出现「板块列表是 level 1、跨板块边是 level 0」那种自相矛盾的报告(那正是
        `kg_analysis.community_overview` 不收 level 参数的理由)。今天没有任何调用点传
        非默认层,所以这笔代价的真实值是 0;拿一列 schema 去换它不值。

        同理,`has_boards` 必须传**实际**的板块数而不是硬写 True:零板块的库不写
        来源画像(设计 §3.3 的 S4 —— 那张表单独看是在说「所有来源都关联稀疏」),
        硬写 True 等于宣布这类库的账本永远不完整。判据与写入口
        `check_artifact_payloads` 的复核**同一条**(都是板块个数),两边不会分岔。

        ⚠ **簇世代也是账本闸的一部分**(codex 第 5 轮评审)。`concept_clusters` 的
        写路径(`write_clusters` / `append_clusters` / 整理时的 cluster-map swap)
        **刻意不动** `kg_mutation_seq`,变化信号独立在 `cluster_mutation_seq` 上。
        只看 KG 世代的闸对整条簇写路径完全失明:簇变了、簇大小直方图与最大簇榜单
        (直接从 `concept_clusters` 算)跟着变了,而闸短路、读侧报「与当前一致」。
        哪四份受影响、`relation_provenance` 为什么不受影响,依据见
        `app.domain.kg_analysis_contracts.CLUSTER_DEPENDENT_ARTIFACT_KINDS` 的逐条 SQL 说明。

        簇世代触发的补账本走的仍是上面那条「只补账本」路径 —— 它**沿用库里已有的板块
        划分**,而那批板块是在旧的合并结果上跑出来的。这不是本次新引入的近似:B1 那条
        路径本来就是「用当前的 canonical 边图 + 库里已有的划分」,设计里已经明确接受
        (代价是 171 万成员行的整表重写)。真正把板块也重铸的是 `rebuild_unified_kg`
        的收尾 —— 它在聚类真的重算之后用 `force=True` 调本方法,理由与本段同源。
        ⚠ 已知的残留缺口(**不**在本次修复范围内,需要一列才能补):板块划分自己没有
        地方记「它建在哪一代合并结果上」,所以 `boards` 那一格的落后量只按
        `community_seq` 报,报不出簇世代。补它要给 `unified_kg_state` 加一列
        = 追加迁移 + bump `SCHEMA_VERSION`。

        ⚠ **补账本路径的板块划分读自写事务之外,发布前必须复核**(codex 第 16 轮 P2)。
        这条路径的 `kept_rows` 来自 `community_rows_for_summary`(事务外),而随后的
        `_compute_kg_analysis` 在生产上是分钟级的 —— 那段窗口里另一次 `force=True` 的
        重建可以把整套板块换掉:**本方法自己没有任何单飞守卫**。`POST /notebooks/{id}/
        unified-kg/rebuild` 如今是后台化的 `run_unified_kg_rebuild_job` 的一步,那条链路
        已经在共享的 per-notebook KG 维护槽下(`KgMaintenanceAlreadyRunning`,见
        `run_unified_kg_rebuild_job`),两次点击不会真的并发跑到这里——但离线的
        `backend/app/scripts/recluster_kg.py`(`force=True`)不经过那个槽,直接调
        `rebuild_unified_kg` → `rebuild_communities`,任何跨进程/跨部署运维触发的重跑
        同样绕得开。所以发布事务里、写产物之前要问一句「我算的那套划分还在吗」
        (`board_partition_still_holds`,查一行足矣 —— `replace_communities` 是整表
        删再插、id 128 bit 新铸),不在就**整份放弃发布**、发
        `kg_analysis_backfill_discarded`,靠既有的账本闸下一次重来。
        全量路径不需要这道复核(板块与产物同事务,构造上自洽);零板块那一档刻意不查,
        理由写在发布段的注释里(没有东西会悬空 + 计数检查挡不住 phantom insert)。
        彻底的替代方案是在 notebook 级串行化整个重建,那超出本次范围。
        """
        self.get_notebook(notebook_id)
        if not self.settings.community_layer_enabled:
            return 0
        # 版本闸(增量):社区已按当前 kg_mutation_seq 建过 → 不重建图(除非 force)。
        # 让「刷新图谱」等重复触发在 KG 未变时秒级 no-op;首次(community_seq=-1)或 KG
        # 变动后(seq 不匹配)才重跑。无 unified_kg_state 行 → _st=None → 不跳过(安全兜底)。
        # ⚠ 默认层的判据里**没有**板块数:零板块是合法终态,理由见 docstring 那段。
        # 非默认层反过来 —— 那份不分 level 的账目替它说不了话,见下面的方向四/方向五。
        # 账本读的是**整行**(含 payload)而不是只读 seq:簇世代盖在 payload 里(刻意
        # 不加列,见 `app.domain.kg_analysis_contracts.CLUSTER_SEQ_PAYLOAD_KEY`),闸拿不到 payload
        # 就判不出簇是否漂过。这也是同一次读同时喂给下面 `_compute_kg_analysis` 的
        # 复用判断 —— 账本只读一遍,闸与复用不可能读到两份不同的账本。
        with self._connect() as _db:
            _st = self.unified_kg.state_row(_db, notebook_id)
            _cnt = self.unified_kg.communities_count(_db, notebook_id, level)
            _ledger = self.unified_kg.kg_analysis_artifact_rows(_db, notebook_id)
        _seq = int(_st["kg_mutation_seq"]) if _st else 0
        _cseq = int(_st["cluster_mutation_seq"]) if _st else 0
        _boards = int(_cnt["c"]) if _cnt else 0
        # 方向四(请求的 level 没建过)+ 方向五(别的层的构建不得替默认层背书):
        # `community_seq`、账本、两张依赖板块的产物表**都不分 level**,那份共享账目整体
        # 归 `DEFAULT_COMMUNITY_LEVEL` 独有 —— 只有它写、也只有它读(docstring 那段)。
        # 于是闸也只对默认层成立:非默认层没有任何账本,所以它**永不短路**、每次都全量
        # 重建。少了这一句,level 0 对齐之后一次 `rebuild_communities(..., level=1)` 会
        # 满足闸、直接返回 0 而**根本不建那一层**(方向四);而退回「其它层看
        # `communities_count(level) > 0`」同样不行 —— 共享的 seq 只证明默认层建于当前
        # KG 世代,那一层的行可能建在好几代以前,「有行」会把陈旧划分冒充成新鲜的。
        _ledger_speaks_for_level = level == DEFAULT_COMMUNITY_LEVEL
        graph_fresh = bool(
            not force and _st is not None and int(_st["community_seq"]) == _seq
            and _ledger_speaks_for_level
        )
        if graph_fresh and analysis_ledger_is_current(
            _ledger, _seq, _cseq, has_boards=_boards > 0
        ):
            return _boards
        # 社区检测后端:igraph(整数边表 + C 核,10^6–10^7 边内存/耗时有界)优先;
        # 缺失(未装)才回退 networkx(纯 Python dict-of-dicts,大库会 OOM)。
        try:
            import igraph as _ig
        except Exception:
            _ig = None
        # 大库守卫:仅在 networkx 回退(无 igraph)时才拒 scale-tier 无 CSR(避免 OOM)。
        # igraph 路径整数边表 + C 核,10^7 边安全,无需 CSR、不拒。
        # 只补账本时同样要过这道守卫:那条路径不跑 Louvain,但仍然要把 canonical 边图
        # 拉进内存,拒的理由(内存)一样成立。区别只在返回值 —— 社区还在,如实返回它的
        # 数量,不能因为一份辅助报告没补上就谎报「一个社区都没有」。
        if (_ig is None
                and not self.notebook_copy_stats(notebook_id)["copyable"]
                and self.scale_artifacts.load(notebook_id, allow_stale=True) is None):
            self.event_log.emit({"kind": "community_build_refused",
                                 "notebook_id": notebook_id, "reason": "no_scale_index"})
            return _boards if graph_fresh else 0
        # canonical 整数边图:SQL-join 把关系两端映射到 canonical(未聚类→自身 object_id),
        # 整数索引累加边权。避开 networkx dict-of-dicts 与全量 cluster_map dict → 10^7 边
        # 内存有界(concept_clusters.member_object_id 有索引,join 走索引)。
        #
        # ⚠ 边表是**列式 int32 数组**而不是 `dict[(int, int), int]`(设计 §3.32,
        # codex 第 2/7/9 轮那三个 P1 的共同根源就是那个 dict 形态)。dict 每条边要一个
        # 新分配的 int 二元组 + 哈希槽,生产 836 万边约 1.4 GB;而且**折叠它**必然要么
        # 造第二份同量级 dict、要么依赖不缩哈希表的 `popitem`。三个数组把同一份数据压到
        # 约 100 MB,折叠是纯向量化。
        can2idx: "Dict[str, int]" = {}
        edge_builder = CanonicalEdgeTableBuilder()
        with self._connect() as db:
            names, graph_rows = self.unified_kg.community_graph_rows(db, notebook_id)
            for r in graph_rows:
                s, t = r["s"], r["t"]
                if not s or not t or s == t:
                    continue
                si = can2idx.setdefault(s, len(can2idx))
                ti = can2idx.setdefault(t, len(can2idx))
                edge_builder.add(si, ti)
        idx2can = [""] * len(can2idx)
        for _c, _i in can2idx.items():
            idx2can[_i] = _c
        n_nodes = len(can2idx)
        # 行序 = 每对 canonical 的**首次出现序**,与被它取代的那张 dict 的插入序逐字
        # 一致 —— Louvain 对边序敏感,漂了就是已部署库的板块划分全漂(设计 §3.32
        # 不变量 1)。`build` 里那段说明写了为什么必须是稳定排序。
        edge_table = edge_builder.build(n_nodes)
        del edge_builder
        if graph_fresh:
            # 只补账本:板块划分从库里读回来(`communities.member_ids` 就是 Louvain 那次
            # 写下的 canonical 列表),Louvain / centrality / 两表重写全部跳过。
            with self._connect() as db:
                kept_rows = [
                    (row["id"], json.loads(row["member_ids"] or "[]"))
                    for row in self.unified_kg.community_rows_for_summary(
                        db, notebook_id, level
                    )
                ]
            kept = len(kept_rows)
        else:
            # 社区检测 + 度中心度(deg: canonical -> degree)。comms: list[list[canonical]]。
            comms: "List[List[str]]" = []
            deg: "Dict[str, float]" = {}
            if n_nodes:
                if _ig is not None:
                    import random as _random
                    _random.seed(42)
                    try:
                        _ig.set_random_number_generator(_random)   # 确定性(seed=42)
                    except Exception:
                        pass
                    # igraph 直接吃 numpy 边数组。`igraph_edges()` 返回的 (M, 2) 数组
                    # 是**独立缓冲区**(column_stack 是拷贝,不是视图),所以第 7 轮
                    # 评审抓到的那个别名 —— `list(ew.keys())` 与 `ew` 共享同一批 key
                    # 元组、一个活着另一个一个字节都放不掉 —— 在数组模型下不存在。
                    # 仍然写成临时值:igraph 拷进 C 结构后这一句就结束、拷贝当场回收。
                    G = _ig.Graph(n=n_nodes, edges=edge_table.igraph_edges())
                    G.es["weight"] = edge_table.weight.tolist()
                    membership = G.community_multilevel(weights="weight").membership
                    degs = G.degree()
                    buckets: "Dict[int, List[str]]" = {}
                    for _i, _m in enumerate(membership):
                        buckets.setdefault(_m, []).append(idx2can[_i])
                        deg[idx2can[_i]] = float(degs[_i])
                    comms = list(buckets.values())
                    # ⚠ Louvain 的图到此为止,当场放掉。igraph 把整张边表拷进了自己的
                    # C 结构(生产 836 万边),而它的全部产出(membership / degree)
                    # 已经抄进 `comms` / `deg`。留着它就会一路跨过下面预计算里的四次
                    # 全表扫 —— 与折叠结果不释放(codex 第 9 轮 P1-2)是同一类缺陷,
                    # 只是换了一个持有者。
                    del G, membership, degs, buckets
                else:
                    import networkx as nx
                    from networkx.algorithms.community import louvain_communities
                    g = nx.Graph()
                    for _a, _b, _w in zip(
                        edge_table.src.tolist(),
                        edge_table.dst.tolist(),
                        edge_table.weight.tolist(),
                    ):
                        g.add_edge(_a, _b, weight=_w)
                    comms = [[idx2can[_i] for _i in c]
                             for c in louvain_communities(g, weight="weight", seed=42)]
                    for _i, _d in g.degree():
                        deg[idx2can[_i]] = float(_d)
                    # 与 igraph 分支那句 `del G, …` 同一条不变式:图的全部产出已经抄进
                    # `comms` / `deg`,而 networkx 的 dict-of-dicts 比 igraph 的 C 结构
                    # 更占地方。不放掉它就会一路跨过下面预计算的四趟全表扫。
                    del g
            min_size = self.settings.community_min_size
            # Policy (min-size filter + id minting + member ordering) stays here;
            # the store owns the two-table full rewrite.
            kept_rows = [(self._new_id("cm"), sorted(comm))
                         for comm in comms if len(comm) >= min_size]
            kept = len(kept_rows)
            del comms
        # ⚠ 社区检测的中间结果到此为止(codex 第 14 轮 P2)。`kept_rows` 已经把要发布的
        # 内容全抄走了,而 `comms` / `idx2can` 装的是**同一批 canonical 字符串的另一份
        # 引用**、`g`(networkx 兜底)是整张图。它们此前一直活到函数返回,也就是活着跨过
        # 下面**新增的**那四趟全表扫(来源画像 + 三条统计快照),把两个内存峰值叠在一起;
        # 生产 171 万 canonical 的量级下这不是可忽略的常数。
        # 这与 `_compute_kg_analysis` 内部「`can2idx` / `community_of_index` 用完即放」
        # 是同一条不变式的两半 —— 那半由「membership 不得活着跨过三条统计快照」守着。
        # `can2idx` 本身**不能**在这里清:`SourceBoardCounter` 还要用它做 canonical→下标。
        idx2can.clear()
        # KG 质量分析的预计算产物(设计 §3.2/§3.3):挂在这一次图构建上顺带**算出来**,
        # 与板块划分一起在下面那**一个**写事务里发布(codex 第 13 轮 P1)。
        #
        # ⚠ **计算全部在写事务之外**,这条是硬约束不是风格:五份产物里有四份是全表级
        # 重活(生产 878 万对象 / 836 万边),而 `SqliteDatabase.write()` 拿的是**进程级
        # 写锁**——把它们挪进写事务就等于把全库写入按住一整趟全表扫(同形状数据点:
        # 835 万边冷扫 39 分钟)。两侧 store 的只读方法自己会检查线程写深度并当场硬失败
        # (`_reject_inside_write_transaction`),`tests/test_kg_analysis_precompute.py`
        # 另有按调用时刻记 `write_depth` 的语义守卫。
        #
        # 为什么**不**让计算的失败冒泡:这三张表是只读报告的派生产物,而 rebuild_communities
        # 是 rebuild_unified_kg 与「刷新图谱」的核心路径。让一份辅助报告的失败去掀翻
        # 437GB 生产库的图重建,爆炸半径不成比例。而**吞掉它是安全的**,因为产物自带
        # `kg_mutation_seq` 戳:失败时上一轮的产物留在原地、戳还是它自己那个旧值,T3
        # 会如实报「落后 N 次变更」并给出整理入口 —— 不是静默降级,是显式陈旧 + 事件。
        # 反过来若让它冒泡,板块本来该照常发布,异常只会让调用方以为整个重建失败。
        #
        # ⚠ 这条豁免只覆盖**计算**。下面那个发布事务本身失败照旧冒泡 —— 它就是板块自己
        # 的写事务(`replace_communities` 的失败一向冒泡),而且原子之后「事务失败」的
        # 含义是**这一趟什么都没发生**:吞掉它再返回 `kept` 就是谎报一次并不存在的重建。
        #
        # ⚠ 「留在原地」只对**与板块无关**的三条统计快照成立。图真的被重建过时,依赖
        # 板块的那两份在同一个发布事务里作废(见下面 `discard_...` 旁的说明)——
        # 它们**必须**走,留着就是指向不存在板块 id 的悬空产物。此时 T3 报的是「缺失」
        # 而不是「陈旧」,那是更诚实的一档,不是退化。
        #
        # 失败**会自愈**:上面的分析账本闸只看账本齐不齐、戳对不对,所以下一次
        # `rebuild_communities`(哪怕 KG 一动没动、哪怕不带 force)会走「只补账本」
        # 路径重试。这正是把两个闸分开的第二个理由。
        #
        # ⚠ 取消**不算**失败,必须放行:`KgBuildAborted` 是用户要停,吞掉它会让中止
        # 看起来成功了。KeyboardInterrupt / SystemExit 属 BaseException,天然不被
        # `except Exception` 捕获。
        # ⚠ 失败**不冒泡,但也绝不谎报成功**(codex 第 8 轮 P2)。这两件事此前被混成
        # 一件:异常被记进 `kg_analysis_precompute_failed`,紧接着照样 emit
        # `kg_analysis_backfilled` —— 同一次执行里失败事件与成功事件并存,而账本其实
        # 仍然不完整。运维监控与任何事件消费方都会被误导(「补过了」与「补失败了」是
        # 相反的运维动作)。变的只是那条成功事件的前提。
        #
        # ⚠ 只有默认层跑它(方向五):账本与两张产物表都不分 level,让非默认层去写就是
        # 把指向**另一层**板块 id 的产物塞进默认层的读侧,而读侧照旧按 `community_seq`
        # 对齐判「与当前一致」。非默认层因此只发布它自己的板块行,一个字都不占共享账目。
        analysis: "Optional[Tuple[list, list, Dict[str, dict]]]" = None
        if not _ledger_speaks_for_level:
            # 边表在这里就到头了 —— 它平时是交给 `_compute_kg_analysis` 边消化边释放的
            # (生产 836 万边约 100 MB),这条路径没有接手方,得自己放掉,不能让它一路
            # 活到函数返回。`can2idx` 同理(生产 171 万 canonical)。
            del edge_table
            can2idx.clear()
        else:
            try:
                analysis = self._compute_kg_analysis(
                    notebook_id, level, _cseq, kept_rows, edge_table, can2idx,
                    # `force` 是「用户明确要求重算」——它上面不省钱,见
                    # `reusable_artifact_payloads` 的最后一段。
                    reusable=({} if force
                              else reusable_artifact_payloads(_ledger, _seq, _cseq)),
                    # 板块划分是这一轮**刚跑出来**的,还是库里现成的?依赖板块的两份产物
                    # 据此决定盖不盖簇世代的戳(见 `stamp_cluster_seq`):补账本那条路径
                    # 拿的是现成的划分,它建在哪一代合并结果上没有地方记,盖上当前世代就是
                    # 把一份混合世代的东西说成「与当前一致」(codex 第 7 轮 P2)。
                    partition_rebuilt=not graph_fresh,
                )
            except KgBuildAborted:
                raise
            except Exception as exc:
                self.event_log.emit({
                    "kind": "kg_analysis_precompute_failed",
                    "notebook_id": notebook_id, "level": level, "seq": _seq,
                    "error": f"{type(exc).__name__}: {exc}",
                })
        # ⚠ 「真的发布了」与「算出来了」是两件事(下面那道复核会让它们分岔),而事件必须
        # 跟着前者走 —— 放弃发布还发「补齐了」就是谎报,与第 8 轮 P2 修的是同一个病。
        analysis_published = False
        partition_replaced_under_us = False
        # ---- 发布:板块划分 + 版本戳 + 全部分析产物,**一个**写事务(codex 第 13 轮 P1)
        #
        # 修复前这里是三个事务(板块 → 版本戳 → 产物),中间隔着分钟级的预计算。生产
        # (878 万对象 / 836 万边)上并发的 `/kg-analysis` 因此会读到「新板块 + 旧账本」:
        # 板块 id 已经换了一整套,而三条统计快照还停在上一个 seq、依赖板块的两份已被作废
        # 但还没写回。报告同屏混着两代库,而本视图存在的全部理由就是「每个数字都能说出
        # 自己建于何时」。把重活挪进事务不是选项(见上面那段写锁的说明),所以改成
        # **先在事务外把全部产物算完,再一次性发布**。
        #
        # 这个事务只做便宜的写:两表整表重写(成员行数级)+ 一次 UPDATE + 三张产物表的
        # 整批重写。它一个 SELECT 重活都不发。
        #
        # ⚠ 没东西可写就**不开事务**:图新鲜 + 预计算失败那一档,这一趟确实什么都没发生,
        # 而 `_write()` 拿的是进程级写锁 —— 为一个空事务去抢它是纯浪费。
        if not graph_fresh or analysis is not None:
            now = self._now()
            with self._write() as db:
                if not graph_fresh:
                    self.unified_kg.replace_communities(
                        db, notebook_id, level, kept_rows, names, deg, now
                    )
                    # ⚠ 上一行**重铸了板块 id**,依赖板块划分的两份分析产物(跨板块边 /
                    # 来源画像)此刻整表变成悬空引用 —— 必须与重铸**同事务**作废。预计算
                    # 失败时下面那次整批重写不会发生,而板块照旧发布:少了这一句,库里就
                    # 会留下指向已不存在板块 id 的悬空产物,而 T3 的记忆化签名对「同 seq
                    # 的 force 重铸」是瞎的 —— 已预热的缓存会**无限期**吐上一套板块 id,
                    # 直到 LRU 淘汰或进程重启。作废账本行同时也就动了签名。
                    # (⚠ 账本里一行都没有时这次作废是 no-op,签名照样不动;那一档由读侧
                    #  的 `kg_analysis._signature_tracks_board_recasts` 拦成「不写缓存」。)
                    # 只作废这两份(三条统计快照与板块无关,留着仍是可读的陈旧快照),
                    # 理由见 `app.domain.kg_analysis_contracts.BOARD_DEPENDENT_ARTIFACT_KINDS`。
                    # 预计算成功时它是冗余的(下面那次重写整表删),留着是因为它守的是
                    # **失败**那一档,而那一档没有别的东西替它守。
                    #
                    # ⚠ 作废与记版本都**只在默认层**做(方向五,codex 第 15 轮 P2-1)。
                    # 上面那次 `replace_communities` 是按 level 删/写的,非默认层重铸的
                    # 是它自己那一层的板块 id —— 默认层的板块 id 一个都没变,那两份产物
                    # 根本没悬空,作废它们是**连坐**;而推进那份不分 level 的 seq 更糟:
                    # 「seq 对齐 ⟹ 默认层建过」是闸里默认层那一支**无条件**信的蕴含关系,
                    # 让别的层去推它,默认层就会在一行板块都没有时被判成「建过」。
                    if _ledger_speaks_for_level:
                        self.unified_kg.discard_board_dependent_kg_analysis_artifacts(
                            db, notebook_id
                        )
                        # 记版本:社区已按 _seq 建好(无 unified_kg_state 行则 UPDATE
                        # no-op,下次仍重建)。⚠ 它必须与 `replace_communities`
                        # **同事务**:分开提交会留下「板块已换、seq 还是旧的」的窗口,
                        # 而闸正是拿 seq 判「建过没有」。
                        self.unified_kg.set_community_seq(db, notebook_id, _seq)
                if analysis is not None:
                    # ⚠ **发布前复核板块划分还在**(codex 第 16 轮 P2)。只有「只补账本」
                    # 那一档需要:它的 `kept_rows` 是**写事务之外**从库里读回来的,而随后
                    # 的 `_compute_kg_analysis` 在生产(878 万对象 / 836 万边)上是分钟级
                    # 的,窗口很宽。期间另一次 `force=True` 的重建完全可以把整套板块换掉
                    # —— 本方法自己没有单飞守卫;`POST /notebooks/{id}/unified-kg/rebuild`
                    # 如今经共享的 per-notebook KG 维护槽串行化,但离线
                    # `backend/app/scripts/recluster_kg.py`(`force=True`)不经过那个槽,
                    # 跨进程/跨部署运维触发的重跑同样能撞上同一条路径。不复核就会写出
                    # 指向已不存在板块 id 的 `kg_community_edges` /
                    # `kg_source_profiles`,而它们的账本盖着「与当前一致」的戳 —— 比缺失
                    # 更糟,缺失至少是诚实的。
                    #
                    # 全量路径**不需要**这道复核:那一档的板块是上面同一个事务里刚写的,
                    # 板块与产物构造上自洽,没有任何窗口可钻。
                    #
                    # ⚠ **查一行就够。** `replace_communities` 对 (notebook_id, level) 是
                    # 整表删再插、新 id 由 `_new_id("cm")` 铸出(128 bit),所以「`kept_rows`
                    # 里任意一个 id 还在」⟺「期间没有任何一次 replace 提交过」。原子性由
                    # store 那侧兑现(PG 的 `FOR SHARE` 行锁 / SQLite 的进程级写锁),
                    # 两侧的不对称写在它的 docstring 里。
                    #
                    # ⚠ **零板块那一档刻意不查,而且刻意不退化成 `COUNT(*) == 0`。** 两个
                    # 理由,后一个更硬:
                    #   1. 零板块时**没有任何东西会悬空** —— 来源画像整份跳过
                    #      (`_compute_kg_analysis` 里的 `if total_members:`),跨板块边是
                    #      空的。最坏结果只是一份 `communities: 0` 的陈旧载荷,而
                    #      `analysis_ledger_is_current(..., has_boards=_boards > 0)` 在下一次
                    #      调用(那时 `_boards > 0`)会因为缺 `source_profiles` 判不齐全 →
                    #      自动重补。一次调用内自愈,不需要人工介入。
                    #   2. 没有行可锁 ⟹ 计数检查挡不住 phantom insert。加一个**看起来严密、
                    #      实际有洞**的守卫,正是本 PR 已经吃过十几次亏的那个形态 ——
                    #      守卫不得夸大自己的强度。
                    if (graph_fresh and kept_rows
                            and not self.unified_kg.board_partition_still_holds(
                                db, notebook_id, level, kept_rows[0][0])):
                        # 放弃整份发布,**不重试**:重试要重跑那趟分钟级的全表重活,而且
                        # 可能反复被同一个抢占者打断。既有的自愈路径就是为这一档设计的 ——
                        # 账本不齐 ⟹ 下一次 `rebuild_communities`(哪怕不带 force)照样走
                        # 补账本重来,那时它读到的是抢占者留下的那套划分。
                        partition_replaced_under_us = True
                    else:
                        _edges, _profiles, _payloads = analysis
                        self.unified_kg.replace_kg_analysis_artifacts(
                            db, notebook_id, _seq, _edges, _profiles, _payloads, now
                        )
                        analysis_published = True
        if graph_fresh:
            # 图没有被重建,只补了账本。用一个**不同**的事件名如实说这件事 ——
            # 复用 communities_rebuilt 会让运维在事件流里看到一次并不存在的图重建。
            #
            # ⚠ 只在产物**真的落库**时发:这条路径除了补账本什么都没做,一旦预计算失败
            # (`kg_analysis_precompute_failed`)或发布前的板块复核判失效
            # (`kg_analysis_backfill_discarded`),这一趟就等于什么都没发生,发一条
            # 「补齐了」是纯粹的谎报。两档各有自己的事件,都不是静默。
            # 判据因此是 `analysis_published` 而不是「算出来了」—— 后者在复核放弃那一档
            # 仍然为真(codex 第 16 轮 P2)。
            # 返回值不受影响:社区还在库里,如实返回它的数量 —— 一份辅助报告没补上,
            # 不能表现成「一个板块都没有」。这里的 `kept` 是读到那一刻的板块数,刻意不为
            # 复核失败再发一次查询:那些板块确实在库里存在过,而抢占者写的是它自己的一套。
            if partition_replaced_under_us:
                # 与 `kg_analysis_precompute_failed` 并列的**另一条**事件,刻意不复用它:
                # 「算不出来」与「算出来了但期间板块被换掉」的运维动作不同,前者要查模型/
                # 数据,后者说明有两个重建在抢同一个库。
                self.event_log.emit({
                    "kind": "kg_analysis_backfill_discarded",
                    "notebook_id": notebook_id, "level": level, "seq": _seq,
                    "reason": "board_partition_replaced",
                })
            if analysis_published:
                self.event_log.emit({
                    "kind": "kg_analysis_backfilled", "notebook_id": notebook_id,
                    "level": level, "seq": _seq, "communities": kept,
                })
            return kept
        self.event_log.emit({"kind": "communities_rebuilt", "notebook_id": notebook_id,
                             "level": level, "communities": kept, "nodes": n_nodes})
        return kept

    def _compute_kg_analysis(
        self,
        notebook_id: str,
        level: int,
        cluster_seq: int,
        kept_rows: "List[Tuple[str, List[str]]]",
        edge_table: "CanonicalEdgeTable",
        can2idx: "Dict[str, int]",
        *,
        reusable: "Optional[Dict[str, dict]]" = None,
        partition_rebuilt: bool,
    ) -> "Tuple[list, list, Dict[str, dict]]":
        """把 KG 质量分析的产物在**这一次**图构建里算出来(设计 §3.2)。**不落库。**

        为什么挂在这里而不是端点上:五份里有三份是**全表级重活**(簇大小直方图 / 最大簇
        榜单 / 边出处构成,见 ports.py 只读聚合段头的双后端实测),生产库 200 万簇行 /
        800 万边,冷态与 `relation_connected_object_ids` 那次 39 分钟事故同量级。它们
        绝不能挂在在线请求上。另两份(跨板块边 / 来源画像)则是这次构建的边际成本:
          · 跨板块边:canonical 边表已经在手,按 membership 向量化折叠一遍,近似免费;
          · 来源画像:新增一次 `knowledge_objects` 扫描,与 `community_graph_rows`
            同量级 —— 是新增开销,但与社区构建同批,不给在线请求付。

        一个板块都没有时**整份跳过来源画像**(不写账本行、不写明细行,连那次扫描都不做):
        没有板块就没有「主体」,每个来源的 mainstream_share 都会是 0.0,而那张表单独看
        是在说「所有来源都关联稀疏」。理由与守卫见 `kg_analysis_precompute.
        OPTIONAL_ARTIFACT_KINDS` / `check_artifact_payloads`。

        新鲜度(设计 §3.3):全部产物写同一个 KG 世代(= 本轮 rebuild 的目标
        `kg_mutation_seq`,与 `community_seq` 同一个数,由发布方一次性盖上),账本因此
        能逐份回答「我建于哪个 KG 状态」。生产上「产物严重落后于当前 KG」是常态而非边缘
        情况,所以这个戳是产品需求,不是审计装饰。

        ⚠ **两个世代,不是一个。** 依赖合并结果的四份还要盖 `cluster_seq`
        (= `unified_kg_state.cluster_mutation_seq`),因为簇的写路径刻意不动
        `kg_mutation_seq`。盖戳由中性的 `stamp_cluster_seq` 统一做(它知道哪四份要盖),
        两侧 store 的 `check_artifact_payloads` 当场复核少盖/多盖 —— 盖与查刻意分在
        不同的函数里,同一个函数里既盖又查的守卫恒真。

        ``partition_rebuilt``:``kept_rows`` 是这一轮 Louvain 刚跑出来的(True),还是
        从库里读回来的现成划分(False,「只补账本」那条路径)。依赖板块的两份产物据此
        决定盖不盖那个戳:后一档的板块划分建在哪一代合并结果上**没有地方记**,而边与
        来源映射是按当前 cluster_map 现算的 —— 产物是混合世代的,只能如实记「无从判断」
        (codex 第 7 轮 P2,完整理由见 `stamp_cluster_seq`)。它没有默认值,就是要让每
        一个调用点当场表态:默认成 True 会让新调用点悄悄开始撒谎。

        ``reusable``:上一轮账本里**可以原样搬过来**的 payload(只可能是与簇无关的
        `relation_provenance`,判据与理由见 `reusable_artifact_payloads`)。给了就不
        重算那一份 —— 纯合并写入触发的补账本因此不用白跑一趟 836 万边的全表扫。
        为空 / 不传 = 全部重算(`force=True` 与首次构建走这条)。

        ⚠ **这个戳保证的是「都属于同一轮预计算」,不是「都读到了同一份 KG 快照」。**
        本方法里有四个以上互不同步的读点(跨板块边用的边表建于 rebuild 的图扫描、
        来源画像自开一条读连接、三条统计快照各自再自开一条),一次并发的 `store_kg`
        插在中间,直方图就会反映一个比跨板块边更新的 KG。发布事务保证的是**可见性
        原子**(要么整批可见、要么整批不可见),不是**内容原子**。
        两个后端各自的实际保证:
          · PostgreSQL:每条快照查询自己是一次 `REPEATABLE READ` 只读事务(多语句的
            `community_overview` 内部一致),但**跨查询之间没有共享快照** —— 池化连接
            拿不到跨方法的事务边界。
          · SQLite:`community_overview` 用 `BEGIN DEFERRED` 起 WAL 快照;三条重活查询
            走本线程复用的读连接、**自动提交**,即每条语句各取一个快照。
        刻意不去把 SQLite 侧四条读包进一个外层事务:那会做出一个 PostgreSQL 侧**无法
        对等**的保证(池化连接),而 parity 红线要的是语义等价;服务层也不许判 dialect。
        与其承诺一个只有一侧成立的强一致,不如把实际保证写清楚,让 T3/T4 知道哪些数字
        可以并排放、哪些不能。

        ⚠ **本方法一个字节都不写库**(codex 第 13 轮 P1 的原子发布)。它返回
        ``(edges, profiles, payloads)``,由 `rebuild_communities` 在**板块自己的那个**
        写事务里一起发布 —— 板块与全部产物因此对读者是同一个瞬间出现的,不再有
        「新板块 + 旧账本」这个生产上分钟级的窗口。半份产物比没有产物更危险,而「新板块
        配旧产物」比半份还糟:报告同屏混着两代库,一个标注都没有。

        ⚠ **正因为如此,本方法里的重活绝不能被挪进那个写事务。** 两个理由,后一个更硬:
          1. 三条统计快照自开只读连接(见 ports.py 的调用契约),在写事务里调会读到提交
             前的旧数据,而且**不会响亮失败**;
          2. `SqliteDatabase.write()` 拿的是**进程级写锁**。把这几条全表重活挪进写事务,
             那把锁会被按住整整一趟全表扫 —— 同形状的数据点是 835 万边冷扫 39 分钟,
             期间全库任何写入(摄取、问答落库、投影)全部排队。
        这条不再只是注释:SQLite 侧的读方法自己会检查线程的 `write_depth` 并当场硬失败
        (见 `sqlite/unified_kg_store.py::_reject_inside_write_transaction`),
        `tests/test_kg_analysis_precompute.py` 另有一条按调用时刻记录 `write_depth`
        的语义守卫 —— 形状守卫(「哪个写事务碰过产物表」)抓不到这类移动,因为这三条
        查询一张产物表都不碰。原子发布让「顺手把计算搬进那个事务」看起来更整洁了,
        所以那两道守卫比以前更要紧,不是更不要紧。
        """
        sizes = [(cid, len(members)) for cid, members in kept_rows]
        # 节点下标 -> community_id。**刻意不是** canonical -> community_id 的 dict:
        # 那会在边表还占着大头的这一刻凭空多一份同基数(生产 ~171 万)的 str->str
        # 映射。`can2idx` 已经在手,按下标存只是一个指针数组。
        #
        # ⚠ 补账本那条路径上 `kept_rows` 读自库里现成的划分,它的成员可能**不在**本轮的
        # `can2idx` 里(合并把某个 canonical 的边全变成了自环,它就不再是图上的节点)。
        # 旧的来源画像 SQL 靠 join `community_members` 照样能给它定位,所以这里给它们
        # 补一个下标 —— 少了这一句,那些对象会被静默判成「没进任何板块」,与旧口径分岔。
        # 全量重建那一路 `kept_rows` 的成员全部来自图节点,这个循环一次也不会扩容。
        community_of_index: "List[Optional[str]]" = [None] * len(can2idx)
        for cid, members in kept_rows:
            for member in members:
                index = can2idx.get(member)
                if index is None:
                    index = len(can2idx)
                    can2idx[member] = index
                    community_of_index.append(None)
                community_of_index[index] = cid
        # ⚠ 这一步**释放** `edge_table`(= 调用方的 `edge_table`),不是遍历它:折叠
        # 出来的板块对数在最坏情况下与边表同量级(生产 836 万条),两份同时驻留就是在
        # 437 GB 库的峰值时刻再叠一个峰值。落库上限(`MAX_PERSISTED_COMMUNITY_EDGES`)
        # 管的是**写出去多少**,管不了算的时候占多少 —— 那是 codex 第 7 轮评审的 P1。
        # 释放必须由**持有者自己**做(`CanonicalEdgeTable.release`):`edge_table` 是
        # 调用方 `rebuild_communities` 传下来的实参,那个栈帧的局部名在本方法执行期间
        # 一直活着,本方法 `del` 自己的形参一个字节也放不掉。
        folder = fold_cross_community_edges(edge_table, community_of_index)
        head, head_members, total_members = head_community_ids(sizes)

        # 有界物化,唯一的一份:`top_edges` 的返回长度有硬上限,store 直接按批消费这个
        # 列表,不再拷第二份。
        edges = folder.top_edges(MAX_PERSISTED_COMMUNITY_EDGES)
        community_edges_payload = {
            "level": int(level),
            "edges": len(edges),                    # 落库行数
            "edges_total": folder.total_edges,      # 截断前的板块对总数
            "truncated": len(edges) < folder.total_edges,
            "edge_limit": MAX_PERSISTED_COMMUNITY_EDGES,
            # 图统计量:全部跨板块边权,**不**随落库上限变化。
            "cross_weight": folder.cross_weight,
            "intra_weight": folder.intra_weight,
            "communities": len(sizes),
        }
        # ⚠ **折叠结果到此为止,必须当场整个放掉**(codex 第 9 轮 P1-2)。它已经把要
        # 落库的东西交出来了(上面那个 payload + 有界的 `edges`),而紧接着的四条查询
        # (来源画像扫描 / 簇大小直方图 / 最大簇榜单 / 边出处构成)每一条都是全表重活,
        # 自己就要分配 O(分组数) 的临时结构 —— 生产 200 万簇行、836 万边。让折叠结果跨过
        # 它们,就是把两个峰值叠在同一时刻,而这四条根本不需要它。
        del folder
        payloads: "Dict[str, dict]" = {
            ARTIFACT_COMMUNITY_EDGES: community_edges_payload,
        }
        # 来源画像**排在三条统计快照之前**,不是随手排的:它是唯一还需要 membership
        # (`community_of_index` + `can2idx`)的一份。算完就把这两份一起放掉,三条统计
        # 快照因此跑在一个更低的水位上 —— 生产 ~171 万 canonical,`can2idx` 那个
        # str->int 的 dict 是几百 MB 量级。
        #
        # ⚠ 板块映射为什么在 Python 侧做:板块划分这一刻**还没落库**(原子发布,codex
        # 第 13 轮 P1),SQL 没有 `community_members` 可 join。`SourceBoardCounter` 拿
        # 内存里的 membership 做同一跳,结果与旧 SQL 逐字相同(同 level 下
        # `community_members` 是一个划分,而它就是那份 membership),而且每行只留两个
        # int32,库内反而少了一棵 GROUP BY 的临时 B 树。
        profiles: "List[tuple]" = []
        if total_members:
            counter = SourceBoardCounter(can2idx, community_of_index)
            with self._connect() as db:
                for row in self.unified_kg.source_canonical_rows(db, notebook_id):
                    counter.add(row["source_id"], row["canonical_id"])
            source_folder = SourceProfileFolder(head)
            for source_id, community_id, count in counter.drain():
                source_folder.add(source_id, community_id, count)
            del counter
            profiles = source_folder.rows()
            del source_folder
            payloads[ARTIFACT_SOURCE_PROFILES] = {
                "level": int(level),
                "sources": len(profiles),
                "mainstream_coverage": MAINSTREAM_COVERAGE,
                "head_communities": len(head),
                "head_members": head_members,
                "total_members": total_members,
            }
        # ⚠ membership 到此为止。`can2idx` 是**调用方**(`rebuild_communities`)的局部
        # 变量,它那个栈帧的名字在本方法执行期间一直活着,`del` 自己的形参一个字节也放
        # 不掉 —— 与 `CanonicalEdgeTable.release()` 是同一条契约,所以由持有者的内容被
        # 就地清空来兑现。调用方在本方法返回之后不得再用它(今天也没有任何使用点)。
        del community_of_index
        can2idx.clear()
        payloads[ARTIFACT_CLUSTER_HISTOGRAM] = self.unified_kg.cluster_size_histogram(
            notebook_id
        )
        payloads[ARTIFACT_LARGEST_CLUSTERS] = self.unified_kg.largest_clusters(
            notebook_id, PRECOMPUTED_LARGEST_CLUSTERS
        )
        # 与簇无关的那份:上一轮的账本行还在同一个 `seq` 上就直接搬过来,不重扫 836 万
        # 边(判据、不变式与「force 不复用」的理由见 `reusable_artifact_payloads`)。
        reused = (reusable or {}).get(ARTIFACT_RELATION_PROVENANCE)
        payloads[ARTIFACT_RELATION_PROVENANCE] = (
            reused if reused is not None
            else self.unified_kg.relation_provenance_counts(notebook_id)
        )
        return (
            edges,
            profiles,
            stamp_cluster_seq(
                payloads, cluster_seq, partition_rebuilt=partition_rebuilt
            ),
        )

    def list_communities(self, notebook_id: str, level: int = 0) -> List[List[str]]:
        """Member-id lists of each detected community (for summaries / global search)."""
        with self._connect() as db:
            return self.unified_kg.community_member_ids(db, notebook_id, level)

    @notebook_model_artifact_scope
    def summarize_communities(self, notebook_id: str, level: int = 0) -> int:
        """For each detected community, generate an LLM report (title/summary/
        findings) from its members + internal relations; persist on the community
        row. No-op (returns 0) when disabled or LLM unconfigured. Returns the
        number of communities summarized."""
        self.get_notebook(notebook_id)
        if (
            not self.settings.kg_community_summary_enabled
            or not self.model_clients.configured("kg_community_summary")
        ):
            return 0
        from app.services.prompts import community_report_prompt, COMMUNITY_REPORT_SCHEMA_HINT
        with self._connect() as db:
            crows = self.unified_kg.community_rows_for_summary(
                db, notebook_id, level)
        done = 0
        for cr in crows:
            members = json.loads(cr["member_ids"] or "[]")
            if not members:
                continue
            with self._connect() as db:
                orows, rrows = self.knowledge.community_context_rows(
                    db, notebook_id, members
                )
            name_by_id = {}
            mlines = []
            for o in orows:
                nm = json.loads(o["payload"] or "{}").get("name", "")
                name_by_id[o["id"]] = nm
                mlines.append(f"- [{o['object_type']}] {nm}")
            rlines = [f"{name_by_id.get(r['source_object_id'],'?')} -[{r['edge_type']}]-> {name_by_id.get(r['target_object_id'],'?')}"
                      for r in rrows]
            members_block = "\n".join(mlines)
            relations_block = "\n".join(rlines) if rlines else "(none)"
            try:
                raw = self.model_clients.chat("kg_community_summary").chat_json(
                    [{"role": "user", "content": community_report_prompt(members_block, relations_block)}],
                    COMMUNITY_REPORT_SCHEMA_HINT)
                data = json.loads(raw)
            except Exception:
                continue
            title = str(data.get("title", "")).strip()
            summary = str(data.get("summary", "")).strip()
            findings = data.get("findings") if isinstance(data.get("findings"), list) else []
            if not summary:
                continue
            with self._write() as db:
                self.unified_kg.set_community_summary(
                    db, cr["id"], title, summary, json.dumps(findings))
            done += 1
        return done

    def get_community_reports(self, notebook_id: str, level: int = 0) -> List[dict]:
        """Persisted community reports (only those summarized). For global search."""
        with self._connect() as db:
            return self.unified_kg.community_reports(db, notebook_id, level)

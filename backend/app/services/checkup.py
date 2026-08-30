"""流水线体检聚合 service(P2·T2)。

承 `docs/superpowers/specs/2026-07-22-pipeline-damage-recovery-design.md`「二·体检层」。
把已经存在的判据(store 查询 / maintenance 计数 / 索引状态)**只读地聚合**成 per-notebook
的 H2–H8 体检结果,供 T3 的 `GET /notebooks/{id}/checkup` 端点消费。

设计红线:
- **只读**:本 service 不写库、不调 LLM / embedding / rerank。体检就是聚合已有判据。
- **不产用户文案**:响应用内部代号(H2 / reparse 等枚举),面向用户的界面词由前端映射层
  (T5)负责。本文件刻意不出中文用户文案(界面词汇守卫的红线)。
- **不 import facade**:collaborators 由 RepositoryRuntime 以窄接口(callable seam)注入,
  本 service 只组装、不拼主业务库 SQL(H2/H3/H6 的 SQL 在 QueryStore,H4/H5 在 maintenance)。

代价模型(已在 master 核实,承 P1.5 可判定性核查):
- H2/H3:直查(索引覆盖)+ 内存活跃租约的 Python 后置减法(在途解析/reparse 的源瞬时缺
  elements/chunks 属正常、非损坏)。
- H4/H5:两条 anti-join COUNT,按 **(本库活跃租约快照, kg_mutation_seq)** 组合键做进程内
  单槽 memo,叠加**边界**事件失效(完整口径论证见 ``_h45_missing_vector_counts``):
  ①租约快照(经 ``QueryStore.notebook_source_ids_among`` 收窄到本 notebook)捕获在途源,
  别的库的上传不冲本库缓存;②kg_mutation_seq 捕获 chunk/element 集合的增删(build_chunks
  与 delete_source 都 bump 它,见 knowledge_counts_cache 的 choke 论证);③向量 embed 成功
  **不** bump seq,由**边界**事件补上——整源嵌入(``embed_source``)完成时与交互式补齐
  job 结束时各通知一次 ``invalidate_missing_vector_counts``;刻意不按页/按批通知(codex
  质量评审 P1:修复中的源被租约排除在计数外,页级失效只会逼出返回同一个数的重算);
  ④跨进程写(离线 CLI ``run_embed``/batch ingest)与个别登记过的边角(见 _h45)由背底
  TTL 兜住,至多陈旧 300 秒。新鲜度上,用户盯着看的交互式补齐从「至多晚 30s」变成
  「job 结束后下一轮轮询即归零」;代价上,稳态零重算(旧方案每 30s 一次),修复期只随
  租约/seq 真变化重算,外加每个边界事件恰好一次——那一次换掉的是旧方案整轮陈旧命中。
- H6:已 memo 在 kg_mutation_seq 上(QueryStore.visible_pending_kg_source_count——排除
  memory/knowhow 合成源,与 H2/H3 及看板「知识图谱」行同口径),O(1)。
- H7:读索引状态的 `state` 字段。按**廉价签名**(version_signal + manifest mtime + building/queued,
  见 ScaleArtifactRuntime.state_signature)memo:签名不变即复用,避免大库上每次 /checkup 都跑
  status()→_index_delta 的全量 source-id 扫(codex P2)。签名是 status() stale 判定输入的廉价超集,
  故缓存绝不比 status() 自身更陈旧;不折进裸 kg_mutation_seq(rebuild 不 bump seq 却换 watermark
  →由 mtime 补上;嵌入变更改 version_facts 也不 bump seq——与 status() 自身的 version() memo 同盲区)。
- H8:需真 load 一次磁盘产物做交叉校验,故按磁盘 **manifest 身份**(exists + manifest.version)缓存
  「健康」结论——rebuild/fold(`.tmp`+swap 原子)换新 manifest.version 即失效。**不用 version_signal**:
  它只由 unified_kg_state 的 seq 组成、rebuild/fold 不 bump 它,与磁盘产物解耦(评审 B1:用它当键会
  漏报「fold 后损坏」、且损坏被缓存后重建清不掉)。损坏结论从不入缓存、每次现探(修好即自愈)。
"""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

from app.services.kg import scale_index as _scale_index_module

# ⚠ 本 service 刻意**不 import** app.repositories.sqlite/postgres 的任何东西——它由**两个后端**的
# facade 各自懒构造(SQLiteRepository / PostgresRepository 的 checkup 属性),H2/H3/H6 的 QueryStore
# 从构造方注入(``queries`` seam,后端相关的实例),故 checkup 本身是后端中性的:同一套聚合逻辑
# 在 sqlite 与 postgres 上都跑,只是注入的 database/queries/count seam 落到各自后端的实现。

# 给前端展示够用即可,不返回全量避免大 payload(承计划「开放实现细节」:sample 上界 20)。
_SAMPLE_CAP = 20
# H8 version_signal 缓存的有界化:进程内只保留最近访问的 N 个 notebook(LRU)。看板逐个
# 打开,工作集本就是个位数~几十;上界只为防「几十万 notebook 全被探过一遍」的无界增长。
_H8_CACHE_MAX = 256
# H8 健康结论的缓存**有界存活**:manifest 身份不变时也最多缓存这么久,过期即重探一次。
# 理由(codex):manifest.version 是内容派生的、外部截断/损坏数组或 ann.bin 而不动 manifest
# 时身份不变——只靠身份键会把「探过之后才损坏」的索引永远当健康。TTL 让健康结论定期复检,
# 使 post-probe 磁盘损坏最终可见(损坏结论本就不缓存、每次现探)。300s:检索热路径外,足够低频。
_H8_CACHE_TTL = 300.0
# H7 memo 的有界化(同 H8:LRU、进程内、重启即空)。H7 不需 TTL:签名含 manifest mtime → 任何
# 产物重写都失效,不像 H8 的 (exists,version) 身份会漏「探后被外部截断」而需 TTL 兜底。
_H7_CACHE_MAX = 256
# H4/H5 memo 的有界化(同 H7/H8:LRU、进程内、重启即空)。epoch 表共用同一上限。
_H45_CACHE_MAX = 256
# H4/H5 memo 的**背底** TTL:兜「租约、seq、边界事件都看不见」的写——主要是跨进程写
# (离线 CLI 的 run_embed / batch ingest),外加 _h45 docstring 登记的少数进程内边角。
# 进程内的向量写与 chunk/element 增删走事件/版本失效(见
# _h45_missing_vector_counts),不等 TTL;用户点「补齐向量」盯着看的那两个数因此是事件级
# 新鲜的,不再受 TTL 束缚——这正是允许把 30s 放宽到与 H8 同档 300s 的前提。跨进程修复
# (管理员在服务器上跑 CLI)期间,看板计数至多陈旧这么多秒。
_H45_CACHE_TTL = 300.0


@dataclass(frozen=True)
class CheckupItem:
    """单个体检项的结果。``code`` 是内部代号(H2..H8),``fix`` 是修复动作枚举
    (reparse|backfill_vectors|extract_kg|fold_index|rebuild_index)——都是内部契约,
    面向用户的文案由前端映射。``sample`` 是有界的 source_id 样本(H4–H8 是计数型,sample
    留空;H2/H3 给前端展示命中的源)。"""

    code: str
    count: int
    sample: list[str]
    fix: str


@dataclass(frozen=True)
class CheckupResult:
    notebook_id: str
    checked_at: str
    healthy: bool
    checks: list[CheckupItem]


def probe_scale_index_integrity(scale_dir: Any, *, logger: Any = None) -> int:
    """H8 的磁盘探针:1=损坏(manifest 在、却加载不出来),0=正常或未建索引。**绝不 raise**。

    关键区分(承计划三·H8):`load_scale_index` 对「manifest 缺失(未建)」与「manifest 在但
    计数/长度校验失配(损坏)」**都返回 None**,故必须先判 manifest 文件是否存在:
    - manifest.json 不存在 → 未建索引 → 不是 H8(返回 0,连 load 都不调)。
    - manifest.json 存在但 load_scale_index 返回 None → 损坏 → 命中 H8(返回 1)。
    - load_scale_index 返回非 None → 正常(返回 0)。

    保守取舍:探针内部任何异常(磁盘 IO / 反序列化)都归结成**不误报损坏**(返回 0)——
    体检在看板打开的热路径上,响亮地误报「索引损坏、请重建」比漏报一次更打扰用户,且损坏本
    就会在下次真正 load 时被 `_unusable` 兜住退化成「无索引」走重建。经 `_scale_index_module`
    属性调用 load_scale_index,让冻结的磁盘缓存测试的 monkeypatch spy 仍能绑上。"""
    try:
        manifest_path = os.path.join(str(scale_dir), "manifest.json")
        if not os.path.exists(manifest_path):
            return 0
        index = _scale_index_module.load_scale_index(str(scale_dir))
        if index is None:
            return 1  # manifest 计数/数组长度失配
        # ⚠ load_scale_index 只校验 .npy 数组、**不校验 ANN 二进制**——ANN 索引懒加载
        # (延迟由检索侧用 ann_path 打开),故 ann.bin 缺失/损坏它照样返 ScaleIndex(codex)。
        # 主 ANN 有标签(n_ann>0)时:先判文件存在,再做**内容级校验**。chunk/relation ANN
        # 是可选产物(load_scale_index 本就容忍缺失),不在此升级成整份损坏。
        labels = getattr(index, "ann_labels", None)
        ann_path = getattr(index, "ann_path", "") or ""
        if labels:
            if not os.path.exists(ann_path):
                return 1  # 有标签却没落盘文件 → 检索时开不了 → 损坏
            # 内容级校验(codex 第2轮 P2):ann.bin 存在但被**截断/非法 HNSW** 时,上面的
            # load_scale_index 只看 .npy、ANN 懒加载,照样报健康——检索侧真 open_ann 才炸、退化成
            # 无索引却不提示重建。这里按 manifest 的 dim 真 load 一次主 ANN,截断/损坏会在 load_index
            # raise。成本由本探针的 300s 健康缓存摊薄(每源每 5min 至多一次);handle 是本地临时对象、
            # 探完即弃(不 memoize 到 index 上,避免额外常驻内存)。
            dim = int(index.manifest.get("dim", 0) or 0)
            if dim > 0:
                import hnswlib  # 既有依赖;万一 import 失败落外层 except → 保守判未损坏(不误报)
                try:
                    probe = hnswlib.Index(space="cosine", dim=dim)
                    probe.load_index(ann_path, max_elements=len(labels))
                    # 结构合法但**条目数 < labels**(如从早期/半截 build 拷来的 ann.bin)load 也成功,
                    # 但检索会静默漏掉没进 ANN 的 labeled 节点(codex 第5轮 P2)→ 判损坏走重建。
                    if probe.get_current_count() != len(labels):
                        return 1
                except Exception:  # noqa: BLE001 — load 失败=内容损坏 → 判 H8(不落外层保守 0)
                    return 1
        return 0
    except Exception:  # noqa: BLE001 — 探针绝不 raise 进体检热路径,保守判「未损坏」
        if logger is not None:
            try:
                logger.warning(
                    "H8 索引完整性探针失败(保守判未损坏):%s", scale_dir, exc_info=True
                )
            except Exception:
                pass
        return 0


class CheckupService:
    """per-notebook 的只读体检聚合器(H2–H8)。

    collaborators 全部由 RepositoryRuntime 注入为窄 callable seam,便于单测直接构造
    (无需给 facade 打桩):

    - ``database``:H2/H3/H6 的一个读快照(三条 QueryStore 查询共用同一 connection)。后端相关
      (sqlite/postgres 各自的 database),由构造方(facade)注入,本 service 只当它是「能 connect()
      出读快照的东西」,不依赖具体后端。
    - ``queries``:后端相关的 **QueryStore 实例**(H2/H3/H6 的 SQL 在它上面,外加 H4/H5 memo
      键用的 ``notebook_source_ids_among``——把进程全局的活跃租约快照收窄成本库子集;
      sqlite 与 postgres 各有一份判据逐字一致的实现)。注入而非 import,故 checkup 后端中性、
      两后端都能跑。
    - ``count_missing_chunk_vectors`` / ``count_missing_element_vectors``:H4/H5,resolve 到
      maintenance 的直连 COUNT(判据与「实际可补数」逐字一致)。收到的 exclude 仍是**原样的**
      全局租约快照(排除口径一字不动),收窄只作用在 memo 键上。
    - ``kg_mutation_seq``:H4/H5 memo 键的版本分量,``(db, notebook_id) -> int`` 的 O(1) 单行读
      (facade 注入 ``unified_kg.graph_seq_row(db, nb)[0]``;只取 kg_mutation_seq 一项——
      cluster/mention seq 与 chunk/element 集合无关,折进键只会白失效,同 collection_catalog
      的键论证)。搭在 run() 已开的读快照连接上,零新增连接。
    - ``scale_index_state``:H7,返回索引状态的 `state` 字符串('stale' 即过期/维度失配)——昂贵
      (内含 _index_delta 全量 source-id 扫),只在下面的签名变化时才调。
    - ``index_state_signature``:H7 memo 的**廉价**失效键(version_signal + manifest mtime +
      building/queued);签名不变即复用上次的 state 结论、不跑 ``scale_index_state``(codex P2)。
    - ``index_manifest_identity``:H8 的缓存键 `(exists, manifest.version)`——磁盘产物身份,
      rebuild/fold 换新 version 即失效(**不是** version_signal:后者与磁盘产物解耦,见 H8 说明)。
    - ``probe_index_integrity``:H8 的磁盘探针(never-raise,见模块级 probe_scale_index_integrity)。
    - ``active_source_ids``:内存活跃租约快照(H2/H3 的 Python 后置减法)。
    - ``now``:时钟 seam;``event_log``:仅用于 fail-soft 探针的 warning。
    """

    def __init__(
        self,
        *,
        database: Any,
        queries: Any,
        count_missing_chunk_vectors: Callable[[str, "set[str]"], int],
        count_missing_element_vectors: Callable[[str, "set[str]"], int],
        kg_mutation_seq: Callable[[Any, str], int],
        scale_index_state: Callable[[str], str],
        index_state_signature: Callable[[str], Any],
        index_manifest_identity: Callable[[str], "tuple[bool, Any]"],
        probe_index_integrity: Callable[[str], int],
        active_source_ids: Callable[[], "set[str]"],
        now: Callable[[], str],
        event_log: Any = None,
    ) -> None:
        self._database = database
        self._queries = queries
        self._count_missing_chunk_vectors = count_missing_chunk_vectors
        self._count_missing_element_vectors = count_missing_element_vectors
        self._kg_mutation_seq = kg_mutation_seq
        self._scale_index_state = scale_index_state
        self._index_state_signature = index_state_signature
        self._index_manifest_identity = index_manifest_identity
        self._probe_index_integrity = probe_index_integrity
        self._active_source_ids = active_source_ids
        self._now = now
        self._event_log = event_log
        # 进程内 H8 缓存:nb -> (manifest_version, 0)。**只缓存「健康」结论**(见 _h8 说明:
        # 损坏结论从不进缓存,每次现探,以免修复后仍粘住误报)。键是磁盘 manifest 身份,
        # rebuild/fold 换新 version 即失效。重启即空。move_to_end + popitem(last=False) LRU 有界化。
        self._h8_cache: "OrderedDict[str, tuple[Any, int, float]]" = OrderedDict()
        self._h8_cache_lock = threading.Lock()
        # 进程内 H7 memo:nb -> (signature, h7_value)。签名是 status() stale 判定输入的廉价超集,
        # 命中即跳过昂贵的 status()/_index_delta 全量扫(codex P2)。0/1 都缓存(两向翻转都被签名
        # 捕获,无粘滞);异常不缓存。move_to_end + popitem(last=False) LRU 有界化。重启即空。
        self._h7_cache: "OrderedDict[str, tuple[Any, int]]" = OrderedDict()
        self._h7_cache_lock = threading.Lock()
        # 进程内 H4/H5 memo:nb -> ((**本库**活跃租约快照, kg_mutation_seq), chunk 计数,
        # element 计数, 取数时刻)。键论证见 _h45_missing_vector_counts。**每个 notebook 只有
        # 一个槽**(不是「按键多槽」):单槽让键的任何变动都覆盖掉旧条目,补齐完成后不可能
        # 再命中修复前的那份计数。LRU 有界化,重启即空。
        self._h45_cache: "OrderedDict[str, tuple[tuple[frozenset, int], int, int, float]]" = OrderedDict()
        self._h45_cache_lock = threading.Lock()
        # H4/H5 的失效代次(镜像 postgres/knowledge_counts_cache 的 _EPOCHS/_GLOBAL_EPOCH,
        # 含 codex #621 R1 的 fail-closed 教训):invalidate_missing_vector_counts 在计算
        # **期间**到来时,光 pop 槽拦不住计算完的写回把失效前的快照钉回去——写回前必须
        # 重新核对 (全局代次, 本库代次) 二元组。per-notebook 代次让 nb-A 的几秒级冷查询
        # 不被 nb-B 的嵌入完成误伤;代次表有界、按**最近一次失效**排序淘汰(不是
        # LRU-by-use:读路径不 move_to_end,常读少失效的库照样可能被挤出——安全,因为
        # **淘汰一条就推进一次全局代次**,被淘汰库的在途写回从「误放行」翻成「被拒绝」,
        # 方向保守:多付一次冷查,绝不钉陈旧值)。
        self._h45_epochs: "OrderedDict[str, int]" = OrderedDict()
        self._h45_global_epoch = 0

    # ------------------------------------------------------------------ run
    def run(self, notebook_id: str) -> CheckupResult:
        """聚合 H2–H8,返回结构化结果。任一 check.count>0 即 ``healthy=False``。"""
        # 活跃租约快照取一次,H2/H3 共用(active 集通常个位数,一次集合减法)。租约的读法
        # 由注入方在锁下取快照,这里拿到的已是不可变副本。
        active = set(self._active_source_ids() or ())
        # H2/H3/H6 共用一个读快照:三条 QueryStore 查询都取 db 连接,合到一个 connection 里
        # 一次性算完(一个读快照,少开两次连接)。QueryStore 由构造方注入(后端相关实例),
        # 静态方法经实例调用会派发到对应后端的实现——故同一套聚合在 sqlite/postgres 都成立。
        with self._database.connect() as db:
            h2_hits = self._queries.sources_without_elements(db, notebook_id) - active
            h3_hits = self._queries.sources_missing_chunks(db, notebook_id) - active
            # ⚠ 用 visible_ 口径(排除 memory/knowhow 合成源),与 H2/H3 及看板「知识图谱」行
            # 对齐(评审:全集口径会把有 elements、却不走文档 KG 抽取的 knowhow 合成源算进
            # H6→与 KG 行「0 待分析」自相矛盾、healthy 恒 false 且点「分析新增」修不掉)。
            h6_count = int(self._queries.visible_pending_kg_source_count(db, notebook_id))
            # 活跃租约快照是**进程全局**的(source_ingestion._active_sources 跨所有 notebook
            # 共用一个 dict)。H4/H5 的 memo 键只能用**本库**的那一小撮,否则别的库上传一个
            # 文件就把每个库的缓存都冲掉——见 _h45_missing_vector_counts 的键论证。收窄本身
            # 就是一条按主键的有界查询(active 是个位数),搭在这个已开的读快照上,零新增连接。
            local_active = (
                self._queries.notebook_source_ids_among(db, notebook_id, active)
                if active else set()
            )
            # H4/H5 memo 键的版本分量:O(1) 单行读,搭同一读快照(零新增连接)。在计数
            # **之前**采样——seq 若在计数期间前进,存下的条目挂着旧 seq,下次必失配重算,
            # 方向保守(同 knowledge_counts_cache 的先读 seq 后计值)。
            h45_seq = int(self._kg_mutation_seq(db, notebook_id))
        # H4/H5 也减活跃租约(codex):正在嵌入的源 chunk/element 已在、向量还没落,是
        # 正常在途而非损坏——不排除会每次嵌入都误报缺向量、甚至触发并发 backfill 重复模型调用。
        h4_count, h5_count = self._h45_missing_vector_counts(
            notebook_id, active, local_active, h45_seq
        )
        checks = [
            CheckupItem("H2", len(h2_hits), _sample(h2_hits), "reparse"),
            CheckupItem("H3", len(h3_hits), _sample(h3_hits), "reparse"),
            CheckupItem("H4", h4_count, [], "backfill_vectors"),
            CheckupItem("H5", h5_count, [], "backfill_vectors"),
            CheckupItem("H6", h6_count, [], "extract_kg"),
            CheckupItem("H7", self._h7_index_stale(notebook_id), [], "fold_index"),
            CheckupItem("H8", self._h8_index_integrity(notebook_id), [], "rebuild_index"),
        ]
        healthy = all(item.count == 0 for item in checks)
        return CheckupResult(
            notebook_id=notebook_id,
            checked_at=self._now(),
            healthy=healthy,
            checks=checks,
        )

    # ------------------------------------------------------------- narrow reads
    def missing_vector_counts(self, notebook_id: str) -> "tuple[int, int]":
        """(缺 chunk 向量数, 缺 element 向量数)的**窄读口**——只算 H4/H5 这两个数,
        不顺路跑 H2/H3(各一次全库 sources anti-join)、H6、H7(索引状态签名/探针)、
        H8(磁盘 manifest 交叉校验)。承 Z7:``backfill_vectors`` 的受理判定只需要
        「有没有缺向量」,调整套 ``run()`` 会为一个布尔判断白付其余五项体检的代价
        (H2/H3 尤其是大库上的整表反连接)。

        底层复用 ``_h45_missing_vector_counts`` 的既有 memo/事件失效/背底 TTL 机制
        (与 ``run()`` 里 H4/H5 的那份是**同一套缓存、同一个键取法**,不是另起一套):
        active/local_active/kg_mutation_seq 三个键分量的取法与 ``run()`` 逐字一致,
        故两条路径算出的键永远相等,不会因为「走了不同入口」而各自命中/未命中出两份
        结果——数值同源、语义一致。为了不牵动 ``run()`` 里三条查询共用一个连接的既有
        结构(H2/H3/H6 也搭那个连接),这里为这三个分量单独开一次连接,是刻意的小重复,
        换来的是不必为窄读口专门拆一个「三选一开关」的 run()。"""
        active = set(self._active_source_ids() or ())
        with self._database.connect() as db:
            local_active = (
                self._queries.notebook_source_ids_among(db, notebook_id, active)
                if active else set()
            )
            h45_seq = int(self._kg_mutation_seq(db, notebook_id))
        return self._h45_missing_vector_counts(notebook_id, active, local_active, h45_seq)

    # ---------------------------------------------------------------- H4/H5
    def invalidate_missing_vector_counts(self, notebook_id: str) -> None:
        """H4/H5 memo 的**边界**事件失效:element/chunk 向量写路径在自己的边界(整源
        ``embed_source`` 完成、交互式补齐 job 结束)各通知一次——facade 构造期把经
        ``__dict__`` 晚解析的转发器挂上 ``RepositoryRuntime.on_source_vectors_written``,
        写路径经它调到这里。为什么必须有它:向量 embed 成功不 bump ``kg_mutation_seq``,
        而「补齐 job 完全落在两次轮询之间」时租约快照又会回到与修复前相同的值——键的两个
        分量都不动,没有显式失效的话,修复前的计数会被原样再端出来直到背底 TTL(用户盯着
        的数冻住,比陈旧更糟)。为什么只在边界、不按页/批(codex 质量评审 P1):修复中的
        源被租约排除在计数外,页级失效只会把每轮轮询逼成一次注定同值的全表 anti-join。

        pop 槽之外还要**推进本库失效代次**:光 pop 拦不住「计算已在途、失效后才写回」把
        失效前的快照钉回去(_h45_missing_vector_counts 写回前核对代次)。代次表有界 LRU,
        淘汰即推进全局代次(fail-closed,见 __init__ 的代次说明)。"""
        with self._h45_cache_lock:
            self._h45_cache.pop(notebook_id, None)
            self._h45_epochs[notebook_id] = self._h45_epochs.get(notebook_id, 0) + 1
            self._h45_epochs.move_to_end(notebook_id)
            while len(self._h45_epochs) > _H45_CACHE_MAX:
                self._h45_epochs.popitem(last=False)
                self._h45_global_epoch += 1

    def _h45_missing_vector_counts(
        self,
        notebook_id: str,
        active: "set[str]",
        local_active: "set[str]",
        seq: int,
    ) -> "tuple[int, int]":
        """(缺 chunk 向量数, 缺 element 向量数),按 (**本库**活跃租约快照, kg_mutation_seq)
        组合键 memo + 显式事件失效 + 背底 TTL。

        **为什么需要 memo**:这两条都是全表 anti-join(element 侧还要对每行做 TRIM/btrim
        非空判定,PG 上会强制读 TOAST)。看板打开就查一次,用户点「补齐向量」后前端进入
        ~8s 轮询,大库上等于反复付整表扫描的钱——而这是**诊断面**,不是检索热路径。

        **键分量一:local_active,不是 active**(评审 P1):``active`` 是**进程全局**的租约
        快照(``source_ingestion._active_sources ∪ _embedding_sources`` 跨所有 notebook 共用)。
        拿它当键会让别的库上传一个文件就冲掉每个库的缓存;收窄成本库子集
        (``QueryStore.notebook_source_ids_among``,一条按主键的有界查询)之后,别库活动
        不再冲本库缓存;本库真有源在途时键仍然跟着变,那是**对的**——那一刻计数确实在变。
        写路径审计(本次改动前提):进程内所有 element/chunk 向量写与 chunk/element 建构
        都发生在 ``_active_sources``(process_source 生命周期 / backfill job 的
        hold_source_chunk_lock)或 ``_embedding_sources``(后台嵌入 worker,spawn 前登记、
        finally 释放)覆盖之下,两个字典的并集在交接期无空窗——故「在途中」的每次轮询
        键必变、必现算。

        **键分量二:kg_mutation_seq**:它语义上是「任何会让检索/聚类输入失效的变更」的
        总闸(migrations 注:EVERY KG write 经 _mark_unified_kg_dirty),对本缓存**过宽也
        过窄**,两个方向都是登记过的取舍:①过宽——KG 抽取/边评审/对象改状态也 bump、
        与向量计数无关,白失效一次只多付一次冷查,且多发生在源本就持租约(键已在变)的
        时段;②过窄——element 落库本身不经它,「解析成功、分块在 bump 前抛错」的半途源
        要等背底 TTL 才见 H5(≤300s,罕见失败路径)。要它的理由:build_chunks 与
        delete_source(FK 级联)都 bump——「删掉一个没向量的源」不经 embedding 写路径、
        也不持租约,没有 seq 分量就只剩背底 TTL。只取 kg_mutation_seq、不折 cluster/
        mention seq(与 chunk/element 集合无关,折进来纯白失效)。⚠ seq 不是严格单调:
        delete_notebook_kg 掉 state 行会让它归零重爬,理论上可撞回旧值命中陈旧条目——
        单槽(重爬期间任何一次重算即覆盖)+ 背底 TTL 把它兜在 ≤300s。

        **边界事件失效**:embed 成功不 bump seq(设计如此,rebuild 幂等性依赖 seq 稳定),
        故由写路径在**边界**通知 ``invalidate_missing_vector_counts``——整源嵌入
        (embed_source)完成一次、交互式补齐 job 结束一次;正是「job 整个落在两次轮询
        之间、租约回到原值」那扇键失效捕获不到、旧方案靠 30s TTL 硬兜的窗。不按页/批
        通知的理由见 invalidate 的 docstring(codex 质量评审 P1)。其余进程内写路径不需
        通知:ingestion/reparse 被 build_chunks 的 seq bump + 租约覆盖;knowhow 投影
        (含 embed_chunk_ids 与 carry-over 直写)由投影**正常结束**的
        mark_unified_dirty_in_tx 覆盖(codex #638 R5 起随 KO 发布事务提交,比原来的
        提交后调用更早、更不可能漏)——投影在 bump 前早退(目标表已删的 target_exists
        短路、KO 发布事务抛错并连 bump 一起回滚)时已提交的行落入背底 TTL,见下。
        写回前核对 (全局, 本库) 失效代次二元组,防止失效期间已在途的计算把失效前的快照
        钉回去(镜像 postgres/knowledge_counts_cache 的 epoch 守卫)。

        **背底 TTL(``_H45_CACHE_TTL``,300s)**:键与事件都只对本进程可见;跨进程写
        (离线 CLI ``run_embed`` / batch ingest)、上面登记的 seq 覆盖不到的边角(半途
        解析、knowhow 投影在 mark_unified_dirty_in_tx 前早退/抛错、knowhow transfer 的重投影
        调度被吞、seq 归零重爬撞值),以及任何未来漏挂通知的路径,都由 TTL 兜底,计数
        至多陈旧 300 秒。交互式「补齐向量」是进程内路径,
        忙碌位解除跟随事件级新鲜的计数,不再多按住一个 TTL——docs/product-and-api*.md
        的口径已同步。

        **口径不变**(红线):排除活跃租约的语义**逐字保留**——计数 seam
        ``count_missing_*_vectors(nb, exclude)`` 收到的仍是原样的 ``active``,一个字没动。
        用 ``local_active`` 当键是安全的,因为两条计数查询本就限定在本 notebook 内,
        exclude 里那些**别库**的 source id 对结果零影响:同一个 ``local_active`` 对应的两次
        计数必然相等,与全局快照里还有谁无关。

        **单槽**(每个 notebook 只留最近一条,不是「按键分多槽」):多槽会保留「修复前」
        那条,键转回原值时正好命中它;单槽让中间任何一次重算都把它覆盖掉。

        异常不缓存:任一计数抛错就整体上抛(与改动前一致),缓存里不留半份结果。"""
        key = (frozenset(local_active), int(seq))
        now = time.monotonic()
        with self._h45_cache_lock:
            cached = self._h45_cache.get(notebook_id)
            if (
                cached is not None
                and cached[0] == key
                and (now - cached[3]) < _H45_CACHE_TTL
            ):
                self._h45_cache.move_to_end(notebook_id)
                return cached[1], cached[2]
            epoch = (
                self._h45_global_epoch,
                self._h45_epochs.get(notebook_id, 0),
            )
        # 查询放在锁外(整表 anti-join 可能很慢,不该把别的 notebook 的体检堵住)。
        chunk_count = int(self._count_missing_chunk_vectors(notebook_id, active))
        elem_count = int(self._count_missing_element_vectors(notebook_id, active))
        with self._h45_cache_lock:
            fresh_epoch = (
                self._h45_global_epoch,
                self._h45_epochs.get(notebook_id, 0),
            )
            if epoch == fresh_epoch:  # 计算期间没被失效才写回(否则丢弃,下次现算)
                self._h45_cache[notebook_id] = (key, chunk_count, elem_count, now)
                self._h45_cache.move_to_end(notebook_id)
                while len(self._h45_cache) > _H45_CACHE_MAX:
                    self._h45_cache.popitem(last=False)
        return chunk_count, elem_count

    # ------------------------------------------------------------ H7 / H8
    def _h7_index_stale(self, notebook_id: str) -> int:
        """索引过期/维度失配 → 1,否则 0。

        memo(codex P2:大库上每次 /checkup 都跑 status()→_index_delta 全量扫 source-id 太贵):
        先取**廉价签名**,签名与上次相同即复用上次结论、**不**跑昂贵的 ``scale_index_state``。签名是
        status() stale 判定所有输入的廉价超集(见 ScaleArtifactRuntime.state_signature),故缓存绝不
        比 status() 自身更陈旧;H7 两向翻转(新数据 bump seq、rebuild/fold 换 manifest mtime)都被
        签名捕获,故 0/1 都可缓存、无粘滞。

        fail-soft:签名或状态探针任一异常,都保守判「未过期」(0)且**不写缓存**——索引状态有额外
        失败面(delta 计算 / 磁盘),单点失败不该拖垮 H2/H3 这些用户最需要的源级项;不缓存异常结论
        以免把偶发失败粘成长期误判(下次现探)。"""
        try:
            signature = self._index_state_signature(notebook_id)
        except Exception:  # noqa: BLE001 — 连廉价签名都取不到:保守判未过期,不缓存
            self._warn("H7 索引签名取不到(保守判未过期):%s", notebook_id)
            return 0
        with self._h7_cache_lock:
            cached = self._h7_cache.get(notebook_id)
            if cached is not None and cached[0] == signature:
                self._h7_cache.move_to_end(notebook_id)
                return cached[1]
        try:
            value = 1 if self._scale_index_state(notebook_id) == "stale" else 0
        except Exception:  # noqa: BLE001 — 状态探针 fail-soft:保守判未过期,不缓存
            self._warn("H7 索引状态探针失败(保守判未过期):%s", notebook_id)
            return 0
        with self._h7_cache_lock:
            self._h7_cache[notebook_id] = (signature, value)
            self._h7_cache.move_to_end(notebook_id)
            while len(self._h7_cache) > _H7_CACHE_MAX:
                self._h7_cache.popitem(last=False)
        return value

    def _h8_index_integrity(self, notebook_id: str) -> int:
        """索引产物损坏 → 1,否则 0。

        缓存策略(承评审 B1:version_signal 键不成立——它与磁盘产物解耦,损坏被缓存后重建
        清不掉):
        - 键 = 磁盘 **manifest 身份** `(exists, manifest.version)`,rebuild/fold 换新 version 即失效。
        - **只缓存「健康」(0)结论**;损坏(1)**从不入缓存、每次现探**。理由:full/fold 都是
          `.tmp`+swap 原子换目录,故「健康→损坏」只可能来自外部篡改(越界);而「损坏→健康」是
          用户点重建的正常闭环——若把损坏也缓存,重建后(即便换了新 version)也要等缓存失效才自愈,
          不如损坏罕见就每次现探,修好立刻现探为健康。
        - manifest 不存在 → 未建索引 → 直接 0、不 load、不缓存(廉价短路)。"""
        try:
            exists, manifest_version = self._index_manifest_identity(notebook_id)
        except Exception:  # noqa: BLE001 — 连 manifest 身份都取不到就无从判定,保守判未损坏
            self._warn("H8 manifest 身份取不到(保守判未损坏):%s", notebook_id)
            return 0
        if not exists:
            return 0  # 未建索引:不是损坏,廉价短路(不 load、不缓存)
        with self._h8_cache_lock:
            cached = self._h8_cache.get(notebook_id)
            if (
                cached is not None
                and cached[0] == manifest_version
                and (time.monotonic() - cached[2]) < _H8_CACHE_TTL
            ):
                self._h8_cache.move_to_end(notebook_id)
                return cached[1]  # 本代产物、TTL 内已探过健康(缓存只存 0)
        # 磁盘探针放在锁外(IO 可能慢);probe 契约上 never-raise,仍兜一层。
        try:
            result = int(self._probe_index_integrity(notebook_id))
        except Exception:  # noqa: BLE001
            self._warn("H8 磁盘探针异常(保守判未损坏,不写缓存):%s", notebook_id)
            return 0
        with self._h8_cache_lock:
            if result == 0:
                self._h8_cache[notebook_id] = (manifest_version, 0, time.monotonic())
                self._h8_cache.move_to_end(notebook_id)
                while len(self._h8_cache) > _H8_CACHE_MAX:
                    self._h8_cache.popitem(last=False)
            else:
                # 损坏结论不缓存,反而清掉本 nb 任何旧的健康缓存——下次仍现探,修好即自愈。
                self._h8_cache.pop(notebook_id, None)
        return result

    # --------------------------------------------------------------- utils
    def _warn(self, msg: str, *args: Any) -> None:
        if self._event_log is not None:
            try:
                self._event_log.logger.warning(msg, *args)
            except Exception:
                pass


def _sample(source_ids: "set[str]") -> list[str]:
    """有界、稳定的样本:排序后取前 N(集合迭代序不稳定,排序让样本对同一命中集确定,
    也让测试可断言)。"""
    return sorted(source_ids)[:_SAMPLE_CAP]

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
- H4/H5:两条 anti-join COUNT,按 **(本库活跃租约快照, ≤30s TTL)** 做进程内单槽 memo(见
  ``_h45_missing_vector_counts`` 的完整口径论证)。租约快照先经
  ``QueryStore.notebook_source_ids_among`` 收窄到本 notebook,别的库的上传不再冲本库缓存。
  **不**能折进 kg_mutation_seq——向量 embed 成功路径不 bump 它,按 seq 记忆化会一直报旧值
  (修复完了计数也不降,比陈旧更糟);故只能退化为 TTL,计数至多陈旧 30 秒。
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
# H4/H5 memo 的有界化(同 H7/H8:LRU、进程内、重启即空)。
_H45_CACHE_MAX = 256
# H4/H5 memo 的存活上限:**计数至多陈旧这么多秒**。体检是诊断面(不在检索热路径上),30s
# 可接受;取值远小于 H8 的 300s,因为向量计数是用户点「补齐向量」后**盯着看**的那两个数。
# 完整口径(为什么只能用 TTL、忙碌位因此可能多按住一个 TTL)见 _h45_missing_vector_counts。
_H45_CACHE_TTL = 30.0


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
        # 进程内 H4/H5 memo:nb -> (**本库**活跃租约快照, chunk 计数, element 计数, 取数时刻)。
        # 键是收窄到本 notebook 的租约子集(不是进程全局快照)——见
        # _h45_missing_vector_counts 的键论证。**每个 notebook 只有一个槽**(不是「按租约
        # 快照多槽」):单槽让本库租约的任何变动都覆盖掉旧条目,补齐完成后不可能再命中
        # 修复前的那份计数。LRU 有界化,重启即空。
        self._h45_cache: "OrderedDict[str, tuple[frozenset, int, int, float]]" = OrderedDict()
        self._h45_cache_lock = threading.Lock()

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
        # H4/H5 也减活跃租约(codex):正在嵌入的源 chunk/element 已在、向量还没落,是
        # 正常在途而非损坏——不排除会每次嵌入都误报缺向量、甚至触发并发 backfill 重复模型调用。
        h4_count, h5_count = self._h45_missing_vector_counts(notebook_id, active, local_active)
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

    # ---------------------------------------------------------------- H4/H5
    def _h45_missing_vector_counts(
        self, notebook_id: str, active: "set[str]", local_active: "set[str]"
    ) -> "tuple[int, int]":
        """(缺 chunk 向量数, 缺 element 向量数),按 (**本库**活跃租约快照, TTL) memo。

        **为什么需要 memo**:这两条都是全表 anti-join(element 侧还要对每行做 TRIM/btrim
        非空判定,PG 上会强制读 TOAST)。看板打开就查一次,用户点「补齐向量」后前端进入
        ~8s 轮询,大库上等于反复付整表扫描的钱——而这是**诊断面**,不是检索热路径。

        **键是 local_active,不是 active**(评审 P1):``active`` 是**进程全局**的租约快照
        (``source_ingestion._active_sources`` 跨所有 notebook 共用一个 dict)。拿它当键有两
        个后果:①**别的库**上传/解析一个文件就让每个库的缓存整片失配,与本库状态无关;
        ②本库补齐 job 逐源推进时全局快照每轮都在动,缓存命中率≈0——正好在最需要它的那段
        时间失效。收窄成本库子集(``QueryStore.notebook_source_ids_among``,一条按主键的有界
        查询)之后,别库活动不再冲本库缓存;本库真有源在途时键仍然跟着变,那是**对的**——
        那一刻计数确实在变。

        **口径不变**(红线):排除活跃租约的语义**逐字保留**——计数 seam
        ``count_missing_*_vectors(nb, exclude)`` 收到的仍是原样的 ``active``,一个字没动。
        用 ``local_active`` 当键是安全的,因为两条计数查询本就限定在本 notebook 内,
        exclude 里那些**别库**的 source id 对结果零影响:同一个 ``local_active`` 对应的两次
        计数必然相等,与全局快照里还有谁无关。

        **缓存至多陈旧 ``_H45_CACHE_TTL`` 秒**(30s),这是登记接受的口径。不能折进单调
        失效键:向量写入路径不 bump ``kg_mutation_seq``(见模块 docstring),
        ``sources.updated_at`` 只被 element 换代推进、embedding 成功不推进——拿它们当键会让
        补齐完成后计数**永远不降**,比陈旧更糟。库里没有第三个「向量写入即前进」的廉价单调
        信号,且本次不许加迁移,故退化为 TTL。于是:本库租约不变、又没到 TTL 时,端出的是
        上一次算的那两个数;别的进程(离线 CLI)或本进程的嵌入 worker 在这段窗口里补上的
        向量,最多晚 30 秒才在体检上体现。前端「补齐中…」的忙碌位因此可能比计数真正归零多
        按住至多一个 TTL——已在 docs/product-and-api*.md 的长任务按钮条登记。

        **单槽**(每个 notebook 只留最近一条,不是「按 active 分多槽」):多槽会保留「修复
        前 local_active=∅」那条,job 结束、租约回到 ∅ 时正好命中它,把修复前的数在 TTL 内
        再端一遍;单槽让中间那次(job 持源锁)把它覆盖掉,job 结束后必然重算。

        异常不缓存:任一计数抛错就整体上抛(与改动前一致),缓存里不留半份结果。"""
        key = frozenset(local_active)
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
        # 查询放在锁外(整表 anti-join 可能很慢,不该把别的 notebook 的体检堵住)。
        chunk_count = int(self._count_missing_chunk_vectors(notebook_id, active))
        elem_count = int(self._count_missing_element_vectors(notebook_id, active))
        with self._h45_cache_lock:
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

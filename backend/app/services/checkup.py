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
- H4/H5:直连 COUNT——向量 embed 成功路径**不 bump kg_mutation_seq**,折进 seq-memo 会一直
  报旧值,故每次直读(per-nb 索引查询,可接受)。
- H6:已 memo 在 kg_mutation_seq 上(QueryStore.visible_pending_kg_source_count——排除
  memory/knowhow 合成源,与 H2/H3 及看板「知识图谱」行同口径),O(1)。
- H7:读索引状态的 `state` 字段;不折进 kg_mutation_seq(嵌入变更改 version_facts 却不 bump
  seq,seq-memo 会漏报),故每次直读一次索引状态(与看板打开同代价)。
- H8:需真 load 一次磁盘产物做交叉校验,故按磁盘 **manifest 身份**(exists + manifest.version)缓存
  「健康」结论——rebuild/fold(`.tmp`+swap 原子)换新 manifest.version 即失效。**不用 version_signal**:
  它只由 unified_kg_state 的 seq 组成、rebuild/fold 不 bump 它,与磁盘产物解耦(评审 B1:用它当键会
  漏报「fold 后损坏」、且损坏被缓存后重建清不掉)。损坏结论从不入缓存、每次现探(修好即自愈)。
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.query_store import QueryStore
from app.services.kg import scale_index as _scale_index_module

# 给前端展示够用即可,不返回全量避免大 payload(承计划「开放实现细节」:sample 上界 20)。
_SAMPLE_CAP = 20
# H8 version_signal 缓存的有界化:进程内只保留最近访问的 N 个 notebook(LRU)。看板逐个
# 打开,工作集本就是个位数~几十;上界只为防「几十万 notebook 全被探过一遍」的无界增长。
_H8_CACHE_MAX = 256


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
        return 0 if index is not None else 1
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

    - ``database``:H2/H3/H6 的一个读快照(三条 QueryStore 静态查询共用同一 connection)。
    - ``count_missing_chunk_vectors`` / ``count_missing_element_vectors``:H4/H5,resolve 到
      maintenance 的直连 COUNT(判据与「实际可补数」逐字一致)。
    - ``scale_index_state``:H7,返回索引状态的 `state` 字符串('stale' 即过期/维度失配)。
    - ``index_manifest_identity``:H8 的缓存键 `(exists, manifest.version)`——磁盘产物身份,
      rebuild/fold 换新 version 即失效(**不是** version_signal:后者与磁盘产物解耦,见 H8 说明)。
    - ``probe_index_integrity``:H8 的磁盘探针(never-raise,见模块级 probe_scale_index_integrity)。
    - ``active_source_ids``:内存活跃租约快照(H2/H3 的 Python 后置减法)。
    - ``now``:时钟 seam;``event_log``:仅用于 fail-soft 探针的 warning。
    """

    def __init__(
        self,
        *,
        database: SqliteDatabase,
        count_missing_chunk_vectors: Callable[[str], int],
        count_missing_element_vectors: Callable[[str], int],
        scale_index_state: Callable[[str], str],
        index_manifest_identity: Callable[[str], "tuple[bool, Any]"],
        probe_index_integrity: Callable[[str], int],
        active_source_ids: Callable[[], "set[str]"],
        now: Callable[[], str],
        event_log: Any = None,
    ) -> None:
        self._database = database
        self._count_missing_chunk_vectors = count_missing_chunk_vectors
        self._count_missing_element_vectors = count_missing_element_vectors
        self._scale_index_state = scale_index_state
        self._index_manifest_identity = index_manifest_identity
        self._probe_index_integrity = probe_index_integrity
        self._active_source_ids = active_source_ids
        self._now = now
        self._event_log = event_log
        # 进程内 H8 缓存:nb -> (manifest_version, 0)。**只缓存「健康」结论**(见 _h8 说明:
        # 损坏结论从不进缓存,每次现探,以免修复后仍粘住误报)。键是磁盘 manifest 身份,
        # rebuild/fold 换新 version 即失效。重启即空。move_to_end + popitem(last=False) LRU 有界化。
        self._h8_cache: "OrderedDict[str, tuple[Any, int]]" = OrderedDict()
        self._h8_cache_lock = threading.Lock()

    # ------------------------------------------------------------------ run
    def run(self, notebook_id: str) -> CheckupResult:
        """聚合 H2–H8,返回结构化结果。任一 check.count>0 即 ``healthy=False``。"""
        # 活跃租约快照取一次,H2/H3 共用(active 集通常个位数,一次集合减法)。租约的读法
        # 由注入方在锁下取快照,这里拿到的已是不可变副本。
        active = set(self._active_source_ids() or ())
        # H2/H3/H6 共用一个读快照:三条 QueryStore 静态查询都取 db 连接,合到一个
        # connection 里一次性算完(一个读快照,少开两次连接)。
        with self._database.connect() as db:
            h2_hits = QueryStore.sources_without_elements(db, notebook_id) - active
            h3_hits = QueryStore.sources_missing_chunks(db, notebook_id) - active
            # ⚠ 用 visible_ 口径(排除 memory/knowhow 合成源),与 H2/H3 及看板「知识图谱」行
            # 对齐(评审:全集口径会把有 elements、却不走文档 KG 抽取的 knowhow 合成源算进
            # H6→与 KG 行「0 待分析」自相矛盾、healthy 恒 false 且点「分析新增」修不掉)。
            h6_count = int(QueryStore.visible_pending_kg_source_count(db, notebook_id))
        checks = [
            CheckupItem("H2", len(h2_hits), _sample(h2_hits), "reparse"),
            CheckupItem("H3", len(h3_hits), _sample(h3_hits), "reparse"),
            CheckupItem(
                "H4",
                int(self._count_missing_chunk_vectors(notebook_id)),
                [],
                "backfill_vectors",
            ),
            CheckupItem(
                "H5",
                int(self._count_missing_element_vectors(notebook_id)),
                [],
                "backfill_vectors",
            ),
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

    # ------------------------------------------------------------ H7 / H8
    def _h7_index_stale(self, notebook_id: str) -> int:
        """索引过期/维度失配 → 1,否则 0。fail-soft:索引状态探针有额外失败面(delta 计算 /
        磁盘),单点失败不该拖垮 H2/H3 这些用户最需要的源级项,异常时保守判「未过期」(0)。"""
        try:
            return 1 if self._scale_index_state(notebook_id) == "stale" else 0
        except Exception:  # noqa: BLE001 — 探针 fail-soft,保守判未过期
            self._warn("H7 索引状态探针失败(保守判未过期):%s", notebook_id)
            return 0

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
            if cached is not None and cached[0] == manifest_version:
                self._h8_cache.move_to_end(notebook_id)
                return cached[1]  # 本代产物已探过健康(缓存只存 0)
        # 磁盘探针放在锁外(IO 可能慢);probe 契约上 never-raise,仍兜一层。
        try:
            result = int(self._probe_index_integrity(notebook_id))
        except Exception:  # noqa: BLE001
            self._warn("H8 磁盘探针异常(保守判未损坏,不写缓存):%s", notebook_id)
            return 0
        with self._h8_cache_lock:
            if result == 0:
                self._h8_cache[notebook_id] = (manifest_version, 0)
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

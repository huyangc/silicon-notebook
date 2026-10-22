#!/usr/bin/env python3
"""图谱分析「问题清单」的生产只读演练(设计 2026-10-19 §14③)。

用途:问题清单的阈值(F1/F2/F3 的数)只在一个 84 个来源的本机库上标定过,规格 §14③
裁决「先按本机初值实现,生产分布回来再定」。本脚本在**一个** PostgreSQL 笔记本上只读地
跑一遍**产品同一套规则引擎**(`app.services.kg_findings`),并把定阈值要看的分布量出来:

  · F1 的 shared_share / giant_share 分布,以及阈值敏感度(shared < 2% / 5% / 10% ×
    概念下限 5 / 10 / 20);
  · F2 的候选对数(去重后与生成数)、J 的分布(各阈值以上的对数,含低于产品阈值的一段);
  · 每个概念出现在几个来源里的分布(df 分位与 > 1/10/30/100/300/1000 的计数,用来定
    「通用概念」的界);
  · F3 各子信号的分布与命中数(新下限:关联/对象比只判 ≥ F3B_MIN_OBJECTS 的来源,孤立
    占比阈值 max(3 × 中位数, F3B_ISOLATED_SHARE_FLOOR));可选 `--with-chunks` 量每段对象数;
  · F4 窄/宽两半各多少组(含扣除已存决定前后);F5 的数量与 relink 历史;
  · 可选 `--s1-impact`:S1 之后种子空间的变化(拆开/合并多少旧种子组、「只合不拆」变体的
    风险合并数、已存的驳回/暂缓决定按 v2 撞种子的对数、找不到当前对象的决定数);
  · 每个阶段的耗时与 RSS(`phases`,标出哪些阶段是产品成本、哪些只是演练自己的测量);
  · 可选 `--partition-bench`:Louvain / Leiden 分区基准(CPU 分钟级,约 1-2 GB RSS)。

**同口径**:读流的 SQL 复用产品存储层的片段(`community_graph_rows` /
`source_canonical_rows` / `decided_seed_pairs_from` 的同一批谓词、同一个行序
`ORDER BY kr.id COLLATE "C"`,测试里有漂移守卫);规则与分布一律调引擎的纯函数
(`GraphStructure.from_edges` / `FindingsObjectTally` / `compute_findings` 与内部的
`_drain_tally` / `_duplicate_pairs` / `_shared_shares` / `_parse_damage` / `_names_pass` /
`_squash_collisions` / `_merge_specs`),种子归一调 `concept_seed_v1/v2`,不重实现。
引擎内部函数改名或改签名时本脚本会响亮失败(测试 `test_rehearsal_calls_the_engine_...`
钉住),不会悄悄量出另一个口径。

**只读保证**:每条连接都用 `psycopg.connect(dsn, options="-c default_transaction_read_only=on
...")` 打开并置 `conn.read_only = True`,服务端拒绝任何写;脚本只发 SELECT(测试逐条
断言);大结果集一律 `cursor().stream()`,不整体缓冲;每个阶段后 `commit()` 释放快照。
只支持 PostgreSQL(SQLite 值直接拒绝)。连接附带 `statement_timeout`(默认 1200 秒,
`--statement-timeout-seconds 0` 不限):流式整表读的计时覆盖整个消费期,服务端默认的
30 秒会把它掐掉(产品侧用 `raise_statement_timeout_floor` 抬到 600 秒,理由同);上限也
不取更大,流式读期间一直持着快照、挡着 vacuum。

**成本**(错峰运行):一次 `knowledge_relations` 全库流(带 `ORDER BY`,服务端先排序)、
一次 `knowledge_objects` 流、一次 `concept_clusters` 名字流,各是该笔记本的整表扫描,
冷 IO 为主;随后是引擎本身的 CPU(生产规模按规格 §7.2 推算,这正是要实测的)与有界的点查
轮。F2 的 J 分布默认再多跑一遍把阈值降到 `--j-floor`(0.30)的连接,CPU 约为产品那遍的
1-2 倍;`--j-floor 0.45` 跳过。`--with-chunks` 多一次 `chunks` 的 GROUP BY,`--s1-impact`
多一次概念对象名流,`--partition-bench` 在进程内跑 Louvain/Leiden 多遍。
内存:引擎消费 tally 是一次性的,为了在产品跑完后还能量分布,脚本在 `compute_findings`
之前给 tally 做一份快照(≈ 每个概念对象 8 字节,不拷 `can2idx`),`phases` 里
`4b_snapshot_for_distributions` 之后的 RSS 含这份快照,读产品内存成本请扣除它
(`notes` 里写明)。

**隐私**:默认输出里**没有任何标题、概念名、来源名**——只有数字、规则码与分布。
`--show-samples` 才会输出样例(来源标题、概念名、问题明细原文),那是**用户数据**,
输出文件按 0600 创建,别外发。

用法(从仓库根运行,带后端解释器):

    PYTHONPATH=backend python3 scripts/kg_findings_rehearsal.py \\
        --dsn "$DSN" --notebook nb-xxxx [--with-chunks] [--s1-impact] \\
        [--partition-bench] [--show-samples] [--out result.json]

`--dsn` 缺省时读 `--dsn-env`(默认 `DATABASE_URL`)指的环境变量;DSN 不会被打印。
输出是一份 JSON:`graph` / `objects` / `concepts` / `f1`..`f5` / `engine`(产品那遍的各规则
状态与逐条数字证据)/ `s1_impact` / `partition_bench` / `samples`(仅 `--show-samples`)
/ `phases` / `notes`。
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import json
import os
import resource
import sys
import time
from array import array
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

import numpy as np
import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
_BACKEND = str(ROOT / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.core.database_url import database_identity  # noqa: E402
from app.domain.kg_findings_contracts import (  # noqa: E402
    DECISION_APART_STATUSES,
    DUP_J_MIN,
    F1_MIN_CONCEPTS,
    F1_SHARED_MAX,
    F3A_MIN_NAMES,
    F3A_SHARE_MIN,
    F3B_ISOLATED_SHARE_FACTOR,
    F3B_ISOLATED_SHARE_FLOOR,
    F3B_MIN_ISOLATED,
    F3B_MIN_OBJECTS,
    F3B_RELATIONS_PER_OBJECT_FRACTION,
    F5_TOP_SOURCES,
    FINDING_RULES,
    RULE_DUPLICATE_IMPORT,
    RULE_OFF_TOPIC_SOURCE,
    RULE_PARSE_DAMAGE,
    RULE_THRESHOLDS,
    RULES_VERSION,
    UBIQUITOUS_SOURCE_COUNT,
)
from app.domain.kg_merge_seed import (  # noqa: E402
    concept_decision_seeds,
    concept_seed_v1,
    concept_seed_v2,
    concept_squash_key,
    fold_decision,
)
from app.domain.knowledge_contracts import (  # noqa: E402
    CLUSTER_OBJECT_TYPES,
    OTHER_OBJECT_TYPE_GROUP,
    USABLE_STATUSES,
)
from app.services import kg_findings  # noqa: E402
from app.services.kg_analysis_precompute import CanonicalEdgeTableBuilder  # noqa: E402
from app.services.kg_findings import (  # noqa: E402
    FindingsObjectTally,
    GraphStructure,
    LookupRequest,
    SourceFacts,
    compute_findings,
)

SCRIPT_VERSION = 1
APPLICATION_NAME = "kg_findings_rehearsal"
# The whole stderr line for a driver error, besides the exception's class name:
# the driver's own text is never printed (AGENTS.md: no exception text in logs).
DATABASE_ERROR_HINT = (
    "数据库连接或查询失败(详情不输出:请核对 DSN、网络可达性与该角色的只读权限)"
)

# Same as ``Settings.kg_findings_max_candidate_pairs`` (pinned by a test): the
# cost cap on F2 pair GENERATIONS the product runs with by default.
DEFAULT_MAX_CANDIDATE_PAIRS = 5_000_000
# F2's J distribution below the product threshold: one extra join at this J.
DEFAULT_J_FLOOR = 0.30
DEFAULT_STATEMENT_TIMEOUT_SECONDS = 1200

PERCENTILES = (1, 5, 10, 25, 50, 75, 90, 95, 99)
DF_BOUNDS = (1, 10, 30, 100, 300, 1000)
J_THRESHOLDS = (0.30, 0.35, 0.40, 0.45, 0.50, 0.60, 0.80)
F1_SHARED_GRID = (0.02, 0.05, 0.10)
F1_MIN_CONCEPTS_GRID = (5, 10, 20)
F3A_SHARE_GRID = (0.05, 0.10, 0.20)
F3A_NAMES_GRID = (3, 5, 10)
F3B_FRACTION_GRID = (0.33, 0.5, 0.67)
F3B_ISOLATED_FACTOR_GRID = (2.0, 3.0, 5.0)
F3B_ISOLATED_FLOOR_GRID = (0.05, 0.10, 0.20)
CHUNK_YIELD_FRACTIONS = (Fraction(1, 4), Fraction(1, 3), Fraction(1, 2))
LOWEST_SHARED_SAMPLES = 30
TOP_PAIR_SAMPLES = 40
SAMPLES_PER_RULE = 20
LOWEST_SHARED_VALUES = 30


class RehearsalError(Exception):
    """A condition the operator must fix (not a bug): reported without a traceback."""


# ================================================================ measurement
def rss_mb() -> Tuple[float, Optional[float]]:
    """``(peak, current)`` resident set in MB. ``current`` needs /proc (Linux)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_mb = peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024
    current = None
    try:
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    current = int(line.split()[1]) / 1024
    except OSError:
        pass
    return round(peak_mb, 1), (round(current, 1) if current is not None else None)


class PhaseTimer:
    """Wall time and RSS per phase, progress on stderr (never any data)."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.perf_counter,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._clock = clock
        self._start = clock()
        self._log = log if log is not None else (
            lambda line: print(line, file=sys.stderr, flush=True)
        )
        self.phases: Dict[str, Dict[str, Any]] = {}

    @property
    def elapsed(self) -> float:
        return self._clock() - self._start

    @contextlib.contextmanager
    def phase(self, name: str, *, product: bool) -> Iterator[None]:
        """``product``: the phase is a cost the product pays too (its RSS / seconds
        are the numbers to read); False: the rehearsal's own measurement."""
        began = self._clock()
        self._log(f"[{self.elapsed:8.1f}s] {name} ...")
        try:
            yield
        finally:
            peak, current = rss_mb()
            self.phases[name] = {
                "seconds": round(self._clock() - began, 2),
                "rss_peak_mb": peak,
                "rss_now_mb": current,
                "product_cost": product,
            }
            self._log(
                f"[{self.elapsed:8.1f}s] {name} done "
                f"({self.phases[name]['seconds']} s, peak RSS {peak} MB)"
            )


def quantiles(values: Any, ps: Sequence[int] = PERCENTILES) -> Dict[str, float]:
    """``{p1..p99, min, max, n}`` of a sample; ``{}`` for an empty one."""
    sample = np.asarray(values, dtype=np.float64)
    if sample.size == 0:
        return {}
    out: Dict[str, float] = {
        f"p{p}": round(float(np.percentile(sample, p)), 4) for p in ps
    }
    out["min"] = round(float(sample.min()), 4)
    out["max"] = round(float(sample.max()), 4)
    out["n"] = int(sample.size)
    return out


def _label(value: float) -> str:
    return f"{value:g}"


def _fraction(value: float) -> Fraction:
    """The decimal literal, not the binary float (the engine reads thresholds so)."""
    return Fraction(repr(value))


# ===================================================================== SQL
# Every statement the script sends. All SELECT (a test asserts it on the wire).
# The read-stream fragments come from the product store so the caliber cannot
# drift; ``test_graph_and_object_sql_share_the_product_tail`` compares the text
# from the FROM clause on against what the product store really sends.
Query = Tuple[str, Sequence[Any]]


def _store() -> Any:
    from app.repositories.postgres import unified_kg_store

    return unified_kg_store


def state_query(notebook_id: str) -> Query:
    return (
        "SELECT kg_mutation_seq, cluster_mutation_seq, community_seq, "
        "cluster_generation, dirty, object_count, relation_count, cluster_count "
        "FROM unified_kg_state WHERE notebook_id=%s",
        (notebook_id,),
    )


def names_query(notebook_id: str) -> Query:
    """``community_graph_rows``' names query: canonical id -> canonical name."""
    generation = _store()._PUBLISHED_CLUSTER_GEN
    return (
        f"SELECT DISTINCT canonical_id, canonical_name FROM concept_clusters "
        f"WHERE notebook_id=%s AND generation = {generation}",
        (notebook_id, notebook_id),
    )


def graph_query(notebook_id: str) -> Query:
    """``community_graph_rows``' relation stream (the product's text from FROM on,
    row order included) with ``kr.source_id`` and the relink flag as extra columns."""
    store = _store()
    generation = store._PUBLISHED_CLUSTER_GEN
    return (
        f"SELECT COALESCE(cs.canonical_id, kr.source_object_id) AS s, "
        f"       COALESCE(ct.canonical_id, kr.target_object_id) AS t, "
        f"       kr.source_id AS source_id, "
        f"       COALESCE(kr.evidence->0->>'basis', '') LIKE 'relink:%%' AS is_relink "
        f"FROM knowledge_relations kr "
        f"LEFT JOIN concept_clusters cs ON cs.notebook_id=kr.notebook_id "
        f"AND cs.member_object_id=kr.source_object_id "
        f"AND cs.generation = {generation} "
        f"LEFT JOIN concept_clusters ct ON ct.notebook_id=kr.notebook_id "
        f"AND ct.member_object_id=kr.target_object_id "
        f"AND ct.generation = {generation} "
        f"WHERE kr.notebook_id=%s AND kr.review_status!='rejected' "
        f"AND {store._not_memory('kr')} "
        f"AND {store._not_memory_object_ref('kr.source_object_id', 'kr.notebook_id', 'xs')} "
        f"AND {store._not_memory_object_ref('kr.target_object_id', 'kr.notebook_id', 'xt')} "
        f"ORDER BY kr.id COLLATE \"C\"",
        (notebook_id, notebook_id, notebook_id),
    )


def object_query(notebook_id: str) -> Query:
    """``source_canonical_rows`` (the product's text from FROM on) with
    ``is_concept`` and ``object_type`` as extra columns."""
    generation = _store()._PUBLISHED_CLUSTER_GEN
    placeholders = ",".join("%s" for _ in USABLE_STATUSES)
    return (
        f"""
            SELECT o.source_id AS source_id,
                   COALESCE(c.canonical_id, o.id) AS canonical_id,
                   (o.object_type = 'concept') AS is_concept,
                   o.object_type AS object_type
            FROM knowledge_objects o
            LEFT JOIN concept_clusters c
                   ON c.notebook_id = o.notebook_id
                  AND c.member_object_id = o.id
                  AND c.generation = {generation}
            WHERE o.notebook_id = %s
              AND o.status IN ({placeholders})
              AND o.source_id != ''
              AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.id = o.source_id
                              AND s.notebook_id = o.notebook_id
                              AND s.source_type IN ('memory','knowhow'))
            """,
        (notebook_id, notebook_id, *USABLE_STATUSES),
    )


def decisions_query(notebook_id: str) -> Query:
    """``decided_seed_pairs_from``' statement."""
    return (
        "SELECT canonical_a, canonical_b, seed_a, seed_b, status "
        "FROM concept_merge_candidates WHERE notebook_id=%s "
        "AND status IN ('confirmed','rejected','deferred')",
        (notebook_id,),
    )


class StoreReads:
    """The product store's own findings name reads, sent on this script's
    connection: ``kg_finding_member_name_rows`` (every member of each listed
    canonical, with its ``visible`` flag) and the display-name read behind
    ``kg_finding_display_name_rows`` (a source's own objects in a canonical).

    The SAME statements the product sends (codex #941 R4): batched by canonical
    id (≤ 500 per statement), driven by the canonical / primary-key indexes
    (``tests/postgres/test_kg_findings_plan_pins.py`` pins their plans on a
    production-shaped corpus), answered with the product's row order — never one
    ``COALESCE(c.canonical_id, o.id) = %s`` scan per key. The store reads rows by
    column name, so each statement gets a dict-row cursor; ``statements`` counts
    them for the report."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self.statements = 0

    def execute(self, sql: str, params: Any = (), **kwargs: Any) -> Any:
        self.statements += 1
        cursor = self._conn.cursor(row_factory=dict_row)
        cursor.execute(sql, params, **kwargs)
        return cursor

    def member_name_rows(
        self, notebook_id: str, canonical_ids: Sequence[str]
    ) -> List[Tuple[str, str, bool, int]]:
        return _store().UnifiedKgStore.kg_finding_member_name_rows(
            self, notebook_id, canonical_ids)

    def display_name_rows(
        self, notebook_id: str, pairs: Sequence[Tuple[str, str]]
    ) -> List[Tuple[str, str, str, int]]:
        return _store().UnifiedKgStore._display_name_rows_in(self, notebook_id, pairs)


def source_facts_query(source_ids: Sequence[str], *, with_titles: bool) -> Query:
    """Creation time (F2's tie-break) and — only when samples are asked for — the title."""
    columns = "id, title, created_at" if with_titles else "id, created_at"
    return (f"SELECT {columns} FROM sources WHERE id = ANY(%s)", (list(source_ids),))


def source_titles_query(source_ids: Sequence[str]) -> Query:
    return ("SELECT id, title FROM sources WHERE id = ANY(%s)", (list(source_ids),))


def chunk_counts_query(notebook_id: str) -> Query:
    return (
        "SELECT source_id, COUNT(*) FROM chunks WHERE notebook_id=%s GROUP BY source_id",
        (notebook_id,),
    )


def concept_names_query(notebook_id: str) -> Query:
    """The name of every concept object that can take part in the seed space
    (not deprecated; Memory-derived ones are excluded exactly as the graph reads do)."""
    return (
        f"SELECT o.payload->>'name' FROM knowledge_objects o WHERE o.notebook_id=%s "
        f"AND o.object_type='concept' AND o.status <> 'deprecated' "
        f"AND {_store()._not_memory('o')}",
        (notebook_id,),
    )


def stream_rows(conn: Any, query: Query) -> Iterator[tuple]:
    """Server-side streamed rows. Always closed, even when the consumer stops early
    (an abandoned stream would wedge the connection)."""
    sql, params = query
    with conn.cursor() as cursor:
        with contextlib.closing(cursor.stream(sql, params)) as rows:
            yield from rows


def fetch_all(conn: Any, query: Query) -> List[tuple]:
    """Small result sets only (point lookups, one-row state)."""
    sql, params = query
    return conn.execute(sql, params).fetchall()


# ================================================================ connection
def connection_options(statement_timeout_seconds: int) -> str:
    """Startup options of every connection: read-only first, then the timeout."""
    return (
        "-c default_transaction_read_only=on "
        f"-c statement_timeout={int(statement_timeout_seconds) * 1000}"
    )


def open_readonly_connection(
    dsn: str, *, statement_timeout_seconds: int = DEFAULT_STATEMENT_TIMEOUT_SECONDS
) -> Any:
    """The only way this script opens a database connection."""
    connection = psycopg.connect(
        dsn,
        options=connection_options(statement_timeout_seconds),
        application_name=APPLICATION_NAME,
    )
    connection.read_only = True
    return connection


def resolve_dsn(dsn: Optional[str], dsn_env: str) -> str:
    """The DSN from the flag or the environment; PostgreSQL only; never echoed."""
    value = dsn or os.environ.get(dsn_env, "")
    if not value:
        raise RehearsalError(
            f"没有 DSN:传 --dsn,或设置环境变量 {dsn_env}(postgresql:// URL)"
        )
    try:
        identity = database_identity(value)
    except ValueError:
        raise RehearsalError(
            "DSN 不是 postgresql:// URL(本脚本只支持 PostgreSQL;值不回显)") from None
    if identity.scheme != "postgresql":
        raise RehearsalError("本脚本只支持 PostgreSQL(SQLite 请用 kg_quality_audit.py)")
    return value


# ============================================================== graph / objects
@dataclass
class RawEdges:
    """Per relation row (non-self-loop), in stream order: only kept for
    ``--partition-bench``, which deletes a source's rows and re-partitions."""

    src: "array[int]" = field(default_factory=lambda: array("i"))
    dst: "array[int]" = field(default_factory=lambda: array("i"))
    source_rank: "array[int]" = field(default_factory=lambda: array("i"))
    ranks: Dict[str, int] = field(default_factory=dict)

    def append(self, left: int, right: int, source_id: str) -> None:
        self.src.append(left)
        self.dst.append(right)
        self.source_rank.append(self.ranks.setdefault(source_id, len(self.ranks)))


@dataclass
class GraphScan:
    """Step ① of the product: what the relation stream leaves behind."""

    can2idx: Dict[str, int]
    table: Any                                  # CanonicalEdgeTable
    relations_by_source: Dict[str, int]
    relation_rows: int
    relink_rows: int
    raw: Optional[RawEdges]


def scan_graph(
    rows: Iterable[Tuple[str, str, Optional[str], bool]], *, keep_raw: bool = False
) -> GraphScan:
    """Fold ``(s, t, source_id, is_relink)`` rows exactly like ``rebuild_communities``:
    node indices in first-seen order, self loops skipped, relations counted per
    source over EVERY row (self loops included, F3b's relations / objects)."""
    can2idx: Dict[str, int] = {}
    builder = CanonicalEdgeTableBuilder()
    by_source: Dict[str, int] = {}
    relation_rows = relink_rows = 0
    raw = RawEdges() if keep_raw else None
    for s, t, source_id, is_relink in rows:
        relation_rows += 1
        sid = source_id or ""
        by_source[sid] = by_source.get(sid, 0) + 1
        relink_rows += bool(is_relink)
        if not s or not t or s == t:
            continue
        left = can2idx.setdefault(s, len(can2idx))
        right = can2idx.setdefault(t, len(can2idx))
        builder.add(left, right)
        if raw is not None:
            raw.append(left, right, sid)
    table = builder.build(len(can2idx))
    return GraphScan(can2idx, table, by_source, relation_rows, relink_rows, raw)


@dataclass
class ObjectScan:
    """Step ⑤ of the product: the object stream folded into the engine's tally."""

    tally: FindingsObjectTally
    object_rows: int
    isolated_by_type: Dict[str, int]


def _type_bucket(object_type: Optional[str]) -> str:
    """Known object types by name; custom ones (knowhow column names are user
    content) fold into one group — the type name is never reported."""
    return object_type if object_type in CLUSTER_OBJECT_TYPES else OTHER_OBJECT_TYPE_GROUP


def scan_objects(
    rows: Iterable[Tuple[str, str, object, Optional[str]]],
    can2idx: Mapping[str, int],
    structure: GraphStructure,
) -> ObjectScan:
    """Feed ``(source_id, canonical_id, is_concept, object_type)`` rows to a tally."""
    tally = FindingsObjectTally(can2idx, structure)
    isolated_by_type: Counter = Counter()
    count = 0
    nodes = structure.n_nodes
    for source_id, canonical_id, is_concept, object_type in rows:
        count += 1
        tally.add(source_id, canonical_id, is_concept)
        index = can2idx.get(canonical_id)
        if index is None or index >= nodes:
            isolated_by_type[_type_bucket(object_type)] += 1
    return ObjectScan(tally, count, dict(isolated_by_type))


def snapshot_tally(tally: FindingsObjectTally, can2idx: Mapping[str, int]) -> FindingsObjectTally:
    """A second, independent tally with the same content.

    ``compute_findings`` drains its tally (one-shot by design), but the
    distributions are measured on the very same drained corpus. A deep copy of the
    few int32 columns costs ≈ 8 B per concept object; ``can2idx`` is shared, never
    copied (the memo maps its id to itself)."""
    return copy.deepcopy(tally, {id(can2idx): can2idx})


# ================================================================== decisions
def _strip_k(canonical_id: Optional[str]) -> str:
    cid = canonical_id or ""
    return cid[2:] if cid.startswith("K-") else cid


def decisions_from_rows(rows: Iterable[tuple]) -> Dict[frozenset, str]:
    """``decided_seed_pairs_from`` on already-read rows
    ``(canonical_a, canonical_b, seed_a, seed_b, status)``."""
    out: Dict[frozenset, str] = {}
    for canonical_a, canonical_b, seed_a, seed_b, status in rows:
        fold_decision(out, frozenset((seed_a or _strip_k(canonical_a),
                                      seed_b or _strip_k(canonical_b))), status)
    return out


def member_seeds_from_rows(rows: Iterable[Sequence[Any]]) -> Dict[str, Set[str]]:
    """Member-name rows ``(canonical_id, name, ...)`` -> the decision seeds of
    each canonical (v1 and v2, ``concept_decision_seeds`` — what the engine
    stores; every member, hidden ones included — as the product's)."""
    out: Dict[str, Set[str]] = {}
    for row in rows:
        out.setdefault(row[0], set()).update(concept_decision_seeds(row[1] or ""))
    return out


# ================================================================== lookups
class DbLookups:
    """Answers the engine's point-lookup rounds from the database (read-only),
    the way the product's ``_findings_lookups`` does, through `StoreReads`:

      · ``(None, canonical_id)`` (F4 / S1 re-judging) → one batched member-name
        read: the visible members' names answer the key, every member's v1 seed
        goes to F4's evidence. These decide counts, so they are always asked;
      · ``(source_id, canonical_id)`` → only sample names: read only when samples
        are asked for (``with_titles``), otherwise left unanswered (the engine
        omits the sample, exactly as for a vanished object);
      · sources → creation time (F2's tie-break), titles only with samples."""

    def __init__(self, conn: Any, notebook_id: str, *, with_titles: bool) -> None:
        self._conn = conn
        self._notebook_id = notebook_id
        self._with_titles = with_titles
        self.reads = StoreReads(conn)

    def __call__(
        self, request: LookupRequest
    ) -> Tuple[List[Tuple[Optional[str], str, str]], Dict[str, SourceFacts], Dict[str, Set[str]]]:
        names: List[Tuple[Optional[str], str, str]] = []
        pairs = [key for key in request.names if key[0] is not None]
        if pairs and self._with_titles:
            names.extend(
                (source_id, canonical_id, name) for source_id, canonical_id, name, _count
                in self.reads.display_name_rows(self._notebook_id, pairs)
            )
        members = sorted({cid for sid, cid in request.names if sid is None})
        rows = self.reads.member_name_rows(self._notebook_id, members) if members else []
        # Like the product: names shown come from visible members only; the v1
        # seeds of ALL members (hidden ones included) go to F4's evidence.
        names.extend((None, cid, name) for cid, name, visible, _count in rows if visible)
        seeds = member_seeds_from_rows(rows)
        sources: Dict[str, SourceFacts] = {}
        if request.sources:
            for row in fetch_all(
                self._conn, source_facts_query(request.sources, with_titles=self._with_titles)
            ):
                source_id, title, created = (row if self._with_titles else (row[0], "", row[1]))
                sources[source_id] = SourceFacts(
                    title or "", created.isoformat() if created else None
                )
        return names, sources, seeds


def drive_draft(
    draft: kg_findings.FindingsDraft,
    answer: Callable[
        [LookupRequest],
        tuple,  # (names, sources[, member seeds])
    ],
) -> Tuple[kg_findings.FindingsResult, Dict[str, int]]:
    """The lookup protocol (``FindingsDraft`` docstring): ask, answer, until done."""
    rounds = name_keys = source_keys = 0
    while (request := draft.pending()) is not None:
        rounds += 1
        name_keys += len(request.names)
        source_keys += len(request.sources)
        answered = answer(request)
        names, sources = answered[0], answered[1]
        draft.supply(names=names, sources=sources,
                     member_seeds=answered[2] if len(answered) > 2 else None)
    return draft.result(), {
        "lookup_rounds": rounds, "lookup_name_keys": name_keys,
        "lookup_source_keys": source_keys,
    }


# ============================================================ F2 floor run
@contextlib.contextmanager
def duplicate_threshold(value: float) -> Iterator[None]:
    """Temporarily run the engine's F2 join at another Jaccard threshold.

    The prefix filter is only complete for the threshold it was built for, so the
    J distribution BELOW the product threshold cannot be read off the product run;
    the engine function is called again with its module constant lowered (restored
    in ``finally``). Counts at higher thresholds follow exactly from the pairs."""
    previous = kg_findings._DUP_J
    kg_findings._DUP_J = _fraction(value)
    try:
        yield
    finally:
        kg_findings._DUP_J = previous


def pairs_at_least(pairs: Sequence[Tuple[int, int, int, int]], threshold: float) -> int:
    """Exact count of verified ``(a, b, inter, union)`` pairs with J >= threshold."""
    cut = _fraction(threshold)
    return sum(1 for _a, _b, inter, union in pairs
               if inter * cut.denominator >= cut.numerator * union)


# ============================================================ report sections
def graph_section(scan: GraphScan, structure: GraphStructure, unique_edges: int) -> dict:
    sizes = (np.bincount(structure.component) if structure.n_nodes
             else np.zeros(0, dtype=np.int64))
    return {
        "relation_rows": scan.relation_rows,
        "relink_relation_rows": scan.relink_rows,
        "relations_with_empty_source_id": scan.relations_by_source.get("", 0),
        "canonical_nodes": structure.n_nodes,
        "unique_edges": unique_edges,
        "components": structure.components,
        "giant_component_nodes": structure.giant_nodes,
        "giant_share_of_nodes": (round(structure.giant_nodes / structure.n_nodes, 4)
                                 if structure.n_nodes else 0.0),
        "component_size_dist": quantiles(sizes),
        "degree_dist": quantiles(structure.degree),
    }


def objects_section(corpus: Any, scan: ObjectScan) -> dict:
    isolated = int(corpus.isolated.sum())
    usable = int(corpus.objects.sum())
    return {
        "usable_visible_objects": usable,
        "object_rows_read": scan.object_rows,
        "sources_with_objects": len(corpus.source_ids),
        "isolated_objects": isolated,
        "isolated_share": round(isolated / usable, 4) if usable else 0.0,
        "isolated_by_type": scan.isolated_by_type,
        "objects_per_source": quantiles(corpus.objects),
    }


def concepts_section(corpus: Any) -> dict:
    """Document frequency: in how many sources each concept canonical occurs."""
    df = corpus.df[corpus.df > 0]
    incidences = int(corpus.inc_can.size)
    sources = max(1, len(corpus.source_ids))
    per_incidence = corpus.df[corpus.inc_can]
    return {
        "ubiquitous_source_count_in_use": UBIQUITOUS_SOURCE_COUNT,
        "concept_incidences": incidences,
        "concept_canonicals": int(df.size),
        "doc_freq_dist": quantiles(df),
        "doc_freq_share_of_sources_dist": quantiles(df / sources),
        "concepts_df_gt": {str(k): int((corpus.df > k).sum()) for k in DF_BOUNDS},
        "incidence_share_df_gt": {
            str(k): (round(float((per_incidence > k).sum()) / incidences, 4)
                     if incidences else 0.0)
            for k in DF_BOUNDS
        },
        "concepts_per_source_dist": quantiles(corpus.concepts),
        "sources_without_concepts": int((corpus.concepts == 0).sum()),
    }


def f2_section(
    corpus: Any, duplicates: Any, comparable: bool, cap: int,
    floor: Optional[Any], j_floor: float,
) -> dict:
    pairs = duplicates.pairs
    out: Dict[str, Any] = {
        "comparable": comparable,
        "candidate_pairs": duplicates.candidate_pairs,
        "candidate_pair_generations": duplicates.candidate_generations,
        "candidate_pair_generation_limit": int(cap),
        "candidates_truncated": duplicates.candidates_truncated,
        "duplicate_pairs": len(pairs),
        "sources_in_duplicate_pairs": len({rank for a, b, *_ in pairs for rank in (a, b)}),
        "j_dist_of_duplicates": quantiles([inter / union for _a, _b, inter, union in pairs]),
    }
    basis, floor_value = (floor, j_floor) if floor is not None else (duplicates, DUP_J_MIN)
    out["pairs_j_ge_floor"] = {
        "floor": floor_value,
        "candidate_pairs": basis.candidate_pairs,
        "candidate_pair_generations": basis.candidate_generations,
        "candidates_truncated": basis.candidates_truncated,
        "pairs": len(basis.pairs),
    }
    out["pairs_j_ge"] = {
        _label(t): pairs_at_least(basis.pairs, t) for t in J_THRESHOLDS if t >= floor_value
    }
    return out


def f1_section(
    corpus: Any, shared_share: "np.ndarray", giant_share: Optional["np.ndarray"],
    comparable: bool, f3_hit: "np.ndarray", pairs: Sequence[Tuple[int, int, int, int]],
) -> Tuple[dict, "np.ndarray", "np.ndarray", "np.ndarray"]:
    """F1 as ``compute_findings`` judges it, plus the sensitivity grid.

    Returns ``(section, eligible, off_topic, f1_sources)``: ``off_topic`` before the
    F3 deferral, ``f1_sources`` after it (the sources that become F1 rows)."""
    n_sources = len(corpus.source_ids)
    eligible = corpus.concepts >= F1_MIN_CONCEPTS
    off_topic = eligible & (shared_share < F1_SHARED_MAX) & comparable
    f1_sources = off_topic & ~f3_hit
    giant = giant_share if giant_share is not None else np.zeros(n_sources)
    in_pair = np.zeros(n_sources, dtype=bool)
    for a, b, _inter, _union in pairs:
        in_pair[a] = in_pair[b] = True
    sensitivity = {
        f"shared<{_label(shared)}&concepts>={floor}": int(
            ((corpus.concepts >= floor) & (shared_share < shared) & comparable).sum()
        )
        for shared in F1_SHARED_GRID for floor in F1_MIN_CONCEPTS_GRID
    }
    in_graph = corpus.in_graph > 0
    lowest = np.sort(shared_share[eligible])[:LOWEST_SHARED_VALUES]
    section = {
        "comparable": comparable,
        "eligible_sources": int(eligible.sum()),
        "too_few_concepts": int(n_sources - eligible.sum()),
        "hits_before_f3_deferral": int(off_topic.sum()),
        "deferred_to_f3": int((off_topic & f3_hit).sum()),
        "hits": int(f1_sources.sum()),
        "hits_with_giant_share_gt_0.5": int((f1_sources & (giant > 0.5)).sum()),
        "hits_also_in_a_duplicate_pair": int((f1_sources & in_pair).sum()),
        "sensitivity_before_f3_deferral": sensitivity,
        "shared_share_dist": quantiles(shared_share[eligible]),
        "giant_share_dist": quantiles(giant[in_graph]),
        "lowest_shared_shares": [round(float(v), 4) for v in lowest],
    }
    return section, eligible, off_topic, f1_sources


def _low_relation_hits(
    corpus: Any, relations_by_source: Mapping[str, int]
) -> Tuple[Optional[Fraction], Dict[str, int], List[int], List[int], List[int]]:
    """F3b relations / objects, exactly as the engine: judged sources only."""
    objects = [max(1, int(n)) for n in corpus.objects.tolist()]
    relations = [int(relations_by_source.get(s, 0) or 0) for s in corpus.source_ids]
    judged = [i for i, o in enumerate(objects) if o >= F3B_MIN_OBJECTS]
    if not judged:
        return None, {_label(f): 0 for f in F3B_FRACTION_GRID}, objects, relations, judged
    median = kg_findings._exact_median(
        [relations[i] for i in judged], [objects[i] for i in judged]
    )
    hits: Dict[str, int] = {}
    for fraction in F3B_FRACTION_GRID:
        cut = median * _fraction(fraction)
        hits[_label(fraction)] = sum(
            relations[i] * cut.denominator < cut.numerator * objects[i] for i in judged
        )
    return median, hits, objects, relations, judged


def _isolated_hits(
    corpus: Any, objects: Sequence[int]
) -> Tuple[Fraction, Dict[str, int]]:
    """F3b isolated share: ``max(factor x median, floor)`` and ``>= F3B_MIN_ISOLATED``."""
    isolated = [int(n) for n in corpus.isolated.tolist()]
    median = kg_findings._exact_median(isolated, objects)
    hits: Dict[str, int] = {}
    for factor in F3B_ISOLATED_FACTOR_GRID:
        for floor in F3B_ISOLATED_FLOOR_GRID:
            cut = max(median * _fraction(factor), _fraction(floor))
            hits[f"factor={_label(factor)}&floor={_label(floor)}"] = sum(
                i >= F3B_MIN_ISOLATED and i * cut.denominator > cut.numerator * o
                for i, o in zip(isolated, objects)
            )
    return median, hits


def chunk_yield_section(corpus: Any, chunk_counts: Mapping[str, int]) -> dict:
    """Objects per chunk (the design's third F3b signal, not part of rules v1)."""
    objects = corpus.objects.tolist()
    ratios: List[float] = []
    judged: List[float] = []
    without = 0
    for rank, source_id in enumerate(corpus.source_ids):
        chunks = chunk_counts.get(source_id)
        if not chunks:
            without += 1
            continue
        ratio = objects[rank] / chunks
        ratios.append(ratio)
        if objects[rank] >= F3B_MIN_OBJECTS:
            judged.append(ratio)
    out: Dict[str, Any] = {
        "sources_without_chunks": without,
        "objects_per_chunk_dist": quantiles(ratios),
        "judged_sources": len(judged),
        "objects_per_chunk_dist_judged": quantiles(judged),
    }
    if judged:
        median = float(np.median(judged))
        out["median_objects_per_chunk_judged"] = round(median, 4)
        out["sensitivity_hits"] = {
            f"yield<{f.numerator}/{f.denominator}*median": int(
                sum(1 for ratio in judged if ratio < median * float(f))
            )
            for f in CHUNK_YIELD_FRACTIONS
        }
    return out


def f3_section(
    corpus: Any, f3: Any, relations_by_source: Mapping[str, int], named: "np.ndarray",
    fragmented: "np.ndarray", off_topic: "np.ndarray",
    chunk_counts: Optional[Mapping[str, int]],
) -> dict:
    median_relations, relation_hits, objects, relations, judged = _low_relation_hits(
        corpus, relations_by_source
    )
    median_isolated, isolated_hits = _isolated_hits(corpus, objects)
    some = f3.fragmented_names > 0
    share_names = {
        f"share>={_label(share)}&names>={names}": int(
            ((corpus.concepts > 0) & (f3.fragmented_share >= share)
             & (f3.fragmented_names >= names)).sum()
        )
        for share in F3A_SHARE_GRID for names in F3A_NAMES_GRID
    }
    rate = np.asarray(relations, dtype=np.float64) / np.asarray(objects, dtype=np.float64) \
        if objects else np.zeros(0)
    out = {
        "hits": int(f3.hit.sum()),
        "hits_also_off_topic": int((f3.hit & off_topic).sum()),
        "fragmented_names": {
            "named_concept_canonicals": int(named.sum()),
            "fragmented_concept_canonicals": int(fragmented.sum()),
            "sources_with_any_fragmented": int(some.sum()),
            "hits": int(f3.fragmented_hit.sum()),
            "min_share": F3A_SHARE_MIN,
            "min_names": F3A_MIN_NAMES,
            "share_dist_nonzero": quantiles(f3.fragmented_share[some]),
            "sensitivity_hits": share_names,
        },
        "low_relations": {
            "judged_sources": len(judged),
            "min_objects": F3B_MIN_OBJECTS,
            "median_relations_per_object": (float(median_relations)
                                            if median_relations is not None else None),
            "relations_per_object_dist_judged": quantiles(rate[judged]) if judged else {},
            "relations_per_object_dist_all": quantiles(rate),
            "fraction_of_median": F3B_RELATIONS_PER_OBJECT_FRACTION,
            "hits": int(f3.low_relations.sum()),
            "sensitivity_hits": relation_hits,
        },
        "high_isolated": {
            "median_isolated_share": float(median_isolated),
            "threshold_share": f3.isolated_threshold,
            "factor": F3B_ISOLATED_SHARE_FACTOR,
            "floor": F3B_ISOLATED_SHARE_FLOOR,
            "min_isolated_objects": F3B_MIN_ISOLATED,
            "isolated_share_dist": quantiles(f3.isolated_share),
            "sources_with_min_isolated": int((corpus.isolated >= F3B_MIN_ISOLATED).sum()),
            "hits": int(f3.high_isolated.sum()),
            "sensitivity_hits": isolated_hits,
        },
    }
    if chunk_counts is not None:
        out["objects_per_chunk"] = chunk_yield_section(corpus, chunk_counts)
    return out


def f5_section(corpus: Any, scan: GraphScan) -> dict:
    isolated = int(corpus.isolated.sum())
    usable = int(corpus.objects.sum())
    top = np.sort(corpus.isolated)[::-1][:F5_TOP_SOURCES]
    rows = scan.relation_rows
    return {
        "isolated_objects": isolated,
        "isolated_share": round(isolated / usable, 4) if usable else 0.0,
        "sources_with_isolated_objects": int((corpus.isolated > 0).sum()),
        "isolated_objects_per_source_dist": quantiles(corpus.isolated[corpus.isolated > 0]),
        "top_sources_share_of_isolated": (round(float(top.sum()) / isolated, 4)
                                          if isolated else 0.0),
        "relink_relation_rows": scan.relink_rows,
        "relink_share_of_relations": round(scan.relink_rows / rows, 4) if rows else 0.0,
    }


# ----------------------------------------------------------------------- F4
def _squash_groups(
    corpus: Any, names: Mapping[str, str], can2idx: Mapping[str, int],
    collisions: Set[int],
) -> Tuple[Dict[int, str], Dict[str, List[int]]]:
    """The squash-key groups of ≥ 2 canonicals, built as ``compute_findings`` builds them."""
    ids = kg_findings._resolve_canonical_ids(can2idx, corpus, set(collisions))
    groups: Dict[str, List[int]] = {}
    for index in sorted(collisions):
        groups.setdefault(concept_squash_key(names[ids[index]]), []).append(index)
    return ids, {key: members for key, members in groups.items() if len(members) >= 2}


def f4_section(
    corpus: Any, names: Mapping[str, str], can2idx: Mapping[str, int],
    named: "np.ndarray", hashes: "np.ndarray", hashed: "np.ndarray", *, show_samples: bool,
) -> Tuple[dict, Optional[list]]:
    """F4 wide / S1 narrow group counts BEFORE stored decisions (the engine's own
    ``_merge_specs`` with an empty adjacency); the counts after decisions are in
    ``engine.rules``."""
    collisions = kg_findings._squash_collisions(hashes, hashed)
    ids, groups = _squash_groups(corpus, names, can2idx, collisions)
    wide, narrow, counts = kg_findings._merge_specs(corpus, groups, ids, names, {})
    section = {
        "named_concept_canonicals": int(named.sum()),
        "squash_hash_collision_canonicals": len(collisions),
        "squash_groups": len(groups),
        "squash_group_size_dist": quantiles([len(m) for m in groups.values()]),
        "wide_groups_before_decisions": len(wide),
        "narrow_groups_before_decisions": counts["narrow_groups"],
        "narrow_group_objects": counts["narrow_group_objects"],
    }
    examples = None
    if show_samples:
        examples = []
        for key in sorted(groups, key=lambda k: (-len(groups[k]), k))[:SAMPLES_PER_RULE]:
            members = groups[key]
            classes = {concept_seed_v2(names[ids[i]]) for i in members}
            examples.append({
                "half": "wide" if len(classes) > 1 else "narrow",
                "names": sorted({names[ids[i]] for i in members})[:4],
            })
    return section, examples


# ============================================================ corpus analysis
@dataclass
class Analysis:
    sections: Dict[str, Any]
    samples: Dict[str, Any]


def analyze_corpus(
    corpus: Any, *, structure: GraphStructure, can2idx: Mapping[str, int],
    names: Mapping[str, str], relations_by_source: Mapping[str, int],
    max_candidate_pairs: int, j_floor: float = DEFAULT_J_FLOOR,
    chunk_counts: Optional[Mapping[str, int]] = None, show_samples: bool = False,
) -> Analysis:
    """Every distribution of §14③ on one drained corpus, by the engine's own functions.

    Mirrors the order of ``compute_findings`` (names pass, F2, F3, F1, F4) so each
    number here is the one the engine computed from — the engine's own run, on its
    own tally, is reported separately under ``engine`` and the two must agree
    (a test pins the hit counts equal)."""
    n_sources = len(corpus.source_ids)
    sections: Dict[str, Any] = {"concepts": concepts_section(corpus)}
    samples: Dict[str, Any] = {}
    if n_sources == 0:
        sections["note"] = "no usable visible objects: every rule is skipped"
        return Analysis(sections, samples)
    comparable = int((corpus.concepts > 0).sum()) >= 2
    named, fragmented, hashes, hashed = kg_findings._names_pass(names, can2idx, corpus)

    # F2 first (F1's "shared" excludes duplicate partners).
    if comparable:
        duplicates = kg_findings._duplicate_pairs(corpus, max_candidate_pairs)
    else:
        duplicates = kg_findings._DuplicatePairs(
            [], 0, 0, False, kg_findings._EMPTY_INDEX, np.zeros(n_sources + 1, dtype=np.int64)
        )
    floor = None
    if comparable and j_floor < DUP_J_MIN:
        with duplicate_threshold(j_floor):
            floor = kg_findings._duplicate_pairs(corpus, max_candidate_pairs)
    sections["f2"] = f2_section(corpus, duplicates, comparable, max_candidate_pairs, floor, j_floor)

    f3 = kg_findings._parse_damage(corpus, relations_by_source, fragmented)
    shared_share, _others = kg_findings._shared_shares(corpus, duplicates.pairs)
    giant_share = (
        np.where(corpus.in_graph > 0, corpus.giant / np.maximum(1, corpus.in_graph), 0.0)
        if corpus.n_nodes else None
    )
    sections["f1"], eligible, off_topic, f1_sources = f1_section(
        corpus, shared_share, giant_share, comparable, f3.hit, duplicates.pairs
    )
    sections["f3"] = f3_section(
        corpus, f3, relations_by_source, named, fragmented, off_topic, chunk_counts
    )
    sections["f4"], f4_examples = f4_section(
        corpus, names, can2idx, named, hashes, hashed, show_samples=show_samples
    )

    if show_samples:
        giant = giant_share if giant_share is not None else np.zeros(n_sources)
        in_pair = {rank for a, b, *_ in duplicates.pairs for rank in (a, b)}
        low = np.argsort(shared_share + (~eligible) * 9, kind="stable")[:LOWEST_SHARED_SAMPLES]
        samples["lowest_shared_sources"] = [
            {"source_id": corpus.source_ids[r], "shared_share": round(float(shared_share[r]), 4),
             "giant_share": round(float(giant[r]), 4), "concepts": int(corpus.concepts[r]),
             "objects": int(corpus.objects[r]), "f1": bool(f1_sources[r]),
             "in_duplicate_pair": r in in_pair}
            for r in low.tolist() if eligible[r]
        ]
        top = sorted(duplicates.pairs, key=lambda p: (-p[2] / p[3], p[0], p[1]))
        samples["top_duplicate_pairs"] = [
            {"jaccard": round(inter / union, 3), "shared_concepts": inter,
             "a": corpus.source_ids[a], "b": corpus.source_ids[b],
             "objects": [int(corpus.objects[a]), int(corpus.objects[b])]}
            for a, b, inter, union in top[:TOP_PAIR_SAMPLES]
        ]
        worst = np.argsort(-f3.fragmented_share, kind="stable")[:15]
        samples["most_fragmented_sources"] = [
            {"source_id": corpus.source_ids[r], "share": round(float(f3.fragmented_share[r]), 4),
             "names": int(f3.fragmented_names[r])}
            for r in worst.tolist() if f3.fragmented_names[r] > 0
        ]
        if f4_examples is not None:
            samples["squash_group_examples"] = f4_examples
    return Analysis(sections, samples)


# ================================================================ engine report
def _row_numbers(rule_id: str, row: Mapping[str, Any]) -> dict:
    """The numbers of one finding — no key, label, name or sample."""
    evidence = row["evidence"]
    out: Dict[str, Any] = {"impact_objects": row["impact_objects"]}
    for key, value in evidence.items():
        if isinstance(value, (bool, int, float)):
            out[key] = value
    if rule_id == RULE_PARSE_DAMAGE:
        out["signals"] = list(evidence.get("signals", []))
    if rule_id == RULE_DUPLICATE_IMPORT:
        out["objects"] = [side["objects"] for side in evidence.get("sources", [])]
    if rule_id == RULE_OFF_TOPIC_SOURCE:
        out["partners"] = len(evidence.get("partners", []))
    return out


def engine_section(
    result: kg_findings.FindingsResult, lookups: Mapping[str, int],
    decisions: int, member_seed_calls: Sequence[int], *, show_samples: bool,
) -> Tuple[dict, dict]:
    """What the product's own pass computed: statuses (counts) and per-finding numbers."""
    rules = {
        rule: {key: value for key, value in status.items() if key != "examples"}
        for rule, status in result.payload["rules"].items()
    }
    by_rule: Dict[str, List[dict]] = {}
    for row in result.rows:
        by_rule.setdefault(row["rule_id"], []).append(row)
    section = {
        **lookups,
        "decisions": decisions,
        "member_seed_calls": list(member_seed_calls),
        "rules_version": result.payload["rules_version"],
        "structure": result.payload["structure"],
        "rules": rules,
        "rows_per_rule": {rule: len(by_rule.get(rule, [])) for rule in FINDING_RULES},
        "findings": {
            rule: [_row_numbers(rule, row) for row in rows] for rule, rows in by_rule.items()
        },
    }
    samples: dict = {}
    if show_samples:
        samples["findings"] = {
            rule: rows[:SAMPLES_PER_RULE] for rule, rows in by_rule.items()
        }
        samples["s1_examples"] = result.payload["rules"].get("S1", {}).get("examples", [])
    return section, samples


# ================================================================ S1 impact
def _bucket_add(table: Dict[str, Any], key: str, value: str) -> None:
    """A multimap that stores a bare string until a second distinct value arrives:
    millions of seeds map 1:1, and a set object each would cost gigabytes."""
    current = table.get(key)
    if current is None:
        table[key] = value
    elif isinstance(current, str):
        if current != value:
            table[key] = {current, value}
    else:
        current.add(value)


def _bucket_set(entry: Any) -> Set[str]:
    return {entry} if isinstance(entry, str) else entry


def _bucket_many(table: Mapping[str, Any]) -> int:
    return sum(1 for entry in table.values() if not isinstance(entry, str))


def s1_impact(
    names: Iterable[Optional[str]], decision_rows: Sequence[tuple],
    *, show_samples: bool = False,
) -> dict:
    """What the S1 seed switch (``concept_seed_v2``) does to the existing seed space.

    ``names``: the name of every non-deprecated concept object;
    ``decision_rows``: ``(canonical_a, canonical_b, seed_a, seed_b, status)``.
    Note: the acronym-alias redirection of the incremental path is not replicated
    (v2's own alias table is)."""
    old2new: Dict[str, Any] = {}
    new2old: Dict[str, Any] = {}
    merge_only: Dict[str, Any] = {}
    concept_objects = 0
    for name in names:
        if not name:
            continue
        concept_objects += 1
        old = concept_seed_v1(name)
        if not old:
            continue
        new = concept_seed_v2(name)
        _bucket_add(old2new, old, new)
        _bucket_add(new2old, new, old)
        _bucket_add(merge_only, old.replace(" ", ""), new)
    status_counts: Counter = Counter()
    empty_seed = unmappable = 0
    collide: Counter = Counter()
    for canonical_a, canonical_b, seed_a, seed_b, status in decision_rows:
        status_counts[status] += 1
        if not seed_a or not seed_b:
            empty_seed += 1
        left, right = old2new.get(seed_a or _strip_k(canonical_a)), \
            old2new.get(seed_b or _strip_k(canonical_b))
        if left is None or right is None:
            unmappable += 1
            continue
        if status in DECISION_APART_STATUSES and _bucket_set(left) & _bucket_set(right):
            collide[status] += 1
    out: Dict[str, Any] = {
        "concept_objects": concept_objects,
        "old_seeds": len(old2new),
        "s1_seeds": len(new2old),
        "old_seed_groups_split_by_s1": _bucket_many(old2new),
        "s1_seeds_merging_several_old": _bucket_many(new2old),
        "merge_only_variant_risky_merges": _bucket_many(merge_only),
        "decisions": dict(status_counts),
        "decisions_with_empty_seed": empty_seed,
        "decisions_unmappable_to_current_objects": unmappable,
        "rejected_or_deferred_that_collide_under_s1": sum(collide.values()),
        "rejected_or_deferred_that_collide_by_status": dict(collide),
        "note": "acronym-alias redirection of the incremental path is not replicated",
    }
    if show_samples:
        out["split_examples"] = [
            [key, sorted(_bucket_set(entry))]
            for key, entry in old2new.items() if not isinstance(entry, str)
        ][:15]
        out["risky_merge_examples"] = [
            [key, sorted(_bucket_set(entry))]
            for key, entry in merge_only.items() if not isinstance(entry, str)
        ][:15]
    return out


# ============================================================ partition bench
def partition_bench(
    timer: PhaseTimer, *, n: int, raw: RawEdges, can2idx: Mapping[str, int],
    structure: GraphStructure, corpus: Any,
) -> dict:
    """Louvain / per-component Louvain / warm-started Leiden on the library graph, and how
    much a source deletion moves the partition (the S2 decision). Needs python-igraph."""
    import random

    import igraph as ig

    src = np.frombuffer(raw.src, dtype=np.int32).copy()
    dst = np.frombuffer(raw.dst, dtype=np.int32).copy()
    rel_src_ids = np.frombuffer(raw.source_rank, dtype=np.int32).copy()
    src_rank_rel = raw.ranks
    comp = structure.component
    giant = structure.giant_label
    src_ids = corpus.source_ids
    n_ingraph, n_giant, n_obj = corpus.in_graph, corpus.giant, corpus.objects
    giant_nodes = np.where(comp == giant)[0]
    idx2can: List[Optional[str]] = [None] * n
    for c, i in can2idx.items():
        idx2can[i] = c
    can_order = np.argsort(np.asarray(idx2can, dtype=object)).astype(np.int64)
    rank_of = np.empty(n, dtype=np.int64)
    rank_of[can_order] = np.arange(n)

    def seeded():
        random.seed(42)
        ig.set_random_number_generator(random)

    def product_graph(mask):
        """Replicates rebuild_communities: nodes numbered by first appearance in
        relation-id order, undirected unique edges (min, max) in first-seen order,
        weight = multiplicity."""
        s0, d0 = src[mask].astype(np.int64), dst[mask].astype(np.int64)
        nodes_seen = np.full(n, -1, dtype=np.int64)
        order = []
        for x in np.column_stack([s0, d0]).ravel().tolist():
            if nodes_seen[x] < 0:
                nodes_seen[x] = len(order)
                order.append(x)
        s, d = nodes_seen[s0], nodes_seen[d0]
        m = len(order)
        k = np.minimum(s, d) * m + np.maximum(s, d)
        uk, first, cnt = np.unique(k, return_index=True, return_counts=True)
        o = np.argsort(first, kind="stable")
        uk, cnt = uk[o], cnt[o]
        g = ig.Graph(n=m, edges=np.column_stack([uk // m, uk % m]))
        g.es["weight"] = cnt.tolist()
        return g, np.asarray(order)

    def run_A(mask):
        g, order = product_graph(mask)
        seeded()
        t = time.perf_counter()
        mem = np.asarray(g.community_multilevel(weights="weight").membership)
        dt = time.perf_counter() - t
        lab = np.full(n, -1, dtype=np.int64)
        lab[order] = mem
        return lab, dt, g.modularity(mem.tolist(), weights="weight")

    def sorted_graph(mask):
        s, d = rank_of[src[mask]], rank_of[dst[mask]]
        lo, hi = np.minimum(s, d), np.maximum(s, d)
        uk, cnt = np.unique(lo * n + hi, return_counts=True)
        g = ig.Graph(n=n, edges=np.column_stack([uk // n, uk % n]))
        g.es["weight"] = cnt.tolist()
        return g

    def run_B(mask):
        g = sorted_graph(mask)
        m = float(sum(g.es["weight"]))
        lab = np.full(n, -1, dtype=np.int64)
        t = time.perf_counter()
        nxt = 0
        for vs in g.connected_components():
            if len(vs) < 3:
                for v in vs:
                    lab[v] = nxt
                nxt += 1
                continue
            sub = g.induced_subgraph(sorted(vs))
            seeded()
            mem = sub.community_multilevel(
                weights="weight", resolution=sum(sub.es["weight"]) / m).membership
            for local, v in enumerate(sorted(vs)):
                lab[v] = nxt + mem[local]
            nxt += max(mem) + 1
        dt = time.perf_counter() - t
        out = np.full(n, -1, dtype=np.int64)
        out[can_order] = lab        # back from canonical-rank space to node-index space
        return out, dt, g

    def run_C(prev, mask):
        g = sorted_graph(mask)
        init = prev[can_order].copy()
        fresh = init < 0
        init[fresh] = init.max() + 1 + np.arange(int(fresh.sum()))
        _, init = np.unique(init, return_inverse=True)
        seeded()
        t = time.perf_counter()
        part = g.community_leiden(objective_function="modularity", weights="weight",
                                  initial_membership=init.tolist(), n_iterations=-1)
        dt = time.perf_counter() - t
        out = np.full(n, -1, dtype=np.int64)
        out[can_order] = np.asarray(part.membership)
        return out, dt, g.modularity(part.membership, weights="weight")

    def compare(a, b, nodes):
        la, lb = a[nodes], b[nodes]
        ari = ig.compare_communities(np.unique(la, return_inverse=True)[1].tolist(),
                                     np.unique(lb, return_inverse=True)[1].tolist(),
                                     method="adjusted_rand")
        key = la * (int(lb.max()) + 1) + lb
        _, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
        best: Dict[int, int] = {}
        for x, c in zip(la.tolist(), cnt[inv].tolist()):
            best[x] = max(best.get(x, 0), c)
        sz_a = Counter(la.tolist())
        boards = [x for x, s in sz_a.items() if s >= 3]
        moved = sum(sz_a[x] - best[x] for x in boards)
        return {"ARI": round(ari, 4), "moved_share": round(moved / max(1, len(nodes)), 4),
                "boards_ge3": len(boards)}

    rep: Dict[str, Any] = {}
    allmask = np.ones(src.size, dtype=bool)
    with timer.phase("11a_bench_base", product=False):
        A0, tA, qA = run_A(allmask)
        B0, tB, _ = run_B(allmask)
        C0, tC, qC = run_C(B0, allmask)
        C0, tC2, qC2 = run_C(C0, allmask)       # steady state
        rep["base"] = {"louvain_A_seconds": round(tA, 1), "modularity_A": round(qA, 4),
                       "per_component_B_seconds": round(tB, 1),
                       "leiden_warm_C_seconds": round(tC, 1),
                       "leiden_warm_C_steady_seconds": round(tC2, 1),
                       "modularity_C": round(qC2, 4)}
    gshare = np.where(n_ingraph > 0, n_giant / np.maximum(1, n_ingraph), 0)
    island = [r for r in np.argsort(-n_obj, kind="stable")
              if gshare[r] == 0 and n_ingraph[r] > 0][:1]
    inside = [r for r in np.argsort(-n_obj, kind="stable")
              if gshare[r] > 0.9][len(src_ids) // 20:len(src_ids) // 20 + 1]
    for label, picked in (("delete_island_source", island),
                          ("delete_giant_component_source", inside)):
        if not picked:
            continue
        rr = src_rank_rel.get(src_ids[picked[0]])
        mask = allmask if rr is None else rel_src_ids != rr
        with timer.phase(f"11b_{label}", product=False):
            A1, _, _ = run_A(mask)
            B1, _, _ = run_B(mask)
            C1, tC1, qC1 = run_C(C0, mask)
            nodes = giant_nodes[(A1[giant_nodes] >= 0) & (B1[giant_nodes] >= 0)
                                & (C1[giant_nodes] >= 0)]
            rep[label] = {"source_objects": int(n_obj[picked[0]]),
                          "relations_removed": int((~mask).sum()),
                          "A_product": compare(A0, A1, nodes),
                          "B_per_component": compare(B0, B1, nodes),
                          "C_warm_leiden": compare(C0, C1, nodes),
                          "C_warm_seconds": round(tC1, 1),
                          "C_modularity": round(qC1, 4)}
    return rep


# ============================================================== orchestration
@dataclass(frozen=True)
class Options:
    with_chunks: bool = False
    s1_impact: bool = False
    partition_bench: bool = False
    show_samples: bool = False
    max_candidate_pairs: int = DEFAULT_MAX_CANDIDATE_PAIRS
    j_floor: float = DEFAULT_J_FLOOR


def _state_section(rows: Sequence[tuple]) -> dict:
    (kg_seq, cluster_seq, community_seq, generation, dirty,
     objects, relations, clusters) = rows[0]
    return {
        "kg_mutation_seq": kg_seq, "cluster_mutation_seq": cluster_seq,
        "community_seq": community_seq, "cluster_generation": generation,
        "dirty": dirty, "last_rebuild_object_count": objects,
        "last_rebuild_relation_count": relations, "last_rebuild_cluster_count": clusters,
    }


def run_rehearsal(conn: Any, notebook_id: str, options: Options, timer: PhaseTimer) -> dict:
    """The whole rehearsal over one open (read-only) connection."""
    report: Dict[str, Any] = {
        "script": {
            "name": "kg_findings_rehearsal", "version": SCRIPT_VERSION,
            "rules_version": RULES_VERSION,
            "connection_read_only": bool(getattr(conn, "read_only", False)),
            "show_samples": options.show_samples,
        },
        "notebook": notebook_id,
        "thresholds": dict(RULE_THRESHOLDS),
    }
    notes: List[str] = [
        "phases with product_cost=false are the rehearsal's own measurement; read the "
        "product's time / RSS from the product_cost=true phases",
        "RSS from 4b_snapshot_for_distributions on includes a snapshot of the object "
        "tally (about 8 bytes per concept object) kept for the distribution pass",
        "the engine run (`engine`) keeps can2idx alive until the analysis is done; the "
        "product clears it before its lookup rounds",
    ]
    snapshot: Optional[FindingsObjectTally] = None

    with timer.phase("0_state", product=False):
        report["pg_version"] = fetch_all(
            conn, ("SELECT current_setting('server_version')", ())
        )[0][0]
        state = fetch_all(conn, state_query(notebook_id))
        if not state:
            raise RehearsalError("这个笔记本在 unified_kg_state 里没有行(未整理过?)")
        report["state"] = _state_section(state)
        conn.commit()

    with timer.phase("1_names_stream", product=True):
        names = {cid: name for cid, name in stream_rows(conn, names_query(notebook_id))}
        conn.commit()

    with timer.phase("2_graph_stream", product=True):
        graph = scan_graph(stream_rows(conn, graph_query(notebook_id)),
                           keep_raw=options.partition_bench)
        conn.commit()

    with timer.phase("3_structure", product=True):
        structure = GraphStructure.from_edges(
            graph.table.src, graph.table.dst, graph.table.n_nodes)
    unique_edges = len(graph.table)
    graph.table = None
    can2idx = graph.can2idx

    with timer.phase("4_object_stream", product=True):
        objects = scan_objects(stream_rows(conn, object_query(notebook_id)), can2idx, structure)
        conn.commit()

    with timer.phase("4b_snapshot_for_distributions", product=False):
        snapshot = snapshot_tally(objects.tally, can2idx)

    with timer.phase("5_decisions", product=True):
        decision_rows = list(stream_rows(conn, decisions_query(notebook_id)))
        decisions = decisions_from_rows(decision_rows)
        conn.commit()

    seed_calls: List[int] = []
    seed_reads = StoreReads(conn)

    def member_seeds(canonical_ids: Sequence[str]) -> Dict[str, Set[str]]:
        seed_calls.append(len(canonical_ids))
        return member_seeds_from_rows(seed_reads.member_name_rows(notebook_id, canonical_ids))

    with timer.phase("6_engine_compute", product=True):
        draft = compute_findings(
            tally=objects.tally, structure=structure, can2idx=can2idx, names=names,
            relations_by_source=graph.relations_by_source, decisions=decisions,
            member_seeds=member_seeds, max_candidate_pairs=options.max_candidate_pairs,
        )

    with timer.phase("7_engine_lookups", product=True):
        lookups = DbLookups(conn, notebook_id, with_titles=options.show_samples)
        result, lookup_counts = drive_draft(draft, lookups)
        conn.commit()
    del draft
    # statements the name reads sent (batched by canonical, never one per key)
    lookup_counts["member_seed_statements"] = seed_reads.statements
    lookup_counts["lookup_name_statements"] = lookups.reads.statements

    report["graph"] = graph_section(graph, structure, unique_edges)
    engine, engine_samples = engine_section(
        result, lookup_counts, len(decisions), seed_calls, show_samples=options.show_samples)
    report["engine"] = engine

    chunk_counts: Optional[Dict[str, int]] = None
    if options.with_chunks:
        with timer.phase("8_chunk_counts", product=False):
            chunk_counts = {sid: int(count) for sid, count
                            in stream_rows(conn, chunk_counts_query(notebook_id))}
            conn.commit()

    with timer.phase("9_analysis", product=False):
        corpus = kg_findings._drain_tally(snapshot, structure)
        analysis = analyze_corpus(
            corpus, structure=structure, can2idx=can2idx, names=names,
            relations_by_source=graph.relations_by_source,
            max_candidate_pairs=options.max_candidate_pairs, j_floor=options.j_floor,
            chunk_counts=chunk_counts, show_samples=options.show_samples,
        )
        report["objects"] = objects_section(corpus, objects)
        report.update(analysis.sections)
        report["f5"] = f5_section(corpus, graph)
    samples: Dict[str, Any] = {**analysis.samples, **engine_samples}

    if options.s1_impact:
        with timer.phase("10_s1_impact", product=False):
            report["s1_impact"] = s1_impact(
                (name for (name,) in stream_rows(conn, concept_names_query(notebook_id))),
                decision_rows, show_samples=options.show_samples)
            conn.commit()

    if options.partition_bench:
        report["partition_bench"] = partition_bench(
            timer, n=structure.n_nodes, raw=graph.raw, can2idx=can2idx,
            structure=structure, corpus=corpus)

    if options.show_samples:
        samples = _with_titles(conn, corpus, samples)
        report["samples"] = samples

    report["phases"] = timer.phases
    report["total_seconds"] = round(timer.elapsed, 1)
    report["rss_peak_mb"] = rss_mb()[0]
    report["notes"] = notes
    return report


def _with_titles(conn: Any, corpus: Any, samples: Dict[str, Any]) -> Dict[str, Any]:
    """``--show-samples`` only: attach titles to the source ids the samples name."""
    ids: Set[str] = set()
    for key in ("lowest_shared_sources", "most_fragmented_sources"):
        ids.update(item["source_id"] for item in samples.get(key, []))
    for pair in samples.get("top_duplicate_pairs", []):
        ids.update((pair["a"], pair["b"]))
    if not ids:
        return samples
    titles = {sid: title for sid, title
              in fetch_all(conn, source_titles_query(sorted(ids)))}
    conn.commit()
    for key in ("lowest_shared_sources", "most_fragmented_sources"):
        for item in samples.get(key, []):
            item["title"] = titles.get(item["source_id"])
    for pair in samples.get("top_duplicate_pairs", []):
        pair["title_a"], pair["title_b"] = titles.get(pair["a"]), titles.get(pair["b"])
    return samples


# ==================================================================== CLI
def _json_default(value: Any) -> Any:
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)


def to_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=1, default=_json_default)


def write_private(path: str, text: str) -> None:
    """The report may carry user data (``--show-samples``): owner-only file.
    The create mode only applies to a new file, so an existing target (say a
    0644 one) is narrowed to owner-only on the descriptor before any byte of
    the report reaches it."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only production rehearsal of the KG-analysis findings list (design "
            "2026-10-19 §14③): runs the product rule engine on ONE PostgreSQL notebook "
            "and reports the distributions the thresholds need. Never writes: every "
            "connection is default_transaction_read_only=on and only SELECTs are sent."
        )
    )
    parser.add_argument("--dsn", help="postgresql:// URL (never printed); "
                        "default: the environment variable named by --dsn-env")
    parser.add_argument("--dsn-env", default="DATABASE_URL")
    parser.add_argument("--notebook", required=True, help="notebook id, e.g. nb-xxxx")
    parser.add_argument("--with-chunks", action="store_true",
                        help="also GROUP BY chunks: objects per chunk (F3b's third signal)")
    parser.add_argument("--s1-impact", action="store_true",
                        help="also stream concept object names: what the S1 seed switch "
                             "(v2 seeds) changes in the existing seed space and decisions")
    parser.add_argument("--partition-bench", action="store_true",
                        help="also run the Louvain/Leiden partition benchmark "
                             "(heavy: CPU minutes, 1-2 GB RSS; needs python-igraph)")
    parser.add_argument("--show-samples", action="store_true",
                        help="include source titles / concept names / finding details in "
                             "the output. That is USER DATA: the default output has none")
    parser.add_argument("--max-candidate-pairs", type=int, default=DEFAULT_MAX_CANDIDATE_PAIRS,
                        help="F2 pair-generation cap (the product setting "
                             "KG_FINDINGS_MAX_CANDIDATE_PAIRS, default %(default)s)")
    parser.add_argument("--j-floor", type=float, default=DEFAULT_J_FLOOR,
                        help="run the F2 join once more at this Jaccard to measure the "
                             "distribution below the product threshold; a value >= the "
                             "product threshold (0.45) skips the extra run (default %(default)s)")
    parser.add_argument("--statement-timeout-seconds", type=int,
                        default=DEFAULT_STATEMENT_TIMEOUT_SECONDS,
                        help="session statement_timeout; the streamed full reads need "
                             "minutes (0 = unlimited, default %(default)s)")
    parser.add_argument("--out", help="write the JSON here (mode 0600) instead of stdout")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_candidate_pairs < 1 or args.statement_timeout_seconds < 0:
        parser.error("--max-candidate-pairs must be >= 1 and --statement-timeout-seconds >= 0")
    if not 0 < args.j_floor <= 1:
        parser.error("--j-floor must be in (0, 1]: a threshold of 0 makes every pair a candidate")
    options = Options(
        with_chunks=args.with_chunks, s1_impact=args.s1_impact,
        partition_bench=args.partition_bench, show_samples=args.show_samples,
        max_candidate_pairs=args.max_candidate_pairs, j_floor=args.j_floor,
    )
    try:
        dsn = resolve_dsn(args.dsn, args.dsn_env)
        conn = open_readonly_connection(
            dsn, statement_timeout_seconds=args.statement_timeout_seconds)
        try:
            report = run_rehearsal(conn, args.notebook, options, PhaseTimer())
        finally:
            conn.close()
    except RehearsalError as exc:
        print(f"kg_findings_rehearsal: {exc}", file=sys.stderr)
        return 2
    except psycopg.Error as exc:
        # The driver's text can carry the server address, a socket path or SQL
        # (codex #941 R4): only the class name and a fixed hint leave the process.
        print(f"kg_findings_rehearsal: {DATABASE_ERROR_HINT} ({type(exc).__name__})",
              file=sys.stderr)
        return 3
    text = to_json(report)
    if args.out:
        write_private(args.out, text)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

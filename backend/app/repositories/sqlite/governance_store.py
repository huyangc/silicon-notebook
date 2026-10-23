"""Governance persistence store (Task 13).

Owns the review / cluster / merge / conflict / promotion / whitelist and
knowledge-mutation primitives. Same composition rules as KnowledgeStore:
connection-taking primitives ride the FACADE's transaction boundary (frozen
``_write`` trace patches and SQL-matching failure injectors keep observing
every statement); only the plan-frozen ``get_conflict_candidate`` /
``decided_pairs`` / ``decided_seed_pairs`` readers open their own connection.
SQL text is moved verbatim from the facade.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.text_whitespace import PY_STRIP_WHITESPACE
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.knowledge_store import KnowledgeStore
from app.repositories.sqlite.memory_sql import (
    cluster_seed_object_id,
    foreign_memory_object_excluded,
    foreign_memory_relation_excluded,
    memory_derived_object,
    hidden_type_index_term,
    memory_derived_relation,
    memory_source_type_predicate,
    own_memory_source,
)
from app.repositories.sqlite.id_binding import (
    bind_ids as id_bind_ids,
    drive_by as id_drive_by,
    member_of as id_member_of,
    not_member_of as id_not_member_of,
)
from app.repositories.sqlite.mount_sql import MOUNT_JOIN, MOUNT_ORDER
from app.repositories.sqlite.promotion_provenance_store import (
    materialize_promotion_evidence,
)
from app.domain.kg_merge_seed import (
    CONCEPT_SEED_VERSION, check_concept_seed_version, check_placement_seed_space,
    fold_decision, stronger_decision,
)
from app.domain.knowledge_contracts import (
    KNOWLEDGE_STATUSES,
    USABLE_STATUSES,
    PromotionApproval,
)
from app.domain.memory_kg_isolation import (
    PROMOTION_APPROVE_MESSAGE,
    MemoryKnowledgeMergeRefused,
    NeedsKnowledgeWrite,
    OWN_STATUS_TOGGLE,
    MemoryPromotionRefused,
    PromotionObjectMissing,
    FOLLOW_REPOINT,
    FOLLOW_RESTORE,
    ABSORBS_PAYLOAD_KEY,
    MERGED_INTO_PAYLOAD_KEY,
    MemoryMergeDetach,
    MergeFollowAction,
    followed_payload,
    marked_absorbs,
    marked_merged_into,
    heal_action,
    patched_payload,
    merge_evidence_source_ids,
    memory_merge_refusal,
    plan_merge_follow_actions,
)

_REVIEW_STATUSES = frozenset({"pending", "verified", "rejected"})

#: Memory sources per objects statement of the merge-heal backstop read.
_HEAL_SOURCE_BATCH = 200


def _only_own_memory_evidence_sql(alias: str) -> str:
    """Every evidence entry of ``alias`` cites one of the viewer's own Memory
    sources of the notebook: the merge's ``only_own_memory_evidence`` (via
    ``evidence_only_from_own_memory``) in SQL, for the owner's duplicate
    groups. Two parameters, in order: notebook id, viewer id. An entry that is
    not an object or has no non-empty ``source_id`` fails it; ``NULL``, ``''``,
    ``[]``, ``{}`` and ``""`` pass (the Python reading iterates nothing). Other
    shapes (a non-empty string or object, a number, invalid JSON) fail: the
    Python reading refused them or could not read them at all."""
    own = (
        "SELECT es.id FROM sources es WHERE es.notebook_id = ? "
        f"AND {hidden_type_index_term('es')} AND {own_memory_source('es')}"
    )
    e = f"{alias}.evidence"
    return (
        f"({e} IS NULL OR {e} = '' OR (json_valid({e}) AND ("
        f"(json_type({e}) = 'array' AND NOT EXISTS (SELECT 1 FROM json_each({e}) "
        "WHERE NOT (json_each.type = 'object' "
        "AND COALESCE(json_extract(json_each.value, '$.source_id'), '') <> '' "
        f"AND json_extract(json_each.value, '$.source_id') IN ({own}))))"
        f" OR (json_type({e}) = 'object' AND NOT EXISTS (SELECT 1 FROM json_each({e})))"
        f" OR (json_type({e}) = 'text' AND json_extract({e}, '$') = ''))))"
    )
# Row ids per statement when the decision recompute re-reads its v1 rows
# (``apply_v1_decision_translation``); a page size, not a cap.
_TRANSLATION_BATCH = 500

# Pagination width for the review queue's endpoint-object lookup — a page size,
# not a cap: every endpoint id is fetched.  500 is the store-wide convention
# (``_DELETE_OBJECT_BATCH_SIZE`` / ``CHUNK_ELEMENT_LOOKUP_BATCH``).
_REVIEW_ENDPOINT_LOOKUP_BATCH = 500

# How many ``concept_clusters.id`` values one orphan-sweep DELETE may carry.
# The sweep's PAGE is wider (``_ORPHAN_SWEEP_BATCH_SIZE``, tuned for round
# trips); PostgreSQL passes the whole page as a single ``= ANY(%s)`` array, but
# SQLite has to expand one placeholder per id, so a page is cut into slices at
# the store-wide expanded-``IN`` ceiling rather than binding thousands of
# parameters in one statement.  Cost is unchanged: ⌈page/chunk⌉ statements,
# each ≤ chunk primary-key probes.
_SWEEP_DELETE_ID_CHUNK = 500


def _review_endpoint_ids(relation_rows) -> List[str]:
    """Distinct object ids appearing on either end of the given relations,
    in first-seen order.  Order only has to be deterministic: the caller folds
    the rows into ``{id: …}`` dicts."""
    seen: set = set()
    ordered: List[str] = []
    for row in relation_rows:
        for object_id in (row["source_object_id"], row["target_object_id"]):
            if object_id and object_id not in seen:
                seen.add(object_id)
                ordered.append(object_id)
    return ordered


def seed_fn_for(object_type: str, *, concept_seed_version: int,
                override: frozenset = frozenset()):
    """Return the kg_merge seed function for a KG object type, concepts in the
    notebook's published seed space with its override keys
    (``kg_merge_seed.seed_fn_for_type``)."""
    from app.domain.kg_merge_seed import seed_fn_for_type
    return seed_fn_for_type(object_type, concept_seed_version=concept_seed_version,
                            override=override)


def _strip_concept_prefix(cid: str) -> str:
    return cid[2:] if cid.startswith("K-") else cid


def _decision_keys(r) -> tuple:
    """A decided row's decision keys: its seed, else the canonical id without
    ``K-`` — the derivation ``decided_seed_pairs_from`` uses."""
    return (
        r["seed_a"] or _strip_concept_prefix(r["canonical_a"]),
        r["seed_b"] or _strip_concept_prefix(r["canonical_b"]),
    )


def _decision_row(r) -> dict:
    """A decided row's id, decision keys (``_decision_keys``) and status."""
    key_a, key_b = _decision_keys(r)
    return {"id": r["id"], "key_a": key_a, "key_b": key_b, "status": r["status"]}


def published_concept_seed_version_on(connection, notebook_id: str) -> int:
    """The concept seed space of ``notebook_id``'s published merge
    (``unified_kg_state.concept_seed_version``; no row reads as
    ``CONCEPT_SEED_VERSION``, the column default), read in the caller's
    transaction."""
    from app.domain.kg_merge_seed import published_concept_seed_version
    row = connection.execute(
        "SELECT concept_seed_version FROM unified_kg_state WHERE notebook_id=?",
        (notebook_id,),
    ).fetchone()
    return published_concept_seed_version(row)


def published_concept_seed_space_on(connection, notebook_id: str) -> "tuple[int, frozenset]":
    """``(published concept seed version, override keys)`` of ``notebook_id``
    in the caller's transaction: the override keys are the v1 seeds its v2
    decisions keep as ``~v1:<seed>`` (none in space 1). The decision read is
    bounded by the notebook's decided rows (``idx_candidates_nb_status``)."""
    from app.domain.kg_merge_seed import OVERRIDE_SEED_PREFIX
    version = published_concept_seed_version_on(connection, notebook_id)
    if version == 1:
        return 1, frozenset()
    rows = connection.execute(
        "SELECT seed_a, seed_b FROM concept_merge_candidates WHERE notebook_id=? "
        "AND status IN ('confirmed','rejected','deferred') AND seed_version=? "
        "AND (seed_a LIKE '~v1:%' OR seed_b LIKE '~v1:%')",
        (notebook_id, version),
    ).fetchall()
    width = len(OVERRIDE_SEED_PREFIX)
    return version, frozenset(
        seed[width:] for row in rows for seed in (row["seed_a"], row["seed_b"])
        if seed and seed.startswith(OVERRIDE_SEED_PREFIX)
    )


def base_dedup_evidence(connection: sqlite3.Connection, object_id: str) -> list:
    """Evidence of the ONE base object a promotion deduped onto.

    The dedup corpus read (``SELECT id,payload …`` over a whole object type of a
    base notebook) deliberately no longer carries ``evidence``: seed matching
    reads only ``payload``, so the evidence of every non-matching row was pulled
    into Python for nothing — and evidence is the fattest column of the three.
    At most one row is ever merged into, so it is re-read here by primary key,
    inside the same transaction that already read (PostgreSQL: locked FOR
    UPDATE) that row, which keeps the value identical to the one the old
    corpus-wide read would have handed back.
    """
    row = connection.execute(
        "SELECT evidence FROM knowledge_objects WHERE id=?", (object_id,)
    ).fetchone()
    return json.loads((row["evidence"] if row is not None else None) or "[]")


def find_base_dedup_match(
    object_type: str, src_payload: dict, base_objs: List[sqlite3.Row], *,
    concept_seed_version: int,
    override: frozenset = frozenset(),
) -> str:
    """Exact-seed dedup (v1): return the id of an existing base object whose
    normalized seed matches the source payload, else ''. This works at cold
    start without vectors (the plan's v1 shortcut). Concepts are compared in the
    BASE notebook's published seed space (``concept_seed_version``), the space its
    clusters were built in, with its override keys)."""
    seed_fn = seed_fn_for(object_type, concept_seed_version=concept_seed_version,
                          override=override)
    src_seed = seed_fn({"name": src_payload.get("name", ""), "payload": src_payload})
    if not src_seed:
        return ""
    for b in base_objs:
        bp = json.loads(b["payload"] or "{}")
        b_seed = seed_fn({"name": bp.get("name", ""), "payload": bp})
        if b_seed and b_seed == src_seed:
            return b["id"]
    return ""


def merge_evidence_lists(base_ev: list, src_ev: list) -> list:
    """Union two evidence lists, deduping on (source_id, element_id, quoted_span)."""
    seen = set()
    merged: list = []
    for ev in [*base_ev, *src_ev]:
        if not isinstance(ev, dict):
            continue
        key = (ev.get("source_id"), ev.get("element_id"), ev.get("quoted_span"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(ev)
    return merged


def require_live_promotion_target(connection: sqlite3.Connection, base_nb_id: str) -> None:
    """最终整支审查 BLOCKER 2:approve 阶段(而非 propose 阶段)复核目标笔记本
    仍然存在且仍是公共知识库。

    propose 时的挂载校验(knowledge_governance._resolve_promotion_target /
    mounted_public_base_ids)只是提交那一刻的快照——审批往往发生在数天后,期间
    目标可能被降级(tier='base'→'personal')、转让给别人,或者整个删除。写入侧
    此前只检查 target_base_id 非空,不复核目标行本身,于是:
      - 降级场景:知识对象被悄悄写进降级后的个人笔记本(数据破坏,且是静默的)
      - 删除场景:INSERT INTO knowledge_objects 撞 FOREIGN KEY 约束,裸
        sqlite3.IntegrityError 冒泡成 500(而不是一个操作者看得懂的错误)
    两处 approve_*_in_transaction 共用同一个校验,在写入前、同一个事务内调用,
    避免两份判定漂移(呼应 mount_sql.py 顶部"谓词只在一处定义"的既有原则)。

    刻意不检查挂载是否仍然生效——挂载是检索关系,不是晋升授权;取消挂载后仍
    允许晋升是设计上的既定行为(governance_store 顶部/spec §6),这次修复不能
    把它也拦掉。

    调用位置是契约的一部分:两处调用点都必须放在各自的"已批准"幂等早返回
    **之后**,只守真正会写数据的路径。放在幂等分支前面看似更"保险",实际是
    回归——会把"重试一个已经批准过的候选"变成硬失败(目标批准后才被降级,
    或 _migration_20 未回填的存量行 target_base_id=''),而重试对一个已完成
    的操作理应是无副作用的空操作。见 codex 对 PR#304 的审查(2026-07-19)。"""
    row = connection.execute(
        "SELECT tier FROM notebooks WHERE id=?", (base_nb_id,)
    ).fetchone()
    if row is None or (row["tier"] or "personal") != "base":
        raise ValueError(
            "晋升目标笔记本已不是公共知识库(可能已被删除或降级为个人库): "
            f"{base_nb_id}；请撤回候选后重新指定晋升目标，或联系管理员重新发布该库"
        )


# 一个合并候选某一侧的**有效种子**(`decided_seed_pairs_from` 的同一条推导):seed 列
# 非空就是它,否则 canonical 去掉 ``K-`` 前缀(``substr`` 起止两侧语法相同,PG 孪生
# 逐字同一段文本)。``{side}`` 是 ``a`` / ``b``。
_EFFECTIVE_SEED_SQL = (
    "(CASE WHEN COALESCE(seed_{side}, '') <> '' THEN seed_{side} "
    "WHEN substr(canonical_{side}, 1, 2) = 'K-' THEN substr(canonical_{side}, 3) "
    "ELSE canonical_{side} END)"
)


def _lifecycle_merge_status_sql(statuses) -> str:
    """``merge_candidate_states_for_seeds`` 的状态谓词:与
    `merge_candidate_pairs_for_canonicals` 同一套 deny-by-default 取值(固定字面量,
    不绑定参数)。"""
    values = tuple(statuses)
    if values == ("pending",):
        return "status='pending'"
    if values == ("confirmed", "rejected", "deferred"):
        return "status IN ('confirmed','rejected','deferred')"
    if values == ("confirmed", "rejected", "deferred", "pending"):
        return "status IN ('confirmed','rejected','deferred','pending')"
    raise ValueError(f"unsupported lifecycle merge statuses: {values!r}")


class GovernanceStore:
    def __init__(self, database: SqliteDatabase, seams) -> None:
        self.database = database
        self.seams = seams

    @staticmethod
    def seed_for(object_type: str, *, concept_seed_version: int):
        return seed_fn_for(object_type, concept_seed_version=concept_seed_version)

    @staticmethod
    def published_concept_seed_space(
        connection, notebook_id: str, *, override_keys: bool = True,
    ) -> "tuple[int, frozenset]":
        """``(version, override keys)`` of the notebook's published merge, in
        the caller's transaction: ``unified_kg_state.concept_seed_version`` (no
        state row = ``CONCEPT_SEED_VERSION``; one primary-key read) and, in
        space 2 only, the v1 seeds its stored decisions keep as ``~v1:<seed>``
        (one read of the decided rows that carry such a seed, through
        ``idx_candidates_nb_status``). ``override_keys=False``: the version
        alone (empty keys), for a caller that does not place or seed."""
        if not override_keys:
            return published_concept_seed_version_on(connection, notebook_id), frozenset()
        return published_concept_seed_space_on(connection, notebook_id)

    @staticmethod
    def find_base_match(
        object_type: str, payload: dict, rows, *, concept_seed_version: int,
        override: frozenset = frozenset(),
    ) -> str:
        return find_base_dedup_match(
            object_type, payload, rows, concept_seed_version=concept_seed_version,
            override=override)

    @staticmethod
    def merge_evidence(base_evidence: list, source_evidence: list) -> list:
        return merge_evidence_lists(base_evidence, source_evidence)

    # ------------------------------------------------ lifecycle projections
    @staticmethod
    def sweep_orphan_clusters_page(
        db: sqlite3.Connection,
        notebook_id: str,
        after_object_type: str,
        after_member_object_id: str,
        after_generation: int,
        limit: int,
    ) -> "tuple[list, int]":
        """Z6: SQLite twin of the PostgreSQL keyset batch — see that adapter's
        docstring for the full cost model and rationale, including the measured
        reason ② is driven off the page's PRIMARY KEYS rather than re-expressed
        as a key range (① a pure keyset page read bounded by ``LIMIT``, so the
        SCANNED rows — not just the deleted ones — are what the batch size caps;
        ② a delete over at most one page of ``id``s, one ``NOT EXISTS``
        primary-key probe per row; both statements O(page); notebook-scoped
        ``k.notebook_id = c.notebook_id`` preserving the ORIGINAL semantics
        bit-for-bit).

        The one shape difference from the PostgreSQL twin: no ``= ANY(?)`` in
        SQLite, so the id list is spelled as an expanded ``IN (?,?,…)`` and cut
        into ``_SWEEP_DELETE_ID_CHUNK`` slices — same ceiling the rest of the
        SQLite adapters use for expanded id lists (``_DELETE_OBJECT_BATCH_SIZE``
        / ``_IN_CHUNK``), so one page never turns into a statement with
        thousands of bound parameters. Each slice is still ≤ the chunk and the
        page total is still O(page).

        Returns ``(page_rows, deleted_count)`` — the caller advances its cursor
        over the SCANNED page and stops on a short page, so a batch that
        deletes nothing (the common case) still makes progress."""
        page = db.execute(
            "SELECT object_type, member_object_id, generation, id FROM concept_clusters"
            " WHERE notebook_id = ?"
            "   AND (object_type, member_object_id, generation) > (?, ?, ?)"
            " ORDER BY object_type, member_object_id, generation"
            " LIMIT ?",
            (notebook_id, after_object_type, after_member_object_id,
             after_generation, limit),
        ).fetchall()
        if not page:
            return [], 0
        cluster_ids = [row["id"] for row in page]
        deleted = 0
        for start in range(0, len(cluster_ids), _SWEEP_DELETE_ID_CHUNK):
            chunk = cluster_ids[start : start + _SWEEP_DELETE_ID_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            deleted += len(db.execute(
                "DELETE FROM concept_clusters AS c"
                " WHERE c.notebook_id = ?"
                f"   AND c.id IN ({placeholders})"
                "   AND NOT EXISTS ("
                "     SELECT 1 FROM knowledge_objects k"
                "     WHERE k.id = c.member_object_id AND k.notebook_id = c.notebook_id"
                "   )"
                # ⚠ 裸 `id`,不是 `c.id`:SQLite 的 RETURNING 子句里看不见 DELETE 目标的
                # 别名(`no such column: c.id`),而 WHERE 里看得见。PG 侧写成同一形态。
                " RETURNING id",
                (notebook_id, *chunk),
            ).fetchall())
        return page, deleted

    @staticmethod
    def incremental_cluster_rows(db: sqlite3.Connection, notebook_id: str, object_type: str):
        # 批 3·W2 A 类:published 代谓词,理由见 PG 孪生。标量子查询用绑定
        # 参数(非相关引用)——SQLite 侧同样一次求值。
        return db.execute(
            "SELECT DISTINCT canonical_id, canonical_name FROM concept_clusters "
            "WHERE notebook_id=? AND object_type=? "
            "AND generation = COALESCE((SELECT cluster_generation "
            "FROM unified_kg_state WHERE notebook_id = ?), 0)",
            (notebook_id, object_type, notebook_id),
        ).fetchall()

    # 测试观察点:整库候选对读取,有界版按它对照;用例也借它断言生产不再整库扫描
    @staticmethod
    def merge_candidate_pairs(db: sqlite3.Connection, notebook_id: str, statuses):
        values = tuple(statuses)
        if values == ("pending",):
            return db.execute(
                "SELECT canonical_a, canonical_b FROM concept_merge_candidates "
                "WHERE notebook_id=? AND status='pending'", (notebook_id,),
            ).fetchall()
        if values == ("confirmed", "rejected", "deferred"):
            return db.execute(
                "SELECT canonical_a, canonical_b FROM concept_merge_candidates "
                "WHERE notebook_id=? AND status IN ('confirmed','rejected','deferred')",
                (notebook_id,),
            ).fetchall()
        if values == ("confirmed", "rejected", "deferred", "pending"):
            return db.execute(
                "SELECT canonical_a, canonical_b FROM concept_merge_candidates "
                "WHERE notebook_id=? AND status IN ('confirmed','rejected','deferred','pending')",
                (notebook_id,),
            ).fetchall()
        raise ValueError(f"unsupported lifecycle merge statuses: {values!r}")

    @staticmethod
    def merge_candidate_pairs_for_canonicals(
        db: sqlite3.Connection, notebook_id: str, statuses, canonical_ids
    ):
        """``merge_candidate_pairs`` 的有界形态:只回**至少一端**落在
        ``canonical_ids`` 里的那些对(PR-C)。

        调用方(增量融合的 Tier2 桥接)拿这份结果只做一件事:判 ``frozenset((a,b))
        in exclude``,而被判的每一对都恒含本次新对象的某个 canonical id。因此把行
        限制到「一端命中」是这个消费方的**超集**,判定结果逐位一致;省下的是把整
        本库的候选对搬进 Python frozenset 集合。

        ``canonical_ids`` 必须已由调用方分批(``concept_merge_candidates`` 上只有
        ``idx_candidates_nb_status``,canonical_a/b 无索引,两种写法都是 notebook
        切片内的残余过滤;写成一条 OR 是为了**只扫一遍**而不是两遍)。"""
        # statuses 校验在**空 id 早退之前**:调用方分批,空批是完全正常的输入,
        # 若先早退,一个拼错的 statuses 就只在「恰好这一批非空」时才炸 —— 与
        # `merge_candidate_pairs` 的 deny-by-default 口径分叉。
        values = tuple(statuses)
        if values == ("pending",):
            status_sql = "status='pending'"
        elif values == ("confirmed", "rejected", "deferred"):
            status_sql = "status IN ('confirmed','rejected','deferred')"
        elif values == ("confirmed", "rejected", "deferred", "pending"):
            status_sql = "status IN ('confirmed','rejected','deferred','pending')"
        else:
            raise ValueError(f"unsupported lifecycle merge statuses: {values!r}")
        ids = list(dict.fromkeys(canonical_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        return db.execute(
            f"SELECT canonical_a, canonical_b FROM concept_merge_candidates "
            f"WHERE notebook_id=? AND {status_sql} "
            f"AND (canonical_a IN ({placeholders}) OR canonical_b IN ({placeholders}))",
            [notebook_id, *ids, *ids],
        ).fetchall()

    @staticmethod
    def merge_candidate_states_for_seeds(
        db: sqlite3.Connection, notebook_id: str, statuses, seeds
    ) -> List[Dict[str, str]]:
        """图谱分析 F4 读侧派生「已处理 / 已判定不是同一个」用的候选状态(设计
        2026-10-19 §3.5 / §4.4):至少一端的**有效种子**落在 ``seeds`` 里的那些候选,
        每行 ``{id, canonical_a, canonical_b, status, seed_a, seed_b, updated_at}``,按
        ``id`` 序。

        有效种子 = ``seed_x`` 非空时就是它,否则回退到 canonical 去掉 ``K-`` 前缀 ——
        与 `decided_seed_pairs_from` 逐字同一条推导(`_EFFECTIVE_SEED_SQL`)。按种子而不是
        按 canonical id 过滤:canonical id 随簇的最小成员漂,两侧 id 都已漂移的决定按 id
        永远匹配不上,驳回过的组会一直停在「待处理」。

        排序在 Python 里做,SQL 里**刻意不写** ``ORDER BY id``:有了它,SQLite 3.45
        (CI 的版本)在生产形态的统计下会改走主键自动索引按 id 序扫**全库**候选,把
        「本库这几种状态的切片」整个丢掉(3.53 不会);结果集被 ``seeds`` 与切片双重
        限定,在内存里排一次是有界的。PG 孪生同样不在 SQL 里排,两侧形态一致。

        ``concept_merge_candidates`` 只有 ``idx_candidates_nb_status``(seed / canonical
        列无索引),所以这不是主键点查:语句由本库、这几种状态的候选切片驱动,种子列表只在
        切片里过滤 —— ``id_binding.member_of``(``+expr IN``,一个 JSON 参数,不随列表
        长度长出变量;两端共用同一个绑定,只扫一遍切片)。

        ``statuses`` 与 `merge_candidate_pairs_for_canonicals` 同一套 deny-by-default
        取值;校验在空列表早退之前。"""
        status_sql = _lifecycle_merge_status_sql(statuses)
        values = list(dict.fromkeys(str(seed) for seed in seeds if seed))
        if not values:
            return []
        bound = id_bind_ids(values, sort=True)
        rows = db.execute(
            "SELECT id, canonical_a, canonical_b, status, seed_a, seed_b, updated_at "
            f"FROM concept_merge_candidates WHERE notebook_id=? AND {status_sql} "
            f"AND ({id_member_of(_EFFECTIVE_SEED_SQL.format(side='a'), bound)} "
            f"OR {id_member_of(_EFFECTIVE_SEED_SQL.format(side='b'), bound)})",
            (notebook_id, bound.param, bound.param),
        ).fetchall()
        return sorted((
            {
                "id": row["id"],
                "canonical_a": row["canonical_a"],
                "canonical_b": row["canonical_b"],
                "status": row["status"],
                "seed_a": row["seed_a"] or "",
                "seed_b": row["seed_b"] or "",
                "updated_at": str(row["updated_at"] or ""),
            }
            for row in rows
        ), key=lambda row: row["id"])

    @staticmethod
    def valid_object_ids(
        db: sqlite3.Connection, object_ids, *, exclude_memory_derived: bool = False
    ):
        return KnowledgeStore.valid_object_ids(
            db, object_ids, exclude_memory_derived=exclude_memory_derived
        )

    # ------------------------------------------------------------- review
    @staticmethod
    def review_queue_rows(
        connection: sqlite3.Connection, notebook_id: str
    ) -> "tuple[List[sqlite3.Row], List[sqlite3.Row]]":
        """Relations (with a pushed-down anchor flag) + only the objects the
        queue actually reads.

        Two deliberate narrowings, both lossless for the one consumer
        (``knowledge_governance.review_queue``):

        * ``evidence`` is no longer selected.  Its ONLY use was
          ``evidence_anchor_score`` — a boolean over the array — so the
          predicate is evaluated in SQL and returned as ``has_anchor`` instead
          of shipping every non-rejected edge's evidence JSON into Python to be
          re-parsed.  ``PY_STRIP_WHITESPACE`` supplies the exact ``str.strip()``
          alphabet so the SQL trim matches the Python one character for
          character; the nested json_valid/json_type CASEs mirror
          ``KnowledgeStore._object_ids_for_source_batch`` (json_each must be
          handed a valid top-level array, and json_extract must never see a bare
          JSON string element).
        * objects are read for the RELATION ENDPOINTS only, instead of scanning
          every object payload in the notebook.  The queue builds
          ``node_types``/``node_names`` from these rows and looks up only
          ``rel["source_object_id"]``/``["target_object_id"]``, so isolated
          objects — of which a real notebook has many — were pure read
          amplification.  Endpoint ids come from the relation rows already in
          hand, so this adds no second pass over knowledge_relations.

          ⚠ The endpoint lookup issues a BARE ``id IN (...)`` and compares
          ``notebook_id`` in Python.  This repository never runs ``ANALYZE`` on
          production databases, and without statistics the planner reads
          ``WHERE notebook_id=? AND id IN (...)`` as
          ``idx_knowledge_objects_nb_* (notebook_id=?)`` — i.e. it walks EVERY
          object of the notebook once PER BATCH, which is worse than the
          full-notebook scan this narrowing set out to remove (measured on a
          200k-row database: 0.138s → 14.155s).  A bare ``id IN (...)`` takes
          ``sqlite_autoindex_knowledge_objects_1 (id=?)`` with or without
          statistics.  Same recipe and same measured reason as
          ``query_store.notebook_source_ids`` and
          ``maintenance.chunk_texts_by_ids``.  The judgement is unchanged — same
          column, same value, only the evaluation site moves — and there is no
          "read a lot and then filter" risk because the result set is capped by
          the id list.  ``notebook_id`` therefore has to be PROJECTED so the
          caller can apply it; a cross-notebook endpoint stayed untyped/unnamed
          before and still does.  Pinned by
          ``test_governance_read_narrowing.py::test_endpoint_lookup_sql_keeps_no_notebook_predicate``
          — a behaviour test cannot see this (fixture-scale EXPLAIN does not
          reproduce the regression), which is exactly how it slipped through
          review once.

        * R3 T-A1: the endpoint projection extracts
          ``json_extract(payload, '$.name')`` in SQL instead of shipping the
          whole ``payload`` document.  The consumer reads exactly two fields off
          these rows — ``object_type`` and ``payload["name"]`` — so every other
          key of every endpoint object's payload was pure transfer/parse cost
          (~500k endpoint rows on the largest production notebook).
          ``json_extract`` yields SQL NULL for a missing key, a JSON ``null`` or
          a non-object payload, and the caller normalises NULL to ``""`` — what
          the old ``dict.get("name", "")`` produced for the first two.
          Registered robustness change (same class as the anchor pushdown's): a
          NON-STRING ``name`` (e.g. a number) used to reach ``edge_trust._norm``
          as an ``int`` and raise a 500; the caller's ``str()`` coercion now
          renders it text.  That text matches the PostgreSQL twin's ``->>``
          ONLY for a string or an integer ``name`` (registered narrowing, codex
          R3 double review) — a JSON bool comes back as SQLite integer 0/1 (so
          ``str()`` yields "0"/"1"), where PostgreSQL's ``->>`` renders
          "false"/"true"; a float can differ in trailing-zero/exponent
          formatting between Python's ``repr`` and PostgreSQL's numeric text;
          and an object/array's compact-JSON serialization is not
          byte-identical across the two engines.  None of that is a regression
          (the OLD path raised on both dialects for every one of those shapes),
          but a non-string/non-integer ``name`` can leave cross-dialect
          corroboration counts and the displayed name different — registered,
          not guarded against.
          A malformed ``payload`` still fails the request on both paths (here
          ``json_extract`` raises, before it was the caller's ``json.loads``);
          no ``json_valid`` guard is added, because widening that shape is a
          behaviour change this equivalence-only narrowing does not carry.
          Same non-guard for an EMPTY-STRING ``payload``: the old path's
          ``json.loads(row["payload"] or "{}")`` tolerated it, but
          ``json_extract('', ...)`` raises.  Not reachable in practice — the
          column is ``payload TEXT NOT NULL DEFAULT '{}'`` and every write path
          serializes through ``json.dumps`` — so this is registered, not
          guarded against (widening it would be the same kind of behaviour
          change as the ``json_valid`` guard above).

          ⚠ ``name`` deliberately does NOT move into the relation JOIN — see the
          PostgreSQL twin's docstring for the read-amplification and
          cross-notebook-naming reasons.
        """
        relations = connection.execute(
            "SELECT kr.id, kr.source_object_id, kr.target_object_id, "
            "kr.edge_type, kr.source_id, kr.review_status, "
            "EXISTS (SELECT 1 FROM json_each("
            "  CASE WHEN json_valid(kr.evidence) THEN "
            "    CASE WHEN json_type(kr.evidence) = 'array' "
            "    THEN kr.evidence ELSE '[]' END "
            "  ELSE '[]' END) AS ev "
            " WHERE ev.type = 'object' "
            "   AND json_type(CASE WHEN ev.type = 'object' THEN ev.value "
            "                      ELSE '{}' END, '$.quote') = 'text' "
            "   AND trim(json_extract(CASE WHEN ev.type = 'object' THEN ev.value "
            "                              ELSE '{}' END, '$.quote'), ?) <> ''"
            ") AS has_anchor, "
            "ko_s.object_type AS src_type, ko_t.object_type AS tgt_type "
            "FROM knowledge_relations kr "
            "LEFT JOIN knowledge_objects ko_s ON ko_s.id = kr.source_object_id "
            "LEFT JOIN knowledge_objects ko_t ON ko_t.id = kr.target_object_id "
            "WHERE kr.notebook_id = ? AND kr.review_status != 'rejected' "
            # M1: Memory-derived relations are left out WHOLE (not per viewer:
            # the queue memo is per notebook). Only their owner may review
            # them, so for everyone else they would be items that can never
            # be acted on; nor do they corroborate shared edges.
            f"AND NOT {memory_derived_relation('kr')}",
            (PY_STRIP_WHITESPACE, notebook_id),
        ).fetchall()

        endpoint_ids = _review_endpoint_ids(relations)
        objects: List[sqlite3.Row] = []
        for offset in range(0, len(endpoint_ids), _REVIEW_ENDPOINT_LOOKUP_BATCH):
            batch = endpoint_ids[offset : offset + _REVIEW_ENDPOINT_LOOKUP_BATCH]
            placeholders = ",".join("?" for _ in batch)
            objects.extend(
                row for row in connection.execute(
                    "SELECT id, object_type, "
                    "json_extract(payload, '$.name') AS name, notebook_id "
                    f"FROM knowledge_objects WHERE id IN ({placeholders})",
                    tuple(batch),
                ).fetchall()
                if row["notebook_id"] == notebook_id
            )
        return relations, objects

    @staticmethod
    def update_edge_review(
        connection: sqlite3.Connection, notebook_id: str, relation_id: str, status: str,
        *, actor_id: str | None = None,
    ) -> str:
        """Set ``review_status`` and return the PREVIOUS value (R3 T-A3 P1-2,
        mirrors the PostgreSQL sibling above) — same-transaction SELECT then
        UPDATE. sqlite has no ``UPDATE...FROM...RETURNING`` construct that
        yields a pre-update column in one statement, but the caller
        (``set_edge_review``) already holds the writer lock via ``_write``
        before either statement runs, so there is no window for a concurrent
        writer to slip a second UPDATE between this SELECT and this UPDATE —
        the two together are as atomic as the PostgreSQL single statement.

        Deliberately does NOT validate ``status`` against ``_REVIEW_STATUSES``
        (unlike the PostgreSQL sibling) — that asymmetry predates this change
        and stays: the allowed-status set and error behavior are unchanged by
        this fix, only the return value is new.

        M1 (E4-3): a relation derived from someone else's Memory (not
        ``actor_id``'s; an orphan Memory source is nobody's) is filtered out
        in the read (``foreign_memory_relation_excluded``), so it takes the
        identical "not found" path as an id that does not exist — no
        existence probe, no write. ``actor_id=None`` owns no Memory."""
        row = connection.execute(
            "SELECT kr.review_status FROM knowledge_relations kr "
            "WHERE kr.id=? AND kr.notebook_id=? "
            f"AND {foreign_memory_relation_excluded('kr')}",
            (relation_id, notebook_id, actor_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"relation {relation_id!r} not found in notebook {notebook_id!r}")
        prev_status = row["review_status"]
        connection.execute(
            "UPDATE knowledge_relations SET review_status=? "
            "WHERE id=? AND notebook_id=?",
            (status, relation_id, notebook_id),
        )
        return prev_status

    # ------------------------------------------------------------ clusters
    @staticmethod
    def delete_clusters(
        connection: sqlite3.Connection, notebook_id: str, object_type: str
    ) -> None:
        # 在飞代豁免(codex #671 R9 P1)——完整论证见 PG 孪生。
        connection.execute(
            "DELETE FROM concept_clusters WHERE notebook_id=? AND object_type=? "
            "AND generation NOT IN ("
            "  SELECT derived_building_generation FROM unified_kg_state u "
            "  WHERE u.notebook_id = ? AND derived_building_generation != 0)",
            (notebook_id, object_type, notebook_id))

    def insert_clusters(
        self,
        connection: sqlite3.Connection,
        notebook_id: str,
        object_type: str,
        rows: List[dict],
        now: str,
        *,
        concept_seed_version: "int | None" = None,
        concept_seed_override: frozenset = frozenset(),
    ) -> int:
        """Insert cluster member rows, skipping member ids already present in
        the (notebook, type) slice — the append_clusters idempotency contract.

        ``concept_seed_version`` / ``concept_seed_override`` (incremental
        placement only): the placement identity ``rows`` were computed in — the
        seed space and its override keys. It is compared, under the same lock
        as the generation pointer, with the one published now; a full rebuild
        that published another space, or the same space with other override
        keys (a suspended rejection translated later), raises
        ``ConceptSeedSpaceMoved`` before any row is written, and the caller
        places again (S1).
        After delete_clusters in the same transaction the existing set is
        empty, so the write_clusters path inserts every row unchanged.

        PR-C 有界化:去重探测只查**本次要插入的** member id,不再把整个
        ``(notebook, object_type)`` 切片读进内存——那一读发生在写事务内(此处
        已 ``BEGIN IMMEDIATE``),9.1M 成员的库上等于把全局写锁按住整整一次全表
        扫描。逐位一致的理由是纯集合论:下面的循环只问 ``r["member_object_id"]
        in existing``,而被问到的 id 恰好就是 ``rows`` 里的那些,故把 ``existing``
        限制到 ``rows`` 的 id 集合不改变任何一次判定,``added`` 与写入的行也逐位
        不变。分批走 ``uq_clusters_nb_type_member_generation(notebook_id,
        object_type, member_object_id)`` 这条唯一索引 —— 前两列等值 + 第三列
        IN,是精确 seek 而不是残余过滤。

        批 3·W2 §2.2 锁序红线(sqlite 化身):published 代指针在
        ``BEGIN IMMEDIATE`` **之后**读——进程写锁把 append 与翻转串行,
        锁后读的指针在本事务内不会再被翻转改走(PG 侧靠 advisory lock,
        论证见那边 docstring)。"""
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        # 锁后重取时刻(codex #671 R7 P1):`now` 形参在 append_clusters 进
        # 写锁**之前**铸造——锁等待超过 KG_CATCHUP_SKEW_SECONDS 时,行会带着
        # 早于催收下界的时间戳落进退休代,翻转后永久漏搬。锁后取时刻使
        # 「行落库晚于取号 ⇒ 时间戳晚于锚点」重新成立(容许同机 skew 余量),
        # 存储形态与服务层 _now() 逐字同款(本地 ISO 带 offset,微秒);
        # PG 孪生的对应修法是 clock_timestamp()。`now` 形参保留签名兼容,
        # 此处刻意不用。
        now = datetime.now().astimezone().isoformat(timespec="microseconds")
        generation = self._published_cluster_generation(connection, notebook_id)
        if concept_seed_version is not None:
            check_placement_seed_space(
                notebook_id, (concept_seed_version, concept_seed_override),
                published_concept_seed_space_on(connection, notebook_id))
        existing = self._existing_cluster_members(
            connection, notebook_id, object_type, rows, generation
        )
        added = 0
        for r in rows:
            if r["member_object_id"] in existing:
                continue
            # created_at 刻意保留应用时钟(与 PG 孪生的 clock_timestamp()
            # 不对称,codex #671 R5 P1 的裁决记录):SQLite 与应用同机同钟,
            # 不存在跨机偏斜;催收比较两侧都过 datetime() 归一(#659 R13),
            # KG_CATCHUP_SKEW_SECONDS 兜的正是这台机器上取时刻与落库之间的
            # 形式余量。改成 datetime('now') 反而会让本表 created_at 出现两种
            # 存储形态,拖累按字符串序消费它的既有读者。
            connection.execute(
                "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,canonical_description,created_at,generation) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (self.seams.new_id("cc"), notebook_id, r["canonical_id"],
                 r["member_object_id"], r["canonical_name"], object_type,
                 r.get("canonical_description", ""), now, generation))
            existing.add(r["member_object_id"])
            added += 1
        return added

    @staticmethod
    def _published_cluster_generation(
        connection: sqlite3.Connection, notebook_id: str
    ) -> int:
        """append 写入的目标代 = 当下 published 指针(无 state 行 ⇒ 0,与读侧
        COALESCE 契约同构)。调用方必须已进写事务(见 insert_clusters 的锁序
        红线)。"""
        row = connection.execute(
            "SELECT cluster_generation FROM unified_kg_state WHERE notebook_id=?",
            (notebook_id,),
        ).fetchone()
        return int(row["cluster_generation"]) if row else 0

    def _existing_cluster_members(
        self,
        connection: sqlite3.Connection,
        notebook_id: str,
        object_type: str,
        rows: List[dict],
        generation: int,
    ) -> set:
        """本次 ``rows`` 的 member id 中,已经在该切片 published 代里的那些
        (有界 IN,分批)。探针按代收窄——理由见 PG 侧同名方法。"""
        member_ids = list(dict.fromkeys(r["member_object_id"] for r in rows))
        size = max(1, int(self.seams.in_chunk_size()))
        found = set()
        for offset in range(0, len(member_ids), size):
            batch = member_ids[offset:offset + size]
            placeholders = ",".join("?" for _ in batch)
            found.update(r["member_object_id"] for r in connection.execute(
                f"SELECT member_object_id FROM concept_clusters "
                f"WHERE notebook_id=? AND object_type=? AND generation=? "
                f"AND member_object_id IN ({placeholders})",
                [notebook_id, object_type, generation, *batch]).fetchall())
        return found

    # --------------------------------------------------------------- merge
    def insert_merge_candidate(
        self,
        connection: sqlite3.Connection,
        notebook_id: str,
        a: str,
        b: str,
        score: float,
        now: str,
        *,
        seed_version: int,
        id_prefix: str = "mc",
    ) -> None:
        # id_prefix keeps the historical surrogate prefixes byte-stable:
        # write_merge_candidate mints "mc-", incremental fusion minted "cm-".
        # seed_version: the seed space ``a`` / ``b`` were minted in (a bridge
        # candidate's ids are bridge ids of the PUBLISHED space) — always
        # explicit, never the column default.
        connection.execute(
            "INSERT INTO concept_merge_candidates (id,notebook_id,canonical_a,canonical_b,score,status,seed_version,created_at,updated_at) VALUES (?,?,?,?,?, 'pending', ?, ?, ?)",
            (self.seams.new_id(id_prefix), notebook_id, a, b, score,
             check_concept_seed_version(seed_version), now, now))

    def write_merge_candidate(
        self,
        connection: sqlite3.Connection,
        notebook_id: str,
        a: str,
        b: str,
        score: float,
        now: str,
        *,
        seed_version: int,
    ) -> None:
        self.insert_merge_candidate(connection, notebook_id, a, b, score, now,
                                    seed_version=seed_version)

    @staticmethod
    def pending_merges(connection: sqlite3.Connection, notebook_id: str) -> List[dict]:
        """Every pending candidate. ``seed_a`` / ``seed_b`` ride along for the
        service's spelling-variant flag (``kg_merge.merge_candidate_kind``),
        which pops them before the row leaves the service."""
        rows = connection.execute(
            "SELECT * FROM concept_merge_candidates WHERE notebook_id=? AND status='pending'",
            (notebook_id,)).fetchall()
        return [{"id": r["id"], "canonical_a": r["canonical_a"], "canonical_b": r["canonical_b"], "score": r["score"], "status": r["status"],
                 "seed_a": r["seed_a"] or "", "seed_b": r["seed_b"] or ""} for r in rows]

    @staticmethod
    def pending_merge_names(
        connection: sqlite3.Connection, notebook_id: str, canonical_ids
    ) -> Dict[str, str]:
        """``{canonical_id: canonical_name}`` of the published concept
        clusters among ``canonical_ids`` -- the names the pending-merge list
        shows for its two sides. An id no published cluster row carries is
        absent (the caller falls back to the id).

        One statement for the whole batch (the caller cuts the response's
        ids into batches), one bounded point lookup per id: a correlated
        ``LIMIT 1`` on ``idx_clusters_nb_canonical_member_gen`` (notebook,
        canonical, member, generation) in member order -- the first member
        row of the published generation, so a hub cluster costs one seek,
        not its member count. The published-generation scalar is
        uncorrelated (evaluated once). Same row as the PostgreSQL twin
        (member ids compare bytewise on both)."""
        values = [str(value) for value in canonical_ids]
        if not values:
            return {}
        rows = connection.execute(
            "SELECT CAST(c.value AS TEXT) AS canonical_id, ("
            "SELECT cc.canonical_name FROM concept_clusters cc "
            "WHERE cc.notebook_id=? AND cc.canonical_id=CAST(c.value AS TEXT) "
            "AND cc.generation = COALESCE((SELECT cluster_generation "
            "FROM unified_kg_state WHERE notebook_id = ?), 0) "
            "ORDER BY cc.member_object_id LIMIT 1) AS canonical_name "
            "FROM json_each(?) c",
            (notebook_id, notebook_id, json.dumps(values, ensure_ascii=False)),
        ).fetchall()
        return {
            str(r["canonical_id"]): str(r["canonical_name"] or "")
            for r in rows if r["canonical_name"] is not None
        }

    @staticmethod
    def pending_merges_batch(
        connection: sqlite3.Connection, notebook_id: str, limit: int
    ) -> List[dict]:
        """Bounded fetch of pending merge candidates, LIMITed in SQL. No ORDER
        BY — SQLite returns rows in rowid order by default absent one, matching
        the implicit order the old ``pending_merges(nb)[:limit]`` slice relied
        on (order-locked to the previous behavior for equal-size batches)."""
        rows = connection.execute(
            "SELECT * FROM concept_merge_candidates WHERE notebook_id=? AND status='pending' LIMIT ?",
            (notebook_id, limit),
        ).fetchall()
        return [{"id": r["id"], "canonical_a": r["canonical_a"], "canonical_b": r["canonical_b"], "score": r["score"], "status": r["status"]} for r in rows]

    @staticmethod
    def has_pending_merges(connection: sqlite3.Connection, notebook_id: str) -> bool:
        row = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM concept_merge_candidates WHERE notebook_id=? AND status='pending') AS e",
            (notebook_id,),
        ).fetchone()
        return bool(row["e"])

    @staticmethod
    def set_merge_decision(
        connection: sqlite3.Connection,
        notebook_id: str,
        candidate_id: str,
        status: str,
        now: str,
    ) -> str | None:
        if status not in ("confirmed", "rejected"):
            raise ValueError(f"invalid merge status: {status!r}")
        target = connection.execute(
            "SELECT canonical_a, canonical_b FROM concept_merge_candidates "
            "WHERE id=? AND notebook_id=?",
            (candidate_id, notebook_id),
        ).fetchone()
        if target is None:
            return None
        siblings = connection.execute(
            "SELECT status FROM concept_merge_candidates WHERE notebook_id=? AND "
            "((canonical_a=? AND canonical_b=?) OR (canonical_a=? AND canonical_b=?))",
            (notebook_id, target["canonical_a"], target["canonical_b"],
             target["canonical_b"], target["canonical_a"]),
        ).fetchall()
        if not siblings:
            return None
        previous_status = (
            "confirmed" if any(r["status"] == "confirmed" for r in siblings)
            else str(siblings[0]["status"])
        )
        # Historical rebuilds could enqueue several seed edges for the same
        # displayed canonical pair. One click is the latest decision for that
        # pair, including already-decided duplicates from an older deployment.
        connection.execute(
            "UPDATE concept_merge_candidates SET status=?, updated_at=? "
            "WHERE notebook_id=? AND "
            "((canonical_a=? AND canonical_b=?) OR (canonical_a=? AND canonical_b=?))",
            (status, now, notebook_id, target["canonical_a"], target["canonical_b"],
             target["canonical_b"], target["canonical_a"]))
        return previous_status

    @staticmethod
    def record_merge_review(
        connection: sqlite3.Connection,
        notebook_id: str,
        candidate_id: str,
        status: str,
        confidence: float,
        rationale: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            UPDATE concept_merge_candidates
            SET status=?, confidence=?, rationale=?, reviewed_by='llm', updated_at=?
            WHERE id=? AND notebook_id=?
            """,
            (status, confidence, rationale, now, candidate_id, notebook_id),
        )

    @staticmethod
    def delete_pending_merges(
        connection: sqlite3.Connection, notebook_id: str, *, lock_ids: Sequence[str] = (),
    ) -> None:
        """The pending refresh's delete. ``lock_ids`` (rows the refresh goes on
        to update) need no lock here: SQLite's write transaction already
        excludes every other writer (see the PostgreSQL twin)."""
        connection.execute(
            "DELETE FROM concept_merge_candidates WHERE notebook_id=? AND status='pending'",
            (notebook_id,))

    @staticmethod
    def insert_pending_merge_rows(
        connection: sqlite3.Connection, rows: List[tuple], *, seed_version: int
    ) -> None:
        """Refresh pending candidates in the caller's transaction. Rows carry
        seed_a/seed_b — the rebuild-stable decision keys (PR#145) — verbatim,
        minted in seed space ``seed_version`` (always explicit)."""
        version = check_concept_seed_version(seed_version)
        connection.executemany(
            "INSERT INTO concept_merge_candidates "
            "(id,notebook_id,canonical_a,canonical_b,seed_a,seed_b,score,status,seed_version,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?, 'pending', ?, ?, ?)",
            [(*row[:-2], version, *row[-2:]) for row in rows])

    def decided_pairs(self, notebook_id: str) -> Dict[tuple, str]:
        with self.database.connect() as db:
            rows = db.execute("SELECT canonical_a, canonical_b, status FROM concept_merge_candidates WHERE notebook_id=? AND status IN ('confirmed','rejected')", (notebook_id,)).fetchall()
        out: Dict[tuple, str] = {}
        for r in rows:
            fold_decision(out, (r["canonical_a"], r["canonical_b"]), r["status"])
        return out

    @staticmethod
    def decided_seed_pairs_from(
        connection: sqlite3.Connection, notebook_id: str, *,
        seed_version: Optional[int],
    ) -> Dict[frozenset, str]:
        """{frozenset({seed_a, seed_b}): status} for confirmed/rejected/deferred.

        Seed-name keys are STABLE across rebuilds (canonical ids shift when a
        cluster's min-member changes; seed names don't). Legacy rows written
        before the seed_a/seed_b columns existed carry '' → fall back to
        strip-"K-"(canonical), matching the old decided_pairs key derivation.
        ``seed_version``: only the rows minted in that seed space (the
        clustering of a space must not read the other space's keys); ``None``
        — every row (the findings rules match both, see
        ``kg_merge_seed.concept_decision_seeds``). Always explicit."""
        version_sql, params = "", [notebook_id]
        if seed_version is not None:
            version_sql = " AND seed_version=?"
            params.append(check_concept_seed_version(seed_version))
        rows = connection.execute(
            "SELECT canonical_a, canonical_b, seed_a, seed_b, status "
            "FROM concept_merge_candidates WHERE notebook_id=? "
            "AND status IN ('confirmed','rejected','deferred')" + version_sql,
            params,
        ).fetchall()
        out: Dict[frozenset, str] = {}
        for r in rows:
            # Rows that contradict each other on one pair: the stronger status
            # (kg_merge_seed.DECISION_STRENGTH), whatever the row order.
            fold_decision(out, frozenset(_decision_keys(r)), r["status"])
        return out

    def decided_seed_pairs(
        self, notebook_id: str, *, seed_version: Optional[int]
    ) -> Dict[frozenset, str]:
        with self.database.connect() as db:
            return self.decided_seed_pairs_from(
                db, notebook_id, seed_version=seed_version)

    @staticmethod
    def merge_decision_rows(
        connection: sqlite3.Connection, notebook_id: str, *, seed_version: int,
        include_pending: bool = False,
    ) -> List[dict]:
        """The decided rows (confirmed / rejected / deferred) of one seed space,
        ``[{id, key_a, key_b, status}]`` in id order, with the decision keys
        derived as in ``decided_seed_pairs_from``. The full rebuild reads the
        v1 rows before it streams (the recompute's step 1) and the v2 rows
        for its clustering — through ``idx_candidates_nb_status``, bounded by
        the notebook's decisions. ``include_pending``: the pending rows too
        (status ``pending``) — the stream tracks their keys so the refresh can
        honour one the user settles mid-rebuild."""
        statuses = ("'confirmed','rejected','deferred','pending'" if include_pending
                    else "'confirmed','rejected','deferred'")
        rows = connection.execute(
            "SELECT id, canonical_a, canonical_b, seed_a, seed_b, status "
            "FROM concept_merge_candidates WHERE notebook_id=? "
            f"AND status IN ({statuses}) AND seed_version=? "
            "ORDER BY id",
            (notebook_id, check_concept_seed_version(seed_version)),
        ).fetchall()
        return [_decision_row(r) for r in rows]

    def apply_v1_decision_translation(
        self,
        connection: sqlite3.Connection,
        notebook_id: str,
        pairs: Dict[str, List[Tuple[str, str]]],
        expected: Dict[str, Tuple[str, str, Optional[str]]],
        now: str,
        *,
        seed_version: int = 1,
    ) -> Tuple[int, List[str]]:
        """Persist the recompute of v1 decisions into seed space 2, in the
        caller's pending-refresh transaction (§五 S1 step 5; SQLite's write
        transaction already excludes every other writer, the PostgreSQL twin
        locks the rows FOR UPDATE).

        For each row id in ``pairs`` still at ``seed_version`` with the keys
        and status it had when the plan was made (``expected``: ``(key_a,
        key_b, status)``; a ``None`` status is not checked): the FIRST pair
        updates the row in place — id, created_at,
        score, reviewer, confidence and rationale kept — and every further
        pair is inserted as a new row carrying the same decision. The rows'
        canonical ids become ``"K-" + seed``, the id a full rebuild gives that
        seed's cluster (a seed the acronym alias redirected, or an override
        seed taken from an aliased v1 seed, is not a form the Memory purge
        recomputes from an object's name — it never needs to be: a decision
        whose side is only a Memory object has no current object and stays
        v1, where the purge's v1 forms match it). A row whose keys or status
        changed since the plan stays v1 — the next rebuild retries it. The
        writes are batched (one ``executemany`` per statement and batch).
        Returns ``(rows the confirmed / rejected count grew by, translated row
        ids)``: the caller folds the first into the stored input version's
        decision count (deferred rows are not counted there).

        ``seed_version=2`` re-keys the stored v2 decisions a new override key
        moved (``DecisionTranslation.move_v2_decisions``) by the same rules
        — first pair in place, one inserted row per further pair, the same
        provenance columns kept — with ``expected`` statuses of ``None``: the
        move depends on the keys alone, so a row decided again while the
        rebuild ran is re-keyed with its live status."""
        version = check_concept_seed_version(seed_version)
        ids = sorted(pairs)
        found: Dict[str, sqlite3.Row] = {}
        for at in range(0, len(ids), _TRANSLATION_BATCH):
            batch = ids[at:at + _TRANSLATION_BATCH]
            for r in connection.execute(
                "SELECT id, canonical_a, canonical_b, seed_a, seed_b, status, score, "
                "confidence, rationale, reviewed_by, created_at "
                "FROM concept_merge_candidates WHERE notebook_id=? AND seed_version=? "
                "AND status IN ('confirmed','rejected','deferred') "
                f"AND id IN ({','.join('?' for _ in batch)})",
                (notebook_id, version, *batch),
            ).fetchall():
                found[r["id"]] = r
        grew = 0
        done: List[str] = []
        updates: List[tuple] = []
        inserts: List[tuple] = []
        for row_id in ids:
            r = found.get(row_id)
            if r is None:
                continue
            row = _decision_row(r)
            key_a, key_b, status = expected[row_id]
            if (row["key_a"], row["key_b"]) != (key_a, key_b) or (
                    status is not None and row["status"] != status):
                continue
            first, rest = pairs[row_id][0], pairs[row_id][1:]
            updates.append((first[0], first[1], "K-" + first[0], "K-" + first[1], now,
                            row_id, notebook_id))
            inserts.extend(
                (self.seams.new_id("mc"), notebook_id, "K-" + a, "K-" + b, a, b,
                 r["score"], r["status"], r["confidence"], r["rationale"],
                 r["reviewed_by"], r["created_at"], now) for a, b in rest)
            if r["status"] in ("confirmed", "rejected"):
                grew += len(rest)
            done.append(row_id)
        for at in range(0, len(updates), _TRANSLATION_BATCH):
            connection.executemany(
                "UPDATE concept_merge_candidates SET seed_a=?, seed_b=?, "
                "canonical_a=?, canonical_b=?, seed_version=2, updated_at=? "
                "WHERE id=? AND notebook_id=?", updates[at:at + _TRANSLATION_BATCH])
        for at in range(0, len(inserts), _TRANSLATION_BATCH):
            connection.executemany(
                "INSERT INTO concept_merge_candidates "
                "(id,notebook_id,canonical_a,canonical_b,seed_a,seed_b,score,status,"
                "confidence,rationale,reviewed_by,seed_version,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,2,?,?)", inserts[at:at + _TRANSLATION_BATCH])
        return grew, done

    @staticmethod
    def drop_superseded_decisions(
        connection: sqlite3.Connection, notebook_id: str, pairs: Sequence[Tuple[str, str]],
    ) -> Tuple[int, int]:
        """One effective decision per seed pair of ``pairs`` among the
        notebook's decided rows in the code's seed space, in the caller's
        pending-refresh transaction: where rows name one pair with different
        statuses, the rows weaker than the pair's strongest status
        (``kg_merge_seed.DECISION_STRENGTH``: confirmed, rejected, deferred)
        are deleted -- a weaker row can never take effect against the
        stronger one and would only contradict it for every reader. Rows of
        the winning status all stay (the same decision twice is no
        contradiction). The decided rows of the space are read once (bounded
        by the notebook's decisions; the refresh reads them all again right
        after), each delete re-checks the status it read. Returns ``(rows
        deleted, confirmed / rejected rows among them)``: the caller folds the
        second into the stored input version's decision count."""
        wanted = {frozenset(pair) for pair in pairs}
        if not wanted:
            return 0, 0
        groups: Dict[frozenset, List[sqlite3.Row]] = {}
        for r in connection.execute(
            "SELECT id, canonical_a, canonical_b, seed_a, seed_b, status "
            "FROM concept_merge_candidates WHERE notebook_id=? AND seed_version=? "
            "AND status IN ('confirmed','rejected','deferred')",
            (notebook_id, CONCEPT_SEED_VERSION),
        ).fetchall():
            key = frozenset(_decision_keys(r))
            if key in wanted:
                groups.setdefault(key, []).append(r)
        deleted = counted = 0
        for group in groups.values():
            strongest = None
            for r in group:
                strongest = stronger_decision(strongest, r["status"])
            for r in group:
                if r["status"] == strongest:
                    continue
                gone = connection.execute(
                    "DELETE FROM concept_merge_candidates "
                    "WHERE id=? AND notebook_id=? AND status=?",
                    (r["id"], notebook_id, r["status"])).rowcount or 0
                deleted += gone
                if r["status"] in ("confirmed", "rejected"):
                    counted += gone
        return deleted, counted

    # ------------------------------------------------------ merge review job
    @staticmethod
    def merge_review_job_row(
        connection: sqlite3.Connection, notebook_id: str
    ) -> "sqlite3.Row | None":
        return connection.execute(
            "SELECT status,total,done,error FROM merge_review_jobs WHERE notebook_id=?",
            (notebook_id,)).fetchone()

    @staticmethod
    def begin_merge_review_job(
        connection: sqlite3.Connection, notebook_id: str, now: str
    ) -> "int | None":
        """Single-flight start: returns None when a job is already running,
        else upserts the running row and returns the pending total — one
        atomic block inside the caller's write transaction."""
        row = connection.execute("SELECT status FROM merge_review_jobs WHERE notebook_id=?",
                                 (notebook_id,)).fetchone()
        if row is not None and row["status"] == "running":
            return None
        total = connection.execute(
            "SELECT COUNT(*) c FROM concept_merge_candidates "
            "WHERE notebook_id=? AND status='pending'", (notebook_id,)).fetchone()["c"]
        connection.execute(
            """
            INSERT INTO merge_review_jobs (notebook_id,status,total,done,started_at,updated_at,error)
            VALUES (?, 'running', ?, 0, ?, ?, '')
            ON CONFLICT(notebook_id) DO UPDATE SET
              status='running', total=excluded.total, done=0,
              started_at=excluded.started_at, updated_at=excluded.updated_at, error=''
            """,
            (notebook_id, total, now, now))
        return int(total)

    @staticmethod
    def set_merge_review_progress(
        connection: sqlite3.Connection, notebook_id: str, done: int, now: str
    ) -> None:
        connection.execute("UPDATE merge_review_jobs SET done=?, updated_at=? WHERE notebook_id=?",
                           (done, now, notebook_id))

    @staticmethod
    def finish_merge_review_job(
        connection: sqlite3.Connection, notebook_id: str, status: str, error: str, now: str
    ) -> None:
        connection.execute("UPDATE merge_review_jobs SET status=?, error=?, updated_at=? WHERE notebook_id=?",
                           (status, error, now, notebook_id))

    # ------------------------------------------------------------ conflicts
    def write_conflict_candidate(
        self,
        connection: sqlite3.Connection,
        notebook_id: str,
        kind: str,
        left_ref: str,
        right_ref: str,
        conflict_type: Optional[str],
        resolution: Optional[str],
        winner_ref: Optional[str],
        resolved_payload: Optional[str],
        confidence: Optional[float],
        rationale: Optional[str],
        now: str,
        *,
        reset_epoch: Optional[int] = None,
    ) -> Optional[str]:
        cid = self.seams.new_id("kcc")
        values = (cid, notebook_id, kind, left_ref, right_ref,
                  conflict_type, resolution, winner_ref, resolved_payload,
                  confidence, rationale, now, now)
        if reset_epoch is None:
            connection.execute(
                """
                INSERT INTO kg_conflict_candidates
                  (id, notebook_id, kind, left_ref, right_ref,
                   conflict_type, resolution, winner_ref, resolved_payload,
                   confidence, rationale, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                values,
            )
            return cid
        # K1: insert only while kg_reset_epoch is still the caller's (missing
        # state row = epoch 0) — PostgreSQL twin's comment. The write lock
        # serializes this against the delete's final reset.
        cur = connection.execute(
            """
            INSERT INTO kg_conflict_candidates
              (id, notebook_id, kind, left_ref, right_ref,
               conflict_type, resolution, winner_ref, resolved_payload,
               confidence, rationale, status, created_at, updated_at)
            SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?
            WHERE COALESCE((SELECT kg_reset_epoch FROM unified_kg_state
                            WHERE notebook_id=?), 0) = ?
            """,
            (*values, notebook_id, int(reset_epoch)),
        )
        return cid if cur.rowcount else None

    @staticmethod
    def pending_conflicts(
        connection: sqlite3.Connection, notebook_id: str
    ) -> List[dict]:
        rows = connection.execute(
            "SELECT * FROM kg_conflict_candidates WHERE notebook_id=? AND status='pending'",
            (notebook_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def set_conflict_status(
        connection: sqlite3.Connection,
        notebook_id: str,
        candidate_id: str,
        status: str,
        now: str,
    ) -> None:
        if status not in ("applied", "rejected"):
            raise ValueError(f"invalid conflict status: {status!r}")
        cur = connection.execute(
            "UPDATE kg_conflict_candidates SET status=?, updated_at=? "
            "WHERE id=? AND notebook_id=?",
            (status, now, candidate_id, notebook_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"conflict candidate {candidate_id!r} not found")

    def get_conflict_candidate(
        self, notebook_id: str, candidate_id: str
    ) -> Optional[dict]:
        """Fetch one conflict candidate inside its notebook authorization scope."""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM kg_conflict_candidates WHERE id=? AND notebook_id=?",
                (candidate_id, notebook_id),
            ).fetchone()
        return dict(row) if row is not None else None

    # ------------------------------------------------------------ whitelist
    @staticmethod
    def concept_whitelist_terms(connection: sqlite3.Connection) -> set:
        return {r["term"] for r in connection.execute("SELECT term FROM concept_whitelist").fetchall()}

    @staticmethod
    def concept_whitelist_rows(connection: sqlite3.Connection) -> List[sqlite3.Row]:
        return connection.execute(
            "SELECT term, note, created_at FROM concept_whitelist ORDER BY term"
        ).fetchall()

    @staticmethod
    def add_whitelist_term(
        connection: sqlite3.Connection, term: str, note: str, now: str
    ) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO concept_whitelist (term, note, created_at) VALUES (?, ?, ?)",
            (term, note, now),
        )

    @staticmethod
    def remove_whitelist_term(connection: sqlite3.Connection, term: str) -> None:
        connection.execute("DELETE FROM concept_whitelist WHERE term = ?", (term,))

    # ------------------------------------------------------------ promotion
    @staticmethod
    def promotion_object_type_row(
        connection: sqlite3.Connection, notebook_id: str, object_id: str,
        *, actor_id: str | None = None,
    ) -> "sqlite3.Row | None":
        """``object_type`` plus ``memory_derived`` (the ``memory_sql`` D4
        classifier) of the object a generic promotion proposal names, so the
        service can refuse a Memory-derived object without another read.
        An object derived from someone else's Memory comes back as ``None``
        (the caller's "not found"); see the PostgreSQL twin."""
        return connection.execute(
            "SELECT ko.object_type, "
            f"{memory_derived_object('ko')} AS memory_derived "
            "FROM knowledge_objects ko WHERE ko.id=? AND ko.notebook_id=? "
            f"AND {foreign_memory_object_excluded('ko')}",
            (object_id, notebook_id, actor_id),
        ).fetchone()

    @staticmethod
    def notebook_tier_row(
        connection: sqlite3.Connection, notebook_id: str
    ) -> "sqlite3.Row | None":
        return connection.execute(
            "SELECT tier FROM notebooks WHERE id=?", (notebook_id,)
        ).fetchone()

    @staticmethod
    def promotion_object_rows(
        connection: sqlite3.Connection, object_ids: List[str]
    ) -> List[sqlite3.Row]:
        if not object_ids:
            return []
        placeholders = ",".join("?" for _ in object_ids)
        return connection.execute(
            f"SELECT id, payload, evidence FROM knowledge_objects "
            f"WHERE id IN ({placeholders})",
            object_ids,
        ).fetchall()

    @staticmethod
    def notebook_name_rows(
        connection: sqlite3.Connection, notebook_ids: List[str]
    ) -> List[sqlite3.Row]:
        """Batched id→name lookup for promotion-queue target display (Task 13
        审查 #4). Mirrors promotion_object_rows' single `id IN (...)` round-trip
        so list_promotion_queue stays O(1) queries regardless of queue size —
        GET /promotion-queue is admin-only (curator sees the whole queue),
        so resolving any owner's notebook name here is intended; unrelated to
        list_mount_edges' demoted-base name masking for regular mount holders
        (that guards a *different* audience: non-admin users who lost mount
        visibility, not the curator this endpoint is scoped to)."""
        if not notebook_ids:
            return []
        placeholders = ",".join("?" for _ in notebook_ids)
        return connection.execute(
            f"SELECT id, name FROM notebooks WHERE id IN ({placeholders})",
            notebook_ids,
        ).fetchall()

    @staticmethod
    def safe_memory_evidence(
        connection: sqlite3.Connection, notebook_id: str, provenance: dict
    ) -> List[dict]:
        """Resolve trusted Ask citations to current source elements.

        Agent-supplied evidence references are intentionally ignored here:
        confirmation accepts the Memory text, not unverified source claims.
        Returned quotes come from stored elements rather than request payloads.
        """
        citations = provenance.get("citations")
        if not isinstance(citations, list):
            return []
        pairs: list[tuple[str, str]] = []
        for citation in citations[:50]:
            if not isinstance(citation, dict):
                continue
            source_id = str(citation.get("source_id") or "")
            element_id = str(citation.get("element_id") or "")
            if source_id and element_id:
                pairs.append((source_id, element_id))
        if not pairs:
            return []
        out: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for source_id, element_id in pairs:
            if (source_id, element_id) in seen:
                continue
            seen.add((source_id, element_id))
            row = connection.execute(
                "SELECT s.id AS source_id,s.title AS source_title,e.id AS element_id,"
                "e.element_type,e.location_label,e.text FROM sources s "
                "JOIN source_elements e ON e.source_id=s.id "
                "WHERE s.id=? AND e.id=? AND s.notebook_id=?",
                (source_id, element_id, notebook_id),
            ).fetchone()
            if row is None:
                continue
            out.append(
                {
                    "source_id": row["source_id"],
                    "source_title": row["source_title"],
                    "element_id": row["element_id"],
                    "element_type": row["element_type"],
                    "location_label": row["location_label"],
                    "quoted_span": str(row["text"] or "")[:500],
                    "confidence": 1.0,
                }
            )
        return out

    @staticmethod
    def object_payload_row(
        connection: sqlite3.Connection, object_id: str
    ) -> "sqlite3.Row | None":
        return connection.execute(
            "SELECT payload FROM knowledge_objects WHERE id=?", (object_id,)
        ).fetchone()

    @staticmethod
    def conflict_resolution_rows(
        connection: sqlite3.Connection, notebook_id: str
    ) -> "tuple[List[sqlite3.Row], List[sqlite3.Row], sqlite3.Row | None]":
        """Detection-shaped read: no evidence bodies, vectors filtered in SQL.

        Evidence is needed for at most the two sides of each surviving candidate,
        so it is fetched by id afterwards (``object_evidence_rows`` /
        ``conflict_relation_evidence_rows``) instead of hauling every object's
        quoted spans through this scan.  The embedding join mirrors the semantic
        strategy's own filter (Concept/Claim, non-deprecated) so the vectors that
        strategy can never look at are not decoded either — ``lower()`` because
        legacy rows may carry capitalised types.

        M1: objects derived from a member's Memory (``memory_sql``'s D4
        classifier — primary ``source_id`` is a Memory source) never enter
        detection, neither as a candidate side nor as a vector. A candidate
        pairing one with a shared object would put Memory text in front of
        the adjudicator, into the queue's ``rationale`` and, through a
        "modify" verdict, into the shared object's payload (N-2). Excluding
        them here, in the one statement that defines the detection universe,
        closes all three; the relation reader does the same for edges.

        Cost: the D4 classifier is not notebook-scoped. On PostgreSQL the
        planner turns it into a hash anti join whose hash side is EVERY Memory
        source in the deployment (``source_type = 'memory'`` on
        ``idx_sources_nb_hidden_type``), also for a notebook that holds no
        Memory. Measured (PG, 49,000 live concepts in the notebook, 1,005
        Memory sources across the deployment, warm cache, median of 7): the
        object read 90 -> 101 ms and the vector read 77 -> 123 ms for a
        notebook without Memory; 88 -> 93 ms / 76 -> 137 ms for one holding
        1,000 of them. SQLite evaluates it as a correlated primary-key probe on
        ``sources`` per scanned row.
        """
        objects = connection.execute(
            "SELECT o.id, o.object_type, o.payload, o.status "
            "FROM knowledge_objects o "
            "WHERE o.notebook_id=? AND o.status != 'deprecated' "
            f"AND NOT {memory_derived_object('o')}",
            (notebook_id,),
        ).fetchall()
        vectors = connection.execute(
            "SELECT e.object_id AS object_id, e.vector AS vector "
            "FROM knowledge_embeddings e "
            "JOIN knowledge_objects o ON o.id = e.object_id "
            "WHERE e.notebook_id=? AND o.status != 'deprecated' "
            "AND lower(o.object_type) IN ('concept','claim') "
            f"AND NOT {memory_derived_object('o')}",
            (notebook_id,),
        ).fetchall()
        notebook = connection.execute(
            "SELECT tier FROM notebooks WHERE id=?", (notebook_id,)
        ).fetchone()
        return objects, vectors, notebook

    @staticmethod
    def conflict_relation_count(
        connection: sqlite3.Connection,
        notebook_id: str,
        *,
        max_rows: int | None = None,
    ) -> int:
        if max_rows is None:
            row = connection.execute(
                "SELECT COUNT(*) AS c FROM knowledge_relations WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT COUNT(*) AS c FROM ("
                "SELECT 1 FROM knowledge_relations WHERE notebook_id=? LIMIT ?"
                ")",
                (notebook_id, max(1, int(max_rows))),
            ).fetchone()
        return int(row["c"])

    @staticmethod
    def conflict_relation_rows(
        connection: sqlite3.Connection,
        notebook_id: str,
        *,
        max_rows: int | None = None,
    ) -> "List[dict]":
        """Thin relation projection for conflict detection.

        Only the columns detection reads: no evidence bodies (fetched by id
        for surviving candidates) and no ``review_status`` — detection has
        never filtered rejected relations and must keep not filtering them.

        Memory-derived relations (D4: primary ``source_id`` is a Memory
        source) are excluded in SQL, before the ``LIMIT`` rail, for the same
        reason as the object side of ``conflict_resolution_rows``; the rail
        therefore counts only the relations detection may actually look at.
        """
        sql = (
            "SELECT r.id, r.source_object_id, r.target_object_id, r.edge_type "
            "FROM knowledge_relations r WHERE r.notebook_id=? "
            f"AND NOT {memory_derived_relation('r')}"
        )
        params: tuple = (notebook_id,)
        if max_rows is not None:
            sql += " LIMIT ?"
            params += (max(1, int(max_rows)),)
        rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def conflict_relation_evidence_rows(
        connection: sqlite3.Connection, relation_ids
    ) -> "List[sqlite3.Row]":
        """Bounded by-id evidence read for the surviving edge candidates."""
        ids = list(relation_ids)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        return connection.execute(
            f"SELECT id, evidence FROM knowledge_relations WHERE id IN ({placeholders})",
            ids,
        ).fetchall()

    @staticmethod
    def promotion_candidate_identity(
        connection: sqlite3.Connection, candidate_id: str
    ) -> "dict | None":
        """Plain-read immutable routing fields before aggregate lock ordering."""
        row = connection.execute(
            "SELECT id,notebook_id,object_id,object_type FROM promotion_candidates "
            "WHERE id=?",
            (candidate_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def promotion_candidate_row(
        connection: sqlite3.Connection, candidate_id: str
    ) -> "sqlite3.Row | None":
        return connection.execute(
            "SELECT * FROM promotion_candidates WHERE id=?", (candidate_id,)
        ).fetchone()

    @staticmethod
    def active_promotion_for_object(
        connection: sqlite3.Connection, object_id: str
    ) -> "sqlite3.Row | None":
        return connection.execute(
            "SELECT * FROM promotion_candidates "
            "WHERE object_id=? AND status NOT IN ('approved','rejected')",
            (object_id,),
        ).fetchone()

    @staticmethod
    def insert_promotion_candidate(
        connection: sqlite3.Connection,
        cand_id: str,
        notebook_id: str,
        object_id: str,
        object_type: str,
        now: str,
        target_base_id: str = "",
    ) -> None:
        connection.execute(
            """
            INSERT INTO promotion_candidates
            (id, notebook_id, object_id, object_type, status, reason,
             reviewed_by, base_match_id, created_at, updated_at, target_base_id)
            VALUES (?, ?, ?, ?, 'proposed', '', '', '', ?, ?, ?)
            """,
            (cand_id, notebook_id, object_id, object_type, now, now, target_base_id),
        )

    @staticmethod
    def promotion_queue_rows(
        connection: sqlite3.Connection, status_filter: Optional[str]
    ) -> List[sqlite3.Row]:
        if status_filter:
            return connection.execute(
                "SELECT * FROM promotion_candidates WHERE status=? "
                "ORDER BY created_at ASC, id ASC",
                (status_filter,),
            ).fetchall()
        return connection.execute(
            "SELECT * FROM promotion_candidates "
            "WHERE status IN ('proposed','under_review') "
            "ORDER BY created_at ASC, id ASC"
        ).fetchall()

    @staticmethod
    def mounted_public_base_ids(
        connection: sqlite3.Connection, notebook_id: str
    ) -> list[str]:
        """本库挂载的**公共知识库** id —— 晋升只能进公共知识库,不能进别人的个人库。
        原 first_base_notebook_row 的全局 LIMIT 1 在多领域下语义已不成立。"""
        rows = connection.execute(
            "SELECT b.id AS id " + MOUNT_JOIN + " AND b.tier = 'base'" + MOUNT_ORDER,
            (notebook_id,),
        ).fetchall()
        return [row["id"] for row in rows]

    @staticmethod
    def promotion_target_column_ready(connection: sqlite3.Connection) -> bool:
        """target_base_id 列(_migration_20)是否已就绪。供离线维护工具(Task 9的
        scripts/backfill_promotion_targets.py)在读 pending_promotion_targets /
        写 set_promotion_target 之前自检 schema 版本, 避免在未迁移库上把"没有
        target_base_id 列"误判成"没有待处理候选"。"""
        cols = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(promotion_candidates)"
            ).fetchall()
        }
        return "target_base_id" in cols

    @staticmethod
    def pending_promotion_targets(connection: sqlite3.Connection) -> List[sqlite3.Row]:
        """status IN ('proposed','under_review') 且 target_base_id 为空串的候选行 ——
        SCHEMA_VERSION=20 之前创建、_migration_20 未回填 target_base_id 的存量候选。
        target_base_id 只在 propose 时可设(insert_promotion_candidate), 没有别的接口
        能给已存在候选补目标, 这是唯一的存量补救读口, 供离线 CLI 使用。"""
        return connection.execute(
            "SELECT id, notebook_id, object_id, object_type, status, created_at "
            "FROM promotion_candidates "
            "WHERE status IN ('proposed','under_review') AND target_base_id='' "
            "ORDER BY notebook_id, created_at, id"
        ).fetchall()

    @staticmethod
    def set_promotion_target(
        connection: sqlite3.Connection, candidate_id: str, target_base_id: str, now: str
    ) -> None:
        """写入存量候选的 target_base_id —— 唯一的"事后补写"入口, 只供离线补救 CLI
        (scripts/backfill_promotion_targets.py)使用。正常 propose 路径走
        insert_promotion_candidate 的 target_base_id 参数, 不经过这里。"""
        connection.execute(
            "UPDATE promotion_candidates SET target_base_id=?, updated_at=? WHERE id=?",
            (target_base_id, now, candidate_id),
        )

    @staticmethod
    def first_admin_user_id(connection: sqlite3.Connection) -> str:
        row = connection.execute(
            "SELECT id FROM users WHERE role='admin' ORDER BY created_at,id LIMIT 1"
        ).fetchone()
        return str(row["id"]) if row is not None else ""

    @staticmethod
    def locate_approved_base_object(
        connection: sqlite3.Connection, candidate_id: str, base_match_id: str
    ) -> "tuple[str, str]":
        """Resolve (base_object_id, base_notebook_id) for an ALREADY-APPROVED
        knowledge-object promotion candidate, without trusting target_base_id —
        callers must be able to find this even when it is stale (the target
        was demoted/deleted after approval) or '' (a pre-_migration_20 row the
        migration backfilled without inferring its historical target; see
        require_live_promotion_target's docstring).

        Fresh-insert approvals stamp source_candidate_id on the new object,
        and candidate ids are globally unique (128-bit, see
        sqlite_repository._new_id), so a lookup by source_candidate_id alone —
        no notebook scope needed — is sufficient and correct. Merge approvals
        never stamp source_candidate_id on the pre-existing matched object
        (only its evidence changes), so those fall back to base_match_id,
        which the write path stamps onto promotion_candidates at approval
        time precisely so a later retry can find its way back."""
        hit = connection.execute(
            "SELECT id, notebook_id FROM knowledge_objects "
            "WHERE source_candidate_id=? ORDER BY created_at ASC, id ASC LIMIT 1",
            (candidate_id,),
        ).fetchone()
        if hit is not None:
            return str(hit["id"]), str(hit["notebook_id"])
        if not base_match_id:
            return "", ""
        match_row = connection.execute(
            "SELECT notebook_id FROM knowledge_objects WHERE id=?",
            (base_match_id,),
        ).fetchone()
        return base_match_id, (str(match_row["notebook_id"]) if match_row else "")

    @staticmethod
    def locate_approved_memory_base_objects(
        connection: sqlite3.Connection, candidate_id: str
    ) -> "tuple[List[str], str]":
        """Resolve (base_object_ids, base_notebook_id) for an ALREADY-APPROVED
        Memory promotion candidate — same rationale as
        locate_approved_base_object (must not trust a stale/empty
        target_base_id). Every knowledge object a Memory promotion creates
        stamps the same source_candidate_id, so one global (non-notebook-
        scoped) lookup covers all of them; a single approval only ever writes
        into one base notebook, so the first row's notebook_id is
        representative of all of them."""
        rows = connection.execute(
            "SELECT id, notebook_id FROM knowledge_objects "
            "WHERE source_candidate_id=? ORDER BY id",
            (candidate_id,),
        ).fetchall()
        ids = [str(row["id"]) for row in rows]
        base_notebook_id = str(rows[0]["notebook_id"]) if rows else ""
        return ids, base_notebook_id

    def approve_memory_promotion_in_transaction(
        self,
        connection: sqlite3.Connection,
        candidate_id: str,
        candidates: List[dict],
        evidence: List[dict],
        reviewer_id: str,
        now: str,
    ) -> dict:
        """Promote a sanitized Memory extraction through the normal base dedupe path."""
        cand = self.promotion_candidate_row(connection, candidate_id)
        if cand is None:
            raise KeyError(candidate_id)
        if cand["status"] == "rejected":
            raise ValueError("cannot approve a rejected promotion candidate")
        base_nb_id = str(cand["target_base_id"] or "")

        # Idempotency BEFORE live-target validation — see the matching
        # comment in approve_promotion_in_transaction (shared bug, shared
        # fix) and require_live_promotion_target's docstring. The common
        # retry path is additionally short-circuited one layer up, in
        # knowledge_governance.approve_promotion, before it ever reaches
        # here; this branch is the fallback for when that layer's own
        # tracking (Memory provenance base_object_ids) is unavailable.
        if cand["status"] == "approved":
            existing_object_ids, existing_base_nb_id = (
                self.locate_approved_memory_base_objects(connection, candidate_id)
            )
            return {
                "base_notebook_id": existing_base_nb_id or base_nb_id,
                "base_object_ids": existing_object_ids,
                "created_object_ids": [],
                "merged_object_ids": [],
            }

        if not base_nb_id:
            raise ValueError("晋升候选缺少目标公共知识库(target_base_id)")
        require_live_promotion_target(connection, base_nb_id)
        # B-12 (PR-E8): see the PostgreSQL twin -- the cards are rewritten to
        # the public library's own "收录自个人记忆：<title>" source.
        memory_row = connection.execute(
            "SELECT title FROM memory_items WHERE id=?", (cand["object_id"],)
        ).fetchone()
        evidence = materialize_promotion_evidence(
            connection, base_nb_id, evidence, now,
            fallback_origin_notebook_id=str(cand["notebook_id"] or ""),
            memory=(
                str(cand["object_id"]),
                str(memory_row["title"] or "") if memory_row is not None else "",
            ),
        )

        base_object_ids: list[str] = []
        created_object_ids: list[str] = []
        merged_object_ids: list[str] = []
        base_space = None       # the base library's published seed space, read once
        for extracted in candidates:
            object_type = str(extracted.get("object_type") or "")
            payload = extracted.get("payload")
            if object_type not in {"concept", "claim", "formula", "procedure"}:
                continue
            if not isinstance(payload, dict) or not payload:
                continue
            base_objs = connection.execute(
                "SELECT id,payload FROM knowledge_objects "
                "WHERE notebook_id=? AND object_type=? AND status IN ({})".format(
                    ",".join("?" for _ in USABLE_STATUSES)
                ),
                (base_nb_id, object_type, *USABLE_STATUSES),
            ).fetchall()
            if base_space is None:
                base_space = published_concept_seed_space_on(connection, base_nb_id)
            seed_version, override = base_space
            base_match_id = find_base_dedup_match(
                object_type, payload, base_objs,
                concept_seed_version=seed_version, override=override)
            if base_match_id:
                merged_evidence = merge_evidence_lists(
                    base_dedup_evidence(connection, base_match_id), evidence
                )
                connection.execute(
                    "UPDATE knowledge_objects SET evidence=?,updated_at=? WHERE id=?",
                    (json.dumps(merged_evidence, ensure_ascii=False), now, base_match_id),
                )
                KnowledgeStore.replace_object_sources(
                    connection,
                    base_match_id,
                    base_nb_id,
                    json.dumps(merged_evidence, ensure_ascii=False),
                )
                base_object_id = base_match_id
                merged_object_ids.append(base_object_id)
            else:
                base_object_id = self.seams.new_id("ko")
                connection.execute(
                    "INSERT INTO knowledge_objects "
                    "(id,notebook_id,object_type,status,owner,payload,evidence,"
                    "source_candidate_id,source_id,created_at,updated_at) "
                    "VALUES (?,?,?,'approved','',?,?,?,'',?,?)",
                    (
                        base_object_id,
                        base_nb_id,
                        object_type,
                        json.dumps(payload, ensure_ascii=False),
                        json.dumps(evidence, ensure_ascii=False),
                        candidate_id,
                        now,
                        now,
                    ),
                )
                KnowledgeStore.replace_object_sources(
                    connection,
                    base_object_id,
                    base_nb_id,
                    json.dumps(evidence, ensure_ascii=False),
                )
                created_object_ids.append(base_object_id)
            base_object_ids.append(base_object_id)

        if not base_object_ids:
            raise ValueError("Memory produced no supported KG candidates")
        connection.execute(
            "UPDATE promotion_candidates SET status='approved',base_match_id=?,"
            "reviewed_by=?,updated_at=? WHERE id=?",
            (merged_object_ids[0] if merged_object_ids else "", reviewer_id, now, candidate_id),
        )
        return {
            "base_notebook_id": base_nb_id,
            "base_object_ids": base_object_ids,
            "created_object_ids": created_object_ids,
            "merged_object_ids": merged_object_ids,
        }

    def approve_promotion_in_transaction(
        self,
        connection: sqlite3.Connection,
        candidate_id: str,
        now: str,
        reviewer_id: str = "curator",
    ) -> PromotionApproval:
        """The in-transaction body of approve_promotion: copy the personal
        object into the base corpus, deduplicating against existing base
        objects of the same type via the kg_merge seed clustering. The caller
        owns the write transaction and the post-commit hooks (embed /
        invalidate / dirty)."""
        cand = self.promotion_candidate_row(connection, candidate_id)
        if cand is None:
            raise KeyError(candidate_id)
        if cand["status"] == "rejected":
            raise ValueError("cannot approve a rejected promotion candidate")
        object_type = cand["object_type"]
        base_nb_id = str(cand["target_base_id"] or "")

        # Idempotency BEFORE live-target validation: a retry of an
        # already-completed approval must return the existing base object
        # as-is, not re-run target validation — the target's tier/existence
        # can legitimately change *after* approval (demoted to personal,
        # deleted, or this is a pre-_migration_20 row whose target_base_id
        # backfilled to '' with no way to infer its historical target — see
        # require_live_promotion_target's docstring), and none of that
        # should turn a no-op retry into a hard failure. Live-target
        # revalidation below guards only the write path — the one place a
        # stale target could actually corrupt data — do not move it back
        # above this branch.
        if cand["status"] == "approved":
            base_object_id, resolved_base_nb_id = self.locate_approved_base_object(
                connection, candidate_id, cand["base_match_id"] or ""
            )
            return PromotionApproval(
                candidate_id=candidate_id,
                source_notebook_id=cand["notebook_id"],
                source_object_id=cand["object_id"],
                base_notebook_id=resolved_base_nb_id or base_nb_id,
                base_object_id=base_object_id,
                created_new_object=False,
            )

        if not base_nb_id:
            raise ValueError("晋升候选缺少目标公共知识库(target_base_id)")
        require_live_promotion_target(connection, base_nb_id)

        # Fetch the personal object being promoted, classified by the D4
        # Memory classifier in the same read (inside the caller's write
        # transaction; nothing has been written yet).
        src = connection.execute(
            "SELECT ko.*, "
            f"{memory_derived_object('ko')} AS memory_derived "
            "FROM knowledge_objects ko WHERE ko.id=?",
            (cand["object_id"],),
        ).fetchone()
        if src is None:
            # Q5: the candidate exists but its object is gone — see the
            # PostgreSQL twin; the service closes the proposal as rejected.
            raise PromotionObjectMissing()
        if src["memory_derived"]:
            # M1: see the PostgreSQL twin — refused before any write; the
            # service rejects the candidate in this same transaction.
            raise MemoryPromotionRefused(PROMOTION_APPROVE_MESSAGE)
        src_payload = json.loads(src["payload"] or "{}")
        # B-12 (PR-E8): see the PostgreSQL twin -- only these incoming entries
        # are rewritten to the public library's own "收录自：<title>" sources.
        src_evidence = materialize_promotion_evidence(
            connection, base_nb_id, json.loads(src["evidence"] or "[]"), now,
            fallback_origin_notebook_id=str(cand["notebook_id"] or ""),
        )

        # Cross-corpus dedup against existing base objects of the same type.
        base_objs = connection.execute(
            "SELECT id, payload FROM knowledge_objects "
            "WHERE notebook_id=? AND object_type=? AND status IN ({})".format(
                ",".join("?" for _ in USABLE_STATUSES)
            ),
            (base_nb_id, object_type, *USABLE_STATUSES),
        ).fetchall()
        seed_version, override = published_concept_seed_space_on(
            connection, base_nb_id)
        base_match_id = find_base_dedup_match(
            object_type, src_payload, base_objs,
            concept_seed_version=seed_version, override=override)

        if base_match_id:
            # Merge: combine evidence into the matched base object; keep its id.
            merged_evidence = merge_evidence_lists(
                base_dedup_evidence(connection, base_match_id), src_evidence
            )
            connection.execute(
                "UPDATE knowledge_objects SET evidence=?, updated_at=? WHERE id=?",
                (json.dumps(merged_evidence, ensure_ascii=False), now, base_match_id),
            )
            base_object_id = base_match_id
            created_new_object = False
            KnowledgeStore.replace_object_sources(
                connection, base_object_id, base_nb_id,
                json.dumps(merged_evidence, ensure_ascii=False),
            )
        else:
            # No match: insert a fresh base object at status='approved'.
            base_object_id = self.seams.new_id("ko")
            connection.execute(
                """
                INSERT INTO knowledge_objects
                (id, notebook_id, object_type, status, owner, payload, evidence,
                 source_candidate_id, source_id, created_at, updated_at)
                VALUES (?, ?, ?, 'approved', '', ?, ?, ?, '', ?, ?)
                """,
                (
                    base_object_id,
                    base_nb_id,
                    object_type,
                    json.dumps(src_payload, ensure_ascii=False),
                    json.dumps(src_evidence, ensure_ascii=False),
                    candidate_id,
                    now,
                    now,
                ),
            )
            created_new_object = True
            KnowledgeStore.replace_object_sources(
                connection, base_object_id, base_nb_id,
                json.dumps(src_evidence, ensure_ascii=False),
            )

        connection.execute(
            "UPDATE promotion_candidates "
            "SET status='approved', base_match_id=?, reviewed_by=?, updated_at=? "
            "WHERE id=?",
            (base_match_id, reviewer_id or "curator", now, candidate_id),
        )
        return PromotionApproval(
            candidate_id=candidate_id,
            source_notebook_id=cand["notebook_id"],
            source_object_id=cand["object_id"],
            base_notebook_id=base_nb_id,
            base_object_id=base_object_id,
            created_new_object=created_new_object,
        )

    @staticmethod
    def set_promotion_rejected(
        connection: sqlite3.Connection,
        candidate_id: str,
        reason: str,
        now: str,
        reviewer_id: str = "curator",
    ) -> None:
        connection.execute(
            "UPDATE promotion_candidates "
            "SET status='rejected', reason=?, reviewed_by=?, updated_at=? "
            "WHERE id=?",
            (reason, reviewer_id, now, candidate_id),
        )

    @staticmethod
    def withdraw_memory_promotions_on(
        connection: sqlite3.Connection,
        notebook_id: str,
        memory_ids: List[str],
        reason: str,
        now: str,
    ) -> int:
        """Reject the still-active proposals of Memory rows the caller is
        about to hard-delete, inside the caller's delete transaction.

        Mirror of the PostgreSQL twin (see its docstring for why a proposal
        must not outlive its Memory, why ``rejected`` is the withdrawal state
        and why ``reviewed_by`` stays empty). Here the process-wide write lock
        the caller holds already serializes this against proposal and
        approval writers."""
        if not memory_ids:
            return 0
        placeholders = ",".join("?" for _ in memory_ids)
        cursor = connection.execute(
            "UPDATE promotion_candidates "
            "SET status='rejected', reason=?, reviewed_by='', updated_at=? "
            f"WHERE notebook_id=? AND object_type='memory' AND object_id IN ({placeholders}) "
            "AND status IN ('proposed','under_review')",
            (reason, now, notebook_id, *memory_ids),
        )
        return int(cursor.rowcount or 0)

    @staticmethod
    def purge_memory_review_rows_on(
        connection: sqlite3.Connection,
        notebook_id: str,
        source_ids: List[str],
        *,
        bridge_canonical_ids: Dict[int, List[str]],
    ) -> int:
        """Mirror of the PostgreSQL twin (its docstring carries the rules):
        the merge candidates naming the Memory's clusters, an id minted from
        one of its objects (``memory_sql.cluster_seed_object_id``) or a bridge
        id no cluster carries; the whole clusters; the conflict candidates
        referencing its objects or relations. Nothing growing with the KG is
        bound; the source ids are one purge
        page (at most 200); the bridge ids go through ``id_binding``.
        Returns the number of cluster rows removed."""
        ids = sorted(set(source_ids))
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        owned = (
            "owned AS (SELECT ko.id FROM knowledge_objects ko "
            f"WHERE ko.notebook_id = ? AND ko.source_id IN ({marks}) "
            f"AND {memory_derived_object('ko')})"
        )
        doomed = (
            "SELECT mc.canonical_id FROM concept_clusters mc WHERE mc.notebook_id = ? "
            "AND (mc.member_object_id IN (SELECT id FROM owned) "
            f"OR {cluster_seed_object_id('mc')} IN (SELECT id FROM owned))"
        )
        v1 = id_bind_ids(sorted(set(bridge_canonical_ids.get(1, ()))), sort=True)
        v2 = id_bind_ids(sorted(set(bridge_canonical_ids.get(2, ()))), sort=True)

        def bridged(side: str) -> str:
            # A row is matched only by the bridge ids of its OWN seed space.
            return (
                f"((seed_version = 1 AND {id_member_of(f'canonical_{side}', v1)}) "
                f"OR (seed_version = 2 AND {id_member_of(f'canonical_{side}', v2)}))"
            )

        connection.execute(
            f"WITH {owned}, doomed AS ({doomed}) "
            "DELETE FROM concept_merge_candidates WHERE notebook_id = ? AND ("
            "canonical_a IN (SELECT canonical_id FROM doomed) "
            "OR canonical_b IN (SELECT canonical_id FROM doomed) "
            f"OR {cluster_seed_object_id('concept_merge_candidates', 'canonical_a')} "
            "IN (SELECT id FROM owned) "
            f"OR {cluster_seed_object_id('concept_merge_candidates', 'canonical_b')} "
            "IN (SELECT id FROM owned) "
            f"OR ({bridged('a')} AND NOT EXISTS ("
            "SELECT 1 FROM concept_clusters xa "
            "WHERE xa.notebook_id = concept_merge_candidates.notebook_id "
            "AND xa.canonical_id = concept_merge_candidates.canonical_a)) "
            f"OR ({bridged('b')} AND NOT EXISTS ("
            "SELECT 1 FROM concept_clusters xb "
            "WHERE xb.notebook_id = concept_merge_candidates.notebook_id "
            "AND xb.canonical_id = concept_merge_candidates.canonical_b)))",
            (notebook_id, *ids, notebook_id, notebook_id,
             v1.param, v2.param, v1.param, v2.param),
        )
        removed = connection.execute(
            f"WITH {owned} DELETE FROM concept_clusters WHERE notebook_id = ? "
            f"AND canonical_id IN ({doomed})",
            (notebook_id, *ids, notebook_id, notebook_id),
        ).rowcount
        connection.execute(
            "WITH refs AS (SELECT ko.id FROM knowledge_objects ko "
            f"WHERE ko.notebook_id = ? AND ko.source_id IN ({marks}) "
            f"AND {memory_derived_object('ko')} UNION ALL "
            "SELECT kr.id FROM knowledge_relations kr "
            f"WHERE kr.notebook_id = ? AND kr.source_id IN ({marks}) "
            f"AND {memory_derived_relation('kr')}) "
            "DELETE FROM kg_conflict_candidates WHERE notebook_id = ? "
            "AND (left_ref IN (SELECT id FROM refs) "
            "OR right_ref IN (SELECT id FROM refs) "
            "OR winner_ref IN (SELECT id FROM refs))",
            (notebook_id, *ids, notebook_id, *ids, notebook_id),
        )
        return int(removed or 0)

    @staticmethod
    def strip_sources_evidence_on(
        connection: sqlite3.Connection,
        notebook_id: str,
        source_ids: List[str],
        now: str,
    ) -> List[str]:
        """Mirror of the PostgreSQL twin: detach several sources' evidence
        from the objects another source owns (the caller's ``BEGIN
        IMMEDIATE`` serialises writers, so there is no row-lock order to
        keep here). Candidate lookup mirrors the teardown branch for branch."""
        ids = sorted(set(source_ids))
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        if KnowledgeStore.source_index_backfilled(connection, notebook_id):
            rows = connection.execute(
                "SELECT ko.id, ko.evidence FROM knowledge_objects ko "
                f"WHERE ko.notebook_id = ? AND (ko.source_id IS NULL OR ko.source_id NOT IN ({marks})) "
                "AND ko.id IN (SELECT kos.object_id FROM knowledge_object_sources kos "
                f"WHERE kos.source_id IN ({marks}) AND kos.notebook_id = ?) "
                "ORDER BY ko.id",
                (notebook_id, *ids, *ids, notebook_id),
            ).fetchall()
        else:
            by_id: Dict[str, sqlite3.Row] = {}
            for source_id in ids:
                for row in connection.execute(
                    "SELECT DISTINCT ko.id, ko.evidence "
                    "FROM knowledge_objects AS ko "
                    "JOIN json_each(CASE WHEN json_valid(ko.evidence) THEN "
                    "CASE WHEN json_type(ko.evidence) = 'array' "
                    "THEN ko.evidence ELSE '[]' END ELSE '[]' END) AS item "
                    f"WHERE ko.notebook_id = ? "
                    f"AND (ko.source_id IS NULL OR ko.source_id NOT IN ({marks})) "
                    "AND item.type = 'object' "
                    "AND json_extract(CASE WHEN item.type = 'object' "
                    "THEN item.value ELSE '{}' END, '$.source_id') = ?",
                    (notebook_id, *ids, source_id),
                ).fetchall():
                    by_id[row["id"]] = row
            rows = [by_id[key] for key in sorted(by_id)]
        wanted = set(ids)
        stripped: List[str] = []
        for row in rows:
            kept = [
                item
                for item in json.loads(row["evidence"] or "[]")
                if not (isinstance(item, dict) and item.get("source_id") in wanted)
            ]
            connection.execute(
                "UPDATE knowledge_objects SET evidence = ?, updated_at = ? "
                "WHERE id = ? AND notebook_id = ?",
                (json.dumps(kept, ensure_ascii=False), now, row["id"], notebook_id),
            )
            stripped.append(row["id"])
        if stripped:
            objects = id_bind_ids(stripped, sort=True)
            connection.execute(
                "DELETE FROM knowledge_object_sources WHERE notebook_id = ? "
                f"AND {id_drive_by('object_id', objects)} AND source_id IN ({marks})",
                (notebook_id, objects.param, *ids),
            )
        return stripped

    @staticmethod
    def merge_followers_on(
        connection: sqlite3.Connection,
        notebook_id: str,
        source_ids: List[str],
        target_ids: List[str],
    ) -> List[tuple]:
        """Mirror of the PostgreSQL twin: ``(object id, marker)`` of the
        deprecated objects whose merge marker names one of ``target_ids``,
        owned by another Memory source of this notebook whose Memory row
        still exists. Driven by the notebook's Memory sources (the
        ``hidden_type_index_term`` partial-index hint), then their objects."""
        if not target_ids:
            return []
        owners = id_bind_ids(sorted(set(source_ids)), sort=True)
        targets = id_bind_ids(sorted(set(target_ids)), sort=True)
        marker = f"json_extract(ko.payload, '$.{MERGED_INTO_PAYLOAD_KEY}')"
        rows = connection.execute(
            f"SELECT ko.id, {marker} AS marker FROM sources s "
            "JOIN knowledge_objects ko ON ko.source_id = s.id "
            "AND ko.notebook_id = s.notebook_id "
            f"WHERE s.notebook_id = ? AND {hidden_type_index_term('s')} "
            f"AND {memory_source_type_predicate('s.source_type')} "
            f"AND {id_not_member_of('s.id', owners)} AND ko.status = 'deprecated' "
            f"AND json_valid(ko.payload) AND {id_member_of(marker, targets)} "
            "AND EXISTS (SELECT 1 FROM memory_items m WHERE m.id = s.memory_id) "
            "ORDER BY ko.id",
            (notebook_id, owners.param, targets.param),
        ).fetchall()
        return [(str(row["id"]), str(row["marker"])) for row in rows]

    @staticmethod
    def apply_merge_follow_actions_on(
        connection: sqlite3.Connection,
        actions: Sequence[MergeFollowAction],
        now: str,
        *,
        wait: bool,
        lock_timeout_ms: Optional[int] = None,
    ) -> tuple:
        """Mirror of the PostgreSQL twin minus its locks (the caller's
        ``BEGIN IMMEDIATE`` serialises writers, so nothing is ever skipped):
        rewrite each still-deprecated row whose marker still equals the
        action's ``expected_target`` and whose Memory row still exists.
        Returns ``(applied, skipped)``; ``skipped`` holds only the re-points
        whose target no longer exists (deferred, as on PostgreSQL)."""
        del wait, lock_timeout_ms  # nothing to wait for under SQLite's single writer
        applied: List[MergeFollowAction] = []
        skipped: List[MergeFollowAction] = []
        by_notebook: Dict[str, Dict[str, MergeFollowAction]] = {}
        for action in actions:
            by_notebook.setdefault(action.notebook_id, {})[action.object_id] = action
        for notebook_id in sorted(by_notebook):
            wanted = by_notebook[notebook_id]
            found = id_bind_ids(sorted(wanted), sort=True)
            # drive_by: the action ids are the keys to read.
            rows = connection.execute(
                "SELECT ko.id, ko.status, ko.payload FROM knowledge_objects ko "
                "JOIN sources s ON s.id = ko.source_id "
                f"WHERE {id_drive_by('ko.id', found)} AND +ko.notebook_id = ? "
                f"AND {memory_source_type_predicate('s.source_type')} "
                "AND EXISTS (SELECT 1 FROM memory_items m WHERE m.id = s.memory_id) "
                "ORDER BY ko.id",
                (found.param, notebook_id),
            ).fetchall()
            gated: set = set()
            deferred: List[MergeFollowAction] = []
            for row in rows:
                action = wanted[str(row["id"])]
                if action.kind == FOLLOW_REPOINT and not GovernanceStore._object_exists_on(
                    connection, notebook_id, action.repoint_to
                ):
                    # See the PostgreSQL twin: the target vanished after the
                    # backstop read it; the row stays and is reported back.
                    deferred.append(action)
                    continue
                payload = json.loads(row["payload"] or "{}")
                if row["status"] != "deprecated" or not isinstance(payload, dict) or (
                    payload.get(MERGED_INTO_PAYLOAD_KEY) != action.expected_target
                ):
                    continue
                connection.execute(
                    "UPDATE knowledge_objects SET payload = ?, status = ?, updated_at = ? "
                    "WHERE id = ? AND notebook_id = ?",
                    (
                        json.dumps(followed_payload(payload, action), ensure_ascii=False),
                        "approved" if action.kind == FOLLOW_RESTORE else "deprecated",
                        now,
                        action.object_id,
                        notebook_id,
                    ),
                )
                applied.append(action)
                if action.kind == FOLLOW_REPOINT and action.repoint_to not in gated:
                    gated.add(action.repoint_to)
                    GovernanceStore._open_follower_gate_on(
                        connection, notebook_id, action.repoint_to
                    )
            skipped.extend(deferred)
        return applied, skipped

    @staticmethod
    def _object_exists_on(
        connection: sqlite3.Connection, notebook_id: str, object_id: str
    ) -> bool:
        return connection.execute(
            "SELECT 1 FROM knowledge_objects WHERE id = ? AND notebook_id = ?",
            (object_id, notebook_id),
        ).fetchone() is not None

    @staticmethod
    def _open_follower_gate_on(
        connection: sqlite3.Connection, notebook_id: str, object_id: str
    ) -> None:
        """A re-point's target gets ``ABSORBS_PAYLOAD_KEY`` if it lacks it."""
        row = connection.execute(
            "SELECT payload FROM knowledge_objects WHERE id = ? AND notebook_id = ?",
            (object_id, notebook_id),
        ).fetchone()
        payload = json.loads(row["payload"] or "{}") if row is not None else None
        if isinstance(payload, dict) and not payload.get(ABSORBS_PAYLOAD_KEY):
            connection.execute(
                "UPDATE knowledge_objects SET payload = ? WHERE id = ? AND notebook_id = ?",
                (json.dumps(marked_absorbs(payload), ensure_ascii=False), object_id,
                 notebook_id),
            )

    @staticmethod
    def lock_memory_teardown_objects_on(
        connection: sqlite3.Connection, notebook_id: str, source_ids: List[str]
    ) -> tuple:
        """Mirror of the PostgreSQL twin's read half. There are no row locks
        to take up front: the caller's ``BEGIN IMMEDIATE`` already serialises
        every writer, so two teardowns can never interleave. Returns the
        sources' own rows ``{id, source_id, status, marker}``, ``False``
        (SQLite's strip reads its own reverse-index verdict) and ``None``."""
        ids = sorted(set(source_ids))
        if not ids:
            return [], False, None
        owners = id_bind_ids(ids, sort=True)
        marker = f"json_extract(payload, '$.{MERGED_INTO_PAYLOAD_KEY}')"
        own = [
            dict(row)
            for row in connection.execute(
                "SELECT id, source_id, status, "
                f"CASE WHEN json_valid(payload) THEN {marker} END AS marker, "
                "CASE WHEN json_valid(payload) THEN "
                f"json_extract(payload, '$.{ABSORBS_PAYLOAD_KEY}') END AS absorbs "
                "FROM knowledge_objects "
                f"WHERE {id_drive_by('source_id', owners)} AND +notebook_id = ?",
                (owners.param, notebook_id),
            ).fetchall()
        ]
        return own, False, None

    @staticmethod
    def detach_memory_merges_on(
        connection: sqlite3.Connection,
        notebook_id: str,
        source_ids: List[str],
        now: str,
    ) -> MemoryMergeDetach:
        """Mirror of the PostgreSQL twin: read the sources' own objects
        (``lock_memory_teardown_objects_on``), settle the objects merged into
        them, then strip the sources' entries from the objects other sources
        own."""
        ids = sorted(set(source_ids))
        own, _backfilled, _cited = GovernanceStore.lock_memory_teardown_objects_on(
            connection, notebook_id, ids
        )
        applied: List[MergeFollowAction] = []
        followers = (
            GovernanceStore.merge_followers_on(
                connection, notebook_id, ids, [str(row["id"]) for row in own]
            )
            if any(row["absorbs"] for row in own)
            else []
        )
        if followers:
            actions = plan_merge_follow_actions(
                notebook_id,
                followers,
                {str(row["id"]): (row["status"], row["marker"] or "") for row in own},
            )
            applied, _skipped = GovernanceStore.apply_merge_follow_actions_on(
                connection, actions, now, wait=True
            )
        GovernanceStore.strip_sources_evidence_on(connection, notebook_id, ids, now)
        return MemoryMergeDetach(tuple(applied), ())

    def heal_merged_away_candidates(
        self,
        limit: int,
        after: tuple = ("", ""),
        *,
        notebook_id: Optional[str] = None,
    ) -> tuple:
        """Mirror of the PostgreSQL twin, on a plain read connection (never
        ``BEGIN IMMEDIATE``: the backstop blocks no writer while it reads).
        The walk is pinned to the Memory volume: the Memory sources come from
        the partial index of hidden sources (``INDEXED BY
        idx_sources_nb_hidden_type`` — without statistics SQLite would
        otherwise walk the whole sources table, or the objects' primary key),
        their objects from ``idx_knowledge_objects_source``."""
        limit = max(1, int(limit))
        key = f"'$.{MERGED_INTO_PAYLOAD_KEY}'"
        actions: List[MergeFollowAction] = []
        next_after = tuple(after)
        with self.database.connect() as db:
            scope = " AND s.notebook_id = ?" if notebook_id else ""
            sources = sorted(
                str(row["id"])
                for row in db.execute(
                    "SELECT s.id FROM sources s INDEXED BY idx_sources_nb_hidden_type "
                    "JOIN memory_items m ON m.id = s.memory_id "
                    f"WHERE {hidden_type_index_term('s')} "
                    f"AND {memory_source_type_predicate('s.source_type')}{scope}",
                    (notebook_id,) if notebook_id else (),
                ).fetchall()
                if str(row["id"]) >= next_after[0]
            )
            backfilled: Dict[str, bool] = {}
            for offset in range(0, len(sources), _HEAL_SOURCE_BATCH):
                bound = id_bind_ids(sources[offset:offset + _HEAL_SOURCE_BATCH], sort=True)
                marker = f"json_extract(ko.payload, {key})"
                rows = db.execute(
                    f"SELECT ko.id, ko.source_id, ko.notebook_id, {marker} AS marker, "
                    "ko.evidence FROM knowledge_objects ko "
                    "INDEXED BY idx_knowledge_objects_source "
                    f"WHERE {id_drive_by('ko.source_id', bound)} "
                    "AND ko.status = 'deprecated' AND json_valid(ko.payload) "
                    f"AND {marker} IS NOT NULL "
                    "AND NOT EXISTS (SELECT 1 FROM knowledge_objects t "
                    f"WHERE t.id = {marker})",
                    (bound.param,),
                ).fetchall()
                for row in sorted(rows, key=lambda r: (str(r["source_id"]), str(r["id"]))):
                    position = (str(row["source_id"]), str(row["id"]))
                    if position <= next_after:
                        continue
                    evidence = json.loads(row["evidence"] or "[]")
                    holders = (
                        GovernanceStore._merge_heal_holders_on(db, row, backfilled)
                        if evidence else []
                    )
                    actions.append(heal_action(
                        str(row["notebook_id"]), str(row["id"]), str(row["marker"]),
                        evidence, holders,
                    ))
                    next_after = position
                    if len(actions) >= limit:
                        return actions, next_after
        return actions, next_after

    @staticmethod
    def _merge_heal_holders_on(
        connection: sqlite3.Connection, row: Any, backfilled: Dict[str, bool]
    ) -> List[tuple]:
        """Mirror of the PostgreSQL twin."""
        notebook_id = str(row["notebook_id"])
        if notebook_id not in backfilled:
            backfilled[notebook_id] = KnowledgeStore.source_index_backfilled(
                connection, notebook_id
            )
        if backfilled[notebook_id]:
            rows = connection.execute(
                "SELECT o.id, o.status, o.evidence, o.payload "
                "FROM knowledge_object_sources kos "
                "JOIN knowledge_objects o ON o.id = kos.object_id "
                "WHERE kos.source_id = ? AND kos.notebook_id = ? AND o.id <> ? "
                "AND o.source_id IS NOT ?",
                (row["source_id"], notebook_id, row["id"], row["source_id"]),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT DISTINCT ko.id, ko.status, ko.evidence, ko.payload "
                "FROM knowledge_objects ko "
                "JOIN json_each(CASE WHEN json_valid(ko.evidence) AND "
                "json_type(ko.evidence) = 'array' THEN ko.evidence ELSE '[]' END) AS item "
                "WHERE ko.notebook_id = ? AND ko.id <> ? AND ko.source_id IS NOT ? "
                "AND item.type = 'object' "
                "AND json_extract(item.value, '$.source_id') = ?",
                (notebook_id, row["id"], row["source_id"], row["source_id"]),
            ).fetchall()
        return [
            (str(r["id"]), str(r["status"]), json.loads(r["evidence"] or "[]"),
             json.loads(r["payload"] or "{}"))
            for r in rows
        ]

    @staticmethod
    def detach_promotion_sources_on(
        connection: sqlite3.Connection, rows: List[dict], now: str
    ) -> List[str]:
        """Mirror of the PostgreSQL twin (PR-E8): strip a promotion source's
        entries, delete only the objects left without evidence."""
        by_notebook: Dict[str, List[str]] = {}
        for row in rows:
            by_notebook.setdefault(str(row["notebook_id"]), []).append(str(row["id"]))
        deleted: List[str] = []
        for notebook_id in sorted(by_notebook):
            stripped = GovernanceStore.strip_sources_evidence_on(
                connection, notebook_id, by_notebook[notebook_id], now
            )
            if not stripped:
                continue
            found = id_bind_ids(stripped, sort=True)
            emptied = sorted(
                str(row["id"])
                for row in connection.execute(
                    "SELECT id, evidence FROM knowledge_objects "
                    f"WHERE {id_drive_by('id', found)}",
                    (found.param,),
                ).fetchall()
                if not any(
                    isinstance(item, dict)
                    for item in json.loads(row["evidence"] or "[]")
                )
            )
            for offset in range(0, len(emptied), _REVIEW_ENDPOINT_LOOKUP_BATCH):
                KnowledgeStore._delete_object_id_batch(
                    connection, notebook_id,
                    emptied[offset:offset + _REVIEW_ENDPOINT_LOOKUP_BATCH],
                )
            deleted.extend(emptied)
        return deleted

    # -------------------------------------------------- knowledge mutation
    @staticmethod
    def update_object_in_transaction(
        connection: sqlite3.Connection,
        notebook_id: str,
        object_id: str,
        payload,
        now: str,
        *,
        actor_id: str | None = None,
        own_status_only: bool = False,
    ) -> sqlite3.Row:
        """The in-transaction body of update_knowledge: validate, apply the
        partial update and return the refetched row. ``payload`` is the
        KnowledgeUpdate model (status/payload/owner partial edit).

        M1 (E4-3): an object derived from someone else's Memory raises the
        same ``KeyError`` as a missing id; see the PostgreSQL twin.

        ``own_status_only`` (the caller lacks ``knowledge:write``, user ruling
        2026-10-22): only a status change between approved and deprecated of
        her own Memory object supported by her own Memory alone
        (``_own_status_toggle_allowed``) is applied; anything else raises
        ``NeedsKnowledgeWrite`` on the row just read, before any write."""
        row = connection.execute(
            "SELECT ko.* FROM knowledge_objects ko WHERE ko.id = ? "
            f"AND ko.notebook_id = ? AND {foreign_memory_object_excluded('ko')}",
            (object_id, notebook_id, actor_id),
        ).fetchone()
        if row is None:
            raise KeyError(object_id)
        if own_status_only and not GovernanceStore._own_status_toggle_allowed(
            connection, notebook_id, actor_id, row, payload
        ):
            raise NeedsKnowledgeWrite()
        if payload.status is not None and payload.status not in KNOWLEDGE_STATUSES:
            raise ValueError(f"invalid status: {payload.status}")
        new_payload = (
            json.dumps(payload.payload, ensure_ascii=False)
            if payload.payload is not None
            else row["payload"]
        )
        if payload.status is not None or payload.payload is not None:
            # A hand edit of status or content clears the merge marker: only a
            # merge writes it, so a hand-deprecated object is never restored.
            parsed = json.loads(new_payload or "{}")
            old = json.loads(row["payload"] or "{}")
            if isinstance(parsed, dict) and isinstance(old, dict):
                patched = patched_payload(parsed, old)
                if patched != parsed:
                    new_payload = json.dumps(patched, ensure_ascii=False)
        new_status = payload.status if payload.status is not None else row["status"]
        new_owner = payload.owner if payload.owner is not None else row["owner"]
        # Stamp last_reviewed whenever a curator changes status.
        last_reviewed = now if payload.status is not None else (
            row["last_reviewed"] if "last_reviewed" in row.keys() else ""
        )
        connection.execute(
            "UPDATE knowledge_objects SET payload = ?, status = ?, owner = ?, "
            "last_reviewed = ?, updated_at = ? WHERE id = ? AND notebook_id = ?",
            (new_payload, new_status, new_owner, last_reviewed, now, object_id, notebook_id),
        )
        return connection.execute(
            "SELECT * FROM knowledge_objects WHERE id = ? AND notebook_id = ?",
            (object_id, notebook_id),
        ).fetchone()

    @staticmethod
    def _own_status_toggle_allowed(
        connection: sqlite3.Connection,
        notebook_id: str,
        actor_id: str | None,
        row: sqlite3.Row,
        payload,
    ) -> bool:
        """The read-level ``PATCH``: a status change alone, between approved
        and deprecated, of an object whose primary source and every evidence
        entry are the caller's own Memory sources of this notebook -- the merge's
        own-pair rule on one object (``merge_evidence_source_ids``, then one
        ``own_memory_source`` read over the cited ids and the primary source)."""
        if payload.payload is not None or payload.owner is not None:
            return False
        if payload.status not in OWN_STATUS_TOGGLE or row["status"] not in OWN_STATUS_TOGGLE:
            return False
        cited = merge_evidence_source_ids(json.loads(row["evidence"] or "[]"))
        if cited is None or not actor_id or not row["source_id"]:
            return False
        wanted = sorted(cited | {row["source_id"]})
        # drive_by: the wanted ids are the keys (a primary-key seek each).
        found = id_bind_ids(wanted, sort=True)
        own = connection.execute(
            f"SELECT s.id FROM sources s WHERE {id_drive_by('s.id', found)} "
            f"AND s.notebook_id = ? AND {own_memory_source('s')}",
            (found.param, notebook_id, actor_id),
        ).fetchall()
        return len(own) == len(wanted)

    @staticmethod
    def own_memory_duplicate_seed_rows(
        connection: sqlite3.Connection,
        notebook_id: str,
        object_type: str,
        viewer_id: str,
    ) -> List[dict]:
        """Pass 1 of the owner's duplicate groups (see the PostgreSQL twin):
        the viewer's own Memory objects of ``object_type`` whose evidence cites
        nothing but her own Memory sources of this notebook
        (``_only_own_memory_evidence_sql``, the merge's reading in SQL) --
        live ones, and deprecated ones still carrying a merge record
        (``MERGED_INTO_PAYLOAD_KEY``: merged away, not hand-deprecated).
        Thin like ``duplicate_seed_rows``: ``{"id", "status", "name"}`` (plus
        ``"payload"`` for a procedure, whose seed reads the steps), ``name``
        read the way that reader reads it, in its order. No evidence and no
        payload leave the database; pass 2 (``own_memory_duplicate_member_rows``)
        hydrates only the ids that end up shown. The empty viewer owns nothing.

        Driven by the notebook's Memory sources (``idx_sources_nb_hidden_type``,
        join order pinned by ``CROSS JOIN``), objects then by
        ``idx_knowledge_objects_source``; the evidence test's own-source list
        is one uncorrelated subquery on the same partial index. The cost
        follows the viewer's own Memory in this notebook, never the notebook."""
        if not viewer_id:
            return []
        procedure = object_type == "procedure"
        payload_column = "o.payload, " if procedure else ""
        marker = f"json_extract(o.payload, '$.{MERGED_INTO_PAYLOAD_KEY}')"
        rows = connection.execute(
            "SELECT o.id, o.status, json_extract(o.payload, '$.name') AS name, "
            f"{payload_column}{marker} AS merged_into "
            "FROM sources s CROSS JOIN knowledge_objects o "
            f"WHERE s.notebook_id=? AND {hidden_type_index_term('s')} "
            f"AND {own_memory_source('s')} "
            "AND o.source_id=s.id AND o.notebook_id=s.notebook_id AND o.object_type=? "
            f"AND (o.status!='deprecated' OR COALESCE({marker}, '') <> '') "
            f"AND {_only_own_memory_evidence_sql('o')} "
            "ORDER BY o.created_at ASC, o.id ASC",
            (notebook_id, viewer_id, object_type, notebook_id, viewer_id),
        ).fetchall()
        out: List[dict] = []
        for row in rows:
            item = {"id": row["id"], "status": row["status"], "name": row["name"]}
            if procedure:
                item["payload"] = json.loads(row["payload"] or "{}")
            out.append(item)
        return out

    @staticmethod
    def own_memory_duplicate_member_rows(
        connection: sqlite3.Connection,
        notebook_id: str,
        object_ids: Sequence[str],
    ) -> Dict[str, dict]:
        """Pass 2: ``{id: payload}`` for ids pass 1 returned (the members of a
        group of two or more, and the merged-away objects). One statement, the
        ids bound as one JSON parameter and driving a primary-key seek each
        (``drive_by``); ``notebook_id`` is projected and checked here, not put
        beside the id list (the planner hazard ``duplicate_member_rows``
        documents)."""
        ids = list(dict.fromkeys(object_ids))
        if not ids:
            return {}
        found = id_bind_ids(ids)
        rows = connection.execute(
            "SELECT o.id, o.payload, o.notebook_id FROM knowledge_objects o "
            f"WHERE {id_drive_by('o.id', found)}",
            (found.param,),
        ).fetchall()
        return {
            row["id"]: json.loads(row["payload"] or "{}")
            for row in rows if row["notebook_id"] == notebook_id
        }

    @staticmethod
    def _own_memory_merge_evidence(
        connection: sqlite3.Connection,
        notebook_id: str,
        actor_id: str | None,
        src: sqlite3.Row,
        tgt: sqlite3.Row,
    ) -> bool:
        """Mirror of the PostgreSQL twin (minus its pre-lock check): does
        every evidence entry of both rows cite one of the caller's own
        Memory sources of this notebook (``own_memory_source``)?"""
        cited = merge_evidence_source_ids(
            json.loads(src["evidence"] or "[]"), json.loads(tgt["evidence"] or "[]")
        )
        if cited is None:
            return False
        if not cited:
            return True
        # drive_by: the cited ids are the keys to read (a primary-key seek
        # each); the notebook would otherwise drive a walk of all its sources.
        found = id_bind_ids(sorted(cited), sort=True)
        own = connection.execute(
            f"SELECT s.id FROM sources s WHERE {id_drive_by('s.id', found)} "
            f"AND s.notebook_id = ? AND {own_memory_source('s')}",
            (found.param, notebook_id, actor_id),
        ).fetchall()
        return len(own) == len(cited)

    @staticmethod
    def merge_objects_in_transaction(
        connection: sqlite3.Connection,
        notebook_id: str,
        source_id: str,
        into_id: str,
        now: str,
        *,
        actor_id: str | None = None,
        own_pair_only: bool = False,
    ) -> sqlite3.Row:
        """The in-transaction body of merge_knowledge: fold source evidence
        into the target, maintain the reverse index, deprecate the source in
        place, and return the refetched target row.

        ``own_pair_only`` (the caller lacks ``knowledge:write``, user ruling
        2026-10-22): only a pair of the caller's own Memory objects supported
        by her own Memory alone, both approved, may merge; any other pair that
        passes every check below raises ``NeedsKnowledgeWrite`` -- decided on
        the same reads that classify the pair, before anything is written.

        M1 (``app.domain.memory_kg_isolation`` has the rule): a
        Memory-derived object merges only with another one of the same
        owner, and only when both are supported by nothing but that owner's
        Memory. The classification rides the two reads this body already
        makes, inside the caller's write transaction (SQLite's single writer:
        nothing can change either row, nor any source, between these reads
        and the UPDATEs — so, unlike the PostgreSQL twin, no source lock is
        taken): an object derived from someone else's Memory is filtered out
        (``foreign_memory_object_excluded``) and raises the same ``KeyError``
        as a missing id; a pair with exactly one of the caller's own, or two
        whose evidence cites anything but the caller's own Memory sources of
        this notebook (``_own_memory_merge_evidence``), raises
        ``MemoryKnowledgeMergeRefused`` before anything is written."""
        read = (
            "SELECT ko.*, "
            f"{memory_derived_object('ko')} AS memory_derived "
            "FROM knowledge_objects ko WHERE ko.id = ? AND ko.notebook_id = ? "
            f"AND {foreign_memory_object_excluded('ko')}"
        )
        src = connection.execute(read, (source_id, notebook_id, actor_id)).fetchone()
        tgt = connection.execute(read, (into_id, notebook_id, actor_id)).fetchone()
        if src is None or tgt is None:
            raise KeyError(source_id if src is None else into_id)
        if src["object_type"] != tgt["object_type"]:
            raise ValueError("can only merge knowledge objects of the same type")
        refusal = memory_merge_refusal(
            bool(src["memory_derived"]),
            bool(tgt["memory_derived"]),
            lambda: GovernanceStore._own_memory_merge_evidence(
                connection, notebook_id, actor_id, src, tgt
            ),
        )
        if refusal is not None:
            raise MemoryKnowledgeMergeRefused(refusal)
        # Reached only past the refusal: Memory-derived on both sides is an
        # own-Memory pair; its merged-away row records the target
        # (MERGED_INTO_PAYLOAD_KEY) so clearing the target can restore it.
        own_pair = bool(src["memory_derived"]) and bool(tgt["memory_derived"])
        # The read-level merge also needs both sides approved: merging and then
        # restoring would otherwise lift a conflict / reviewed object to approved,
        # around the read-level PATCH's approved <-> deprecated rule.
        if own_pair_only and not (
            own_pair and src["status"] == "approved" and tgt["status"] == "approved"
        ):
            raise NeedsKnowledgeWrite()
        merged: List[dict] = json.loads(tgt["evidence"] or "[]")
        seen = {(e.get("element_id"), e.get("quoted_span")) for e in merged}
        for item in json.loads(src["evidence"] or "[]"):
            key = (item.get("element_id"), item.get("quoted_span"))
            if key not in seen:
                merged.append(item)
                seen.add(key)
        merged_json = json.dumps(merged, ensure_ascii=False)
        connection.execute(
            "UPDATE knowledge_objects SET evidence = ?, updated_at = ? WHERE id = ?",
            (merged_json, now, into_id),
        )
        # into_id's evidence gained items (possibly new source_ids) from source_id;
        # source_id itself only flips status (its own evidence/reverse-index rows
        # are unchanged and stay correct until the object is actually deleted).
        KnowledgeStore.replace_object_sources(connection, into_id, notebook_id, merged_json)
        connection.execute(
            "UPDATE knowledge_objects SET status = 'deprecated', last_reviewed = ?, updated_at = ? WHERE id = ?",
            (now, now, source_id),
        )
        if own_pair:
            connection.execute(
                "UPDATE knowledge_objects SET payload = ? WHERE id = ?",
                (
                    json.dumps(
                        marked_merged_into(json.loads(src["payload"] or "{}"), into_id),
                        ensure_ascii=False,
                    ),
                    source_id,
                ),
            )
            connection.execute(
                "UPDATE knowledge_objects SET payload = ? WHERE id = ?",
                (
                    json.dumps(
                        marked_absorbs(json.loads(tgt["payload"] or "{}")),
                        ensure_ascii=False,
                    ),
                    into_id,
                ),
            )
        return connection.execute(
            "SELECT * FROM knowledge_objects WHERE id = ?", (into_id,)
        ).fetchone()

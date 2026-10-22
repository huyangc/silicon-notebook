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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.core.json_safety import validate_finite_json
from app.core.text_whitespace import PY_STRIP_WHITESPACE
from app.repositories.postgres._store_utils import (
    execute_many,
    iso_timestamp,
    json_value,
    jsonb,
    normalize_timestamp,
)
from app.repositories.postgres.cluster_lock import lock_cluster_artifact_type
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.knowledge_store import KnowledgeStore
from app.repositories.postgres.memory_sql import (
    cluster_seed_object_id,
    foreign_memory_object_excluded,
    foreign_memory_relation_excluded,
    memory_derived_object,
    memory_derived_relation,
    memory_source_type_predicate,
    own_memory_source,
)
from app.repositories.postgres.id_binding import (
    bind_ids as id_bind_ids,
    execute_ids as id_execute_ids,
    member_of as id_member_of,
    not_member_of as id_not_member_of,
)
from app.repositories.postgres.mount_sql import MOUNT_JOIN, MOUNT_ORDER
from app.repositories.postgres.promotion_provenance_store import (
    materialize_promotion_evidence,
)
from app.domain.kg_merge_seed import check_concept_seed_version, check_placement_seed_space
from app.domain.knowledge_contracts import (
    KNOWLEDGE_STATUSES,
    USABLE_STATUSES,
    PromotionApproval,
)
from app.domain.memory_kg_isolation import (
    MEMORY_MERGE_RETRY_MESSAGE,
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


def _own_memory_sources_array_sql() -> str:
    """``ARRAY(...)`` of the viewer's own Memory sources of the notebook. Two
    parameters: notebook id, viewer id."""
    return (
        "ARRAY(SELECT es.id FROM sources es WHERE es.notebook_id = %s "
        f"AND {own_memory_source('es')})"
    )


def _only_own_memory_evidence_sql(alias: str) -> str:
    """Every evidence entry of ``alias`` cites one of the viewer's own Memory
    sources of the notebook: the merge's ``only_own_memory_evidence`` (via
    ``evidence_only_from_own_memory``) in SQL. Two parameters, in order:
    notebook id, viewer id. An entry that is not an object or has no non-empty
    ``source_id`` fails it; SQL ``NULL``, JSON ``null``, ``[]``, ``{}`` and
    ``""`` pass (the Python reading iterates nothing). Any other shape (a
    non-empty object or string, a number) fails: the Python reading refused
    those, or decoded a string as JSON text, which no writer stores."""
    e = f"{alias}.evidence"
    return (
        f"({e} IS NULL OR {e} = 'null'::jsonb OR {e} = '{{}}'::jsonb "
        f"OR {e} = '\"\"'::jsonb OR (jsonb_typeof({e}) = 'array' AND NOT EXISTS ("
        "SELECT 1 FROM jsonb_array_elements(CASE WHEN "
        f"jsonb_typeof({e}) = 'array' THEN {e} ELSE '[]'::jsonb END) ev "
        "WHERE NOT (jsonb_typeof(ev.value) = 'object' "
        "AND COALESCE(ev.value->>'source_id', '') <> '' "
        f"AND (ev.value->>'source_id') = ANY({_own_memory_sources_array_sql()})))))"
    )
# Row ids per statement when the decision recompute re-reads its v1 rows
# (``apply_v1_decision_translation``); a page size, not a cap.
_TRANSLATION_BATCH = 500

# Pagination width for the review queue's endpoint-object lookup — a page size,
# not a cap: every endpoint id is fetched.  500 is the store-wide convention
# (``_DELETE_OBJECT_BATCH_SIZE`` / ``CHUNK_ELEMENT_LOOKUP_BATCH``).
_REVIEW_ENDPOINT_LOOKUP_BATCH = 500


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


def _json_document(value: Any, *, expected: type, field: str):
    if isinstance(value, str):
        value = json.loads(value)
    if value is None or not isinstance(value, expected):
        raise ValueError(f"{field} must be a {expected.__name__}")
    validate_finite_json(value, field=field)
    return value


def _compat_rows(rows, *, json_columns=(), timestamp_columns=()):
    output = []
    for row in rows:
        item = dict(row)
        for column, default in json_columns:
            if column in item and not isinstance(item[column], str):
                if item[column] is None and default is None:
                    continue
                item[column] = json.dumps(
                    default if item[column] is None else item[column],
                    ensure_ascii=False,
                    allow_nan=False,
                )
        for column in timestamp_columns:
            if column in item:
                item[column] = iso_timestamp(item[column])
        output.append(item)
    return output


def _promotion_candidate_for_update(connection, candidate_id: str):
    row = connection.execute(
        "SELECT * FROM promotion_candidates WHERE id=%s FOR UPDATE",
        (candidate_id,),
    ).fetchone()
    return (
        _compat_rows([row], timestamp_columns=("created_at", "updated_at"))[0]
        if row is not None else None
    )


def _lock_promotion_object(connection, object_id: str) -> None:
    """Serialize proposal idempotency across processes for one source object.

    Knowledge and Memory ids are globally unique.  A namespaced hash keeps the
    lock independent from other advisory-lock users; the partial unique index
    remains the final integrity guard, while normal contenders now wait and
    return the winner instead of surfacing ``UniqueViolation``.
    """
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"silicon-notebook:promotion-object:{object_id}",),
    )


def _base_dedup_rows_for_update(
    connection: Any,
    base_notebook_id: str,
    object_type: str,
):
    """Read the live dedup corpus while holding every row in stable id order.

    Promotion evidence is derived only after these locks are acquired.  A
    concurrent merge that won first is therefore visible here; one that loses
    waits and recomputes from the promotion's committed evidence instead of
    either writer overwriting the other.

    ``evidence`` is intentionally NOT selected: seed matching reads only
    ``payload``, and at most one row is merged into, whose evidence
    ``base_dedup_evidence`` re-reads by primary key while this transaction still
    holds its FOR UPDATE lock.  The lock set, the ORDER BY and the WHERE clause
    are unchanged — only the projection got thinner.
    """
    return connection.execute(
        "SELECT id,payload FROM knowledge_objects "
        "WHERE notebook_id=%s AND object_type=%s AND status IN ({}) "
        "ORDER BY id COLLATE \"C\" FOR UPDATE".format(
            ",".join("%s" for _ in USABLE_STATUSES)
        ),
        (base_notebook_id, object_type, *USABLE_STATUSES),
    ).fetchall()


def base_dedup_evidence(connection: Any, object_id: str) -> list:
    """Evidence of the ONE base object a promotion deduped onto — read by
    primary key inside the transaction that already locked it FOR UPDATE, so the
    value is identical to what the (now thinner) corpus read used to carry."""
    row = connection.execute(
        "SELECT evidence FROM knowledge_objects WHERE id=%s", (object_id,)
    ).fetchone()
    return json_value(row["evidence"], []) if row is not None else []


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
        "SELECT concept_seed_version FROM unified_kg_state WHERE notebook_id=%s",
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
        "SELECT seed_a, seed_b FROM concept_merge_candidates WHERE notebook_id=%s "
        "AND status IN ('confirmed','rejected','deferred') AND seed_version=%s "
        "AND (seed_a LIKE '~v1:%%' OR seed_b LIKE '~v1:%%')",
        (notebook_id, version),
    ).fetchall()
    width = len(OVERRIDE_SEED_PREFIX)
    return version, frozenset(
        seed[width:] for row in rows for seed in (row["seed_a"], row["seed_b"])
        if seed and seed.startswith(OVERRIDE_SEED_PREFIX)
    )


def find_base_dedup_match(
    object_type: str, src_payload: dict, base_objs: List[dict], *,
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
        bp = json_value(b["payload"], {})
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


def require_live_promotion_target(connection: Any, base_nb_id: str) -> None:
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
        "SELECT tier FROM notebooks WHERE id=%s FOR UPDATE", (base_nb_id,)
    ).fetchone()
    if row is None or (row["tier"] or "personal") != "base":
        raise ValueError(
            "晋升目标笔记本已不是公共知识库(可能已被删除或降级为个人库): "
            f"{base_nb_id}；请撤回候选后重新指定晋升目标，或联系管理员重新发布该库"
        )


# SQLite 孪生的 `_EFFECTIVE_SEED_SQL`,逐字同一段文本(`seed_{side}` 非空就是它,否则
# canonical 去掉 ``K-`` 前缀;`decided_seed_pairs_from` 的同一条推导)。
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
    def __init__(self, database: PostgresDatabase, seams) -> None:
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
        db: Any,
        notebook_id: str,
        after_object_type: str,
        after_member_object_id: str,
        after_generation: int,
        limit: int,
    ) -> "tuple[list, int]":
        """Z6: one keyset batch of the orphan-cluster sweep — scan ``≤ limit``
        cluster rows, delete whichever of THOSE rows are orphans.

        Cost model (this is the whole point of the two-statement shape):

        * ① page read — ``notebook_id = %s`` equality plus a row-wise
          ``(object_type, member_object_id, generation) > (cursor)`` range on
          the FOUR-column ``uq_clusters_nb_type_member_generation(notebook_id,
          object_type, member_object_id, generation)`` UNIQUE index (batch
          3·W2: the old three-column unique is gone — ``(type, member)`` is no
          longer unique across generations, so the cursor carries the
          generation as its tie-break; the four-column index serves the range
          AND the ORDER BY directly), ``LIMIT %s``. Pure keyset paging with NO
          orphan predicate: the scan stops after ``limit`` index entries, so
          the statement is **strictly O(page)** no matter how many cluster
          rows the notebook has, and no matter how many of them are orphans.
          The sweep is deliberately CROSS-generation (census C class): a dead
          member is dead in every generation. The explicit ``COLLATE "C"`` matches the
          column-level ``text COLLATE "C"`` of ``0001_initial.sql``, so it IS
          the index's own collation — the index stays usable and the ordering
          stays byte-wise, the same total order the SQLite twin gets for free.
          Measured on a 1M-row notebook (local PG 16, page 5000): Index Scan,
          76 buffers, 0.65 ms — and identical at a deep cursor as at a cold
          one, which is what "independent of N" means here.
        * ② bounded delete — driven off the page's PRIMARY KEYS
          (``c.id = ANY(%s)``, at most ``limit`` of them), with the orphan test
          as a correlated ``NOT EXISTS`` probe against ``knowledge_objects``'s
          primary key. Also **O(page)**.

        ⚠ ② deliberately does NOT re-express the page as a key RANGE
          (``> cursor AND <= page_end``), which reads more elegant and was
          measured to be wrong: with no ``LIMIT`` to make the index attractive,
          the planner takes the row-comparison as a mere *filter* and picks a
          Seq Scan of the whole notebook slice plus a Hash Anti Join that hashes
          EVERY ``knowledge_objects`` row — on the same 1M-row fixture, 5000
          rows deleted after touching 1,000,000 (201 ms, and linear in N). The
          PK-list form on that same fixture plans as Index Scan + Nested Loop
          Anti Join, 5000 index probes, 5.3 ms. A PK ``= ANY`` cannot degrade
          into a slice scan; a range predicate can, and silently.

        A full sweep of a notebook holding N cluster rows is therefore
        ``ceil(N / limit)`` batches, O(N) in total but with every statement
        bounded by the page — versus the two shapes this replaces, BOTH of
        which put an O(N) statement on the upload hot path:

        * the original single-shot ``member_object_id NOT IN (SELECT id FROM
          knowledge_objects WHERE notebook_id=%s)`` anti-join (materialised the
          whole per-notebook id set, joined it against every cluster row);
        * its first batched rewrite, whose ``LIMIT`` sat on a subquery already
          filtered by ``NOT EXISTS`` — so the LIMIT bounded the DELETED rows,
          not the SCANNED ones. With zero orphans (the normal state, since the
          producers are zeroed out) every batch still scanned the notebook's
          entire cluster slice before returning empty; with many orphans each
          batch re-scanned the whole remaining range. That is the P1 this
          shape fixes.

        ``k.notebook_id = c.notebook_id`` in the NOT EXISTS probe preserves the
        ORIGINAL notebook-scoped semantics bit-for-bit: a ``member_object_id``
        that exists as a knowledge-object id but under a DIFFERENT notebook
        must still count as orphan for this notebook (exactly what the old
        ``WHERE notebook_id=%s`` on the NOT IN subquery enforced) — dropping
        that join condition would silently change which rows are orphan. The
        ``c.notebook_id = %s`` predicate rides along on the DELETE for the same
        reason ``KnowledgeStore._delete_object_id_batch`` carries one: the
        statement states its own scope instead of resting on the remote fact
        that the ids came from a notebook-scoped page.

        Returns ``(page_rows, deleted_count)``. ``page_rows`` are the rows
        SCANNED this batch, in key order: the caller feeds the last one's
        ``(object_type, member_object_id)`` back as the next cursor (so the
        cursor advances over scanned rows, deleted or not) and stops when the
        page is short. ``deleted_count`` is how many of them were orphans — 0
        is the common case and must NOT stop the loop."""
        page = db.execute(
            "SELECT object_type, member_object_id, generation, id FROM concept_clusters"
            " WHERE notebook_id = %s"
            "   AND (object_type COLLATE \"C\", member_object_id COLLATE \"C\", generation)"
            "       > (%s, %s, %s)"
            " ORDER BY object_type COLLATE \"C\", member_object_id COLLATE \"C\", generation"
            " LIMIT %s",
            (notebook_id, after_object_type, after_member_object_id,
             after_generation, limit),
        ).fetchall()
        if not page:
            return [], 0
        deleted = db.execute(
            "DELETE FROM concept_clusters AS c"
            " WHERE c.notebook_id = %s"
            "   AND c.id = ANY(%s)"
            "   AND NOT EXISTS ("
            "     SELECT 1 FROM knowledge_objects k"
            "     WHERE k.id = c.member_object_id AND k.notebook_id = c.notebook_id"
            "   )"
            # 裸 `id` 与 SQLite 孪生同形(那边的 RETURNING 看不见 DELETE 目标别名)。
            " RETURNING id",
            (notebook_id, [row["id"] for row in page]),
        ).fetchall()
        return page, len(deleted)

    @staticmethod
    def incremental_cluster_rows(db: Any, notebook_id: str, object_type: str):
        # 批 3·W2 A 类:安置(place_new_concepts)的既有簇名切片只看 published
        # 代——在飞代/残代的簇名混进来会把新对象安进读者看不见的簇。绑定参数
        # 标量子查询 = 一次求值 InitPlan;COALESCE 兜「无 state 行 ⇒ 代 0」。
        return db.execute(
            "SELECT DISTINCT canonical_id, canonical_name FROM concept_clusters "
            "WHERE notebook_id=%s AND object_type=%s "
            "AND generation = COALESCE((SELECT cluster_generation "
            "FROM unified_kg_state WHERE notebook_id = %s), 0)",
            (notebook_id, object_type, notebook_id),
        ).fetchall()

    @staticmethod
    def merge_candidate_pairs(db: Any, notebook_id: str, statuses):
        values = tuple(statuses)
        if values == ("pending",):
            return db.execute(
                "SELECT canonical_a, canonical_b FROM concept_merge_candidates "
                "WHERE notebook_id=%s AND status='pending'", (notebook_id,),
            ).fetchall()
        if values == ("confirmed", "rejected", "deferred"):
            return db.execute(
                "SELECT canonical_a, canonical_b FROM concept_merge_candidates "
                "WHERE notebook_id=%s AND status IN ('confirmed','rejected','deferred')",
                (notebook_id,),
            ).fetchall()
        if values == ("confirmed", "rejected", "deferred", "pending"):
            return db.execute(
                "SELECT canonical_a, canonical_b FROM concept_merge_candidates "
                "WHERE notebook_id=%s AND status IN ('confirmed','rejected','deferred','pending')",
                (notebook_id,),
            ).fetchall()
        raise ValueError(f"unsupported lifecycle merge statuses: {values!r}")

    @staticmethod
    def merge_candidate_pairs_for_canonicals(
        db: Any, notebook_id: str, statuses, canonical_ids
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
        placeholders = ",".join("%s" for _ in ids)
        return db.execute(
            f"SELECT canonical_a, canonical_b FROM concept_merge_candidates "
            f"WHERE notebook_id=%s AND {status_sql} "
            f"AND (canonical_a IN ({placeholders}) OR canonical_b IN ({placeholders}))",
            [notebook_id, *ids, *ids],
        ).fetchall()

    @staticmethod
    def merge_candidate_states_for_seeds(
        db: Any, notebook_id: str, statuses, seeds
    ) -> List[Dict[str, str]]:
        """SQLite 孪生同名方法(用途、有效种子的推导、切片驱动与过滤形态见那边;
        `_EFFECTIVE_SEED_SQL` 两侧逐字相同)。

        种子列表经 ``id_binding``(``member_of`` + ``execute_ids``),两端共用同一个绑定
        参数,custom plan 按真实列表出计划。按 ``id`` 排序在 Python 里做(理由见 SQLite
        孪生),Python 的码点序与 ``COLLATE "C"`` / SQLite 二进制序逐字一致。
        ``updated_at`` 经 `iso_timestamp`。"""
        status_sql = _lifecycle_merge_status_sql(statuses)
        values = list(dict.fromkeys(str(seed) for seed in seeds if seed))
        if not values:
            return []
        bound = id_bind_ids(values)
        rows = id_execute_ids(
            db,
            "SELECT id, canonical_a, canonical_b, status, seed_a, seed_b, updated_at "
            f"FROM concept_merge_candidates WHERE notebook_id=%s AND {status_sql} "
            f"AND ({id_member_of(_EFFECTIVE_SEED_SQL.format(side='a'), bound)} "
            f"OR {id_member_of(_EFFECTIVE_SEED_SQL.format(side='b'), bound)})",
            [notebook_id, bound.param, bound.param],
        ).fetchall()
        return sorted((
            {
                "id": row["id"],
                "canonical_a": row["canonical_a"],
                "canonical_b": row["canonical_b"],
                "status": row["status"],
                "seed_a": row["seed_a"] or "",
                "seed_b": row["seed_b"] or "",
                "updated_at": iso_timestamp(row["updated_at"]),
            }
            for row in rows
        ), key=lambda row: row["id"])

    @staticmethod
    def valid_object_ids(
        db: Any, object_ids, *, exclude_memory_derived: bool = False
    ):
        return KnowledgeStore.valid_object_ids(
            db, object_ids, exclude_memory_derived=exclude_memory_derived
        )

    # ------------------------------------------------------------- review
    @staticmethod
    def review_queue_rows(
        connection: Any, notebook_id: str
    ) -> "tuple[List[dict], List[dict]]":
        """SQLite ``GovernanceStore.review_queue_rows``'s mirror — see that
        docstring for why ``evidence`` became a pushed-down ``has_anchor`` flag
        and why objects are now fetched per relation endpoint.  jsonb makes the
        predicate shorter here (no json_valid guard: the column is already
        parsed, only its top-level type needs checking).

        The endpoint lookup keeps its ``notebook_id`` predicate in SQL: unlike
        SQLite, PostgreSQL plans ``notebook_id = %s AND id = ANY(%s)`` as an
        ``pk_knowledge_objects`` scan with ``notebook_id`` as a filter, so there
        is no full-partition regression to dodge here.

        R3 T-A1: the endpoint projection extracts ``payload->>'name'`` in SQL
        instead of shipping the whole ``payload`` document.  The consumer
        (``knowledge_governance.review_queue``) reads exactly two fields off
        these rows — ``object_type`` and ``payload["name"]`` — so every other
        key of every endpoint object's payload was pure transfer/parse cost
        (~500k endpoint rows on the largest production notebook, each payload a
        full claim/concept document).  ``->>`` returns SQL NULL for a missing
        key, a JSON ``null``, or a non-object payload; the caller normalises
        NULL to ``""``, which is what the old ``dict.get("name", "")`` produced
        for the first two.  Registered robustness change (same class as the
        anchor pushdown's): a NON-STRING ``name`` (e.g. a number) used to reach
        ``edge_trust._norm`` as an ``int`` and raise a 500; it now participates
        in scoring as its text form.  ``->>`` renders it the SAME text the
        SQLite twin's ``json_extract`` + ``str()`` does ONLY for a string or an
        integer ``name`` (registered narrowing, codex R3 double review) — a
        JSON bool renders "true"/"false" here vs "1"/"0" on the SQLite side, a
        float can differ in trailing-zero/exponent formatting, and an
        object/array's compact-JSON text is not byte-identical across the two
        engines' serializers.  Not a regression (the OLD path raised on both
        dialects for all of those shapes), but cross-dialect corroboration
        counts and the displayed name can differ for a non-string/non-integer
        ``name`` — registered, not guarded against.

        ⚠ ``name`` deliberately does NOT move into the relation JOIN.  That
        would replace ~500k deduplicated endpoint lookups with 8.35M edges × 2
        payload dereferences (read amplification), and it would resolve names
        for CROSS-NOTEBOOK endpoints that this notebook-scoped lookup leaves
        unnamed — changing the corroboration triple (pinned by
        ``test_governance_read_narrowing.py::
        test_review_queue_rows_filters_cross_notebook_endpoints``)."""
        relations = connection.execute(
            "SELECT kr.id, kr.source_object_id, kr.target_object_id, "
            "kr.edge_type, kr.source_id, kr.review_status, "
            "EXISTS (SELECT 1 FROM jsonb_array_elements("
            "  CASE WHEN jsonb_typeof(kr.evidence) = 'array' "
            "  THEN kr.evidence ELSE '[]'::jsonb END) AS ev(value) "
            " WHERE jsonb_typeof(ev.value) = 'object' "
            "   AND jsonb_typeof(ev.value -> 'quote') = 'string' "
            "   AND btrim(ev.value ->> 'quote', %s) <> ''"
            ") AS has_anchor, "
            "ko_s.object_type AS src_type, ko_t.object_type AS tgt_type "
            "FROM knowledge_relations kr "
            "LEFT JOIN knowledge_objects ko_s ON ko_s.id = kr.source_object_id "
            "LEFT JOIN knowledge_objects ko_t ON ko_t.id = kr.target_object_id "
            "WHERE kr.notebook_id = %s AND kr.review_status != 'rejected' "
            # M1: Memory-derived relations are left out WHOLE (not per viewer:
            # the queue memo is per notebook). Only their owner may review
            # them, so for everyone else they would be items that can never
            # be acted on; nor do they corroborate shared edges.
            f"AND NOT {memory_derived_relation('kr')}",
            (PY_STRIP_WHITESPACE, notebook_id),
        ).fetchall()

        endpoint_ids = _review_endpoint_ids(relations)
        objects: List[Any] = []
        for offset in range(0, len(endpoint_ids), _REVIEW_ENDPOINT_LOOKUP_BATCH):
            batch = endpoint_ids[offset : offset + _REVIEW_ENDPOINT_LOOKUP_BATCH]
            objects.extend(connection.execute(
                "SELECT id, object_type, payload->>'name' AS name "
                "FROM knowledge_objects "
                "WHERE notebook_id = %s AND id = ANY(%s)",
                (notebook_id, batch),
            ).fetchall())
        return (
            _compat_rows(relations),
            # No ``json_columns``: ``name`` already arrives as text, so there is
            # no document left to re-serialize for the caller to re-parse.
            _compat_rows(objects),
        )

    @staticmethod
    def update_edge_review(
        connection: Any, notebook_id: str, relation_id: str, status: str,
        *, actor_id: str | None = None,
    ) -> str:
        """Set ``review_status`` and return the PREVIOUS value (R3 T-A3 P1-2) —
        callers (``KnowledgeGovernanceService.set_edge_review``) need it to
        decide whether a transition can carry-forward the review-queue count
        memos (verified<->pending, neither side 'rejected') or must invalidate
        them (either side 'rejected', including the rejected->rejected no-op).

        Single UPDATE...FROM(SELECT...FOR UPDATE) RETURNING: the subquery locks
        the target row and captures its PRE-update ``review_status`` as
        ``prev`` in the same statement the UPDATE applies the new value in —
        no separate SELECT round-trip, and no window between reading prev and
        writing new where a concurrent writer could interleave. Zero matching
        rows (bad id/notebook) makes the join produce zero rows, so
        ``fetchone()`` returns ``None`` — same "not found" signal the old
        ``cur.rowcount == 0`` check used, now via RETURNING's row count
        instead of the UPDATE's own rowcount.

        M1 (E4-3): a relation derived from someone else's Memory (not
        ``actor_id``'s; an orphan Memory source is nobody's) is filtered out
        in the same locking subquery (``foreign_memory_relation_excluded``),
        so it takes the identical "not found" path as an id that does not
        exist — no existence probe. ``actor_id=None`` (internal callers)
        owns no Memory: every Memory-derived relation is out of reach."""
        if status not in _REVIEW_STATUSES:
            raise ValueError(f"invalid edge review status: {status!r}")
        row = connection.execute(
            "UPDATE knowledge_relations SET review_status=%s "
            "FROM (SELECT kr.id, kr.review_status AS prev FROM knowledge_relations kr "
            "WHERE kr.id=%s AND kr.notebook_id=%s "
            f"AND {foreign_memory_relation_excluded('kr')} FOR UPDATE) old "
            "WHERE knowledge_relations.id=old.id "
            "RETURNING old.prev",
            (status, relation_id, notebook_id, actor_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"relation {relation_id!r} not found in notebook {notebook_id!r}")
        return row["prev"]

    # ------------------------------------------------------------ clusters
    @staticmethod
    def delete_clusters(
        connection: Any, notebook_id: str, object_type: str
    ) -> None:
        lock_cluster_artifact_type(connection, notebook_id, object_type)
        # 在飞代豁免(codex #671 R9 P1):全量重写与 rebuild 重叠时,跨代
        # DELETE 会把已写好的在飞代行一并抹掉,而 rebuild 的认领与双 CAS
        # 都还有效——随后翻转就发布一个该类型为空的代。子查询单语句快照:
        # claimant 的行只在其取号提交后出现,「看得见行 ⇒ 看得见认领」。
        # 本次重写自己的行落 published 代、时间戳新——若被并发翻转退休,
        # 催收窗口会把成员重放进新代(last-publisher-wins,无结构性丢失)。
        connection.execute(
            "DELETE FROM concept_clusters WHERE notebook_id=%s AND object_type=%s "
            "AND generation NOT IN ("
            "  SELECT derived_building_generation FROM unified_kg_state u "
            "  WHERE u.notebook_id = %s AND derived_building_generation != 0)",
            (notebook_id, object_type, notebook_id))

    def insert_clusters(
        self,
        connection: Any,
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
        ``(notebook, object_type)`` 切片读进内存。逐位一致的理由是纯集合论:
        下面的循环只问 ``r["member_object_id"] in existing``,而被问到的 id 恰好
        就是 ``rows`` 里的那些,故把 ``existing`` 限制到 ``rows`` 的 id 集合不改变
        任何一次判定,``added`` 与写入的行也逐位不变。分批走
        ``uq_clusters_nb_type_member_generation(notebook_id, object_type,
        member_object_id)`` 这条唯一索引(前两列等值 + 第三列 IN = 精确 seek)。
        ``lock_cluster_artifact_type`` 仍在最前,写序不变。

        批 3·W2 §2.2 锁序红线:published 代指针必须在 advisory lock **之后**
        读——翻转微事务持全 4 类锁改指针,append 与它串行后要么读到旧 P(行落
        P 代,created_at 在锚点窗口内,催收兜住),要么读到新 G(直接写对),
        两个分支都闭合;锁前读则可能拿旧 P 写在翻转提交之后,永久丢行。
        「直接写对」还要求安置用的概念种子空间仍是已发布的那个(S1):库的第一次
        v2 全量重建可能恰在融合锁外读空间与这里之间翻转,所以空间在同一把锁后
        复核(``concept_seed_version`` 连同覆盖键 ``concept_seed_override``——同一
        空间里后来翻译出的驳回会新增 ``~v1:`` 覆盖键,canonical id 随之改变),
        变了就不写、抛 ``ConceptSeedSpaceMoved``
        由融合方按新空间重新安置——催收只重放退休代里的行,看不到写进新代的
        旧空间行。"""
        lock_cluster_artifact_type(connection, notebook_id, object_type)
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
            # created_at 用 DB 语句时钟(codex #671 R5 P1):催收窗口的锚点是
            # 取号事务的 DB 时钟,append 行若沿用应用时钟,跨机偏斜超过
            # KG_CATCHUP_SKEW_SECONDS 时窗口内落地的行会被判「太旧」而漏搬
            # ——链 a 的成员静默丢到下一次全量重建。clock_timestamp()(语句
            # 时刻,advisory lock 之后)而非 now()(事务起点):行的时间戳
            # ≥ 任何先于它提交的取号锚点,单时钟域闭合,skew 只剩兜 sqlite
            # 侧同机双时钟的形式余量。`now` 形参保留给别的列消费方(此处
            # 不再使用)。
            connection.execute(
                "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,canonical_description,created_at,generation) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,clock_timestamp(),%s)",
                (self.seams.new_id("cc"), notebook_id, r["canonical_id"],
                 r["member_object_id"], r["canonical_name"], object_type,
                 r.get("canonical_description", ""),
                 generation))
            existing.add(r["member_object_id"])
            added += 1
        return added

    @staticmethod
    def _published_cluster_generation(connection: Any, notebook_id: str) -> int:
        """append 写入的目标代 = 当下 published 指针(无 state 行 ⇒ 0,与读侧
        COALESCE 契约同构)。调用方必须已持该类型 advisory lock(见
        insert_clusters docstring 的锁序红线)。"""
        row = connection.execute(
            "SELECT cluster_generation FROM unified_kg_state WHERE notebook_id=%s",
            (notebook_id,),
        ).fetchone()
        return int(row["cluster_generation"]) if row else 0

    def _existing_cluster_members(
        self,
        connection: Any,
        notebook_id: str,
        object_type: str,
        rows: List[dict],
        generation: int,
    ) -> set:
        """本次 ``rows`` 的 member id 中,已经在该切片 published 代里的那些
        (有界 IN,分批)。探针按代收窄:四列唯一按代隔离后,跨代旧行不该再
        挡住本代 append(v1 的「探针跨代失明成静默 no-op」结构性消失)。"""
        member_ids = list(dict.fromkeys(r["member_object_id"] for r in rows))
        size = max(1, int(self.seams.in_chunk_size()))
        found = set()
        for offset in range(0, len(member_ids), size):
            batch = member_ids[offset:offset + size]
            placeholders = ",".join("%s" for _ in batch)
            found.update(r["member_object_id"] for r in connection.execute(
                f"SELECT member_object_id FROM concept_clusters "
                f"WHERE notebook_id=%s AND object_type=%s AND generation=%s "
                f"AND member_object_id IN ({placeholders})",
                [notebook_id, object_type, generation, *batch]).fetchall())
        return found

    # --------------------------------------------------------------- merge
    def insert_merge_candidate(
        self,
        connection: Any,
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
            "INSERT INTO concept_merge_candidates (id,notebook_id,canonical_a,canonical_b,score,status,seed_version,created_at,updated_at) VALUES (%s,%s,%s,%s,%s, 'pending', %s, %s, %s)",
            (
                self.seams.new_id(id_prefix), notebook_id, a, b, score,
                check_concept_seed_version(seed_version),
                normalize_timestamp(now), normalize_timestamp(now),
            ))

    def write_merge_candidate(
        self,
        connection: Any,
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
    def pending_merges(connection: Any, notebook_id: str) -> List[dict]:
        rows = connection.execute(
            "SELECT * FROM concept_merge_candidates WHERE notebook_id=%s "
            "AND status='pending' ORDER BY ordinal",
            (notebook_id,)).fetchall()
        # seed_a / seed_b ride along for the service's spelling-variant flag
        # (kg_merge.merge_candidate_kind), which pops them before the row
        # leaves the service.
        return [{"id": r["id"], "canonical_a": r["canonical_a"], "canonical_b": r["canonical_b"], "score": r["score"], "status": r["status"],
                 "seed_a": r["seed_a"] or "", "seed_b": r["seed_b"] or ""} for r in rows]

    @staticmethod
    def pending_merge_names(
        connection: Any, notebook_id: str, canonical_ids
    ) -> Dict[str, str]:
        """``{canonical_id: canonical_name}`` of the published concept
        clusters among ``canonical_ids`` -- the names the pending-merge list
        shows for its two sides; an id no published cluster row carries is
        absent (the caller falls back to the id). Mirrors the SQLite twin.

        One statement for the whole batch (the caller cuts the response's
        ids into batches), one bounded point lookup per id: a LATERAL
        ``LIMIT 1`` seek on ``idx_clusters_nb_canonical_member_gen``
        (notebook, canonical, member) INCLUDE (generation) in member order --
        the first member row of the published generation, so a hub cluster
        costs one seek, not its member count. The published-generation scalar
        is a bound-parameter InitPlan (evaluated once). The id array is one
        parameter run unprepared (same reason as
        ``concept_cluster_detail_rows(canonical_ids=...)``)."""
        values = [str(value) for value in canonical_ids]
        if not values:
            return {}
        rows = connection.execute(
            "SELECT c.cid AS canonical_id, n.canonical_name AS canonical_name "
            "FROM unnest(%s::text[]) AS c(cid) CROSS JOIN LATERAL ("
            "SELECT cc.canonical_name FROM concept_clusters cc "
            "WHERE cc.notebook_id=%s AND cc.canonical_id=c.cid "
            "AND cc.generation = COALESCE((SELECT cluster_generation "
            "FROM unified_kg_state WHERE notebook_id = %s), 0) "
            'ORDER BY cc.member_object_id COLLATE "C" LIMIT 1) n',
            (values, notebook_id, notebook_id),
            prepare=False,
        ).fetchall()
        return {
            str(r["canonical_id"]): str(r["canonical_name"] or "") for r in rows
        }

    @staticmethod
    def pending_merges_batch(
        connection: Any, notebook_id: str, limit: int
    ) -> List[dict]:
        """Bounded fetch preserving SQLite's historical rowid insertion order."""
        rows = connection.execute(
            "SELECT * FROM concept_merge_candidates WHERE notebook_id=%s "
            "AND status='pending' ORDER BY ordinal LIMIT %s",
            (notebook_id, limit),
        ).fetchall()
        return [{"id": r["id"], "canonical_a": r["canonical_a"], "canonical_b": r["canonical_b"], "score": r["score"], "status": r["status"]} for r in rows]

    @staticmethod
    def has_pending_merges(connection: Any, notebook_id: str) -> bool:
        row = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM concept_merge_candidates WHERE notebook_id=%s AND status='pending') AS e",
            (notebook_id,),
        ).fetchone()
        return bool(row["e"])

    @staticmethod
    def set_merge_decision(
        connection: Any,
        notebook_id: str,
        candidate_id: str,
        status: str,
        now: str,
    ) -> str | None:
        if status not in ("confirmed", "rejected"):
            raise ValueError(f"invalid merge status: {status!r}")
        target = connection.execute(
            "SELECT canonical_a, canonical_b FROM concept_merge_candidates "
            "WHERE id=%s AND notebook_id=%s",
            (candidate_id, notebook_id),
        ).fetchone()
        if target is None:
            return None
        # Lock the complete displayed pair in one deterministic order. Legacy
        # duplicates can be selected concurrently by separate requests; locking
        # only each selected id lets their later sibling updates deadlock.
        siblings = connection.execute(
            "SELECT id, status FROM concept_merge_candidates WHERE notebook_id=%s AND "
            "((canonical_a=%s AND canonical_b=%s) OR (canonical_a=%s AND canonical_b=%s)) "
            "ORDER BY id COLLATE \"C\" FOR UPDATE",
            (notebook_id, target["canonical_a"], target["canonical_b"],
             target["canonical_b"], target["canonical_a"]),
        ).fetchall()
        if not siblings:
            return None
        previous_status = (
            "confirmed" if any(r["status"] == "confirmed" for r in siblings)
            else str(siblings[0]["status"])
        )
        # One row represents a displayed canonical-pair decision.  Settle any
        # legacy duplicate rows, including older decisions, in the same write.
        connection.execute(
            "UPDATE concept_merge_candidates SET status=%s, updated_at=%s "
            "WHERE notebook_id=%s AND "
            "((canonical_a=%s AND canonical_b=%s) OR (canonical_a=%s AND canonical_b=%s))",
            (status, normalize_timestamp(now), notebook_id,
             target["canonical_a"], target["canonical_b"],
             target["canonical_b"], target["canonical_a"]))
        return previous_status

    @staticmethod
    def record_merge_review(
        connection: Any,
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
            SET status=%s, confidence=%s, rationale=%s, reviewed_by='llm', updated_at=%s
            WHERE id=%s AND notebook_id=%s
            """,
            (
                status,
                confidence,
                rationale,
                normalize_timestamp(now),
                candidate_id,
                notebook_id,
            ),
        )

    @staticmethod
    def delete_pending_merges(
        connection: Any, notebook_id: str, *, lock_ids: Sequence[str] = (),
    ) -> None:
        """The pending refresh's delete, one statement: lock every pending row
        of the notebook AND ``lock_ids`` (the v1 decision rows the refresh
        then translates) in ``id COLLATE "C"`` order — the order
        ``set_merge_decision`` locks a displayed pair's rows (legacy duplicates
        included) — then delete the pending ones. Locking the pending rows by
        the DELETE and the decision rows later would take them in the opposite
        order to a concurrent human decision on a pair with both kinds of row,
        and the two would deadlock (S1 step 5). A row updated meanwhile is
        re-checked by the lock and deleted only if still pending."""
        bound = id_bind_ids(sorted(set(lock_ids)))
        id_execute_ids(
            connection,
            "WITH locked AS (SELECT id, status FROM concept_merge_candidates "
            f"WHERE notebook_id=%s AND (status='pending' OR {id_member_of('id', bound)}) "
            "ORDER BY id COLLATE \"C\" FOR UPDATE) "
            "DELETE FROM concept_merge_candidates c USING locked l "
            "WHERE c.notebook_id=%s AND c.id = l.id AND l.status = 'pending'",
            [notebook_id, bound.param, notebook_id])

    @staticmethod
    def insert_pending_merge_rows(
        connection: Any, rows: List[tuple], *, seed_version: int
    ) -> None:
        """Refresh pending candidates in the caller's transaction. Rows carry
        seed_a/seed_b — the rebuild-stable decision keys (PR#145) — verbatim,
        minted in seed space ``seed_version`` (always explicit)."""
        version = check_concept_seed_version(seed_version)
        execute_many(
            connection,
            "INSERT INTO concept_merge_candidates "
            "(id,notebook_id,canonical_a,canonical_b,seed_a,seed_b,score,status,seed_version,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s, 'pending', %s, %s, %s)",
            [
                (*row[:-2], version,
                 normalize_timestamp(row[-2]), normalize_timestamp(row[-1]))
                for row in rows
            ],
        )

    def decided_pairs(self, notebook_id: str) -> Dict[tuple, str]:
        with self.database.connect() as db:
            rows = db.execute("SELECT canonical_a, canonical_b, status FROM concept_merge_candidates WHERE notebook_id=%s AND status IN ('confirmed','rejected')", (notebook_id,)).fetchall()
        return {(r["canonical_a"], r["canonical_b"]): r["status"] for r in rows}

    @staticmethod
    def decided_seed_pairs_from(
        connection: Any, notebook_id: str, *, seed_version: Optional[int],
    ) -> Dict[frozenset, str]:
        """{frozenset({seed_a, seed_b}): status} for confirmed/rejected/deferred.

        Seed-name keys are STABLE across rebuilds (canonical ids shift when a
        cluster's min-member changes; seed names don't). Legacy rows written
        before the seed_a/seed_b columns existed carry '' → fall back to
        strip-"K-"(canonical), matching the old decided_pairs key derivation.
        ``seed_version``: only the rows minted in that seed space; ``None`` —
        every row (see the SQLite twin). Always explicit."""
        version_sql, params = "", [notebook_id]
        if seed_version is not None:
            version_sql = " AND seed_version=%s"
            params.append(check_concept_seed_version(seed_version))
        rows = connection.execute(
            "SELECT canonical_a, canonical_b, seed_a, seed_b, status "
            "FROM concept_merge_candidates WHERE notebook_id=%s "
            "AND status IN ('confirmed','rejected','deferred')" + version_sql,
            params,
        ).fetchall()
        out: Dict[frozenset, str] = {}
        for r in rows:
            out[frozenset(_decision_keys(r))] = r["status"]
        return out

    def decided_seed_pairs(
        self, notebook_id: str, *, seed_version: Optional[int]
    ) -> Dict[frozenset, str]:
        with self.database.connect() as db:
            return self.decided_seed_pairs_from(
                db, notebook_id, seed_version=seed_version)

    @staticmethod
    def merge_decision_rows(
        connection: Any, notebook_id: str, *, seed_version: int,
        include_pending: bool = False,
    ) -> List[dict]:
        """The decided rows of one seed space, ``[{id, key_a, key_b, status}]``
        in id order; ``include_pending`` adds the pending rows (see the SQLite
        twin)."""
        statuses = ("'confirmed','rejected','deferred','pending'" if include_pending
                    else "'confirmed','rejected','deferred'")
        rows = connection.execute(
            "SELECT id, canonical_a, canonical_b, seed_a, seed_b, status "
            "FROM concept_merge_candidates WHERE notebook_id=%s "
            f"AND status IN ({statuses}) AND seed_version=%s "
            "ORDER BY id",
            (notebook_id, check_concept_seed_version(seed_version)),
        ).fetchall()
        return [_decision_row(r) for r in rows]

    def apply_v1_decision_translation(
        self,
        connection: Any,
        notebook_id: str,
        pairs: Dict[str, List[Tuple[str, str]]],
        expected: Dict[str, Tuple[str, str, str]],
        now: str,
    ) -> Tuple[int, List[str]]:
        """Persist the recompute of v1 decisions into seed space 2 in the
        caller's pending-refresh transaction (see the SQLite twin). The rows
        are read ``FOR UPDATE`` in id order, so a concurrent decision on one
        of them waits for this transaction and the plan's check (same keys,
        same status) sees what is committed."""
        ids = sorted(pairs)
        found: Dict[str, Any] = {}
        for at in range(0, len(ids), _TRANSLATION_BATCH):
            batch = ids[at:at + _TRANSLATION_BATCH]
            for r in connection.execute(
                "SELECT id, canonical_a, canonical_b, seed_a, seed_b, status, score, "
                "confidence, rationale, reviewed_by, created_at "
                "FROM concept_merge_candidates WHERE notebook_id=%s AND seed_version=1 "
                "AND status IN ('confirmed','rejected','deferred') "
                "AND id = ANY(%s) ORDER BY id FOR UPDATE",
                (notebook_id, batch),
            ).fetchall():
                found[r["id"]] = r
        grew = 0
        done: List[str] = []
        now_ts = normalize_timestamp(now)
        updates: List[tuple] = []
        inserts: List[tuple] = []
        for row_id in ids:
            r = found.get(row_id)
            if r is None:
                continue
            row = _decision_row(r)
            if (row["key_a"], row["key_b"], row["status"]) != tuple(expected[row_id]):
                continue
            first, rest = pairs[row_id][0], pairs[row_id][1:]
            updates.append((first[0], first[1], "K-" + first[0], "K-" + first[1], now_ts,
                            row_id, notebook_id))
            inserts.extend(
                (self.seams.new_id("mc"), notebook_id, "K-" + a, "K-" + b, a, b,
                 r["score"], r["status"], r["confidence"], r["rationale"],
                 r["reviewed_by"], r["created_at"], now_ts) for a, b in rest)
            if r["status"] in ("confirmed", "rejected"):
                grew += len(rest)
            done.append(row_id)
        for at in range(0, len(updates), _TRANSLATION_BATCH):
            execute_many(
                connection,
                "UPDATE concept_merge_candidates SET seed_a=%s, seed_b=%s, "
                "canonical_a=%s, canonical_b=%s, seed_version=2, updated_at=%s "
                "WHERE id=%s AND notebook_id=%s", updates[at:at + _TRANSLATION_BATCH])
        for at in range(0, len(inserts), _TRANSLATION_BATCH):
            execute_many(
                connection,
                "INSERT INTO concept_merge_candidates "
                "(id,notebook_id,canonical_a,canonical_b,seed_a,seed_b,score,status,"
                "confidence,rationale,reviewed_by,seed_version,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,2,%s,%s)",
                inserts[at:at + _TRANSLATION_BATCH])
        return grew, done

    # ------------------------------------------------------ merge review job
    @staticmethod
    def merge_review_job_row(
        connection: Any, notebook_id: str
    ) -> "dict | None":
        return connection.execute(
            "SELECT status,total,done,error FROM merge_review_jobs WHERE notebook_id=%s",
            (notebook_id,)).fetchone()

    @staticmethod
    def begin_merge_review_job(
        connection: Any, notebook_id: str, now: str
    ) -> "int | None":
        """Single-flight start: returns None when a job is already running,
        else upserts the running row and returns the pending total — one
        atomic block inside the caller's write transaction."""
        total = connection.execute(
            "SELECT COUNT(*) c FROM concept_merge_candidates "
            "WHERE notebook_id=%s AND status='pending'", (notebook_id,)).fetchone()["c"]
        row = connection.execute(
            """
            INSERT INTO merge_review_jobs (notebook_id,status,total,done,started_at,updated_at,error)
            VALUES (%s, 'running', %s, 0, %s, %s, '')
            ON CONFLICT(notebook_id) DO UPDATE SET
              status='running', total=excluded.total, done=0,
              started_at=excluded.started_at, updated_at=excluded.updated_at, error=''
            WHERE merge_review_jobs.status != 'running'
            RETURNING total
            """,
            (
                notebook_id,
                total,
                normalize_timestamp(now),
                normalize_timestamp(now),
            )).fetchone()
        return int(row["total"]) if row is not None else None

    @staticmethod
    def set_merge_review_progress(
        connection: Any, notebook_id: str, done: int, now: str
    ) -> None:
        connection.execute("UPDATE merge_review_jobs SET done=%s, updated_at=%s WHERE notebook_id=%s",
                           (done, normalize_timestamp(now), notebook_id))

    @staticmethod
    def finish_merge_review_job(
        connection: Any, notebook_id: str, status: str, error: str, now: str
    ) -> None:
        connection.execute("UPDATE merge_review_jobs SET status=%s, error=%s, updated_at=%s WHERE notebook_id=%s",
                           (status, error, normalize_timestamp(now), notebook_id))

    # ------------------------------------------------------------ conflicts
    def write_conflict_candidate(
        self,
        connection: Any,
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
        resolved_json = (
            None
            if resolved_payload is None
            else jsonb(_json_document(
                resolved_payload, expected=dict, field="resolved conflict payload"
            ))
        )
        values = (cid, notebook_id, kind, left_ref, right_ref,
                  conflict_type, resolution, winner_ref, resolved_json,
                  confidence, rationale, normalize_timestamp(now),
                  normalize_timestamp(now))
        if reset_epoch is None:
            connection.execute(
                """
                INSERT INTO kg_conflict_candidates
                  (id, notebook_id, kind, left_ref, right_ref,
                   conflict_type, resolution, winner_ref, resolved_payload,
                   confidence, rationale, status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s)
                """,
                values,
            )
            return cid
        # K1: insert only while the graph is still the one the caller read
        # (kg_reset_epoch unchanged; a missing state row reads as epoch 0,
        # graph_seq_row's convention). FOR SHARE: a delete whose reset UPSERT
        # is in flight makes this wait for its commit and then compare
        # against the advanced epoch, instead of slipping in underneath it.
        # It cannot cover an insert that commits while the final reset runs
        # but BEFORE its UPSERT (no row lock to wait on yet, and the reset's
        # REPEATABLE READ snapshot cannot see the row): that window — about
        # the length of the final transaction — is closed by the delete's
        # post-commit sweep of the drain-only steps
        # (KnowledgeLifecycleService._sweep_drain_only_residue).
        cur = connection.execute(
            """
            INSERT INTO kg_conflict_candidates
              (id, notebook_id, kind, left_ref, right_ref,
               conflict_type, resolution, winner_ref, resolved_payload,
               confidence, rationale, status, created_at, updated_at)
            SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s
            WHERE COALESCE((SELECT kg_reset_epoch FROM unified_kg_state
                            WHERE notebook_id=%s FOR SHARE), 0) = %s
            """,
            (*values, notebook_id, int(reset_epoch)),
        )
        return cid if cur.rowcount else None

    @staticmethod
    def pending_conflicts(
        connection: Any, notebook_id: str
    ) -> List[dict]:
        rows = connection.execute(
            "SELECT * FROM kg_conflict_candidates WHERE notebook_id=%s AND status='pending'",
            (notebook_id,),
        ).fetchall()
        return _compat_rows(
            rows,
            json_columns=(("resolved_payload", None),),
            timestamp_columns=("created_at", "updated_at"),
        )

    @staticmethod
    def set_conflict_status(
        connection: Any,
        notebook_id: str,
        candidate_id: str,
        status: str,
        now: str,
    ) -> None:
        if status not in ("applied", "rejected"):
            raise ValueError(f"invalid conflict status: {status!r}")
        cur = connection.execute(
            "UPDATE kg_conflict_candidates SET status=%s, updated_at=%s "
            "WHERE id=%s AND notebook_id=%s",
            (status, normalize_timestamp(now), candidate_id, notebook_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"conflict candidate {candidate_id!r} not found")

    def get_conflict_candidate(
        self, notebook_id: str, candidate_id: str
    ) -> Optional[dict]:
        """Fetch one conflict candidate inside its notebook authorization scope."""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM kg_conflict_candidates WHERE id=%s AND notebook_id=%s",
                (candidate_id, notebook_id),
            ).fetchone()
        return (
            _compat_rows(
                [row],
                json_columns=(("resolved_payload", None),),
                timestamp_columns=("created_at", "updated_at"),
            )[0]
            if row is not None
            else None
        )

    # ------------------------------------------------------------ whitelist
    @staticmethod
    def concept_whitelist_terms(connection: Any) -> set:
        return {r["term"] for r in connection.execute("SELECT term FROM concept_whitelist").fetchall()}

    @staticmethod
    def concept_whitelist_rows(connection: Any) -> List[dict]:
        return _compat_rows(connection.execute(
            "SELECT term, note, created_at FROM concept_whitelist "
            "ORDER BY term COLLATE \"C\""
        ).fetchall(), timestamp_columns=("created_at",))

    @staticmethod
    def add_whitelist_term(
        connection: Any, term: str, note: str, now: str
    ) -> None:
        connection.execute(
            "INSERT INTO concept_whitelist (term,note,created_at) VALUES (%s,%s,%s) "
            "ON CONFLICT (term) DO UPDATE SET note=EXCLUDED.note,"
            "created_at=EXCLUDED.created_at",
            (term, note, normalize_timestamp(now)),
        )

    @staticmethod
    def remove_whitelist_term(connection: Any, term: str) -> None:
        connection.execute("DELETE FROM concept_whitelist WHERE term = %s", (term,))

    # ------------------------------------------------------------ promotion
    @staticmethod
    def promotion_object_type_row(
        connection: Any, notebook_id: str, object_id: str,
        *, actor_id: str | None = None,
    ) -> "dict | None":
        """``object_type`` plus ``memory_derived`` (the ``memory_sql`` D4
        classifier) of the object a generic promotion proposal names, so the
        service can refuse a Memory-derived object without another read.

        The owner is read in this same locking read: an object derived from
        someone else's Memory (not ``actor_id``'s, or an orphan Memory source)
        is filtered out by ``foreign_memory_object_excluded`` and comes back
        as ``None`` — the caller's "not found", identical to a missing id. So
        ``memory_derived`` is true only for the proposer's own Memory."""
        return connection.execute(
            "SELECT ko.object_type, "
            f"{memory_derived_object('ko')} AS memory_derived "
            "FROM knowledge_objects ko "
            "WHERE ko.id=%s AND ko.notebook_id=%s "
            f"AND {foreign_memory_object_excluded('ko')} FOR UPDATE",
            (object_id, notebook_id, actor_id),
        ).fetchone()

    @staticmethod
    def notebook_tier_row(
        connection: Any, notebook_id: str
    ) -> "dict | None":
        return connection.execute(
            "SELECT tier FROM notebooks WHERE id=%s", (notebook_id,)
        ).fetchone()

    @staticmethod
    def promotion_object_rows(
        connection: Any, object_ids: List[str]
    ) -> List[dict]:
        if not object_ids:
            return []
        placeholders = ",".join("%s" for _ in object_ids)
        rows = connection.execute(
            f"SELECT id, payload, evidence FROM knowledge_objects "
            f"WHERE id IN ({placeholders})",
            object_ids,
        ).fetchall()
        return _compat_rows(
            rows,
            json_columns=(("payload", {}), ("evidence", [])),
        )

    @staticmethod
    def notebook_name_rows(
        connection: Any, notebook_ids: List[str]
    ) -> List[dict]:
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
        placeholders = ",".join("%s" for _ in notebook_ids)
        return connection.execute(
            f"SELECT id, name FROM notebooks WHERE id IN ({placeholders})",
            notebook_ids,
        ).fetchall()

    @staticmethod
    def safe_memory_evidence(
        connection: Any, notebook_id: str, provenance: dict
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
                "WHERE s.id=%s AND e.id=%s AND s.notebook_id=%s",
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
        connection: Any, object_id: str
    ) -> "dict | None":
        row = connection.execute(
            "SELECT payload FROM knowledge_objects WHERE id=%s", (object_id,)
        ).fetchone()
        return (
            _compat_rows([row], json_columns=(("payload", {}),))[0]
            if row is not None else None
        )

    @staticmethod
    def conflict_resolution_rows(
        connection: Any, notebook_id: str
    ) -> "tuple[List[dict], List[dict], dict | None]":
        """Detection-shaped read: no evidence bodies, vectors filtered in SQL.

        Mirrors the SQLite statement exactly (see its docstring for why evidence
        moved to a bounded by-id read, why the embedding join case-folds
        ``object_type``, why Memory-derived objects never reach detection, and
        what that exclusion costs here: a hash anti join over every Memory
        source in the deployment, measured there).
        """
        objects = connection.execute(
            "SELECT o.id, o.object_type, o.payload, o.status "
            "FROM knowledge_objects o "
            "WHERE o.notebook_id=%s AND o.status != 'deprecated' "
            f"AND NOT {memory_derived_object('o')}",
            (notebook_id,),
        ).fetchall()
        vectors = connection.execute(
            "SELECT e.object_id AS object_id, e.vector AS vector "
            "FROM knowledge_embeddings e "
            "JOIN knowledge_objects o ON o.id = e.object_id "
            "WHERE e.notebook_id=%s AND o.status != 'deprecated' "
            "AND lower(o.object_type) IN ('concept','claim') "
            f"AND NOT {memory_derived_object('o')}",
            (notebook_id,),
        ).fetchall()
        notebook = connection.execute(
            "SELECT tier FROM notebooks WHERE id=%s", (notebook_id,)
        ).fetchone()
        return (
            _compat_rows(
                objects,
                json_columns=(("payload", {}),),
            ),
            [
                {**dict(row), "vector": bytes(row["vector"])}
                for row in vectors
            ],
            notebook,
        )

    @staticmethod
    def conflict_relation_count(
        connection: Any,
        notebook_id: str,
        *,
        max_rows: int | None = None,
    ) -> int:
        if max_rows is None:
            row = connection.execute(
                "SELECT COUNT(*) AS c FROM knowledge_relations WHERE notebook_id=%s",
                (notebook_id,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT COUNT(*) AS c FROM ("
                "SELECT 1 FROM knowledge_relations WHERE notebook_id=%s LIMIT %s"
                ") AS bounded_relations",
                (notebook_id, max(1, int(max_rows))),
            ).fetchone()
        return int(row["c"])

    @staticmethod
    def conflict_relation_rows(
        connection: Any,
        notebook_id: str,
        *,
        max_rows: int | None = None,
    ) -> "List[dict]":
        """Thin relation projection for conflict detection.

        Only the columns detection reads: no evidence bodies (fetched by id
        for surviving candidates) and no ``review_status`` — detection has
        never filtered rejected relations and must keep not filtering them.
        Memory-derived relations are excluded in SQL, before the ``LIMIT``
        rail (see the SQLite docstring).
        """
        sql = (
            "SELECT r.id, r.source_object_id, r.target_object_id, r.edge_type "
            "FROM knowledge_relations r WHERE r.notebook_id=%s "
            f"AND NOT {memory_derived_relation('r')}"
        )
        params: tuple = (notebook_id,)
        if max_rows is not None:
            sql += " LIMIT %s"
            params += (max(1, int(max_rows)),)
        rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def conflict_relation_evidence_rows(
        connection: Any, relation_ids
    ) -> "List[dict]":
        """Bounded by-id evidence read for the surviving edge candidates."""
        ids = list(relation_ids)
        if not ids:
            return []
        placeholders = ",".join("%s" for _ in ids)
        rows = connection.execute(
            f"SELECT id, evidence FROM knowledge_relations WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        return _compat_rows(rows, json_columns=(("evidence", []),))

    @staticmethod
    def promotion_candidate_identity(
        connection: Any, candidate_id: str
    ) -> "dict | None":
        """Plain-read immutable routing fields before aggregate lock ordering."""
        row = connection.execute(
            "SELECT id,notebook_id,object_id,object_type FROM promotion_candidates "
            "WHERE id=%s",
            (candidate_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def promotion_candidate_row(
        connection: Any, candidate_id: str
    ) -> "dict | None":
        row = connection.execute(
            "SELECT * FROM promotion_candidates WHERE id=%s FOR UPDATE", (candidate_id,)
        ).fetchone()
        return (
            _compat_rows([row], timestamp_columns=("created_at", "updated_at"))[0]
            if row is not None else None
        )

    @staticmethod
    def active_promotion_for_object(
        connection: Any, object_id: str
    ) -> "dict | None":
        _lock_promotion_object(connection, object_id)
        row = connection.execute(
            "SELECT * FROM promotion_candidates "
            "WHERE object_id=%s AND status NOT IN ('approved','rejected')",
            (object_id,),
        ).fetchone()
        return (
            _compat_rows([row], timestamp_columns=("created_at", "updated_at"))[0]
            if row is not None else None
        )

    @staticmethod
    def insert_promotion_candidate(
        connection: Any,
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
            VALUES (%s, %s, %s, %s, 'proposed', '', '', '', %s, %s, %s)
            """,
            (
                cand_id,
                notebook_id,
                object_id,
                object_type,
                normalize_timestamp(now),
                normalize_timestamp(now),
                target_base_id,
            ),
        )

    @staticmethod
    def promotion_queue_rows(
        connection: Any, status_filter: Optional[str]
    ) -> List[dict]:
        if status_filter:
            rows = connection.execute(
                "SELECT * FROM promotion_candidates WHERE status=%s "
                "ORDER BY created_at ASC, id ASC",
                (status_filter,),
            ).fetchall()
        else:
            rows = connection.execute(
            "SELECT * FROM promotion_candidates "
            "WHERE status IN ('proposed','under_review') "
            "ORDER BY created_at ASC, id ASC"
            ).fetchall()
        return _compat_rows(rows, timestamp_columns=("created_at", "updated_at"))

    @staticmethod
    def mounted_public_base_ids(
        connection: Any, notebook_id: str
    ) -> list[str]:
        """本库挂载的**公共知识库** id —— 晋升只能进公共知识库,不能进别人的个人库。
        原 first_base_notebook_row 的全局 LIMIT 1 在多领域下语义已不成立。"""
        rows = connection.execute(
            "SELECT b.id AS id " + MOUNT_JOIN + " AND b.tier = 'base'" + MOUNT_ORDER,
            (notebook_id,),
        ).fetchall()
        return [row["id"] for row in rows]

    @staticmethod
    def promotion_target_column_ready(connection: Any) -> bool:
        """target_base_id 列(_migration_20)是否已就绪。供离线维护工具(Task 9的
        scripts/backfill_promotion_targets.py)在读 pending_promotion_targets /
        写 set_promotion_target 之前自检 schema 版本, 避免在未迁移库上把"没有
        target_base_id 列"误判成"没有待处理候选"。"""
        return bool(
            connection.execute(
                "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
                "WHERE table_schema=current_schema() "
                "AND table_name='promotion_candidates' "
                "AND column_name='target_base_id') AS present"
            ).fetchone()["present"]
        )

    @staticmethod
    def pending_promotion_targets(connection: Any) -> List[dict]:
        """status IN ('proposed','under_review') 且 target_base_id 为空串的候选行 ——
        SCHEMA_VERSION=20 之前创建、_migration_20 未回填 target_base_id 的存量候选。
        target_base_id 只在 propose 时可设(insert_promotion_candidate), 没有别的接口
        能给已存在候选补目标, 这是唯一的存量补救读口, 供离线 CLI 使用。"""
        return _compat_rows(connection.execute(
            "SELECT id, notebook_id, object_id, object_type, status, created_at "
            "FROM promotion_candidates "
            "WHERE status IN ('proposed','under_review') AND target_base_id='' "
            "ORDER BY notebook_id, created_at, id"
        ).fetchall(), timestamp_columns=("created_at",))

    @staticmethod
    def set_promotion_target(
        connection: Any, candidate_id: str, target_base_id: str, now: str
    ) -> None:
        """写入存量候选的 target_base_id —— 唯一的"事后补写"入口, 只供离线补救 CLI
        (scripts/backfill_promotion_targets.py)使用。正常 propose 路径走
        insert_promotion_candidate 的 target_base_id 参数, 不经过这里。"""
        connection.execute(
            "UPDATE promotion_candidates SET target_base_id=%s, updated_at=%s WHERE id=%s",
            (target_base_id, normalize_timestamp(now), candidate_id),
        )

    @staticmethod
    def first_admin_user_id(connection: Any) -> str:
        row = connection.execute(
            "SELECT id FROM users WHERE role='admin' ORDER BY created_at,id LIMIT 1"
        ).fetchone()
        return str(row["id"]) if row is not None else ""

    @staticmethod
    def locate_approved_base_object(
        connection: Any, candidate_id: str, base_match_id: str
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
            "WHERE source_candidate_id=%s ORDER BY created_at ASC, id ASC LIMIT 1",
            (candidate_id,),
        ).fetchone()
        if hit is not None:
            return str(hit["id"]), str(hit["notebook_id"])
        if not base_match_id:
            return "", ""
        match_row = connection.execute(
            "SELECT notebook_id FROM knowledge_objects WHERE id=%s",
            (base_match_id,),
        ).fetchone()
        return base_match_id, (str(match_row["notebook_id"]) if match_row else "")

    @staticmethod
    def locate_approved_memory_base_objects(
        connection: Any, candidate_id: str
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
            "WHERE source_candidate_id=%s ORDER BY id",
            (candidate_id,),
        ).fetchall()
        ids = [str(row["id"]) for row in rows]
        base_notebook_id = str(rows[0]["notebook_id"]) if rows else ""
        return ids, base_notebook_id

    def approve_memory_promotion_in_transaction(
        self,
        connection: Any,
        candidate_id: str,
        candidates: List[dict],
        evidence: List[dict],
        reviewer_id: str,
        now: str,
    ) -> dict:
        """Promote a sanitized Memory extraction through the normal base dedupe path."""
        cand = _promotion_candidate_for_update(connection, candidate_id)
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
        # B-12 (PR-E8): the evidence cards point at the member's notebook; the
        # public library gets its own "收录自个人记忆：<title>" source and one
        # element per card, and the cards are rewritten to point at them
        # (app.domain.promotion_provenance).
        memory_row = connection.execute(
            "SELECT title FROM memory_items WHERE id=%s", (cand["object_id"],)
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
            base_objs = _base_dedup_rows_for_update(
                connection, base_nb_id, object_type
            )
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
                    "UPDATE knowledge_objects SET evidence=%s,updated_at=%s WHERE id=%s",
                    (
                        jsonb(_json_document(
                            merged_evidence, expected=list, field="knowledge evidence"
                        )),
                        normalize_timestamp(now),
                        base_match_id,
                    ),
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
                    "VALUES (%s,%s,%s,'approved','',%s,%s,%s,'',%s,%s)",
                    (
                        base_object_id,
                        base_nb_id,
                        object_type,
                        jsonb(_json_document(
                            payload, expected=dict, field="knowledge payload"
                        )),
                        jsonb(_json_document(
                            evidence, expected=list, field="knowledge evidence"
                        )),
                        candidate_id,
                        normalize_timestamp(now),
                        normalize_timestamp(now),
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
            "UPDATE promotion_candidates SET status='approved',base_match_id=%s,"
            "reviewed_by=%s,updated_at=%s WHERE id=%s",
            (
                merged_object_ids[0] if merged_object_ids else "",
                reviewer_id,
                normalize_timestamp(now),
                candidate_id,
            ),
        )
        return {
            "base_notebook_id": base_nb_id,
            "base_object_ids": base_object_ids,
            "created_object_ids": created_object_ids,
            "merged_object_ids": merged_object_ids,
        }

    def approve_promotion_in_transaction(
        self,
        connection: Any,
        candidate_id: str,
        now: str,
        reviewer_id: str = "curator",
    ) -> PromotionApproval:
        """The in-transaction body of approve_promotion: copy the personal
        object into the base corpus, deduplicating against existing base
        objects of the same type via the kg_merge seed clustering. The caller
        owns the write transaction and the post-commit hooks (embed /
        invalidate / dirty)."""
        cand = _promotion_candidate_for_update(connection, candidate_id)
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
        # Memory classifier in the same read. The candidate row is already
        # locked; nothing has been written yet.
        src = connection.execute(
            "SELECT ko.*, "
            f"{memory_derived_object('ko')} AS memory_derived "
            "FROM knowledge_objects ko WHERE ko.id=%s",
            (cand["object_id"],),
        ).fetchone()
        if src is None:
            # Q5: the candidate exists (it is locked above) but the object it
            # names is gone — e.g. its source was re-analysed and minted new
            # ids. Distinct from "candidate missing" (KeyError → 404): the
            # service closes this proposal as rejected in this transaction.
            raise PromotionObjectMissing()
        if src["memory_derived"]:
            # M1: a proposal queued before the propose-side refusal (or racing
            # it) must never copy a Memory object's payload or raw evidence
            # into the public library. The service rejects the candidate in
            # this same transaction and surfaces the refusal.
            raise MemoryPromotionRefused(PROMOTION_APPROVE_MESSAGE)
        src_payload = json_value(src["payload"], {})
        # B-12 (PR-E8): the promoted evidence points at the public library's
        # own "收录自：<title>" sources; on a merge only these incoming entries
        # are rewritten (app.domain.promotion_provenance).
        src_evidence = materialize_promotion_evidence(
            connection, base_nb_id, json_value(src["evidence"], []), now,
            fallback_origin_notebook_id=str(cand["notebook_id"] or ""),
        )

        # Cross-corpus dedup against existing base objects of the same type.
        base_objs = _base_dedup_rows_for_update(
            connection, base_nb_id, object_type
        )
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
                "UPDATE knowledge_objects SET evidence=%s, updated_at=%s WHERE id=%s",
                (
                    jsonb(_json_document(
                        merged_evidence, expected=list, field="knowledge evidence"
                    )),
                    normalize_timestamp(now),
                    base_match_id,
                ),
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
                VALUES (%s, %s, %s, 'approved', '', %s, %s, %s, '', %s, %s)
                """,
                (
                    base_object_id,
                    base_nb_id,
                    object_type,
                    jsonb(_json_document(
                        src_payload, expected=dict, field="knowledge payload"
                    )),
                    jsonb(_json_document(
                        src_evidence, expected=list, field="knowledge evidence"
                    )),
                    candidate_id,
                    normalize_timestamp(now),
                    normalize_timestamp(now),
                ),
            )
            created_new_object = True
            KnowledgeStore.replace_object_sources(
                connection, base_object_id, base_nb_id,
                json.dumps(src_evidence, ensure_ascii=False),
            )

        connection.execute(
            "UPDATE promotion_candidates "
            "SET status='approved', base_match_id=%s, reviewed_by=%s, updated_at=%s "
            "WHERE id=%s",
            (
                base_match_id,
                reviewer_id or "curator",
                normalize_timestamp(now),
                candidate_id,
            ),
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
        connection: Any,
        candidate_id: str,
        reason: str,
        now: str,
        reviewer_id: str = "curator",
    ) -> None:
        connection.execute(
            "UPDATE promotion_candidates "
            "SET status='rejected', reason=%s, reviewed_by=%s, updated_at=%s "
            "WHERE id=%s",
            (reason, reviewer_id, normalize_timestamp(now), candidate_id),
        )

    @staticmethod
    def withdraw_memory_promotions_on(
        connection: Any,
        notebook_id: str,
        memory_ids: List[str],
        reason: str,
        now: str,
    ) -> int:
        """Reject the still-active proposals of Memory rows the caller is
        about to hard-delete, inside the caller's delete transaction.

        ``promotion_candidates.object_id`` has no foreign key to
        ``memory_items``: a proposal outliving its Memory stays in the curator
        queue forever and can never be approved (the pinned snapshot is gone).
        Withdrawal is ``status='rejected'`` because the queue has no other
        terminal state for "never reviewed"; an already approved proposal is
        left alone — its base object is independent of the Memory row. A
        withdrawal is not a review: ``reviewed_by`` is left empty (no
        reviewer acted) and ``reason`` carries the machine code. The curator
        queue lists only active proposals, and no UI renders ``reason``.

        Scoped by notebook AND the Memory ids; the caller already holds the
        Memory rows ``FOR UPDATE`` (same Memory-then-candidate lock order as
        ``approve_promotion`` / ``reject_promotion``), so a concurrent
        proposal either committed before this statement or fails on the
        missing row afterwards."""
        if not memory_ids:
            return 0
        cursor = connection.execute(
            "UPDATE promotion_candidates "
            "SET status='rejected', reason=%s, reviewed_by='', updated_at=%s "
            "WHERE notebook_id=%s AND object_type='memory' AND object_id=ANY(%s) "
            "AND status IN ('proposed','under_review')",
            (
                reason,
                normalize_timestamp(now),
                notebook_id,
                list(memory_ids),
            ),
        )
        return int(cursor.rowcount or 0)

    @staticmethod
    def purge_memory_review_rows_on(
        connection: Any,
        notebook_id: str,
        source_ids: List[str],
        *,
        bridge_canonical_ids: Dict[int, List[str]],
    ) -> int:
        """Remove what names the objects of Memory sources about to be torn
        down, in the caller's transaction (before the objects go):

        1. the merge candidates (any status) naming a cluster that contains
           one of those objects, or naming a canonical id minted from one of
           them (``memory_sql.cluster_seed_object_id`` — the one minted-id
           rule the copy uses too), or
           naming a bridge canonical id of one of them
           (``bridge_canonical_ids``: ``{seed_version: ids}``, a row matched
           only by the ids of its own ``seed_version``) that no cluster row
           carries — a name only the Memory gave; a live cluster carrying the
           same bridge id keeps its candidates;
        2. every such cluster WHOLE — every generation, all member rows: its
           canonical id, name and description may carry the Memory's text on
           every shared member's row (plan §3 step 3; the teardown marks the
           notebook dirty, so the rebuild re-clusters the surviving members);
        3. the conflict candidates (any status: ``rationale`` and
           ``resolved_payload`` may quote the Memory) whose left, right or
           winner reference is one of the objects or relations. A candidate
           between shared objects only is never touched: its references are
           shared object or relation ids.

        "Object of a Memory source" is ``memory_sql``'s
        ``memory_derived_object`` over the given sources; "a cluster of a
        Memory" is a cluster with such a member or seeded by such an object —
        the member and seed arms of ``memory_sql.memory_cluster``, restricted
        to the purged objects. Every list that grows with the KG is
        derived in SQL from the owned-objects subquery; the source ids are one
        purge page (at most 200) and the bridge ids go through ``id_binding``.
        Returns the number of cluster rows removed."""
        ids = sorted(set(source_ids))
        if not ids:
            return 0
        owned = (
            "owned AS (SELECT ko.id FROM knowledge_objects ko "
            "WHERE ko.notebook_id = %s AND ko.source_id = ANY(%s) "
            f"AND {memory_derived_object('ko')})"
        )
        doomed = (
            "SELECT mc.canonical_id FROM concept_clusters mc WHERE mc.notebook_id = %s "
            "AND (mc.member_object_id IN (SELECT id FROM owned) "
            f"OR {cluster_seed_object_id('mc')} IN (SELECT id FROM owned))"
        )
        v1 = id_bind_ids(sorted(set(bridge_canonical_ids.get(1, ()))))
        v2 = id_bind_ids(sorted(set(bridge_canonical_ids.get(2, ()))))

        def bridged(side: str) -> str:
            # A row is matched only by the bridge ids of its OWN seed space.
            return (
                f"((m.seed_version = 1 AND {id_member_of(f'm.canonical_{side}', v1)}) "
                f"OR (m.seed_version = 2 AND {id_member_of(f'm.canonical_{side}', v2)}))"
            )

        id_execute_ids(
            connection,
            f"WITH {owned}, doomed AS ({doomed}) "
            "DELETE FROM concept_merge_candidates m WHERE m.notebook_id = %s AND ("
            "m.canonical_a IN (SELECT canonical_id FROM doomed) "
            "OR m.canonical_b IN (SELECT canonical_id FROM doomed) "
            f"OR {cluster_seed_object_id('m', 'canonical_a')} IN (SELECT id FROM owned) "
            f"OR {cluster_seed_object_id('m', 'canonical_b')} IN (SELECT id FROM owned) "
            f"OR ({bridged('a')} AND NOT EXISTS ("
            "SELECT 1 FROM concept_clusters xa WHERE xa.notebook_id = m.notebook_id "
            "AND xa.canonical_id = m.canonical_a)) "
            f"OR ({bridged('b')} AND NOT EXISTS ("
            "SELECT 1 FROM concept_clusters xb WHERE xb.notebook_id = m.notebook_id "
            "AND xb.canonical_id = m.canonical_b)))",
            (notebook_id, ids, notebook_id, notebook_id,
             v1.param, v2.param, v1.param, v2.param),
        )
        removed = connection.execute(
            f"WITH {owned} DELETE FROM concept_clusters c WHERE c.notebook_id = %s "
            f"AND c.canonical_id IN ({doomed})",
            (notebook_id, ids, notebook_id, notebook_id),
        ).rowcount
        connection.execute(
            "WITH refs AS (SELECT ko.id FROM knowledge_objects ko "
            "WHERE ko.notebook_id = %s AND ko.source_id = ANY(%s) "
            f"AND {memory_derived_object('ko')} UNION ALL "
            "SELECT kr.id FROM knowledge_relations kr "
            "WHERE kr.notebook_id = %s AND kr.source_id = ANY(%s) "
            f"AND {memory_derived_relation('kr')}) "
            "DELETE FROM kg_conflict_candidates c WHERE c.notebook_id = %s "
            "AND (c.left_ref IN (SELECT id FROM refs) "
            "OR c.right_ref IN (SELECT id FROM refs) "
            "OR c.winner_ref IN (SELECT id FROM refs))",
            (notebook_id, ids, notebook_id, ids, notebook_id),
        )
        return int(removed or 0)

    @staticmethod
    def strip_sources_evidence_on(
        connection: Any,
        notebook_id: str,
        source_ids: List[str],
        now: str,
        *,
        backfilled: Optional[bool] = None,
        cited: Optional[Iterable[str]] = None,
    ) -> List[str]:
        """Detach several sources' evidence from the objects ANOTHER source
        owns, in the caller's transaction.

        Deleting a source removes every object whose evidence references it
        (``clear_sources_graph_state``). For an object minted by that source
        this is right; for an object minted by a different source that merely
        gained this source's evidence (a manual ``merge_knowledge`` of a
        Memory-derived object into a shared one), it deletes the shared
        object with everything its own source contributed. Run this first
        and the teardown that follows only reaches the rows these sources
        minted: the foreign object keeps its payload and its other evidence,
        and loses exactly the items citing one of ``source_ids``.

        "Owned" is the object's primary ``source_id`` column. The candidate
        lookup mirrors the teardown's (reverse index when backfilled,
        evidence containment per source otherwise). Every foreign object is
        then locked in ONE statement, in id order: two members leaving at
        once whose objects were merged into the same shared objects take
        those row locks in the same global order, so they queue instead of
        deadlocking. Writes are scoped by ``notebook_id``. ``backfilled``
        and ``cited`` pass what a caller in the same transaction already read
        (``lock_memory_teardown_objects_on``: the reverse-index verdict and,
        without a certified index, the containment scan's object ids), so
        neither is read twice. Returns the stripped object ids."""
        ids = sorted(set(source_ids))
        if not ids:
            return []
        if backfilled is None:
            backfilled = KnowledgeStore.source_index_backfilled(connection, notebook_id)
        if backfilled:
            rows = connection.execute(
                "SELECT ko.id, ko.evidence FROM knowledge_objects ko "
                "WHERE ko.notebook_id = %s "
                "AND (ko.source_id IS NULL OR ko.source_id <> ALL(%s)) "
                "AND ko.id IN (SELECT kos.object_id FROM knowledge_object_sources kos "
                "WHERE kos.source_id = ANY(%s) AND kos.notebook_id = %s) "
                "ORDER BY ko.id COLLATE \"C\" FOR UPDATE OF ko",
                (notebook_id, ids, ids, notebook_id),
            ).fetchall()
        else:
            candidates: set[str] = set()
            if cited is not None:
                # The containment scan the caller already ran in this
                # transaction (the lock below drops the sources' own rows).
                candidates.update(cited)
            else:
                for source_id in ids:
                    candidates.update(
                        row["id"]
                        for row in connection.execute(
                            "SELECT id FROM knowledge_objects "
                            "WHERE notebook_id = %s AND source_id IS DISTINCT FROM %s "
                            "AND evidence @> jsonb_build_array("
                            "jsonb_build_object('source_id', %s::text))",
                            (notebook_id, source_id, source_id),
                        ).fetchall()
                    )
            found = id_bind_ids(sorted(candidates))
            rows = id_execute_ids(
                connection,
                "SELECT id, evidence FROM knowledge_objects "
                f"WHERE notebook_id = %s AND {id_member_of('id', found)} "
                "AND (source_id IS NULL OR source_id <> ALL(%s)) "
                "ORDER BY id COLLATE \"C\" FOR UPDATE",
                (notebook_id, found.param, ids),
            ).fetchall() if candidates else []
        wanted = set(ids)
        stripped: List[str] = []
        for row in rows:
            kept = [
                item
                for item in json_value(row["evidence"], [])
                if not (isinstance(item, dict) and item.get("source_id") in wanted)
            ]
            connection.execute(
                "UPDATE knowledge_objects SET evidence = %s, updated_at = %s "
                "WHERE id = %s AND notebook_id = %s",
                (
                    jsonb(_json_document(
                        kept, expected=list, field="knowledge evidence"
                    )),
                    normalize_timestamp(now),
                    row["id"],
                    notebook_id,
                ),
            )
            stripped.append(row["id"])
        if stripped:
            objects = id_bind_ids(stripped)
            id_execute_ids(
                connection,
                "DELETE FROM knowledge_object_sources WHERE notebook_id = %s "
                f"AND {id_member_of('object_id', objects)} AND source_id = ANY(%s)",
                (notebook_id, objects.param, ids),
            )
        return stripped

    @staticmethod
    def lock_memory_teardown_objects_on(
        connection: Any, notebook_id: str, source_ids: List[str]
    ) -> tuple[List[dict], bool, Optional[set]]:
        """First step of clearing Memory sources' graph (removal and re-ingest
        alike), under the caller's ``FOR UPDATE`` source locks: lock, in ONE
        id-ordered statement, every object the teardown will write — the
        sources' own objects and the objects ANOTHER source owns that cite
        them (``strip_sources_evidence_on``'s candidates). Two Memories
        merged into each other's objects and removed at once (A(S)→B(T),
        D(T)→C(S), then T and S) otherwise lock C-then-B and B-then-C in
        their strip and their delete and deadlock; with every row taken up
        front in one global order they queue. The candidate lookup is the
        teardown's (reverse index when backfilled, evidence containment per
        source otherwise); while the source locks are held no merge can add
        a citation (a merge takes FOR KEY SHARE on those sources first), so
        the set cannot grow before the delete.

        Returns the sources' own rows ``{id, source_id, status, marker}``
        (``marker`` = ``MERGED_INTO_PAYLOAD_KEY``), the backfill verdict and,
        without a certified reverse index, the containment scan's object ids
        (``None`` otherwise) — both reused by the strip that follows, so no
        read is made twice."""
        ids = sorted(set(source_ids))
        if not ids:
            return [], False, None
        owners = id_bind_ids(ids)
        key = MERGED_INTO_PAYLOAD_KEY
        backfilled = KnowledgeStore.source_index_backfilled(connection, notebook_id)
        columns = (
            f"ko.id, ko.source_id, ko.status, ko.payload->>'{key}' AS marker, "
            f"COALESCE(ko.payload->>'{ABSORBS_PAYLOAD_KEY}', '') = 'true' AS absorbs"
        )
        cited: Optional[set] = None
        if backfilled:
            rows = id_execute_ids(
                connection,
                f"SELECT {columns} FROM knowledge_objects ko "
                f"WHERE ko.notebook_id = %s AND ({id_member_of('ko.source_id', owners)} "
                "OR ko.id IN (SELECT kos.object_id FROM knowledge_object_sources kos "
                f"WHERE {id_member_of('kos.source_id', owners)} AND kos.notebook_id = %s)) "
                "ORDER BY ko.id COLLATE \"C\" FOR UPDATE OF ko",
                (notebook_id, owners.param, owners.param, notebook_id),
            ).fetchall()
        else:
            candidates = {
                row["id"]
                for row in id_execute_ids(
                    connection,
                    "SELECT id FROM knowledge_objects WHERE notebook_id = %s "
                    f"AND {id_member_of('source_id', owners)}",
                    (notebook_id, owners.param),
                ).fetchall()
            }
            cited = set()
            for source_id in ids:
                cited.update(
                    row["id"]
                    for row in connection.execute(
                        "SELECT id FROM knowledge_objects "
                        "WHERE notebook_id = %s AND evidence @> jsonb_build_array("
                        "jsonb_build_object('source_id', %s::text))",
                        (notebook_id, source_id),
                    ).fetchall()
                )
            candidates |= cited
            found = id_bind_ids(sorted(candidates))
            rows = id_execute_ids(
                connection,
                f"SELECT {columns} FROM knowledge_objects ko "
                f"WHERE ko.notebook_id = %s AND {id_member_of('ko.id', found)} "
                "ORDER BY ko.id COLLATE \"C\" FOR UPDATE OF ko",
                (notebook_id, found.param),
            ).fetchall() if candidates else []
        wanted = set(ids)
        own = [dict(row) for row in rows if row["source_id"] in wanted]
        return own, backfilled, cited

    @staticmethod
    def merge_followers_on(
        connection: Any, notebook_id: str, source_ids: List[str], target_ids: List[str]
    ) -> List[tuple[str, str]]:
        """``(object id, marker)`` of the deprecated objects whose merge marker
        names one of ``target_ids`` (objects about to be cleared), owned by a
        Memory source of this notebook other than ``source_ids`` whose Memory
        row still exists. Driven by the notebook's Memory sources
        (``idx_sources_nb_hidden_type``), then their objects by source:
        bounded by the notebook's Memory objects, not by the notebook."""
        if not target_ids:
            return []
        owners = id_bind_ids(sorted(set(source_ids)))
        targets = id_bind_ids(sorted(set(target_ids)))
        marker = f"(ko.payload->>'{MERGED_INTO_PAYLOAD_KEY}')"
        rows = id_execute_ids(
            connection,
            f"SELECT ko.id, {marker} AS marker FROM sources s "
            "JOIN knowledge_objects ko ON ko.source_id = s.id "
            "AND ko.notebook_id = s.notebook_id "
            f"WHERE s.notebook_id = %s AND {memory_source_type_predicate('s.source_type')} "
            f"AND {id_not_member_of('s.id', owners)} AND ko.status = 'deprecated' "
            f"AND {id_member_of(marker, targets)} "
            "AND EXISTS (SELECT 1 FROM memory_items m WHERE m.id = s.memory_id) "
            "ORDER BY ko.id COLLATE \"C\"",
            (notebook_id, owners.param, targets.param),
        ).fetchall()
        return [(str(row["id"]), str(row["marker"])) for row in rows]

    @staticmethod
    def apply_merge_follow_actions_on(
        connection: Any,
        actions: Sequence[MergeFollowAction],
        now: str,
        *,
        wait: bool,
        lock_timeout_ms: Optional[int] = None,
    ) -> tuple[List[MergeFollowAction], List[MergeFollowAction]]:
        """Apply ``actions`` (``plan_merge_follow_actions``) in the caller's
        transaction, per notebook. Lock order, source → object rows: the
        objects' Memory sources take ``FOR KEY SHARE`` in id order (only
        those whose Memory row still exists), then the object rows
        ``FOR UPDATE`` in id order; a row still deprecated whose marker still
        equals ``expected_target`` is rewritten (``followed_payload``;
        ``FOLLOW_RESTORE`` also sets ``approved``), anything else is left
        alone (idempotent).

        ``wait=False`` (inside a teardown, which already holds source and
        object locks): both lock statements ``SKIP LOCKED`` — the holder may
        be that Memory's removal or re-ingest, relation completion, an
        extraction publish, an element swap, or a transaction about to roll
        back — and what could not be locked is returned as skipped, for the
        caller to re-run after its commit with ``wait=True`` (a short
        transaction of its own that holds nothing else). ``lock_timeout_ms``
        bounds that wait (``SET LOCAL lock_timeout``): a holder that keeps its
        lock longer fails the re-run, which is left to the backstop. A
        re-point whose target is not among the locked rows (deleted while the
        statement waited, or held under ``SKIP LOCKED``) is not applied and is
        returned with the skipped ones. Returns ``(applied, skipped)``."""
        applied: List[MergeFollowAction] = []
        skipped: List[MergeFollowAction] = []
        skip = "" if wait else " SKIP LOCKED"
        if wait and lock_timeout_ms:
            connection.execute(f"SET LOCAL lock_timeout = '{int(lock_timeout_ms)}ms'")
        by_notebook: Dict[str, Dict[str, MergeFollowAction]] = {}
        for action in actions:
            by_notebook.setdefault(action.notebook_id, {})[action.object_id] = action
        stamp = normalize_timestamp(now)
        for notebook_id in sorted(by_notebook):
            wanted = by_notebook[notebook_id]
            found = id_bind_ids(sorted(wanted))
            live = sorted({
                str(row["id"])
                for row in id_execute_ids(
                    connection,
                    "SELECT ko.id FROM knowledge_objects ko JOIN sources s "
                    f"ON s.id = ko.source_id WHERE {id_member_of('ko.id', found)} "
                    "AND ko.notebook_id = %s "
                    f"AND {memory_source_type_predicate('s.source_type')} "
                    "AND EXISTS (SELECT 1 FROM memory_items m WHERE m.id = s.memory_id) "
                    f"ORDER BY s.id FOR KEY SHARE OF s{skip}",
                    (found.param, notebook_id),
                ).fetchall()
            })
            # A re-point's target gets the follower gate (ABSORBS_PAYLOAD_KEY)
            # if it lacks it; its row is locked with the followers in ONE
            # id-ordered statement, so the two row sets never lock out of order.
            targets = {
                wanted[object_id].repoint_to for object_id in live
                if wanted[object_id].kind == FOLLOW_REPOINT and wanted[object_id].repoint_to
            }
            locked = []
            if live:
                bound = id_bind_ids(sorted(set(live) | targets))
                locked = id_execute_ids(
                    connection,
                    "SELECT id, status, payload FROM knowledge_objects "
                    f"WHERE {id_member_of('id', bound)} AND notebook_id = %s "
                    f"ORDER BY id COLLATE \"C\" FOR UPDATE{skip}",
                    (bound.param, notebook_id),
                ).fetchall()
            by_locked = {str(row["id"]): row for row in locked}
            done = set()
            gated: set = set()
            deferred: List[MergeFollowAction] = []
            for object_id in live:
                row = by_locked.get(object_id)
                if row is None:
                    continue
                action = wanted[object_id]
                done.add(action.object_id)
                if action.kind == FOLLOW_REPOINT and action.repoint_to not in by_locked:
                    # The target was not locked: under SKIP LOCKED someone holds
                    # it (the re-run will wait for it); while waiting it was
                    # deleted. Re-pointing at a missing object would hand the
                    # row to the backstop with stale holder knowledge, and
                    # restoring blindly could make a second copy of knowledge
                    # another holder still carries — so the row stays as it is
                    # (its marker names a missing object) and is reported back:
                    # the caller re-runs the backstop, whose fresh re-check
                    # picks another eligible holder or, with none, restores.
                    deferred.append(action)
                    continue
                payload = json_value(row["payload"], {})
                if row["status"] != "deprecated" or (
                    payload.get(MERGED_INTO_PAYLOAD_KEY) != action.expected_target
                ):
                    continue
                connection.execute(
                    "UPDATE knowledge_objects SET payload = %s, status = %s, "
                    "updated_at = %s WHERE id = %s AND notebook_id = %s",
                    (
                        jsonb(followed_payload(payload, action)),
                        "approved" if action.kind == FOLLOW_RESTORE else "deprecated",
                        stamp,
                        action.object_id,
                        notebook_id,
                    ),
                )
                applied.append(action)
                target = by_locked.get(action.repoint_to) if action.kind == FOLLOW_REPOINT else None
                if target is not None and action.repoint_to not in gated:
                    gated.add(action.repoint_to)
                    target_payload = json_value(target["payload"], {})
                    if not target_payload.get(ABSORBS_PAYLOAD_KEY):
                        connection.execute(
                            "UPDATE knowledge_objects SET payload = %s "
                            "WHERE id = %s AND notebook_id = %s",
                            (jsonb(marked_absorbs(target_payload)), action.repoint_to,
                             notebook_id),
                        )
            if not wait:
                skipped.extend(
                    action for object_id, action in sorted(wanted.items())
                    if object_id not in done
                )
            skipped.extend(deferred)
        return applied, skipped

    @staticmethod
    def detach_memory_merges_on(
        connection: Any, notebook_id: str, source_ids: List[str], now: str
    ) -> MemoryMergeDetach:
        """The merge half of clearing Memory sources' graph, removal and
        re-ingest alike, under the caller's source locks: lock every row the
        teardown writes (``lock_memory_teardown_objects_on``); settle the
        objects merged into the sources' objects (``merge_followers_on`` →
        ``plan_merge_follow_actions`` → ``apply_merge_follow_actions_on``
        without waiting); strip the sources' own entries from the objects
        other sources own (``strip_sources_evidence_on``). Returns what was
        applied and what was skipped (to re-run after the commit)."""
        own, backfilled, cited = GovernanceStore.lock_memory_teardown_objects_on(
            connection, notebook_id, source_ids
        )
        applied: List[MergeFollowAction] = []
        skipped: List[MergeFollowAction] = []
        # The follower read only when one of the sources' own objects has
        # absorbed an own-Memory merge (ABSORBS_PAYLOAD_KEY): a plain Memory
        # removal or edit pays the up-front lock and nothing else.
        followers = (
            GovernanceStore.merge_followers_on(
                connection, notebook_id, source_ids, [row["id"] for row in own]
            )
            if any(row["absorbs"] for row in own)
            else []
        )
        if followers:
            actions = plan_merge_follow_actions(
                notebook_id,
                followers,
                {row["id"]: (row["status"], row["marker"] or "") for row in own},
            )
            applied, skipped = GovernanceStore.apply_merge_follow_actions_on(
                connection, actions, now, wait=False
            )
        GovernanceStore.strip_sources_evidence_on(
            connection, notebook_id, source_ids, now, backfilled=backfilled, cited=cited
        )
        return MemoryMergeDetach(tuple(applied), tuple(skipped))

    def heal_merged_away_candidates(
        self,
        limit: int,
        after: tuple = ("", ""),
        *,
        notebook_id: Optional[str] = None,
    ) -> tuple[List[MergeFollowAction], tuple]:
        """The self-healing backstop's read, on a read-only connection of its
        own (never inside a write transaction): one keyset page — keyed on
        ``(source id, object id)`` — of follow-up actions for the deprecated
        Memory objects whose merge marker names an object that no longer
        exists and whose own Memory row still exists: what a skipped
        in-transaction step whose after-commit re-run also failed left
        behind. ``notebook_id`` limits it to one notebook.

        Bounded by the Memory volume, never by the tables: the Memory sources
        with a live Memory row are read first (``idx_sources_nb_hidden_type``,
        a partial index of the hidden sources), then their objects by source
        (``idx_knowledge_objects_source``), ``_HEAL_SOURCE_BATCH`` sources per
        statement; with nothing to heal that is the same walk and an empty
        page. Each candidate is re-checked with ``heal_action``: if another
        object still carries all of its own evidence entries (a chain link
        the skipped step would have re-pointed to), it is re-pointed there
        instead of restored, so the backstop never restores a second copy.
        Returns ``(actions, next_after)``; no lock (the apply step locks and
        re-checks every row)."""
        limit = max(1, int(limit))
        key = MERGED_INTO_PAYLOAD_KEY
        actions: List[MergeFollowAction] = []
        next_after = tuple(after)
        with self.database.connect() as db:
            db.read_only = True
            scope = " AND s.notebook_id = %s" if notebook_id else ""
            sources = sorted(
                str(row["id"])
                for row in db.execute(
                    "SELECT s.id FROM sources s JOIN memory_items m ON m.id = s.memory_id "
                    f"WHERE {memory_source_type_predicate('s.source_type')}{scope}",
                    (notebook_id,) if notebook_id else (),
                ).fetchall()
                if str(row["id"]) >= next_after[0]
            )
            backfilled: Dict[str, bool] = {}
            for offset in range(0, len(sources), _HEAL_SOURCE_BATCH):
                bound = id_bind_ids(sources[offset:offset + _HEAL_SOURCE_BATCH])
                rows = id_execute_ids(
                    db,
                    # One correlated probe of idx_knowledge_objects_source per
                    # Memory source (LATERAL): with few deprecated rows in its
                    # statistics the planner would otherwise walk a status
                    # index over the whole table.
                    f"SELECT ko.id, ko.source_id, ko.notebook_id, ko.marker, ko.evidence "
                    f"FROM unnest({bound.sql}) AS src(id) CROSS JOIN LATERAL ("
                    f"SELECT o.id, o.source_id, o.notebook_id, o.payload->>'{key}' AS marker, "
                    "o.evidence FROM knowledge_objects o WHERE o.source_id = src.id "
                    f"AND o.status = 'deprecated' AND o.payload->>'{key}' IS NOT NULL "
                    "AND NOT EXISTS (SELECT 1 FROM knowledge_objects t "
                    f"WHERE t.id = o.payload->>'{key}')) ko",
                    (bound.param,),
                ).fetchall()
                for row in sorted(rows, key=lambda r: (str(r["source_id"]), str(r["id"]))):
                    position = (str(row["source_id"]), str(row["id"]))
                    if position <= next_after:
                        continue
                    evidence = json_value(row["evidence"], [])
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
        connection: Any, row: Any, backfilled: Dict[str, bool]
    ) -> List[tuple]:
        """``(id, status, evidence)`` of the objects of OTHER sources citing
        the candidate's own source — where a merge put its entries (a sibling
        of the same source quoting the same element is not a merge result):
        the reverse index when the notebook's is certified, evidence
        containment otherwise (background only)."""
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
                "WHERE kos.source_id = %s AND kos.notebook_id = %s AND o.id <> %s "
                "AND o.source_id IS DISTINCT FROM %s",
                (row["source_id"], notebook_id, row["id"], row["source_id"]),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT id, status, evidence, payload FROM knowledge_objects "
                "WHERE notebook_id = %s AND id <> %s AND source_id IS DISTINCT FROM %s "
                "AND evidence @> jsonb_build_array("
                "jsonb_build_object('source_id', %s::text))",
                (notebook_id, row["id"], row["source_id"], row["source_id"]),
            ).fetchall()
        return [
            (str(r["id"]), str(r["status"]), json_value(r["evidence"], []),
             json_value(r["payload"], {}))
            for r in rows
        ]

    @staticmethod
    def detach_promotion_sources_on(
        connection: Any, rows: List[dict], now: str
    ) -> List[str]:
        """PR-E8: deleting a public library's promotion source removes ITS
        entries from the evidence of every object citing it, and deletes only
        the objects left with no evidence entry at all.

        The generic teardown deletes every object whose evidence names the
        source, which is right for an object the source minted but would
        delete a NATIVE object a promotion was merged into (its own document's
        evidence included). A promotion source mints nothing (promoted objects
        carry ``source_id = ''``), so every citing object goes through
        ``strip_sources_evidence_on`` (same candidate rule, id-ordered lock,
        reverse-index rows removed); the teardown that follows finds nothing
        of these sources left. Returns the deleted object ids."""
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
            found = id_bind_ids(sorted(stripped))
            emptied = sorted(
                str(row["id"])
                for row in id_execute_ids(
                    connection,
                    "SELECT id, evidence FROM knowledge_objects "
                    f"WHERE {id_member_of('id', found)}",
                    (found.param,),
                ).fetchall()
                if not any(
                    isinstance(item, dict) for item in json_value(row["evidence"], [])
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
        connection: Any,
        notebook_id: str,
        object_id: str,
        payload,
        now: str,
        *,
        actor_id: str | None = None,
        own_status_only: bool = False,
    ) -> dict:
        """The in-transaction body of update_knowledge: validate, apply the
        partial update and return the refetched row. ``payload`` is the
        KnowledgeUpdate model (status/payload/owner partial edit).

        M1 (E4-3): an object derived from someone else's Memory (not
        ``actor_id``'s; an orphan Memory source is nobody's) is filtered out
        in the locking read, so it raises the same ``KeyError`` as a missing
        id — no existence probe, no write. ``actor_id=None`` (internal
        callers) owns no Memory.

        ``own_status_only`` (the caller lacks ``knowledge:write``, user ruling
        2026-10-22): only a status change between approved and deprecated of
        her own Memory object supported by her own Memory alone is applied;
        anything else raises ``NeedsKnowledgeWrite`` before any write. An
        unlocked read refuses first (no row lock for an id that is not hers);
        acceptance is decided on the locked row (``_own_status_toggle_allowed``)."""
        if own_status_only:
            shaped = (
                payload.payload is None and payload.owner is None
                and payload.status in OWN_STATUS_TOGGLE
            )
            if not shaped or connection.execute(
                "SELECT ko.id FROM knowledge_objects ko "
                "WHERE ko.id = %s AND ko.notebook_id = %s "
                f"AND {memory_derived_object('ko')} "
                f"AND {foreign_memory_object_excluded('ko')} "
                f"AND {_only_own_memory_evidence_sql('ko')}",
                (object_id, notebook_id, actor_id, notebook_id, actor_id),
            ).fetchone() is None:
                raise NeedsKnowledgeWrite()
        row = connection.execute(
            "SELECT ko.* FROM knowledge_objects ko WHERE ko.id = %s "
            f"AND ko.notebook_id = %s AND {foreign_memory_object_excluded('ko')} "
            "FOR UPDATE",
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
        document = (
            _json_document(payload.payload, expected=dict, field="knowledge payload")
            if payload.payload is not None
            else _json_document(row["payload"], expected=dict, field="knowledge payload")
        )
        if payload.status is not None or payload.payload is not None:
            # A hand edit of status or content clears the merge marker: only a
            # merge writes it, so a hand-deprecated object is never restored.
            document = patched_payload(
                document,
                _json_document(row["payload"], expected=dict, field="knowledge payload"),
            )
        new_payload = jsonb(document)
        new_status = payload.status if payload.status is not None else row["status"]
        new_owner = payload.owner if payload.owner is not None else row["owner"]
        # Stamp last_reviewed whenever a curator changes status.
        last_reviewed = normalize_timestamp(now) if payload.status is not None else (
            row["last_reviewed"] if "last_reviewed" in row.keys() else None
        )
        connection.execute(
            "UPDATE knowledge_objects SET payload = %s, status = %s, owner = %s, "
            "last_reviewed = %s, updated_at = %s WHERE id = %s AND notebook_id = %s",
            (
                new_payload,
                new_status,
                new_owner,
                last_reviewed,
                normalize_timestamp(now),
                object_id,
                notebook_id,
            ),
        )
        result = connection.execute(
            "SELECT * FROM knowledge_objects WHERE id = %s AND notebook_id = %s",
            (object_id, notebook_id),
        ).fetchone()
        return {
            **dict(result),
            "payload": json.dumps(result["payload"], ensure_ascii=False),
            "evidence": json.dumps(result["evidence"], ensure_ascii=False),
            "created_at": iso_timestamp(result["created_at"]),
            "updated_at": iso_timestamp(result["updated_at"]),
            "last_reviewed": iso_timestamp(result["last_reviewed"]),
        }

    @staticmethod
    def _own_status_toggle_allowed(
        connection: Any,
        notebook_id: str,
        actor_id: str | None,
        row: Any,
        payload,
    ) -> bool:
        """The read-level ``PATCH`` on the locked row: a status change alone,
        between approved and deprecated, of an object whose primary source and
        every evidence entry are the caller's own Memory sources of this
        notebook -- the merge's own-pair rule on one object
        (``merge_evidence_source_ids``, then one ``own_memory_source`` read over
        the cited ids and the primary source)."""
        if payload.payload is not None or payload.owner is not None:
            return False
        if payload.status not in OWN_STATUS_TOGGLE or row["status"] not in OWN_STATUS_TOGGLE:
            return False
        cited = merge_evidence_source_ids(json_value(row["evidence"], []))
        if cited is None or not actor_id or not row["source_id"]:
            return False
        wanted = sorted(cited | {row["source_id"]})
        found = id_bind_ids(wanted)
        own = id_execute_ids(
            connection,
            f"SELECT s.id FROM sources s WHERE {id_member_of('s.id', found)} "
            f"AND s.notebook_id = %s AND {own_memory_source('s')}",
            (found.param, notebook_id, actor_id),
        ).fetchall()
        return len(own) == len(wanted)

    @staticmethod
    def own_memory_duplicate_seed_rows(
        connection: Any,
        notebook_id: str,
        object_type: str,
        viewer_id: str,
    ) -> List[dict]:
        """Pass 1 of the owner's duplicate groups: the viewer's own Memory
        objects of ``object_type`` whose evidence cites nothing but her own
        Memory sources of this notebook (``_only_own_memory_evidence_sql``: the
        merge's ``only_own_memory_evidence``, judged in the statement) -- the
        objects ``merge_objects_in_transaction`` lets her merge with one
        another. Live ones, and deprecated ones still carrying a merge record
        (``MERGED_INTO_PAYLOAD_KEY``: merged away, not hand-deprecated, so the
        owner can restore them). Shared duplicate groups never list any of them
        (``duplicate_seed_rows`` leaves every Memory-derived row out), and
        nobody else's Memory is read: the statement starts from
        ``own_memory_source``, so a site admin, another member and the empty
        viewer (the Memory channel closed) get nothing.

        Thin like ``duplicate_seed_rows``: ``{"id", "status", "name"}`` (plus
        ``"payload"`` for a procedure, whose seed reads the steps), ``name`` read
        with ``->>`` as that reader reads it, in its order (``created_at``,
        ``id``). Neither evidence nor payload leaves the database; pass 2
        (``own_memory_duplicate_member_rows``) hydrates only the ids shown. One
        statement bounded by her own Memory in this notebook: her Memory sources
        (an InitPlan on ``idx_sources_nb_hidden_type``) drive her objects through
        ``idx_knowledge_objects_source`` (``= ANY(ARRAY(subquery))``, the form
        ``unified_graph_rows``'s overlay uses; a join hash-joins the notebook);
        the evidence test's own-source list is a second such InitPlan."""
        if not viewer_id:
            return []
        procedure = object_type == "procedure"
        payload_column = "o.payload, " if procedure else ""
        marker = f"(o.payload->>'{MERGED_INTO_PAYLOAD_KEY}')"
        rows = connection.execute(
            "SELECT o.id, o.status, o.payload->>'name' AS name, "
            f"{payload_column}{marker} AS merged_into "
            "FROM knowledge_objects o "
            "WHERE o.source_id = ANY(ARRAY(SELECT s.id FROM sources s "
            f"WHERE s.notebook_id=%s AND {own_memory_source('s')})) "
            "AND o.notebook_id=%s AND o.object_type=%s "
            f"AND (o.status!='deprecated' OR COALESCE({marker}, '') <> '') "
            f"AND {_only_own_memory_evidence_sql('o')} "
            "ORDER BY o.created_at ASC, o.id ASC",
            (notebook_id, viewer_id, notebook_id, object_type, notebook_id, viewer_id),
        ).fetchall()
        out: List[dict] = []
        for row in rows:
            item = {"id": row["id"], "status": row["status"], "name": row["name"]}
            if procedure:
                item["payload"] = json_value(row["payload"], {})
            out.append(item)
        return out

    @staticmethod
    def own_memory_duplicate_member_rows(
        connection: Any,
        notebook_id: str,
        object_ids: Sequence[str],
    ) -> Dict[str, dict]:
        """Pass 2: ``{id: payload}`` for ids pass 1 returned (the members of a
        group of two or more, and the merged-away objects): one statement
        binding those ids as one array (``id_binding``, a custom plan), a
        primary-key probe each, ``notebook_id`` beside it as a filter."""
        ids = list(dict.fromkeys(object_ids))
        if not ids:
            return {}
        found = id_bind_ids(ids)
        rows = id_execute_ids(
            connection,
            "SELECT o.id, o.payload FROM knowledge_objects o "
            f"WHERE o.notebook_id = %s AND {id_member_of('o.id', found)}",
            (notebook_id, found.param),
        ).fetchall()
        return {row["id"]: json_value(row["payload"], {}) for row in rows}

    @staticmethod
    def _own_memory_merge_evidence(
        connection: Any,
        notebook_id: str,
        actor_id: str | None,
        src: Any,
        tgt: Any,
        pinned: frozenset,
    ) -> bool:
        """Both locked rows are the caller's own Memory objects: does every
        evidence entry of both cite one of the caller's own Memory sources
        of this notebook (``own_memory_source``, the readers' definition)?
        Raises ``MemoryKnowledgeMergeRefused(MEMORY_MERGE_RETRY_MESSAGE)``
        when the source object cites a Memory source the pre-lock did not
        pin (its evidence changed between that read and the row lock)."""
        src_cited = merge_evidence_source_ids(json_value(src["evidence"], []))
        cited = merge_evidence_source_ids(
            json_value(src["evidence"], []), json_value(tgt["evidence"], [])
        )
        if src_cited is None or cited is None:
            return False
        if cited:
            found = id_bind_ids(sorted(cited))
            own = id_execute_ids(
                connection,
                f"SELECT s.id FROM sources s WHERE {id_member_of('s.id', found)} "
                f"AND s.notebook_id = %s AND {own_memory_source('s')}",
                (found.param, notebook_id, actor_id),
            ).fetchall()
            if len(own) != len(cited):
                return False
        if not (src_cited | {src["source_id"]}) <= pinned:
            raise MemoryKnowledgeMergeRefused(MEMORY_MERGE_RETRY_MESSAGE)
        return True

    @staticmethod
    def merge_objects_in_transaction(
        connection: Any,
        notebook_id: str,
        source_id: str,
        into_id: str,
        now: str,
        *,
        actor_id: str | None = None,
        own_pair_only: bool = False,
    ) -> dict:
        """The in-transaction body of merge_knowledge: fold source evidence
        into the target, maintain the reverse index, deprecate the source in
        place, and return the refetched target row.

        ``own_pair_only`` (the caller lacks ``knowledge:write``, user ruling
        2026-10-22): only a pair of the caller's own Memory objects supported
        by her own Memory alone, both approved, may merge; any other pair that
        passes every check below raises ``NeedsKnowledgeWrite`` -- decided on
        the rows the locking statement returned, before anything is written
        (the transaction then rolls back, releasing the locks).

        M1 (``app.domain.memory_kg_isolation`` has the rule): a
        Memory-derived object merges only with another one of the same
        owner, and only when both are supported by nothing but that owner's
        Memory. The classification rides the statement that takes the row
        locks — no window between the check and the write:

        * an object derived from someone else's Memory (not ``actor_id``'s,
          or an orphan Memory source) is filtered out by
          ``foreign_memory_object_excluded`` and raises the same ``KeyError``
          as a missing id (no existence probe);
        * what survives and is Memory-derived is the caller's own; a pair
          with exactly one such side, or two whose evidence cites anything
          but the caller's own Memory sources of this notebook
          (``_own_memory_merge_evidence``), raises
          ``MemoryKnowledgeMergeRefused`` (409 with the reason) before
          anything is written.

        Lock order. Before the object rows, the Memory sources this merge
        makes the target cite — the source object's primary source and,
        when it is Memory-derived, every Memory source its evidence cites —
        take ``FOR KEY SHARE`` in id order: the order of the Memory teardown
        (``lock_sources_for_teardown_tx``, then the evidence strip and the
        object deletes) and of the Memory re-ingest (``FOR UPDATE`` on the
        source, then the strip and the clear). A concurrent delete,
        transfer, member exit or edit of one of those Memories therefore
        either commits first (this merge then reads rows already stripped,
        or finds an object gone: ``KeyError``) or waits for this commit and
        strips what the merge wrote. A shared merge locks no row here (no
        Memory source; the evidence arm is cut by its filter). The target's
        own citations need no lock: a concurrent strip already counts the
        target among its candidates and re-reads it after this commit.

        With ``own_pair_only`` an unlocked read first refuses, before any lock,
        a pair that is not two of the caller's own Memory objects supported by
        her own Memory alone (both primary sources hers, every evidence entry
        citing one of her Memory sources: ``_only_own_memory_evidence_sql``): a
        read-only member asking about shared or someone else's ids, or about her
        own object carrying shared evidence, takes no row or source lock, so she
        can neither queue behind nor hold up other writers with them. It only
        ever refuses; acceptance is still decided on the locked rows below."""
        if own_pair_only:
            own_rows = connection.execute(
                "SELECT ko.id FROM knowledge_objects ko "
                "WHERE ko.notebook_id = %s AND ko.id IN (%s, %s) "
                "AND ko.status = 'approved' "
                f"AND {memory_derived_object('ko')} "
                f"AND {foreign_memory_object_excluded('ko')} "
                f"AND {_only_own_memory_evidence_sql('ko')}",
                (notebook_id, source_id, into_id, actor_id, notebook_id, actor_id),
            ).fetchall()
            if len(own_rows) < 2:
                raise NeedsKnowledgeWrite()
        pinned = frozenset(
            row["id"]
            for row in connection.execute(
                "SELECT s.id FROM sources s "
                f"WHERE {memory_source_type_predicate('s.source_type')} AND s.id IN ("
                "SELECT ko.source_id FROM knowledge_objects ko "
                "WHERE ko.id = %s AND ko.notebook_id = %s "
                "UNION ALL SELECT ev.value->>'source_id' FROM knowledge_objects ko "
                "CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN "
                "jsonb_typeof(ko.evidence) = 'array' THEN ko.evidence "
                "ELSE '[]'::jsonb END) ev "
                "WHERE ko.id = %s AND ko.notebook_id = %s "
                f"AND {memory_derived_object('ko')}) "
                "ORDER BY s.id FOR KEY SHARE OF s",
                (source_id, notebook_id, source_id, notebook_id),
            ).fetchall()
        )
        locked = connection.execute(
            "SELECT ko.*, "
            f"{memory_derived_object('ko')} AS memory_derived "
            "FROM knowledge_objects ko WHERE ko.notebook_id = %s "
            "AND ko.id IN (%s, %s) "
            f"AND {foreign_memory_object_excluded('ko')} "
            "ORDER BY ko.id COLLATE \"C\" FOR UPDATE",
            (notebook_id, source_id, into_id, actor_id),
        ).fetchall()
        by_id = {row["id"]: row for row in locked}
        src = by_id.get(source_id)
        tgt = by_id.get(into_id)
        if src is None or tgt is None:
            raise KeyError(source_id if src is None else into_id)
        if src["object_type"] != tgt["object_type"]:
            raise ValueError("can only merge knowledge objects of the same type")
        refusal = memory_merge_refusal(
            bool(src["memory_derived"]),
            bool(tgt["memory_derived"]),
            lambda: GovernanceStore._own_memory_merge_evidence(
                connection, notebook_id, actor_id, src, tgt, pinned
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
        merged: List[dict] = json_value(tgt["evidence"], [])
        seen = {(e.get("element_id"), e.get("quoted_span")) for e in merged}
        for item in json_value(src["evidence"], []):
            key = (item.get("element_id"), item.get("quoted_span"))
            if key not in seen:
                merged.append(item)
                seen.add(key)
        merged_json = json.dumps(merged, ensure_ascii=False)
        connection.execute(
            "UPDATE knowledge_objects SET evidence = %s, updated_at = %s WHERE id = %s",
            (
                jsonb(_json_document(
                    merged, expected=list, field="knowledge evidence"
                )),
                normalize_timestamp(now),
                into_id,
            ),
        )
        # into_id's evidence gained items (possibly new source_ids) from source_id;
        # source_id itself only flips status (its own evidence/reverse-index rows
        # are unchanged and stay correct until the object is actually deleted).
        KnowledgeStore.replace_object_sources(connection, into_id, notebook_id, merged_json)
        connection.execute(
            "UPDATE knowledge_objects SET status = 'deprecated', last_reviewed = %s, updated_at = %s WHERE id = %s",
            (normalize_timestamp(now), normalize_timestamp(now), source_id),
        )
        if own_pair:
            connection.execute(
                "UPDATE knowledge_objects SET payload = %s WHERE id = %s",
                (
                    jsonb(marked_merged_into(json_value(src["payload"], {}), into_id)),
                    source_id,
                ),
            )
            connection.execute(
                "UPDATE knowledge_objects SET payload = %s WHERE id = %s",
                (jsonb(marked_absorbs(json_value(tgt["payload"], {}))), into_id),
            )
        result = connection.execute(
            "SELECT * FROM knowledge_objects WHERE id = %s", (into_id,)
        ).fetchone()
        return {
            **dict(result),
            "payload": json.dumps(result["payload"], ensure_ascii=False),
            "evidence": json.dumps(result["evidence"], ensure_ascii=False),
            "created_at": iso_timestamp(result["created_at"]),
            "updated_at": iso_timestamp(result["updated_at"]),
            "last_reviewed": iso_timestamp(result["last_reviewed"]),
        }

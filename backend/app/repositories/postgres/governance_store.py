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
from typing import Any, Dict, List, Optional

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
from app.repositories.postgres.mount_sql import MOUNT_JOIN, MOUNT_ORDER
from app.domain.knowledge_contracts import (
    KNOWLEDGE_STATUSES,
    USABLE_STATUSES,
    PromotionApproval,
)

_REVIEW_STATUSES = frozenset({"pending", "verified", "rejected"})

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


def seed_fn_for(object_type: str):
    """Return the kg_merge seed function for a KG object type."""
    from app.domain.kg_merge_seed import (
        seed_claim, seed_concept, seed_formula, seed_procedure,
    )
    return {
        "concept": seed_concept,
        "claim": seed_claim,
        "formula": seed_formula,
        "procedure": seed_procedure,
    }.get(object_type, seed_claim)


def find_base_dedup_match(
    object_type: str, src_payload: dict, base_objs: List[dict]
) -> str:
    """Exact-seed dedup (v1): return the id of an existing base object whose
    normalized seed matches the source payload, else ''. This works at cold
    start without vectors (the plan's v1 shortcut)."""
    seed_fn = seed_fn_for(object_type)
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


class GovernanceStore:
    def __init__(self, database: PostgresDatabase, seams) -> None:
        self.database = database
        self.seams = seams

    @staticmethod
    def seed_for(object_type: str):
        return seed_fn_for(object_type)

    @staticmethod
    def find_base_match(object_type: str, payload: dict, rows) -> str:
        return find_base_dedup_match(object_type, payload, rows)

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
        return db.execute(
            "SELECT DISTINCT canonical_id, canonical_name FROM concept_clusters "
            "WHERE notebook_id=%s AND object_type=%s", (notebook_id, object_type),
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
    def valid_object_ids(db: Any, object_ids):
        return KnowledgeStore.valid_object_ids(db, object_ids)

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
            "WHERE kr.notebook_id = %s AND kr.review_status != 'rejected'",
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
        connection: Any, notebook_id: str, relation_id: str, status: str
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
        instead of the UPDATE's own rowcount."""
        if status not in _REVIEW_STATUSES:
            raise ValueError(f"invalid edge review status: {status!r}")
        row = connection.execute(
            "UPDATE knowledge_relations SET review_status=%s "
            "FROM (SELECT id, review_status AS prev FROM knowledge_relations "
            "WHERE id=%s AND notebook_id=%s FOR UPDATE) old "
            "WHERE knowledge_relations.id=old.id "
            "RETURNING old.prev",
            (status, relation_id, notebook_id),
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
        connection.execute(
            "DELETE FROM concept_clusters WHERE notebook_id=%s AND object_type=%s",
            (notebook_id, object_type))

    def insert_clusters(
        self,
        connection: Any,
        notebook_id: str,
        object_type: str,
        rows: List[dict],
        now: str,
    ) -> int:
        """Insert cluster member rows, skipping member ids already present in
        the (notebook, type) slice — the append_clusters idempotency contract.
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
        两个分支都闭合;锁前读则可能拿旧 P 写在翻转提交之后,永久丢行。"""
        lock_cluster_artifact_type(connection, notebook_id, object_type)
        generation = self._published_cluster_generation(connection, notebook_id)
        existing = self._existing_cluster_members(
            connection, notebook_id, object_type, rows, generation
        )
        added = 0
        for r in rows:
            if r["member_object_id"] in existing:
                continue
            connection.execute(
                "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,canonical_description,created_at,generation) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (self.seams.new_id("cc"), notebook_id, r["canonical_id"],
                 r["member_object_id"], r["canonical_name"], object_type,
                 r.get("canonical_description", ""), normalize_timestamp(now),
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
        id_prefix: str = "mc",
    ) -> None:
        # id_prefix keeps the historical surrogate prefixes byte-stable:
        # write_merge_candidate mints "mc-", incremental fusion minted "cm-".
        connection.execute(
            "INSERT INTO concept_merge_candidates (id,notebook_id,canonical_a,canonical_b,score,status,created_at,updated_at) VALUES (%s,%s,%s,%s,%s, 'pending', %s, %s)",
            (
                self.seams.new_id(id_prefix), notebook_id, a, b, score,
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
    ) -> None:
        self.insert_merge_candidate(connection, notebook_id, a, b, score, now)

    @staticmethod
    def pending_merges(connection: Any, notebook_id: str) -> List[dict]:
        rows = connection.execute(
            "SELECT * FROM concept_merge_candidates WHERE notebook_id=%s "
            "AND status='pending' ORDER BY ordinal",
            (notebook_id,)).fetchall()
        return [{"id": r["id"], "canonical_a": r["canonical_a"], "canonical_b": r["canonical_b"], "score": r["score"], "status": r["status"]} for r in rows]

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
    def delete_pending_merges(connection: Any, notebook_id: str) -> None:
        connection.execute(
            "DELETE FROM concept_merge_candidates WHERE notebook_id=%s AND status='pending'",
            (notebook_id,))

    @staticmethod
    def insert_pending_merge_rows(
        connection: Any, rows: List[tuple]
    ) -> None:
        """Refresh pending candidates in the caller's transaction. Rows carry
        seed_a/seed_b — the rebuild-stable decision keys (PR#145) — verbatim."""
        execute_many(
            connection,
            "INSERT INTO concept_merge_candidates "
            "(id,notebook_id,canonical_a,canonical_b,seed_a,seed_b,score,status,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s, 'pending', %s, %s)",
            [
                (*row[:-2], normalize_timestamp(row[-2]), normalize_timestamp(row[-1]))
                for row in rows
            ],
        )

    def decided_pairs(self, notebook_id: str) -> Dict[tuple, str]:
        with self.database.connect() as db:
            rows = db.execute("SELECT canonical_a, canonical_b, status FROM concept_merge_candidates WHERE notebook_id=%s AND status IN ('confirmed','rejected')", (notebook_id,)).fetchall()
        return {(r["canonical_a"], r["canonical_b"]): r["status"] for r in rows}

    @staticmethod
    def decided_seed_pairs_from(
        connection: Any, notebook_id: str
    ) -> Dict[frozenset, str]:
        """{frozenset({seed_a, seed_b}): status} for confirmed/rejected/deferred.

        Seed-name keys are STABLE across rebuilds (canonical ids shift when a
        cluster's min-member changes; seed names don't). Legacy rows written
        before the seed_a/seed_b columns existed carry '' → fall back to
        strip-"K-"(canonical), matching the old decided_pairs key derivation."""
        rows = connection.execute(
            "SELECT canonical_a, canonical_b, seed_a, seed_b, status "
            "FROM concept_merge_candidates WHERE notebook_id=%s "
            "AND status IN ('confirmed','rejected','deferred')",
            (notebook_id,),
        ).fetchall()
        def _strip(cid: str) -> str:
            return cid[2:] if cid.startswith("K-") else cid
        out: Dict[frozenset, str] = {}
        for r in rows:
            a = r["seed_a"] or _strip(r["canonical_a"])
            b = r["seed_b"] or _strip(r["canonical_b"])
            out[frozenset((a, b))] = r["status"]
        return out

    def decided_seed_pairs(self, notebook_id: str) -> Dict[frozenset, str]:
        with self.database.connect() as db:
            return self.decided_seed_pairs_from(db, notebook_id)

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
    ) -> str:
        cid = self.seams.new_id("kcc")
        resolved_json = (
            None
            if resolved_payload is None
            else jsonb(_json_document(
                resolved_payload, expected=dict, field="resolved conflict payload"
            ))
        )
        connection.execute(
            """
            INSERT INTO kg_conflict_candidates
              (id, notebook_id, kind, left_ref, right_ref,
               conflict_type, resolution, winner_ref, resolved_payload,
               confidence, rationale, status, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s)
            """,
            (cid, notebook_id, kind, left_ref, right_ref,
             conflict_type, resolution, winner_ref, resolved_json,
             confidence, rationale, normalize_timestamp(now), normalize_timestamp(now)),
        )
        return cid

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
        connection: Any, notebook_id: str, object_id: str
    ) -> "dict | None":
        return connection.execute(
            "SELECT object_type FROM knowledge_objects "
            "WHERE id=%s AND notebook_id=%s FOR UPDATE",
            (object_id, notebook_id),
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
        moved to a bounded by-id read and why the embedding join case-folds
        ``object_type``).
        """
        objects = connection.execute(
            "SELECT id, object_type, payload, status "
            "FROM knowledge_objects "
            "WHERE notebook_id=%s AND status != 'deprecated'",
            (notebook_id,),
        ).fetchall()
        vectors = connection.execute(
            "SELECT e.object_id AS object_id, e.vector AS vector "
            "FROM knowledge_embeddings e "
            "JOIN knowledge_objects o ON o.id = e.object_id "
            "WHERE e.notebook_id=%s AND o.status != 'deprecated' "
            "AND lower(o.object_type) IN ('concept','claim')",
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
        """
        sql = (
            "SELECT id, source_object_id, target_object_id, edge_type "
            "FROM knowledge_relations WHERE notebook_id=%s"
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

        base_object_ids: list[str] = []
        created_object_ids: list[str] = []
        merged_object_ids: list[str] = []
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
            base_match_id = find_base_dedup_match(object_type, payload, base_objs)
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

        # Fetch the personal object being promoted.
        src = connection.execute(
            "SELECT * FROM knowledge_objects WHERE id=%s", (cand["object_id"],)
        ).fetchone()
        if src is None:
            raise KeyError(cand["object_id"])
        src_payload = json_value(src["payload"], {})
        src_evidence = json_value(src["evidence"], [])

        # Cross-corpus dedup against existing base objects of the same type.
        base_objs = _base_dedup_rows_for_update(
            connection, base_nb_id, object_type
        )
        base_match_id = find_base_dedup_match(object_type, src_payload, base_objs)

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

    # -------------------------------------------------- knowledge mutation
    @staticmethod
    def update_object_in_transaction(
        connection: Any,
        notebook_id: str,
        object_id: str,
        payload,
        now: str,
    ) -> dict:
        """The in-transaction body of update_knowledge: validate, apply the
        partial update and return the refetched row. ``payload`` is the
        KnowledgeUpdate model (status/payload/owner partial edit)."""
        row = connection.execute(
            "SELECT * FROM knowledge_objects WHERE id = %s AND notebook_id = %s "
            "FOR UPDATE",
            (object_id, notebook_id),
        ).fetchone()
        if row is None:
            raise KeyError(object_id)
        if payload.status is not None and payload.status not in KNOWLEDGE_STATUSES:
            raise ValueError(f"invalid status: {payload.status}")
        new_payload = (
            jsonb(_json_document(
                payload.payload, expected=dict, field="knowledge payload"
            ))
            if payload.payload is not None
            else jsonb(_json_document(
                row["payload"], expected=dict, field="knowledge payload"
            ))
        )
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
    def merge_objects_in_transaction(
        connection: Any,
        notebook_id: str,
        source_id: str,
        into_id: str,
        now: str,
    ) -> dict:
        """The in-transaction body of merge_knowledge: fold source evidence
        into the target, maintain the reverse index, deprecate the source in
        place, and return the refetched target row."""
        locked = connection.execute(
            "SELECT * FROM knowledge_objects WHERE notebook_id = %s "
            "AND id IN (%s, %s) ORDER BY id COLLATE \"C\" FOR UPDATE",
            (notebook_id, source_id, into_id),
        ).fetchall()
        by_id = {row["id"]: row for row in locked}
        src = by_id.get(source_id)
        tgt = by_id.get(into_id)
        if src is None or tgt is None:
            raise KeyError(source_id if src is None else into_id)
        if src["object_type"] != tgt["object_type"]:
            raise ValueError("can only merge knowledge objects of the same type")
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

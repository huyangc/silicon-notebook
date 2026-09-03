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
from typing import Dict, List, Optional

from app.core.text_whitespace import PY_STRIP_WHITESPACE
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.knowledge_store import KnowledgeStore
from app.repositories.sqlite.mount_sql import MOUNT_JOIN, MOUNT_ORDER
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
    object_type: str, src_payload: dict, base_objs: List[sqlite3.Row]
) -> str:
    """Exact-seed dedup (v1): return the id of an existing base object whose
    normalized seed matches the source payload, else ''. This works at cold
    start without vectors (the plan's v1 shortcut)."""
    seed_fn = seed_fn_for(object_type)
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


class GovernanceStore:
    def __init__(self, database: SqliteDatabase, seams) -> None:
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
        return db.execute(
            "SELECT DISTINCT canonical_id, canonical_name FROM concept_clusters "
            "WHERE notebook_id=? AND object_type=?", (notebook_id, object_type),
        ).fetchall()

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
    def valid_object_ids(db: sqlite3.Connection, object_ids):
        return KnowledgeStore.valid_object_ids(db, object_ids)

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
            "WHERE kr.notebook_id = ? AND kr.review_status != 'rejected'",
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
        connection: sqlite3.Connection, notebook_id: str, relation_id: str, status: str
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
        this fix, only the return value is new."""
        row = connection.execute(
            "SELECT review_status FROM knowledge_relations WHERE id=? AND notebook_id=?",
            (relation_id, notebook_id),
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
        connection.execute(
            "DELETE FROM concept_clusters WHERE notebook_id=? AND object_type=?",
            (notebook_id, object_type))

    def insert_clusters(
        self,
        connection: sqlite3.Connection,
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
        ``(notebook, object_type)`` 切片读进内存——那一读发生在写事务内(此处
        已 ``BEGIN IMMEDIATE``),9.1M 成员的库上等于把全局写锁按住整整一次全表
        扫描。逐位一致的理由是纯集合论:下面的循环只问 ``r["member_object_id"]
        in existing``,而被问到的 id 恰好就是 ``rows`` 里的那些,故把 ``existing``
        限制到 ``rows`` 的 id 集合不改变任何一次判定,``added`` 与写入的行也逐位
        不变。分批走 ``uq_clusters_nb_type_member_generation(notebook_id,
        object_type, member_object_id)`` 这条唯一索引 —— 前两列等值 + 第三列
        IN,是精确 seek 而不是残余过滤。"""
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        existing = self._existing_cluster_members(
            connection, notebook_id, object_type, rows
        )
        added = 0
        for r in rows:
            if r["member_object_id"] in existing:
                continue
            connection.execute(
                "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,canonical_description,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (self.seams.new_id("cc"), notebook_id, r["canonical_id"],
                 r["member_object_id"], r["canonical_name"], object_type,
                 r.get("canonical_description", ""), now))
            existing.add(r["member_object_id"])
            added += 1
        return added

    def _existing_cluster_members(
        self,
        connection: sqlite3.Connection,
        notebook_id: str,
        object_type: str,
        rows: List[dict],
    ) -> set:
        """本次 ``rows`` 的 member id 中,已经在该切片里的那些(有界 IN,分批)。"""
        member_ids = list(dict.fromkeys(r["member_object_id"] for r in rows))
        size = max(1, int(self.seams.in_chunk_size()))
        found = set()
        for offset in range(0, len(member_ids), size):
            batch = member_ids[offset:offset + size]
            placeholders = ",".join("?" for _ in batch)
            found.update(r["member_object_id"] for r in connection.execute(
                f"SELECT member_object_id FROM concept_clusters "
                f"WHERE notebook_id=? AND object_type=? "
                f"AND member_object_id IN ({placeholders})",
                [notebook_id, object_type, *batch]).fetchall())
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
        id_prefix: str = "mc",
    ) -> None:
        # id_prefix keeps the historical surrogate prefixes byte-stable:
        # write_merge_candidate mints "mc-", incremental fusion minted "cm-".
        connection.execute(
            "INSERT INTO concept_merge_candidates (id,notebook_id,canonical_a,canonical_b,score,status,created_at,updated_at) VALUES (?,?,?,?,?, 'pending', ?, ?)",
            (self.seams.new_id(id_prefix), notebook_id, a, b, score, now, now))

    def write_merge_candidate(
        self,
        connection: sqlite3.Connection,
        notebook_id: str,
        a: str,
        b: str,
        score: float,
        now: str,
    ) -> None:
        self.insert_merge_candidate(connection, notebook_id, a, b, score, now)

    @staticmethod
    def pending_merges(connection: sqlite3.Connection, notebook_id: str) -> List[dict]:
        rows = connection.execute(
            "SELECT * FROM concept_merge_candidates WHERE notebook_id=? AND status='pending'",
            (notebook_id,)).fetchall()
        return [{"id": r["id"], "canonical_a": r["canonical_a"], "canonical_b": r["canonical_b"], "score": r["score"], "status": r["status"]} for r in rows]

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
    def delete_pending_merges(connection: sqlite3.Connection, notebook_id: str) -> None:
        connection.execute(
            "DELETE FROM concept_merge_candidates WHERE notebook_id=? AND status='pending'",
            (notebook_id,))

    @staticmethod
    def insert_pending_merge_rows(
        connection: sqlite3.Connection, rows: List[tuple]
    ) -> None:
        """Refresh pending candidates in the caller's transaction. Rows carry
        seed_a/seed_b — the rebuild-stable decision keys (PR#145) — verbatim."""
        connection.executemany(
            "INSERT INTO concept_merge_candidates "
            "(id,notebook_id,canonical_a,canonical_b,seed_a,seed_b,score,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?, 'pending', ?, ?)",
            rows)

    def decided_pairs(self, notebook_id: str) -> Dict[tuple, str]:
        with self.database.connect() as db:
            rows = db.execute("SELECT canonical_a, canonical_b, status FROM concept_merge_candidates WHERE notebook_id=? AND status IN ('confirmed','rejected')", (notebook_id,)).fetchall()
        return {(r["canonical_a"], r["canonical_b"]): r["status"] for r in rows}

    @staticmethod
    def decided_seed_pairs_from(
        connection: sqlite3.Connection, notebook_id: str
    ) -> Dict[frozenset, str]:
        """{frozenset({seed_a, seed_b}): status} for confirmed/rejected/deferred.

        Seed-name keys are STABLE across rebuilds (canonical ids shift when a
        cluster's min-member changes; seed names don't). Legacy rows written
        before the seed_a/seed_b columns existed carry '' → fall back to
        strip-"K-"(canonical), matching the old decided_pairs key derivation."""
        rows = connection.execute(
            "SELECT canonical_a, canonical_b, seed_a, seed_b, status "
            "FROM concept_merge_candidates WHERE notebook_id=? "
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
    ) -> str:
        cid = self.seams.new_id("kcc")
        connection.execute(
            """
            INSERT INTO kg_conflict_candidates
              (id, notebook_id, kind, left_ref, right_ref,
               conflict_type, resolution, winner_ref, resolved_payload,
               confidence, rationale, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (cid, notebook_id, kind, left_ref, right_ref,
             conflict_type, resolution, winner_ref, resolved_payload,
             confidence, rationale, now, now),
        )
        return cid

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
        connection: sqlite3.Connection, notebook_id: str, object_id: str
    ) -> "sqlite3.Row | None":
        return connection.execute(
            "SELECT object_type FROM knowledge_objects WHERE id=? AND notebook_id=?",
            (object_id, notebook_id),
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
        """
        objects = connection.execute(
            "SELECT id, object_type, payload, status "
            "FROM knowledge_objects "
            "WHERE notebook_id=? AND status != 'deprecated'",
            (notebook_id,),
        ).fetchall()
        vectors = connection.execute(
            "SELECT e.object_id AS object_id, e.vector AS vector "
            "FROM knowledge_embeddings e "
            "JOIN knowledge_objects o ON o.id = e.object_id "
            "WHERE e.notebook_id=? AND o.status != 'deprecated' "
            "AND lower(o.object_type) IN ('concept','claim')",
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
        """
        sql = (
            "SELECT id, source_object_id, target_object_id, edge_type "
            "FROM knowledge_relations WHERE notebook_id=?"
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
            base_objs = connection.execute(
                "SELECT id,payload FROM knowledge_objects "
                "WHERE notebook_id=? AND object_type=? AND status IN ({})".format(
                    ",".join("?" for _ in USABLE_STATUSES)
                ),
                (base_nb_id, object_type, *USABLE_STATUSES),
            ).fetchall()
            base_match_id = find_base_dedup_match(object_type, payload, base_objs)
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

        # Fetch the personal object being promoted.
        src = connection.execute(
            "SELECT * FROM knowledge_objects WHERE id=?", (cand["object_id"],)
        ).fetchone()
        if src is None:
            raise KeyError(cand["object_id"])
        src_payload = json.loads(src["payload"] or "{}")
        src_evidence = json.loads(src["evidence"] or "[]")

        # Cross-corpus dedup against existing base objects of the same type.
        base_objs = connection.execute(
            "SELECT id, payload FROM knowledge_objects "
            "WHERE notebook_id=? AND object_type=? AND status IN ({})".format(
                ",".join("?" for _ in USABLE_STATUSES)
            ),
            (base_nb_id, object_type, *USABLE_STATUSES),
        ).fetchall()
        base_match_id = find_base_dedup_match(object_type, src_payload, base_objs)

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

    # -------------------------------------------------- knowledge mutation
    @staticmethod
    def update_object_in_transaction(
        connection: sqlite3.Connection,
        notebook_id: str,
        object_id: str,
        payload,
        now: str,
    ) -> sqlite3.Row:
        """The in-transaction body of update_knowledge: validate, apply the
        partial update and return the refetched row. ``payload`` is the
        KnowledgeUpdate model (status/payload/owner partial edit)."""
        row = connection.execute(
            "SELECT * FROM knowledge_objects WHERE id = ? AND notebook_id = ?",
            (object_id, notebook_id),
        ).fetchone()
        if row is None:
            raise KeyError(object_id)
        if payload.status is not None and payload.status not in KNOWLEDGE_STATUSES:
            raise ValueError(f"invalid status: {payload.status}")
        new_payload = (
            json.dumps(payload.payload, ensure_ascii=False)
            if payload.payload is not None
            else row["payload"]
        )
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
    def merge_objects_in_transaction(
        connection: sqlite3.Connection,
        notebook_id: str,
        source_id: str,
        into_id: str,
        now: str,
    ) -> sqlite3.Row:
        """The in-transaction body of merge_knowledge: fold source evidence
        into the target, maintain the reverse index, deprecate the source in
        place, and return the refetched target row."""
        src = connection.execute(
            "SELECT * FROM knowledge_objects WHERE id = ? AND notebook_id = ?",
            (source_id, notebook_id),
        ).fetchone()
        tgt = connection.execute(
            "SELECT * FROM knowledge_objects WHERE id = ? AND notebook_id = ?",
            (into_id, notebook_id),
        ).fetchone()
        if src is None or tgt is None:
            raise KeyError(source_id if src is None else into_id)
        if src["object_type"] != tgt["object_type"]:
            raise ValueError("can only merge knowledge objects of the same type")
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
        return connection.execute(
            "SELECT * FROM knowledge_objects WHERE id = ?", (into_id,)
        ).fetchone()

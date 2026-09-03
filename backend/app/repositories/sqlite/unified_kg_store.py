"""Unified-KG persistence store (Task 13).

Owns unified state (dirty flag + monotonic counters), ``cluster_map`` rows,
cluster input facts, cluster scratch rows, canonical relations, the mention
bridge, communities, the rebuild checkpoints (kg_rebuild_checkpoint — the
master-v10 resumable-rebuild rows) and the final rebuild-state upsert.

Composition rules (Gate 5): connection-taking primitives ride the FACADE's
transaction boundary (frozen ``_write`` trace patches keep observing every
commit). The four checkpoint methods and the community-peer readers own their
connections — they are self-contained row-level accessors whose facade
delegates/callers never carried an outer transaction.

Red lines preserved verbatim:
- ``mark_dirty`` is the ONLY place kg_mutation_seq advances (+1 via the
  table's own current value — monotonic counter, NOT a timestamp).
- ``finish_rebuild_state`` must NOT touch kg_mutation_seq — the column is
  omitted from both the INSERT column list and the SET so an existing row's
  counter is preserved (bumping would make the gate never skip; resetting
  would lose mutations that arrived mid-rebuild).
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from app.repositories.kg_analysis_payloads import (
    artifact_ledger_rows as _artifact_ledger_rows,
    clamp as _clamp,
    cluster_histogram_payload as _cluster_histogram_payload,
    community_overview_payload as _community_overview_payload,
    largest_clusters_payload as _largest_clusters_payload,
    relation_provenance_payload as _relation_provenance_payload,
)
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.mount_sql import MOUNT_JOIN, MOUNT_ORDER, MOUNT_VALID
from app.domain.kg_analysis_contracts import (
    BOARD_DEPENDENT_ARTIFACT_KINDS,
    batched,
    check_artifact_payloads,
)
from app.domain.knowledge_contracts import (
    COMMUNITY_OVERVIEW_MAX,
    COMMUNITY_TOP_MEMBERS_MAX,
    KG_COMMUNITY_EDGES_MAX,
    KG_SOURCE_PAGE_MAX,
    LARGEST_CLUSTERS_MAX,
    USABLE_STATUSES,
)

# 批 3·W2 §1.4:published 代次谓词(PG 孪生同名常量;? 绑定参数保证子查询
# 一次求值;COALESCE 兜「无 unified_kg_state 行 ⇒ 代次 0」——副本/合并库契约)。
_PUBLISHED_CLUSTER_GEN = (
    "COALESCE((SELECT cluster_generation FROM unified_kg_state "
    "WHERE notebook_id = ?), 0)"
)
_PUBLISHED_COMMUNITY_GEN = (
    "COALESCE((SELECT community_generation FROM unified_kg_state "
    "WHERE notebook_id = ?), 0)"
)


class UnifiedKgStore:
    def __init__(self, database: SqliteDatabase, now=None) -> None:
        self.database = database
        self.now = now

    # --------------------------------------------- lifecycle rebuild streams
    @staticmethod
    def seed_payload_rows(
        db: sqlite3.Connection, notebook_id: str, object_type: str,
    ):
        return db.execute(
            "SELECT payload FROM knowledge_objects "
            "WHERE notebook_id=? AND object_type=? AND status!='deprecated' "
            "ORDER BY rowid", (notebook_id, object_type),
        )

    @staticmethod
    def stream_seed_rows(
        db: sqlite3.Connection, notebook_id: str, object_type: str,
    ):
        return db.execute(
            "SELECT id, payload FROM knowledge_objects "
            "WHERE notebook_id=? AND object_type=? AND status!='deprecated' "
            "ORDER BY rowid", (notebook_id, object_type),
        )

    @staticmethod
    def scratch_vector_rows(db: sqlite3.Connection, notebook_id: str, run_id: str):
        # The representative mean is accumulated in float32 insertion order.
        # Preserve the pre-shadow-guard scratch rowid stream explicitly: after
        # ANALYZE SQLite may otherwise choose the observation-only logical-key
        # guard and reorder additions, changing rounding and cluster outcomes.
        return db.execute(
            "SELECT s.seed AS seed, e.vector AS vector "
            "FROM knowledge_embeddings e "
            "JOIN kg_cluster_scratch s ON s.object_id=e.object_id "
            "  AND s.notebook_id=e.notebook_id AND s.run_id=? "
            "WHERE e.notebook_id=? ORDER BY s.rowid", (run_id, notebook_id),
        )

    @staticmethod
    def replace_cluster_rows_streamed(
        db: sqlite3.Connection,
        notebook_id: str,
        object_type: str,
        rows,
    ) -> None:
        # 直接整体替换某 notebook/object_type 的 concept_clusters(DELETE + 分批
        # INSERT)。生产 rebuild 已改走 scratch + swap_cluster_map_from_scratch,#320
        # 写锁瘦身时因「无调用者」删掉了它;但跨后端一致性用例(postgres/
        # test_knowledge_store_conformance)在 sqlite 与 PostgreSQL 上都经这一个
        # 方法直接铺 cluster 行,PostgreSQL adapter 也保留着它——故此处保留为两端
        # 对等的测试用直写原语(写串行由 database.write() 保证,无需咨询锁)。
        db.execute(
            "DELETE FROM concept_clusters WHERE notebook_id=? AND object_type=?",
            (notebook_id, object_type),
        )
        buf: list[tuple] = []
        for row in rows:
            buf.append(row)
            if len(buf) >= 1000:
                db.executemany(
                    "INSERT INTO concept_clusters "
                    "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,"
                    "canonical_description,canonical_desc_sig,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", buf,
                )
                buf.clear()
        if buf:
            db.executemany(
                "INSERT INTO concept_clusters "
                "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,"
                "canonical_description,canonical_desc_sig,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)", buf,
            )

    @staticmethod
    def cluster_description_rows(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            "SELECT DISTINCT canonical_id, canonical_description, canonical_desc_sig "
            "FROM concept_clusters WHERE notebook_id=? AND object_type='concept' "
            f"AND generation = {_PUBLISHED_CLUSTER_GEN}",
            (notebook_id, notebook_id),
        ).fetchall()

    @staticmethod
    def cluster_evidence_rows(
        db: sqlite3.Connection, notebook_id: str, run_id: str, seeds,
    ):
        """Evidence blobs for a set of cluster seeds, each tagged with the ``seed``
        it came from. The concept-description stage asks for MANY canonicals'
        seeds in one call and regroups the rows in memory (one round trip per
        batch instead of one per canonical), which is only possible because the
        seed rides along in the projection.

        ⚠ Registered follow-up (a migration, deliberately not in this batch):
        ``idx_kg_cluster_scratch_nb_run`` is ``(notebook_id, run_id)`` with **no
        ``seed`` key**, so ``seed IN (...)`` is a residual filter over every
        scratch row of the run — each batch costs O(scratch), and batching only
        cut the number of those scans, not their width. Extending the index to
        ``(notebook_id, run_id, seed)`` (both backends, appended as a new
        ``_migration_N`` + SCHEMA_VERSION bump) would turn it into one seek per
        seed; the same note is on the PostgreSQL twin."""
        values = list(seeds)
        if not values:
            return []
        ph = ",".join("?" for _ in values)
        return db.execute(
            f"SELECT s.seed AS seed, k.evidence AS evidence FROM knowledge_objects k "
            f"JOIN kg_cluster_scratch s ON s.object_id=k.id "
            f"WHERE s.notebook_id=? AND s.run_id=? AND s.seed IN ({ph})",
            (notebook_id, run_id, *values),
        ).fetchall()

    @staticmethod
    def canonical_relation_seed_rows(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            "SELECT kr.id AS rid, kr.source_id AS src_doc, kr.edge_type AS et, "
            "       COALESCE(cs.canonical_id, kr.source_object_id) AS s, "
            "       COALESCE(ct.canonical_id, kr.target_object_id) AS t, "
            "       so.object_type AS st, tp.object_type AS tt "
            "FROM knowledge_relations kr "
            "JOIN knowledge_objects so ON so.id=kr.source_object_id "
            "JOIN knowledge_objects tp ON tp.id=kr.target_object_id "
            "LEFT JOIN concept_clusters cs ON cs.notebook_id=kr.notebook_id "
            f"  AND cs.member_object_id=kr.source_object_id AND cs.generation = {_PUBLISHED_CLUSTER_GEN} "
            "LEFT JOIN concept_clusters ct ON ct.notebook_id=kr.notebook_id "
            f"  AND ct.member_object_id=kr.target_object_id AND ct.generation = {_PUBLISHED_CLUSTER_GEN} "
            "WHERE kr.notebook_id=? AND kr.review_status!='rejected'",
            (notebook_id, notebook_id, notebook_id),
        )

    @staticmethod
    def mention_seed_rows(db: sqlite3.Connection, notebook_id: str):
        # 批 3·W2 A 类(codex #671 R1 P1):published 谓词,理由见 PG 孪生。
        clusters = db.execute(
            "SELECT cc.canonical_id AS cid, cc.canonical_name AS cname, ko.source_id AS src "
            "FROM concept_clusters cc JOIN knowledge_objects ko ON ko.id=cc.member_object_id "
            "WHERE cc.notebook_id=? AND cc.object_type='concept' "
            f"AND cc.generation = {_PUBLISHED_CLUSTER_GEN}",
            (notebook_id, notebook_id),
        ).fetchall()
        claims = db.execute(
            "SELECT id, json_extract(payload,'$.name') AS nm FROM knowledge_objects "
            "WHERE notebook_id=? AND object_type='claim' AND status!='deprecated'",
            (notebook_id,),
        ).fetchall()
        return clusters, claims

    @staticmethod
    def claim_name_rows(db: sqlite3.Connection, rows) -> None:
        db.execute(
            "CREATE VIRTUAL TABLE temp.mention_scan_fts "
            "USING fts5(text, tokenize='trigram')"
        )
        db.executemany(
            "INSERT INTO temp.mention_scan_fts(rowid, text) VALUES (?,?)", rows,
        )

    @staticmethod
    def mention_scan_matches(db: sqlite3.Connection, match_expr: str):
        return db.execute(
            "SELECT rowid FROM temp.mention_scan_fts WHERE mention_scan_fts MATCH ?",
            (match_expr,),
        )

    @contextmanager
    def mention_alias_candidate_batches(
        self, claims: Sequence[tuple[str, str]], aliases: Sequence[str]
    ) -> Iterator[Iterator[tuple[str, Iterator[tuple[str, str]]]]]:
        """Yield one alias cursor at a time while owning the TEMP-table lifetime."""
        scan_db = self.database.connect()
        try:
            self.claim_name_rows(
                scan_db,
                (
                    (index, folded)
                    for index, (_claim_id, folded) in enumerate(claims, 1)
                ),
            )

            def batches() -> Iterator[tuple[str, Iterator[tuple[str, str]]]]:
                for alias in aliases:
                    match_expr = '"' + alias.replace('"', '""') + '"'
                    rows = self.mention_scan_matches(scan_db, match_expr)
                    yield alias, (
                        claims[int(row["rowid"]) - 1]
                        for row in rows
                    )

            yield batches()
        finally:
            self.database.close_local()

    @staticmethod
    def community_graph_rows(db: sqlite3.Connection, notebook_id: str):
        names = {
            row["canonical_id"]: row["canonical_name"]
            for row in db.execute(
                "SELECT DISTINCT canonical_id, canonical_name FROM concept_clusters "
                f"WHERE notebook_id=? AND generation = {_PUBLISHED_CLUSTER_GEN}",
                (notebook_id, notebook_id),
            )
        }
        relations = db.execute(
            "SELECT COALESCE(cs.canonical_id, kr.source_object_id) AS s, "
            "       COALESCE(ct.canonical_id, kr.target_object_id) AS t "
            "FROM knowledge_relations kr "
            "LEFT JOIN concept_clusters cs ON cs.notebook_id=kr.notebook_id "
            f"AND cs.member_object_id=kr.source_object_id AND cs.generation = {_PUBLISHED_CLUSTER_GEN} "
            "LEFT JOIN concept_clusters ct ON ct.notebook_id=kr.notebook_id "
            f"AND ct.member_object_id=kr.target_object_id AND ct.generation = {_PUBLISHED_CLUSTER_GEN} "
            "WHERE kr.notebook_id=? AND kr.review_status!='rejected' "
            "ORDER BY kr.id",
            (notebook_id, notebook_id, notebook_id),
        )
        return names, relations

    @staticmethod
    def cluster_version_row(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(created_at), '') AS ts "
            "FROM concept_clusters WHERE notebook_id = ? "
            f"AND generation = {_PUBLISHED_CLUSTER_GEN}",
            (notebook_id, notebook_id),
        ).fetchone()

    @staticmethod
    def cluster_member_rows(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            "SELECT canonical_id, member_object_id FROM concept_clusters "
            "WHERE notebook_id = ? "
            f"AND generation = {_PUBLISHED_CLUSTER_GEN} "
            "ORDER BY canonical_id, member_object_id",
            (notebook_id, notebook_id),
        ).fetchall()

    @staticmethod
    def ppr_version_rows(db: sqlite3.Connection, notebook_id: str):
        rel = db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
            "FROM knowledge_relations WHERE notebook_id=? "
            "AND review_status!='rejected'", (notebook_id,),
        ).fetchone()
        obj = db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at),'') AS ts "
            "FROM knowledge_objects WHERE notebook_id=?", (notebook_id,),
        ).fetchone()
        chunk = db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
            "FROM chunks WHERE notebook_id=?", (notebook_id,),
        ).fetchone()
        cluster = db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
            "FROM concept_clusters WHERE notebook_id=? "
            f"AND generation = {_PUBLISHED_CLUSTER_GEN}",
            (notebook_id, notebook_id),
        ).fetchone()
        mention = db.execute(
            "SELECT COALESCE(mention_seq,-1) AS ms FROM unified_kg_state "
            "WHERE notebook_id=?", (notebook_id,),
        ).fetchone()
        return rel, obj, chunk, cluster, mention

    @staticmethod
    def graph_seq_row(db: sqlite3.Connection, notebook_id: str) -> "tuple[int, int, int, int]":
        """O(1) single-row monotonic seq quadruple for the graph/PPR version
        keys: (kg_mutation_seq, cluster_mutation_seq, mention_seq,
        kg_reset_epoch). Replaces the per-request COUNT/MAX aggregate scans
        (ppr_version_rows / graph_version_rows / cluster_version_row) that ran
        on EVERY graph/PPR retrieval, even on a cache HIT. Coverage of every
        production write path (adversarially verified):
          - kg_mutation_seq: object writes (create/status/payload/delete via
            store_kg/update_knowledge/merge/promotion/conflict/relink/delete_source),
            edge-review flips (set_edge_review), and chunk writes
            (build_chunks_for_source) all bump it.
          - cluster_mutation_seq: concept_clusters writes (write_clusters /
            append_clusters / rebuild — which DELIBERATELY keeps kg_mutation_seq
            stable) advance this instead.
          - mention_seq: the co-mention bridge rebuild.
          - kg_reset_epoch: bumped ONLY by delete_notebook_graph_rows, in the
            SAME transaction that resets kg_mutation_seq/cluster_mutation_seq/
            mention_seq back to their birth-row values (design doc batch-3-W1
            Sec 3.3 option C). Only increases, never decreases.
        A monotonic counter is STRICTLY more sensitive than (COUNT, MAX ts): it
        also catches a same-second in-place edit that a 1s-resolution timestamp
        would miss. Absent row -> (0, 0, -1, 0), matching version_signal's
        sentinel.

        kg_reset_epoch MUST stay the LAST element of this tuple: three
        call sites consume this return by position — see the design doc's
        Sec 3.2/3.4 for the full list and the "append at tuple end" rule.
        Before kg_reset_epoch existed, kg_mutation_seq/cluster_mutation_seq/
        mention_seq RESET on delete_notebook_kg (which used to drop the state
        row outright), so a delete+reingest of a participant could re-climb to
        a colliding triple — retrieval_snapshot_cache.invalidate_kg's belt-
        and-braces full :ppr_graph/:fed_rxgraph eviction on every KG mutation
        is what covered that gap. Folding kg_reset_epoch into this tuple's
        consumers makes that specific collision structurally impossible (a
        notebook's epoch never repeats), though invalidate_kg's broader sweep
        still guards the same-second-in-place-edit case that is orthogonal to
        the epoch fix."""
        row = db.execute(
            "SELECT COALESCE(kg_mutation_seq,0) AS ks, COALESCE(cluster_mutation_seq,0) AS cs, "
            "COALESCE(mention_seq,-1) AS ms, COALESCE(kg_reset_epoch,0) AS ep "
            "FROM unified_kg_state WHERE notebook_id=?",
            (notebook_id,),
        ).fetchone()
        if row is None:
            return (0, 0, -1, 0)
        return (int(row["ks"]), int(row["cs"]), int(row["ms"]), int(row["ep"]))

    @staticmethod
    def mention_rows(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            "SELECT claim_object_id, concept_canonical_id FROM mention_edges "
            "WHERE notebook_id=?", (notebook_id,),
        ).fetchall()

    # -------------------------------------------------------- unified state
    @staticmethod
    def state_row(db: sqlite3.Connection, notebook_id: str) -> "sqlite3.Row | None":
        return db.execute(
            "SELECT * FROM unified_kg_state WHERE notebook_id=?", (notebook_id,)
        ).fetchone()

    @staticmethod
    def mark_dirty(db: sqlite3.Connection, notebook_id: str, now: str) -> None:
        """Bump the monotonic mutation counter on every KG write. Reference the
        table's own current value (+1), NOT excluded, so an existing row
        increments rather than resets to the inserted literal (1). First
        mutation -> seq 1."""
        db.execute(
            """
            INSERT INTO unified_kg_state (notebook_id, dirty, kg_mutation_seq, updated_at)
            VALUES (?, 1, 1, ?)
            ON CONFLICT(notebook_id) DO UPDATE SET
              dirty=1,
              kg_mutation_seq=unified_kg_state.kg_mutation_seq+1,
              updated_at=excluded.updated_at
            """,
            (notebook_id, now),
        )

    @staticmethod
    def bump_cluster_seq(db: sqlite3.Connection, notebook_id: str, now: str) -> None:
        """concept_clusters 写路径的单调计数器 bump——在调用方已持有的写事务 db 内
        执行(写簇+bump 同 commit,原子)。kg_mutation_seq 不在此处动:rebuild 刻意
        保持它稳定(幂等),clusters 的变化信号独立成列。"""
        db.execute(
            """
            INSERT INTO unified_kg_state (notebook_id, dirty, cluster_mutation_seq, updated_at)
            VALUES (?, 0, 1, ?)
            ON CONFLICT(notebook_id) DO UPDATE SET
              cluster_mutation_seq=unified_kg_state.cluster_mutation_seq+1,
              updated_at=excluded.updated_at
            """,
            (notebook_id, now),
        )

    # ---------------------------------------------- derived generations (W2)
    # 批 3·W2 PR-2(PG 孪生同注释):代次化写者的数据级原语。SQLite 差异:
    # 时钟用 datetime('now')(UTC 文本,与 PG 的 DB now() 同为服务端时钟);
    # claim 走「INSERT OR IGNORE 造行 + UPDATE CAS」两步——write() 的进程级
    # 写锁 + 首写即锁升级保证原子;flip 无 advisory lock(单进程写事务天然
    # 串行,append 与翻转走同一把全局写锁)。

    @staticmethod
    def claim_derived_generation(
        db: sqlite3.Connection, notebook_id: str, *, ttl_seconds: int
    ) -> "dict | None":
        db.execute(
            "INSERT OR IGNORE INTO unified_kg_state (notebook_id, updated_at) "
            "VALUES (?, datetime('now'))",
            (notebook_id,),
        )
        cur = db.execute(
            "UPDATE unified_kg_state SET "
            "derived_generation_counter = derived_generation_counter + 1, "
            "derived_building_generation = derived_generation_counter + 1, "
            "derived_building_claimed_at = datetime('now'), "
            "updated_at = datetime('now') "
            "WHERE notebook_id = ? AND (derived_building_generation = 0 "
            "OR datetime(COALESCE(derived_building_claimed_at, '1970-01-01')) "
            "< datetime('now', '-' || CAST(? AS TEXT) || ' seconds'))",
            (notebook_id, int(ttl_seconds)),
        )
        if cur.rowcount != 1:
            return None
        row = db.execute(
            "SELECT derived_generation_counter AS generation, "
            "cluster_generation, community_generation, derived_catchup_from, "
            "datetime('now') AS ts FROM unified_kg_state WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchone()
        return {
            "generation": int(row["generation"]),
            "cluster_generation": int(row["cluster_generation"]),
            "community_generation": int(row["community_generation"]),
            "catchup_from": row["derived_catchup_from"],
            "ts": str(row["ts"]),
        }

    @staticmethod
    def release_derived_claim(
        db: sqlite3.Connection, notebook_id: str, generation: int
    ) -> None:
        db.execute(
            "UPDATE unified_kg_state SET derived_building_generation=0, "
            "derived_building_claimed_at=NULL, updated_at=datetime('now') "
            "WHERE notebook_id=? AND derived_building_generation=?",
            (notebook_id, generation),
        )

    @staticmethod
    def derived_claim_still_held(
        db: sqlite3.Connection, notebook_id: str, generation: int
    ) -> bool:
        """写段前复读——**必须**与随后的写在同一事务里对跨进程写者原子
        (begin_guarded_write 接缝,复评 P3-2):否则「读到仍持有」与「写入」
        之间另一进程可抢占+预回收,残行照样落下。只在写连接上调用。"""
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT 1 FROM unified_kg_state "
            "WHERE notebook_id=? AND derived_building_generation=?",
            (notebook_id, generation),
        ).fetchone()
        return row is not None

    @staticmethod
    def write_cluster_map_generation(
        db: sqlite3.Connection,
        notebook_id: str,
        object_type: str,
        run_id: str,
        created_at: str,
        generation: int,
    ) -> None:
        # 行序/连接形状与旧 swap_cluster_map_from_scratch 的 INSERT 半部逐字
        # 一致(ORDER BY s.rowid,PR#136 行序红线),只多 generation 列、
        # 少 DELETE 半部。
        db.execute(
            "INSERT INTO concept_clusters "
            "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,"
            "canonical_description,canonical_desc_sig,created_at,generation) "
            "SELECT 'cc-' || lower(hex(randomblob(16))), "
            "s.notebook_id,c.canonical_id,s.object_id,c.canonical_name,?,"
            "c.canonical_description,c.canonical_desc_sig,?,? "
            "FROM kg_cluster_scratch AS s "
            "JOIN kg_canonical_scratch AS c "
            "  ON c.notebook_id = s.notebook_id AND c.run_id = s.run_id AND c.seed = s.seed "
            "WHERE s.notebook_id = ? AND s.run_id = ? "
            "ORDER BY s.rowid",
            (object_type, created_at, generation, notebook_id, run_id),
        )

    @staticmethod
    def flip_cluster_generation(
        db: sqlite3.Connection,
        notebook_id: str,
        *,
        published_from: int,
        generation: int,
        catchup_from_ts: str,
        now: str,
    ) -> bool:
        cur = db.execute(
            "UPDATE unified_kg_state SET "
            "cluster_generation=?, cluster_mutation_seq=cluster_mutation_seq+1, "
            "derived_building_generation=0, derived_building_claimed_at=NULL, "
            "derived_catchup_from=?, updated_at=? "
            "WHERE notebook_id=? AND cluster_generation=? "
            "AND derived_building_generation=?",
            (
                generation, catchup_from_ts, now, notebook_id, published_from,
                generation,
            ),
        )
        return cur.rowcount == 1

    @staticmethod
    def community_generation_for_publish(
        db: sqlite3.Connection, notebook_id: str
    ) -> int:
        """补账本发布复核专用——PG 孪生带 FOR SHARE;SQLite 进程写锁 +
        BEGIN IMMEDIATE 已把发布事务与任何并发翻转串行,平读即等价
        (parity 是语义等价,不是语法照抄)。"""
        row = db.execute(
            "SELECT community_generation FROM unified_kg_state "
            "WHERE notebook_id=?",
            (notebook_id,),
        ).fetchone()
        return int(row["community_generation"]) if row else 0

    @staticmethod
    def flip_community_generation(
        db: sqlite3.Connection, notebook_id: str, *, published_from: int,
        generation: int, now: str,
    ) -> bool:
        cur = db.execute(
            "UPDATE unified_kg_state SET community_generation=?, updated_at=? "
            "WHERE notebook_id=? AND community_generation=? "
            "AND derived_building_generation=?",
            (generation, now, notebook_id, published_from, generation),
        )
        return cur.rowcount == 1

    @staticmethod
    def clear_catchup_marker(
        db: sqlite3.Connection, notebook_id: str, ts: str
    ) -> None:
        db.execute(
            "UPDATE unified_kg_state SET derived_catchup_from=NULL, "
            "updated_at=datetime('now') "
            "WHERE notebook_id=? AND derived_catchup_from=?",
            (notebook_id, ts),
        )

    @staticmethod
    def catchup_window_members(
        db: sqlite3.Connection, notebook_id: str, published_generation: int,
        since_ts: str, skew_seconds: int, limit: int, *,
        after_object_type: str = "", after_member_object_id: str = "",
    ) -> list:
        # 谓词 != published 而非 = 旧P、keyset 分页、payload 文本契约:理由
        # 见 PG 孪生 docstring(sqlite 的 payload 本就是 TEXT,契约天然满足)。
        # datetime() 双侧归一(#659 R13 教训:存储行的 offset 可能异源)。
        return db.execute(
            "SELECT c.member_object_id, c.object_type, "
            "MIN(o.payload) AS payload "
            "FROM concept_clusters c "
            "JOIN knowledge_objects o ON o.id = c.member_object_id "
            "WHERE c.notebook_id=? AND c.generation != ? "
            "AND datetime(c.created_at) >= "
            "datetime(?, '-' || CAST(? AS TEXT) || ' seconds') "
            "AND (c.object_type, c.member_object_id) > (?, ?) "
            "GROUP BY c.object_type, c.member_object_id "
            "ORDER BY c.object_type, c.member_object_id "
            "LIMIT ?",
            (notebook_id, published_generation, since_ts, int(skew_seconds),
             after_object_type, after_member_object_id, limit),
        ).fetchall()

    @staticmethod
    def reap_derived_generations_page(
        db: sqlite3.Connection, notebook_id: str, table: str,
        keep: "tuple[int, ...]", limit: int,
    ) -> int:
        if table not in ("concept_clusters", "communities", "community_members"):
            raise ValueError(f"reap_derived_generations_page: 非派生表 {table!r}")
        marks = ",".join("?" * len(keep))
        cur = db.execute(
            f"DELETE FROM {table} WHERE rowid IN ("
            f"SELECT rowid FROM {table} WHERE notebook_id=? "
            f"AND generation NOT IN ({marks}) LIMIT ?)",
            (notebook_id, *keep, limit),
        )
        return cur.rowcount

    @staticmethod
    def cluster_input_facts(
        db: sqlite3.Connection, notebook_id: str, *, exclude_emb_count: bool = False
    ) -> Tuple[int, int, int, int]:
        """The data-derived components of _cluster_input_version: (seq, obj_c,
        dec_c, emb_c). The decided-pair COUNT WHERE mirrors decided_pairs()
        EXACTLY (pending excluded so rebuild's own pending-refresh doesn't move
        the version). emb_c stays 0 when excluded (the checkpoint version
        namespace — see _cluster_input_version's docstring)."""
        st = db.execute(
            "SELECT kg_mutation_seq FROM unified_kg_state WHERE notebook_id=?",
            (notebook_id,)).fetchone()
        seq = int(st["kg_mutation_seq"]) if st else 0
        obj_c = db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_objects "
            "WHERE notebook_id=? AND status!='deprecated'",
            (notebook_id,)).fetchone()["c"]
        dec_c = db.execute(
            "SELECT COUNT(*) AS c FROM concept_merge_candidates "
            "WHERE notebook_id=? AND status IN ('confirmed','rejected')",
            (notebook_id,)).fetchone()["c"]
        emb_c = 0
        if not exclude_emb_count:
            emb_c = db.execute(
                "SELECT COUNT(*) AS c FROM knowledge_embeddings WHERE notebook_id=?",
                (notebook_id,)).fetchone()["c"]
        return seq, int(obj_c), int(dec_c), int(emb_c)

    # ------------------------------------------------------------ clusters
    @staticmethod
    def cluster_map_rows(db: sqlite3.Connection, notebook_id: str) -> Dict[str, str]:
        rows = db.execute(
            "SELECT member_object_id, canonical_id FROM concept_clusters WHERE notebook_id=? "
            f"AND generation = {_PUBLISHED_CLUSTER_GEN}",
            (notebook_id, notebook_id),
        ).fetchall()
        return {r["member_object_id"]: r["canonical_id"] for r in rows}

    @staticmethod
    def cluster_fold_rows(
        db: sqlite3.Connection, notebook_id: str, ids: List[str]
    ) -> List[sqlite3.Row]:
        """BOUNDED canonical fold lookup (only the given hit ids) — never the
        full cluster_map, which can be 5M entries at scale.

        ⚠ PR-2 复核:rebuild 折叠路径若需读 building 代,再参数化。"""
        placeholders = ",".join("?" for _ in ids)
        return db.execute(
            f"SELECT member_object_id, canonical_id, canonical_name "
            f"FROM concept_clusters "
            f"WHERE notebook_id=? AND member_object_id IN ({placeholders}) "
            f"AND generation = {_PUBLISHED_CLUSTER_GEN}",
            [notebook_id] + ids + [notebook_id],
        ).fetchall()

    @staticmethod
    def concept_clusters_count(db: sqlite3.Connection, notebook_id: str) -> int:
        return int(db.execute(
            "SELECT COUNT(*) AS c FROM concept_clusters WHERE notebook_id=? "
            f"AND generation = {_PUBLISHED_CLUSTER_GEN}",
            (notebook_id, notebook_id)).fetchone()["c"])

    @staticmethod
    def distinct_cluster_count(db: sqlite3.Connection, notebook_id: str) -> int:
        return int(db.execute(
            "SELECT COUNT(DISTINCT canonical_id) AS c FROM concept_clusters WHERE notebook_id=? "
            f"AND generation = {_PUBLISHED_CLUSTER_GEN}",
            (notebook_id, notebook_id),
        ).fetchone()["c"])

    # ------------------------------------------------------------- scratch
    @staticmethod
    def clear_scratch_run(
        db: sqlite3.Connection, notebook_id: str, run_id: str
    ) -> None:
        db.execute("DELETE FROM kg_cluster_scratch WHERE notebook_id=? AND run_id=?",
                   (notebook_id, run_id))

    @staticmethod
    def insert_scratch_rows(db: sqlite3.Connection, rows: List[tuple]) -> None:
        db.executemany(
            "INSERT INTO kg_cluster_scratch (notebook_id, run_id, object_id, seed) VALUES (?,?,?,?)",
            rows)

    # ---------------------------------------------------- canonical scratch
    # Preparation-segment table for swap_cluster_map_from_scratch (write-lock
    # slimming improvement point 2): holds the seed -> canonical mapping
    # (name/description/desc-sig) a rebuild just computed for ONE object_type,
    # so the swap can join it against kg_cluster_scratch in pure SQL instead
    # of a Python row-by-row construction inside the write lock.
    @staticmethod
    def clear_canonical_scratch_run(
        db: sqlite3.Connection, notebook_id: str, run_id: str
    ) -> None:
        db.execute("DELETE FROM kg_canonical_scratch WHERE notebook_id=? AND run_id=?",
                   (notebook_id, run_id))

    @staticmethod
    def insert_canonical_scratch_rows(db: sqlite3.Connection, rows: List[tuple]) -> None:
        db.executemany(
            "INSERT INTO kg_canonical_scratch "
            "(notebook_id, run_id, seed, canonical_id, canonical_name, "
            "canonical_description, canonical_desc_sig) VALUES (?,?,?,?,?,?,?)",
            rows)

    # -------------------------------------------------- rebuild checkpoints
    # kg_rebuild_checkpoint 行级读写(master v10 可续跑轨道)。自己开连接/事务:
    # 每个 helper 历来就是独立的小事务(rebuild 的 LLM 阶段从 worker 线程写入),
    # 走 database.write() 即同一把全局写锁。
    def checkpoint_gc(self, notebook_id: str, input_version: str) -> None:
        """删掉本 notebook 里 input_version 不等于当前值的所有 checkpoint 行(表有界)。
        rebuild 开头调一次:数据/算法版本一变,旧决策自动失效。"""
        with self.database.write() as db:
            db.execute(
                "DELETE FROM kg_rebuild_checkpoint WHERE notebook_id=? AND input_version!=?",
                (notebook_id, input_version))

    def checkpoint_clear(self, notebook_id: str) -> None:
        """删掉本 notebook 的全部 checkpoint(所有版本/阶段)。--fresh 用,强制两个 LLM 阶段重跑。"""
        with self.database.write() as db:
            db.execute("DELETE FROM kg_rebuild_checkpoint WHERE notebook_id=?", (notebook_id,))

    def checkpoint_load(
        self, notebook_id: str, input_version: str, stage: str
    ) -> Dict[str, dict]:
        """载入某阶段在当前 input_version 下已完成的 item:{item_key: payload_dict}。"""
        with self.database.connect() as db:
            return {
                r["item_key"]: json.loads(r["payload"])
                for r in db.execute(
                    "SELECT item_key, payload FROM kg_rebuild_checkpoint "
                    "WHERE notebook_id=? AND input_version=? AND stage=?",
                    (notebook_id, input_version, stage)).fetchall()
            }

    def checkpoint_put(
        self,
        notebook_id: str,
        input_version: str,
        stage: str,
        rows: List[Tuple[str, dict]],
        now: str,
    ) -> None:
        """把一批已完成 item 落库(一个写事务,幂等 REPLACE)。rows=[(item_key, payload_dict)]。"""
        if not rows:
            return
        with self.database.write() as db:
            db.executemany(
                "INSERT OR REPLACE INTO kg_rebuild_checkpoint "
                "(notebook_id, input_version, stage, item_key, payload, created_at) "
                "VALUES (?,?,?,?,?,?)",
                [(notebook_id, input_version, stage, k, json.dumps(v), now) for k, v in rows])

    def checkpoint_put_current(
        self, notebook_id: str, input_version: str, stage: str,
        rows: List[Tuple[str, dict]],
    ) -> None:
        if self.now is None:
            raise RuntimeError("checkpoint clock is not configured")
        return self.checkpoint_put(
            notebook_id, input_version, stage, rows, self.now()
        )

    # ---------------------------------------------------- rebuild end-state
    @staticmethod
    def finish_rebuild_state(
        db: sqlite3.Connection,
        notebook_id: str,
        cluster_input_version: str,
        cluster_count: int,
        now: str,
    ) -> None:
        """The rebuild end-write: store the input version this rebuild consumed
        and clear dirty. CRITICAL: MUST NOT touch kg_mutation_seq — omitted
        from both the column list and the SET so an existing row's counter is
        PRESERVED."""
        object_count = db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=? AND status!='deprecated'",
            (notebook_id,),
        ).fetchone()["c"]
        relation_count = db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_relations WHERE notebook_id=?",
            (notebook_id,),
        ).fetchone()["c"]
        db.execute(
            """
            INSERT INTO unified_kg_state
            (notebook_id, dirty, cluster_input_version, last_rebuild_at, object_count, relation_count, cluster_count, updated_at)
            VALUES (?, 0, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(notebook_id) DO UPDATE SET
              dirty=0,
              cluster_input_version=excluded.cluster_input_version,
              last_rebuild_at=excluded.last_rebuild_at,
              object_count=excluded.object_count,
              relation_count=excluded.relation_count,
              cluster_count=excluded.cluster_count,
              updated_at=excluded.updated_at
            """,
            (notebook_id, cluster_input_version, now, object_count, relation_count, cluster_count, now),
        )

    # ---------------------------------------------------- canonical relations
    @staticmethod
    def canonical_relations_count(db: sqlite3.Connection, notebook_id: str) -> int:
        return int(db.execute(
            "SELECT COUNT(*) AS c FROM canonical_relations WHERE notebook_id=?",
            (notebook_id,)).fetchone()["c"])

    @staticmethod
    def edge_support_rows(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            "SELECT canonical_src, edge_type, canonical_tgt, support_count, source_count "
            "FROM canonical_relations WHERE notebook_id=?", (notebook_id,))

    @staticmethod
    def relation_support_rows(
        db: sqlite3.Connection, notebook_id: str, triples: List[tuple],
    ) -> List[sqlite3.Row]:
        """Bounded point lookup by the ``canonical_relations`` primary key
        ``(notebook_id, canonical_src, edge_type, canonical_tgt)`` — the
        batched replacement for ``edge_support_rows``'s per-notebook full
        table scan (see that method + ``graph_retrieval.relation_support_counts``
        for the semantics this must match). Row-value ``IN`` — same form as
        the PostgreSQL adapter — not an OR chain: measured on an un-ANALYZEd
        database (this repo never runs ``ANALYZE`` against its production
        SQLite databases), the OR-chain form's plan degrades from
        per-branch PK seeks to ``SEARCH ... USING COVERING
        INDEX (notebook_id=?)`` — a scan of the whole notebook partition —
        once the branch count crosses roughly a few dozen; a production-scale
        measurement on the batch-1 hot-path audit put that at 115ms vs. the
        row-value form's 0.02ms (~440×) on the same table, and this repo's
        own toy reproduction at 30 branches over 200k rows showed the same
        qualitative flip (``EXPLAIN QUERY PLAN`` losing the composite-PK
        seek). SQLite's query planner also starts mis-costing OR chains
        somewhere around N≈2334 branches, and the OR chain's expression tree
        can hit SQLite's parser node-count ceiling somewhere in the
        N≈1000–10000 range depending on build flags — both reachable here
        before the caller's own batching (see
        ``GraphRetrievalService.relation_support_counts``) caps it. Row-value
        IN instead walks the composite PK directly via one seek per tuple
        (confirmed via ``EXPLAIN QUERY PLAN``: ``SEARCH ... USING INDEX
        sqlite_autoindex_canonical_relations_1 (notebook_id=? AND
        canonical_src=? AND edge_type=? AND canonical_tgt=?)``), independent
        of ``sqlite_stat1`` / ``ANALYZE``. ``ORDER BY`` makes the row order
        deterministic (ascending on the same three PK columns) regardless of
        physical storage order or the caller's tuple order — the caller
        builds an unordered ``dict`` from these rows so this is not required
        for correctness, only for reproducible reads (tests, logs).

        投影里带 ``support_count``(热路径修复批 2 · R2-1):这条定点查询现在有
        两个消费者,它们要的列不同——``relation_support_counts`` 只读
        ``source_count``,而 ``KnowledgeQueryService.annotate_edge_support``
        (从整表 ``edge_support_rows`` 迁过来)要 ``(support_count, source_count)``
        二元组,与它替换掉的那份整表 map 的值形状逐字一致。两列都在
        ``pk_canonical_relations`` 的行里,多投影一列不改变访问路径(仍是每个
        tuple 一次 PK seek),也不改变任何既有调用方的取值——它们按列名读。
        与其复制一份只差一列的孪生方法(连同上面这整段 row-value IN 的论证),
        不如让这一条继续做 canonical_relations 支撑数的**唯一有界定点原语**。"""
        rows = [triple for triple in triples if triple]
        if not rows:
            return []
        placeholders = ",".join("(?,?,?)" for _ in rows)
        params: List[object] = [notebook_id]
        for triple in rows:
            params.extend(triple)
        return db.execute(
            f"SELECT canonical_src, edge_type, canonical_tgt, "
            f"       support_count, source_count "
            f"FROM canonical_relations WHERE notebook_id=? "
            f"AND (canonical_src, edge_type, canonical_tgt) IN ({placeholders}) "
            f"ORDER BY canonical_src, edge_type, canonical_tgt",
            params,
        ).fetchall()

    @staticmethod
    def weak_support_relation_rows(
        db: sqlite3.Connection,
        notebook_id: str,
        canonical_ids: List[str],
        source_max: int,
        limit: int,
    ) -> List[sqlite3.Row]:
        """BOUNDED weak-support probe (设计文档 §3.3):给定 canonical 源端集合,
        取**来源数** ≤ ``source_max`` 的出边,最多 ``limit`` 行。

        判据是 `source_count`(支撑这条边的**不同文档**数)而不是 `support_count`
        (聚合掉的原始关系行数)。两者在 canonical 层经常差得很远:别名归一与 claim
        聚簇会让同一篇文档里的好几条原始关系折叠进同一条 canonical 边,于是一条
        **单源**边可以攒出 5 行 support —— 按行数过滤恰好把它滤掉,而它正是这个
        特性要救的那一批(只有一篇文献提到、最该补证)。头行对模型说的也是
        「仅 1-2 源支撑」,按行数算会让那句断言直接失真。

        谓词 `(notebook_id, canonical_src)` 正好是 `canonical_relations` 主键的
        **前缀**,所以每个源端都是一次索引 seek,不扫全表。但代价随源端的**出度**
        增长而不是恒定:`source_count` 上没有索引,所以它是 seek 之后的残余过滤,
        一个 hub 源端单次可以扫到数万行(`LIMIT` 只封住**返回**行数,封不住扫描
        行数)。本特性靠「每 run ≤7 次、只探本轮新出现的 seed」把总量压住。

        `ORDER BY` 比合同多带两个尾键(`canonical_src`/`edge_type`)。合同只写了
        计数键与 `canonical_tgt`,而它们相等的行完全可能来自不同源端/不同边类型
        —— 那时「取前 24 行」取到哪 24 行由存储顺序决定,双后端不必一致、同一个
        库两次也不必一致。补上尾键只在合同留白处定序,不改前两键的语义。

        反向(`canonical_tgt IN (...)`)刻意不做:那一列没有索引,加覆盖索引要动
        双后端 schema。登记为残余,等真机评估证明 src 侧有用再花这笔。
        """
        if not canonical_ids:
            return []
        placeholders = ",".join("?" for _ in canonical_ids)
        return db.execute(
            f"SELECT canonical_src, edge_type, canonical_tgt, source_count, "
            f"       sample_relation_ids "
            f"FROM canonical_relations "
            f"WHERE notebook_id=? AND canonical_src IN ({placeholders}) "
            f"  AND source_count<=? "
            f"ORDER BY source_count ASC, canonical_tgt ASC, canonical_src ASC, "
            f"         edge_type ASC "
            f"LIMIT ?",
            [notebook_id, *canonical_ids, source_max, limit],
        ).fetchall()

    @staticmethod
    def relation_endpoint_name_rows(
        db: sqlite3.Connection, notebook_id: str, relation_ids: List[str]
    ) -> List[sqlite3.Row]:
        """按**关系 id** 批量解析两端显示名(`canonical_name` 优先,payload name 回退)。

        为什么经关系而不是直接按 `canonical_id` 查 `concept_clusters`:那张表上
        `canonical_id` **没有索引**(只有 `notebook_id` 与 `member_object_id`),
        所以 `WHERE notebook_id=? AND canonical_id IN (...)` 会退化成「把该
        notebook 的每一行簇成员都取回来再残余过滤」—— 实测 4 万行簇的库 62ms,
        按行数线性放大到百万级簇就是秒级,而这条查询每次被接受的大纲更新都要跑
        一次。`canonical_relations.sample_relation_ids` 给了一条**全索引**的等价
        路径:样本关系 id 是主键,它的两个端点对象 id 也是主键,而端点对象落在哪个
        簇由 `idx_clusters_member` seek。数据来源的优先级与合同逐字一致,只是到达
        它的路径换成了有索引的那条。

        join 形状与 `canonical_relation_seed_rows` 逐字同构(同样的两张对象表 +
        两条 `member_object_id` 簇 join),那条查询正是 canonical 边的产地 —— 于是
        「样本关系的两端折叠后就是这条 canonical 边的两端」是构造上成立的,不是
        一句需要人复核的话。

        ⚠ **快照口径,刻意不继承产地的状态过滤**:产地那条查询排掉 `rejected` 关系、
        并只把 `deprecated` 之外的对象算进图,而这里按现存行原样解析 —— 样本关系
        自上次 rebuild 后被判 rejected、或端点对象被 deprecated 时,名字照样解析得
        出来。这是**有意**的:`canonical_relations` 整张表本来就是上次 rebuild 的
        快照(support/source 计数同样是那一刻的),提示行说的是「上次建图时这条边
        只有一两篇文献撑着」。在这里补状态过滤只会让名字与计数分属两代事实,而代价
        是两条 join 各多一个无索引的残余谓词。真正过期的整份快照由 rebuild 换掉。
        """
        if not relation_ids:
            return []
        placeholders = ",".join("?" for _ in relation_ids)
        return db.execute(
            f"SELECT kr.id AS rid, "
            f"       COALESCE(NULLIF(cs.canonical_name,''), "
            f"                json_extract(so.payload,'$.name'), '') AS src_name, "
            f"       COALESCE(NULLIF(ct.canonical_name,''), "
            f"                json_extract(tp.payload,'$.name'), '') AS tgt_name "
            f"FROM knowledge_relations kr "
            f"JOIN knowledge_objects so ON so.id=kr.source_object_id "
            f"JOIN knowledge_objects tp ON tp.id=kr.target_object_id "
            f"LEFT JOIN concept_clusters cs ON cs.notebook_id=kr.notebook_id "
            f"  AND cs.member_object_id=kr.source_object_id AND cs.generation = {_PUBLISHED_CLUSTER_GEN} "
            f"LEFT JOIN concept_clusters ct ON ct.notebook_id=kr.notebook_id "
            f"  AND ct.member_object_id=kr.target_object_id AND ct.generation = {_PUBLISHED_CLUSTER_GEN} "
            f"WHERE kr.notebook_id=? AND kr.id IN ({placeholders})",
            [notebook_id, notebook_id, notebook_id, *relation_ids],
        ).fetchall()

    @staticmethod
    def replace_canonical_relations(
        db: sqlite3.Connection, notebook_id: str, rows: List[tuple], seq: int
    ) -> None:
        db.execute("DELETE FROM canonical_relations WHERE notebook_id=?", (notebook_id,))
        for i in range(0, len(rows), 1000):
            db.executemany(
                "INSERT INTO canonical_relations "
                "(notebook_id, canonical_src, edge_type, canonical_tgt, "
                " support_count, source_count, sample_relation_ids, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)", rows[i:i + 1000])
        db.execute(
            "UPDATE unified_kg_state SET canonical_rel_seq=? WHERE notebook_id=?",
            (seq, notebook_id))

    # ------------------------------------------------------- mention bridge
    @staticmethod
    def clear_mention_bridge(db: sqlite3.Connection, notebook_id: str) -> None:
        db.execute("DELETE FROM mention_edges WHERE notebook_id=?", (notebook_id,))
        db.execute("DELETE FROM concept_comentions WHERE notebook_id=?", (notebook_id,))

    @staticmethod
    def mention_edges_count(db: sqlite3.Connection, notebook_id: str) -> int:
        return int(db.execute(
            "SELECT COUNT(*) AS c FROM mention_edges WHERE notebook_id=?",
            (notebook_id,)).fetchone()["c"])

    @staticmethod
    def replace_mention_bridge(
        db: sqlite3.Connection,
        notebook_id: str,
        edges: List[tuple],
        comention_rows: List[tuple],
        seq: int,
    ) -> None:
        db.execute("DELETE FROM mention_edges WHERE notebook_id=?", (notebook_id,))
        db.execute("DELETE FROM concept_comentions WHERE notebook_id=?", (notebook_id,))
        for i in range(0, len(edges), 1000):
            db.executemany(
                "INSERT INTO mention_edges "
                "(notebook_id, claim_object_id, concept_canonical_id, matched_alias) "
                "VALUES (?,?,?,?)", edges[i:i + 1000])
        for i in range(0, len(comention_rows), 1000):
            db.executemany(
                "INSERT INTO concept_comentions "
                "(notebook_id, canonical_a, canonical_b, bridge_claims) "
                "VALUES (?,?,?,?)", comention_rows[i:i + 1000])
        db.execute(
            "UPDATE unified_kg_state SET mention_seq=? WHERE notebook_id=?",
            (seq, notebook_id))

    # ---------------------------------------------------------- communities
    @staticmethod
    def communities_count(
        db: sqlite3.Connection, notebook_id: str, level: int
    ) -> "sqlite3.Row | None":
        return db.execute(
            "SELECT COUNT(*) AS c FROM communities WHERE notebook_id=? AND level=? "
            f"AND generation = {_PUBLISHED_COMMUNITY_GEN}",
            (notebook_id, level, notebook_id)).fetchone()

    @staticmethod
    def write_communities_generation(
        db: sqlite3.Connection,
        notebook_id: str,
        level: int,
        kept: List[Tuple[str, List[str]]],
        names: Dict[str, str],
        deg: Dict[str, float],
        now: str,
        generation: int,
    ) -> None:
        """写新代(批 3·W2 §1.3,取代旧 replace_communities 的整表重写):
        不 DELETE,旧代行由发布路径前的预回收清理;理由见 PG 孪生。"""
        for cid, members in kept:
            db.execute(
                "INSERT INTO communities (id, notebook_id, level, member_ids, size, created_at, generation) "
                "VALUES (?,?,?,?,?,?,?)",
                (cid, notebook_id, level, json.dumps(members), len(members), now,
                 generation))
            db.executemany(
                "INSERT INTO community_members "
                "(canonical_id, notebook_id, level, community_id, canonical_name, centrality, generation) "
                "VALUES (?,?,?,?,?,?,?)",
                [(m, notebook_id, level, cid, names.get(m, m), deg.get(m, 0.0),
                  generation) for m in members])

    @staticmethod
    def copy_forward_communities(
        db: sqlite3.Connection,
        notebook_id: str,
        exclude_level: int,
        from_generation: int,
        to_generation: int,
        mint_id,
    ) -> int:
        """翻转事务内复制未被本轮重建的 level 进新代,板块 id 重铸 + 成员行
        同步重映射——语义与 PG 孪生逐条对应(那边有完整理由)。"""
        rows = db.execute(
            "SELECT id, level, member_ids, size, title, summary, findings, "
            "created_at FROM communities "
            "WHERE notebook_id=? AND level != ? AND generation=?",
            (notebook_id, exclude_level, from_generation)).fetchall()
        remap: Dict[str, str] = {}
        for r in rows:
            new_id = mint_id()
            remap[str(r["id"])] = new_id
            db.execute(
                "INSERT INTO communities "
                "(id, notebook_id, level, member_ids, size, title, summary, "
                "findings, created_at, generation) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (new_id, notebook_id, r["level"], r["member_ids"], r["size"],
                 r["title"], r["summary"], r["findings"], r["created_at"],
                 to_generation))
        if remap:
            mrows = db.execute(
                "SELECT canonical_id, level, community_id, canonical_name, centrality "
                "FROM community_members "
                "WHERE notebook_id=? AND level != ? AND generation=?",
                (notebook_id, exclude_level, from_generation)).fetchall()
            db.executemany(
                "INSERT INTO community_members "
                "(canonical_id, notebook_id, level, community_id, canonical_name, centrality, generation) "
                "VALUES (?,?,?,?,?,?,?)",
                [(m["canonical_id"], notebook_id, m["level"],
                  remap[str(m["community_id"])], m["canonical_name"],
                  m["centrality"], to_generation)
                 for m in mrows if str(m["community_id"]) in remap])
        return len(remap)

    @staticmethod
    def discard_board_dependent_kg_analysis_artifacts(
        db: sqlite3.Connection, notebook_id: str
    ) -> None:
        """作废依赖板块划分的两份 KG 分析产物 —— 必须与 `replace_communities`
        **同一个写事务**(理由见 `kg_analysis_precompute.BOARD_DEPENDENT_ARTIFACT_KINDS`)。

        明细行与账本行都删。读侧确实**不会**把只剩明细行的那一半读出来 —— T3 的
        `kg_analysis._detail_rows_are_readable` 把两条明细查询门控在账本行上 —— 但那是
        读侧的自保,不是这里可以少删一半的理由:留着的是指向已重铸板块 id 的悬空行,
        生产上量级不小(跨板块边上限 20 万行 / 来源画像 ≈ 来源数),而且下一轮预计算
        若在写明细之前失败,它们就会一直躺在库里。两侧都做,才是「悬空行不存在」。

        另三份统计快照与板块无关,刻意不动 —— 见那个常量的说明。
        """
        db.execute(
            "DELETE FROM kg_community_edges WHERE notebook_id=?", (notebook_id,)
        )
        db.execute(
            "DELETE FROM kg_source_profiles WHERE notebook_id=?", (notebook_id,)
        )
        placeholders = ",".join("?" * len(BOARD_DEPENDENT_ARTIFACT_KINDS))
        db.execute(
            "DELETE FROM kg_analysis_artifacts "
            f"WHERE notebook_id=? AND kind IN ({placeholders})",
            (notebook_id, *BOARD_DEPENDENT_ARTIFACT_KINDS),
        )

    @staticmethod
    def set_community_seq(db: sqlite3.Connection, notebook_id: str, seq: int) -> None:
        db.execute("UPDATE unified_kg_state SET community_seq=? WHERE notebook_id=?",
                   (seq, notebook_id))

    @staticmethod
    def community_member_ids(
        db: sqlite3.Connection, notebook_id: str, level: int
    ) -> List[List[str]]:
        rows = db.execute(
            "SELECT member_ids FROM communities WHERE notebook_id=? AND level=? "
            f"AND generation = {_PUBLISHED_COMMUNITY_GEN} "
            "ORDER BY size DESC, id ASC",
            (notebook_id, level, notebook_id)).fetchall()
        return [json.loads(r["member_ids"]) for r in rows]

    @staticmethod
    def community_rows_for_summary(
        db: sqlite3.Connection, notebook_id: str, level: int
    ) -> List[sqlite3.Row]:
        return db.execute(
            "SELECT id, member_ids FROM communities WHERE notebook_id=? AND level=? "
            f"AND generation = {_PUBLISHED_COMMUNITY_GEN}",
            (notebook_id, level, notebook_id)).fetchall()

    @staticmethod
    def set_community_summary(
        db: sqlite3.Connection,
        community_id: str,
        title: str,
        summary: str,
        findings_json: str,
    ) -> None:
        db.execute("UPDATE communities SET title=?, summary=?, findings=? WHERE id=?",
                   (title, summary, findings_json, community_id))

    @staticmethod
    def community_reports(
        db: sqlite3.Connection, notebook_id: str, level: int
    ) -> List[dict]:
        rows = db.execute(
            "SELECT member_ids, title, summary, findings FROM communities "
            "WHERE notebook_id=? AND level=? AND summary!='' "
            f"AND generation = {_PUBLISHED_COMMUNITY_GEN} "
            "ORDER BY size DESC, id ASC",
            (notebook_id, level, notebook_id)).fetchall()
        return [{"member_ids": json.loads(r["member_ids"] or "[]"), "title": r["title"],
                 "summary": r["summary"], "findings": json.loads(r["findings"] or "[]")} for r in rows]

    # -------------------------------------------- community-peer primitives
    # communities.py(对比检索原语)的读接口 —— 自己开只读连接(原实现即用
    # 独立 repo._connect() 短查询;WAL 并发读)。
    def mounted_base_ids(self, active_nb: str) -> list[str]:
        """本库挂载的有效参考库 id —— 社区对比检索的扩展域。原
        first_base_notebook_id 的全局 LIMIT 1 在多领域下无意义。"""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT b.id AS id " + MOUNT_JOIN + MOUNT_VALID + MOUNT_ORDER,
                (active_nb,)).fetchall()
        return [row["id"] for row in rows]

    def resolve_focal(self, notebook_id: str, focal_key: str) -> Optional[str]:
        """focal 归一键 → canonical_id(lower(canonical_name)==key,多簇取成员最多者)。"""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT canonical_id FROM concept_clusters WHERE notebook_id=? AND lower(canonical_name)=? "
                f"AND generation = {_PUBLISHED_CLUSTER_GEN} "
                "GROUP BY canonical_id ORDER BY COUNT(*) DESC, canonical_id ASC LIMIT 1",
                (notebook_id, focal_key, notebook_id)).fetchone()
        return row["canonical_id"] if row else None

    def top_community_for(self, notebook_id: str, canonical_id: str) -> Optional[str]:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT community_id FROM community_members WHERE notebook_id=? AND canonical_id=? "
                f"AND generation = {_PUBLISHED_COMMUNITY_GEN} "
                "ORDER BY level DESC, community_id ASC LIMIT 1",
                (notebook_id, canonical_id, notebook_id)).fetchone()
        return row["community_id"] if row else None

    def community_member_peers(
        self, notebook_id: str, community_id: str, exclude_canonical_id: str, limit: int
    ) -> List[sqlite3.Row]:
        with self.database.connect() as db:
            return db.execute(
                "SELECT canonical_name, centrality FROM community_members "
                "WHERE notebook_id=? AND community_id=? AND canonical_id!=? "
                f"AND generation = {_PUBLISHED_COMMUNITY_GEN} "
                "ORDER BY centrality DESC, canonical_id ASC LIMIT ?",
                (notebook_id, community_id, exclude_canonical_id, notebook_id, limit)
            ).fetchall()

    def comention_peers(
        self, notebook_id: str, canonical_id: str, min_bridge: int, limit: int
    ) -> List[Tuple[str, int]]:
        """concept_comentions 两侧按 bridge_claims 降序取对端 canonical_name。"""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT canonical_a, canonical_b, bridge_claims FROM concept_comentions "
                "WHERE notebook_id=? AND (canonical_a=? OR canonical_b=?) AND bridge_claims>=? "
                "ORDER BY bridge_claims DESC, "
                "CASE WHEN canonical_a=? THEN canonical_b ELSE canonical_a END ASC LIMIT ?",
                (notebook_id, canonical_id, canonical_id, min_bridge,
                 canonical_id, limit)).fetchall()
            out: List[Tuple[str, int]] = []
            for r in rows:
                other = r["canonical_b"] if r["canonical_a"] == canonical_id else r["canonical_a"]
                nm = db.execute(
                    "SELECT canonical_name FROM concept_clusters WHERE notebook_id=? "
                    f"AND canonical_id=? AND generation = {_PUBLISHED_CLUSTER_GEN} LIMIT 1",
                    (notebook_id, other, notebook_id)).fetchone()
                if nm and nm["canonical_name"]:
                    out.append((nm["canonical_name"], int(r["bridge_claims"])))
            return out

    # ------------------------------------------ KG 质量分析(只读聚合,T1)
    # 承 docs/superpowers/specs/2026-07-25-kg-analysis-view-design.md「T1 只读聚合
    # 查询层」。四个查询都**自开只读连接**(与上面的 community-peer 原语同形),绝不写库。
    #
    # ⚠ 调用契约:**调用方不得持有外层写事务**。这里自开连接(SQLite 侧是本线程复用的
    # 读连接,PostgreSQL 侧是从池里另取一条),从 write() 里调用会读到提交前的旧数据,
    # 而且不会响亮失败——它会安静地返回一份过时的报告。T2 起唯一的调用方是
    # KnowledgeLifecycleService._compute_kg_analysis,它整个跑在**发布写事务之外**
    # (原子发布之后,板块与三条统计快照由同一个 `_write()` 一起写出去 —— 计算却必须
    # 全部发生在它之前)。T3 落地 KgAnalysisService 时要在服务层把这条契约钉成断言
    # (见 ports.py 同段)。
    #
    # 口径与产品一致(设计 §3.5,沿用 scripts/kg_quality_audit.py 踩过 9 轮评审的那套):
    #   · 对象只算 status IN USABLE_STATUSES;
    #   · 边只算 review_status != 'rejected',且**两端都是本 notebook 的可用对象**——
    #     knowledge_relations 的两个端点列既没有外键、也没有 notebook 归属约束,历史/
    #     损坏数据会指向别的库或已 deprecated 的对象,而产品的图/检索按可用节点集建图,
    #     那种边根本进不去;
    #   · 被排除的量一律单独返回,不凭空消失。
    #
    # 效率:分桶/分组一律发生在 SQL 里,绝不把逐簇/逐边的明细拉进 Python 再聚合
    # (生产库 concept_clusters 200 万+ 行、knowledge_relations 800 万+ 行),也绝不发
    # 巨型 IN。所有有界返回都带截断标志。
    #
    # ⚠ 与 PostgreSQL 侧的形态分岔(parity 要求语义等价 + 两侧都有守卫,不要求 SQL
    # 逐字相同):`community_overview` 在 PostgreSQL 上用**一条窗口函数查询**取全部
    # 板块的代表概念,这里保持**逐板块有界 top-K**。理由见该方法的 docstring。返回值
    # 的等价性由 tests/postgres/test_knowledge_store_conformance.py 对同一批夹具在
    # 两个后端各跑一遍、断言同一份期望值来证明。

    def _reject_inside_write_transaction(self, what: str) -> None:
        """本段的只读聚合**不得**在本线程的写事务内被调用。当场硬失败。

        两个理由,后一个更硬:

        1. 正确性:这些方法用的是本线程复用的**读**连接,而 `SqliteDatabase.write()`
           每次另开一条**独立**连接。在写事务里调它们,读到的是那个事务提交**之前**的
           库 —— 一份过时的报告,而且不会有任何报错。
        2. 可用性:`write()` 拿的是**进程级写锁**(`threading.Lock`)。这三条是全表级
           重活(生产 200 万簇行 / 836 万边,同形状数据点是冷扫 39 分钟),放进写事务
           就等于把全库的写入按住一整趟全表扫。

        为什么要一条运行时守卫而不是只写注释:调用契约原来只有注释,而**形状守卫抓不到
        违规** —— 「哪个写事务碰过产物表」这种断言对这三条查询完全无效,它们一张产物表
        都不碰,在 SQLite 上还跑在另一条连接上。评审用「把三条调用从 `_write()` 外面
        搬到里面」的移动变异实测过:25 条测试**全绿**。

        `write_depth` 是 thread-local 的,所以只拦「本线程正持有写事务」;别的线程在写
        与本次读无关(WAL 允许并发读),不拦。
        """
        if self.database.in_write_transaction:
            raise RuntimeError(
                f"{what}:全表级只读聚合被放进了写事务里。它会读到提交前的旧数据"
                "(不报错、静默过时),并且把进程级写锁按住一整趟全表扫"
                "(生产规模同形状数据点:835 万边冷扫 39 分钟)。"
                "把它挪到 write() 之外调用。"
            )

    @contextmanager
    def _read_snapshot(self) -> Iterator[sqlite3.Connection]:
        """一次显式只读事务 = 一个 WAL 快照,让**多条语句**看到同一份库。

        Python 的 sqlite3 在 legacy 事务控制下**不会**为 SELECT 开启事务
        (``in_transaction`` 恒为 False),所以同一个 ``with connect()`` 里连续两条
        SELECT 会各自取一个快照。并发的 ``rebuild_communities``(DELETE + INSERT
        同事务)只要在两条语句之间提交,报告就会自相矛盾:先数到 1200 个板块、再一个
        都取不到(``total=1200, returned=0, truncated=True``),或者某个板块
        ``{"size": 3, "top_members": []}``。

        ``BEGIN DEFERRED`` 不立刻抢锁:快照在**第一条读语句**时建立,之后整块共享它。
        只读所以退出时 ``rollback()``(比 commit 更早释放读标记,语义也更诚实)。
        ``owned`` 守卫:万一被套在别人已开的事务里,就不越权接管它的边界。

        ⚠ 与 PostgreSQL 侧的一处**不对称,如实说明**:PG 的 `_read_snapshot` 还会发
        ``BEGIN … READ ONLY``,让「绝不写库」由引擎强制;这里**没有**对应的
        ``PRAGMA query_only``。原因不是疏忽:PG 每次从池里借的是一条**专属**连接,会话
        级开关随连接归还自动复位;而这里拿到的是**本线程复用的共享读连接**,在它上面
        翻会话开关就是本模块 ``_Conn`` docstring 反复警告的那类状态泄漏(一旦某条路径
        没走到 finally,整条线程连接就永久只读了)。SQLite 侧的「只读」因此由测试守卫
        兜底(tests/test_kg_analysis_queries.py::test_analysis_queries_never_write:
        语句级 trace + 相关表的行数与内容指纹),不是由引擎兜底。
        """
        db = self.database.connect()
        owned = not db.in_transaction
        if owned:
            db.execute("BEGIN DEFERRED")
        try:
            yield db
        finally:
            if owned and db.in_transaction:
                db.rollback()

    @contextmanager
    def read_snapshot(self) -> Iterator[sqlite3.Connection]:
        """**给外部调用方**的多语句共享快照 —— 与 `community_overview` 内部用的是同一
        个机制(`_read_snapshot`),只是把它开成一个可以骑的连接。

        为什么必须公开而不是让调用方自己 `database.connect()`(codex 第 8 轮 P2):
        T3 的 `/sources` 一趟要发**四条**语句(state 行、账本、来源画像的 COUNT、那一页
        的 SELECT),而 `connect()` 给的是**自动提交**的读连接 —— 每条语句各取一个快照。
        并发的预计算(`replace_kg_analysis_artifacts` 整批重写)只要提交在中间,新写入的
        画像行就会被盖上**上一代**账本的世代戳,`total` 与 `rows` 也可以互相对不上。

        ⚠ 与 PostgreSQL 侧的同名方法**语义等价、实现分岔**(§3.35 允许):那边是
        `BEGIN … READ ONLY` + REPEATABLE READ,这边是 `BEGIN DEFERRED` 起 WAL 快照,
        且**刻意不设** `PRAGMA query_only` —— 这条是本线程复用的共享读连接,翻会话级
        开关会泄漏状态(完整理由见 `_read_snapshot`)。参数表**刻意为空**:调用方是
        后端中性的 service,不该知道「要不要 REPEATABLE READ」这种方言细节,该由哪一侧
        怎么兑现由 store 自己决定。
        """
        with self._read_snapshot() as db:
            yield db

    def cluster_size_histogram(self, notebook_id: str) -> Dict[str, object]:
        """收敛率的分布面:簇大小分桶直方图,**按 object_type 分组**(A2)。

        ⚠ **重活**,与下面 `largest_clusters` / `relation_provenance_counts` 同级。
        本 notebook 的 concept_clusters 全扫 + 每行一次 knowledge_objects 主键探查 +
        (object_type, canonical_id) 的临时 B 树分组。生产库 200 万+ 簇行。代价量级见
        ports.py 该段头的双后端标注。**不适合挂在在线请求上**,由调用方(T3 按
        kg_mutation_seq 记忆化的预计算路径)决定何时调。

        ⚠ 内存也不便宜,而且那部分**不在 Python 里**:EXPLAIN QUERY PLAN 显示这条要建
        **两个** temp B 树(内层按 (object_type, canonical_id) 分组、外层按
        (object_type, bucket) 分组),内层无条件是本 notebook 的全部簇行(生产约 200 万)。
        `_connect()` 设了 `PRAGMA temp_store = MEMORY`(mention-alias 那条 TEMP FTS 路径
        的硬需求),所以这些临时 B 树**不能落盘**。外层那个只有几十行可以忽略,内层那个
        不行。这里如实记下来,不为它做 SQLite 侧的调优(§3.35:性能调优只投 PostgreSQL,
        SQLite 侧的标准是正确、有界、不长时间持锁)。

        **一条 SQL 出分组分桶**——先按 (object_type, canonical_id) 分组算成员数,再把
        成员数映射成固定的 7 档(见 CLUSTER_SIZE_BUCKETS),分桶发生在 SQL 里,Python
        只负责补零、定序、求合计。逐簇计数绝不进 Python;结果集恒为「类型数 × 8」行。

        为什么按 object_type 分组:`concept_clusters` 同时装 concept / claim / formula /
        procedure 四类(见 knowledge_lifecycle 的 _TYPE_MERGE),后三类只做精确种子合并、
        天然几乎全是单元素簇,混在一起算收敛率会被稀释成噪音。分开报,每类各自可读。
        契约外的类型(knowhow 投影用用户列名建的自定义类型)归进 "other" 组,只报计数
        与类型个数、**不报类型名**(那是用户的表头,属于内容)。

        刻意**不**复用 concept_clusters_count + distinct_cluster_count:那是对同一批行
        的另外两趟扫描,而 member_rows / clusters 本来就是同一次 GROUP BY 的两个求和;
        并且它们没有 USABLE_STATUSES 过滤,复用会把已弃用成员算进收敛率。

        返回(顶层是全类型合计,`by_object_type` 是定长定序的五个分组):
          member_rows          计入的簇成员行数(成员对象可用)
          clusters             至少还有一个可用成员的簇数
          excluded_member_rows 成员对象缺失 / 跨库 / 状态不可用而被排除的簇成员行数
          empty_clusters       成员全被排除、整簇落空的簇数(单独报,不混进分桶)
          buckets              [{bucket, clusters, members}] × 7,定长定序
          by_object_type       [{object_type, object_types, …上面同一组字段}] × 5
        """
        self._reject_inside_write_transaction("簇大小直方图")
        placeholders = ",".join("?" for _ in USABLE_STATUSES)
        with self.database.connect() as db:
            rows = db.execute(
                f"""
            SELECT object_type, bucket,
                   COUNT(*) AS n_clusters,
                   SUM(usable) AS n_members,
                   SUM(raw - usable) AS n_excluded
            FROM (
              SELECT object_type,
                     CASE
                       WHEN usable = 0  THEN 'empty'
                       WHEN usable = 1  THEN '1'
                       WHEN usable = 2  THEN '2'
                       WHEN usable = 3  THEN '3'
                       WHEN usable <= 5  THEN '4-5'
                       WHEN usable <= 10 THEN '6-10'
                       WHEN usable <= 50 THEN '11-50'
                       ELSE '51+' END AS bucket,
                     usable, raw
              FROM (
                SELECT c.object_type AS object_type,
                       COUNT(*) AS raw,
                       SUM(CASE WHEN o.id IS NULL THEN 0 ELSE 1 END) AS usable
                FROM concept_clusters c
                LEFT JOIN knowledge_objects o
                       ON o.id = c.member_object_id
                      AND o.notebook_id = c.notebook_id
                      AND o.status IN ({placeholders})
                WHERE c.notebook_id = ?
                  AND c.generation = {_PUBLISHED_CLUSTER_GEN}
                GROUP BY c.object_type, c.canonical_id
              ) g
            ) b
            GROUP BY object_type, bucket
            """,
                (*USABLE_STATUSES, notebook_id, notebook_id),
            ).fetchall()
        return _cluster_histogram_payload(
            {
                (r["object_type"], r["bucket"]): (
                    int(r["n_clusters"]), int(r["n_members"] or 0), int(r["n_excluded"] or 0)
                )
                for r in rows
            }
        )

    def largest_clusters(
        self, notebook_id: str, limit: int = 20
    ) -> Dict[str, object]:
        """收敛率的头部:成员最多的 N 个 concept 簇(canonical_name + 成员数)。

        ⚠ **重活,与直方图同量级**——本 notebook 的 concept 簇行全扫 + 主键探查 + 分组,
        之后**还多一次排序**。LIMIT 只截断输出,不减少扫描与排序的输入。它只扫 concept
        分片,所以在 claim 占多数的库上绝对耗时可以比直方图低(本机真实库:14 ms vs
        64 ms),但**单位输入**的代价更高(同规模全 concept 合成数据上两者只差 ~1.7 倍,
        见 ports.py 该段头的双后端实测表)。总之它不便宜,别把它当廉价查询挂在线上。

        只取 `object_type='concept'`:榜单上冒出整句 claim 当名字没有意义。其他类型的
        成员行数与簇数由 `cluster_size_histogram` 按类型分组报出——那里本来就要全扫一遍,
        在这里再扫一遍只为了报两个数字不值当。载荷里的 ``object_type`` 显式声明这个口径。

        ``canonical_name`` 取 **`MIN(NULLIF(...,''))`**,不是裸 `MIN()`:同一个
        canonical_id 下的 canonical_name **并不保证一致** —— kg_merge.place_new_concepts
        对新簇用对象自己的 `o.get("name", "")`(空串因此是可达取值),只有落进已有簇时
        才沿用既有 canon 名。名字不一致在本机库上**实测存在**(nb-a73f16940c 有 16 个
        canonical_id 挂着多个不同的 canonical_name),裸 `MIN()` 在那里等于「随便挑一个」;
        一旦其中一个是空串,它在 `MIN()` 下永远排第一,榜首就变成
        `{"canonical_name": "", "members": N}`。

        如实说明证据强度:空名本身在本机这几个库里**没有**实测到
        (`SELECT COUNT(*) … WHERE TRIM(canonical_name)=''` 为 0),所以这条是按上面那条
        代码路径**可达**而防的,不是照着一个观测到的坏数据修的。代价是零(同一次分组
        聚合里多一个函数调用),收益是把「榜首无名」整类从结构上排除。
        全组都是空名时 `MIN(NULLIF(...))` 返回 NULL,`COALESCE` 兜回空串。

        ``limit`` 由调用方传,硬 clamp 到 [1, LARGEST_CLUSTERS_MAX] —— 无界返回在 200 万
        簇的库上就是把整张表搬进内存。多取一行判 ``truncated``,不额外跑 COUNT。
        """
        self._reject_inside_write_transaction("最大簇榜单")
        limit = _clamp(limit, LARGEST_CLUSTERS_MAX)
        placeholders = ",".join("?" for _ in USABLE_STATUSES)
        with self.database.connect() as db:
            rows = db.execute(
                f"SELECT c.canonical_id AS canonical_id, "
                f"       COALESCE(MIN(NULLIF(c.canonical_name, '')), '') AS canonical_name, "
                f"       COUNT(o.id) AS members "
                f"FROM concept_clusters c "
                f"JOIN knowledge_objects o ON o.id = c.member_object_id "
                f"     AND o.notebook_id = c.notebook_id "
                f"     AND o.status IN ({placeholders}) "
                f"WHERE c.notebook_id = ? AND c.object_type = 'concept' "
                f"AND c.generation = {_PUBLISHED_CLUSTER_GEN} "
                f"GROUP BY c.canonical_id "
                f"ORDER BY members DESC, c.canonical_id ASC LIMIT ?",
                (*USABLE_STATUSES, notebook_id, notebook_id, limit + 1),
            ).fetchall()
        return _largest_clusters_payload(
            [
                (r["canonical_id"], r["canonical_name"] or "", int(r["members"]))
                for r in rows
            ],
            limit,
        )

    def community_overview(
        self, notebook_id: str, *, level: int = 0, limit: int = 50, top_k: int = 5
    ) -> Dict[str, object]:
        """主题板块列表(C1)的**自开快照**入口 —— 给手上没有读连接的调用方用。

        查询本体在 `community_overview_on`(connection-taking)。这里只多做两件事:
        一道 `_reject_inside_write_transaction`(自开连接才需要,骑别人连接不需要),
        以及开一个多语句共享快照 —— total、板块列表、逐板块成员三段必须看同一份库,
        否则并发 rebuild_communities 一劈就会出现「有 1200 个板块,一个都没有」这种
        自相矛盾的载荷。

        ⚠ **一次调用只开一个快照**:两件事不能拆成两个 `with`,也不能让调用方为每条读
        各开一个 —— 那和没有快照一样坏(codex 第 8/12 轮 P2)。
        """
        self._reject_inside_write_transaction("主题板块列表")
        with self._read_snapshot() as db:
            return self.community_overview_on(
                db, notebook_id, level=level, limit=limit, top_k=top_k
            )

    @staticmethod
    def community_overview_on(
        db: sqlite3.Connection,
        notebook_id: str,
        *,
        level: int = 0,
        limit: int = 50,
        top_k: int = 5,
    ) -> Dict[str, object]:
        """主题板块列表(C1):板块 id / 规模 / 按 centrality 降序的前 K 个代表概念。

        **connection-taking**:骑调用方的读连接,不自开连接。存在的理由是 T3 的总览
        (`KgAnalysisService.overview`)一趟要读 state 行、账本、板块列表、跨板块边**四
        样**,它们必须来自**同一份库** —— 板块列表自开一个快照的话,并发的社区重建提交
        在中间就会把「上一代的新鲜度戳」与「新一代的板块 id」拼进同一份响应,俯瞰图照
        着这两样画出来的连线是悬空的(codex 第 12 轮 P2)。自开快照的调用方走上面那个
        同名入口。

        本方法自己也是**多条语句**(COUNT + 板块列表 + 逐板块成员),所以调用方给的必须
        是 `read_snapshot()` 开出来的连接而不是裸 `connect()`;两侧的 `read_snapshot`
        各自兑现这件事。**不**在这里补一道 `_reject_inside_write_transaction`:骑调用方
        连接不存在「自开的另一条连接读到提交前的库」这个失配,服务层入口另有一道同语义
        断言;而 staticmethod 也够不到 `self.database`。

        代表概念**逐板块**取(`ORDER BY centrality DESC ... LIMIT K`,走
        idx_commmem_nb_comm + 有界 top-K 排序器)。这里刻意与 PostgreSQL 侧**分岔**:
        PG 用一条 `ROW_NUMBER() OVER (PARTITION BY …)` 查询,因为它的瓶颈是 limit+2 次
        网络 round-trip;SQLite 是**同进程文件访问,往返本来就免费**,换成窗口函数只会
        把成本挪到临时 B 树上。

        这不是推演,是实测(本机真实库 nb-b37185f4ae,1217 个板块):

            limit=50    逐板块 3.0 ms   窗口函数  52.5 ms   (17×)
            limit=200   逐板块 4.8 ms   窗口函数 193.1 ms   (40×)

        两种写法在该库上返回**逐字相同**的结果(已比对),所以分岔的只是形态、不是语义。
        `EXPLAIN QUERY PLAN` 说明了原因:窗口函数版会 `MATERIALIZE ranked` 并为窗口的
        ORDER BY 建临时 B 树,即把选中板块的全部成员行排一遍;逐板块版只有一次
        `SEARCH … USING INDEX idx_commmem_nb_comm` + 一个有界 top-K 排序器,峰值排序
        内存是 O(K) 而不是 O(该板块成员数) —— 生产库上单个板块可以有几十万成员。
        板块数上限因此是代价的唯一旋钮。

        截断绝不静默:返回 ``total`` / ``returned`` / ``truncated``,每个板块另带
        ``top_members_truncated``(该板块成员数是否超过 K)。
        """
        level = int(level)
        limit = _clamp(limit, COMMUNITY_OVERVIEW_MAX)
        top_k = _clamp(top_k, COMMUNITY_TOP_MEMBERS_MAX)
        total = int(db.execute(
            "SELECT COUNT(*) AS c FROM communities WHERE notebook_id=? AND level=? "
            f"AND generation = {_PUBLISHED_COMMUNITY_GEN}",
            (notebook_id, level, notebook_id)).fetchone()["c"])
        picked = db.execute(
            "SELECT id, size FROM communities WHERE notebook_id=? AND level=? "
            f"AND generation = {_PUBLISHED_COMMUNITY_GEN} "
            "ORDER BY size DESC, id ASC LIMIT ?",
            (notebook_id, level, notebook_id, limit)).fetchall()
        communities = []
        for row in picked:
            members = db.execute(
                "SELECT canonical_name FROM community_members "
                "WHERE notebook_id=? AND level=? AND community_id=? "
                f"AND generation = {_PUBLISHED_COMMUNITY_GEN} "
                "ORDER BY centrality DESC, canonical_id ASC LIMIT ?",
                (notebook_id, level, row["id"], notebook_id, top_k)).fetchall()
            communities.append(
                (row["id"], int(row["size"]),
                 [m["canonical_name"] or "" for m in members])
            )
        return _community_overview_payload(communities, total, level, limit, top_k)

    def relation_provenance_counts(self, notebook_id: str) -> Dict[str, object]:
        """边的出处构成 —— ⚠ **重活:本 notebook 的 knowledge_relations 全表扫**。

        生产 base 库 800 万+ 边,每行还要两次 knowledge_objects 主键探查(口径要求两端
        都是本库可用对象)。**它不适合挂在在线请求上**——由调用方决定何时调(T3 会把它
        放进按 kg_mutation_seq 记忆化的路径,预计算阶段更合适)。

        ⚠ 代价量级**不要**从本机热态线性外推,也不要写成一个秒数。本机 5.1GB 真实库上
        5.2 万边:冷跑(全新进程)1165 ms,暖态 217 ms —— 同一条查询差 5 倍,而那还只是
        生产规模的 0.6%。仓库有同形状的实测数据点:`relation_connected_object_ids`
        (sqlite/knowledge_store.py)那次事故是 835 万边**冷扫 39 分钟**,而那条查询
        **没有**每行两次 PK 随机探查。本查询在 SQLite 上应按「与那个 39 分钟数据点同量级
        或更差」估,**不是秒级**。PostgreSQL 上规划器会选 hash join 并**并行顺扫**(本机
        实测 2 个 worker,同规模合成数据快约 4.4 倍),量级明显更好但仍是全表级 ——
        两侧的分列实测数字见 ports.py 该段头。

        只做**一次**扫描、只 COUNT 不取行:LEFT JOIN + 单层 CASE 让同一趟同时产出出处
        分桶与被排除量(rejected / 端点不可用),不为「不凭空消失」再补第二趟 COUNT。
        json 解析每行只做一次(先在内层子查询里算出 tag,再在外层分桶),而不是在一串
        WHEN 里反复 json_extract —— 实测这一项就快 3 倍。

        分桶顺序:``rejected`` 判在最前,所以一条既被否决、端点又不可用的边只计一次
        (计入 rejected)。``endpoint_unusable`` 合并了「端点不存在 / 跨库 / 状态不可用」
        三种情况——它们对产品是同一件事:那个端点不在可用节点集里,这条边进不了图。
        """
        self._reject_inside_write_transaction("边出处构成")
        placeholders = ",".join("?" for _ in USABLE_STATUSES)
        with self.database.connect() as db:
            rows = db.execute(
                f"""
            SELECT bucket, COUNT(*) AS n FROM (
              SELECT CASE
                       WHEN rs = 'rejected' THEN 'rejected'
                       WHEN bad THEN 'endpoint_unusable'
                       WHEN tag = 'relink:shared-element' THEN 'relink:shared-element'
                       WHEN tag = 'relink:name-match' THEN 'relink:name-match'
                       WHEN tag LIKE 'relink:%' THEN 'relink:other'
                       WHEN COALESCE(tag, '') != '' THEN 'tagged:other'
                       ELSE 'untagged' END AS bucket
              FROM (
                SELECT r.review_status AS rs,
                       (s.id IS NULL OR t.id IS NULL) AS bad,
                       CASE WHEN json_valid(r.evidence)
                            THEN json_extract(r.evidence, '$[0].basis') END AS tag
                FROM knowledge_relations r
                LEFT JOIN knowledge_objects s
                       ON s.id = r.source_object_id
                      AND s.notebook_id = r.notebook_id
                      AND s.status IN ({placeholders})
                LEFT JOIN knowledge_objects t
                       ON t.id = r.target_object_id
                      AND t.notebook_id = r.notebook_id
                      AND t.status IN ({placeholders})
                WHERE r.notebook_id = ?
              ) y
            ) x GROUP BY bucket
            """,
                (*USABLE_STATUSES, *USABLE_STATUSES, notebook_id),
            ).fetchall()
        return _relation_provenance_payload(
            {r["bucket"]: int(r["n"]) for r in rows}
        )

    # ------------------------------ KG 质量分析的预计算产物(T2,写路径)
    # 承设计 §3.2/§3.3/§3.4。这三张表是 `rebuild_communities` 的派生产物,由那一次
    # 图构建**顺带**产出;折叠逻辑在后端中性的 `app.services.kg_analysis_precompute`
    # 里,两侧 store 只负责「一次扫描」与「一次整体重写」。
    #
    # ⚠ 与上面的只读聚合段**相反**:这几个方法是 connection-taking 的,骑调用方的事务
    # 边界(不自开连接)。`replace_kg_analysis_artifacts` 必须和它的账本行落在**同一个
    # 事务**里 —— 设计 §3.3 的硬要求:一次预计算要么整批可见、要么完全不可见,绝不允许
    # 出现「跨板块边是新的、来源画像是旧的」这种隐蔽矛盾。
    #
    # ⚠ 三张表**都不带 level 维度**。社区层的新鲜度闸 `unified_kg_state.community_seq`
    # 本身就不分 level,所以「产物建于哪个 level」只能记在账本 payload 里(那里有
    # `level` 字段),不能靠明细表的列去区分。早先 `kg_community_edges` 带过一个 level
    # 列而另两张表没有,删除口径因此三处不一致:一次 level=1 的重建会抹掉 level-0 的
    # 账本、却留下 level-0 的明细行 —— 按「账本存在与否是唯一判据」它们「不存在」,却
    # 会被 `WHERE level=0` 读出来。现在三张表统一按 notebook 整表重写:一次预计算产出
    # 的永远是**一套**自洽的产物,描述账本里记着的那个 level。

    # ------------------------ KG 质量分析产物的**读**路径(T3,在线请求路径)
    # 与上面那三条 T1 只读聚合的关键区别:这三个只读**预计算产物表**,代价按行数硬
    # 有界(账本 ≤5 行;来源画像 ≤ 来源数、一次只取一页;跨板块边 ≤
    # MAX_PERSISTED_COMMUNITY_EDGES,一次只取 top-N)。所以它们**可以**挂在在线请求上,
    # 那三条全表重活不行。
    #
    # 连接契约:connection-taking,骑调用方(T3 的 KgAnalysisService / 预计算的新鲜度闸)
    # 的读连接 —— 不自开连接,故不需要那道 `_reject_inside_write_transaction`(它防的是
    # 「自开的另一条连接读到提交前的库」,骑调用方连接不存在这个失配)。服务层另有一道
    # 同语义的入口断言。

    @staticmethod
    def kg_analysis_artifact_rows(
        db: sqlite3.Connection, notebook_id: str
    ) -> Dict[str, Dict[str, object]]:
        """账本全文:``{kind: {kg_mutation_seq, payload, created_at}}``。最多 5 行,点读。

        **读账本只有这一个入口**:T3 的报告与预计算的新鲜度闸
        (`rebuild_communities`)共用它。早先另有一个只取 seq 的窄读,理由是「闸在热
        路径上,不该顺带把五份 payload 也读出来」;簇世代改盖进 payload(刻意不加列,
        见 `kg_analysis_precompute.CLUSTER_SEQ_PAYLOAD_KEY`)之后那个理由不再成立 ——
        闸也必须拿 payload 才判得出簇是否漂过,两个方法就只差一个 `created_at`。
        合成一个:账本的读取与判据都只剩一处,不可能漂。

        **行的存在与否才是「这份产物在不在」的判据**,明细表的行数不是:单一板块的图
        legitimately 产出 0 条跨板块边。调用方据此区分「缺失」与「为空」。

        payload 的解析与畸形校验在中性的 `kg_analysis_payloads.artifact_ledger_rows` 里
        (SQLite 存 JSON 文本、PostgreSQL 存 jsonb,上层拿到的形状必须一致)。
        """
        return _artifact_ledger_rows(
            (row["kind"], row["kg_mutation_seq"], row["payload"], row["created_at"])
            for row in db.execute(
                "SELECT kind, kg_mutation_seq, payload, created_at "
                "FROM kg_analysis_artifacts WHERE notebook_id=? ORDER BY kind",
                (notebook_id,),
            )
        )

    @staticmethod
    def kg_community_edges_top(
        db: sqlite3.Connection, notebook_id: str, limit: int = 200
    ) -> List[Tuple[str, str, int]]:
        """跨板块边的 **top-N**(按 weight 降序),给板块俯瞰图用。

        代价有界**靠的是行数上限而不是索引**,如实说明:主键是
        ``(notebook_id, src, dst)``,没有 ``weight`` 上的索引,所以这条是「本 notebook
        的行做一次范围扫 + 一个有界 top-N 排序器」。它可以挂在线上的理由是那次范围扫
        的输入被 T2 的 `MAX_PERSISTED_COMMUNITY_EDGES` 硬钉在 20 万行以内 —— 与那三条
        T1 全表重活(200 万簇行 / 836 万边)差一到两个数量级,而且是窄行、走主键前缀。
        真机上若这一条成为瓶颈,正解是加 ``(notebook_id, weight)`` 索引(要一次迁移),
        不是在这里放宽上限。

        ``limit`` 硬 clamp 到 [1, KG_COMMUNITY_EDGES_MAX]。**不多取一行判截断**:截断
        与否由账本里的 ``edges``(库里存了多少行)与这里的返回条数一比就知道,而账本
        本来就要读 —— 多取一行反而会让「返回 N 条」这个数字不再等于调用方要的 N。
        排序键补 ``src, dst`` 让并列 weight 的取法确定(两个后端逐字一致)。
        """
        limit = _clamp(limit, KG_COMMUNITY_EDGES_MAX)
        return [
            (row["src_community_id"], row["dst_community_id"], int(row["weight"]))
            for row in db.execute(
                "SELECT src_community_id, dst_community_id, weight "
                "FROM kg_community_edges WHERE notebook_id=? "
                "ORDER BY weight DESC, src_community_id ASC, dst_community_id ASC "
                "LIMIT ?",
                (notebook_id, limit),
            )
        ]

    @staticmethod
    def kg_source_profile_page(
        db: sqlite3.Connection,
        notebook_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        ascending: bool = True,
    ) -> "Tuple[int, List[Dict[str, object]]]":
        """来源画像的**一页**,外加该 notebook 的总行数 → ``(total, rows)``。

        生产 base 库有 48 836 个来源,一次全返回既是几 MB 的 payload、也没人读得完,
        所以分页是硬要求。排序键是 ``mainstream_share``(升序 = 与主体板块最不连通的
        排前面,正是「哪些来源不属于这里」这个问题的答案),走
        ``idx_kg_source_profiles_nb_mainstream``。

        ⚠ 并列消歧 ``source_id ASC`` 是**分页正确性**的一部分,不是洁癖:
        ``mainstream_share`` 上并列极多(混杂库里一大片恰好 0.0),没有这个次级键,
        两页之间会重复/漏掉行,而且两个后端还会各给一种顺序。代价是次级键不在索引里,
        SQLite 会对**并列组**补一次排序(``USE TEMP B-TREE FOR LAST TERM OF ORDER BY``)
        —— 上界是本表的全部行数(= 来源数),窄行,可接受。

        ``sources`` 的 LEFT JOIN 只发生在**已分页的 ≤200 行**上(子查询带 LIMIT,
        SQLite 不会把它拍平进 join),所以是至多 200 次主键点查。``source_id`` 没有外键
        (历史清理会留下指向已删 source 的引用,见 `_migration_32` 的说明),所以
        ``source_missing`` 显式报出来:标题为空到底是「这个来源没标题」还是「这个来源
        已经不在了」,读报告的人有权知道。

        ``total`` 单独 COUNT(*) 而**不**复用账本 payload 里的 ``sources``:分页要的是
        这张表此刻的真实行数。两者由写事务保证一致(明细与账本同批重写),但让分页去
        依赖另一张表的一个数字,只会在它们万一不一致时给出一份分不出页的报告。
        COUNT 走主键前缀,行数 ≤ 来源数,便宜。
        """
        limit = _clamp(limit, KG_SOURCE_PAGE_MAX)
        offset = max(0, int(offset))
        direction = "ASC" if ascending else "DESC"
        total = int(db.execute(
            "SELECT COUNT(*) AS c FROM kg_source_profiles WHERE notebook_id=?",
            (notebook_id,)).fetchone()["c"])
        rows = db.execute(
            f"""
            SELECT p.source_id AS source_id,
                   p.n_objects AS n_objects,
                   p.n_graph_objects AS n_graph_objects,
                   p.top_community_id AS top_community_id,
                   p.top_share AS top_share,
                   p.community_spread AS community_spread,
                   p.mainstream_share AS mainstream_share,
                   s.title AS title,
                   CASE WHEN s.id IS NULL THEN 1 ELSE 0 END AS source_missing
            FROM (
              SELECT notebook_id, source_id, n_objects, n_graph_objects,
                     top_community_id, top_share, community_spread, mainstream_share
              FROM kg_source_profiles
              WHERE notebook_id = ?
              ORDER BY mainstream_share {direction}, source_id ASC
              LIMIT ? OFFSET ?
            ) p
            LEFT JOIN sources s
                   ON s.id = p.source_id AND s.notebook_id = p.notebook_id
            ORDER BY p.mainstream_share {direction}, p.source_id ASC
            """,
            (notebook_id, limit, offset),
        ).fetchall()
        return total, [
            {
                "source_id": row["source_id"],
                "title": row["title"] or "",
                "source_missing": bool(row["source_missing"]),
                "n_objects": int(row["n_objects"]),
                "n_graph_objects": int(row["n_graph_objects"]),
                "top_community_id": row["top_community_id"] or "",
                "top_share": float(row["top_share"]),
                "community_spread": int(row["community_spread"]),
                "mainstream_share": float(row["mainstream_share"]),
            }
            for row in rows
        ]

    @staticmethod
    def source_canonical_rows(db: sqlite3.Connection, notebook_id: str):
        """来源画像的**唯一一次扫描**:``(source_id, canonical_id)`` 的逐对象游标。

        ⚠ **这里刻意没有 `community_members`,也没有 GROUP BY**(codex 第 13 轮 P1 的
        原子发布)。板块与全部分析产物必须在**同一个**写事务里一次出现,所以产物得能在
        板块落库**之前**算出来 —— 而那一刻板块划分只存在于内存里(`community_of_index`)。
        canonical → 板块 这一跳因此交给 `kg_analysis_precompute.SourceBoardCounter`,
        这条查询只负责把「可用对象的 (来源, canonical)」如实吐出来。结果与旧形态
        (``LEFT JOIN community_members … GROUP BY source_id, community_id``)**逐字
        相同**:同一 level 下 `community_members` 是一个划分(唯一的写者
        `replace_communities` 照 Louvain 的 membership 整表重写),而内存里的
        `community_of_index` 就是那份 membership。

        ``COALESCE(c.canonical_id, o.id)`` 与 `community_graph_rows` 的 canonical 口径
        **逐字一致**:没有 cluster_map 行的对象就是它自己的 canonical。社区正是建在那个
        口径的图上,这里换成裸 `c.canonical_id` 会把「未聚类但进了板块」的对象整片判成
        「没进任何板块」—— 报告会凭空多出一批 mainstream_share=0 的「关联稀疏来源」。
        canonical 没进任何板块的那些行同样要吐出来:它们必须计入 `n_objects` 却不能进
        分母(旧形态里那是 LEFT JOIN 的 NULL 组)。

        **游标逐行进、逐行被消化掉**(`SourceBoardCounter.add` 只留两个 int32),所以
        红线「绝不把百万行拉进 Python 聚合」照旧成立。

        ⚠ 与被它取代的那条相比,库内临时结构**少了一份**:旧形态的
        ``GROUP BY o.source_id, cm.community_id`` 走 `USE TEMP B-TREE FOR GROUP BY`,
        而 `_connect()` 设了 `PRAGMA temp_store = MEMORY`(见 database.py;那是
        mention-alias 那条 TEMP FTS 路径的硬需求),所以那棵按**全部输入行**排序的临时
        B 树也落在进程内存里。这条一个 GROUP BY 都没有,还少一次 `community_members`
        的索引探查。本机真实库 nb-b37185f4ae(4.17 万对象)暖态实测 90 ms → 66 ms。

        口径与 T1 的只读聚合一致:对象只算 `USABLE_STATUSES`;`source_id=''` 的对象
        (晋升 / Memory→KG 写路径刻意写空来源)整体排除 —— 它们共享同一个空 source_id,
        算进去等于凭空造一个「空来源」的画像。

        ⚠ **隐藏合成来源(`source_type IN ('memory','knowhow')`)整体排除**,与
        `list_sources` / 文档计数 / 投影 / H3 体检同一条口径(仓库里那条
        `NOT EXISTS (... AND s.source_type IN ('memory','knowhow'))` 的复用)。理由两条,
        后一条更硬:
          1. 排行会被扭曲 —— Memory 与 knowhow 投影天生只连自己那一小片,`mainstream_share`
             恒接近 0,于是「与主体板块最不连通的来源」这张榜的头部会被产品从不显示的
             内部行占满,真正需要用户处理的孤立文档被挤到后面;
          2. **它们的 `title` 是内容,不是元数据** —— knowhow 投影的来源标题就是用户的表名、
             Memory 的就是那条记忆的抬头。产品其余各处都把这两类当隐藏来源,只有这里会把
             它们连标题一起发给 notebook 的任何读者。这与 §3.5 里「knowhow 的自定义类型名
             不进返回载荷、查询层不制造泄漏面」是同一条决定。

        ⚠ **排除判据是「来源存在且类型隐藏」,不是「join 不到就排除」。** `source_id` 没有
        外键,历史清理会留下指向已删来源的**孤儿引用**(见 `_migration_32`),而把孤儿报出来
        (读侧的 `source_missing`)是本视图**有意**的诊断能力。`NOT EXISTS` 这个形态对孤儿
        天然为真 —— 它们照常进画像;换成 `JOIN sources` 或
        `LEFT JOIN ... WHERE s.source_type NOT IN (...)`(NULL 使谓词为 NULL)都会把孤儿
        一起吞掉,把一个诊断信号变成静默丢弃。两类各有守卫钉住。

        为什么排在这里而不是读侧(`kg_source_profile_page`):产物表就不该存这些行。
        `kg_source_profiles` 会被分享拷贝、`merge_dbs` 合并与快照校验带走,留在表里等于
        每个下游各自记得再过滤一次;而且账本 payload 的 `sources`(= `len(profiles)`)
        与读侧的 `total` 会分岔成两个口径。

        ⚠ 排除**不改变**新鲜度契约:账本 seq 仍是 `kg_mutation_seq`,所以本次改动之前
        建好的产物里那些行要等下一次预计算才会消失。三张产物表由 `_migration_34` 引入
        (与本特性同批,尚未进过任何已部署库),所以受影响的只有跑过本分支的开发库;
        任何一次 KG 变更后的 `rebuild_communities` 会整表重写、自然愈合。

        ⚠ **重活**:本 notebook 的 `knowledge_objects` 全扫 + 每行一次索引探查
        (`idx_clusters_member`)。与 `community_graph_rows` 同量级,同样只能待在预计算
        路径上。不要把上面那个毫秒数线性外推到 437GB 的生产库 —— 那里是冷态随机 IO
        支配,见 ports.py 只读聚合段头的说明。
        """
        placeholders = ",".join("?" for _ in USABLE_STATUSES)
        return db.execute(
            f"""
            SELECT o.source_id AS source_id,
                   COALESCE(c.canonical_id, o.id) AS canonical_id
            FROM knowledge_objects o
            LEFT JOIN concept_clusters c
                   ON c.notebook_id = o.notebook_id
                  AND c.member_object_id = o.id
                  AND c.generation = {_PUBLISHED_CLUSTER_GEN}
            WHERE o.notebook_id = ?
              AND o.status IN ({placeholders})
              AND o.source_id != ''
              AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.id = o.source_id
                              AND s.notebook_id = o.notebook_id
                              AND s.source_type IN ('memory','knowhow'))
            """,
            (notebook_id, notebook_id, *USABLE_STATUSES),
        )

    @staticmethod
    def replace_kg_analysis_artifacts(
        db: sqlite3.Connection,
        notebook_id: str,
        kg_mutation_seq: int,
        edges: "Iterable[Tuple[str, str, int]]",
        profiles: "Iterable[Tuple[str, int, int, str, float, int, float]]",
        payloads: Dict[str, dict],
        now: str,
    ) -> None:
        """整批重写一个 notebook 的三张产物表 —— **必须由调用方包在一个写事务里**。

        设计 §3.3:「半份产物比没有产物更危险」。DELETE + INSERT 全部发生在调用方的
        `_write()` 事务内,所以一次预计算对读者是原子的:要么看到整批新产物,要么看到
        整批旧产物,不存在「跨板块边已更新、来源画像还是上一轮」这种自相矛盾的组合。

        账本(`kg_analysis_artifacts`)按 notebook **整表**重写,不是按 kind upsert:
        upsert 会让一次失败的预计算留下「三条统计快照是新的、两张明细的账本行是旧的」
        —— 正是本方法要消灭的那种状态。账本行的 `kg_mutation_seq` 全部取同一个值,
        由调用方传入(= 本轮 rebuild 的目标 seq,与 `community_seq` 同一个数)。

        三张表**统一按 notebook 整表删**(没有 level 维度,理由见本段段头)。

        `edges` / `profiles` 只按**可迭代**消费,分批喂 `executemany`:`edges` 已被
        `CrossCommunityEdgeFold.top_edges` 截到硬上限(20 万行)并整份压在调用方栈帧上,
        再为落库多物化一份完整列表就是在峰值上叠峰值(#340/#342/#347/#351/#352/#354
        那条 OOM 轨道盯的同一时刻)。折叠结果自己已在取完 top-N 之后当场释放,不跨到
        这里(见 `_compute_kg_analysis` 的 `del folder`)。

        账本 payload 走 `allow_nan=False`:NaN/Inf 不是合法 JSON,写进去读回来就是一个
        不可解析的 payload。今天的口径里除不出 NaN(分母为 0 时显式落 0.0),这是**入口
        对称性** —— PostgreSQL 侧 `_json_document` 本来就拒它,两侧都拒才不会出现
        「同一份 payload 一侧写得进、一侧写不进」。
        """
        check_artifact_payloads(payloads)
        db.execute(
            "DELETE FROM kg_community_edges WHERE notebook_id=?", (notebook_id,)
        )
        db.execute(
            "DELETE FROM kg_source_profiles WHERE notebook_id=?", (notebook_id,)
        )
        db.execute(
            "DELETE FROM kg_analysis_artifacts WHERE notebook_id=?", (notebook_id,)
        )
        for batch in batched(
            (
                (notebook_id, src, dst, int(weight))
                for src, dst, weight in edges
            ),
            1000,
        ):
            db.executemany(
                "INSERT INTO kg_community_edges "
                "(notebook_id, src_community_id, dst_community_id, weight) "
                "VALUES (?,?,?,?)",
                batch,
            )
        for batch in batched(
            (
                (notebook_id, source_id, int(n_objects), int(n_graph_objects),
                 top_community_id, float(top_share), int(spread),
                 float(mainstream_share))
                for (source_id, n_objects, n_graph_objects, top_community_id,
                     top_share, spread, mainstream_share) in profiles
            ),
            1000,
        ):
            db.executemany(
                "INSERT INTO kg_source_profiles "
                "(notebook_id, source_id, n_objects, n_graph_objects, "
                " top_community_id, top_share, community_spread, mainstream_share) "
                "VALUES (?,?,?,?,?,?,?,?)",
                batch,
            )
        db.executemany(
            "INSERT INTO kg_analysis_artifacts "
            "(notebook_id, kind, kg_mutation_seq, payload, created_at) "
            "VALUES (?,?,?,?,?)",
            [
                (notebook_id, kind, int(kg_mutation_seq),
                 json.dumps(payloads[kind], ensure_ascii=False, allow_nan=False), now)
                for kind in sorted(payloads)
            ],
        )

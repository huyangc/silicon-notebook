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

import json
import sqlite3
from typing import Dict, List, Optional, Tuple

from app.repositories.sqlite.database import SqliteDatabase


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
        return db.execute(
            "SELECT s.seed AS seed, e.vector AS vector "
            "FROM knowledge_embeddings e "
            "JOIN kg_cluster_scratch s ON s.object_id=e.object_id "
            "  AND s.notebook_id=e.notebook_id AND s.run_id=? "
            "WHERE e.notebook_id=?", (run_id, notebook_id),
        )

    @staticmethod
    def stream_scratch_rows(db: sqlite3.Connection, notebook_id: str, run_id: str):
        return db.execute(
            "SELECT object_id, seed FROM kg_cluster_scratch "
            "WHERE notebook_id=? AND run_id=?", (notebook_id, run_id),
        )

    @staticmethod
    def replace_cluster_rows_streamed(
        db: sqlite3.Connection,
        notebook_id: str,
        object_type: str,
        rows,
    ) -> None:
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
            "FROM concept_clusters WHERE notebook_id=? AND object_type='concept'",
            (notebook_id,),
        ).fetchall()

    @staticmethod
    def cluster_evidence_rows(
        db: sqlite3.Connection, notebook_id: str, run_id: str, seeds,
    ):
        values = list(seeds)
        if not values:
            return []
        ph = ",".join("?" for _ in values)
        return db.execute(
            f"SELECT k.evidence AS evidence FROM knowledge_objects k "
            f"JOIN kg_cluster_scratch s ON s.object_id=k.id "
            f"WHERE s.notebook_id=? AND s.run_id=? AND s.seed IN ({ph})",
            (notebook_id, run_id, *values),
        ).fetchall()

    @staticmethod
    def canonical_relation_seed_rows(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            "SELECT kr.id AS rid, kr.source_id AS src_doc, kr.edge_type AS et, "
            "       COALESCE(cs.canonical_id, kr.source_object_id) AS s, "
            "       COALESCE(ct.canonical_id, kr.target_object_id) AS t "
            "FROM knowledge_relations kr "
            "LEFT JOIN concept_clusters cs ON cs.notebook_id=kr.notebook_id "
            "  AND cs.member_object_id=kr.source_object_id "
            "LEFT JOIN concept_clusters ct ON ct.notebook_id=kr.notebook_id "
            "  AND ct.member_object_id=kr.target_object_id "
            "WHERE kr.notebook_id=? AND kr.review_status!='rejected'", (notebook_id,),
        )

    @staticmethod
    def mention_seed_rows(db: sqlite3.Connection, notebook_id: str):
        clusters = db.execute(
            "SELECT cc.canonical_id AS cid, cc.canonical_name AS cname, ko.source_id AS src "
            "FROM concept_clusters cc JOIN knowledge_objects ko ON ko.id=cc.member_object_id "
            "WHERE cc.notebook_id=? AND cc.object_type='concept'", (notebook_id,),
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

    @staticmethod
    def community_graph_rows(db: sqlite3.Connection, notebook_id: str):
        names = {
            row["canonical_id"]: row["canonical_name"]
            for row in db.execute(
                "SELECT DISTINCT canonical_id, canonical_name FROM concept_clusters "
                "WHERE notebook_id=?", (notebook_id,),
            )
        }
        relations = db.execute(
            "SELECT COALESCE(cs.canonical_id, kr.source_object_id) AS s, "
            "       COALESCE(ct.canonical_id, kr.target_object_id) AS t "
            "FROM knowledge_relations kr "
            "LEFT JOIN concept_clusters cs ON cs.notebook_id=kr.notebook_id "
            "AND cs.member_object_id=kr.source_object_id "
            "LEFT JOIN concept_clusters ct ON ct.notebook_id=kr.notebook_id "
            "AND ct.member_object_id=kr.target_object_id "
            "WHERE kr.notebook_id=?", (notebook_id,),
        )
        return names, relations

    @staticmethod
    def cluster_version_row(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(created_at), '') AS ts "
            "FROM concept_clusters WHERE notebook_id = ?", (notebook_id,),
        ).fetchone()

    @staticmethod
    def cluster_member_rows(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            "SELECT canonical_id, member_object_id FROM concept_clusters "
            "WHERE notebook_id = ?", (notebook_id,),
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
            "FROM concept_clusters WHERE notebook_id=?", (notebook_id,),
        ).fetchone()
        mention = db.execute(
            "SELECT COALESCE(mention_seq,-1) AS ms FROM unified_kg_state "
            "WHERE notebook_id=?", (notebook_id,),
        ).fetchone()
        return rel, obj, chunk, cluster, mention

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
            "SELECT member_object_id, canonical_id FROM concept_clusters WHERE notebook_id=?",
            (notebook_id,),
        ).fetchall()
        return {r["member_object_id"]: r["canonical_id"] for r in rows}

    @staticmethod
    def cluster_fold_rows(
        db: sqlite3.Connection, notebook_id: str, ids: List[str]
    ) -> List[sqlite3.Row]:
        """BOUNDED canonical fold lookup (only the given hit ids) — never the
        full cluster_map, which can be 5M entries at scale."""
        placeholders = ",".join("?" for _ in ids)
        return db.execute(
            f"SELECT member_object_id, canonical_id, canonical_name "
            f"FROM concept_clusters "
            f"WHERE notebook_id=? AND member_object_id IN ({placeholders})",
            [notebook_id] + ids,
        ).fetchall()

    @staticmethod
    def concept_clusters_count(db: sqlite3.Connection, notebook_id: str) -> int:
        return int(db.execute(
            "SELECT COUNT(*) AS c FROM concept_clusters WHERE notebook_id=?",
            (notebook_id,)).fetchone()["c"])

    @staticmethod
    def distinct_cluster_count(db: sqlite3.Connection, notebook_id: str) -> int:
        return int(db.execute(
            "SELECT COUNT(DISTINCT canonical_id) AS c FROM concept_clusters WHERE notebook_id=?",
            (notebook_id,),
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
            "SELECT COUNT(*) AS c FROM communities WHERE notebook_id=? AND level=?",
            (notebook_id, level)).fetchone()

    @staticmethod
    def replace_communities(
        db: sqlite3.Connection,
        notebook_id: str,
        level: int,
        kept: List[Tuple[str, List[str]]],
        names: Dict[str, str],
        deg: Dict[str, float],
        now: str,
    ) -> None:
        """Full rewrite for one level: (cid, sorted members) pairs prepared by
        the caller (min-size policy stays with the orchestration)."""
        db.execute("DELETE FROM communities WHERE notebook_id=? AND level=?", (notebook_id, level))
        db.execute("DELETE FROM community_members WHERE notebook_id=? AND level=?", (notebook_id, level))
        for cid, members in kept:
            db.execute(
                "INSERT INTO communities (id, notebook_id, level, member_ids, size, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (cid, notebook_id, level, json.dumps(members), len(members), now))
            db.executemany(
                "INSERT INTO community_members "
                "(canonical_id, notebook_id, level, community_id, canonical_name, centrality) "
                "VALUES (?,?,?,?,?,?)",
                [(m, notebook_id, level, cid, names.get(m, m), deg.get(m, 0.0)) for m in members])

    @staticmethod
    def set_community_seq(db: sqlite3.Connection, notebook_id: str, seq: int) -> None:
        db.execute("UPDATE unified_kg_state SET community_seq=? WHERE notebook_id=?",
                   (seq, notebook_id))

    @staticmethod
    def community_member_ids(
        db: sqlite3.Connection, notebook_id: str, level: int
    ) -> List[List[str]]:
        rows = db.execute(
            "SELECT member_ids FROM communities WHERE notebook_id=? AND level=? ORDER BY size DESC, id ASC",
            (notebook_id, level)).fetchall()
        return [json.loads(r["member_ids"]) for r in rows]

    @staticmethod
    def community_rows_for_summary(
        db: sqlite3.Connection, notebook_id: str, level: int
    ) -> List[sqlite3.Row]:
        return db.execute(
            "SELECT id, member_ids FROM communities WHERE notebook_id=? AND level=?",
            (notebook_id, level)).fetchall()

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
            "WHERE notebook_id=? AND level=? AND summary!='' ORDER BY size DESC, id ASC",
            (notebook_id, level)).fetchall()
        return [{"member_ids": json.loads(r["member_ids"] or "[]"), "title": r["title"],
                 "summary": r["summary"], "findings": json.loads(r["findings"] or "[]")} for r in rows]

    # -------------------------------------------- community-peer primitives
    # communities.py(对比检索原语)的读接口 —— 自己开只读连接(原实现即用
    # 独立 repo._connect() 短查询;WAL 并发读)。
    def first_base_notebook_id(self, active_nb: str) -> Optional[str]:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT id FROM notebooks WHERE tier='base' AND id != ? ORDER BY updated_at DESC LIMIT 1",
                (active_nb,)).fetchone()
        return row["id"] if row else None

    def resolve_focal(self, notebook_id: str, focal_key: str) -> Optional[str]:
        """focal 归一键 → canonical_id(lower(canonical_name)==key,多簇取成员最多者)。"""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT canonical_id FROM concept_clusters WHERE notebook_id=? AND lower(canonical_name)=? "
                "GROUP BY canonical_id ORDER BY COUNT(*) DESC LIMIT 1", (notebook_id, focal_key)).fetchone()
        return row["canonical_id"] if row else None

    def top_community_for(self, notebook_id: str, canonical_id: str) -> Optional[str]:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT community_id FROM community_members WHERE notebook_id=? AND canonical_id=? "
                "ORDER BY level DESC LIMIT 1", (notebook_id, canonical_id)).fetchone()
        return row["community_id"] if row else None

    def community_member_peers(
        self, notebook_id: str, community_id: str, exclude_canonical_id: str, limit: int
    ) -> List[sqlite3.Row]:
        with self.database.connect() as db:
            return db.execute(
                "SELECT canonical_name, centrality FROM community_members "
                "WHERE notebook_id=? AND community_id=? AND canonical_id!=? "
                "ORDER BY centrality DESC LIMIT ?",
                (notebook_id, community_id, exclude_canonical_id, limit)
            ).fetchall()

    def comention_peers(
        self, notebook_id: str, canonical_id: str, min_bridge: int, limit: int
    ) -> List[Tuple[str, int]]:
        """concept_comentions 两侧按 bridge_claims 降序取对端 canonical_name。"""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT canonical_a, canonical_b, bridge_claims FROM concept_comentions "
                "WHERE notebook_id=? AND (canonical_a=? OR canonical_b=?) AND bridge_claims>=? "
                "ORDER BY bridge_claims DESC LIMIT ?",
                (notebook_id, canonical_id, canonical_id, min_bridge, limit)).fetchall()
            out: List[Tuple[str, int]] = []
            for r in rows:
                other = r["canonical_b"] if r["canonical_a"] == canonical_id else r["canonical_a"]
                nm = db.execute(
                    "SELECT canonical_name FROM concept_clusters WHERE notebook_id=? "
                    "AND canonical_id=? LIMIT 1", (notebook_id, other)).fetchone()
                if nm and nm["canonical_name"]:
                    out.append((nm["canonical_name"], int(r["bridge_claims"])))
            return out

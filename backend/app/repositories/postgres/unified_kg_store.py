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
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from app.core.json_safety import validate_finite_json
from app.repositories.postgres._store_utils import (
    execute_many,
    iso_timestamp,
    json_value,
    jsonb,
    normalize_timestamp,
)
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.search import (
    drop_mention_scan,
    mention_claim_rows,
    mention_scan_matches as search_mention_scan_matches,
    prepare_mention_scan,
)


MOUNT_JOIN = (
    "FROM notebook_bases e JOIN notebooks b ON b.id=e.base_notebook_id "
    "JOIN notebooks a ON a.id=e.notebook_id "
    "WHERE e.notebook_id=%s AND b.id!=e.notebook_id"
)
MOUNT_VALID = (
    " AND b.status!='copying' AND "
    "(b.tier='base' OR b.created_by=a.created_by)"
)
MOUNT_ORDER = (
    " ORDER BY CASE WHEN b.tier='base' THEN 0 ELSE 1 END,b.name COLLATE \"C\""
)


def _json_document(value: Any, *, expected: type, field: str):
    if isinstance(value, str):
        value = json.loads(value)
    if value is None or not isinstance(value, expected):
        raise ValueError(f"{field} must be a {expected.__name__}")
    validate_finite_json(value, field=field)
    return value


class UnifiedKgStore:
    def __init__(self, database: PostgresDatabase, now=None) -> None:
        self.database = database
        self.now = now

    # --------------------------------------------- lifecycle rebuild streams
    @staticmethod
    def seed_payload_rows(
        db: Any, notebook_id: str, object_type: str,
    ):
        return db.execute(
            "SELECT payload::text AS payload FROM knowledge_objects "
            "WHERE notebook_id=%s AND object_type=%s AND status!='deprecated' "
            "ORDER BY ordinal", (notebook_id, object_type),
        )

    @staticmethod
    def stream_seed_rows(
        db: Any, notebook_id: str, object_type: str,
    ):
        return db.execute(
            "SELECT id,payload::text AS payload FROM knowledge_objects "
            "WHERE notebook_id=%s AND object_type=%s AND status!='deprecated' "
            "ORDER BY ordinal", (notebook_id, object_type),
        )

    @staticmethod
    def scratch_vector_rows(db: Any, notebook_id: str, run_id: str):
        return db.execute(
            "SELECT s.seed AS seed, e.vector AS vector "
            "FROM knowledge_embeddings e "
            "JOIN kg_cluster_scratch s ON s.object_id=e.object_id "
            "  AND s.notebook_id=e.notebook_id AND s.run_id=%s "
            "WHERE e.notebook_id=%s", (run_id, notebook_id),
        )

    @staticmethod
    def stream_scratch_rows(db: Any, notebook_id: str, run_id: str):
        return db.execute(
            "SELECT object_id, seed FROM kg_cluster_scratch "
            "WHERE notebook_id=%s AND run_id=%s", (notebook_id, run_id),
        )

    @staticmethod
    def replace_cluster_rows_streamed(
        db: Any,
        notebook_id: str,
        object_type: str,
        rows,
    ) -> None:
        db.execute(
            "DELETE FROM concept_clusters WHERE notebook_id=%s AND object_type=%s",
            (notebook_id, object_type),
        )
        buf: list[tuple] = []
        for row in rows:
            buf.append(row)
            if len(buf) >= 1000:
                execute_many(
                    db,
                    "INSERT INTO concept_clusters "
                    "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,"
                    "canonical_description,canonical_desc_sig,created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    [(*item[:-1], normalize_timestamp(item[-1])) for item in buf],
                )
                buf.clear()
        if buf:
            execute_many(
                db,
                "INSERT INTO concept_clusters "
                "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,"
                "canonical_description,canonical_desc_sig,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [(*item[:-1], normalize_timestamp(item[-1])) for item in buf],
            )

    @staticmethod
    def cluster_description_rows(db: Any, notebook_id: str):
        return db.execute(
            "SELECT DISTINCT canonical_id, canonical_description, canonical_desc_sig "
            "FROM concept_clusters WHERE notebook_id=%s AND object_type='concept'",
            (notebook_id,),
        ).fetchall()

    @staticmethod
    def cluster_evidence_rows(
        db: Any, notebook_id: str, run_id: str, seeds,
    ):
        values = list(seeds)
        if not values:
            return []
        ph = ",".join("%s" for _ in values)
        return db.execute(
            f"SELECT k.evidence::text AS evidence FROM knowledge_objects k "
            f"JOIN kg_cluster_scratch s ON s.object_id=k.id "
            f"WHERE s.notebook_id=%s AND s.run_id=%s AND s.seed IN ({ph})",
            (notebook_id, run_id, *values),
        ).fetchall()

    @staticmethod
    def canonical_relation_seed_rows(db: Any, notebook_id: str):
        return db.execute(
            "SELECT kr.id AS rid, kr.source_id AS src_doc, kr.edge_type AS et, "
            "       COALESCE(cs.canonical_id, kr.source_object_id) AS s, "
            "       COALESCE(ct.canonical_id, kr.target_object_id) AS t "
            "FROM knowledge_relations kr "
            "LEFT JOIN concept_clusters cs ON cs.notebook_id=kr.notebook_id "
            "  AND cs.member_object_id=kr.source_object_id "
            "LEFT JOIN concept_clusters ct ON ct.notebook_id=kr.notebook_id "
            "  AND ct.member_object_id=kr.target_object_id "
            "WHERE kr.notebook_id=%s AND kr.review_status!='rejected'", (notebook_id,),
        )

    @staticmethod
    def mention_seed_rows(db: Any, notebook_id: str):
        clusters = db.execute(
            "SELECT cc.canonical_id AS cid, cc.canonical_name AS cname, ko.source_id AS src "
            "FROM concept_clusters cc JOIN knowledge_objects ko ON ko.id=cc.member_object_id "
            "WHERE cc.notebook_id=%s AND cc.object_type='concept'", (notebook_id,),
        ).fetchall()
        claims = mention_claim_rows(db, notebook_id)
        return clusters, claims

    @staticmethod
    def claim_name_rows(db: Any, rows) -> None:
        prepare_mention_scan(db, rows)

    @staticmethod
    def mention_scan_matches(db: Any, match_expr: str):
        alias = match_expr[1:-1].replace('""', '"') if (
            len(match_expr) >= 2 and match_expr.startswith('"') and match_expr.endswith('"')
        ) else match_expr
        return search_mention_scan_matches(db, alias)

    @contextmanager
    def mention_alias_candidate_batches(
        self, claims: Sequence[tuple[str, str]], aliases: Sequence[str]
    ) -> Iterator[Iterator[tuple[str, Iterator[tuple[str, str]]]]]:
        """Yield one alias cursor at a time while owning the TEMP-table lifetime."""
        with self.database.connect() as scan_db:
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
                        rows = self.mention_scan_matches(scan_db, alias)
                        yield alias, (
                            claims[int(row["rowid"]) - 1]
                            for row in rows
                        )

                yield batches()
            finally:
                drop_mention_scan(scan_db)

    @staticmethod
    def community_graph_rows(db: Any, notebook_id: str):
        names = {
            row["canonical_id"]: row["canonical_name"]
            for row in db.execute(
                "SELECT DISTINCT canonical_id, canonical_name FROM concept_clusters "
                "WHERE notebook_id=%s", (notebook_id,),
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
            "WHERE kr.notebook_id=%s", (notebook_id,),
        )
        return names, relations

    @staticmethod
    def cluster_version_row(db: Any, notebook_id: str):
        row = db.execute(
            "SELECT COUNT(*) AS c,MAX(created_at) AS ts "
            "FROM concept_clusters WHERE notebook_id = %s", (notebook_id,),
        ).fetchone()
        return {**dict(row), "ts": iso_timestamp(row["ts"])}

    @staticmethod
    def cluster_member_rows(db: Any, notebook_id: str):
        return db.execute(
            "SELECT canonical_id, member_object_id FROM concept_clusters "
            "WHERE notebook_id = %s", (notebook_id,),
        ).fetchall()

    @staticmethod
    def ppr_version_rows(db: Any, notebook_id: str):
        rel = db.execute(
            "SELECT COUNT(*) AS c,MAX(created_at) AS ts "
            "FROM knowledge_relations WHERE notebook_id=%s "
            "AND review_status!='rejected'", (notebook_id,),
        ).fetchone()
        obj = db.execute(
            "SELECT COUNT(*) AS c,MAX(updated_at) AS ts "
            "FROM knowledge_objects WHERE notebook_id=%s", (notebook_id,),
        ).fetchone()
        chunk = db.execute(
            "SELECT COUNT(*) AS c,MAX(created_at) AS ts "
            "FROM chunks WHERE notebook_id=%s", (notebook_id,),
        ).fetchone()
        cluster = db.execute(
            "SELECT COUNT(*) AS c,MAX(created_at) AS ts "
            "FROM concept_clusters WHERE notebook_id=%s", (notebook_id,),
        ).fetchone()
        mention = db.execute(
            "SELECT COALESCE(mention_seq,-1) AS ms FROM unified_kg_state "
            "WHERE notebook_id=%s", (notebook_id,),
        ).fetchone()
        versions = []
        for row in (rel, obj, chunk, cluster):
            versions.append({**dict(row), "ts": iso_timestamp(row["ts"])})
        return (*versions, mention)

    @staticmethod
    def graph_seq_row(db: Any, notebook_id: str) -> "tuple[int, int, int]":
        """O(1) single-row monotonic seq triple for the graph/PPR version keys:
        (kg_mutation_seq, cluster_mutation_seq, mention_seq). Replaces the
        per-request COUNT/MAX aggregate scans (ppr_version_rows /
        graph_version_rows / cluster_version_row) that ran on EVERY graph/PPR
        retrieval, even on a cache HIT. Coverage of every production write path
        (adversarially verified):
          - kg_mutation_seq: object writes (create/status/payload/delete via
            store_kg/update_knowledge/merge/promotion/conflict/relink/delete_source),
            edge-review flips (set_edge_review), and chunk writes
            (build_chunks_for_source) all bump it.
          - cluster_mutation_seq: concept_clusters writes (write_clusters /
            append_clusters / rebuild — which DELIBERATELY keeps kg_mutation_seq
            stable) advance this instead.
          - mention_seq: the co-mention bridge rebuild.
        A monotonic counter is STRICTLY more sensitive than (COUNT, MAX ts): it
        also catches a same-second in-place edit that a 1s-resolution timestamp
        would miss. Absent row -> (0, 0, -1), matching version_signal's sentinel.

        NOTE: the seq RESETS on delete_notebook_kg (which drops the state row),
        so a delete+reingest of a participant can re-climb to a colliding triple
        — retrieval_snapshot_cache.invalidate_kg therefore evicts ALL :ppr_graph
        and :fed_rxgraph entries (not just self) as the belt-and-braces."""
        row = db.execute(
            "SELECT COALESCE(kg_mutation_seq,0) AS ks, COALESCE(cluster_mutation_seq,0) AS cs, "
            "COALESCE(mention_seq,-1) AS ms FROM unified_kg_state WHERE notebook_id=%s",
            (notebook_id,),
        ).fetchone()
        if row is None:
            return (0, 0, -1)
        return (int(row["ks"]), int(row["cs"]), int(row["ms"]))

    @staticmethod
    def mention_rows(db: Any, notebook_id: str):
        return db.execute(
            "SELECT claim_object_id, concept_canonical_id FROM mention_edges "
            "WHERE notebook_id=%s", (notebook_id,),
        ).fetchall()

    # -------------------------------------------------------- unified state
    @staticmethod
    def state_row(db: Any, notebook_id: str) -> "dict | None":
        row = db.execute(
            "SELECT * FROM unified_kg_state WHERE notebook_id=%s", (notebook_id,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["last_rebuild_at"] = iso_timestamp(result["last_rebuild_at"])
        result["updated_at"] = iso_timestamp(result["updated_at"])
        return result

    @staticmethod
    def mark_dirty(db: Any, notebook_id: str, now: str) -> None:
        """Bump the monotonic mutation counter on every KG write. Reference the
        table's own current value (+1), NOT excluded, so an existing row
        increments rather than resets to the inserted literal (1). First
        mutation -> seq 1."""
        db.execute(
            """
            INSERT INTO unified_kg_state (notebook_id, dirty, kg_mutation_seq, updated_at)
            VALUES (%s, 1, 1, %s)
            ON CONFLICT(notebook_id) DO UPDATE SET
              dirty=1,
              kg_mutation_seq=unified_kg_state.kg_mutation_seq+1,
              updated_at=excluded.updated_at
            """,
            (notebook_id, normalize_timestamp(now)),
        )

    @staticmethod
    def bump_cluster_seq(db: Any, notebook_id: str, now: str) -> None:
        """concept_clusters 写路径的单调计数器 bump——在调用方已持有的写事务 db 内
        执行(写簇+bump 同 commit,原子)。kg_mutation_seq 不在此处动:rebuild 刻意
        保持它稳定(幂等),clusters 的变化信号独立成列。"""
        db.execute(
            """
            INSERT INTO unified_kg_state (notebook_id, dirty, cluster_mutation_seq, updated_at)
            VALUES (%s, 0, 1, %s)
            ON CONFLICT(notebook_id) DO UPDATE SET
              cluster_mutation_seq=unified_kg_state.cluster_mutation_seq+1,
              updated_at=excluded.updated_at
            """,
            (notebook_id, normalize_timestamp(now)),
        )

    @staticmethod
    def cluster_input_facts(
        db: Any, notebook_id: str, *, exclude_emb_count: bool = False
    ) -> Tuple[int, int, int, int]:
        """The data-derived components of _cluster_input_version: (seq, obj_c,
        dec_c, emb_c). The decided-pair COUNT WHERE mirrors decided_pairs()
        EXACTLY (pending excluded so rebuild's own pending-refresh doesn't move
        the version). emb_c stays 0 when excluded (the checkpoint version
        namespace — see _cluster_input_version's docstring)."""
        st = db.execute(
            "SELECT kg_mutation_seq FROM unified_kg_state WHERE notebook_id=%s",
            (notebook_id,)).fetchone()
        seq = int(st["kg_mutation_seq"]) if st else 0
        obj_c = db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_objects "
            "WHERE notebook_id=%s AND status!='deprecated'",
            (notebook_id,)).fetchone()["c"]
        dec_c = db.execute(
            "SELECT COUNT(*) AS c FROM concept_merge_candidates "
            "WHERE notebook_id=%s AND status IN ('confirmed','rejected')",
            (notebook_id,)).fetchone()["c"]
        emb_c = 0
        if not exclude_emb_count:
            emb_c = db.execute(
                "SELECT COUNT(*) AS c FROM knowledge_embeddings WHERE notebook_id=%s",
                (notebook_id,)).fetchone()["c"]
        return seq, int(obj_c), int(dec_c), int(emb_c)

    # ------------------------------------------------------------ clusters
    @staticmethod
    def cluster_map_rows(db: Any, notebook_id: str) -> Dict[str, str]:
        rows = db.execute(
            "SELECT member_object_id, canonical_id FROM concept_clusters WHERE notebook_id=%s",
            (notebook_id,),
        ).fetchall()
        return {r["member_object_id"]: r["canonical_id"] for r in rows}

    @staticmethod
    def cluster_fold_rows(
        db: Any, notebook_id: str, ids: List[str]
    ) -> List[dict]:
        """BOUNDED canonical fold lookup (only the given hit ids) — never the
        full cluster_map, which can be 5M entries at scale."""
        if not ids:
            return []
        placeholders = ",".join("%s" for _ in ids)
        return db.execute(
            f"SELECT member_object_id, canonical_id, canonical_name "
            f"FROM concept_clusters "
            f"WHERE notebook_id=%s AND member_object_id IN ({placeholders})",
            [notebook_id] + ids,
        ).fetchall()

    @staticmethod
    def concept_clusters_count(db: Any, notebook_id: str) -> int:
        return int(db.execute(
            "SELECT COUNT(*) AS c FROM concept_clusters WHERE notebook_id=%s",
            (notebook_id,)).fetchone()["c"])

    @staticmethod
    def distinct_cluster_count(db: Any, notebook_id: str) -> int:
        return int(db.execute(
            "SELECT COUNT(DISTINCT canonical_id) AS c FROM concept_clusters WHERE notebook_id=%s",
            (notebook_id,),
        ).fetchone()["c"])

    # ------------------------------------------------------------- scratch
    @staticmethod
    def clear_scratch_run(
        db: Any, notebook_id: str, run_id: str
    ) -> None:
        db.execute("DELETE FROM kg_cluster_scratch WHERE notebook_id=%s AND run_id=%s",
                   (notebook_id, run_id))

    @staticmethod
    def insert_scratch_rows(db: Any, rows: List[tuple]) -> None:
        execute_many(
            db,
            "INSERT INTO kg_cluster_scratch (notebook_id, run_id, object_id, seed) VALUES (%s,%s,%s,%s)",
            rows,
        )

    # -------------------------------------------------- rebuild checkpoints
    # kg_rebuild_checkpoint 行级读写(master v10 可续跑轨道)。自己开连接/事务:
    # 每个 helper 历来就是独立的小事务(rebuild 的 LLM 阶段从 worker 线程写入),
    # 走 database.write() 即同一把全局写锁。
    def checkpoint_gc(self, notebook_id: str, input_version: str) -> None:
        """删掉本 notebook 里 input_version 不等于当前值的所有 checkpoint 行(表有界)。
        rebuild 开头调一次:数据/算法版本一变,旧决策自动失效。"""
        with self.database.write() as db:
            db.execute(
                "DELETE FROM kg_rebuild_checkpoint WHERE notebook_id=%s AND input_version!=%s",
                (notebook_id, input_version))

    def checkpoint_clear(self, notebook_id: str) -> None:
        """删掉本 notebook 的全部 checkpoint(所有版本/阶段)。--fresh 用,强制两个 LLM 阶段重跑。"""
        with self.database.write() as db:
            db.execute("DELETE FROM kg_rebuild_checkpoint WHERE notebook_id=%s", (notebook_id,))

    def checkpoint_load(
        self, notebook_id: str, input_version: str, stage: str
    ) -> Dict[str, dict]:
        """载入某阶段在当前 input_version 下已完成的 item:{item_key: payload_dict}。"""
        with self.database.connect() as db:
            return {
                r["item_key"]: json_value(r["payload"], {})
                for r in db.execute(
                    "SELECT item_key, payload FROM kg_rebuild_checkpoint "
                    "WHERE notebook_id=%s AND input_version=%s AND stage=%s",
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
            execute_many(
                db,
                "INSERT INTO kg_rebuild_checkpoint "
                "(notebook_id, input_version, stage, item_key, payload, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT "
                "(notebook_id,input_version,stage,item_key) DO UPDATE SET "
                "payload=EXCLUDED.payload,created_at=EXCLUDED.created_at",
                [
                    (
                        notebook_id,
                        input_version,
                        stage,
                        key,
                        jsonb(_json_document(value, expected=dict, field="checkpoint payload")),
                        normalize_timestamp(now),
                    )
                    for key, value in rows
                ],
            )

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
        db: Any,
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
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=%s AND status!='deprecated'",
            (notebook_id,),
        ).fetchone()["c"]
        relation_count = db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_relations WHERE notebook_id=%s",
            (notebook_id,),
        ).fetchone()["c"]
        db.execute(
            """
            INSERT INTO unified_kg_state
            (notebook_id, dirty, cluster_input_version, last_rebuild_at, object_count, relation_count, cluster_count, updated_at)
            VALUES (%s, 0, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(notebook_id) DO UPDATE SET
              dirty=0,
              cluster_input_version=excluded.cluster_input_version,
              last_rebuild_at=excluded.last_rebuild_at,
              object_count=excluded.object_count,
              relation_count=excluded.relation_count,
              cluster_count=excluded.cluster_count,
              updated_at=excluded.updated_at
            """,
            (
                notebook_id,
                cluster_input_version,
                normalize_timestamp(now),
                object_count,
                relation_count,
                cluster_count,
                normalize_timestamp(now),
            ),
        )

    # ---------------------------------------------------- canonical relations
    @staticmethod
    def canonical_relations_count(db: Any, notebook_id: str) -> int:
        return int(db.execute(
            "SELECT COUNT(*) AS c FROM canonical_relations WHERE notebook_id=%s",
            (notebook_id,)).fetchone()["c"])

    @staticmethod
    def edge_support_rows(db: Any, notebook_id: str):
        return db.execute(
            "SELECT canonical_src, edge_type, canonical_tgt, support_count, source_count "
            "FROM canonical_relations WHERE notebook_id=%s", (notebook_id,))

    @staticmethod
    def replace_canonical_relations(
        db: Any, notebook_id: str, rows: List[tuple], seq: int
    ) -> None:
        db.execute("DELETE FROM canonical_relations WHERE notebook_id=%s", (notebook_id,))
        for i in range(0, len(rows), 1000):
            execute_many(
                db,
                "INSERT INTO canonical_relations "
                "(notebook_id, canonical_src, edge_type, canonical_tgt, "
                " support_count, source_count, sample_relation_ids, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                [
                    (
                        *row[:6],
                        jsonb(_json_document(
                            row[6], expected=list, field="canonical relation samples"
                        )),
                        normalize_timestamp(row[7]),
                    )
                    for row in rows[i:i + 1000]
                ],
            )
        db.execute(
            "UPDATE unified_kg_state SET canonical_rel_seq=%s WHERE notebook_id=%s",
            (seq, notebook_id))

    # ------------------------------------------------------- mention bridge
    @staticmethod
    def clear_mention_bridge(db: Any, notebook_id: str) -> None:
        db.execute("DELETE FROM mention_edges WHERE notebook_id=%s", (notebook_id,))
        db.execute("DELETE FROM concept_comentions WHERE notebook_id=%s", (notebook_id,))

    @staticmethod
    def mention_edges_count(db: Any, notebook_id: str) -> int:
        return int(db.execute(
            "SELECT COUNT(*) AS c FROM mention_edges WHERE notebook_id=%s",
            (notebook_id,)).fetchone()["c"])

    @staticmethod
    def replace_mention_bridge(
        db: Any,
        notebook_id: str,
        edges: List[tuple],
        comention_rows: List[tuple],
        seq: int,
    ) -> None:
        db.execute("DELETE FROM mention_edges WHERE notebook_id=%s", (notebook_id,))
        db.execute("DELETE FROM concept_comentions WHERE notebook_id=%s", (notebook_id,))
        for i in range(0, len(edges), 1000):
            execute_many(
                db,
                "INSERT INTO mention_edges "
                "(notebook_id, claim_object_id, concept_canonical_id, matched_alias) "
                "VALUES (%s,%s,%s,%s)", edges[i:i + 1000])
        for i in range(0, len(comention_rows), 1000):
            execute_many(
                db,
                "INSERT INTO concept_comentions "
                "(notebook_id, canonical_a, canonical_b, bridge_claims) "
                "VALUES (%s,%s,%s,%s)", comention_rows[i:i + 1000])
        db.execute(
            "UPDATE unified_kg_state SET mention_seq=%s WHERE notebook_id=%s",
            (seq, notebook_id))

    # ---------------------------------------------------------- communities
    @staticmethod
    def communities_count(
        db: Any, notebook_id: str, level: int
    ) -> "dict | None":
        return db.execute(
            "SELECT COUNT(*) AS c FROM communities WHERE notebook_id=%s AND level=%s",
            (notebook_id, level)).fetchone()

    @staticmethod
    def replace_communities(
        db: Any,
        notebook_id: str,
        level: int,
        kept: List[Tuple[str, List[str]]],
        names: Dict[str, str],
        deg: Dict[str, float],
        now: str,
    ) -> None:
        """Full rewrite for one level: (cid, sorted members) pairs prepared by
        the caller (min-size policy stays with the orchestration)."""
        db.execute("DELETE FROM communities WHERE notebook_id=%s AND level=%s", (notebook_id, level))
        db.execute("DELETE FROM community_members WHERE notebook_id=%s AND level=%s", (notebook_id, level))
        for cid, members in kept:
            db.execute(
                "INSERT INTO communities (id, notebook_id, level, member_ids, size, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    cid,
                    notebook_id,
                    level,
                    jsonb(_json_document(members, expected=list, field="community members")),
                    len(members),
                    normalize_timestamp(now),
                ))
            execute_many(
                db,
                "INSERT INTO community_members "
                "(canonical_id, notebook_id, level, community_id, canonical_name, centrality) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                [(m, notebook_id, level, cid, names.get(m, m), deg.get(m, 0.0)) for m in members])

    @staticmethod
    def set_community_seq(db: Any, notebook_id: str, seq: int) -> None:
        db.execute("UPDATE unified_kg_state SET community_seq=%s WHERE notebook_id=%s",
                   (seq, notebook_id))

    @staticmethod
    def community_member_ids(
        db: Any, notebook_id: str, level: int
    ) -> List[List[str]]:
        rows = db.execute(
            "SELECT member_ids FROM communities WHERE notebook_id=%s AND level=%s ORDER BY size DESC, id ASC",
            (notebook_id, level)).fetchall()
        return [json_value(r["member_ids"], []) for r in rows]

    @staticmethod
    def community_rows_for_summary(
        db: Any, notebook_id: str, level: int
    ) -> List[dict]:
        rows = db.execute(
            "SELECT id,member_ids::text AS member_ids FROM communities "
            "WHERE notebook_id=%s AND level=%s",
            (notebook_id, level)).fetchall()
        return rows

    @staticmethod
    def set_community_summary(
        db: Any,
        community_id: str,
        title: str,
        summary: str,
        findings_json: str,
    ) -> None:
        db.execute(
            "UPDATE communities SET title=%s,summary=%s,findings=%s WHERE id=%s",
            (
                title,
                summary,
                jsonb(_json_document(
                    findings_json, expected=list, field="community findings"
                )),
                community_id,
            ),
        )

    @staticmethod
    def community_reports(
        db: Any, notebook_id: str, level: int
    ) -> List[dict]:
        rows = db.execute(
            "SELECT member_ids, title, summary, findings FROM communities "
            "WHERE notebook_id=%s AND level=%s AND summary!='' ORDER BY size DESC, id ASC",
            (notebook_id, level)).fetchall()
        return [{"member_ids": json_value(r["member_ids"], []), "title": r["title"],
                 "summary": r["summary"], "findings": json_value(r["findings"], [])} for r in rows]

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
                "SELECT canonical_id FROM concept_clusters WHERE notebook_id=%s AND lower(canonical_name)=%s "
                "GROUP BY canonical_id ORDER BY COUNT(*) DESC LIMIT 1", (notebook_id, focal_key)).fetchone()
        return row["canonical_id"] if row else None

    def top_community_for(self, notebook_id: str, canonical_id: str) -> Optional[str]:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT community_id FROM community_members WHERE notebook_id=%s AND canonical_id=%s "
                "ORDER BY level DESC LIMIT 1", (notebook_id, canonical_id)).fetchone()
        return row["community_id"] if row else None

    def community_member_peers(
        self, notebook_id: str, community_id: str, exclude_canonical_id: str, limit: int
    ) -> List[dict]:
        with self.database.connect() as db:
            return db.execute(
                "SELECT canonical_name, centrality FROM community_members "
                "WHERE notebook_id=%s AND community_id=%s AND canonical_id!=%s "
                "ORDER BY centrality DESC LIMIT %s",
                (notebook_id, community_id, exclude_canonical_id, limit)
            ).fetchall()

    def comention_peers(
        self, notebook_id: str, canonical_id: str, min_bridge: int, limit: int
    ) -> List[Tuple[str, int]]:
        """concept_comentions 两侧按 bridge_claims 降序取对端 canonical_name。"""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT canonical_a, canonical_b, bridge_claims FROM concept_comentions "
                "WHERE notebook_id=%s AND (canonical_a=%s OR canonical_b=%s) AND bridge_claims>=%s "
                "ORDER BY bridge_claims DESC LIMIT %s",
                (notebook_id, canonical_id, canonical_id, min_bridge, limit)).fetchall()
            out: List[Tuple[str, int]] = []
            for r in rows:
                other = r["canonical_b"] if r["canonical_a"] == canonical_id else r["canonical_a"]
                nm = db.execute(
                    "SELECT canonical_name FROM concept_clusters WHERE notebook_id=%s "
                    "AND canonical_id=%s LIMIT 1", (notebook_id, other)).fetchone()
                if nm and nm["canonical_name"]:
                    out.append((nm["canonical_name"], int(r["bridge_claims"])))
            return out

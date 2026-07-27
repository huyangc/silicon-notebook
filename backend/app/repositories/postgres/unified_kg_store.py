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
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from psycopg import IsolationLevel

from app.core.json_safety import validate_finite_json
from app.repositories.kg_analysis_payloads import (
    artifact_ledger_rows as _artifact_ledger_rows,
    clamp as _clamp,
    cluster_histogram_payload as _cluster_histogram_payload,
    community_overview_payload as _community_overview_payload,
    largest_clusters_payload as _largest_clusters_payload,
    relation_provenance_payload as _relation_provenance_payload,
)
from app.repositories.postgres._store_utils import (
    execute_many,
    iso_timestamp,
    json_value,
    jsonb,
    normalize_timestamp,
)
from app.repositories.postgres.cluster_lock import lock_cluster_artifact_type
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.mount_sql import MOUNT_JOIN, MOUNT_ORDER, MOUNT_VALID
from app.repositories.postgres.search import (
    drop_mention_scan,
    mention_claim_rows,
    mention_scan_matches as search_mention_scan_matches,
    prepare_mention_scan,
)
from app.services.kg_analysis_precompute import batched, check_artifact_payloads
from app.services.knowledge_contracts import (
    COMMUNITY_OVERVIEW_MAX,
    COMMUNITY_TOP_MEMBERS_MAX,
    KG_COMMUNITY_EDGES_MAX,
    KG_SOURCE_PAGE_MAX,
    LARGEST_CLUSTERS_MAX,
    RELATION_EXCLUSION_BUCKETS as _RELATION_EXCLUSION_BUCKETS,
    RELATION_PROVENANCE_BUCKETS as _RELATION_PROVENANCE_BUCKETS,
    USABLE_STATUSES,
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
        # Pass A inserts scratch rows from stream_seed_rows(), whose historical
        # contract is knowledge_objects.ordinal order. PostgreSQL has no rowid
        # on this scratch table, so recover that preserved order via the source
        # object instead of inventing object-id lexical order. Float32 mean
        # accumulation in Pass B is order-sensitive.
        return db.execute(
            "SELECT s.seed AS seed, e.vector AS vector "
            "FROM knowledge_embeddings e "
            "JOIN kg_cluster_scratch s ON s.object_id=e.object_id "
            "  AND s.notebook_id=e.notebook_id AND s.run_id=%s "
            "JOIN knowledge_objects k ON k.id=s.object_id "
            "  AND k.notebook_id=s.notebook_id "
            "WHERE e.notebook_id=%s ORDER BY k.ordinal", (run_id, notebook_id),
        )

    @staticmethod
    def stream_scratch_rows(db: Any, notebook_id: str, run_id: str):
        # Keep the same upstream insertion/ordinal contract as the vector reader.
        return db.execute(
            "SELECT s.object_id,s.seed FROM kg_cluster_scratch s "
            "JOIN knowledge_objects k ON k.id=s.object_id "
            "  AND k.notebook_id=s.notebook_id "
            "WHERE s.notebook_id=%s AND s.run_id=%s ORDER BY k.ordinal",
            (notebook_id, run_id),
        )

    @staticmethod
    def replace_cluster_rows_streamed(
        db: Any,
        notebook_id: str,
        object_type: str,
        rows,
    ) -> None:
        lock_cluster_artifact_type(db, notebook_id, object_type)
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
            "       COALESCE(ct.canonical_id, kr.target_object_id) AS t, "
            "       so.object_type AS st, tp.object_type AS tt "
            "FROM knowledge_relations kr "
            "JOIN knowledge_objects so ON so.id=kr.source_object_id "
            "JOIN knowledge_objects tp ON tp.id=kr.target_object_id "
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
            "WHERE kr.notebook_id=%s AND kr.review_status!='rejected' "
            "ORDER BY kr.id COLLATE \"C\"", (notebook_id,),
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
            "WHERE notebook_id = %s "
            "ORDER BY canonical_id COLLATE \"C\", member_object_id COLLATE \"C\"",
            (notebook_id,),
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

    @staticmethod
    def clear_canonical_scratch_run(
        db: Any, notebook_id: str, run_id: str
    ) -> None:
        db.execute(
            "DELETE FROM kg_canonical_scratch WHERE notebook_id=%s AND run_id=%s",
            (notebook_id, run_id),
        )

    @staticmethod
    def insert_canonical_scratch_rows(db: Any, rows: List[tuple]) -> None:
        execute_many(
            db,
            "INSERT INTO kg_canonical_scratch "
            "(notebook_id,run_id,seed,canonical_id,canonical_name,"
            "canonical_description,canonical_desc_sig) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            rows,
        )

    @staticmethod
    def swap_cluster_map_from_scratch(
        db: Any,
        notebook_id: str,
        object_type: str,
        run_id: str,
        created_at: str,
    ) -> None:
        """Atomically replace one cluster artifact from its two scratch maps."""
        lock_cluster_artifact_type(db, notebook_id, object_type)
        db.execute(
            "DELETE FROM concept_clusters WHERE notebook_id=%s AND object_type=%s",
            (notebook_id, object_type),
        )
        db.execute(
            "INSERT INTO concept_clusters "
            "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,"
            "canonical_description,canonical_desc_sig,created_at) "
            "SELECT 'cc-' || md5(random()::text || clock_timestamp()::text || s.object_id), "
            "s.notebook_id,c.canonical_id,s.object_id,c.canonical_name,%s,"
            "c.canonical_description,c.canonical_desc_sig,%s::timestamptz "
            "FROM kg_cluster_scratch s "
            "JOIN kg_canonical_scratch c "
            "ON c.notebook_id=s.notebook_id AND c.run_id=s.run_id AND c.seed=s.seed "
            "JOIN knowledge_objects k ON k.id=s.object_id "
            "WHERE s.notebook_id=%s AND s.run_id=%s ORDER BY k.ordinal",
            (object_type, created_at, notebook_id, run_id),
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
                "GROUP BY canonical_id ORDER BY COUNT(*) DESC, "
                "canonical_id COLLATE \"C\" ASC LIMIT 1",
                (notebook_id, focal_key)).fetchone()
        return row["canonical_id"] if row else None

    def top_community_for(self, notebook_id: str, canonical_id: str) -> Optional[str]:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT community_id FROM community_members WHERE notebook_id=%s AND canonical_id=%s "
                "ORDER BY level DESC, community_id COLLATE \"C\" ASC LIMIT 1",
                (notebook_id, canonical_id)).fetchone()
        return row["community_id"] if row else None

    def community_member_peers(
        self, notebook_id: str, community_id: str, exclude_canonical_id: str, limit: int
    ) -> List[dict]:
        with self.database.connect() as db:
            return db.execute(
                "SELECT canonical_name, centrality FROM community_members "
                "WHERE notebook_id=%s AND community_id=%s AND canonical_id!=%s "
                "ORDER BY centrality DESC, canonical_id COLLATE \"C\" ASC LIMIT %s",
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
                "ORDER BY bridge_claims DESC, "
                "CASE WHEN canonical_a=%s THEN canonical_b ELSE canonical_a END "
                "COLLATE \"C\" ASC LIMIT %s",
                (notebook_id, canonical_id, canonical_id, min_bridge, canonical_id, limit)).fetchall()
            out: List[Tuple[str, int]] = []
            for r in rows:
                other = r["canonical_b"] if r["canonical_a"] == canonical_id else r["canonical_a"]
                nm = db.execute(
                    "SELECT canonical_name FROM concept_clusters WHERE notebook_id=%s "
                    "AND canonical_id=%s LIMIT 1", (notebook_id, other)).fetchone()
                if nm and nm["canonical_name"]:
                    out.append((nm["canonical_name"], int(r["bridge_claims"])))
            return out

    # ------------------------------------------ KG 质量分析(只读聚合,T1)
    # 与 sqlite/unified_kg_store.py 的同名段落**语义等价**(parity 红线):同样的口径、
    # 同样的分桶分组、同样的载荷形状——形状由 app.repositories.kg_analysis_payloads 的
    # 中性装配函数保证,两侧只是各写各的方言 SQL(%s 占位符;evidence 是 jsonb,用
    # -> / ->> 取 basis,不需要 SQLite 那边的 json_valid 兜底,因为列本身就是 jsonb
    # NOT NULL;排序用 COLLATE "C" 对齐 SQLite 的字节序)。
    #
    # ⚠ **形态刻意分岔的一处**:`community_overview` 在这里用**一条窗口函数查询**
    # (ROW_NUMBER() OVER (PARTITION BY community_id),限制在已选出的 ≤200 个板块上)
    # 取全部代表概念,而 SQLite 侧保持逐板块有界 top-K。理由见该方法的 docstring:
    # 两侧的瓶颈不是同一个东西。parity 要求的是**语义等价 + 两侧都有守卫**,不是 SQL
    # 逐字相同;返回值的等价性由 tests/postgres/test_knowledge_store_conformance.py
    # 对同一批夹具在两个后端各跑一遍、断言同一份期望值来证明。
    #
    # ⚠ 调用契约:**调用方不得持有外层写事务**。这里从池里另取一条连接,从 write()
    # 里调用会读到提交前的旧数据,而且不会响亮失败——它会安静地返回一份过时的报告。
    # T2 起唯一的调用方是 KnowledgeLifecycleService._precompute_kg_analysis,它在两个
    # `_write()` 之外调用;T3 落地 KgAnalysisService 时要在服务层配断言(见 ports.py)。
    #
    # 口径(设计 §3.5):对象只算 USABLE_STATUSES;边只算 review_status != 'rejected'
    # 且两端都是本 notebook 的可用对象;被排除的量单独返回,不凭空消失。
    #
    # 效率:分桶/分组一律发生在 SQL 里,绝不把逐簇/逐边明细拉进 Python,绝不发巨型 IN,
    # 有界返回一律带截断标志。

    def _reject_inside_write_transaction(self, what: str) -> None:
        """本段的只读聚合**不得**在调用方的写事务内被调用。当场硬失败。

        与 SQLite 侧的同名守卫语义等价(parity 红线要求两侧都有守卫)。PostgreSQL 每次
        `connect()` 从池里另借一条连接,所以在 `write()` 里调这几条会读到那个事务提交
        **之前**的库 —— 一份过时的报告,而且不会有任何报错。SQLite 侧还多一层危害
        (进程级写锁被全表扫按住),PG 没有那把锁,但静默过时这一条两侧完全相同。

        为什么必须是运行时守卫而不是只写注释:形状守卫抓不到这类违规 —— 这几条查询一张
        产物表都不碰,也不在写连接上执行,「哪个写事务碰过产物表」的断言对它们完全无效。
        评审用「把三条调用从 `_write()` 外面搬到里面」的移动变异实测过:25 条测试全绿。
        """
        if self.database.in_write_transaction:
            raise RuntimeError(
                f"{what}:全表级只读聚合被放进了写事务里。池化连接会读到提交前的旧数据"
                "(不报错、静默过时)。把它挪到 write() 之外调用。"
            )

    @contextmanager
    def _read_snapshot(self, *, repeatable: bool = False) -> Iterator[Any]:
        """一条**只读**连接;``repeatable=True`` 时再套一个 REPEATABLE READ 快照。

        ``read_only=True`` 让 psycopg 发 ``BEGIN … READ ONLY``:本段四个查询绝不写库
        这条契约因此由引擎强制,而不是靠代码评审记得。零额外 round-trip(autocommit
        关着,psycopg 本来就要发 BEGIN)。

        ``repeatable=True`` 只给**多条语句**的查询用。PostgreSQL 的 READ COMMITTED 是
        **每条语句**取一次快照,所以同一个连接里连续两条 SELECT 会看到不同的库:并发的
        `rebuild_communities`(DELETE + INSERT 同事务)只要在两条语句之间提交,报告就会
        自相矛盾(先数到 1200 个板块、再一个都取不到)。REPEATABLE READ 把快照提到
        **事务级**,整块共享同一份库。单语句查询不加它:快照本来就只有一个,而更长的
        快照会拖住 xmin horizon、妨碍 vacuum。

        连接归还池时 `PostgresDatabase._reset_connection` 会把 read_only /
        isolation_level 复位,不会污染下一个借用者。
        """
        with self.database.connect() as db:
            db.read_only = True
            if repeatable:
                db.isolation_level = IsolationLevel.REPEATABLE_READ
            yield db

    def cluster_size_histogram(self, notebook_id: str) -> Dict[str, object]:
        """收敛率的分布面:簇大小分桶直方图,**按 object_type 分组**(A2)。
        分组分桶在 SQL 里完成,Python 只补零、定序、求合计。

        ⚠ **重活**(本 notebook 的 concept_clusters 全扫 + 每行一次 knowledge_objects
        探查 + 分组聚合)。生产库 200 万+ 簇行;代价量级见 ports.py 该段头的双后端标注。
        由调用方(T3 的预计算路径)决定何时调,不挂在线请求上。

        与 SQLite 侧同理,刻意不复用 concept_clusters_count + distinct_cluster_count:
        member_rows / clusters 就是同一次 GROUP BY 的两个求和,再跑两趟扫描既浪费、
        又没有 USABLE_STATUSES 过滤(复用会把已弃用成员算进收敛率)。

        按 object_type 分组的理由与「其他」组不报类型名的理由,见 SQLite 侧同名方法
        与 knowledge_contracts.CLUSTER_OBJECT_TYPES / OTHER_OBJECT_TYPE_GROUP。
        """
        self._reject_inside_write_transaction("簇大小直方图")
        placeholders = ",".join("%s" for _ in USABLE_STATUSES)
        with self._read_snapshot() as db:
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
                    WHERE c.notebook_id = %s
                    GROUP BY c.object_type, c.canonical_id
                  ) g
                ) b
                GROUP BY object_type, bucket
                """,
                (*USABLE_STATUSES, notebook_id),
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
        """收敛率的头部:成员最多的 N 个 concept 簇。

        ⚠ **重活,与直方图同量级**——全扫 + hash 分组之后**还多一次排序**(LIMIT 只截断
        输出,不减少扫描与排序的输入;本机 30 万簇行实测 152 ms,计划是 Hash Join →
        HashAggregate(已溢盘,Planned Partitions 4)→ top-N heapsort)。
        分列实测数字见 ports.py 该段头。

        只取 `object_type='concept'`(榜单上冒出整句 claim 当名字没有意义);其他类型的
        计数由 `cluster_size_histogram` 分组报出,不在这里重扫一遍。``canonical_name``
        取 `MIN(NULLIF(...,''))` 而不是裸 `MIN()` —— 同一 canonical_id 下名字并不保证
        一致(本机库实测有 16 例),裸 `MIN()` 等于随便挑一个,而空串一旦出现就会永远
        排第一、劫持榜首。证据强度与「空名本身在本机未实测到」的如实说明,见 SQLite 侧
        同名方法。``limit`` clamp 到 [1, LARGEST_CLUSTERS_MAX],多取一行判 ``truncated``
        (不额外跑 COUNT)。
        """
        self._reject_inside_write_transaction("最大簇榜单")
        limit = _clamp(limit, LARGEST_CLUSTERS_MAX)
        placeholders = ",".join("%s" for _ in USABLE_STATUSES)
        with self._read_snapshot() as db:
            rows = db.execute(
                f"SELECT c.canonical_id AS canonical_id, "
                f"       COALESCE(MIN(NULLIF(c.canonical_name, '')), '') AS canonical_name, "
                f"       COUNT(o.id) AS members "
                f"FROM concept_clusters c "
                f"JOIN knowledge_objects o ON o.id = c.member_object_id "
                f"     AND o.notebook_id = c.notebook_id "
                f"     AND o.status IN ({placeholders}) "
                f"WHERE c.notebook_id = %s AND c.object_type = 'concept' "
                f"GROUP BY c.canonical_id "
                f"ORDER BY members DESC, c.canonical_id COLLATE \"C\" ASC LIMIT %s",
                (*USABLE_STATUSES, notebook_id, limit + 1),
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
        """主题板块列表(C1):板块 id / 规模 / 按 centrality 降序的前 K 个代表概念。

        **两条语句,跑在一次 REPEATABLE READ 只读事务里**(`_read_snapshot`):total 与
        板块明细必须看同一份库,否则并发 rebuild_communities 一劈就会出现「有 1200 个
        板块,一个都没有」这种自相矛盾的载荷(READ COMMITTED 每条语句各取一次快照)。

        ⚠ 与 SQLite 侧**形态分岔**,理由是两侧的瓶颈不同:
          · SQLite 是同进程文件访问,逐板块 `ORDER BY centrality DESC LIMIT K` 的往返
            成本为零,而有界 top-K 的峰值排序内存是 O(K)。在那边换成窗口函数**实测更慢
            17~40 倍**(本机真实库 1217 个板块:limit=50 3.0→52.5 ms,limit=200
            4.8→193.1 ms),所以那一侧保持逐板块取;
          · PostgreSQL 每条语句是一次网络 round-trip。逐板块取 = limit+2 条语句
            (limit=200 时 202 次往返),这在 PG 上是真实成本。
        所以这里用**一条**窗口函数查询:`ROW_NUMBER() OVER (PARTITION BY community_id
        ORDER BY centrality DESC, canonical_id)` 限制在**已选出的 ≤200 个板块**上
        (`community_id IN (SELECT id FROM picked)` 是对一个 ≤200 行 CTE 的半连接,
        不是巨型 IN 字面量),`rn <= K` 过滤。窗口的输入因此被 picked 有界住,不会退化
        成对整张 community_members 的全量排序。

        `LEFT JOIN` 保证没有成员行的板块照样出现在结果里(不静默丢板块);`rn IS NULL`
        是「这个板块一个成员行都没有」的判据,与 canonical_name 恰好是空串区分得开。

        截断绝不静默:返回 ``total`` / ``returned`` / ``truncated``,每个板块另带
        ``top_members_truncated``(该板块成员数是否超过 K)。
        """
        self._reject_inside_write_transaction("主题板块列表")
        level = int(level)
        limit = _clamp(limit, COMMUNITY_OVERVIEW_MAX)
        top_k = _clamp(top_k, COMMUNITY_TOP_MEMBERS_MAX)
        with self._read_snapshot(repeatable=True) as db:
            total = int(db.execute(
                "SELECT COUNT(*) AS c FROM communities WHERE notebook_id=%s AND level=%s",
                (notebook_id, level)).fetchone()["c"])
            rows = db.execute(
                """
                WITH picked AS (
                  SELECT id, size FROM communities
                  WHERE notebook_id=%s AND level=%s
                  ORDER BY size DESC, id COLLATE "C" ASC LIMIT %s
                ), ranked AS (
                  SELECT m.community_id AS community_id,
                         m.canonical_name AS canonical_name,
                         ROW_NUMBER() OVER (
                           PARTITION BY m.community_id
                           ORDER BY m.centrality DESC, m.canonical_id COLLATE "C" ASC
                         ) AS rn
                  FROM community_members m
                  WHERE m.notebook_id=%s AND m.level=%s
                    AND m.community_id IN (SELECT id FROM picked)
                )
                SELECT p.id AS id, p.size AS size,
                       r.canonical_name AS canonical_name, r.rn AS rn
                FROM picked p
                LEFT JOIN ranked r ON r.community_id = p.id AND r.rn <= %s
                ORDER BY p.size DESC, p.id COLLATE "C" ASC, r.rn ASC
                """,
                (notebook_id, level, limit, notebook_id, level, top_k),
            ).fetchall()
        communities: List[Tuple[str, int, List[str]]] = []
        for row in rows:
            if not communities or communities[-1][0] != row["id"]:
                communities.append((row["id"], int(row["size"]), []))
            if row["rn"] is not None:
                communities[-1][2].append(row["canonical_name"] or "")
        return _community_overview_payload(communities, total, level, limit, top_k)

    def relation_provenance_counts(self, notebook_id: str) -> Dict[str, object]:
        """边的出处构成 —— ⚠ **重活:本 notebook 的 knowledge_relations 全表扫**。

        生产库 800 万+ 边,每行还要匹配两个 knowledge_objects 端点(口径要求两端都是本
        库可用对象)。**不适合挂在在线请求上**,由调用方决定何时调(预计算阶段更合适)。

        ⚠ 代价量级**不要**从本机热态线性外推,也不要用一个数字盖住两个后端:
          · SQLite 上是全扫 + 每行两次 PK 随机探查,没有并行。本机 5.1GB 真实库 5.2 万边
            冷跑 1165 ms / 暖态 217 ms(差 5 倍),而仓库里那条 835 万边**冷扫 39 分钟**
            的事故(`sqlite/knowledge_store.py` 的 relation_connected_object_ids,而且那条
            **没有**每行两次探查)是同形状 —— 生产规模按小时量级估,不是秒级。
          · PostgreSQL 本机实测计划:`Parallel Hash Left Join ×2` + `Parallel Seq Scan`,
            **2 个 worker**,60 万边 234 ms,比同规模 SQLite 快约 4.4 倍。仍然是全表级。
        两侧的分列实测数字见 ports.py 段头。

        只做**一次**扫描、只 COUNT 不取行:LEFT JOIN + 单层 CASE 让同一趟同时产出出处
        分桶与被排除量,不为「不凭空消失」再补第二趟 COUNT。

        分桶顺序:``rejected`` 判在最前,所以既被否决、端点又不可用的边只计一次。
        ``endpoint_unusable`` 合并「端点不存在 / 跨库 / 状态不可用」——对产品是同一件事:
        那个端点不在可用节点集里,这条边进不了图。
        ⚠ 这个顺序是**有序且互斥**的语义,不是一堆独立谓词:`rejected` 压过
        `endpoint_unusable`,两个精确 `relink:` 压过 `relink:other`,任何有 tag 的压过
        `untagged`,而 `tag` 还可能是 NULL(`evidence -> 0 ->> 'basis'`)。所以 CASE
        **必须留着** —— 把它拆成一串独立的 `FILTER (WHERE …)` 谓词要手工把每一层否定
        重新编码一遍,任何一处漏了就静默重复计数或漏计。

        evidence 是 jsonb NOT NULL,故不需要 SQLite 侧的 json_valid 兜底;但仍显式判
        ``jsonb_typeof(...)='object'``,免得 evidence[0] 是标量时取 basis 出意外。

        ⚠ **与 SQLite 侧的形态分岔(§3.35:允许分岔,但必须写明理由 + 两侧都有守卫)**:
        这里的外层是 `count(*) FILTER (WHERE bucket = …)` 的**单行**聚合,SQLite 侧保持
        `GROUP BY bucket`。互斥性仍由内层那个 CASE 保证,分桶语义两侧逐字相同。

        分岔的理由是**省掉一次 836 万行的排序**,不是本机那点热态耗时差:规划器对那个
        巨大的 CASE 表达式估不出基数(以为有 60 万个分组),于是外层走 `GroupAggregate`
        + 一次 `external merge sort`。把分组换成固定的 7 个 FILTER 聚合,PG 直接
        Parallel Aggregate,那次排序整个消失。
        (别拿「PG 505ms vs SQLite 125ms」当理由——那是 60 万行热态的假象。437GB / 836 万
        边上 PG 是并行顺扫 + hash join + 一次外排,几十秒量级;SQLite 是 2×836 万次冷态
        随机主键探查,同形状数据点是 39 分钟。倒挂会翻回来。)

        ⚠ **守卫必须跟着一起分岔,否则分岔就是在掏空绊线。** `_relation_provenance_payload`
        里的「SQL 冒出契约外的桶名就炸」那条绊线,在固定 FILTER 形态下会退化成空转
        (payload 恒含且只含契约里的桶)。所以这里额外查一个
        `min(bucket) FILTER (WHERE NOT (bucket = ANY(…)))`:同一趟扫描、零额外代价,
        契约外的桶名照样在第一次调用就炸出来,而且还报得出是哪个名字。SQLite 侧则保留
        `GROUP BY` 形态,让原生的那条绊线至少活在一个后端上。
        """
        self._reject_inside_write_transaction("边出处构成")
        placeholders = ",".join("%s" for _ in USABLE_STATUSES)
        # 桶名从契约本身展开,SQL 与 knowledge_contracts 之间不可能漂移。
        bucket_names = (*_RELATION_PROVENANCE_BUCKETS, *_RELATION_EXCLUSION_BUCKETS)
        filters = ", ".join(
            f"count(*) FILTER (WHERE bucket = %s) AS b{i}"
            for i in range(len(bucket_names))
        )
        with self._read_snapshot() as db:
            row = db.execute(
                f"""
                SELECT {filters},
                       min(bucket) FILTER (WHERE NOT (bucket = ANY(%s)))
                         AS unknown_bucket
                FROM (
                  SELECT CASE
                           WHEN rs = 'rejected' THEN 'rejected'
                           WHEN bad THEN 'endpoint_unusable'
                           WHEN tag = 'relink:shared-element' THEN 'relink:shared-element'
                           WHEN tag = 'relink:name-match' THEN 'relink:name-match'
                           WHEN tag LIKE 'relink:%%' THEN 'relink:other'
                           WHEN COALESCE(tag, '') != '' THEN 'tagged:other'
                           ELSE 'untagged' END AS bucket
                  FROM (
                    SELECT r.review_status AS rs,
                           (s.id IS NULL OR t.id IS NULL) AS bad,
                           CASE WHEN jsonb_typeof(r.evidence -> 0) = 'object'
                                THEN r.evidence -> 0 ->> 'basis' END AS tag
                    FROM knowledge_relations r
                    LEFT JOIN knowledge_objects s
                           ON s.id = r.source_object_id
                          AND s.notebook_id = r.notebook_id
                          AND s.status IN ({placeholders})
                    LEFT JOIN knowledge_objects t
                           ON t.id = r.target_object_id
                          AND t.notebook_id = r.notebook_id
                          AND t.status IN ({placeholders})
                    WHERE r.notebook_id = %s
                  ) y
                ) x
                """,
                (*bucket_names, list(bucket_names),
                 *USABLE_STATUSES, *USABLE_STATUSES, notebook_id),
            ).fetchone()
        counts = {name: int(row[f"b{i}"]) for i, name in enumerate(bucket_names)}
        if row["unknown_bucket"] is not None:
            # 走中性装配层的同一条错误路径,两侧报同一句话。
            counts[row["unknown_bucket"]] = 0
        return _relation_provenance_payload(counts)

    # ------------------------------ KG 质量分析的预计算产物(T2,写路径)
    # 与 sqlite/unified_kg_store.py 的同名段落**语义等价**:同样的口径、同样的分组、
    # 同样的整批重写与账本行。这一段两侧**没有形态分岔** —— 单条 hash-aggregate 分组
    # 在两个规划器上都是正确形态,没有像 `community_overview` 那样出现「一侧明显更优」
    # 的分歧,所以刻意保持逐条对应,少一处需要论证的差异。方言差异只有占位符(%s)、
    # jsonb 参数、以及 `COUNT(*)` 的类型收口。
    #
    # 连接契约:connection-taking,骑调用方的事务边界。
    # `replace_kg_analysis_artifacts` 与它的账本行必须落在同一个事务里 —— 设计 §3.3:
    # 一次预计算要么整批可见、要么完全不可见。

    @staticmethod
    def kg_analysis_artifact_seqs(db: Any, notebook_id: str) -> Dict[str, int]:
        """账本现状:``{kind: kg_mutation_seq}``。最多 5 行,点读。

        与 SQLite 侧逐条对应。预计算**自己的**新鲜度闸靠它判定(见
        `KnowledgeLifecycleService.rebuild_communities` 的 B1 说明);判定本身留在
        后端中性的 `kg_analysis_precompute.analysis_ledger_is_current` 里,store 不
        替它下结论。
        """
        return {
            row["kind"]: int(row["kg_mutation_seq"])
            for row in db.execute(
                "SELECT kind, kg_mutation_seq FROM kg_analysis_artifacts "
                "WHERE notebook_id=%s", (notebook_id,),
            ).fetchall()
        }

    # ------------------------ KG 质量分析产物的**读**路径(T3,在线请求路径)
    # 与 sqlite/unified_kg_store.py 的同名段落逐条对应、语义等价:同样的排序键、同样的
    # 并列消歧、同样的行形状。方言差异只有占位符、``timestamptz`` → ISO 文本的收口、
    # 以及 ``s.id IS NULL`` 天然是 bool(SQLite 侧要 CASE 出 0/1)。
    #
    # 这三个只读**预计算产物表**,代价按行数硬有界(账本 ≤5 行;来源画像一次一页;
    # 跨板块边 ≤ MAX_PERSISTED_COMMUNITY_EDGES,一次只取 top-N),所以它们可以挂在
    # 在线请求上——T1 那三条全表重活不行。connection-taking,骑调用方的读连接。

    @staticmethod
    def kg_analysis_artifact_rows(
        db: Any, notebook_id: str
    ) -> Dict[str, Dict[str, object]]:
        """账本全文:``{kind: {kg_mutation_seq, payload, created_at}}``。最多 5 行,点读。

        与 SQLite 侧逐条对应(见那边的 docstring:与 `kg_analysis_artifact_seqs` 的分工、
        「行在不在才是产物在不在」的判据)。``payload`` 这边是 jsonb → psycopg 直接给
        dict,SQLite 那边是 JSON 文本,两种都由中性的
        `kg_analysis_payloads.artifact_ledger_rows` 收成同一个形状。
        ``created_at`` 是 timestamptz,按本 store 既有惯例(`state_row`)收成 ISO 文本。
        """
        return _artifact_ledger_rows(
            (
                row["kind"],
                row["kg_mutation_seq"],
                row["payload"],
                iso_timestamp(row["created_at"]),
            )
            for row in db.execute(
                "SELECT kind, kg_mutation_seq, payload, created_at "
                "FROM kg_analysis_artifacts WHERE notebook_id=%s ORDER BY kind",
                (notebook_id,),
            ).fetchall()
        )

    @staticmethod
    def kg_community_edges_top(
        db: Any, notebook_id: str, limit: int = 200
    ) -> List[Tuple[str, str, int]]:
        """跨板块边的 **top-N**(按 weight 降序),给板块俯瞰图用。

        与 SQLite 侧逐条对应,包括「代价有界靠的是 T2 的行数上限而不是 weight 索引」
        这一条:主键是 (notebook_id, src, dst),没有 weight 索引,所以 PG 上是本
        notebook 分片的一次扫 + top-N heapsort,输入被
        `MAX_PERSISTED_COMMUNITY_EDGES` 钉在 20 万行内。排序键补 src/dst 让并列
        weight 的取法在两个后端上逐字一致(``COLLATE "C"`` 与仓库其它 id 排序同款,
        对齐 SQLite 的字节序)。
        """
        limit = _clamp(limit, KG_COMMUNITY_EDGES_MAX)
        return [
            (row["src_community_id"], row["dst_community_id"], int(row["weight"]))
            for row in db.execute(
                "SELECT src_community_id, dst_community_id, weight "
                "FROM kg_community_edges WHERE notebook_id=%s "
                "ORDER BY weight DESC, src_community_id COLLATE \"C\" ASC, "
                "         dst_community_id COLLATE \"C\" ASC "
                "LIMIT %s",
                (notebook_id, limit),
            ).fetchall()
        ]

    @staticmethod
    def kg_source_profile_page(
        db: Any,
        notebook_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        ascending: bool = True,
    ) -> "Tuple[int, List[Dict[str, object]]]":
        """来源画像的**一页**,外加该 notebook 的总行数 → ``(total, rows)``。

        与 SQLite 侧逐条对应(见那边的 docstring:为什么必须分页、为什么
        ``source_id ASC`` 的并列消歧是分页正确性的一部分、为什么 ``total`` 用
        COUNT(*) 而不复用账本里的 ``sources``、``source_missing`` 为什么要显式报)。

        形态上唯一的差别是 PG 会对次级排序键走 incremental sort 而不是 SQLite 的
        「最后一项补一次临时 B 树」——同样只在并列组内排,量级相同。
        LEFT JOIN 同样只发生在已分页的 ≤200 行上(带 LIMIT 的子查询是连接顺序的
        优化屏障),即至多 200 次主键点查。
        """
        limit = _clamp(limit, KG_SOURCE_PAGE_MAX)
        offset = max(0, int(offset))
        direction = "ASC" if ascending else "DESC"
        total = int(db.execute(
            "SELECT COUNT(*) AS c FROM kg_source_profiles WHERE notebook_id=%s",
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
                   (s.id IS NULL) AS source_missing
            FROM (
              SELECT notebook_id, source_id, n_objects, n_graph_objects,
                     top_community_id, top_share, community_spread, mainstream_share
              FROM kg_source_profiles
              WHERE notebook_id = %s
              ORDER BY mainstream_share {direction}, source_id COLLATE "C" ASC
              LIMIT %s OFFSET %s
            ) p
            LEFT JOIN sources s
                   ON s.id = p.source_id AND s.notebook_id = p.notebook_id
            ORDER BY p.mainstream_share {direction}, p.source_id COLLATE "C" ASC
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
    def source_community_counts(db: Any, notebook_id: str, level: int):
        """来源画像的**唯一一次扫描**:``(source_id, community_id, n)`` 的库内分组游标。

        LEFT JOIN 而不是 INNER JOIN:``community_id IS NULL`` 的那一组是「该来源里没进
        任何主题板块的对象」,它必须计入 `n_objects` 却不能进分母。用 INNER JOIN 就得
        再扫一遍 `knowledge_objects` 才能拿到总数。

        ``COALESCE(c.canonical_id, o.id)`` 与 `community_graph_rows` 的 canonical 口径
        逐字一致:没有 cluster_map 行的对象就是它自己的 canonical(社区正是建在那个口径
        的图上)。SQLite 侧同款。

        PostgreSQL 上规划器对这个形态选 hash join + HashAggregate(分组键是两个普通
        列,基数估得准 —— 与 T1 那条 `relation_provenance_counts` 不同,那里的巨大 CASE
        表达式让规划器误判成 60 万个分组、退化成 GroupAggregate + external merge sort)。
        分组仍然发生在库内,调用方流式消费,Python 侧内存是 O(来源数)。

        口径与 SQLite 侧逐字一致:对象只算 `USABLE_STATUSES`;`source_id=''` 的对象
        整体排除(它们共享同一个空 source_id,算进去等于凭空造一个「空来源」的画像)。
        """
        placeholders = ",".join("%s" for _ in USABLE_STATUSES)
        return db.execute(
            f"""
            SELECT o.source_id AS source_id,
                   cm.community_id AS community_id,
                   COUNT(*) AS n
            FROM knowledge_objects o
            LEFT JOIN concept_clusters c
                   ON c.notebook_id = o.notebook_id
                  AND c.member_object_id = o.id
            LEFT JOIN community_members cm
                   ON cm.notebook_id = o.notebook_id
                  AND cm.canonical_id = COALESCE(c.canonical_id, o.id)
                  AND cm.level = %s
            WHERE o.notebook_id = %s
              AND o.status IN ({placeholders})
              AND o.source_id != ''
            GROUP BY o.source_id, cm.community_id
            """,
            (int(level), notebook_id, *USABLE_STATUSES),
        )

    @staticmethod
    def replace_kg_analysis_artifacts(
        db: Any,
        notebook_id: str,
        kg_mutation_seq: int,
        edges: "Iterable[Tuple[str, str, int]]",
        profiles: "Iterable[Tuple[str, int, int, str, float, int, float]]",
        payloads: Dict[str, dict],
        now: str,
    ) -> None:
        """整批重写一个 notebook 的三张产物表 —— **必须由调用方包在一个写事务里**。

        与 SQLite 侧逐条对应(见那边的 docstring):三张表统一按 notebook 整表删(没有
        level 维度),`edges` / `profiles` 只按可迭代分批消费、不多物化一份。
        PostgreSQL 侧的额外收口:账本 payload 走 `jsonb()` 且先过 `_json_document` 的
        有限性校验 —— NaN/Inf 进 jsonb 会在读回时变成不可解析的值,必须在写入口就拒
        (SQLite 侧的对等收口是 `json.dumps(..., allow_nan=False)`)。
        """
        check_artifact_payloads(payloads)
        db.execute(
            "DELETE FROM kg_community_edges WHERE notebook_id=%s", (notebook_id,)
        )
        db.execute(
            "DELETE FROM kg_source_profiles WHERE notebook_id=%s", (notebook_id,)
        )
        db.execute(
            "DELETE FROM kg_analysis_artifacts WHERE notebook_id=%s", (notebook_id,)
        )
        for batch in batched(
            (
                (notebook_id, src, dst, int(weight))
                for src, dst, weight in edges
            ),
            1000,
        ):
            execute_many(
                db,
                "INSERT INTO kg_community_edges "
                "(notebook_id, src_community_id, dst_community_id, weight) "
                "VALUES (%s,%s,%s,%s)",
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
            execute_many(
                db,
                "INSERT INTO kg_source_profiles "
                "(notebook_id, source_id, n_objects, n_graph_objects, "
                " top_community_id, top_share, community_spread, mainstream_share) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                batch,
            )
        execute_many(
            db,
            "INSERT INTO kg_analysis_artifacts "
            "(notebook_id, kind, kg_mutation_seq, payload, created_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            [
                (notebook_id, kind, int(kg_mutation_seq),
                 jsonb(_json_document(
                     payloads[kind], expected=dict, field=f"kg analysis {kind}"
                 )),
                 normalize_timestamp(now))
                for kind in sorted(payloads)
            ],
        )

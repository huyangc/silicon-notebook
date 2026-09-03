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
from app.repositories.postgres.cluster_lock import (
    lock_cluster_artifact_type,
    lock_cluster_artifact_types,
)
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.mount_sql import MOUNT_JOIN, MOUNT_ORDER, MOUNT_VALID
from app.repositories.postgres.search import (
    drop_mention_scan,
    mention_claim_rows,
    mention_scan_matches as search_mention_scan_matches,
    prepare_mention_scan,
)
from app.domain.kg_analysis_contracts import (
    BOARD_DEPENDENT_ARTIFACT_KINDS,
    batched,
    check_artifact_payloads,
)
from app.domain.knowledge_contracts import (
    CLUSTER_OBJECT_TYPES,
    COMMUNITY_OVERVIEW_MAX,
    COMMUNITY_TOP_MEMBERS_MAX,
    KG_COMMUNITY_EDGES_MAX,
    KG_SOURCE_PAGE_MAX,
    LARGEST_CLUSTERS_MAX,
    RELATION_EXCLUSION_BUCKETS as _RELATION_EXCLUSION_BUCKETS,
    RELATION_PROVENANCE_BUCKETS as _RELATION_PROVENANCE_BUCKETS,
    USABLE_STATUSES,
)


# 批 3·W2 §1.4:published 代次谓词。子查询必须用绑定参数(而非相关列引用),
# PG 才会规划成一次求值的 InitPlan;COALESCE 兜「无 unified_kg_state 行 ⇒
# 代次 0」的显式契约(副本/合并库,见设计 §1.6)。{alias} 由站点填表别名。
_PUBLISHED_CLUSTER_GEN = (
    "COALESCE((SELECT cluster_generation FROM unified_kg_state "
    "WHERE notebook_id = %s), 0)"
)
_PUBLISHED_COMMUNITY_GEN = (
    "COALESCE((SELECT community_generation FROM unified_kg_state "
    "WHERE notebook_id = %s), 0)"
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
            f"SELECT DISTINCT canonical_id, canonical_description, canonical_desc_sig "
            f"FROM concept_clusters WHERE notebook_id=%s AND object_type='concept' "
            f"AND generation = {_PUBLISHED_CLUSTER_GEN}",
            (notebook_id, notebook_id),
        ).fetchall()

    @staticmethod
    def cluster_evidence_rows(
        db: Any, notebook_id: str, run_id: str, seeds,
    ):
        """Evidence blobs for a set of cluster seeds, each tagged with the ``seed``
        it came from (mirrors the SQLite projection). The concept-description
        stage batches many canonicals' seeds into one call and regroups in
        memory, which needs the seed on every row.

        ⚠ Registered follow-up (a migration, deliberately not in this batch):
        ``idx_kg_cluster_scratch_nb_run`` is ``(notebook_id, run_id)`` with no
        ``seed`` key, so ``seed = ANY`` stays a residual filter over the run's
        whole scratch table — see the SQLite twin for the full note."""
        values = list(seeds)
        if not values:
            return []
        ph = ",".join("%s" for _ in values)
        return db.execute(
            f"SELECT s.seed AS seed, k.evidence::text AS evidence FROM knowledge_objects k "
            f"JOIN kg_cluster_scratch s ON s.object_id=k.id "
            f"WHERE s.notebook_id=%s AND s.run_id=%s AND s.seed IN ({ph})",
            (notebook_id, run_id, *values),
        ).fetchall()

    @staticmethod
    def canonical_relation_seed_rows(db: Any, notebook_id: str):
        return db.execute(
            f"SELECT kr.id AS rid, kr.source_id AS src_doc, kr.edge_type AS et, "
            f"       COALESCE(cs.canonical_id, kr.source_object_id) AS s, "
            f"       COALESCE(ct.canonical_id, kr.target_object_id) AS t, "
            f"       so.object_type AS st, tp.object_type AS tt "
            f"FROM knowledge_relations kr "
            f"JOIN knowledge_objects so ON so.id=kr.source_object_id "
            f"JOIN knowledge_objects tp ON tp.id=kr.target_object_id "
            f"LEFT JOIN concept_clusters cs ON cs.notebook_id=kr.notebook_id "
            f"  AND cs.member_object_id=kr.source_object_id "
            f"  AND cs.generation = {_PUBLISHED_CLUSTER_GEN} "
            f"LEFT JOIN concept_clusters ct ON ct.notebook_id=kr.notebook_id "
            f"  AND ct.member_object_id=kr.target_object_id "
            f"  AND ct.generation = {_PUBLISHED_CLUSTER_GEN} "
            f"WHERE kr.notebook_id=%s AND kr.review_status!='rejected'",
            (notebook_id, notebook_id, notebook_id),
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
                f"SELECT DISTINCT canonical_id, canonical_name FROM concept_clusters "
                f"WHERE notebook_id=%s AND generation = {_PUBLISHED_CLUSTER_GEN}",
                (notebook_id, notebook_id),
            )
        }
        relations = db.execute(
            f"SELECT COALESCE(cs.canonical_id, kr.source_object_id) AS s, "
            f"       COALESCE(ct.canonical_id, kr.target_object_id) AS t "
            f"FROM knowledge_relations kr "
            f"LEFT JOIN concept_clusters cs ON cs.notebook_id=kr.notebook_id "
            f"AND cs.member_object_id=kr.source_object_id "
            f"AND cs.generation = {_PUBLISHED_CLUSTER_GEN} "
            f"LEFT JOIN concept_clusters ct ON ct.notebook_id=kr.notebook_id "
            f"AND ct.member_object_id=kr.target_object_id "
            f"AND ct.generation = {_PUBLISHED_CLUSTER_GEN} "
            f"WHERE kr.notebook_id=%s AND kr.review_status!='rejected' "
            f"ORDER BY kr.id COLLATE \"C\"",
            (notebook_id, notebook_id, notebook_id),
        )
        return names, relations

    @staticmethod
    def cluster_version_row(db: Any, notebook_id: str):
        row = db.execute(
            f"SELECT COUNT(*) AS c,MAX(created_at) AS ts "
            f"FROM concept_clusters WHERE notebook_id = %s "
            f"AND generation = {_PUBLISHED_CLUSTER_GEN}",
            (notebook_id, notebook_id),
        ).fetchone()
        return {**dict(row), "ts": iso_timestamp(row["ts"])}

    @staticmethod
    def cluster_member_rows(db: Any, notebook_id: str):
        return db.execute(
            f"SELECT canonical_id, member_object_id FROM concept_clusters "
            f"WHERE notebook_id = %s AND generation = {_PUBLISHED_CLUSTER_GEN} "
            f"ORDER BY canonical_id COLLATE \"C\", member_object_id COLLATE \"C\"",
            (notebook_id, notebook_id),
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
            f"SELECT COUNT(*) AS c,MAX(created_at) AS ts "
            f"FROM concept_clusters WHERE notebook_id=%s "
            f"AND generation = {_PUBLISHED_CLUSTER_GEN}",
            (notebook_id, notebook_id),
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
    def graph_seq_row(db: Any, notebook_id: str) -> "tuple[int, int, int, int]":
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
            "FROM unified_kg_state WHERE notebook_id=%s",
            (notebook_id,),
        ).fetchone()
        if row is None:
            return (0, 0, -1, 0)
        return (int(row["ks"]), int(row["cs"]), int(row["ms"]), int(row["ep"]))

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

    # ---------------------------------------------- derived generations (W2)
    # 批 3·W2 PR-2:代次化写者的四个数据级原语。取号/释放/翻转构成跨进程
    # 单飞(连离线 recluster CLI 也被闸住——进程内维护槽只覆盖本进程);
    # 时钟一律取 DB 服务端 now()(设计 §1.5:催收锚点不得依赖应用时钟)。

    @staticmethod
    def claim_derived_generation(
        db: Any, notebook_id: str, *, ttl_seconds: int
    ) -> "dict | None":
        """取号微事务:counter+1 并登记在飞代。三种结局:抢到(返回
        generation/双指针/催收欠账标记/DB 时钟 ts);被占且未过 TTL(返回
        None——KgMaintenanceAlreadyRunning 同款拒绝);上一位崩溃且过 TTL
        (抢占——被抢占者的写段重读 building 早停,其翻转双 CAS 必然作废)。
        TTL 只是崩溃兜底:正常失败路径由 release_derived_claim 的 finally
        CAS 即时释放(设计 D-W2-5 v3)。"""
        row = db.execute(
            """
            INSERT INTO unified_kg_state
              (notebook_id, derived_generation_counter,
               derived_building_generation, derived_building_claimed_at,
               updated_at)
            VALUES (%s, 1, 1, now(), now())
            ON CONFLICT(notebook_id) DO UPDATE SET
              derived_generation_counter=unified_kg_state.derived_generation_counter+1,
              derived_building_generation=unified_kg_state.derived_generation_counter+1,
              derived_building_claimed_at=now(),
              updated_at=now()
            WHERE unified_kg_state.derived_building_generation = 0
               OR unified_kg_state.derived_building_claimed_at
                  < now() - make_interval(secs => %s)
            RETURNING derived_generation_counter AS generation,
                      cluster_generation, community_generation,
                      derived_catchup_from, now() AS ts
            """,
            (notebook_id, ttl_seconds),
        ).fetchone()
        if row is None:
            return None
        return {
            "generation": int(row["generation"]),
            "cluster_generation": int(row["cluster_generation"]),
            "community_generation": int(row["community_generation"]),
            "catchup_from": iso_timestamp(row["derived_catchup_from"])
            if row["derived_catchup_from"] is not None else None,
            "ts": iso_timestamp(row["ts"]),
        }

    @staticmethod
    def release_derived_claim(db: Any, notebook_id: str, generation: int) -> None:
        """finally 释放通道:CAS 只清自己那次认领——被 TTL 抢占后迟到的释放
        天然 no-op,绝不误清抢占者。"""
        db.execute(
            "UPDATE unified_kg_state SET derived_building_generation=0, "
            "derived_building_claimed_at=NULL, updated_at=now() "
            "WHERE notebook_id=%s AND derived_building_generation=%s",
            (notebook_id, generation),
        )

    @staticmethod
    def derived_claim_still_held(db: Any, notebook_id: str, generation: int) -> bool:
        """写段前复读(设计 §1.2/复评 P2-4):≠自己的 G 即被抢占,当场作废
        早停——抢占者预回收扫过的键区不会被再填残行。"""
        row = db.execute(
            "SELECT 1 FROM unified_kg_state "
            "WHERE notebook_id=%s AND derived_building_generation=%s",
            (notebook_id, generation),
        ).fetchone()
        return row is not None

    @staticmethod
    def write_cluster_map_generation(
        db: Any,
        notebook_id: str,
        object_type: str,
        run_id: str,
        created_at: str,
        generation: int,
    ) -> None:
        """写新代:swap 的 INSERT 半部 + generation 列。**不持 advisory
        lock、不 DELETE**(设计 §1.2:新代行与 published 代/并发 append 无
        行冲突,四列唯一按代隔离;锁窗口整个收进翻转微事务)。"""
        db.execute(
            "INSERT INTO concept_clusters "
            "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,"
            "canonical_description,canonical_desc_sig,created_at,generation) "
            "SELECT 'cc-' || md5(random()::text || clock_timestamp()::text || s.object_id), "
            "s.notebook_id,c.canonical_id,s.object_id,c.canonical_name,%s,"
            "c.canonical_description,c.canonical_desc_sig,%s::timestamptz,%s "
            "FROM kg_cluster_scratch s "
            "JOIN kg_canonical_scratch c "
            "ON c.notebook_id=s.notebook_id AND c.run_id=s.run_id AND c.seed=s.seed "
            "JOIN knowledge_objects k ON k.id=s.object_id "
            "WHERE s.notebook_id=%s AND s.run_id=%s ORDER BY k.ordinal",
            (object_type, created_at, generation, notebook_id, run_id),
        )

    @staticmethod
    def flip_cluster_generation(
        db: Any,
        notebook_id: str,
        *,
        published_from: int,
        generation: int,
        catchup_from_ts: str,
        now: str,
    ) -> bool:
        """四类一把翻的微事务(设计 §1.2):先按 key 字节序取齐四把
        advisory lock(append 与翻转互相串行,两者皆毫秒级——链 c 的 5s
        超时结构性消失),同事务内双 CAS 改指针:``cluster_generation`` 未被
        别人动过 且 在飞认领仍是自己。零行更新 = 被 TTL 抢占或被
        delete_notebook_kg 重置——本轮作废不发布,调用方响亮失败。
        ``cluster_mutation_seq`` 在同一语句 bump(版本身份与指针行变化同
        提交,kg_mutation.py 两段式红线);``derived_catchup_from`` 同事务
        落库(崩溃后下轮先补欠账,设计 §1.5)。"""
        lock_cluster_artifact_types(db, notebook_id, CLUSTER_OBJECT_TYPES)
        row = db.execute(
            """
            UPDATE unified_kg_state SET
              cluster_generation=%s,
              cluster_mutation_seq=cluster_mutation_seq+1,
              derived_building_generation=0,
              derived_building_claimed_at=NULL,
              derived_catchup_from=%s::timestamptz,
              updated_at=%s
            WHERE notebook_id=%s AND cluster_generation=%s
              AND derived_building_generation=%s
            RETURNING notebook_id
            """,
            (
                generation, normalize_timestamp(catchup_from_ts),
                normalize_timestamp(now), notebook_id, published_from,
                generation,
            ),
        ).fetchone()
        return row is not None

    @staticmethod
    def flip_community_generation(
        db: Any, notebook_id: str, *, published_from: int, generation: int,
        now: str,
    ) -> bool:
        """communities 族指针翻转——**在既有发布事务内**调用(设计 D-W2-6:
        该族无并发融合写者,原子性优先于锁窗口;kg_analysis 板块账本作废与
        set_community_seq 的同事务不变量原样保住)。双 CAS 同 cluster 侧。"""
        row = db.execute(
            "UPDATE unified_kg_state SET community_generation=%s, updated_at=%s "
            "WHERE notebook_id=%s AND community_generation=%s "
            "AND derived_building_generation=%s RETURNING notebook_id",
            (
                generation, normalize_timestamp(now), notebook_id,
                published_from, generation,
            ),
        ).fetchone()
        return row is not None

    @staticmethod
    def clear_catchup_marker(db: Any, notebook_id: str, ts: str) -> None:
        """催收完成:CAS 清欠账标记(只清自己那次翻转落的 ts)。"""
        db.execute(
            "UPDATE unified_kg_state SET derived_catchup_from=NULL, updated_at=now() "
            "WHERE notebook_id=%s AND derived_catchup_from=%s::timestamptz",
            (notebook_id, normalize_timestamp(ts)),
        )

    @staticmethod
    def catchup_window_members(
        db: Any, notebook_id: str, published_generation: int, since_ts: str,
        skew_seconds: int, limit: int,
    ) -> list:
        """催收输入(设计 §1.5):非 published 代里落在「翻转锚点 - 偏斜余量」
        之后的融合行——翻转后 append 全走新 published 代,这个集合冻结。
        谓词是 ``generation != published`` 而非 ``= 旧P``:欠账轮(上轮翻转后
        崩溃)只有锚点落库、退休代号无处可查,而窗口内可能混入的僵尸在飞代行
        幂等安置无害且被 created_at 窗口自然限量——「搬多了幂等无害,漏搬才是
        洞」的余量方向。join objects 取安置所需的 payload(期间被删的对象
        join 不到,天然跳过)。有界性凭据:idx_clusters_nb_created_gen 的
        (notebook_id, created_at) 范围 seek。"""
        return db.execute(
            "SELECT DISTINCT c.member_object_id, c.object_type, o.payload "
            "FROM concept_clusters c "
            "JOIN knowledge_objects o ON o.id = c.member_object_id "
            "WHERE c.notebook_id=%s AND c.generation != %s "
            "AND c.created_at >= %s::timestamptz - make_interval(secs => %s) "
            "LIMIT %s",
            (notebook_id, published_generation, normalize_timestamp(since_ts),
             skew_seconds, limit),
        ).fetchall()

    @staticmethod
    def reap_derived_generations_page(
        db: Any, notebook_id: str, table: str, keep: "tuple[int, ...]",
        limit: int,
    ) -> int:
        """残代回收单页(T-5a 排水同款 ctid 分页;唯一回收通道=下轮预回收+
        启动恢复,无 finally 回收——跨翻转在飞读者整轮宽限,设计 D-W2-7)。
        keep = (published, 在飞 G);catchup 标记在时调用方跳过整库。"""
        assert table in ("concept_clusters", "communities", "community_members")
        marks = ",".join(["%s"] * len(keep))
        cur = db.execute(
            f"DELETE FROM {table} WHERE ctid IN ("
            f"SELECT ctid FROM {table} WHERE notebook_id=%s "
            f"AND generation NOT IN ({marks}) LIMIT %s)",
            (notebook_id, *keep, limit),
        )
        return cur.rowcount

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
            f"SELECT member_object_id, canonical_id FROM concept_clusters "
            f"WHERE notebook_id=%s AND generation = {_PUBLISHED_CLUSTER_GEN}",
            (notebook_id, notebook_id),
        ).fetchall()
        return {r["member_object_id"]: r["canonical_id"] for r in rows}

    @staticmethod
    def cluster_fold_rows(
        db: Any, notebook_id: str, ids: List[str]
    ) -> List[dict]:
        """BOUNDED canonical fold lookup (only the given hit ids) — never the
        full cluster_map, which can be 5M entries at scale.

        ⚠ PR-2 复核:rebuild 折叠路径若需读 building 代,这里的 published
        代次谓词要再参数化(接收调用方要读哪一代,而不是永远钉 published)。"""
        if not ids:
            return []
        placeholders = ",".join("%s" for _ in ids)
        return db.execute(
            f"SELECT member_object_id, canonical_id, canonical_name "
            f"FROM concept_clusters "
            f"WHERE notebook_id=%s AND member_object_id IN ({placeholders}) "
            f"AND generation = {_PUBLISHED_CLUSTER_GEN}",
            [notebook_id] + ids + [notebook_id],
        ).fetchall()

    @staticmethod
    def concept_clusters_count(db: Any, notebook_id: str) -> int:
        return int(db.execute(
            f"SELECT COUNT(*) AS c FROM concept_clusters WHERE notebook_id=%s "
            f"AND generation = {_PUBLISHED_CLUSTER_GEN}",
            (notebook_id, notebook_id)).fetchone()["c"])

    @staticmethod
    def distinct_cluster_count(db: Any, notebook_id: str) -> int:
        return int(db.execute(
            f"SELECT COUNT(DISTINCT canonical_id) AS c FROM concept_clusters "
            f"WHERE notebook_id=%s AND generation = {_PUBLISHED_CLUSTER_GEN}",
            (notebook_id, notebook_id),
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
    def relation_support_rows(
        db: Any, notebook_id: str, triples: List[tuple],
    ) -> List[dict]:
        """SQLite ``relation_support_rows`` 的 parity 实现:同一 PK 定点查询,
        PostgreSQL 用 row-value ``IN`` 代替 OR 链 —— planner 对
        ``(a,b,c) IN ((...),(...))`` 就地展开成对 `pk_canonical_relations`
        的多路索引探查(EXPLAIN 已确认,见批 1 热点整改的 T1)。语义与
        SQLite 侧、与旧逐条 ``edge_support_rows`` 全表扫的等价性见那侧
        docstring 与 ``graph_retrieval.relation_support_counts``。投影里的
        ``support_count``(热路径修复批 2 · R2-1)同样是 parity:两个消费者
        (``relation_support_counts`` 只要 source,``annotate_edge_support``
        要 (support, source) 二元组)共用这一条定点原语,理由见 SQLite 侧。"""
        rows = [triple for triple in triples if triple]
        if not rows:
            return []
        placeholders = ",".join("(%s,%s,%s)" for _ in rows)
        params: List[object] = [notebook_id]
        for triple in rows:
            params.extend(triple)
        return db.execute(
            f"SELECT canonical_src, edge_type, canonical_tgt, "
            f"       support_count, source_count "
            f"FROM canonical_relations WHERE notebook_id=%s "
            f"AND (canonical_src, edge_type, canonical_tgt) IN ({placeholders})",
            params,
        ).fetchall()

    @staticmethod
    def weak_support_relation_rows(
        db: Any,
        notebook_id: str,
        canonical_ids: List[str],
        source_max: int,
        limit: int,
    ) -> List[dict]:
        """SQLite `weak_support_relation_rows` 的 parity 实现(设计文档 §3.3)。

        判据是 `source_count`(不同文档数)而不是 `support_count`(原始关系行数)
        —— 理由见 SQLite 侧同名方法。`(notebook_id, canonical_src)` 是
        `pk_canonical_relations` 的前缀,所以每个源端一次索引 seek,但 `source_count`
        无索引、是 seek 之后的残余过滤,hub 源端单次可扫数万行(`LIMIT` 只封返回
        行数)。`sample_relation_ids` 是 jsonb,按本适配器惯例 `::text` 出参,让服务
        层拿到与 SQLite 逐字同形的 JSON 文本(服务层因此不必判断 dialect)。排序
        尾键的理由同样见 SQLite 侧。
        """
        if not canonical_ids:
            return []
        placeholders = ",".join("%s" for _ in canonical_ids)
        return db.execute(
            f"SELECT canonical_src, edge_type, canonical_tgt, source_count, "
            f"       sample_relation_ids::text AS sample_relation_ids "
            f"FROM canonical_relations "
            f"WHERE notebook_id=%s AND canonical_src IN ({placeholders}) "
            f"  AND source_count<=%s "
            f"ORDER BY source_count ASC, canonical_tgt ASC, canonical_src ASC, "
            f"         edge_type ASC "
            f"LIMIT %s",
            [notebook_id, *canonical_ids, source_max, limit],
        ).fetchall()

    @staticmethod
    def relation_endpoint_name_rows(
        db: Any, notebook_id: str, relation_ids: List[str]
    ) -> List[dict]:
        """SQLite `relation_endpoint_name_rows` 的 parity 实现。

        为什么经样本关系而不是直接按 `canonical_id` 查簇表,见 SQLite 侧的说明
        (那一列两侧都没有索引,直查是按 notebook 的整段扫描)。同样按现存行原样
        解析、刻意不继承产地的 rejected/deprecated 过滤(快照口径,理由见那边)。
        """
        if not relation_ids:
            return []
        placeholders = ",".join("%s" for _ in relation_ids)
        return db.execute(
            f"SELECT kr.id AS rid, "
            f"       COALESCE(NULLIF(cs.canonical_name,''), "
            f"                so.payload ->> 'name', '') AS src_name, "
            f"       COALESCE(NULLIF(ct.canonical_name,''), "
            f"                tp.payload ->> 'name', '') AS tgt_name "
            f"FROM knowledge_relations kr "
            f"JOIN knowledge_objects so ON so.id=kr.source_object_id "
            f"JOIN knowledge_objects tp ON tp.id=kr.target_object_id "
            f"LEFT JOIN concept_clusters cs ON cs.notebook_id=kr.notebook_id "
            f"  AND cs.member_object_id=kr.source_object_id "
            f"  AND cs.generation = {_PUBLISHED_CLUSTER_GEN} "
            f"LEFT JOIN concept_clusters ct ON ct.notebook_id=kr.notebook_id "
            f"  AND ct.member_object_id=kr.target_object_id "
            f"  AND ct.generation = {_PUBLISHED_CLUSTER_GEN} "
            f"WHERE kr.notebook_id=%s AND kr.id IN ({placeholders})",
            [notebook_id, notebook_id, notebook_id, *relation_ids],
        ).fetchall()

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
            f"SELECT COUNT(*) AS c FROM communities WHERE notebook_id=%s AND level=%s "
            f"AND generation = {_PUBLISHED_COMMUNITY_GEN}",
            (notebook_id, level, notebook_id)).fetchone()

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
    def discard_board_dependent_kg_analysis_artifacts(
        db: Any, notebook_id: str
    ) -> None:
        """作废依赖板块划分的两份 KG 分析产物 —— 与 SQLite 侧逐条对应(见那边的
        docstring 与 `kg_analysis_precompute.BOARD_DEPENDENT_ARTIFACT_KINDS`)。

        `kind = ANY(%s)` 是 PostgreSQL 侧的等价写法(SQLite 那边是展开的 `IN (?,?)`),
        语义相同:只删这两个 kind 的账本行,三条统计快照原样留着。
        """
        db.execute(
            "DELETE FROM kg_community_edges WHERE notebook_id=%s", (notebook_id,)
        )
        db.execute(
            "DELETE FROM kg_source_profiles WHERE notebook_id=%s", (notebook_id,)
        )
        db.execute(
            "DELETE FROM kg_analysis_artifacts "
            "WHERE notebook_id=%s AND kind = ANY(%s)",
            (notebook_id, list(BOARD_DEPENDENT_ARTIFACT_KINDS)),
        )

    @staticmethod
    def set_community_seq(db: Any, notebook_id: str, seq: int) -> None:
        db.execute("UPDATE unified_kg_state SET community_seq=%s WHERE notebook_id=%s",
                   (seq, notebook_id))

    @staticmethod
    def community_member_ids(
        db: Any, notebook_id: str, level: int
    ) -> List[List[str]]:
        rows = db.execute(
            f"SELECT member_ids FROM communities WHERE notebook_id=%s AND level=%s "
            f"AND generation = {_PUBLISHED_COMMUNITY_GEN} "
            f"ORDER BY size DESC, id ASC",
            (notebook_id, level, notebook_id)).fetchall()
        return [json_value(r["member_ids"], []) for r in rows]

    @staticmethod
    def community_rows_for_summary(
        db: Any, notebook_id: str, level: int
    ) -> List[dict]:
        rows = db.execute(
            f"SELECT id,member_ids::text AS member_ids FROM communities "
            f"WHERE notebook_id=%s AND level=%s AND generation = {_PUBLISHED_COMMUNITY_GEN}",
            (notebook_id, level, notebook_id)).fetchall()
        return rows

    @staticmethod
    def board_partition_still_holds(
        db: Any, notebook_id: str, level: int, board_id: str
    ) -> bool:
        """复核「产物所依据的那套板块划分」是否还在库里 —— 与 SQLite 侧逐条对应
        (完整用途与「查一行就够」的判据见那边的 docstring)。

        ⚠ **`FOR SHARE` 是这一侧的载重件,不是装饰。** `PostgresDatabase.write()` 是
        READ COMMITTED、**没有**进程级锁,裸 SELECT 只是一次瞬时读:并发的
        `replace_communities` 完全可以在我们「查完」与「插完」之间提交,复核照样放行,
        写出去的仍是悬空产物。带上行锁之后两个方向都对:
          · 并发写者的 `DELETE FROM communities …` 会**阻塞**到我们提交为止;
          · 它若已经提交,`FOR SHARE` 根本读不到这一行 → 我们正确地判失效。
        SQLite 那侧没有也不需要这个语法(进程级写锁 + 文件写锁已经串行),parity 红线
        要的是语义等价而不是 SQL 等价。

        ⚠ **不改锁对象(死锁)。** 锁 `unified_kg_state` 那一行看着更"权威",但它会与
        并发写者的 `discard_board_dependent_… → set_community_seq` 顺序**反向**,PG 上
        可以真死锁。锁 `communities` 的行不会:并发写者的第一个动作就是
        `DELETE FROM communities`,它在**碰任何产物表之前**就被挡住,因此不可能持有
        我们想要的东西 —— 两边的加锁顺序共享同一个前缀。
        """
        return db.execute(
            "SELECT 1 FROM communities "
            "WHERE notebook_id=%s AND level=%s AND id=%s FOR SHARE",
            (notebook_id, level, board_id),
        ).fetchone() is not None

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
            f"SELECT member_ids, title, summary, findings FROM communities "
            f"WHERE notebook_id=%s AND level=%s AND summary!='' "
            f"AND generation = {_PUBLISHED_COMMUNITY_GEN} "
            f"ORDER BY size DESC, id ASC",
            (notebook_id, level, notebook_id)).fetchall()
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
                f"SELECT canonical_id FROM concept_clusters WHERE notebook_id=%s "
                f"AND lower(canonical_name)=%s AND generation = {_PUBLISHED_CLUSTER_GEN} "
                f"GROUP BY canonical_id ORDER BY COUNT(*) DESC, "
                f"canonical_id COLLATE \"C\" ASC LIMIT 1",
                (notebook_id, focal_key, notebook_id)).fetchone()
        return row["canonical_id"] if row else None

    def top_community_for(self, notebook_id: str, canonical_id: str) -> Optional[str]:
        with self.database.connect() as db:
            row = db.execute(
                f"SELECT community_id FROM community_members WHERE notebook_id=%s "
                f"AND canonical_id=%s AND generation = {_PUBLISHED_COMMUNITY_GEN} "
                f"ORDER BY level DESC, community_id COLLATE \"C\" ASC LIMIT 1",
                (notebook_id, canonical_id, notebook_id)).fetchone()
        return row["community_id"] if row else None

    def community_member_peers(
        self, notebook_id: str, community_id: str, exclude_canonical_id: str, limit: int
    ) -> List[dict]:
        with self.database.connect() as db:
            return db.execute(
                f"SELECT canonical_name, centrality FROM community_members "
                f"WHERE notebook_id=%s AND community_id=%s AND canonical_id!=%s "
                f"AND generation = {_PUBLISHED_COMMUNITY_GEN} "
                f"ORDER BY centrality DESC, canonical_id COLLATE \"C\" ASC LIMIT %s",
                (notebook_id, community_id, exclude_canonical_id, notebook_id, limit)
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
                    f"SELECT canonical_name FROM concept_clusters WHERE notebook_id=%s "
                    f"AND canonical_id=%s AND generation = {_PUBLISHED_CLUSTER_GEN} LIMIT 1",
                    (notebook_id, other, notebook_id)).fetchone()
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
    # T2 起唯一的调用方是 KnowledgeLifecycleService._compute_kg_analysis,它整个跑在
    # 发布写事务之外;T3 落地 KgAnalysisService 时要在服务层配断言(见 ports.py)。
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

    @contextmanager
    def read_snapshot(self) -> Iterator[Any]:
        """**给外部调用方**的多语句共享快照 —— `_read_snapshot(repeatable=True)`。

        与 SQLite 侧同名方法**语义等价**(那边是 `BEGIN DEFERRED` 起的 WAL 快照),
        存在的理由也逐字相同(codex 第 8 轮 P2):T3 的 `/sources` 一趟发四条语句,
        而 PostgreSQL 的 READ COMMITTED 是**每条语句**取一次快照 —— 并发的预计算提交在
        中间,新写入的画像行就会被盖上上一代账本的世代戳,`total` 与 `rows` 也可以互相
        对不上。REPEATABLE READ 把快照提到事务级,整块共享同一份库;`read_only=True`
        让「绝不写库」由引擎强制(这一条 SQLite 侧兑现不了,见那边的说明)。

        参数表**刻意为空**:调用方是后端中性的 service,不该知道「要不要 REPEATABLE
        READ」这种方言细节。内部单语句查询继续直接用 `_read_snapshot()`——那里多一层
        事务级快照只会白白拖住 xmin horizon。
        """
        with self._read_snapshot(repeatable=True) as db:
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
                f"AND c.generation = {_PUBLISHED_CLUSTER_GEN} "
                f"GROUP BY c.canonical_id "
                f"ORDER BY members DESC, c.canonical_id COLLATE \"C\" ASC LIMIT %s",
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
        以及开一次 REPEATABLE READ 只读事务 —— total 与板块明细必须看同一份库,否则
        并发 rebuild_communities 一劈就会出现「有 1200 个板块,一个都没有」这种自相
        矛盾的载荷(READ COMMITTED 每条语句各取一次快照)。

        ⚠ **一次调用只开一个快照**:两件事不能拆成两个 `with`,也不能让调用方为每条读
        各开一个 —— 那和没有快照一样坏,而且在 PG 上每个 `with` 还各借一条池连接
        (codex 第 8/12 轮 P2)。
        """
        self._reject_inside_write_transaction("主题板块列表")
        with self._read_snapshot(repeatable=True) as db:
            return self.community_overview_on(
                db, notebook_id, level=level, limit=limit, top_k=top_k
            )

    @staticmethod
    def community_overview_on(
        db: Any,
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

        本方法自己也是**两条语句**,所以调用方给的必须是 `read_snapshot()` 开出来的连接
        (REPEATABLE READ)而不是裸 `connect()`(READ COMMITTED 每条语句各取一次快照)。
        **不**在这里补一道 `_reject_inside_write_transaction`:骑调用方连接不存在「另借
        一条池连接读到提交前的库」这个失配,服务层入口另有一道同语义断言;而 staticmethod
        也够不到 `self.database`。

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
        level = int(level)
        limit = _clamp(limit, COMMUNITY_OVERVIEW_MAX)
        top_k = _clamp(top_k, COMMUNITY_TOP_MEMBERS_MAX)
        total = int(db.execute(
            f"SELECT COUNT(*) AS c FROM communities WHERE notebook_id=%s AND level=%s "
            f"AND generation = {_PUBLISHED_COMMUNITY_GEN}",
            (notebook_id, level, notebook_id)).fetchone()["c"])
        rows = db.execute(
            f"""
            WITH picked AS (
              SELECT id, size FROM communities
              WHERE notebook_id=%s AND level=%s
                AND generation = {_PUBLISHED_COMMUNITY_GEN}
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
                AND m.generation = {_PUBLISHED_COMMUNITY_GEN}
                AND m.community_id IN (SELECT id FROM picked)
            )
            SELECT p.id AS id, p.size AS size,
                   r.canonical_name AS canonical_name, r.rn AS rn
            FROM picked p
            LEFT JOIN ranked r ON r.community_id = p.id AND r.rn <= %s
            ORDER BY p.size DESC, p.id COLLATE "C" ASC, r.rn ASC
            """,
            (notebook_id, level, notebook_id, limit,
             notebook_id, level, notebook_id, top_k),
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

        与 SQLite 侧逐条对应(见那边的 docstring:**读账本只有这一个入口**,T3 的报告
        与预计算的新鲜度闸共用;「行在不在才是产物在不在」的判据)。``payload`` 这边是
        jsonb → psycopg 直接给
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
    def source_canonical_rows(db: Any, notebook_id: str):
        """来源画像的**唯一一次扫描**:``(source_id, canonical_id)`` 的逐对象游标。

        ⚠ **刻意没有 `community_members`,也没有 GROUP BY**(codex 第 13 轮 P1 的原子
        发布):板块与全部产物要在同一个写事务里一次出现,所以产物必须能在板块落库**之前**
        算出来,而那一刻板块划分只在内存里。canonical → 板块 这一跳交给
        `kg_analysis_precompute.SourceBoardCounter`。完整理由与「为什么结果逐字不变」
        见 SQLite 侧的 docstring。

        ``COALESCE(c.canonical_id, o.id)`` 与 `community_graph_rows` 的 canonical 口径
        逐字一致:没有 cluster_map 行的对象就是它自己的 canonical(社区正是建在那个口径
        的图上)。SQLite 侧同款。

        PostgreSQL 上这条退成一次 `knowledge_objects` 的顺扫 + 一次到 `concept_clusters`
        的 hash/nested-loop left join,**不再有** HashAggregate —— 旧形态那一步在 30 万行
        的实测计划里就已经溢盘(见 §3.2 的 T1 实测表)。分组改由调用方在列式缓冲里做,
        Python 侧每行只留两个 int32。

        口径与 SQLite 侧逐字一致:对象只算 `USABLE_STATUSES`;`source_id=''` 的对象
        整体排除(它们共享同一个空 source_id,算进去等于凭空造一个「空来源」的画像);
        隐藏合成来源(`source_type IN ('memory','knowhow')`)整体排除 —— 完整理由(排行
        被扭曲 + 内部标题泄漏 + 为什么排在预计算而不是读侧)见 SQLite 侧的 docstring。

        ⚠ 与那边同款,判据是 `NOT EXISTS`(「来源存在且类型隐藏」)而不是「join 不到就
        排除」:孤儿引用(`source_id` 指向已删来源)必须**留下**并在读侧标 `source_missing`,
        那是本视图有意的诊断能力。PG 会把相关 `NOT EXISTS` 提升成 anti-join(hash 还是
        nested-loop 由统计决定 —— 这里**不**断言某一种,本机测试库量级太小,拿它的
        `EXPLAIN` 当生产计划的证据是自欺);`sources` 那侧走主键。
        NULL 语义与 SQLite 一致 —— 在 NULL 上翻车的是 `NOT IN`(孤儿的 `s.source_type`
        为 NULL 时整个谓词变 NULL、行被静默滤掉),`NOT EXISTS` 不会。

        ⚠ **必须走 `cursor().stream()`,不能用 `db.execute()`**(codex 第 14 轮 P1)。
        psycopg3 的普通游标在 `execute()` 返回时就把**整个结果集**收进了 libpq 的
        `PGresult`——那是 C 层缓冲,`tracemalloc` 看不见它,只有 RSS 能。而本方法自第 13 轮
        原子发布起返回的是**逐对象**一行(去掉了 GROUP BY,因为板块那一刻还没落库),
        行数 = 可用对象数。

        本机真实 PG 库(`silicon_notebook_local_pg16_live`,93,127 行)独立子进程实测,
        每形态跑两遍:

            execute(客户端缓冲)   RSS +12.3 / +12.3 MB   ≈132 B/行
            cursor().stream()     RSS + 0.4 / + 0.2 MB   与行数无关

        ⚠ 那 132 B/行**不能**线性外推成生产结论(生产 878 万对象,是这里的 ~94 倍;
        本机也没有生产规模的 PG 库)。它证明的是**形态**:`execute` 随行数线性增长、
        `stream` 不增长。这正是「有界内存」在 PG 侧唯一站得住的形态。

        `stream()` 用 libpq 的单行模式,不需要 named cursor、不需要显式事务;
        游标从连接继承 `dict_row`,所以调用方按键取值的写法两侧一致。
        代价:整个迭代期间占住这条连接(调用方本就把循环包在一个 `_connect()` 里),
        且不能在 pipeline 模式下用(本仓库不用 pipeline)。
        """
        placeholders = ",".join("%s" for _ in USABLE_STATUSES)
        return db.cursor().stream(
            f"""
            SELECT o.source_id AS source_id,
                   COALESCE(c.canonical_id, o.id) AS canonical_id
            FROM knowledge_objects o
            LEFT JOIN concept_clusters c
                   ON c.notebook_id = o.notebook_id
                  AND c.member_object_id = o.id
                  AND c.generation = {_PUBLISHED_CLUSTER_GEN}
            WHERE o.notebook_id = %s
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

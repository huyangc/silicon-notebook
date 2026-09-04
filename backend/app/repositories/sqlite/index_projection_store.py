"""Scale-index database projections (Task 18).

Owns the read-only SQLite snapshots the scale/viz artifact domain consumes:
the O(1) version-signal probe, the five-table version-facts aggregates, the
effective-object / chunk counts, the source-id watermark reads, the delta
chunk counts, the gathered PPR graph rows and the direct embedding-matrix
loads used by the offline build/fold pipeline.

Composition rules (Gate 6): every read resolves the facade's ``_connect``
compatibility seam per call — connection spies, gated slow connections and
failure injections in the frozen suites keep observing every query.
``in_batches`` resolves the facade helper per call so the frozen ``_IN_CHUNK``
class patch keeps flowing into the batched IN clauses. The ent-chunk map /
mention-edge / vector-matrix snapshot providers stay facade-late callables
(they are retrieval-owned caches that move with their domain in Gate 7).
SQL text is moved verbatim; the version list FORMAT is unchanged (facts +
caller-appended settings tail), so on-disk manifest.version keeps matching.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, TYPE_CHECKING

from app.domain.knowledge_contracts import USABLE_STATUSES
from app.repositories.source_subgraph_projection import (
    source_graph_partition_rows_on,
    source_subgraph_rows_on,
    source_subgraph_signature_on,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy


def _binary_text_key(value: str) -> bytes:
    """Match SQLite BINARY collation for identifier ordering."""
    return value.encode("utf-8", "surrogatepass")

# The two frozen edge encodings the gathered graph produces: the default
# string path and the build-only int-indexed array fast path.
ScaleGraphEdges = (
    "list[tuple[str, str, float]]"
    " | tuple[numpy.ndarray, numpy.ndarray, numpy.ndarray]"
)

# Page size for the whole-notebook embedding keyset scan (embedding_matrix,
# object_ids=None). Each keyset page (rowid > last LIMIT this) is an independent
# statement: it bounds the native-dim BLOBs held on top of the preallocated
# matrix (the OOM fix), avoids per-row _DiagnosticCursor.__next__
# instrumentation, and releases the WAL read snapshot between pages so a long
# online build never blocks checkpoints. See embedding_matrix's docstring.
MATRIX_FETCH_BATCH = 10_000


def _stream_vector_rows(db, notebook_id: str, table: str, id_column: str,
                        connect=None, batch: int = MATRIX_FETCH_BATCH):
    """Keyset pagination by rowid, de-duplicated. Each page is an INDEPENDENT,
    fully-exhausted statement, so the WAL read snapshot is released between
    pages — a single long-lived cursor would pin ONE snapshot for the whole
    multi-million-row build and block WAL checkpoints under concurrent writes
    → unbounded `-wal` growth. But rowid is NOT stable across the scan: the
    embedding writers use INSERT OR REPLACE (embedding_store.py), which
    deletes+reinserts a refreshed row at a LARGER rowid. A row refreshed after
    the scan passed its old rowid would re-surface in a later page, and
    build_matrix would then hold BOTH the stale and the current vector under
    one id. `seen` keeps the FIRST occurrence (the value at the id's original
    scan position — snapshot-consistent; the refreshed vector is picked up by
    the next fold/rebuild) and drops any re-surfacing, so every id enters the
    matrix exactly once. `rowid > ?` walks idx_{table}_nb in rowid order.
    n_hint (a COUNT at start) is an estimate; build_matrix tolerates the drift
    dedup and concurrent writes introduce.

    Shared verbatim by ``embedding_matrix``'s whole-notebook load and
    ``embedding_pages``' paged ANN feed, so the two can never drift apart on
    the de-dup rule (a second copy of this scan is exactly how one of them
    would quietly lose it).

    ``connect`` (batch-3 W4 T-W4-3.3 double-review fix C) says who owns the
    connection. Default (None): every page runs on the caller's ``db``, which
    therefore stays open for the whole scan. Passing a connection factory
    opens and closes one PER page instead (``db`` unused) — what the ANN feed
    needs, because it interleaves hnswlib index construction between pages and
    must not hold a connection (on the PostgreSQL sibling, a pool slot) across
    that. Safe because the only cross-page state — ``seen`` and
    ``last_rowid`` — already lives in Python, and each page was already an
    independent statement.
    """
    from contextlib import nullcontext

    seen: set = set()
    last_rowid = 0
    while True:
        with (nullcontext(db) if connect is None else connect()) as page_db:
            page = page_db.execute(
                f"SELECT rowid AS rid, {id_column} AS vid, vector "
                f"FROM {table} WHERE notebook_id=? AND rowid > ? "
                f"ORDER BY rowid LIMIT ?",
                (notebook_id, last_rowid, batch),
            ).fetchall()
        if not page:
            break
        for row in page:
            vid = row["vid"]
            if vid in seen:
                continue
            seen.add(vid)
            yield vid, row["vector"]
        last_rowid = page[-1]["rid"]
        if len(page) < batch:
            break


@dataclass(frozen=True)
class ScaleGraphRows:
    node_ids: list
    edges: "ScaleGraphEdges"
    chunk_ids: list
    kg_node_ids: list
    membership_counts: dict


class IndexProjectionStore:
    def __init__(
        self,
        settings,
        *,
        connect: Callable,
        in_batches: Callable,
        ent_chunk_map: Callable,
        mention_extra_edges: Callable,
        vector_matrix: Callable,
    ) -> None:
        self.settings = settings
        self.connect = connect
        self.in_batches = in_batches
        self.ent_chunk_map = ent_chunk_map
        self.mention_extra_edges = mention_extra_edges
        self.vector_matrix = vector_matrix

    def bind_runtime_callbacks(
        self,
        *,
        connect: Callable,
        in_batches: Callable,
        ent_chunk_map: Callable,
        mention_extra_edges: Callable,
        vector_matrix: Callable,
    ) -> None:
        self.connect = connect
        self.in_batches = in_batches
        self.ent_chunk_map = ent_chunk_map
        self.mention_extra_edges = mention_extra_edges
        self.vector_matrix = vector_matrix

    # ────────────────────────────────────────────────── version snapshots ──
    def version_signal(self, notebook_id: str) -> "tuple[int, int, tuple, int]":
        """Cheap probe: (seq, cseq, settings_tail, kg_reset_epoch) — a single
        unified_kg_state row read, no table aggregates. runtime_dim /
        mention_seq fold into the settings tail exactly as before (they flow
        through version_facts' caller into the on-disk manifest.version
        comparison). kg_reset_epoch is a SEPARATE, trailing element — never
        folded into settings_tail (design doc batch-3-W1 Sec 3.4: that would
        put it inside the settings_tail component consumers already unpack by
        position). It MUST stay LAST: three call sites read
        ``version_signal(nb)[1]`` for cseq (scale_artifact_runtime.py,
        scale_index_builder.py) and appending anywhere but the end would
        silently shift what they read."""
        from app.domain.vector_index import resolve_runtime_dim
        settings_tail = (
            self.settings.ppr_variant_edge_weight,
            self.settings.ppr_emb_synonym_enabled,
            self.settings.ppr_emb_synonym_threshold,
            self.settings.ppr_emb_synonym_topk,
            resolve_runtime_dim(self.settings),
            self.settings.mention_bridge_enabled,
            self.settings.mention_edge_weight,
        )
        with self.connect() as db:
            st = db.execute(
                "SELECT kg_mutation_seq,cluster_mutation_seq,mention_seq,"
                "indexing_pipeline_id,indexing_pipeline_version,kg_reset_epoch "
                "FROM unified_kg_state WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()
            seq = int(st["kg_mutation_seq"]) if st else 0
            cseq = int(st["cluster_mutation_seq"]) if st else 0
            mseq = int(st["mention_seq"]) if (st and st["mention_seq"] is not None) else -1
            pipeline_id = str(st["indexing_pipeline_id"] or "") if st else ""
            pipeline_version = (
                str(st["indexing_pipeline_version"] or "builtin.chunk.v1")
                if st else "builtin.chunk.v1"
            )
            epoch = int(st["kg_reset_epoch"]) if (st and st["kg_reset_epoch"] is not None) else 0
        return seq, cseq, settings_tail + (mseq, pipeline_id, pipeline_version), epoch

    def pipeline_identity(self, notebook_id: str) -> tuple[str, str]:
        with self.connect() as db:
            row = db.execute(
                "SELECT indexing_pipeline_id,indexing_pipeline_version "
                "FROM unified_kg_state WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()
        if row is None:
            return "", "builtin.chunk.v1"
        return (
            str(row["indexing_pipeline_id"] or ""),
            str(row["indexing_pipeline_version"] or "builtin.chunk.v1"),
        )

    def version_facts(self, notebook_id: str) -> list:
        """Cold-path five-table COUNT/MAX aggregates. Returns the version-list
        facts WITHOUT the settings tail — the caller appends the tail probed
        in the same memoization window, keeping the frozen list format (and
        on-disk manifest.version compatibility) byte-identical."""
        with self.connect() as db:
            obj_ver = db.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at),'') AS ts "
                "FROM knowledge_objects WHERE notebook_id=?", (notebook_id,)).fetchone()
            rel_ver = db.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
                "FROM knowledge_relations WHERE notebook_id=?", (notebook_id,)).fetchone()
            chunk_ver = db.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
                "FROM chunks WHERE notebook_id=?", (notebook_id,)).fetchone()
            # 批 3·W2 §1.4 红线「版本身份不得被未发布代污染」(PG 孪生同注释)。
            clu_ver = db.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
                "FROM concept_clusters WHERE notebook_id=? "
                "AND generation = COALESCE((SELECT cluster_generation "
                "FROM unified_kg_state WHERE notebook_id = ?), 0)",
                (notebook_id, notebook_id)).fetchone()
            emb_ver = db.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
                "FROM knowledge_embeddings WHERE notebook_id=?", (notebook_id,)).fetchone()
            state = db.execute(
                "SELECT indexing_pipeline_id,indexing_pipeline_version "
                "FROM unified_kg_state WHERE notebook_id=?", (notebook_id,)
            ).fetchone()
        return [
            notebook_id,
            int(obj_ver["c"]), obj_ver["ts"],
            int(rel_ver["c"]), rel_ver["ts"],
            int(chunk_ver["c"]), chunk_ver["ts"],
            int(clu_ver["c"]), clu_ver["ts"],
            int(emb_ver["c"]), emb_ver["ts"],
            "indexing_pipeline",
            str(state["indexing_pipeline_id"] or "") if state else "",
            str(state["indexing_pipeline_version"] or "builtin.chunk.v1")
            if state else "builtin.chunk.v1",
        ]

    def version_with_settings(self, notebook_id: str, settings_tail: tuple) -> list:
        return self.version_facts(notebook_id) + list(settings_tail)

    # ─────────────────────────────────────────────────── count snapshots ──
    def effective_object_count(self, notebook_id: str) -> int:
        """Non-deprecated knowledge-object count (viz sync-vs-background gate)."""
        from app.repositories.sqlite import knowledge_counts_cache
        with self.connect() as db:
            return knowledge_counts_cache.active_object_count(db, notebook_id)

    def total_chunk_count(self, notebook_id: str) -> int:
        # Seq-gated memo: the chunks COUNT fires on every /scale-index/status
        # (i.e. every notebook open) and is cold-page-bound at millions of rows.
        from app.repositories.sqlite import knowledge_counts_cache
        with self.connect() as db:
            return knowledge_counts_cache.chunk_count(db, notebook_id)

    def is_mounted_by_anyone(self, notebook_id: str) -> bool:
        """被任何笔记本当作参考库挂着(Task 6:scale eligible() 的挂载分支)——
        不区分挂载边是否仍然「有效」,故意不走 mount_sql.py 的 MOUNT_VALID_EXPR:
        那个谓词是解析参与集用的(边失效是可恢复的临时态),这里问的是「该不该为它
        投入建索引的成本」,答案不该随边的有效性瞬时抖动。ScaleArtifactRuntime.eligible
        消费;QueryStore.is_mounted_by_anyone 是 NotebookScaleProfile.index_eligible
        侧的镜像实现,两处必须保持同一判定。"""
        with self.connect() as db:
            return bool(db.execute(
                "SELECT EXISTS(SELECT 1 FROM notebook_bases WHERE base_notebook_id=?)",
                (notebook_id,),
            ).fetchone()[0])

    def source_ids(self, notebook_id: str) -> List[str]:
        with self.connect() as db:
            return [r["id"] for r in db.execute(
                "SELECT id FROM sources WHERE notebook_id=?", (notebook_id,)).fetchall()]

    def chunk_sources_for_ids(
        self, notebook_id: str, chunk_ids: Sequence[str]
    ) -> Dict[str, str]:
        """Return the row-aligned ANN source identity for a bounded id page."""
        if not chunk_ids:
            return {}
        out: Dict[str, str] = {}
        with self.connect() as db:
            for batch in self.in_batches(chunk_ids):
                placeholders = ",".join("?" for _ in batch)
                rows = db.execute(
                    "SELECT id,source_id FROM chunks WHERE notebook_id=? "
                    f"AND id IN ({placeholders})",
                    (notebook_id, *batch),
                ).fetchall()
                out.update((row["id"], row["source_id"]) for row in rows)
        return out

    def visible_source_ids(
        self, notebook_id: str, source_ids: List[str]
    ) -> List[str]:
        if not source_ids:
            return []
        visible = set()
        with self.connect() as db:
            for batch in self.in_batches(source_ids):
                placeholders = ",".join("?" for _ in batch)
                rows = db.execute(
                    "SELECT id FROM sources WHERE notebook_id=? "
                    "AND source_type NOT IN ('memory','knowhow') "
                    f"AND id IN ({placeholders})",
                    (notebook_id, *batch),
                ).fetchall()
                visible.update(row["id"] for row in rows)
        return [source_id for source_id in source_ids if source_id in visible]

    # The retrieval-scope universe reads live on ``SourceStore``
    # (``all_visible_source_ids`` / ``hidden_source_ids``).  A second copy here
    # would be a second spelling of "whose Memory may enter a ceiling", and the
    # freeze and the retrieval drift probe would be free to disagree.

    def notebook_owner(self, notebook_id: str) -> "str | None":
        with self.connect() as db:
            row = db.execute(
                "SELECT created_by FROM notebooks WHERE id = ?", (notebook_id,)
            ).fetchone()
        return row["created_by"] if row else None

    def notebook_tier(self, notebook_id: str) -> "str | None":
        """Cheap PK read of just the tier column — for hot status polls that
        need only tier and must not rebuild the full NotebookSummary (from_row)."""
        with self.connect() as db:
            row = db.execute(
                "SELECT tier FROM notebooks WHERE id = ?", (notebook_id,)
            ).fetchone()
        return row["tier"] if row else None

    def notebook_name(self, notebook_id: str) -> str:
        with self.connect() as db:
            row = db.execute(
                "SELECT name FROM notebooks WHERE id = ?", (notebook_id,)
            ).fetchone()
        return row["name"] if row else ""

    def unified_last_rebuild_at(self, notebook_id: str) -> str:
        with self.connect() as db:
            row = db.execute(
                "SELECT last_rebuild_at FROM unified_kg_state WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()
        return str(row["last_rebuild_at"]) if row and row["last_rebuild_at"] else ""

    def delta_chunk_count(self, notebook_id: str, source_ids: Sequence[str]) -> int:
        """Chunk count over the given (post-watermark) sources, batched through
        the facade's IN-clause chunking (SQLite variable-count safe)."""
        nchunks = 0
        with self.connect() as db:
            for batch in self.in_batches(source_ids):
                ph = ",".join("?" for _ in batch)
                nchunks += db.execute(
                    f"SELECT COUNT(*) c FROM chunks WHERE notebook_id=? AND source_id IN ({ph})",
                    (notebook_id, *batch)).fetchone()["c"]
        return int(nchunks)

    def relation_ids_for_source_batch(
        self, db, notebook_id: str, source_ids: Sequence[str]
    ) -> list[str]:
        placeholders = ",".join("?" for _ in source_ids)
        return [
            row["id"]
            for row in db.execute(
                "SELECT id FROM knowledge_relations "
                f"WHERE notebook_id=? AND source_id IN ({placeholders})",
                (notebook_id, *source_ids),
            ).fetchall()
        ]

    def active_object_graph_rows(self, db, notebook_id: str) -> list:
        return db.execute(
            "SELECT id, object_type, json_extract(payload,'$.name') AS name "
            "FROM knowledge_objects "
            "WHERE notebook_id=? AND status!='deprecated'",
            (notebook_id,),
        ).fetchall()

    def source_subgraph_signature(
        self, notebook_id: str, source_ids: Sequence[str]
    ) -> tuple:
        allowed = tuple(sorted(set(source_ids)))
        if not allowed:
            return (0, 0, 0, ())
        with self.connect() as db:
            return source_subgraph_signature_on(
                db,
                notebook_id,
                allowed,
                placeholder="?",
                postgres=False,
            )

    def source_subgraph_rows(
        self,
        notebook_id: str,
        source_ids: Sequence[str],
        limits: Mapping[str, int],
    ) -> Mapping[str, Any]:
        allowed = tuple(sorted(set(source_ids)))
        if not allowed:
            return {
                "signature": (0, 0, 0, ()),
                "kg_generation": 0,
                "cluster_generation": 0,
                "sources": [],
                "objects": [],
                "relations": [],
                "chunks": [],
                "facts": [],
                "fact_elements": [],
                "clusters": [],
                "reasons": ["empty_source_scope"],
            }
        with self.connect() as db:
            db.execute("BEGIN")
            return source_subgraph_rows_on(
                db,
                notebook_id,
                allowed,
                limits,
                placeholder="?",
                postgres=False,
            )

    def source_graph_partition_rows(
        self, notebook_id: str, source_id: str
    ) -> Mapping[str, Any]:
        """Offline source-local rows for the partitioned scale artifact."""
        with self.connect() as db:
            db.execute("BEGIN")
            return source_graph_partition_rows_on(
                db,
                notebook_id,
                source_id,
                placeholder="?",
                postgres=False,
                limits={
                    "objects": self.settings.source_subgraph_max_objects,
                    "relations": self.settings.source_subgraph_max_relations,
                    "chunks": self.settings.source_subgraph_max_chunks,
                    "facts": self.settings.source_subgraph_max_facts,
                    "fact_elements": self.settings.source_subgraph_max_fact_elements,
                    "cluster_memberships": self.settings.source_subgraph_max_cluster_memberships,
                },
            )

    # ─────────────────────────────────────────────────── graph snapshots ──
    def graph_rows(
        self,
        notebook_id: str,
        source_ids: "Sequence[str] | None",
        *,
        synonym_edges=None,
        as_arrays: bool = False,
    ) -> ScaleGraphRows:
        """Gather all KG nodes/relations/chunks/cluster_groups for a notebook
        and build the undirected edge set used by both build_scale_index and
        _active_kg_delta.  Single source of truth — no duplicated _add_undirected.

        source_ids : None (default) = whole notebook, byte-identical to the
        pre-scoping behaviour.  A list = only objects/relations/chunks from those
        sources (delta domain); memberships limited to gathered objects; the
        variant/synonym extra_edges are skipped (delta is small, connectivity
        comes from relations/cluster-hub/cross-layer bridges).  An empty list
        returns ([], [], [], [], {}) (as_arrays=True: ([], (empty arrays), [], [], {})).

        synonym_edges : None (default) = current behaviour — this method loads
        the KG embedding matrix itself and calls emb_synonym_edges() internally
        (whole-notebook / unscoped path only). Pass a pre-computed
        [(id_a,id_b,sim), ...] list (e.g. from build_scale_index, which already
        loaded the matrix and built the hnsw index once for both the ann.bin
        persist AND the synonym KNN) to SKIP the internal matrix load + KNN
        call entirely and merge the given edges into extra_edges instead —
        avoids doing the single most expensive build step (hnsw construction)
        twice. Only meaningful when source_ids is None; scoped/delta callers
        never pass this (variant/synonym edges are already skipped when scoped).

        as_arrays : False (default) = current behaviour, edges as a Python
        list of (str,str,float) tuples plus a `seen_undir` tuple set for
        dedup — byte-identical to before, used by every existing caller
        (_active_kg_delta, scoped/delta paths, etc). True (build_scale_index
        only) = same node_ids, but edges come back as three int32/int32/
        float32 numpy arrays (src_idx, tgt_idx, w) already encoded against
        `index = {nid: i for i, nid in enumerate(node_ids)}`, deduped via an
        encoded-pair-key np.unique instead of a Python tuple set — avoids the
        ~5GB of Python-object overhead a 10M+-edge notebook's `edges` list +
        `seen_undir` set costs. Dedup keeps the FIRST-seen weight for a given
        undirected pair, matching the string path's `if key in seen_undir:
        return` short-circuit (relations → memberships → extra_edges →
        cluster-hub insertion order, exactly as below). Both directions are
        present in the output arrays, self-loops are dropped, and cluster hub
        ids are appended to node_ids BEFORE edge assembly (so the index dict
        is complete for one array-encoding pass instead of growing mid-way).

        Returns ScaleGraphRows with
          node_ids          : list[str]  — kg node ids + chunk_ids + cluster hub ids
          edges             : list[(str,str,float)] — undirected (both dirs, deduped)
                               OR (as_arrays=True) (src:int32[], tgt:int32[], w:float32[])
          chunk_ids         : list[str]  — raw chunk ids (stable subset of node_ids)
          kg_node_ids       : list[str]  — KG object ids (for idf / n_kg_nodes)
          membership_counts : dict[str,int] — {object_id: len(chunks)} for IDF
        """
        from app.domain.kg.edge_schema import is_queryable_edge_pair
        from app.domain.kg.ppr_pairs import variant_edge_pairs, emb_synonym_edges
        import numpy as np

        ph = ",".join("?" for _ in USABLE_STATUSES)
        scoped = source_ids is not None
        if scoped and not source_ids:
            if as_arrays:
                empty = np.empty(0, dtype=np.int32)
                return ScaleGraphRows(
                    [], (empty, empty, np.empty(0, dtype=np.float32)), [], [], {})
            return ScaleGraphRows([], [], [], [], {})
        clauses = [("", ())]
        if scoped:
            clauses = [
                (f" AND source_id IN ({','.join('?' for _ in b)})", tuple(b))
                for b in self.in_batches(source_ids)
            ]
        kg_nodes: Dict[str, dict] = {}
        relations: list = []
        chunk_ids: list = []
        cluster_groups: Dict[str, list] = {}

        # These four gathers stay WHOLE-TABLE `.fetchall()`s while the
        # PostgreSQL adapter's equivalents read in keyset pages (batch-3 W4
        # T-W4-3.1). The reason is scope, not a SQLite hazard: WR-9's 8M-row
        # target is a PostgreSQL deployment, and every key the paging design
        # leans on — `uq_knowledge_objects_ordinal` / `uq_chunks_ordinal` as
        # globally-unique single-column cursors, `idx_clusters_nb_canonical_
        # member_gen`'s INCLUDE(generation) Index-Only Scan — is a PostgreSQL
        # fact. Paging here would have to be designed against SQLite's own
        # indexes and measured on SQLite, for a backend that is not the one
        # running at that scale, so it is deliberately out of scope.
        # NOT the reason (an earlier draft of this ledger said so and was
        # wrong): rowid instability under `INSERT OR REPLACE`. That hazard is
        # real for the EMBEDDING tables — hence `_stream_vector_rows`' `seen`
        # set — but `knowledge_objects` / `chunks` / `knowledge_relations` are
        # written with plain `INSERT` and carry TEXT primary keys, so a rowid
        # (or id) cursor over them would be perfectly stable.
        with self.connect() as db:
            for src_clause, src_params in clauses:
                for r in db.execute(
                        f"SELECT id, object_type, payload FROM knowledge_objects "
                        f"WHERE notebook_id=? AND status IN ({ph}){src_clause} "
                        f"ORDER BY rowid, id",
                        (notebook_id, *USABLE_STATUSES, *src_params)).fetchall():
                    kg_nodes[r["id"]] = {
                        "type": r["object_type"],
                        "name": json.loads(r["payload"] or "{}").get("name", ""),
                    }
            for src_clause, src_params in clauses:
                for r in db.execute(
                        f"SELECT source_object_id, target_object_id, edge_type FROM knowledge_relations "
                        f"WHERE notebook_id=? AND review_status!='rejected'{src_clause} "
                        f"ORDER BY id",
                        (notebook_id, *src_params)).fetchall():
                    relation = dict(r)
                    source = kg_nodes.get(relation["source_object_id"])
                    target = kg_nodes.get(relation["target_object_id"])
                    if source and target and is_queryable_edge_pair(
                        relation["edge_type"], source["type"], target["type"]
                    ):
                        relations.append(relation)
            for src_clause, src_params in clauses:
                for r in db.execute(
                        f"SELECT id FROM chunks WHERE notebook_id=?{src_clause} ORDER BY rowid, id",
                        (notebook_id, *src_params)).fetchall():
                    chunk_ids.append(r["id"])
            for r in db.execute(
                    "SELECT canonical_id, member_object_id FROM concept_clusters "
                    "WHERE notebook_id=? "
                    "AND generation = COALESCE((SELECT cluster_generation "
                    "FROM unified_kg_state WHERE notebook_id = ?), 0) "
                    "ORDER BY canonical_id, member_object_id",
                    (notebook_id, notebook_id)).fetchall():
                cluster_groups.setdefault(r["canonical_id"], []).append(r["member_object_id"])

        # Memberships: entity ↔ chunk (scoped → limit to gathered objects).
        # `paged=True` mirrors the PostgreSQL adapter's gather so one seam
        # serves both; on this backend the paged evidence entry point resolves
        # to the same single statement (see the ledger above).
        ent_chunk_map = self.ent_chunk_map(notebook_id, paged=True)
        _kg_keys = set(kg_nodes.keys())
        membership_object_ids = sorted(
            (
                oid
                for oid in ent_chunk_map
                if not scoped or oid in _kg_keys
            ),
            key=_binary_text_key,
        )
        memberships = [
            (oid, cid)
            for oid in membership_object_ids
            for cid in sorted(ent_chunk_map[oid], key=_binary_text_key)
        ]
        membership_counts: Dict[str, int] = {
            oid: len(ent_chunk_map[oid]) for oid in membership_object_ids
        }
        del ent_chunk_map, _kg_keys, membership_object_ids

        # Extra edges: variant pairs + optional synonym pairs (whole-notebook only)
        extra_edges = []
        if not scoped:
            extra_edges = variant_edge_pairs(kg_nodes, self.settings.ppr_variant_edge_weight)
            if synonym_edges is not None:
                # Caller (build_scale_index) already computed these — reusing
                # the SAME hnsw build it also persists as ann.bin, instead of
                # this method loading the matrix + building hnsw again.
                extra_edges = extra_edges + list(synonym_edges)
            else:
                with self.connect() as db:
                    ann_ids_raw, ann_matrix_raw = self.vector_matrix(
                        db, notebook_id, "knowledge_embeddings", "object_id")
                ann_ids: list = list(ann_ids_raw) if ann_ids_raw else []
                has_vecs = bool(ann_ids) and ann_matrix_raw is not None and len(ann_matrix_raw)
                if has_vecs and self.settings.ppr_emb_synonym_enabled:
                    extra_edges = extra_edges + emb_synonym_edges(
                        ann_ids, np.asarray(ann_matrix_raw),
                        self.settings.ppr_emb_synonym_threshold,
                        self.settings.ppr_emb_synonym_topk,
                        self.settings.ppr_emb_synonym_max_entities,
                        ef_construction=self.settings.hnsw_ef_construction,
                    )
            # P2 共提桥:scale CSR 节点空间含 cluster router(下方 hub 装配),故 claim↔cluster
            # 软边同样适用;仅 whole-notebook(not scoped)路径追加,与 variant/synonym 一致。
            extra_edges = extra_edges + self.mention_extra_edges(notebook_id)

        # node_ids: kg nodes first, then chunk nodes, then cluster hubs.
        # Hubs are pre-appended HERE (before edge assembly) rather than
        # discovered while walking cluster_groups interleaved with
        # _add_undirected calls — final node_ids content/order is identical
        # either way (hub eligibility only depends on `members` vs the
        # kg+chunk node_ids_set, never on edges), but the array path needs a
        # COMPLETE id→index map before it can encode any edge, so both paths
        # share this single hub-discovery pass.
        node_ids: list = list(kg_nodes.keys()) + chunk_ids
        node_ids_set: set = set(node_ids)
        hub_members: List[Tuple[str, str]] = []  # (hub_id, member_id) pairs to wire as edges below
        for canonical_id, members in cluster_groups.items():
            present = [m for m in members if m in node_ids_set]
            if not present:
                continue
            hub_id = f"cluster:{canonical_id}"
            if hub_id not in node_ids_set:
                node_ids.append(hub_id)
                node_ids_set.add(hub_id)
            for m in present:
                hub_members.append((hub_id, m))
        del cluster_groups

        if as_arrays:
            index = {nid: i for i, nid in enumerate(node_ids)}
            n = len(node_ids)
            # Collect all directed (a,b,w) contributions in encoding order —
            # relations → memberships → extra_edges → hub_members — same
            # precedence order as the string path's _add_undirected calls,
            # so first-seen-wins dedup below picks the same winning weight.
            a_list: List[int] = []
            b_list: List[int] = []
            w_list: List[float] = []
            for rel in relations:
                sa = index.get(rel["source_object_id"])
                sb = index.get(rel["target_object_id"])
                if sa is not None and sb is not None and sa != sb:
                    a_list.append(sa); b_list.append(sb); w_list.append(1.0)
            del relations
            for oid, cid in memberships:
                sa = index.get(oid); sb = index.get(cid)
                if sa is not None and sb is not None and sa != sb:
                    a_list.append(sa); b_list.append(sb); w_list.append(1.0)
            del memberships
            for a, b, w in extra_edges:
                sa = index.get(a); sb = index.get(b)
                if sa is not None and sb is not None and sa != sb:
                    a_list.append(sa); b_list.append(sb); w_list.append(float(w))
            del extra_edges
            for hub_id, m in hub_members:
                sa = index.get(hub_id); sb = index.get(m)
                if sa is not None and sb is not None and sa != sb:
                    a_list.append(sa); b_list.append(sb); w_list.append(1.0)
            del hub_members

            if not a_list:
                empty = np.empty(0, dtype=np.int32)
                kg_node_ids: list = list(kg_nodes.keys())
                return ScaleGraphRows(
                    node_ids, (empty, empty, np.empty(0, dtype=np.float32)),
                    chunk_ids, kg_node_ids, membership_counts)

            a_arr = np.asarray(a_list, dtype=np.int64)
            b_arr = np.asarray(b_list, dtype=np.int64)
            w_arr = np.asarray(w_list, dtype=np.float64)
            del a_list, b_list, w_list
            # Undirected dedup: canonical (lo,hi) pair encoded as one int64 key
            # (lo*n + hi); np.unique(..., return_index=True) keeps the FIRST
            # occurrence of each key — matches the string path's `if key in
            # seen_undir: return` (first-wins) semantics exactly, given the
            # same relations→memberships→extra→hub encoding order above.
            lo = np.minimum(a_arr, b_arr)
            hi = np.maximum(a_arr, b_arr)
            keys = lo * n + hi
            _, first_idx = np.unique(keys, return_index=True)
            first_idx.sort()  # np.unique sorts by key value, not first-seen order — restore it
            src_u = a_arr[first_idx]
            tgt_u = b_arr[first_idx]
            w_u = w_arr[first_idx]
            del a_arr, b_arr, w_arr, lo, hi, keys, first_idx

            # Emit both directions (src->tgt and tgt->src), matching the
            # string path's edges.append((a,b,w)); edges.append((b,a,w)).
            src_final = np.concatenate([src_u, tgt_u]).astype(np.int32, copy=False)
            tgt_final = np.concatenate([tgt_u, src_u]).astype(np.int32, copy=False)
            w_final = np.concatenate([w_u, w_u]).astype(np.float32, copy=False)
            del src_u, tgt_u, w_u

            kg_node_ids = list(kg_nodes.keys())
            return ScaleGraphRows(
                node_ids, (src_final, tgt_final, w_final),
                chunk_ids, kg_node_ids, membership_counts)

        # Build undirected edges (single _add_undirected closure, shared state)
        edges: List[Tuple[str, str, float]] = []
        seen_undir: set = set()

        def _add_undirected(a: str, b: str, w: float) -> None:
            if a == b:
                return
            key = (a, b) if a < b else (b, a)
            if key in seen_undir:
                return
            seen_undir.add(key)
            edges.append((a, b, w))
            edges.append((b, a, w))

        for rel in relations:
            _add_undirected(rel["source_object_id"], rel["target_object_id"], 1.0)
        for oid, cid in memberships:
            _add_undirected(oid, cid, 1.0)
        for a, b, w in extra_edges:
            _add_undirected(a, b, w)
        for hub_id, m in hub_members:
            _add_undirected(hub_id, m, 1.0)

        kg_node_ids: list = list(kg_nodes.keys())
        return ScaleGraphRows(node_ids, edges, chunk_ids, kg_node_ids, membership_counts)

    # ────────────────────────────────────────────────── vector snapshots ──
    def embedding_matrix(
        self,
        notebook_id: str,
        table: str,
        id_column: str,
        object_ids: "Optional[Sequence[str]]" = None,
    ):
        """Direct embedding-matrix load for the offline build/fold pipeline.

        object_ids=None = whole-notebook load with a COUNT(*) n_hint so
        build_matrix preallocates (the frozen build-scale memory diet) —
        deliberately BYPASSES the query-time vector cache: a build's multi-GB
        matrices must never become LRU entries. Rows are read in bounded keyset
        pages (rowid > last LIMIT MATRIX_FETCH_BATCH), each an independent
        fully-exhausted statement — never .fetchall()'d whole, never iterated
        row-by-row, never a single long-lived streaming cursor. Four properties
        are load-bearing at 8M-row scale:
          • Memory: the whole-table result set is ~130GB of native BLOBs at
            8M×4096-dim; materialising it alongside the preallocated (already
            truncated) matrix is exactly what OOM-killed the offline `index`
            build (relation load: ~130GB transient on top of the ~78GB KG/chunk
            residents → ~208GB). A page bounds the raw BLOBs held on top of the
            output matrix to MATRIX_FETCH_BATCH rows.
          • CPU/lock: _DiagnosticCursor.__next__ is instrumented, so row-by-row
            iteration would enter sql_scope for EVERY embedding — on the online
            build path (diagnostics runtime installed) that is normalize-SQL +
            the global diagnostics lock twice + a writer wake per row, tens of
            millions of times. A page pays that once, then iterates a plain
            list (native, no instrumented __next__).
          • WAL: each page's statement is exhausted before the next begins, so
            no read snapshot spans the whole build. A single streaming cursor
            would pin one snapshot for the (hours-long) build, blocking WAL
            checkpoints while concurrent writes append frames → unbounded `-wal`
            growth. build_matrix's n_hint tolerates the row-count drift a
            cross-page concurrent write may cause.
          • Consistency: rowid is NOT stable across pages — INSERT OR REPLACE
            re-embeds delete+reinsert a refreshed row at a larger rowid, so a
            row refreshed after the scan passed it re-surfaces in a later page.
            _stream_rows de-duplicates by id (keeps the first occurrence), so an
            id never lands in the ANN twice (stale + current); without it the
            persisted index would silently carry duplicate/stale entries under
            an up-to-date manifest.
        The connection stays open across the paged consumption because the
        return runs inside the `with`. A list = fold's bounded delta load,
        batched through the facade's IN-clause chunking with the connection
        held open across the generator (frozen `_delta_vecs` shape); an empty
        list returns ([], []). Both paths truncate through
        build_matrix(runtime_dim=...) — the dim-consumption point moves here
        UNCHANGED (漏消费点 = 静默零召回)."""
        from app.domain.vector_index import build_matrix, resolve_runtime_dim
        runtime_dim = resolve_runtime_dim(self.settings)
        if object_ids is None:
            with self.connect() as db:
                n_hint = db.execute(
                    f"SELECT COUNT(*) AS c FROM {table} WHERE notebook_id=?",
                    (notebook_id,)).fetchone()["c"]
                return build_matrix(
                    _stream_vector_rows(db, notebook_id, table, id_column),
                    n_hint=n_hint, runtime_dim=runtime_dim)
        if not object_ids:
            return [], []

        def _rows():
            with self.connect() as db:
                for batch in self.in_batches(object_ids):
                    ph = ",".join("?" for _ in batch)
                    for r in db.execute(
                            f"SELECT {id_column} AS vid, vector FROM {table} "
                            f"WHERE notebook_id=? AND {id_column} IN ({ph})",
                            (notebook_id, *batch)).fetchall():
                        yield r["vid"], r["vector"]
        return build_matrix(_rows(), runtime_dim=runtime_dim)

    def embedding_row_count(self, notebook_id: str, table: str) -> int:
        """Row count for the whole-notebook embedding scan — the UPPER BOUND
        the paged ANN build sizes ``init_index(max_elements=...)`` with before
        it knows how many rows will actually survive decoding (and, on this
        backend, the rowid de-dup). Same aggregate ``embedding_matrix``
        already uses for ``build_matrix``'s ``n_hint``."""
        with self.connect() as db:
            return int(db.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE notebook_id=?",
                (notebook_id,)).fetchone()["c"])

    def embedding_pages(self, notebook_id: str, table: str, id_column: str,
                        page_rows: int = MATRIX_FETCH_BATCH):
        """Whole-notebook vectors as bounded ``(ids, matrix)`` pages.

        The load-side half of ``embedding_matrix(object_ids=None)`` without
        its one whole-notebook matrix: the DB read is the SAME
        ``_stream_vector_rows`` rowid scan (same de-dup, same WAL ledger), and
        ``matrix_pages`` applies ``build_matrix``'s five semantics across the
        whole stream, so concatenating the pages is element-identical to
        ``embedding_matrix``. hnswlib copies each page into its own graph, so
        the caller drops the page right after ``add_items`` instead of holding
        the whole matrix alongside the index.

        A GENERATOR that opens and closes a connection PER DB page
        (``_stream_vector_rows(connect=...)``) rather than holding one across
        its whole consumption: the consumer builds an hnsw index between
        pages, and a connection held across that is a pool slot (PostgreSQL
        sibling) or a pinned read snapshot (here) for the entire build. The
        page loop's only cross-page state is Python-side, so the de-dup and
        WAL ledgers above are unaffected.
        """
        from app.domain.vector_index import matrix_pages, resolve_runtime_dim
        runtime_dim = resolve_runtime_dim(self.settings)
        yield from matrix_pages(
            _stream_vector_rows(
                # batch=page_rows (codex #676 R10 P2): bound the raw rowid
                # scan too, not only the decoded output pages.
                None, notebook_id, table, id_column, connect=self.connect,
                batch=page_rows,
            ),
            page_rows,
            runtime_dim=runtime_dim,
        )

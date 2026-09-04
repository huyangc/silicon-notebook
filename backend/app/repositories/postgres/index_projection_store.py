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

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, TYPE_CHECKING

from app.domain.knowledge_contracts import USABLE_STATUSES
from app.repositories.postgres._store_utils import (
    iso_timestamp,
    json_value,
    keyset_pages as _keyset_pages,
)
from app.repositories.postgres.embedding_store import (
    MATRIX_FETCH_BATCH,
    EmbeddingStore,
)
from app.repositories.postgres.search import PAYLOAD_NAME_EXPRESSION
from app.repositories.source_subgraph_projection import (
    source_graph_partition_rows_on,
    source_subgraph_rows_on,
    source_subgraph_signature_on,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy


def _binary_text_key(value: str) -> bytes:
    """Match PostgreSQL C collation for identifier ordering."""
    return value.encode("utf-8", "surrogatepass")


# The two frozen edge encodings the gathered graph produces: the default
# string path and the build-only int-indexed array fast path.
ScaleGraphEdges = (
    "list[tuple[str, str, float]]"
    " | tuple[numpy.ndarray, numpy.ndarray, numpy.ndarray]"
)


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
                "FROM unified_kg_state WHERE notebook_id=%s",
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
        with self.connect() as connection:
            row = connection.execute(
                "SELECT indexing_pipeline_id,indexing_pipeline_version "
                "FROM unified_kg_state WHERE notebook_id=%s",
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
                "SELECT COUNT(*) AS c, MAX(updated_at) AS ts "
                "FROM knowledge_objects WHERE notebook_id=%s", (notebook_id,)).fetchone()
            rel_ver = db.execute(
                "SELECT COUNT(*) AS c, MAX(created_at) AS ts "
                "FROM knowledge_relations WHERE notebook_id=%s", (notebook_id,)).fetchone()
            chunk_ver = db.execute(
                "SELECT COUNT(*) AS c, MAX(created_at) AS ts "
                "FROM chunks WHERE notebook_id=%s", (notebook_id,)).fetchone()
            # 批 3·W2 §1.4 红线「版本身份不得被未发布代污染」:这条 COUNT/MAX
            # 是 on-disk manifest.version 向量的簇分量,双代窗口不加谓词即全库
            # manifest 判不等(W1 §3.4 的重建风暴)。走 idx_clusters_nb_created_gen。
            clu_ver = db.execute(
                "SELECT COUNT(*) AS c, MAX(created_at) AS ts "
                "FROM concept_clusters WHERE notebook_id=%s "
                "AND generation = COALESCE((SELECT cluster_generation "
                "FROM unified_kg_state WHERE notebook_id = %s), 0)",
                (notebook_id, notebook_id)).fetchone()
            emb_ver = db.execute(
                "SELECT COUNT(*) AS c, MAX(created_at) AS ts "
                "FROM knowledge_embeddings WHERE notebook_id=%s", (notebook_id,)).fetchone()
            state = db.execute(
                "SELECT indexing_pipeline_id,indexing_pipeline_version "
                "FROM unified_kg_state WHERE notebook_id=%s", (notebook_id,)
            ).fetchone()
        return [
            notebook_id,
            int(obj_ver["c"]), iso_timestamp(obj_ver["ts"]),
            int(rel_ver["c"]), iso_timestamp(rel_ver["ts"]),
            int(chunk_ver["c"]), iso_timestamp(chunk_ver["ts"]),
            int(clu_ver["c"]), iso_timestamp(clu_ver["ts"]),
            int(emb_ver["c"]), iso_timestamp(emb_ver["ts"]),
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
        from app.repositories.postgres import knowledge_counts_cache
        with self.connect() as db:
            return knowledge_counts_cache.active_object_count(db, notebook_id)

    def total_chunk_count(self, notebook_id: str) -> int:
        # Seq-gated memo: the chunks COUNT fires on every /scale-index/status
        # (i.e. every notebook open) and is cold-page-bound at millions of rows.
        from app.repositories.postgres import knowledge_counts_cache
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
                "SELECT EXISTS(SELECT 1 FROM notebook_bases "
                "WHERE base_notebook_id=%s) AS exists",
                (notebook_id,),
            ).fetchone()["exists"])

    def source_ids(self, notebook_id: str) -> List[str]:
        with self.connect() as db:
            return [r["id"] for r in db.execute(
                "SELECT id FROM sources WHERE notebook_id=%s", (notebook_id,)).fetchall()]

    def chunk_sources_for_ids(
        self, notebook_id: str, chunk_ids: Sequence[str]
    ) -> Dict[str, str]:
        """Return the row-aligned ANN source identity for a bounded id page."""
        if not chunk_ids:
            return {}
        out: Dict[str, str] = {}
        with self.connect() as db:
            for batch in self.in_batches(chunk_ids):
                placeholders = ",".join("%s" for _ in batch)
                rows = db.execute(
                    "SELECT id,source_id FROM chunks WHERE notebook_id=%s "
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
                placeholders = ",".join("%s" for _ in batch)
                rows = db.execute(
                    "SELECT id FROM sources WHERE notebook_id=%s "
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
                "SELECT created_by FROM notebooks WHERE id = %s", (notebook_id,)
            ).fetchone()
        return row["created_by"] if row else None

    def notebook_tier(self, notebook_id: str) -> "str | None":
        """Cheap PK read of just the tier column — for hot status polls that
        need only tier and must not rebuild the full NotebookSummary (from_row)."""
        with self.connect() as db:
            row = db.execute(
                "SELECT tier FROM notebooks WHERE id = %s", (notebook_id,)
            ).fetchone()
        return row["tier"] if row else None

    def notebook_name(self, notebook_id: str) -> str:
        with self.connect() as db:
            row = db.execute(
                "SELECT name FROM notebooks WHERE id = %s", (notebook_id,)
            ).fetchone()
        return row["name"] if row else ""

    def unified_last_rebuild_at(self, notebook_id: str) -> str:
        with self.connect() as db:
            row = db.execute(
                "SELECT last_rebuild_at FROM unified_kg_state WHERE notebook_id=%s",
                (notebook_id,),
            ).fetchone()
        return iso_timestamp(row["last_rebuild_at"]) if row else ""

    def delta_chunk_count(self, notebook_id: str, source_ids: Sequence[str]) -> int:
        """Chunk count over the given (post-watermark) sources, batched through
        the facade's IN-clause chunking (SQLite variable-count safe)."""
        nchunks = 0
        with self.connect() as db:
            for batch in self.in_batches(source_ids):
                ph = ",".join("%s" for _ in batch)
                nchunks += db.execute(
                    f"SELECT COUNT(*) c FROM chunks WHERE notebook_id=%s AND source_id IN ({ph})",
                    (notebook_id, *batch)).fetchone()["c"]
        return int(nchunks)

    def relation_ids_for_source_batch(
        self, db, notebook_id: str, source_ids: Sequence[str]
    ) -> list[str]:
        if not source_ids:
            return []
        placeholders = ",".join("%s" for _ in source_ids)
        return [
            row["id"]
            for row in db.execute(
                "SELECT id FROM knowledge_relations "
                f"WHERE notebook_id=%s AND source_id IN ({placeholders})",
                (notebook_id, *source_ids),
            ).fetchall()
        ]

    def active_object_graph_rows(self, db, notebook_id: str):
        """Whole-notebook active object rows for the standalone viz derive,
        streamed in ``ordinal`` keyset pages instead of one whole-table
        ``fetchall`` (batch-3 W4 T-W4-3.1).

        Key: ``ordinal`` alone — ``uq_knowledge_objects_ordinal`` makes it
        GLOBALLY unique, so a single-column ``>`` cursor is a total order and
        cannot drop a tie. The ORDER BY is byte-identical to the pre-paging
        one, so the consumer sees the same rows in the same order.

        Registered cost — TWO planner regimes, measured, not assumed (the
        EXPLAIN harness is ``scripts/bench_scale_build_paging.py explain``).
        There is no ``(notebook_id, ordinal)`` composite, so:

        - page << the notebook's remaining rows (the regime a real build runs
          in): ``Index Scan using uq_knowledge_objects_ordinal`` with
          ``ordinal > cursor`` as the Index Cond and ``notebook_id``/``status``
          as residual filters. The cursor advances monotonically, so
          successive pages CONTINUE the range rather than restarting it, and
          the whole scan costs about ONE traversal of the global ordinal
          index. The minority-share cost is that the traversal covers the
          whole TABLE's index (other notebooks' interleaved rows are filtered
          out as they are passed), not that this notebook is re-read per page.
        - page >= the notebook's remaining rows: the planner flips to the
          notebook index plus a top-N Sort, which does read and sort all
          remaining rows — but in that regime a single page covers the rest of
          the notebook, so it happens once, not per page.

        These two are only contradictory if stated as one continuous claim (an
        earlier version of this ledger did exactly that). Same trade-off, same
        acceptance, as ``EmbeddingStore.vector_pages``.

        This is a GENERATOR: it must be consumed inside the caller's
        connection scope, exactly once, by iteration — no ``len()``, no
        indexing, no second pass. A caller that iterates after closing ``db``
        gets a loud driver error, never a silently short result.
        """
        for page in _keyset_pages(
            db,
            int(self.settings.graph_fetch_page_rows),
            lambda cursor: (
                f"SELECT id,object_type,{PAYLOAD_NAME_EXPRESSION} AS name,ordinal "
                "FROM knowledge_objects "
                "WHERE notebook_id=%s AND status!='deprecated'"
                + ("" if cursor is None else " AND ordinal>%s")
                + " ORDER BY ordinal",
                (notebook_id,) if cursor is None else (notebook_id, cursor),
            ),
            lambda row: row["ordinal"],
        ):
            yield from page

    def source_subgraph_signature(
        self, notebook_id: str, source_ids: Sequence[str]
    ) -> tuple:
        allowed = tuple(sorted(set(source_ids)))
        if not allowed:
            return (0, 0, 0, ())
        with self.connect() as connection:
            return source_subgraph_signature_on(
                connection,
                notebook_id,
                allowed,
                placeholder="%s",
                postgres=True,
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
        with self.connect() as connection:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            return source_subgraph_rows_on(
                connection,
                notebook_id,
                allowed,
                limits,
                placeholder="%s",
                postgres=True,
            )

    def source_graph_partition_rows(
        self, notebook_id: str, source_id: str
    ) -> Mapping[str, Any]:
        """Offline source-local rows for the partitioned scale artifact."""
        with self.connect() as connection:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            return source_graph_partition_rows_on(
                connection,
                notebook_id,
                source_id,
                placeholder="%s",
                postgres=True,
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

        ph = ",".join("%s" for _ in USABLE_STATUSES)
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
                (f" AND source_id IN ({','.join('%s' for _ in b)})", tuple(b))
                for b in self.in_batches(source_ids)
            ]
        kg_nodes: Dict[str, dict] = {}
        relations: list = []
        chunk_ids: list = []
        cluster_groups: Dict[str, list] = {}

        # All four gathers below read in keyset pages (batch-3 W4 T-W4-3.1)
        # instead of one whole-table `.fetchall()`. What that bounds is the
        # driver's per-statement result buffer (the objects leg's `payload`
        # JSON and the relations leg's rows are the two big ones) and the
        # lifetime of any single statement's MVCC snapshot across a build that
        # can run for hours. It does NOT bound the Python structures being
        # accumulated here — `kg_nodes`/`relations`/`chunk_ids`/
        # `cluster_groups` still hold the whole notebook, which is the
        # inherent shape of the gathered graph and explicitly out of scope for
        # this change. Every page's ORDER BY is byte-identical to the
        # pre-paging one, so row order (and therefore node_ids order, the
        # first-seen edge-weight winner, and the persisted artifact) is
        # unchanged.
        # Connection boundary, registered: ONE connection stays checked out
        # across all four paged gathers, because between pages this loop does
        # nothing but fold rows into dicts (milliseconds). That is why these
        # keep `keyset_pages`' shared-connection shape while the ANN feed —
        # which runs an hnsw build between pages — acquires and releases a
        # connection per page instead (`embedding_pages`).
        with self.connect() as db:
            for src_clause, src_params in clauses:
                # Key: `ordinal` alone. `uq_knowledge_objects_ordinal` is
                # GLOBALLY unique, so the trailing `id COLLATE "C"` in the
                # ORDER BY can never break a tie — a single-column `>` cursor
                # is already a total order. Planner/index ledger as in
                # `active_object_graph_rows` (no (notebook_id, ordinal)
                # composite; dominant-share notebooks pay nothing).
                for page in _keyset_pages(
                    db, int(self.settings.graph_fetch_page_rows),
                    lambda cursor, _c=src_clause, _p=src_params: (
                        f"SELECT id, object_type, payload, ordinal FROM knowledge_objects "
                        f"WHERE notebook_id=%s AND status IN ({ph}){_c}"
                        + ("" if cursor is None else " AND ordinal>%s")
                        + " ORDER BY ordinal, id COLLATE \"C\"",
                        (notebook_id, *USABLE_STATUSES, *_p) if cursor is None
                        else (notebook_id, *USABLE_STATUSES, *_p, cursor),
                    ),
                    lambda row: row["ordinal"],
                ):
                    for r in page:
                        kg_nodes[r["id"]] = {
                            "type": r["object_type"],
                            "name": json_value(r["payload"], {}).get("name", ""),
                        }
            for src_clause, src_params in clauses:
                # Key: `id COLLATE "C"` — the relations leg's existing ORDER
                # BY key, and the table's primary key, so it is unique by
                # construction. Every text column in this schema is declared
                # `COLLATE "C"`, so the explicit COLLATE is a no-op that keeps
                # the SQL text honest about which ordering the cursor assumes.
                # `id` joins the select list purely to carry that cursor; the
                # accumulated dict keeps the same three keys as before, so the
                # resident `relations` list is byte-for-byte the old one.
                for page in _keyset_pages(
                    db, int(self.settings.graph_fetch_page_rows),
                    lambda cursor, _c=src_clause, _p=src_params: (
                        "SELECT id, source_object_id, target_object_id, edge_type "
                        f"FROM knowledge_relations "
                        f"WHERE notebook_id=%s AND review_status!='rejected'{_c}"
                        + ("" if cursor is None else " AND id COLLATE \"C\">%s")
                        + " ORDER BY id COLLATE \"C\"",
                        (notebook_id, *_p) if cursor is None
                        else (notebook_id, *_p, cursor),
                    ),
                    lambda row: row["id"],
                ):
                    for r in page:
                        relation = {
                            "source_object_id": r["source_object_id"],
                            "target_object_id": r["target_object_id"],
                            "edge_type": r["edge_type"],
                        }
                        source = kg_nodes.get(relation["source_object_id"])
                        target = kg_nodes.get(relation["target_object_id"])
                        if source and target and is_queryable_edge_pair(
                            relation["edge_type"], source["type"], target["type"]
                        ):
                            relations.append(relation)
            for src_clause, src_params in clauses:
                # Key: `ordinal` (`uq_chunks_ordinal`, globally unique) — same
                # argument as the objects leg above.
                for page in _keyset_pages(
                    db, int(self.settings.graph_fetch_page_rows),
                    lambda cursor, _c=src_clause, _p=src_params: (
                        f"SELECT id, ordinal FROM chunks WHERE notebook_id=%s{_c}"
                        + ("" if cursor is None else " AND ordinal>%s")
                        + " ORDER BY ordinal, id COLLATE \"C\"",
                        (notebook_id, *_p) if cursor is None
                        else (notebook_id, *_p, cursor),
                    ),
                    lambda row: row["ordinal"],
                ):
                    for r in page:
                        chunk_ids.append(r["id"])
            # Key: (canonical_id, member_object_id), matching
            # `idx_clusters_nb_canonical_member_gen` (notebook_id,
            # canonical_id, member_object_id) INCLUDE (generation) — 0051's
            # index, whose INCLUDE exists precisely so the published-generation
            # predicate stays Index-Only. Total order: a cluster row's
            # `object_type` IS the type of its objects, and
            # `uq_clusters_nb_type_member_generation` makes `member_object_id`
            # unique per (notebook, type, generation) — an object id has
            # exactly one type, so `member_object_id` (hence the pair) is
            # unique per (notebook, generation) and `>` drops nothing.
            #
            # SINGLE-GENERATION SEMANTICS = one evaluation + a per-page bind.
            # The published generation is resolved ONCE, right here, and the
            # resulting integer is bound into EVERY page's predicate. Leaving
            # the old inline `COALESCE((<published-generation pointer read>),
            # 0)` scalar subquery in the predicate (spelled out one statement
            # below) would have re-evaluated it per page, and under
            # READ COMMITTED a generation flip committed between two pages
            # then splits the scan across two generations: the same
            # member_object_id can arrive under two different canonical_ids
            # (a state `uq_clusters_nb_type_member_generation` makes
            # impossible WITHIN one generation, so nothing downstream defends
            # against it), and the torn graph would be persisted under the new
            # generation's version identity. The predicate itself still rides
            # every page — never hoisted to a first-page-only filter, which
            # would both lose the Index-Only Scan and let a non-published
            # generation's members in from page two onward (the W2 red line
            # "version identity only ever counts the published generation").
            published_row = db.execute(
                "SELECT cluster_generation FROM unified_kg_state "
                "WHERE notebook_id=%s",
                (notebook_id,),
            ).fetchone()
            published_generation = int(
                0 if published_row is None
                or published_row["cluster_generation"] is None
                else published_row["cluster_generation"]
            )
            for page in _keyset_pages(
                db, int(self.settings.graph_fetch_page_rows),
                lambda cursor: (
                    "SELECT canonical_id, member_object_id FROM concept_clusters "
                    "WHERE notebook_id=%s AND generation = %s"
                    + ("" if cursor is None else
                       " AND (canonical_id COLLATE \"C\", member_object_id COLLATE \"C\")"
                       " > (%s, %s)")
                    + " ORDER BY canonical_id COLLATE \"C\", "
                    "member_object_id COLLATE \"C\"",
                    (notebook_id, published_generation) if cursor is None
                    else (notebook_id, published_generation, *cursor),
                ),
                lambda row: (row["canonical_id"], row["member_object_id"]),
            ):
                for r in page:
                    cluster_groups.setdefault(
                        r["canonical_id"], []).append(r["member_object_id"])

        # Memberships: entity ↔ chunk (scoped → limit to gathered objects).
        # `paged=True` is the ONLY route to the bounded evidence read
        # (`notebook_object_evidence_rows_paged`); the online PPR callers of
        # the same cache keep the unordered whole-table read, whose plan this
        # gather's `ORDER BY id` would otherwise have cost +31% (batch-3 W4
        # T-W4-3.1 double-review fix A). Same rows, same dict, same cache
        # entry — only the statement differs.
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
        matrices must never become LRU entries. Rows are read through
        `EmbeddingStore.vector_pages` in bounded keyset pages (`{id} > last
        LIMIT MATRIX_FETCH_BATCH`, embedding_store.py), each page an
        independent fully-exhausted statement — the whole-table result set
        is never `.fetchall()`'d in one call and no single statement holds a
        streaming cursor across the whole scan. This mirrors the SQLite
        side's `_stream_rows`
        (app/repositories/sqlite/index_projection_store.py) at the same
        8-9M-row scale — see `vector_pages`' docstring for the full memory /
        de-dup / snapshot argument and the three points where the
        PostgreSQL shape deliberately differs from SQLite's rowid pagination
        (id-column keyset instead of rowid, no `seen` de-dup because
        id-keyset cannot revisit an id, and MVCC-snapshot release instead of
        WAL-checkpoint release).

        Planner shape: `ORDER BY {id} LIMIT n` / `{id} > last` is a plain PK
        index range scan; `notebook_id` applies as a residual heap filter.
        The offline build only ever runs for notebooks holding the dominant
        row share of the table (production: 99.1%), so that residual filter
        costs nothing there. Below roughly sqrt(total_rows × batch) of share
        the planner instead re-reads the whole notebook through
        `idx_{table}_nb` with a per-page top-N sort — total work stays
        bounded by about one full PK traversal, but it is a real
        minority-share cost, unlike SQLite whose `idx_{table}_nb` implicitly
        ends in rowid and seeks at every share (details in
        EmbeddingStore.vector_pages). A `(notebook_id, {id})` composite
        index would remove it but is deliberately NOT added by this change;
        if ever needed it ships via an offline CREATE INDEX CONCURRENTLY
        maintenance path, not a startup migration (schema currently v21).

        A list = fold's bounded delta load, batched through the facade's
        IN-clause chunking with the connection held open across the
        generator (frozen `_delta_vecs` shape); an empty list returns
        ([], []). Both paths truncate through build_matrix(runtime_dim=...)
        — the dim-consumption point stays here UNCHANGED (漏消费点 = 静默零召回)."""
        from app.domain.vector_index import build_matrix, resolve_runtime_dim
        runtime_dim = resolve_runtime_dim(self.settings)
        if object_ids is None:
            with self.connect() as db:
                n_hint = EmbeddingStore.version_row(db, notebook_id, table)["c"]
                return build_matrix(
                    EmbeddingStore.vector_pages(db, notebook_id, table, id_column),
                    n_hint=n_hint, runtime_dim=runtime_dim)
        if not object_ids:
            return [], []

        def _rows():
            with self.connect() as db:
                for batch in self.in_batches(object_ids):
                    for r in EmbeddingStore.vector_rows_for_ids(
                        db, notebook_id, table, id_column, batch
                    ):
                        yield r["vid"], r["vector"]
        return build_matrix(_rows(), runtime_dim=runtime_dim)

    def embedding_row_count(self, notebook_id: str, table: str) -> int:
        """Row count for the whole-notebook embedding scan — the UPPER BOUND
        the paged ANN build sizes ``init_index(max_elements=...)`` with before
        it knows how many rows will actually survive decoding. Same aggregate
        ``embedding_matrix`` already uses for ``build_matrix``'s ``n_hint``."""
        with self.connect() as db:
            return int(EmbeddingStore.version_row(db, notebook_id, table)["c"])

    def embedding_pages(self, notebook_id: str, table: str, id_column: str,
                        page_rows: int = MATRIX_FETCH_BATCH):
        """Whole-notebook vectors as bounded ``(ids, matrix)`` pages.

        The load-side half of ``embedding_matrix(object_ids=None)`` without
        its one whole-notebook matrix: the DB read is the SAME
        ``EmbeddingStore.vector_pages`` keyset scan (same ordering, same
        snapshot-release and drift ledger — see its docstring), and
        ``matrix_pages`` applies ``build_matrix``'s five semantics across the
        whole stream, so concatenating the pages is element-identical to
        ``embedding_matrix``. hnswlib copies each page into its own graph, so
        the caller drops the page right after ``add_items`` and never holds
        the ~33GB relation matrix (WR-9). ``np.asarray`` on the old whole
        matrix was only an ALIAS — the memory this removes is the load-side
        copy, not hnswlib's own (which is unavoidable).

        A GENERATOR that acquires and RELEASES a pooled connection per DB
        page (``vector_pages(connect=...)``) rather than holding ONE open
        across its whole consumption. That distinction is the point of this
        entry point existing separately from ``embedding_matrix``: its
        consumer builds an hnsw index between pages, so a held connection
        would keep a pool slot (and an ACCESS SHARE lock on the embedding
        table) checked out for the entire multi-minute index build — at
        ``POSTGRES_POOL_MAX_SIZE=1``, the minimum the settings validator
        accepts, that is the whole pool and every other request blocks behind
        it. Each keyset page is already an
        independent statement whose only cross-page state is a Python-side
        cursor, so nothing about the scan's ordering or its drift ledger
        depends on the pages sharing one connection.
        """
        from app.domain.vector_index import matrix_pages, resolve_runtime_dim
        runtime_dim = resolve_runtime_dim(self.settings)
        yield from matrix_pages(
            EmbeddingStore.vector_pages(
                # batch=page_rows (codex #676 R10 P2): the configured budget
                # must bound the RAW db fetch too, not only the decoded
                # output pages matrix_pages cuts afterwards.
                None, notebook_id, table, id_column, batch=page_rows,
                connect=self.connect,
            ),
            page_rows,
            runtime_dim=runtime_dim,
        )

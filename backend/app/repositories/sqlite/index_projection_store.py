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
from typing import Callable, Dict, List, Optional, Sequence, Tuple, TYPE_CHECKING

from app.services.knowledge_contracts import USABLE_STATUSES

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy

# The two frozen edge encodings the gathered graph produces: the default
# string path and the build-only int-indexed array fast path.
ScaleGraphEdges = (
    "list[tuple[str, str, float]]"
    " | tuple[numpy.ndarray, numpy.ndarray, numpy.ndarray]"
)

# Batch size for the whole-notebook embedding cursor stream (embedding_matrix,
# object_ids=None). fetchmany() this many rows at a time instead of row-by-row:
# it bounds the native-dim BLOBs held on top of the preallocated matrix (the
# OOM fix) AND avoids per-row _DiagnosticCursor.__next__ instrumentation on the
# online build path. See embedding_matrix's docstring for the full rationale.
_MATRIX_FETCH_BATCH = 10_000


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

    # ────────────────────────────────────────────────── version snapshots ──
    def version_signal(self, notebook_id: str) -> "tuple[int, int, tuple]":
        """Cheap probe: (seq, cseq, settings_tail) — a single unified_kg_state
        row read, no table aggregates. runtime_dim / mention_seq fold into the
        settings tail exactly as before (they flow through version_facts'
        caller into the on-disk manifest.version comparison)."""
        from app.services.vector_index import resolve_runtime_dim
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
                "SELECT kg_mutation_seq, cluster_mutation_seq, mention_seq FROM unified_kg_state "
                "WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()
            seq = int(st["kg_mutation_seq"]) if st else 0
            cseq = int(st["cluster_mutation_seq"]) if st else 0
            mseq = int(st["mention_seq"]) if (st and st["mention_seq"] is not None) else -1
        return seq, cseq, settings_tail + (mseq,)

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
            clu_ver = db.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
                "FROM concept_clusters WHERE notebook_id=?", (notebook_id,)).fetchone()
            emb_ver = db.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
                "FROM knowledge_embeddings WHERE notebook_id=?", (notebook_id,)).fetchone()
        return [
            notebook_id,
            int(obj_ver["c"]), obj_ver["ts"],
            int(rel_ver["c"]), rel_ver["ts"],
            int(chunk_ver["c"]), chunk_ver["ts"],
            int(clu_ver["c"]), clu_ver["ts"],
            int(emb_ver["c"]), emb_ver["ts"],
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
        from app.services.kg.ppr import variant_edge_pairs, emb_synonym_edges
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

        with self.connect() as db:
            for src_clause, src_params in clauses:
                for r in db.execute(
                        f"SELECT id, object_type, payload FROM knowledge_objects "
                        f"WHERE notebook_id=? AND status IN ({ph}){src_clause}",
                        (notebook_id, *USABLE_STATUSES, *src_params)).fetchall():
                    kg_nodes[r["id"]] = {
                        "type": r["object_type"],
                        "name": json.loads(r["payload"] or "{}").get("name", ""),
                    }
            for src_clause, src_params in clauses:
                for r in db.execute(
                        f"SELECT source_object_id, target_object_id FROM knowledge_relations "
                        f"WHERE notebook_id=? AND review_status!='rejected'{src_clause}",
                        (notebook_id, *src_params)).fetchall():
                    relations.append(dict(r))
            for src_clause, src_params in clauses:
                for r in db.execute(
                        f"SELECT id FROM chunks WHERE notebook_id=?{src_clause}",
                        (notebook_id, *src_params)).fetchall():
                    chunk_ids.append(r["id"])
            for r in db.execute(
                    "SELECT canonical_id, member_object_id FROM concept_clusters "
                    "WHERE notebook_id=?", (notebook_id,)).fetchall():
                cluster_groups.setdefault(r["canonical_id"], []).append(r["member_object_id"])

        # Memberships: entity ↔ chunk (scoped → limit to gathered objects)
        ent_chunk_map = self.ent_chunk_map(notebook_id)
        _kg_keys = set(kg_nodes.keys())
        memberships = [(oid, cid) for oid, cids in ent_chunk_map.items()
                       if (not scoped or oid in _kg_keys) for cid in cids]
        membership_counts: Dict[str, int] = {
            oid: len(cids) for oid, cids in ent_chunk_map.items()
            if (not scoped or oid in _kg_keys)}
        del ent_chunk_map, _kg_keys

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
        matrices must never become LRU entries. The row cursor is streamed in
        bounded fetchmany(_MATRIX_FETCH_BATCH) batches — never .fetchall()'d
        whole, never iterated row-by-row. Both bounds are load-bearing at
        8M-row scale:
          • Memory: the whole-table result set is ~130GB of native BLOBs at
            8M×4096-dim; materialising it alongside the preallocated (already
            truncated) matrix is exactly what OOM-killed the offline `index`
            build (relation load: ~130GB transient on top of the ~78GB KG/chunk
            residents → ~208GB). A batch bounds the raw BLOBs held on top of the
            output matrix to _MATRIX_FETCH_BATCH rows.
          • CPU/lock: _DiagnosticCursor.__next__ is instrumented, so row-by-row
            iteration would enter sql_scope for EVERY embedding — on the online
            build path (diagnostics runtime installed) that is normalize-SQL +
            the global diagnostics lock twice + a writer wake per row, tens of
            millions of times. fetchmany() is instrumented once per batch;
            iterating the returned plain list is native (no instrumented
            __next__).
        The connection stays open for the whole single-pass consumption because
        the return runs inside the `with`. A list = fold's bounded delta load,
        batched through the facade's IN-clause chunking with the connection
        held open across the generator (frozen `_delta_vecs` shape); an empty
        list returns ([], []). Both paths truncate through
        build_matrix(runtime_dim=...) — the dim-consumption point moves here
        UNCHANGED (漏消费点 = 静默零召回)."""
        from app.services.vector_index import build_matrix, resolve_runtime_dim
        runtime_dim = resolve_runtime_dim(self.settings)
        if object_ids is None:
            with self.connect() as db:
                n_hint = db.execute(
                    f"SELECT COUNT(*) AS c FROM {table} WHERE notebook_id=?",
                    (notebook_id,)).fetchone()["c"]
                cursor = db.execute(
                    f"SELECT {id_column} AS vid, vector FROM {table} WHERE notebook_id=?",
                    (notebook_id,))

                def _stream_rows():
                    while True:
                        batch = cursor.fetchmany(_MATRIX_FETCH_BATCH)
                        if not batch:
                            break
                        for row in batch:
                            yield row["vid"], row["vector"]

                return build_matrix(
                    _stream_rows(), n_hint=n_hint, runtime_dim=runtime_dim)
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

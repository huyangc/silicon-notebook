from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional

from app.models.ask import RuleCard
from app.models.knowledge import (
    KnowledgeEdge,
    KnowledgeGraph,
    KnowledgeNode,
    KnowledgeRecord,
    KnowledgeTypeCount,
    PaginatedKnowledge,
)
from app.services.retrieval import RetrievedKnowledge
from app.services.extraction_profiles import OBJECT_SCHEMAS, OBJECT_TYPE_LABELS, ObjectSchema
from app.services.knowledge_contracts import CONCEPT_DETAIL_PAGE_MAX, KnowledgeGraphTooLargeError
from app.services.model_work import ModelProviderError


class KnowledgeQueryService:
    """Canonical KG search, browser, graph, and detail projections."""

    def __init__(
        self,
        *,
        settings,
        model_provider,
        event_log,
        database,
        catalog,
        knowledge,
        chunk_store,
        unified_kg,
        scale_runtime,
        retrieval: Callable[[], Any],
        schemas,
        snapshots,
        notebook_languages: Callable[[], dict],
        participant_notebook_ids: Callable[[str], list[str]] = lambda notebook_id: [notebook_id],
        node_context_reader: Callable[[str, str], dict] = lambda _notebook_id, _object_id: {},
        memory_retriever=None,
        current_user_id: Callable[[], str] = lambda: "",
        queries=None,
    ) -> None:
        self.settings = settings
        self.models = model_provider
        self.event_log = event_log
        self.database = database
        self.catalog = catalog
        self.knowledge = knowledge
        self.chunk_store = chunk_store
        self.unified_kg = unified_kg
        self.scale_runtime = scale_runtime
        self.retrieval = retrieval
        self.schemas = schemas
        self.snapshots = snapshots
        self.notebook_languages = notebook_languages
        self.participant_notebook_ids = participant_notebook_ids
        self.node_context_reader = node_context_reader
        self.memory_retriever = memory_retriever
        self.current_user_id = current_user_id
        self.queries = queries

    def backfill_kg_fts(self, notebook_id: str) -> int:
        self.catalog.get_notebook(notebook_id)
        with self.database.write() as db:
            return self.knowledge.backfill_fts(db, notebook_id)

    def backfill_chunk_fts(self, notebook_id: str) -> int:
        with self.database.write() as db:
            count = self.chunk_store.backfill_fts(db, notebook_id)
        self.notebook_languages().pop(notebook_id, None)
        return count

    def semantic_search(self, notebook_id: str, query: str, limit: int) -> list:
        workload_id = "retrieval_query_embedding"
        if not self.models.configured(workload_id):
            return []
        index = self.scale_runtime.load(notebook_id)
        if index is None or not index.ann_labels:
            return []
        try:
            vector = self.models.embedding(workload_id).embed_query(query)
        except ModelProviderError as exc:
            self.models.note_model_error(
                "kg_semantic_search", exc, workload_id=workload_id
            )
            return []
        if vector is None:
            return []
        import numpy as np

        dimension = int(index.manifest.get("dim", len(vector)))
        if dimension != len(vector):
            self.event_log.emit({
                "kind": "dim_mismatch",
                "notebook_id": notebook_id,
                "site": "kg_semantic_search",
                "manifest_dim": dimension,
                "query_dim": len(vector),
            })
            return []
        ann = self.scale_runtime.open_ann(index, "kg")
        if ann is None:
            return []
        ann.set_ef(max(limit + 1, 50))
        actual = min(limit, len(index.ann_labels))
        labels, distances = ann.knn_query(
            np.asarray(vector, dtype=np.float32), k=actual
        )
        hits = []
        for label, distance in zip(labels[0], distances[0]):
            object_id = index.ann_labels[int(label)]
            if object_id.startswith("cluster:") or not object_id.startswith("ko-"):
                continue
            score = max(0.0, 1.0 - float(distance))
            if score > 0:
                hits.append({
                    "object_id": object_id,
                    "name": "",
                    "score": score,
                    "match": "semantic",
                })
        return hits

    def hydrate_search_hits(self, notebook_id: str, hits: list) -> list:
        if not hits:
            return []
        with self.database.connect() as db:
            rows = self.knowledge.object_meta_rows(
                db, [hit["object_id"] for hit in hits]
            )
        metadata = {}
        for row in rows:
            if row["status"] == "deprecated":
                continue
            try:
                payload = json.loads(row["payload"] or "{}")
            except Exception:
                payload = {}
            metadata[row["id"]] = {
                "object_type": row["object_type"],
                "name": payload.get("name", ""),
            }
        result = []
        for hit in hits:
            current = metadata.get(hit["object_id"])
            if current is None:
                continue
            enriched = dict(hit)
            enriched["object_type"] = current["object_type"]
            enriched["name"] = current["name"] or enriched.get("name", "")
            result.append(enriched)
        return result

    def fold_hits_to_canonical(self, notebook_id: str, hits: list, limit: int) -> list:
        if not hits:
            return hits
        with self.database.connect() as db:
            rows = self.unified_kg.cluster_fold_rows(
                db, notebook_id, [hit["object_id"] for hit in hits]
            )
        folded_ids = {
            row["member_object_id"]: (row["canonical_id"], row["canonical_name"])
            for row in rows
        }
        best = {}
        for hit in hits:
            folded = dict(hit)
            mapping = folded_ids.get(hit["object_id"])
            if mapping is not None:
                folded["object_id"], folded["name"] = mapping
            key = folded["object_id"]
            if key not in best or folded["score"] > best[key]["score"]:
                best[key] = folded
        return sorted(best.values(), key=lambda item: item["score"], reverse=True)[:limit]

    def search(self, notebook_id: str, query: str, limit: int = 30) -> list:
        from app.services.kg.search import merge_search_hits

        self.catalog.get_notebook(notebook_id)
        # Probed BEFORE the connection is taken: on a cold language cache the
        # probe opens its own connection, and nesting that inside this `with`
        # would contend for the pool. Same shape as every retrieval call site.
        corpus_langs = self.retrieval().lexical_corpus_languages(notebook_id)
        with self.database.connect() as db:
            lexical = self.knowledge.fts_search(
                db, notebook_id, query, limit, corpus_langs=corpus_langs
            )
        semantic = self.semantic_search(notebook_id, query, limit)
        merged = merge_search_hits(lexical, semantic, limit)
        hydrated = self.hydrate_search_hits(notebook_id, merged)
        result = self.fold_hits_to_canonical(notebook_id, hydrated, limit)
        if self.memory_retriever is not None:
            memories = self.memory_retriever.notebook_memory_hits(
                self.current_user_id(), notebook_id, query, limit
            )
            result.extend({
                "object_id": hit.memory_id,
                "name": hit.title,
                "object_type": "memory",
                "score": hit.score,
                "match": "memory",
            } for hit in memories)
            result.sort(
                key=lambda item: (
                    float(item.get("score", 0.0)), str(item.get("object_id", ""))
                ),
                reverse=True,
            )
        return result[:limit]

    def knowledge_types(self, notebook_id: str) -> List[KnowledgeTypeCount]:
        self.catalog.get_notebook(notebook_id)
        with self.database.connect() as db:
            counts, _ = self.knowledge.type_counts(db, notebook_id)
        labels = self.schemas.schema_labels(notebook_id)
        ordered = [item for item in OBJECT_SCHEMAS if item in counts]
        ordered += [item for item in counts if item not in OBJECT_SCHEMAS]
        return [
            KnowledgeTypeCount(
                object_type=item,
                label=labels.get(item, OBJECT_TYPE_LABELS.get(item, item)),
                count=counts[item],
            )
            for item in ordered
        ]

    @staticmethod
    def knowledge_record(
        object_type: str, obj: dict, schema: Optional[ObjectSchema]
    ) -> KnowledgeRecord:
        from app.services.ask_service import knowledge_record

        return knowledge_record(object_type, obj, schema)

    def list_knowledge(
        self, notebook_id: str, object_type: str, status: Optional[str] = None,
        offset: int = 0, limit: int = 50,
    ) -> PaginatedKnowledge:
        self.catalog.get_notebook(notebook_id)
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 200))
        schema = self.schemas.effective_schemas(notebook_id).get(object_type)
        with self.database.connect() as db:
            total, objects = self.knowledge.list_knowledge_page(
                db, notebook_id, object_type, status, offset, limit
            )
        return PaginatedKnowledge(
            items=[self.knowledge_record(object_type, obj, schema) for obj in objects],
            total_count=total,
            offset=offset,
            limit=limit,
        )

    @staticmethod
    def kg_headline(payload: dict) -> str:
        name = (payload.get("name") or "").strip()
        return name[:120] if len(name) > 120 else name

    def relations_for_notebook(self, notebook_id: str) -> List[dict]:
        with self.database.connect() as db:
            return self.knowledge.relations_for_notebook(db, notebook_id)

    def graph(self, notebook_id: str) -> KnowledgeGraph:
        self.catalog.get_notebook(notebook_id)
        with self.database.connect() as db:
            count = self.knowledge.count_active_objects(db, notebook_id)
        if int(count) > self.settings.viz_sync_build_max_objects:
            raise KnowledgeGraphTooLargeError(
                f"notebook {notebook_id} has {count} objects "
                f"(> {self.settings.viz_sync_build_max_objects}); the legacy "
                "/graph endpoint has no bounded fallback for large notebooks — "
                "use /notebooks/{id}/unified-kg instead (bounded/paginated)."
            )
        with self.database.connect() as db:
            rows = self.knowledge.graph_node_rows(db, notebook_id)
        nodes = [
            KnowledgeNode(
                id=row["id"],
                object_type=row["object_type"],
                headline=self.kg_headline(json.loads(row["payload"] or "{}")),
                status=row["status"],
            )
            for row in rows
        ]
        valid = {node.id for node in nodes}
        edges = [
            KnowledgeEdge(
                from_id=relation["source_object_id"],
                to_id=relation["target_object_id"],
                relation=relation["edge_type"],
                label=relation["edge_type"],
            )
            for relation in self.relations_for_notebook(notebook_id)
            if relation["source_object_id"] in valid
            and relation["target_object_id"] in valid
        ]
        return KnowledgeGraph(nodes=nodes, edges=edges)

    def edge_centrality_map(self, notebook_id: str) -> Dict[str, float]:
        from app.services.kg.graph_reason import build_rx_graph, compute_edge_centrality

        version = tuple(self.scale_runtime.version(notebook_id))

        def load() -> Dict[str, float]:
            with self.database.connect() as db:
                node_rows, relations = self.knowledge.edge_centrality_source_rows(
                    db, notebook_id, self.settings.edge_centrality_max_nodes
                )
            nodes = {
                row["id"]: {"type": row["object_type"], "name": ""}
                for row in node_rows
            }
            graph, _idx_to_oid, _oid_to_idx = build_rx_graph(nodes, relations)
            return compute_edge_centrality(graph)

        return self.snapshots.vector_cache.get(
            f"{notebook_id}:edge_centrality", version, load
        )

    def annotate_edge_support(self, notebook_id: str, edges: List[dict]) -> List[dict]:
        """给本次响应内的边(≤300 条)标注 (support_count, source_count)。

        热路径修复批 2 · R2-1(审计 KG-1):这里原来先取
        ``retrieval.edge_support_map(notebook_id)`` —— canonical_relations
        的**整表** dict(生产 8.35M 行 ≈3.6GB,见 ``graph_retrieval``
        ``_edge_support_map`` 与 ``relation_support_count`` 的登记),而且那次
        整表取值排在判空之前:一次空响应也照付。同一函数里的 cluster 折叠早已
        改成有界点查(下面 ``cluster_fold_rows`` 那段),支撑数这一半漏掉了。

        现在两半对称:先判空早退(零查询),非空时按本次边集的 canonical 三元组
        走 ``relation_support_rows`` 定点查询(PK ``pk_canonical_relations``
        精确命中,row-value IN,按 ``_SUPPORT_IN_CHUNK`` 分批)——与
        ``GraphRetrievalService.relation_support_counts`` 同一形态、同一原语。

        等价性(逐点对齐旧的整表 map):
        - 折叠:不变,仍是 ``cluster_fold_rows`` + ``get(id, id)`` 回退。
        - 命中:整表 map 的键是 ``(canonical_src, edge_type, canonical_tgt)``、
          值是 ``(support_count, source_count)``;定点查询按同一主键取同两列,
          所以命中项逐字相同。
        - 反向兜底:旧写法 ``support.get(key) or support.get(反向 key)`` 在整表
          map 里两个方向都在手。这里把**两个朝向**都放进查询集合,再在 Python
          里保持「正向优先、正向缺失才用反向」的同一偏好——注意旧的 ``or`` 只在
          正向为 ``None`` 时才落到反向(命中值是非空 tuple,恒为真),所以
          「正向存在」与「正向为真」在这里是同一件事,不存在被 ``or`` 吞掉的
          假值命中。
        - 未命中:边原样返回(不加字段),与旧实现一致。
        - 整表 map 为空(该库没有任何 canonical 关系)这一条旧早退不再需要:
          定点查询在这种库上返回零行,所有边照样原样返回,值相同。代价是这类库
          多付一次有界折叠 + 一次有界点查(各 ≤2·len(edges) 个绑定值、走 PK),
          换掉的是一次 8.35M 行的整表扫描 —— 方向明确。

        ``edge_support_map`` 至此不再被本路径调用;它仍是
        ``graph_retrieval.relation_support_count`` 的实现细节(那条被刻意保留
        为语义真源、由差分测试钉住),所以 ``:edge_support`` 缓存键族**不**在
        本项里下线。
        """
        if not edges:
            # 判空早退必须在任何查询之前(旧实现把整表取值排在这一步之前,
            # 空响应也要付一次全表扫描)。
            return edges
        # Bound the canonical-fold lookup to just THESE edges' endpoints
        # (≤2·len(edges)) instead of loading the full cluster_map: at 8M
        # concept_clusters that dict is ~1.2GB+ and this runs on every KG-view /
        # kanban open only to fold ≤300 edges (OOM audit P1-5). cluster_fold_rows
        # is the SAME bounded query the KG view already uses (works on SQLite and
        # Postgres); ids not in a cluster fall back to the raw id — exactly what
        # clusters.get(id, id) did over the full map. Batched under the SQLite
        # IN-variable limit.
        ids = sorted({
            oid
            for edge in edges
            for oid in (edge["source_object_id"], edge["target_object_id"])
        })
        clusters: dict = {}
        keys: List[tuple] = []
        with self.database.connect() as db:
            if ids:
                for start in range(0, len(ids), 900):
                    for row in self.unified_kg.cluster_fold_rows(
                        db, notebook_id, ids[start:start + 900]
                    ):
                        clusters[row["member_object_id"]] = row["canonical_id"]
            for edge in edges:
                keys.append((
                    clusters.get(edge["source_object_id"], edge["source_object_id"]),
                    edge["edge_type"],
                    clusters.get(edge["target_object_id"], edge["target_object_id"]),
                ))
            support = self._edge_support_point_lookup(db, notebook_id, keys)
        result = []
        for edge, key in zip(edges, keys):
            hit = support.get(key) or support.get((key[2], key[1], key[0]))
            result.append(
                {**edge, "support_count": hit[0], "source_count": hit[1]}
                if hit else edge
            )
        return result

    # 与 ``GraphRetrievalService._RELATION_SUPPORT_IN_CHUNK`` 同值同理由:
    # row-value IN 的绑定值个数必须封住(SQLite 表达式树上限 / PostgreSQL 规划
    # 耗时),完整论证见 ``unified_kg_store.relation_support_rows`` 的 docstring。
    # 这里刻意不 import 那个类常量:knowledge_query 不依赖 graph_retrieval 的
    # 模块加载顺序(``retrieval`` 是懒回调),为一个整数换一条模块级依赖不值。
    _SUPPORT_IN_CHUNK = 300

    def _edge_support_point_lookup(
        self, db, notebook_id: str, keys: Iterable[tuple]
    ) -> Dict[tuple, tuple]:
        """``{(canonical_src, edge_type, canonical_tgt): (support, source)}``
        —— 只包含本次 ``keys`` 里**真实存在**的 canonical 边(缺席即未命中,
        调用方原样返回该边),两个朝向都查(见 ``annotate_edge_support``)。

        去重 + 排序后分批:sorted 而不是裸 set,理由与
        ``relation_support_counts`` 相同 —— set 的迭代序随 ``PYTHONHASHSEED``
        变化,会让同一逻辑输入在不同进程里切出不同的 SQL 文本。
        """
        lookup: set = set()
        for key in keys:
            lookup.add(tuple(key))
            lookup.add((key[2], key[1], key[0]))
        ordered = sorted(lookup)
        support: Dict[tuple, tuple] = {}
        for start in range(0, len(ordered), self._SUPPORT_IN_CHUNK):
            batch = ordered[start:start + self._SUPPORT_IN_CHUNK]
            for row in self.unified_kg.relation_support_rows(db, notebook_id, batch):
                support[(row["canonical_src"], row["edge_type"], row["canonical_tgt"])] = (
                    int(row["support_count"]), int(row["source_count"]),
                )
        return support

    def _participant_source(
        self, active_notebook_id: str, source_notebook_id: str
    ) -> str:
        self.catalog.get_notebook(active_notebook_id)
        if not source_notebook_id:
            return active_notebook_id
        if source_notebook_id not in self.participant_notebook_ids(active_notebook_id):
            raise KeyError(source_notebook_id)
        return source_notebook_id

    def concept_detail(
        self,
        notebook_id: str,
        canonical_id: str,
        *,
        source_notebook_id: str = "",
        limit: Optional[int] = CONCEPT_DETAIL_PAGE_MAX,
        after: str = "",
    ) -> dict:
        source_id = self._participant_source(notebook_id, source_notebook_id)
        return self._concept_detail(source_id, canonical_id, limit=limit, after=after)

    def _concept_detail(
        self,
        notebook_id: str,
        canonical_id: str,
        *,
        limit: Optional[int] = CONCEPT_DETAIL_PAGE_MAX,
        after: str = "",
    ) -> dict:
        # Hub-cluster member pagination (KG-4 application-side fix, R3·T-B2):
        # `concept_cluster_detail_rows` used to return every member (plus
        # full payload/evidence) unbounded — production has seen 8-9M cluster
        # rows for a single hub concept. `limit=None` keeps the legacy
        # unbounded read available for internal callers (e.g. the pagination
        # equality oracle in tests) that still need the whole member set in
        # one shot.
        #
        # `next_cursor` is derived by over-fetching one extra row past
        # `limit` and trimming it off, rather than comparing against
        # `member_total`: it stays correct even if the member set changes
        # between this page's row read and the separate COUNT query below,
        # and needs no notion of "how many members came before this page".
        fetch_limit = None if limit is None else limit + 1
        with self.database.connect() as db:
            cluster_rows, name = self.knowledge.concept_cluster_detail_rows(
                db, notebook_id, canonical_id, limit=fetch_limit, after=after
            )
        next_cursor = None
        if limit is not None and len(cluster_rows) > limit:
            cluster_rows = cluster_rows[:limit]
            next_cursor = cluster_rows[-1]["member_object_id"]
        members = []
        member_ids = []
        for row in cluster_rows:
            members.append({
                "id": row["member_object_id"],
                "object_type": row["object_type"],
                "payload": json.loads(row["payload"] or "{}"),
                "evidence": json.loads(row["evidence"] or "[]"),
            })
            member_ids.append(row["member_object_id"])
        with self.database.connect() as db:
            member_total = self.knowledge.concept_cluster_member_total(
                db, notebook_id, canonical_id
            )
        if not member_ids:
            return {
                "canonical_id": canonical_id,
                "canonical_name": name,
                "members": [],
                "attached": [],
                "evidence": [],
                "member_total": member_total,
                "next_cursor": None,
            }
        with self.database.connect() as db:
            relation_edges, other_objects = self.knowledge.concept_neighbor_rows(
                db, notebook_id, member_ids
            )
        # `attached`/`evidence` are computed over THIS PAGE's members only
        # (registered display-semantics change, R3·T-B2): each page reports
        # the adjacency/evidence of the members it just returned, not the
        # whole cluster. Paging through every page still surfaces the
        # complete set — pagination, not truncation.
        attached = []
        seen = set()
        for edge in relation_edges:
            other = edge["other"]
            if (
                other in other_objects
                and other_objects[other]["object_type"] != "concept"
                and other not in seen
            ):
                seen.add(other)
                attached.append({**other_objects[other], "edge_type": edge["edge_type"]})
        by_id = {member["id"]: member for member in members}
        evidence = [
            item
            for object_id in member_ids
            for item in by_id.get(object_id, {}).get("evidence", [])
        ]
        with self.database.connect() as db:
            evidence = self.knowledge._enrich_evidence(db, evidence)
        return {
            "canonical_id": canonical_id,
            "canonical_name": name,
            "members": [by_id[item] for item in member_ids if item in by_id],
            "attached": attached,
            "evidence": evidence,
            "member_total": member_total,
            "next_cursor": next_cursor,
        }

    def node_context(
        self,
        notebook_id: str,
        object_id: str,
        *,
        source_notebook_id: str = "",
    ) -> dict:
        source_id = self._participant_source(notebook_id, source_notebook_id)
        return self.node_context_reader(source_id, object_id)

    def insert_test_object(
        self, notebook_id: str, object_type: str, payload: dict, source_id: str = ""
    ) -> str:
        with self.database.write() as db:
            oid = self.knowledge.insert_test_object(
                db, notebook_id, object_type, payload, source_id
            )
        # Test-only raw insert bypasses store_kg's kg_mutation_seq bump (which is
        # what invalidates the seq-gated count cache in production). Drop the
        # cache entry so tests that seed via this helper see fresh counts.
        if self.queries is not None:
            self.queries.invalidate_knowledge_counts(notebook_id)
        return oid

    @staticmethod
    def rule_card(item: RetrievedKnowledge) -> RuleCard:
        payload = item.payload
        applies_to = payload.get("applies_to")
        if isinstance(applies_to, list):
            applies = [str(value) for value in applies_to if str(value).strip()]
        elif applies_to:
            applies = [str(applies_to)]
        else:
            applies = []
        return RuleCard(
            id=item.object_id,
            title=str(payload.get("title", "")),
            statement=str(payload.get("statement", "")),
            applies_to=applies,
            recommendation=str(payload.get("recommendation", "")),
            risk_if_ignored=str(payload.get("risk_if_ignored", "")),
            severity=str(payload.get("severity", "medium")),
            status=item.status or "approved",
            owner=item.owner,
            last_reviewed=item.last_reviewed,
            evidence=item.evidence,
        )

    @staticmethod
    def as_retrieved(obj: dict, object_type: str) -> RetrievedKnowledge:
        return RetrievedKnowledge(
            object_id=obj["id"],
            object_type=object_type,
            payload=obj["payload"],
            evidence=obj["evidence"],
            status=obj.get("status", "approved"),
            owner=obj.get("owner", ""),
            last_reviewed=obj.get("last_reviewed", ""),
        )

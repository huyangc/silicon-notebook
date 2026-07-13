from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional

from app.models.schemas import (
    KnowledgeEdge,
    KnowledgeGraph,
    KnowledgeNode,
    KnowledgeRecord,
    KnowledgeTypeCount,
    PaginatedKnowledge,
    RuleCard,
)
from app.services.retrieval import RetrievedKnowledge
from app.services.extraction_profiles import OBJECT_SCHEMAS, OBJECT_TYPE_LABELS, ObjectSchema
from app.services.knowledge_contracts import KnowledgeGraphTooLargeError


class KnowledgeQueryService:
    """Canonical KG search, browser, graph, and detail projections."""

    def __init__(
        self,
        *,
        settings,
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
        memory_retriever=None,
        current_user_id: Callable[[], str] = lambda: "",
    ) -> None:
        self.settings = settings
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
        self.memory_retriever = memory_retriever
        self.current_user_id = current_user_id

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
        try:
            if not self.settings.embedder_configured:
                return []
            index = self.scale_runtime.load(notebook_id)
            if index is None or not index.ann_labels:
                return []
            vector = self.retrieval().embed_query(query)
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
        except Exception:  # noqa: BLE001 - semantic overlay is fail-open
            return []

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
        with self.database.connect() as db:
            lexical = self.knowledge.fts_search(db, notebook_id, query, limit)
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
            counts, labels = self.knowledge.type_counts(db, notebook_id)
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
        schema = self.schemas.effective_schemas().get(object_type)
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
                node_ids, relations = self.knowledge.edge_centrality_source_rows(
                    db, notebook_id, self.settings.edge_centrality_max_nodes
                )
            nodes = {object_id: {"type": "", "name": ""} for object_id in node_ids}
            graph, _idx_to_oid, _oid_to_idx = build_rx_graph(nodes, relations)
            return compute_edge_centrality(graph)

        return self.snapshots.vector_cache.get(
            f"{notebook_id}:edge_centrality", version, load
        )

    def annotate_edge_support(self, notebook_id: str, edges: List[dict]) -> List[dict]:
        retrieval = self.retrieval()
        support = retrieval.edge_support_map(notebook_id)
        if not support:
            return edges
        clusters = retrieval.cluster_map(notebook_id)
        result = []
        for edge in edges:
            key = (
                clusters.get(edge["source_object_id"], edge["source_object_id"]),
                edge["edge_type"],
                clusters.get(edge["target_object_id"], edge["target_object_id"]),
            )
            hit = support.get(key) or support.get((key[2], key[1], key[0]))
            result.append(
                {**edge, "support_count": hit[0], "source_count": hit[1]}
                if hit else edge
            )
        return result

    def concept_detail(self, notebook_id: str, canonical_id: str) -> dict:
        self.catalog.get_notebook(notebook_id)
        with self.database.connect() as db:
            cluster_rows, name = self.knowledge.concept_cluster_detail_rows(
                db, notebook_id, canonical_id
            )
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
        if not member_ids:
            return {
                "canonical_id": canonical_id,
                "canonical_name": name,
                "members": [],
                "attached": [],
                "evidence": [],
            }
        with self.database.connect() as db:
            relation_edges, other_objects = self.knowledge.concept_neighbor_rows(
                db, notebook_id, member_ids
            )
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
        }

    def insert_test_object(
        self, notebook_id: str, object_type: str, payload: dict, source_id: str = ""
    ) -> str:
        with self.database.write() as db:
            return self.knowledge.insert_test_object(
                db, notebook_id, object_type, payload, source_id
            )

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

"""Public retrieval port composed from candidate and graph owners."""
from __future__ import annotations

from typing import Any


class RetrievalService:
    def __init__(self, *, candidates, graph, community_queries) -> None:
        self.candidates = candidates
        self.graph = graph
        self._community_queries = community_queries

    def community_queries(self):
        return self._community_queries()

    def replace_embedder(self, embedder: Any) -> None:
        self.candidates.embedder = embedder
        self.graph.embedder = embedder

    def replace_notebook_languages(
        self, notebook_languages: dict[str, list[str]]
    ) -> None:
        self.candidates._notebook_langs_cache = notebook_languages
        self.graph._notebook_langs_cache = notebook_languages

    def retrieve_scored(self, *args, **kwargs):
        """按关键词 + 语义混合打分检索知识对象 → List[RetrievedKnowledge]。"""
        return self.candidates.retrieve_scored(*args, **kwargs)

    def retrieve_neighbors(self, *args, **kwargs):
        """沿 knowledge_relations 取某对象的 1-hop 邻居 → List[RetrievedKnowledge]。"""
        return self.candidates.retrieve_neighbors(*args, **kwargs)

    def retrieve_elements(self, *args, **kwargs):
        """按 query 检索 source_elements → List[RetrievedElement]。"""
        return self.candidates.retrieve_elements(*args, **kwargs)

    def ppr_retrieve(self, *args, **kwargs):
        """HippoRAG 式 PPR 跨文档传播检索 → List[RetrievedChunk]。"""
        return self.graph.ppr_retrieve(*args, **kwargs)

    def follow_chain(self, *args, **kwargs):
        """沿受控可传递关系做查询期两跳组合 → FollowChainResult。"""
        return self.graph.follow_chain(*args, **kwargs)

    def node_context(self, *args, **kwargs):
        """取某对象的邻域上下文。"""
        return self.graph.node_context(*args, **kwargs)

    def retrieve_relations_scored(self, *args, **kwargs):
        """单 notebook 关系检索(词法∪语义) → List[RetrievedRelation]。"""
        return self.candidates._retrieve_relations_scored(*args, **kwargs)

    def relations_with_names(self, notebook_id, relation_ids=None):
        """Hydrate relation labels/evidence for maintenance diagnostics."""
        with self.candidates._connect() as db:
            return self.candidates._relations_with_names(
                db, notebook_id, relation_ids
            )

    # Ask candidate/graph adapters.  Ask receives these public ports
    # explicitly; the private implementation split remains local here.
    def notebook_languages(self, notebook_id):
        return self.candidates._notebook_langs(notebook_id)

    def chunk_plan(self, notebook_id, queries):
        return self.candidates._build_chunk_retrieval_plan(notebook_id, queries)

    def keyword_chunk_candidates(self, notebook_id, keywords):
        return self.candidates._keyword_chunk_candidates(notebook_id, keywords)

    def retrieve_chunk_candidates(self, notebook_id, query):
        return self.candidates._retrieve_chunks(notebook_id, query)

    def retrieve_chunk_candidates_multi(self, notebook_id, queries):
        return self.candidates._retrieve_chunks_multi(notebook_id, queries)

    def mixed_chunk_candidates(self, notebook_id, query, high_level, queries):
        return self.candidates._mix_retrieve(
            notebook_id, query, high_level, queries
        )

    def merge_chunk_candidates(self, base, extra):
        return self.candidates._union_chunk_candidates(base, extra)

    def select_chunk_candidates(self, scored, ids, matrix, k, lambda_):
        return self.candidates._mmr_select_chunks(
            scored, ids, matrix, k, lambda_
        )

    def has_kg(self, notebook_id):
        return self.candidates._notebook_has_kg(notebook_id)

    def any_base_has_kg(self):
        return self.candidates._any_base_notebook_has_kg()

    def graph_is_large(self, notebook_id):
        return self.candidates._federated_graph_is_large(notebook_id)

    def fuse_graph_seeds(self, notebook_id, question, seeds, cancel_event=None):
        return self.candidates._graph_seed_fusion(
            notebook_id, question, seeds, cancel_event
        )

    def federated_graph(self, notebook_id):
        return self.graph._federated_rx_graph(notebook_id)

    def source_chunks(self, notebook_id, object_ids):
        return self.graph._kg_source_chunks(notebook_id, object_ids)

    def embed_query(self, query):
        return self.candidates._embed_query(query)

    def edge_support_map(self, notebook_id):
        return self.graph._edge_support_map(notebook_id)

    def cluster_map(self, notebook_id):
        return self.graph.cluster_map(notebook_id)

    def concept_cluster_id(self, notebook_id, object_id):
        return self.cluster_map(notebook_id).get(object_id, object_id)

    def runtime_dim(self):
        from app.services.vector_index import resolve_runtime_dim

        return resolve_runtime_dim(self.candidates.settings)

    @staticmethod
    def element_vectors(elements):
        from app.services.retrieval_candidates import CandidateRetrievalService

        return CandidateRetrievalService._element_vectors(elements)

    @staticmethod
    def merge_chunk_candidates(base, extra):
        from app.services.retrieval_candidates import CandidateRetrievalService

        return CandidateRetrievalService._union_chunk_candidates(base, extra)

    @staticmethod
    def in_batches(ids, batch_size: int = 900):
        values = list(dict.fromkeys(ids))
        return (
            values[index:index + batch_size]
            for index in range(0, len(values), batch_size)
        )

    def federated_retrieve(self, *args, **kwargs):
        """跨 tier（base ∪ active）联邦检索 → List[RetrievedKnowledge]。"""
        return self.candidates.federated_retrieve(*args, **kwargs)

    def federated_retrieve_relations(self, *args, **kwargs):
        return self.candidates.federated_retrieve_relations(*args, **kwargs)

"""Public retrieval port composed from candidate and graph owners."""
from __future__ import annotations

from typing import Any


class RetrievalService:
    def __init__(self, *, candidates, graph) -> None:
        self.candidates = candidates
        self.graph = graph

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

    def federated_retrieve(self, *args, **kwargs):
        """跨 tier（base ∪ active）联邦检索 → List[RetrievedKnowledge]。"""
        return self.candidates.federated_retrieve(*args, **kwargs)

    def federated_retrieve_relations(self, *args, **kwargs):
        return self.candidates.federated_retrieve_relations(*args, **kwargs)

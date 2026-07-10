"""检索原语的显式接口层（W2.1）。

reasoning / graph 等消费者应依赖本接口，而非穿透 `SQLiteRepository` 的私有检索方法
（`repo._retrieve_scored` 等）。当前为**委托层**——实现仍在 `SQLiteRepository`；
后续增量把检索实现逐步搬进本 service 时，消费者调用点无需改动（这正是立此接缝的目的）。

刻意用 `*args/**kwargs` 透传：零行为风险（完全转发，由 repo 应用其自身默认值），
避免在两处重复维护默认值而漂移。各方法 docstring 说明语义。
"""
from __future__ import annotations


class RetrievalService:
    """一个 notebook 检索所需的原语集合，委托给持有的 repository。"""

    def __init__(self, repo):
        self._repo = repo

    def retrieve_scored(self, *args, **kwargs):
        """按关键词 + 语义混合打分检索知识对象 → List[RetrievedKnowledge]。"""
        return self._repo._retrieve_scored(*args, **kwargs)

    def retrieve_neighbors(self, *args, **kwargs):
        """沿 knowledge_relations 取某对象的 1-hop 邻居 → List[RetrievedKnowledge]。"""
        return self._repo._retrieve_neighbors(*args, **kwargs)

    def retrieve_elements(self, *args, **kwargs):
        """按 query 检索 source_elements → List[RetrievedElement]。"""
        return self._repo._retrieve_elements(*args, **kwargs)

    def ppr_retrieve(self, *args, **kwargs):
        """HippoRAG 式 PPR 跨文档传播检索 → List[RetrievedChunk]。"""
        return self._repo._ppr_retrieve(*args, **kwargs)

    def follow_chain(self, *args, **kwargs):
        """沿受控可传递关系做查询期两跳组合 → FollowChainResult。"""
        return self._repo._follow_chain(*args, **kwargs)

    def node_context(self, *args, **kwargs):
        """取某对象的邻域上下文。"""
        return self._repo.node_context(*args, **kwargs)

    def federated_retrieve(self, *args, **kwargs):
        """跨 tier（base ∪ active）联邦检索 → List[RetrievedKnowledge]。"""
        return self._repo.federated_retrieve(*args, **kwargs)

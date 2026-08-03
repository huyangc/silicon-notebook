"""Public retrieval port composed from candidate and graph owners."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.services.source_scope import filter_retrieval_items


def _notebook_id(args, kwargs, *, keyword: str = "notebook_id") -> str:
    """Read the leading notebook id without constraining port call style."""
    return str(args[0] if args else kwargs.get(keyword, ""))


class RetrievalService:
    def __init__(self, *, candidates, graph, community_queries) -> None:
        self.candidates = candidates
        self.graph = graph
        self._community_queries = community_queries

    def community_queries(self, settings=None):
        if settings is None:
            return self._community_queries()
        return self._community_queries(settings)

    def replace_embedder(self, embedder: Any) -> None:
        self.candidates.embedder = embedder
        self.graph.embedder = embedder

    def replace_notebook_languages(
        self, notebook_languages: dict[str, list[str]]
    ) -> None:
        self.candidates._notebook_langs_cache = notebook_languages
        self.graph._notebook_langs_cache = notebook_languages

    def preload_scale_artifacts(self, progress=None) -> dict[str, int]:
        """Strict startup preload for scale indexes and reusable hot artifacts."""
        return self.candidates.scale_runtime.preload_retrieval_artifacts(
            progress=progress,
        )

    def retrieve_scored(self, *args, **kwargs):
        """按关键词 + 语义混合打分检索知识对象 → List[RetrievedKnowledge]。"""
        return filter_retrieval_items(
            _notebook_id(args, kwargs), "knowledge",
            self.candidates.retrieve_scored(*args, **kwargs),
        )

    def retrieve_neighbors(self, *args, **kwargs):
        """沿 knowledge_relations 取某对象的 1-hop 邻居 → List[RetrievedKnowledge]。"""
        return filter_retrieval_items(
            _notebook_id(args, kwargs), "knowledge",
            self.candidates.retrieve_neighbors(*args, **kwargs),
        )

    def retrieve_elements(self, *args, **kwargs):
        """按 query 检索 source_elements → List[RetrievedElement]。"""
        return filter_retrieval_items(
            _notebook_id(args, kwargs), "element",
            self.candidates.retrieve_elements(*args, **kwargs),
        )

    def federated_retrieve_elements(self, *args, **kwargs):
        return self.candidates.federated_retrieve_elements(*args, **kwargs)

    def notebook_memory_hits(self, *args, **kwargs):
        return self.candidates.notebook_memory_hits(*args, **kwargs)

    def agent_memory_hits(self, *args, **kwargs):
        return self.candidates.agent_memory_hits(*args, **kwargs)

    def ppr_retrieve(self, *args, **kwargs):
        """HippoRAG 式 PPR 跨文档传播检索 → List[RetrievedChunk]。

        Same accepted second-order cost as ``scoped_subgraph_nodes`` (see its
        docstring): the library-scope filter here is a pure OUTPUT filter.
        ``graph_retrieval._ppr_retrieve`` builds/queries the PPR graph
        (``_ppr_graph``/``scale_ppr``) across every mounted library -- checked
        or not -- and its own ``ranked[: settings.ppr_top_chunks]`` truncation
        happens BEFORE this filter runs, so an excluded library's chunks can
        still occupy some of that fixed budget and get dropped by the
        truncation rather than by scope, shrinking what a checked library gets
        to fill. No excluded content is ever returned -- ``filter_retrieval_items``
        still drops it here -- only recall/budget share is at stake, same as
        the graph-walk case.
        """
        return filter_retrieval_items(
            _notebook_id(args, kwargs), "chunk",
            self.graph.ppr_retrieve(*args, **kwargs),
        )

    def follow_chain(self, *args, **kwargs):
        """沿受控可传递关系做查询期两跳组合 → FollowChainResult。"""
        from app.services.source_scope import current_source_scope, filter_evidence

        notebook_id = _notebook_id(args, kwargs)
        result = self.graph.follow_chain(*args, **kwargs)
        scope = current_source_scope()
        # Two ORTHOGONAL ceilings gate this result: the local checkbox one
        # (`restricted`) and the mounted-library one (`base_restricted`).
        # Testing `source_scope_restricted()` alone hands an unchecked
        # reference library's chains back untouched, because narrowing only
        # the library dimension deliberately leaves `restricted` False — that
        # flag gates the ACTIVE notebook's own PPR/graph expansion and private
        # Memory, which library selection has nothing to do with.
        if scope is None or not (scope.restricted or scope.base_restricted):
            return result
        result.nodes = filter_retrieval_items(notebook_id, "knowledge", result.nodes)
        allowed_nodes = {
            str(node.get("object_id") or node.get("id") or "")
            if isinstance(node, dict) else str(node.object_id)
            for node in result.nodes
        }
        scoped_chains = []
        for chain in result.inferences:
            if not {chain.source_id, chain.via_id, chain.target_id} <= allowed_nodes:
                continue
            scoped_hops = []
            for hop in chain.hops:
                # Library dimension first, exactly as filter_retrieval_items'
                # knowledge/relation branch does: a hop carried by an
                # unchecked reference library is dropped outright.  The
                # cross-notebook arm below keeps such a hop's evidence
                # verbatim, which is right for a library that is still checked
                # and a leak for one that is not.
                if not scope.covers_notebook(hop.notebook_id):
                    scoped_hops = []
                    break
                evidence = (
                    list(hop.evidence)
                    if hop.notebook_id != notebook_id
                    else filter_evidence(notebook_id, hop.evidence)
                )
                if not evidence:
                    scoped_hops = []
                    break
                scoped_hops.append(replace(hop, evidence=evidence))
            if scoped_hops:
                scoped_chains.append(replace(chain, hops=tuple(scoped_hops)))
        result.inferences = scoped_chains
        return result

    def node_context(self, *args, **kwargs):
        """取某对象的邻域上下文。

        The library gate here serves THIS method's own consumer — reasoning's
        query-time chain hydration (``reasoning_retrieval`` calls
        ``retrieval.node_context``) — and nothing else.  It is explicitly NOT
        the gate for ``evidence_context.knowledge_context()``: that function is
        wired to the graph service directly
        (``EvidenceContextService(knowledge=graph)``), never passes through
        here, and needs a stronger gate anyway — emptying the row it re-reads
        would still leave the object's name rendering into the prompt behind a
        live ``k{n}`` anchor.  It carries its own hit-level gate for that
        reason.  ``{}`` is the existing "gone" sentinel.
        """
        from app.services.source_scope import current_source_scope, filter_evidence

        row = self.graph.node_context(*args, **kwargs)
        notebook_id = _notebook_id(args, kwargs)
        scope = current_source_scope()
        if scope is None or not isinstance(row, dict):
            return row
        origin = str(row.get("notebook_id") or notebook_id)
        if not scope.covers_notebook(origin):
            return {}
        # Local dimension unchanged: only the active notebook's own rows are
        # evidence-filtered, and only when the checkboxes narrowed it.
        if not scope.restricted or origin != notebook_id:
            return row
        evidence = filter_evidence(origin, row.get("evidence") or [])
        return {**row, "evidence": evidence} if evidence else {}

    def retrieve_relations_scored(self, *args, **kwargs):
        """单 notebook 关系检索(词法∪语义) → List[RetrievedRelation]。"""
        return filter_retrieval_items(
            _notebook_id(args, kwargs), "relation",
            self.candidates._retrieve_relations_scored(*args, **kwargs),
        )

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

    def lexical_corpus_languages(self, notebook_id):
        """Corpus languages a lexical probe set may be filtered against.

        Distinct from `notebook_languages`: this one honours
        `LEXICAL_LANGUAGE_GATE_ENABLED` and answers `None` when the gate is
        off, which is how "do not filter" reaches the adapters.
        """
        return self.candidates._lexical_corpus_langs(notebook_id)

    def chunk_plan(self, notebook_id, queries):
        return self.candidates._build_chunk_retrieval_plan(notebook_id, queries)

    def keyword_chunk_candidates(self, notebook_id, keywords):
        return filter_retrieval_items(
            notebook_id, "chunk",
            self.candidates._keyword_chunk_candidates(notebook_id, keywords),
        )

    def exact_lookup_chunks(self, notebook_id, query):
        return filter_retrieval_items(
            notebook_id, "chunk",
            self.candidates._exact_lookup_chunks(notebook_id, query),
        )

    def retrieve_chunk_candidates(self, notebook_id, query):
        scored, ids, matrix = self.candidates._retrieve_chunks(notebook_id, query)
        return filter_retrieval_items(notebook_id, "chunk", scored), ids, matrix

    def retrieve_chunk_candidates_multi(self, notebook_id, queries):
        collected, per_query, ids, matrix = self.candidates._retrieve_chunks_multi(
            notebook_id, queries
        )
        allowed = {
            item.chunk_id: item for item in filter_retrieval_items(
                notebook_id, "chunk", collected.values()
            )
        }
        filtered_per_query = [
            {chunk_id: item for chunk_id, item in rows.items() if chunk_id in allowed}
            for rows in per_query
        ]
        return allowed, filtered_per_query, ids, matrix

    def mixed_chunk_candidates(self, notebook_id, query, high_level, queries):
        chunks, kg_block, kg_id_map, kg_hits, ppr_count = self.candidates._mix_retrieve(
            notebook_id, query, high_level, queries
        )
        return (
            filter_retrieval_items(notebook_id, "chunk", chunks),
            kg_block,
            kg_id_map,
            filter_retrieval_items(notebook_id, "knowledge", kg_hits),
            ppr_count,
        )

    def merge_chunk_candidates(self, base, extra):
        return self.candidates._union_chunk_candidates(base, extra)

    def select_chunk_candidates(self, scored, ids, matrix, k, lambda_):
        return self.candidates._mmr_select_chunks(
            scored, ids, matrix, k, lambda_
        )

    def has_kg(self, notebook_id):
        return self.candidates._notebook_has_kg(notebook_id)

    def any_base_has_kg(self, notebook_id):
        return self.candidates._any_base_notebook_has_kg(notebook_id)

    def graph_is_large(self, notebook_id):
        return self.candidates._federated_graph_is_large(notebook_id)

    def fuse_graph_seeds(self, notebook_id, question, seeds, cancel_event=None):
        return self.candidates._graph_seed_fusion(
            notebook_id, question, seeds, cancel_event
        )

    def federated_graph(self, notebook_id):
        return self.graph._federated_rx_graph(notebook_id)

    def source_chunks(self, notebook_id, object_ids):
        return filter_retrieval_items(
            notebook_id, "chunk",
            self.graph._kg_source_chunks(notebook_id, object_ids),
        )

    def embed_query(self, query):
        return self.candidates._embed_query(query)

    def edge_support_map(self, notebook_id):
        return self.graph._edge_support_map(notebook_id)

    def cluster_map(self, notebook_id):
        return self.graph.cluster_map(notebook_id)

    def concept_cluster_id(self, notebook_id, object_id):
        return self.cluster_map(notebook_id).get(object_id, object_id)

    def weak_support_relations(self, notebook_id, object_ids):
        """canonical 层上支撑薄弱的相关边 → List[GapRelationRow](设计文档 §3.3)。"""
        from app.services.source_scope import source_scope_restricted

        if source_scope_restricted():
            return []
        return self.candidates.weak_support_relations(notebook_id, object_ids)

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
        return filter_retrieval_items(
            _notebook_id(args, kwargs, keyword="active_notebook_id"), "knowledge",
            self.candidates.federated_retrieve(*args, **kwargs),
        )

    def federated_retrieve_relations(self, *args, **kwargs):
        return filter_retrieval_items(
            _notebook_id(args, kwargs, keyword="active_notebook_id"), "relation",
            self.candidates.federated_retrieve_relations(*args, **kwargs),
        )

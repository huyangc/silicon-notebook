"""Public retrieval port composed from candidate and graph owners."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.services.retrieval import NeighborExpansion
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

    def retrieve_neighbors(self, *args, **kwargs) -> NeighborExpansion:
        """沿 knowledge_relations 取某对象的 1-hop 邻居 → NeighborExpansion
        (命中 + 是否因每方向邻居上限被截断,截断由调用方披露)。"""
        expansion = self.candidates.retrieve_neighbors(*args, **kwargs)
        return NeighborExpansion(
            filter_retrieval_items(
                _notebook_id(args, kwargs), "knowledge", expansion.hits,
            ),
            expansion.truncated,
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
        ``graph_retrieval._ppr_retrieve`` builds/queries the PPR graph across
        every mounted library -- checked or not -- and its own
        ``ranked[: settings.ppr_top_chunks]`` truncation happens BEFORE this
        filter runs, so an excluded library's chunks can still occupy some of
        that fixed budget. No excluded content is ever returned --
        ``filter_retrieval_items`` still drops it here -- only recall/budget
        share is at stake, same as the graph-walk case.

        Known, deliberately unfixed limitation: ``_federated_graph_is_large``
        (the size guard this and ``_chunk_kg_overlay`` sit behind) walks EVERY
        mounted participant. It cannot consult the per-request scope without
        either publishing a scope-blind cache under a library-less key or
        forcing a full multi-million-node rebuild per checkbox combination, so
        UNCHECKING a library large enough to trip the guard does not turn the
        guard back off.
        """
        return filter_retrieval_items(
            _notebook_id(args, kwargs), "chunk",
            self.graph.ppr_retrieve(*args, **kwargs),
        )

    def follow_chain(self, *args, **kwargs):
        """沿受控可传递关系做查询期两跳组合 → FollowChainResult。"""
        from app.services.source_scope import (
            base_scope_ceiling_active,
            current_source_scope,
            filter_evidence,
            source_scope_ceiling_active,
        )

        notebook_id = _notebook_id(args, kwargs)
        result = self.graph.follow_chain(*args, **kwargs)
        scope = current_source_scope()
        # Two ORTHOGONAL ceilings gate this result: the local checkbox one and
        # the mounted-library one. Testing the local one alone hands an
        # unchecked reference library's chains back untouched, because
        # narrowing only the library dimension deliberately leaves the local
        # answers alone (R1).
        #
        # CEILING, not narrowing: skipping the whole filter is a FILTERING
        # decision, and the frozen snapshots bind on every submitted scope --
        # including the browser's default full selection, where both narrowing
        # answers are False.
        if not (source_scope_ceiling_active() or base_scope_ceiling_active()):
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
                # knowledge/relation branch does: a hop carried by an unchecked
                # reference library is dropped outright. The cross-notebook arm
                # below keeps such a hop's evidence verbatim, which is right for
                # a library that is still checked and a leak for one that is not.
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

        The library gate here serves THIS method's own consumer -- reasoning's
        query-time chain hydration -- and nothing else. It is explicitly NOT
        the gate for ``evidence_context.knowledge_context()``: that function is
        wired to the graph service directly, never passes through here, and
        needs a stronger gate anyway (emptying the row it re-reads would still
        leave the object's name rendering into the prompt behind a live
        ``k{n}`` anchor). ``{}`` is the existing "gone" sentinel.
        """
        from app.services.source_scope import (
            current_source_scope,
            filter_evidence,
            source_scope_ceiling_active,
        )

        row = self.graph.node_context(*args, **kwargs)
        notebook_id = _notebook_id(args, kwargs)
        scope = current_source_scope()
        if scope is None or not isinstance(row, dict):
            return row
        origin = str(row.get("notebook_id") or notebook_id)
        if not scope.covers_notebook(origin):
            return {}
        # Local dimension unchanged: only the active notebook's own rows are
        # evidence-filtered, and only when a frozen local ceiling is in force.
        if not source_scope_ceiling_active() or origin != notebook_id:
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
        from app.services.retrieval import partition_generated_question_chunks

        baseline, supplemental = partition_generated_question_chunks(scored)
        selected = self.candidates._mmr_select_chunks(
            baseline, ids, matrix, k, lambda_
        )
        remaining = max(0, k - len(selected))
        if not remaining or not supplemental:
            return selected
        return selected + self.candidates._mmr_select_chunks(
            supplemental, ids, matrix, remaining, lambda_
        )

    def has_kg(self, notebook_id):
        return self.candidates._notebook_has_kg(notebook_id)

    def any_base_has_kg(self, notebook_id):
        """Does any reference library THIS RUN MAY SEARCH have a knowledge graph?

        The underlying repository query (``_any_base_notebook_has_kg``) is a
        single EXISTS over the mount join, so it answers for EVERY mounted
        library -- checked or not. That makes it the last KG-side gate blind to
        the library dimension: with the active notebook carrying no graph of
        its own and the ONLY graph-bearing library unchecked, it would still
        report "a KG is available", ``ask_service``'s no-KG early exit would
        not fire, ``kg_required`` would not flip, and the graph path would run
        a whole round over a KG this run is forbidden to read. Deciding
        availability from libraries the candidate producers will then filter
        out is the definition of a misleading gate.

        The narrowing goes through the two established seams rather than into
        the SQL: ``participant_notebook_ids``
        (``resolve_participants``/``mount_sql.py`` -- the shared retrieval AND
        authorization predicate, which a per-request checkbox must never
        narrow) followed by ``scoped_participants`` (the consumption-boundary
        filter the collection map and the typed enumerations already use). Same
        list, same predicate, one filter -- so this gate can never disagree
        with what enumeration and federated retrieval consider in scope.

        R1 is preserved: only the BASE dimension is consulted. The active
        notebook is dropped from the participant list and answered separately
        by ``has_kg`` at both call sites, so narrowing local sources cannot
        touch this and unchecking a reference library cannot disable the active
        notebook's own channels.

        Cost. With no base scope submitted this is byte-identical to before:
        one mount-join EXISTS, zero new queries. With a scope submitted it
        becomes one bounded mount read plus at most one indexed EXISTS per
        CHECKED library, short-circuited by ``any()`` -- and zero of the latter
        when every library is unchecked, which is cheaper than before. Paid
        once per run, only on reasoning/graph and only when the active notebook
        has no graph of its own (both call sites short-circuit on ``has_kg``
        first).

        Deliberately gated on ``base_scope_ceiling_active``, not
        ``base_scope_restricted``: a full selection is still a FROZEN
        selection, so answering it from the live mount join would let a library
        mounted after the freeze count toward availability while every
        candidate producer excludes it.

        NOT applied to ``RetrievalCandidates``' own overlay gates
        (``_mix_retrieve``/``_build_chunk_retrieval_plan``): those pick a chunk
        retrieval STRATEGY, and their KG hits are scope-filtered downstream
        anyway, so an unchecked library costs some overlay budget there but
        cannot reach the answer.
        """
        from app.services.source_scope import (
            base_scope_ceiling_active,
            scoped_participants,
        )

        if not base_scope_ceiling_active():
            return self.candidates._any_base_notebook_has_kg(notebook_id)
        return any(
            self.has_kg(base_id)
            for base_id in scoped_participants(
                self.candidates.notebooks.participant_notebook_ids(notebook_id)
            )
            # participant_notebook_ids leads with the active notebook, and
            # covers_notebook() always keeps it -- this gate is about the base
            # dimension only (R1).
            if base_id != notebook_id
        )

    def graph_is_large(self, notebook_id):
        return self.candidates._federated_graph_is_large(notebook_id)

    def unsafe_source_scope_restricted(self, notebook_id: str) -> bool:
        """True for a narrowed scope or an all-selected universe drift."""
        return self.candidates._unsafe_source_scope_restricted(notebook_id)

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

    def hydrate_chunk_candidates(self, candidate_ids):
        """Hydrate a bounded candidate-id set through the public port."""
        return self.candidates.hydrate_chunk_candidates(candidate_ids)

    def hydrate_retrieval_contribution_chunks(
        self, notebook_id: str, actor_id: str, candidate_ids
    ):
        """Hydrate extension proposals under notebook/source SQL ceilings."""
        return self.candidates.hydrate_retrieval_contribution_chunks(
            notebook_id, actor_id, candidate_ids
        )

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

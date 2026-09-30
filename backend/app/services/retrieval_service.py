"""Public retrieval port composed from candidate and graph owners."""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from app.services.evidence_attestation import DEAD, attest_pointers
from app.services.retrieval import NeighborExpansion
from app.services.source_scope import (
    CeilingSet, current_source_scope, filter_evidence, filter_retrieval_items,
    library_source_ceiling, source_allowed,
    node_context_row_within_ceiling, record_ceiling_drift,
    scoped_node_context_row, scoped_source_ceiling, source_ceiling_exists,
    subjectless_run_active,
)


def _bindable_ceiling(scope, notebook_id: str):
    """``notebook_id``'s frozen ceiling as a ``CeilingSet``, so the store's
    ``source_ceiling.ceiling_param`` builds its bound SQL form once per run.

    A per-library ceiling already is one; the local include ceiling is the
    union ``source_ids | hidden_source_ids`` (a plain frozenset), wrapped once
    per retrieval run."""
    from app.services.retrieval_run import memoized_retrieval_value

    ceiling = library_source_ceiling(scope, notebook_id)
    if ceiling is None or isinstance(ceiling, CeilingSet):
        return ceiling
    return memoized_retrieval_value(
        ("weak_support_ceiling", id(scope), notebook_id or scope.notebook_id),
        lambda: CeilingSet(ceiling),
    )


def _notebook_id(args, kwargs, *, keyword: str = "notebook_id") -> str:
    """Read the leading notebook id without constraining port call style."""
    return str(args[0] if args else kwargs.get(keyword, ""))


class RetrievalService:
    def __init__(
        self, *, candidates, graph, community_queries, ceiling_verdict=None,
    ) -> None:
        self.candidates = candidates
        self.graph = graph
        self._community_queries = community_queries
        # ``source_scope.ceiling_binds`` for the current run (production wires
        # ``kg_viewer_scope.NodeContextCeilingVerdict``); without one every
        # existing ceiling binds -- the conservative historical answer.
        self._ceiling_binds = ceiling_verdict or source_ceiling_exists

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

        ``graph_retrieval._ppr_retrieve`` builds/queries the PPR graph across
        every mounted library, and it applies every library's source ceiling
        and the library dimension before its ``ppr_top_chunks`` cut
        (``_PprCeiling``), so an excluded or out-of-ceiling chunk never takes
        one of those slots. Reaching past refused candidates is bounded (a
        library the run admits nothing of is dropped in memory when its scale
        index maps its chunks; otherwise at most ``_PPR_CEILING_WALK_WINDOWS``
        hydration windows past the over-ranked prefix), so when in-ceiling
        passages rank below that reach the call returns fewer than
        ``ppr_top_chunks`` and emits ``ppr_ceiling_walk_exhausted``; this
        filter remains the fail-closed backstop.

        Known, deliberately unfixed limitation: ``_federated_graph_is_large``
        (the size guard this and ``_chunk_kg_overlay`` sit behind) walks EVERY
        mounted participant, because the graphs it guards are BUILT over every
        mounted participant. It cannot consult the per-request scope on its own
        without either publishing a scope-blind cache under a library-less key
        or forcing a full multi-million-node rebuild per checkbox combination,
        so UNCHECKING a library large enough to trip the guard does not turn the
        guard back off. Narrowing the guard alone would be strictly worse than
        the current behaviour: the build it admits still reads the unchecked
        library. Guard and builder move together or not at all.
        """
        return filter_retrieval_items(
            _notebook_id(args, kwargs), "chunk",
            self.graph.ppr_retrieve(*args, **kwargs),
        )

    def follow_chain(self, *args, **kwargs):
        """沿受控可传递关系做查询期两跳组合 → FollowChainResult。

        EVERY hop's evidence goes through ``filter_evidence``, judged against
        the hop's OWN library, on every run -- there is no "no ceiling, hand it
        back as is" exit.  A hop's ``primary_evidence`` is what
        ``render_follow_chain_context`` writes into the prompt as a citable
        relation anchor, so an unfiltered hop is another member's Memory quote
        with a live ``[k]`` behind it.  Whether a ceiling binds is
        ``filter_evidence``'s own question (``ActiveSourceScope.allows``: the
        library dimension, the library's per-notebook ceiling, the local
        ceiling for the scope's own notebook); asking it a second time up here,
        from a list of "which ceilings exist" flags, is how a new kind of
        ceiling ends up skipped.  With no scope, or with no ceiling binding a
        hop's library, ``filter_evidence`` keeps every entry, so such a run's
        chains come back value-identical.

        A hop left with no evidence drops its whole chain (a chain is two
        hops); so does a hop from an excluded library and a chain whose
        endpoint node did not survive the node filter.  The filter is Python
        over evidence the walk already read: no statement binds a source list.
        """
        notebook_id = _notebook_id(args, kwargs)
        result = self.graph.follow_chain(*args, **kwargs)
        scope = current_source_scope()
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
                # Library dimension first, as filter_retrieval_items' knowledge
                # branch does: a hop carried by an unchecked reference library
                # goes outright (its evidence is not the question).
                if scope is not None and not scope.covers_notebook(hop.notebook_id):
                    scoped_hops = []
                    break
                evidence = filter_evidence(hop.notebook_id or notebook_id, hop.evidence)
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

        Serves reasoning's query-time reads (``ReasoningRetriever.get`` -- the
        human-readable node name in the trace).  ``evidence_context
        .knowledge_context()`` does not pass through here (it is wired to the
        graph service directly) but applies the SAME verdict,
        ``scoped_node_context_row``, to the row it re-reads -- one rule, two
        call sites.

        Under a scope: an unchecked library → ``{}`` (the existing "gone"
        sentinel) without reading; otherwise the notebook's source ceiling is
        pushed to the store (``allowed_source_ids``, only when it is not
        ``None`` so an unbounded read stays byte-identical), then the row is
        judged on its ``occurrences`` -- the key real store rows carry (an
        earlier version filtered a nonexistent ``evidence`` key, so every run
        with a binding ceiling got ``{}`` and the trace fell back to raw object
        ids).  A row with no in-ceiling occurrence left → ``{}``.  When the
        run's verdict says the ceiling does not bind, the row is read without
        one and returned as is only if ``node_context_row_within_ceiling``
        verifies it; otherwise the drift is recorded and the row is re-read
        and judged as above.

        The row is judged against ``notebook_id``, the library it was READ from:
        the downstream store (``graph.node_context`` passes straight through to
        ``KnowledgeStore.node_context``) scopes the object lookup to that
        notebook, so the row can belong to no other.  There is no
        ``source_notebook_id`` here -- unlike ``KnowledgeQueryService
        .node_context``, nothing below re-targets the read at another
        participant -- so the argument is rejected outright rather than
        forwarded: a row read from a different library than the one whose
        ceiling was pushed and judged would bypass that library's ceiling.
        """
        if "source_notebook_id" in kwargs:
            raise TypeError(
                "RetrievalService.node_context reads the row from notebook_id "
                "itself; source_notebook_id is not supported (call it with the "
                "participant library's own notebook_id)"
            )
        scope = current_source_scope()
        if scope is None:
            return self.graph.node_context(*args, **kwargs)
        notebook_id = _notebook_id(args, kwargs)
        if not scope.covers_notebook(notebook_id):
            return {}
        if not self._ceiling_binds(notebook_id):
            # The ceiling could not exclude anything when the verdict was taken
            # (``source_scope.ceiling_binds``): the unbounded read, as is --
            # but only once verified on read.  A row naming a source outside
            # the frozen ceiling means the library changed after the verdict:
            # the rest of the run binds, and this row is re-read bound below.
            row = self.graph.node_context(*args, **kwargs)
            if node_context_row_within_ceiling(scope, notebook_id, row):
                return row
            record_ceiling_drift(scope, notebook_id)
        allowed = scoped_source_ceiling(notebook_id)
        if allowed is not None:
            kwargs = {**kwargs, "allowed_source_ids": allowed}
        row = self.graph.node_context(*args, **kwargs)
        if not isinstance(row, dict):
            return row
        scoped = scoped_node_context_row(
            notebook_id, row, ceiling_pushed=allowed is not None
        )
        return {} if scoped is None else scoped

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

    def keyword_arm_available(self):
        """Whether the keyword (FTS) arm can return anything this run.

        Delegates to the channel's own predicate
        (``retrieval_candidates.keyword_arm_available``), so the answer reasoning shows the model
        and the answer the channel acts on cannot drift. Not on
        ``RetrievalPort`` for the same reason as ``keyword_corpus_languages``:
        reasoning is its only consumer and probes it with ``getattr`` (a double
        without it counts as "available", the pre-existing behaviour).
        """
        from app.services.retrieval_candidates import keyword_arm_available

        return keyword_arm_available(self.candidates.settings)

    def keyword_corpus_languages(self, notebook_id):
        """The corpus languages the keyword (FTS) arm's keywords should be in.

        Scoped exactly like that arm (``keyword_chunk_candidates``):

        * single-library run -- this notebook's own languages, the value chunk
          mode hands ``expand_query`` (``notebook_languages``); the arm only
          searches this notebook there, reference libraries included or not;
        * peer (global-ask) run -- the UNION over the libraries the arm
          federates to (``federation_participant_ids``, the same bounded set
          ``_bounded_participants`` gives the peer keyword legs). Each leg
          filters the terms against its own corpus, so a union loses nothing,
          while the nominal active's languages alone would drop every term a
          peer written in the other language could match.

        Returned in the canonical ``("zh", "en")`` order, ``["en"]`` when
        nothing is known (``_notebook_langs``' own empty default). The mode
        test is ``subjectless_run_active`` rather than ``federated_ask_active``:
        this module is not a reader of the participant-override module, and the
        one manager that installs a global run sets both bits together.

        Deliberately NOT on ``RetrievalPort``: reasoning is its only consumer
        and probes it with ``getattr`` (the ``chunk_participant_count``
        precedent), so a retrieval double without it simply means "no hint".
        """
        if not subjectless_run_active():
            return self.notebook_languages(notebook_id)
        from app.services.chunk_federation import federation_participant_ids

        found = set()
        for participant in federation_participant_ids(self.candidates, notebook_id):
            found.update(self.candidates._notebook_langs(participant))
        return [lang for lang in ("zh", "en") if lang in found] or ["en"]

    def lexical_corpus_languages(self, notebook_id):
        """Corpus languages a lexical probe set may be filtered against.

        Distinct from `notebook_languages`: this one honours
        `LEXICAL_LANGUAGE_GATE_ENABLED` and answers `None` when the gate is
        off, which is how "do not filter" reaches the adapters.
        """
        return self.candidates._lexical_corpus_langs(notebook_id)

    def chunk_plan(self, notebook_id, queries):
        return self.candidates._build_chunk_retrieval_plan(notebook_id, queries)

    def chunk_participant_count(self, notebook_id):
        """How many libraries one passage search fans out over (>= 1).

        Same seat, library filter and ``CHUNK_FEDERATION_MAX_PARTICIPANTS``
        bound as the fan-out itself (``federation_participant_ids``, the silent
        form -- no truncation event). The participant set is frozen per
        retrieval run (``_retrieval_participants`` goes through
        ``memoized_retrieval_value``), and the first-round KG search has
        already resolved it, so this is normally zero extra I/O; with a
        participant override it is re-resolved, also without I/O.
        Deliberately NOT on ``RetrievalPort``: the reasoning seed pass is its
        only consumer and reads it with ``getattr``, so a retrieval double
        without it simply counts as one library.
        """
        from app.services.chunk_federation import federation_participant_ids

        return max(1, len(federation_participant_ids(self.candidates, notebook_id)))

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
        """单查询原文段落召回 —— 范围是**参与集**,不再是 active 一本。

        ``chunk_federation`` 在参与集 ≤1(或 ``CHUNK_FEDERATION_ENABLED`` 关)
        时短路回 ``_retrieve_chunks``,所以未挂参考库的笔记本逐值回到今天。

        ``filter_retrieval_items`` 仍留在**外层**,且跨库正确:它的 ``chunk``
        分支按每条候选**自己的** ``notebook_id`` 判 ``covers_notebook`` /
        ``allows``(联邦腿已给每条打上归属库),取消勾选的参考库因此在这道结果
        边界上仍是 fail-closed 的后盾 —— 参与集访问器里的库维度跳过只是成本闸。
        """
        from app.services.chunk_federation import federated_chunk_candidates

        result = federated_chunk_candidates(self.candidates, notebook_id, [query])
        scored = list(result.collected.values())
        return (
            filter_retrieval_items(notebook_id, "chunk", scored),
            result.ids,
            result.matrix,
        )

    def retrieve_chunk_candidates_multi(self, notebook_id, queries):
        """多子查询原文段落召回 —— 同样是参与集口径,四元组形状一字不变。

        ``per_query`` **仍然是每个子查询一组**,各库对同一子查询的命中并进同一
        组。这一点是刻意的,不是顺手:``quota_fuse_baseline_first`` 是**跨组**
        round-robin,组数就是每个子查询配额的分母。若按「(库, 子查询)」出组,
        4 库 × 4 子查询 = 16 组对 16 个席位(当前库被钉死在 4 席),8 库就是
        32 组对 16 席、MOUNT_ORDER 靠后的库一席不得——挂库越多,每个子查询的
        配额被切得越碎。合回子查询维度后,挂参考库只加宽候选池,不重切配额。

        组里的每条候选带的是**该子查询自己**的相关度(provenance 取合并代表),
        与单库腿 ``_retrieve_chunks_multi`` 的 ``per_query`` 同形。统一换成跨
        子查询折叠后的最大值会让同一条候选在每组并列,``quota_fuse`` 的并列取
        最小下标于是把重叠的召回窗整片塌进第一组,后面的查询方向一席不得。

        分组里**不含**任何保底组:``ask_chunk`` 在拿到这份 ``per_query`` 之后还会
        追加自己的关键词/精确命中分组,所以写进分组里的规则只是「关于本模块交出
        的那几组」的规则。当前笔记本的保底改在**融合之后**执行一次,见
        ``retrieval.enforce_active_floor``;它的 floor 由返回的 ``collected``
        自己带着(``chunk_federation.FederatedCollected.active_reserve``)。

        ⚠ 这里按来源范围重建 ``collected`` 之后必须把那个属性接回去
        (``with_active_reserve``),否则保底在这一跳就静默丢了。单参与者短路或
        ``CHUNK_FEDERATION_ACTIVE_RESERVE=0`` 时它返回的仍是普通 ``dict``,
        下游逐值不变。
        """
        from app.services.chunk_federation import (
            federated_chunk_candidates, with_active_reserve,
        )

        result = federated_chunk_candidates(self.candidates, notebook_id, list(queries))
        collected, per_query = result.collected, result.per_query
        ids, matrix = result.ids, result.matrix
        allowed = with_active_reserve(
            {
                item.chunk_id: item for item in filter_retrieval_items(
                    notebook_id, "chunk", collected.values()
                )
            },
            getattr(collected, "active_reserve", 0),
        )
        filtered_per_query = [
            {chunk_id: item for chunk_id, item in rows.items() if chunk_id in allowed}
            for rows in per_query
        ]
        return allowed, filtered_per_query, ids, matrix

    def mixed_chunk_candidates(self, notebook_id, query, high_level, queries):
        chunks, kg_block, kg_id_map, kg_hits, ppr_count = self.candidates._mix_retrieve(
            notebook_id, query, high_level, queries
        )
        kg_block, kg_id_map = self._scoped_overlay(notebook_id, kg_block, kg_id_map)
        return (
            filter_retrieval_items(notebook_id, "chunk", chunks),
            kg_block,
            kg_id_map,
            filter_retrieval_items(notebook_id, "knowledge", kg_hits),
            ppr_count,
        )

    def _scoped_overlay(self, notebook_id, kg_block, kg_id_map):
        """``mixed_chunk_candidates``' KG overlay (``kg_block`` / ``kg_id_map``)
        with only the nodes that survive this run's scope.

        The overlay is prompt text plus live ``k{n}`` anchors, and until now it
        was the one output of the mix path that crossed this boundary
        unfiltered.  A node survives when its library is covered and, where a
        source ceiling binds that library, one of its OWN evidence sources is
        inside it (``source_allowed`` -- the rule ``filter_retrieval_items``
        applies to a KG hit).  The node's library is its id_map ``notebook_id``
        ("" = this run's notebook).  The sources come from one batched read per
        library by object id (``object_support_source_rows``: the reverse index
        when certified, otherwise the evidence JSON projected to source ids in
        SQL), only when a ceiling binds a rendered node's library; every node is
        checked, so no run-level verdict is trusted here.

        Filtering re-renders from structure (``_OverlayStructure``), never by
        editing text: dropped nodes lose their line and anchor, an edge goes
        with either endpoint, and a surviving node whose incoming edge came
        from a dropped node loses its quote -- the renderer takes a node's quote
        from its own incoming edge, i.e. from that edge.  A block that is not
        exactly the renderer's output for this id_map cannot be re-rendered
        safely, so when anything must be dropped from it the whole overlay goes.

        The node quote comes from the incoming edge's evidence, which is not
        checked here.  It is from the node's own source: every relation writer
        is intra-source (``store_kg``, relation completion, ``relink_notebook_kg``,
        Knowhow projection) and no writer re-points a relation's endpoints
        (a manual merge moves evidence and deprecates the merged object, it does
        not touch relations) -- pinned by
        ``test_merge_does_not_repoint_relation_endpoints``.

        Nothing dropped → both values are returned as they came (a run without
        a scope never reads).
        """
        scope = current_source_scope()
        if scope is None or not kg_id_map:
            return kg_block, kg_id_map
        owners = {
            key: str(entry.get("notebook_id") or notebook_id)
            for key, entry in kg_id_map.items()
        }
        dropped = {key for key, owner in owners.items() if not scope.covers_notebook(owner)}
        governed: dict[str, dict[str, str]] = {}
        for key, owner in owners.items():
            if key not in dropped and scope.source_ceiling_binds(owner):
                governed.setdefault(owner, {})[key] = str(
                    kg_id_map[key].get("object_id") or "")
        for owner, keys in governed.items():
            sources = self._object_support_sources(owner, keys.values())
            dropped.update(
                key for key, object_id in keys.items()
                if not any(source_allowed(owner, sid)
                           for sid in sources.get(object_id, ()))
            )
        if not dropped:
            return kg_block, kg_id_map
        structure = _OverlayStructure.parse(kg_block, kg_id_map)
        if structure is None:
            return "", {}
        return structure.without(dropped)

    def _object_support_sources(self, notebook_id, object_ids) -> dict:
        """``{object_id: [source ids]}`` for objects of one library, batched."""
        wanted = [oid for oid in dict.fromkeys(object_ids) if oid]
        found: dict = {}
        store = self.candidates.unified_kg
        with self.candidates._connect() as db:
            for batch in self.candidates._in_batches(wanted):
                for row in store.object_support_source_rows(db, notebook_id, batch):
                    found.setdefault(str(row["object_id"]), []).append(
                        str(row["source_id"] or ""))
        return found

    def merge_chunk_candidates(self, base, extra):
        return self.candidates._union_chunk_candidates(base, extra)

    def select_chunk_candidates(
        self, scored, ids, matrix, k, lambda_, *, active_notebook_id,
    ):
        """MMR 精选,再补上当前笔记本的保底席位。

        MMR 只认候选自己的 ``relevance``,而联邦召回把「几篇短文的当前库」和
        「一个 40 条强命中的参考库」放进了同一个池子:``chunk_mmr_k`` 个席位会
        整片落在参考库,用户问自己刚上传的文档却一条自己的原文都拿不到。保底在
        MMR **之后**做(``chunk_federation.apply_active_reserve`` → 与 multi 分支
        共用的 ``retrieval.enforce_active_floor``),因为 MMR 的多样性排序本身不该
        被改写——只把排在最后的若干个参考库席位换成当前库最高分的落选候选,其余
        位置一字不动。

        这一层就是 single 分支的**最终选择**:``ask_chunk`` 在调本方法之前已经把
        关键词与精确命中并进了 ``scored``,所以不存在「之后还有生产者把保底绕过去」
        的问题(multi 分支有,所以那边的保底落在融合之后)。

        单参与者(或 ``CHUNK_FEDERATION_ENABLED=0``)时每条候选都属于当前库,
        保底恒自动满足,这里逐值回到改动之前。

        ``active_notebook_id`` 必填:本轮真实的当前笔记本 id。PPR 腿、生成问题
        水合腿与第三方贡献者会给当前库自己的行盖上它的原始 id,缺省 ``""`` 会把
        这些行当成参考库——既算错保底,又打穿「池里有没有参考库行」的惰性闸。
        """
        from app.services.chunk_federation import apply_active_reserve
        from app.services.retrieval import partition_generated_question_chunks

        baseline, supplemental = partition_generated_question_chunks(scored)
        selected = self.candidates._mmr_select_chunks(
            baseline, ids, matrix, k, lambda_
        )
        remaining = max(0, k - len(selected))
        if remaining and supplemental:
            selected = selected + self.candidates._mmr_select_chunks(
                supplemental, ids, matrix, remaining, lambda_
            )
        return apply_active_reserve(
            self.candidates.settings, selected, scored, k,
            active_notebook_id=active_notebook_id,
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
        the SQL: ``candidates._retrieval_participants`` -- the participant SEAT,
        whose own fallback is ``resolve_participants``/``mount_sql.py``, the
        shared retrieval AND authorization predicate a per-request checkbox must
        never narrow -- followed by ``scoped_participants`` (the
        consumption-boundary filter the collection map and the typed
        enumerations already use). Same list, same predicate, one filter -- so
        this gate can never disagree with what enumeration and federated
        retrieval consider in scope.

        Deliberately the seat rather than a direct
        ``retrieval_participants`` import: the override module's reader
        whitelist is five files and every addition to it has to be a reviewed
        edit (``backend/tests/test_participant_override_guard.py``). Reading the
        seat gets this gate the override for free without widening that list,
        and the no-override branch below reaches the same answer through
        ``_any_base_notebook_has_kg``, which is override-aware on its own.

        R1 is preserved: only the BASE dimension is consulted. The active
        notebook is dropped from the participant list and answered separately
        by the ``has_kg`` half of the same expression, so narrowing local
        sources cannot
        touch this and unchecking a reference library cannot disable the active
        notebook's own channels.

        Cost. With no base scope submitted this is byte-identical to before:
        one mount-join EXISTS, zero new queries. With a scope submitted it
        becomes one bounded mount read plus at most one indexed EXISTS per
        CHECKED library, short-circuited by ``any()`` -- and zero of the latter
        when every library is unchecked, which is cheaper than before. Paid
        once per run, only on reasoning/graph and only when the active notebook
        has no graph of its own (the ``or`` in
        ``reasoning_retrieval.kg_in_scope_for`` short-circuits on ``has_kg``
        first). That helper is now the SINGLE evaluation point -- both
        consumers of this fact (``ask_service``'s ``no_usable_kg`` early exit
        and ``ReasoningRetriever``'s graph gates -- the no-graph disclosure,
        the planner's KG type vocabulary, the five graph actions; since
        2026-09-29 the passage seed no longer reads it) read it through the same
        request-level memo, so the pair is computed at most once per request
        rather than once per call site.

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
            current_source_scope,
            scoped_participants,
        )

        scope = current_source_scope()
        total = scope is not None and scope.ceilings_total
        if not base_scope_ceiling_active() and not total:
            return self.candidates._any_base_notebook_has_kg(notebook_id)
        # ``ceilings_total`` (every default ceiling, every global run) is a
        # frozen library set too: a library mounted after the freeze has no
        # entry and is not in, and one frozen to ``frozenset()`` (skipped
        # because its sources could not be read) contributes nothing, so
        # neither may make a graph look available.
        return any(
            self.has_kg(base_id)
            for base_id in scoped_participants(
                participant_id
                for participant_id, _tier in
                self.candidates._retrieval_participants(notebook_id)
            )
            # The seat leads with the active notebook, and covers_notebook()
            # always keeps it -- this gate is about the base dimension only
            # (R1).
            if base_id != notebook_id
            and not (total and scope.source_ceiling_for(base_id) == frozenset())
        )

    def unsafe_source_scope_restricted(self, notebook_id: str) -> bool:
        """True for a narrowed scope or an all-selected universe drift."""
        return self.candidates._unsafe_source_scope_restricted(notebook_id)

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
        """canonical 层上支撑薄弱的相关边 → List[GapRelationRow](设计文档 §3.3)。

        The rows become reflect-prompt text (both endpoint names), so the source
        ceiling is applied BEFORE the names are read -- a result-side filter
        cannot help, the rows carry no evidence.  A narrowed run gets nothing
        (the channel is off, as before).  Otherwise, when a source ceiling binds
        the library:

        * the run's verdict says it binds (``ceiling_binds``: another member's
          Memory in the library, drift, a per-library freeze, a subjectless run)
          → both store reads (``weak_support_relation_rows`` /
          ``relation_endpoint_name_rows``) are bounded: an edge is kept only
          when its TARGET still has an in-ceiling object and its sample relation
          itself is in-ceiling.  When the ceiling is this notebook's own
          all-selected freeze and still matches the library
          (``_weak_support_viewer_form``), "in-ceiling" is stated as "not
          derived from another member's Memory" (``viewer_id``, one scalar);
          otherwise the frozen list is bound once per statement
          (``allowed_source_ids``, a ``CeilingSet`` so its bound form is built
          once per run);
        * the verdict says it does not → the unbounded reads (no list bound,
          the statements of a run without a scope), verified on read: every
          sample relation's ``source_id`` must be inside the frozen ceiling.
          The first that is not records the drift for the rest of the run
          (``record_ceiling_drift``, the verdict ``node_context`` shares) and
          the probe is re-read bound.  A sample relation with no source is
          outside too (as under ``allows`` and the bound statement) but is not
          a change after the freeze: it drops its own row only.  What the check
          cannot see is the target's support; an in-ceiling relation's
          endpoints come from that same source (store_kg mints objects per
          source), which is what makes the sample the row's evidence.

        No scope, or no ceiling binding the library: the historical call.
        """
        from app.services.source_scope import source_scope_restricted

        if source_scope_restricted():
            return []
        scope = current_source_scope()
        if scope is None or not scope.source_ceiling_binds(notebook_id):
            if scope is not None and not scope.covers_notebook(notebook_id):
                return []
            return self.candidates.weak_support_relations(notebook_id, object_ids)
        object_ids = list(object_ids)
        # The local ``exclude`` shape (direct service callers only) has no
        # allow-list to bind: the unbounded read, judged row by row, is all
        # there is for it.
        bindable = library_source_ceiling(scope, notebook_id) is not None
        if not bindable or not self._ceiling_binds(notebook_id):
            ceiling = library_source_ceiling(scope, notebook_id)
            drifted = []

            def inside(source_id: str) -> bool:
                if not source_id:
                    # Outside, as under ``allows`` and the bound statement's
                    # ``member_of`` -- but a relation with no source is not a
                    # change after the freeze, so it drops only its own row.
                    return False
                ok = (source_id in ceiling if ceiling is not None
                      else scope.allows(notebook_id, source_id))
                if not ok:
                    drifted.append(source_id)
                return ok

            rows = self.candidates.weak_support_relations(
                notebook_id, object_ids, sample_source_inside=inside,
            )
            if not drifted or not bindable:
                return rows
            record_ceiling_drift(scope, notebook_id)
        if self._weak_support_viewer_form(scope, notebook_id):
            return self.candidates.weak_support_relations(
                notebook_id, object_ids, viewer_id=scope.owner_id,
            )
        ceiling = _bindable_ceiling(scope, notebook_id)
        if ceiling is None:  # unreachable (``bindable``); fail closed, never unbound
            return []
        return self.candidates.weak_support_relations(
            notebook_id, object_ids, allowed_source_ids=ceiling,
        )

    def _weak_support_viewer_form(self, scope, notebook_id) -> bool:
        """May the bound weak-support probe say "not derived from another
        member's Memory" (``viewer_id``, one scalar) instead of binding the
        frozen list?

        Only when the two say the same thing: the ceiling is this notebook's
        own all-selected local freeze (include, not narrowed -- a narrowed run
        never gets here), no per-library freeze, not a subjectless run, and the
        library's visible universe and the asker's hidden half still equal the
        frozen lists, re-probed now (``unsafe_source_scope_restricted``, the
        per-call drift probe -- never memoised).  Then the ceiling is exactly
        "everything this asker may read", and what it excludes is another
        member's Memory, which ``memory_sql.foreign_memory_*`` states in SQL.
        Anything else -- drift, a per-library or deny-all freeze, a global run
        -- binds the list.
        """
        if (
            scope.subjectless
            or scope.source_ceiling_for(notebook_id) is not None
            or (notebook_id and notebook_id != scope.notebook_id)
            or scope.mode != "include"
            or scope.narrowed is not False
            or not library_source_ceiling(scope, notebook_id)
        ):
            return False
        return not self.unsafe_source_scope_restricted(notebook_id)

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


_CHAIN_KEY = re.compile(r"  \[(k\d+|\?)\] ")
_CHAIN_TARGET = re.compile(r"--> \[(k\d+)\] ")
_CHAIN_TIER = re.compile(r"  \(tier=([^\n]*)\)(?=\n|$)")


class _OverlayStructure:
    """The chunk-mix KG overlay as data: ``kg_id_map`` (the nodes, in render
    order) plus the edges ``(src_key, edge_type, tgt_key, tier)`` of its
    ``chain:`` section, with the block's text a pure function of the two
    (``kg.graph_reason.render_subgraph_context``'s format).

    ``parse`` never splits a node line on its text: every node line is known
    exactly from the id_map (a quote with a newline, or a line that looks
    like another entry, stays inside the node line it belongs to).  Only the
    chain section is read, with each name taken from the id_map, and the
    result is accepted only if rendering it gives back the block byte for
    byte -- anything else answers ``None`` (the caller then drops the whole
    overlay rather than guess)."""

    def __init__(self, id_map: dict, edges: list) -> None:
        self.id_map = id_map
        self.edges = edges

    @staticmethod
    def _node_line(key: str, entry: dict) -> str:
        quote = entry.get("snippet") or ""
        suffix = f'  — ev: "{quote}"' if quote else ""
        return (f"{key}: [{entry.get('object_type', '')}]"
                f"[{entry.get('tier', '')}] {entry.get('name', '')}{suffix}")

    def render(self) -> str:
        lines = [self._node_line(key, entry) for key, entry in self.id_map.items()]
        chain = []
        for src, edge_type, tgt, tier in self.edges:
            src_name = self.id_map[src].get("name", "") if src in self.id_map else ""
            tgt_name = self.id_map[tgt].get("name", "")
            chain.append(
                f"  [{src}] {src_name} --{edge_type}--> [{tgt}] {tgt_name}  "
                f"(tier={tier})".rstrip())
        if chain:
            lines.append("chain:")
            lines.extend(chain)
        return "\n".join(lines) if lines else "(none)"

    @classmethod
    def parse(cls, kg_block: str, kg_id_map: dict) -> "_OverlayStructure | None":
        id_map = dict(kg_id_map)
        head = "\n".join(cls._node_line(key, entry) for key, entry in id_map.items())
        if not kg_block.startswith(head):
            return None
        rest = kg_block[len(head):]
        edges: list = []
        if rest:
            if not rest.startswith("\nchain:\n"):
                return None
            rest = rest[len("\nchain:\n"):]
            pos = 0
            while pos < len(rest):
                if edges:
                    if rest[pos] != "\n":
                        return None
                    pos += 1
                match = _CHAIN_KEY.match(rest, pos)
                if match is None:
                    return None
                src = match.group(1)
                pos = match.end()
                src_name = id_map[src].get("name", "") if src in id_map else ""
                if not rest.startswith(f"{src_name} --", pos):
                    return None
                pos += len(src_name) + 3
                arrow = rest.find("--> [", pos)
                target = _CHAIN_TARGET.match(rest, arrow) if arrow >= 0 else None
                if target is None or target.group(1) not in id_map:
                    return None
                edge_type, tgt = rest[pos:arrow], target.group(1)
                pos = target.end()
                tgt_name = id_map[tgt].get("name", "")
                if not rest.startswith(tgt_name, pos):
                    return None
                tier = _CHAIN_TIER.match(rest, pos + len(tgt_name))
                if tier is None:
                    return None
                edges.append((src, edge_type, tgt, tier.group(1)))
                pos = tier.end()
        structure = cls(id_map, edges)
        return structure if structure.render() == kg_block else None

    def without(self, dropped: set) -> tuple[str, dict]:
        """``(kg_block, kg_id_map)`` without the ``dropped`` node keys.  An edge
        goes with either endpoint; a surviving node whose incoming edge came
        from a dropped node loses its quote (its line's quote is that edge's)."""
        unquote = {
            tgt for src, _type, tgt, _tier in self.edges
            if src in dropped and tgt not in dropped
        }
        id_map = {
            key: ({**entry, "snippet": ""} if key in unquote else entry)
            for key, entry in self.id_map.items() if key not in dropped
        }
        if not id_map:
            return "", {}
        edges = [
            edge for edge in self.edges
            if edge[0] not in dropped and edge[2] not in dropped
        ]
        return _OverlayStructure(id_map, edges).render(), id_map


FOLLOW_CHAIN_PRODUCER = "follow_chain"


def attest_chain_evidence(inferences: list) -> list:
    """Register the evidence each derived-chain hop will be cited by (PR-D).

    A hop becomes a citable relation anchor through its ``primary_evidence``
    (``kg.follow_chain.render_follow_chain_context``), and only that entry, so
    that is what is registered: ONE batched pointer read per ``follow_chain``
    call, over the chains that survived every filter. The stored relation
    evidence carries a quote, not the element's full text, hence a pointer
    read rather than a hash.

    A primary whose element was already gone at retrieval time (J2) loses its
    locator: the relation still renders, but its anchor no longer names a row
    that opens on nothing and is judged at source level only (J3). Returns the
    input list itself when nothing was dead, which includes every call outside
    a global run (the seam answers ``{}`` there). The input chains, hops and
    evidence entries are never modified: a hop with a dead primary is copied.

    Lives beside ``RetrievalService.follow_chain`` rather than in
    ``kg.follow_chain``: that module is the pure, storage-free composer and
    renderer, while this spends a bounded store read through the run's seat,
    exactly like the ceiling filter on chains above.
    """
    states = attest_pointers(FOLLOW_CHAIN_PRODUCER, (
        str(hop.primary_evidence.get("element_id") or "")
        for chain in inferences for hop in chain.hops
    ))
    if DEAD not in states.values():
        return inferences
    return [
        replace(chain, hops=tuple(
            _hop_without_dead_primary(hop, states) for hop in chain.hops
        ))
        for chain in inferences
    ]


def _hop_without_dead_primary(hop, states: dict):
    """A copy of ``hop`` with its dead primary entry's element id cleared."""
    primary = hop.primary_evidence
    if states.get(str(primary.get("element_id") or "")) != DEAD:
        return hop
    return replace(hop, evidence=[
        {**entry, "element_id": ""} if entry is primary else entry
        for entry in hop.evidence
    ])

from app.models.common import Evidence
from app.models.source_scope import SourceScope
from app.models.notebooks import NotebookSummary
from app.api.ask_routes import _validate_source_scope
from fastapi import HTTPException
import pytest
from app.services.retrieval import (
    RetrievedChunk,
    RetrievedElement,
    RetrievedKnowledge,
)
from app.services.kg.follow_chain import ChainHop, FollowChainResult, InferredChain
from app.services.retrieval_service import RetrievalService
from app.services.source_scope import (
    filter_retrieval_items,
    scoped_allowed_source_ids,
    scoped_conversation_history,
    source_allowed,
    source_scope_restricted,
    source_scope_context,
    source_scope_visible_universe_matches,
)
from app.services.reasoning_retrieval import (
    ReasoningRetriever,
    ReflectDecision,
    SubQuery,
)


def _knowledge(source_id: str, *, notebook_id: str = "nb") -> RetrievedKnowledge:
    return RetrievedKnowledge(
        object_id=f"ko-{source_id}",
        object_type="claim",
        payload={"name": source_id},
        evidence=[Evidence(
            source_id=source_id,
            source_title=source_id,
            element_id=f"el-{source_id}",
            element_type="paragraph",
            location_label="p1",
            quoted_span="evidence",
            confidence=1.0,
        )],
        notebook_id=notebook_id,
    )


def test_omitted_or_default_exclude_scope_preserves_historical_behavior():
    chunks = [
        RetrievedChunk("c1", "s1", "one", "", "one"),
        RetrievedChunk("c2", "s2", "two", "", "two"),
    ]
    assert filter_retrieval_items("nb", "chunk", chunks) == chunks
    with source_scope_context("nb", SourceScope(mode="exclude", source_ids=[])):
        assert filter_retrieval_items("nb", "chunk", chunks) == chunks


def test_include_scope_filters_active_chunks_and_kg_evidence_but_keeps_base():
    chunks = [
        RetrievedChunk("c1", "s1", "one", "", "one"),
        RetrievedChunk("c2", "s2", "two", "", "two"),
        RetrievedChunk("cb", "base-source", "base", "", "base", notebook_id="base"),
    ]
    knowledge = [_knowledge("s1"), _knowledge("s2"), _knowledge("base-source", notebook_id="base")]
    with source_scope_context("nb", SourceScope(mode="include", source_ids=["s1"])):
        assert [row.chunk_id for row in filter_retrieval_items("nb", "chunk", chunks)] == [
            "c1", "cb"
        ]
        assert [row.object_id for row in filter_retrieval_items(
            "nb", "knowledge", knowledge
        )] == ["ko-s1", "ko-base-source"]


def test_empty_include_scope_removes_all_active_sources_but_not_mounted_base():
    with source_scope_context("nb", SourceScope(mode="include", source_ids=[])):
        assert source_allowed("nb", "s1") is False
        assert source_allowed("base", "base-source") is True


def test_restricted_scope_drops_prior_conversation_history():
    assert scoped_conversation_history("prior answer") == "prior answer"
    with source_scope_context("nb", SourceScope(mode="include", source_ids=["s1"])):
        assert scoped_conversation_history("prior answer from s2") == ""


def test_scoped_dict_nodes_keep_only_selected_evidence():
    nodes = [
        {
            "object_id": "k1",
            "notebook_id": "nb",
            "evidence": [{"source_id": "s1"}, {"source_id": "s2"}],
        },
        {
            "object_id": "k2",
            "notebook_id": "nb",
            "evidence": [{"source_id": "s2"}],
        },
    ]
    with source_scope_context("nb", SourceScope(mode="include", source_ids=["s1"])):
        filtered = filter_retrieval_items("nb", "knowledge", nodes)
    assert [row["object_id"] for row in filtered] == ["k1"]
    assert filtered[0]["evidence"] == [{"source_id": "s1"}]


def test_follow_chain_replaces_hop_evidence_with_scoped_copy():
    def hop(relation_id: str, source: str, target: str, evidence):
        return ChainHop(
            relation_id=relation_id,
            notebook_id="nb",
            tier="personal",
            source_object_id=source,
            target_object_id=target,
            edge_type="precedes",
            source_name=source,
            target_name=target,
            evidence=evidence,
        )

    first = hop("r1", "a", "b", [
        {"source_id": "s2", "quoted_span": "must not leak"},
        {"source_id": "s1", "quoted_span": "selected"},
    ])
    second = hop("r2", "b", "c", [
        {"source_id": "s1", "quoted_span": "selected second"},
    ])
    chain = InferredChain(
        source_object_id="a",
        via_object_id="b",
        target_object_id="c",
        source_name="a",
        via_name="b",
        target_name="c",
        inferred_edge_type="precedes",
        hops=(first, second),
    )
    nodes = [
        {"object_id": oid, "notebook_id": "nb", "evidence": [{"source_id": "s1"}]}
        for oid in ("a", "b", "c")
    ]

    class _Graph:
        def follow_chain(self, *_args, **_kwargs):
            return FollowChainResult(inferences=[chain], nodes=nodes)

    retrieval = RetrievalService(
        candidates=object(), graph=_Graph(), community_queries=lambda: []
    )
    with source_scope_context("nb", SourceScope(mode="include", source_ids=["s1"])):
        result = retrieval.follow_chain("nb", "a")

    assert len(result.inferences) == 1
    assert result.inferences[0].hops[0].evidence == [
        {"source_id": "s1", "quoted_span": "selected"}
    ]


class _ScopeRepo:
    def __init__(self, visible: list[str], count: int, hidden=None):
        self.visible = visible
        self.count = count
        self.hidden = list(hidden or [])

    def visible_source_ids(self, _notebook_id, source_ids):
        return [source_id for source_id in source_ids if source_id in self.visible]

    def visible_source_count(self, _notebook_id):
        return self.count

    def visible_source_scope_snapshot(self, _notebook_id, source_ids):
        return self.visible_source_ids(_notebook_id, source_ids), self.count

    def all_visible_source_ids(self, _notebook_id):
        return list(self.visible)

    def all_hidden_source_ids(self, _notebook_id):
        return list(self.hidden)


def _notebook(*, bases=None):
    return NotebookSummary(
        id="nb", name="n", purpose="", primary_domain="", status="ready",
        counts={}, created_label="", base_notebooks=bases or [],
    )


def test_empty_local_scope_requires_a_mounted_base():
    with pytest.raises(HTTPException) as exc:
        _validate_source_scope(
            _ScopeRepo([], 0), _notebook(),
            SourceScope(mode="include", source_ids=[]),
        )
    assert exc.value.status_code == 409


def test_scope_rejects_cross_notebook_source_ids():
    with pytest.raises(HTTPException) as exc:
        _validate_source_scope(
            _ScopeRepo(["s1"], 1), _notebook(),
            SourceScope(mode="include", source_ids=["foreign"]),
        )
    assert exc.value.status_code == 422


def test_compact_include_never_materializes_the_visible_source_universe():
    class _CompactRepo(_ScopeRepo):
        def all_visible_source_ids(self, _notebook_id):
            raise AssertionError("compact include must not enumerate the universe")

    resolved = _validate_source_scope(
        _CompactRepo(["selected"], 10_000),
        _notebook(),
        SourceScope(mode="include", source_ids=["selected"]),
    )

    assert resolved == SourceScope(
        mode="include", source_ids=["selected"], narrowed=True
    )


def test_sqlite_compact_scope_snapshot_matches_visibility_and_count(tmp_path, monkeypatch):
    from app.core.config import Settings
    from app.models.schemas import NotebookCreate
    from app.services.sqlite_repository import SQLiteRepository

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'scope.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    repo = SQLiteRepository(Settings(_env_file=None))
    notebook_id = repo.create_notebook(NotebookCreate(name="scope")).id
    store = repo._runtime.source_store
    for source_id, source_type in (
        ("s1", "markdown"),
        ("s2", "pdf"),
        ("hidden-memory", "memory"),
    ):
        store.insert_source(
            source_id=source_id,
            notebook_id=notebook_id,
            title=source_id,
            source_type=source_type,
            status="extracted",
            parse_status="extracted",
            file_name=f"{source_id}.md",
            file_path=f"/tmp/{source_id}.md",
            file_size=0,
            file_hash="",
            summary="",
            doc_type="",
        )

    assert store.visible_source_scope_snapshot(
        notebook_id, ["s2", "foreign"]
    ) == (["s2"], 2)
    assert store.visible_source_scope_snapshot(notebook_id, []) == ([], 2)

    # Requested rows stay O(selected) even beyond the ordinary IN-clause
    # threshold; the implementation must not materialize the source universe.
    store.IN_CHUNK = 1
    assert store.visible_source_scope_snapshot(
        notebook_id, ["s2", "foreign", "s1"]
    ) == (["s2", "s1"], 2)

    with store.database.connect() as db:
        plan = db.execute(
            "EXPLAIN QUERY PLAN WITH requested(id, ordinal) AS ("
            "SELECT CAST(value AS TEXT), CAST(key AS INTEGER) FROM json_each(?)"
            ") SELECT requested.id FROM requested "
            "CROSS JOIN sources ON sources.id=requested.id "
            "WHERE sources.notebook_id=? AND "
            "source_type NOT IN ('memory', 'knowhow')",
            ('["s2", "foreign", "s1"]', notebook_id),
        ).fetchall()
    details = [str(row["detail"]) for row in plan]
    requested_scan = next(
        index for index, detail in enumerate(details) if "json_each" in detail
    )
    source_probe = next(
        index for index, detail in enumerate(details)
        if "sources" in detail and "SEARCH" in detail
    )
    assert requested_scan < source_probe
    assert "id=?" in details[source_probe].replace(" ", "")


def test_exclusion_scope_is_frozen_to_an_explicit_allow_list():
    resolved = _validate_source_scope(
        _ScopeRepo(["s1", "s2", "s3"], 3),
        _notebook(),
        SourceScope(mode="exclude", source_ids=["s2"], narrowed=False),
    )
    assert resolved == SourceScope(
        mode="include", source_ids=["s1", "s3"], narrowed=True
    )


def test_all_selected_is_frozen_but_not_misclassified_as_narrowed():
    resolved = _validate_source_scope(
        _ScopeRepo(["s1"], 1, hidden=["hidden-memory", "hidden-knowhow"]),
        _notebook(),
        SourceScope(mode="exclude", source_ids=[], narrowed=True),
    )

    assert resolved.mode == "include"
    assert resolved.source_ids == ["s1"]
    assert resolved.narrowed is False
    assert "hidden_source_ids" not in resolved.model_dump()
    assert "hidden_source_ids" not in SourceScope.model_json_schema()["properties"]
    with source_scope_context("nb", resolved):
        assert scoped_conversation_history("prior answer") == "prior answer"
        assert source_scope_restricted() is False
        assert scoped_allowed_source_ids("nb") == (
            "hidden-knowhow", "hidden-memory", "s1"
        )
        assert source_allowed("nb", "s1") is True
        assert source_allowed("nb", "hidden-memory") is True
        assert source_allowed("nb", "hidden-knowhow") is True
        assert source_allowed("nb", "concurrently-added") is False
        assert source_scope_visible_universe_matches(
            "nb", ["s1"], ["hidden-memory", "hidden-knowhow"]
        ) is True
        assert source_scope_visible_universe_matches(
            "nb", ["s1", "s2"], ["hidden-memory", "hidden-knowhow"]
        ) is False
        assert source_scope_visible_universe_matches(
            "nb", ["s1"], ["hidden-memory", "hidden-knowhow", "new-hidden"]
        ) is False


def test_narrowed_scope_excludes_hidden_projection_sources():
    resolved = _validate_source_scope(
        _ScopeRepo(["s1", "s2"], 2, hidden=["hidden-memory"]),
        _notebook(),
        SourceScope(mode="include", source_ids=["s1"]),
    )

    with source_scope_context("nb", resolved):
        assert source_scope_restricted() is True
        assert source_scope_visible_universe_matches("nb", ["s1", "s2", "s3"]) is True
        assert scoped_allowed_source_ids("nb") == ("s1",)
        assert source_allowed("nb", "hidden-memory") is False


def test_candidate_detects_hidden_participant_drift_through_source_store():
    from app.services.retrieval_candidates import CandidateRetrievalService

    class _Sources:
        visible = ["s1"]
        hidden = ["hidden-memory"]

        def all_visible_source_ids(self, _notebook_id):
            return list(self.visible)

        def all_hidden_source_ids(self, _notebook_id):
            return list(self.hidden)

    class _Candidates:
        sources = _Sources()

    scope = SourceScope(mode="include", source_ids=["s1"], narrowed=False)
    scope._hidden_source_ids = ["hidden-memory"]
    with source_scope_context("nb", scope):
        assert CandidateRetrievalService._unsafe_source_scope_restricted(
            _Candidates(), "nb"
        ) is False
        _Candidates.sources.hidden.append("new-hidden")
        assert CandidateRetrievalService._unsafe_source_scope_restricted(
            _Candidates(), "nb"
        ) is True


def test_candidate_without_active_scope_does_not_touch_source_store():
    from app.services.retrieval_candidates import CandidateRetrievalService

    # Some bounded adapters construct only the producer fields they use.  An
    # omitted checkbox scope must retain the historical zero-probe path and
    # therefore must not require the optional ``sources`` field at all.
    candidate = CandidateRetrievalService.__new__(CandidateRetrievalService)
    assert candidate._unsafe_source_scope_restricted("nb") is False


def test_explicit_include_of_the_whole_universe_is_not_narrowed():
    resolved = _validate_source_scope(
        _ScopeRepo(["s1", "s2"], 2),
        _notebook(),
        SourceScope(mode="include", source_ids=["s1", "s2"]),
    )

    assert resolved.narrowed is False


def test_checkbox_ceiling_intersects_producer_allow_list_and_leaves_base_alone():
    with source_scope_context(
        "nb", SourceScope(mode="include", source_ids=["s1", "s2"])
    ):
        assert scoped_allowed_source_ids("nb") == ("s1", "s2")
        assert scoped_allowed_source_ids("nb", ["s2", "s3"]) == ("s2",)
        assert scoped_allowed_source_ids("base", ["b1"]) == ("b1",)


def _call_line(func, callee: str) -> int:
    """按 AST 定位一次真实调用的行号。

    刻意不做 `source.index(name)` 那种文本查找:那样连 docstring 和注释里的
    同名字样都算数,于是「把真实调用挪到后面、同时在函数开头的注释里提到它」
    就能骗过顺序断言。这两个函数的 docstring 正好就在上方,不是臆想的场景。
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            getattr(node.func, "attr", None) == callee
            or getattr(node.func, "id", None) == callee
        )
    ]
    assert lines, f"未找到对 {callee} 的调用"
    return min(lines)


def test_reasoning_submission_is_validated_before_a_durable_job_exists():
    """两条 Ask 路径都必须在发布持久 job 之前校验提交。

    以前包着它的路由预检(_validate_reasoning_scope_preflight)随模型判断来源
    一起删了——那道预检唯一的工作就是把模型选中的来源集与勾选上限取交集。
    必须活下来的是**顺序**:无效的 reasoning 提交仍要在持久 job / stream 头
    出现之前失败,否则用户会看到一个已经发布、注定失败的会话。

    同步与流式两条路径分别由不同模块实现,所以两条都要钉。
    """
    from app.services.ask_execution import AskExecutionCoordinator
    from app.services.ask_service import AskService

    assert (
        _call_line(AskService.ask_current, "validate_reasoning_submission")
        < _call_line(AskService.ask_current, "begin_job_current")
    ), "同步 /ask:校验必须在 begin_job_current 之前"

    assert (
        _call_line(AskExecutionCoordinator.start, "validate")
        < _call_line(AskExecutionCoordinator.start, "begin_durable_job")
    ), "流式 /ask/stream:校验必须在 begin_durable_job 之前"


def test_scoped_chunk_overlay_keeps_base_seeds_without_whole_graph_io():
    from app.services.retrieval_candidates import CandidateRetrievalService

    base_hit = _knowledge("base-source", notebook_id="base")
    base_hit.tier = "base"

    class _Candidates:
        _MIX_NODE_SEEDS = 8

        def federated_retrieve(self, *_args, **_kwargs):
            return [base_hit]

        def _federated_graph_is_large(self, *_args, **_kwargs):
            raise AssertionError("scoped direct seeds must not inspect whole graph")

        def federated_retrieve_relations(self, *_args, **_kwargs):
            raise AssertionError("scoped overlay must not retrieve relations")

    with source_scope_context(
        "nb", SourceScope(mode="include", source_ids=[])
    ):
        block, id_map, hits, supports = (
            CandidateRetrievalService._chunk_kg_overlay(
                _Candidates(), "nb", "question", "", 1000
            )
        )

    assert hits == [base_hit]
    assert supports == {}
    assert id_map["k1001"]["notebook_id"] == "base"
    assert "base-source" in block


# --- ReasoningRetriever behaviour under a checkbox-only source ceiling ------
#
# Migrated from the now-deleted test_reasoning_source_scope.py (that file was
# dedicated to the now-removed model-inferred source restriction feature from
# PR #422; this one test function exercised the still-live user checkbox
# scope instead and is kept here).


class _ScopedRunSettings:
    retrieval_top_n = 12
    reasoning_top_n_per_query = 3
    reasoning_top_n_cap = 36
    reasoning_max_steps = 4
    reasoning_max_subqueries = 3
    reasoning_stale_limit = 10
    reasoning_max_element_searches = 2
    reasoning_quota_enabled = False
    graph_ppr_enabled = False
    reasoning_ppr_prefetch = False
    exact_lookup_enabled = True
    exact_lookup_max_identifiers = 3
    reasoning_timeout_seconds = 5
    reasoning_max_retries = 0
    community_peers_topk = 4
    community_rerank_candidates = 20


def _scoped_run_hit(source_id: str) -> RetrievedKnowledge:
    return RetrievedKnowledge(
        object_id=f"ko-{source_id}",
        object_type="claim",
        payload={"name": f"command-{source_id}"},
        evidence=[Evidence(
            source_id=source_id,
            source_title=f"Manual {source_id}",
            element_id=f"el-{source_id}",
            element_type="paragraph",
            location_label="Commands",
            quoted_span=f"command from {source_id}",
            confidence=1.0,
        )],
        relevance=0.9,
        score=0.9,
        notebook_id="nb",
    )


class _ScopedRunRetrieval:
    def __init__(self):
        self.ppr_calls = 0

    def federated_retrieve(self, *args, **kwargs):
        return [
            _scoped_run_hit("A"), _scoped_run_hit("B"), _scoped_run_hit("C"),
        ]

    def retrieve_elements(self, *args, **kwargs):
        return []

    def retrieve_scored(self, *args, **kwargs):
        return [
            _scoped_run_hit("C"), _scoped_run_hit("A"), _scoped_run_hit("B"),
        ]

    def ppr_retrieve(self, *args, **kwargs):
        self.ppr_calls += 1
        raise AssertionError("restricted PPR must have zero I/O")

    def exact_lookup_chunks(self, *args, **kwargs):
        raise AssertionError("restricted exact lookup must have zero I/O")


class _ScopedRunModels:
    def chat(self, workload_id):
        return type("Client", (), {"configured": False})()


class _ScopedRunCommunities:
    pass


def test_checkbox_only_scope_disables_enumeration_and_ppr_before_io():
    retrieval = _ScopedRunRetrieval()
    settings = _ScopedRunSettings()
    settings.graph_ppr_enabled = True
    settings.reasoning_ppr_prefetch = False
    retriever = ReasoningRetriever(
        retrieval=retrieval,
        model_clients=_ScopedRunModels(),
        communities=_ScopedRunCommunities(),
        settings=settings,
    )
    retriever.plan = lambda *args, **kwargs: [SubQuery(query="plain question")]
    retriever.reflect = lambda *args, **kwargs: ReflectDecision(
        next_action="answer", sufficient=True
    )

    with source_scope_context(
        "nb", SourceScope(mode="include", source_ids=["A"])
    ):
        assert retriever.enumeration_active() is False
        result = retriever.run("nb", "plain question")

    assert retrieval.ppr_calls == 0
    assert result.baseline_manifest is not None
    assert result.baseline_manifest.mode == "reasoning"
    unsafe = [
        step for step in result.trace
        if step.detail.get("reason") == "source_scope_unsafe_channels"
    ]
    assert unsafe


def test_all_selected_single_source_recovers_raw_elements_before_reflect():
    class _RawFallback(_ScopedRunRetrieval):
        def federated_retrieve(self, *_args, **_kwargs):
            return []

        def retrieve_elements(self, *_args, **_kwargs):
            return [RetrievedElement(
                element_id="el-A",
                source_id="A",
                source_title="Manual A",
                location_label="Commands",
                element_type="paragraph",
                text="command from A",
                score=0.8,
            )]

        def retrieve_scored(self, *_args, **_kwargs):
            return []

    retrieval = _RawFallback()
    retriever = ReasoningRetriever(
        retrieval=retrieval,
        model_clients=_ScopedRunModels(),
        communities=_ScopedRunCommunities(),
        settings=_ScopedRunSettings(),
    )
    retriever.plan = lambda *args, **kwargs: [SubQuery(query="plain question")]
    retriever.reflect = lambda *args, **kwargs: ReflectDecision(
        next_action="answer", sufficient=True
    )

    with source_scope_context(
        "nb", SourceScope(mode="include", source_ids=["A"], narrowed=False)
    ):
        result = retriever.run("nb", "plain question")

    assert [element.source_id for element in result.elements] == ["A"]
    assert result.baseline_manifest is None
    assert any(
        step.detail.get("reason") == "initial_evidence_empty"
        and step.detail.get("found") == 1
        for step in result.trace
    )


def test_all_selected_universe_drift_disables_ppr_before_io():
    class _Drifted(_ScopedRunRetrieval):
        def unsafe_source_scope_restricted(self, _notebook_id):
            return True

    retrieval = _Drifted()
    settings = _ScopedRunSettings()
    settings.graph_ppr_enabled = True
    retriever = ReasoningRetriever(
        retrieval=retrieval,
        model_clients=_ScopedRunModels(),
        communities=_ScopedRunCommunities(),
        settings=settings,
    )
    retriever.plan = lambda *args, **kwargs: [SubQuery(query="plain question")]
    retriever.reflect = lambda *args, **kwargs: ReflectDecision(
        next_action="answer", sufficient=True
    )

    with source_scope_context(
        "nb", SourceScope(mode="include", source_ids=["A"], narrowed=False)
    ):
        result = retriever.run("nb", "plain question")

    assert retrieval.ppr_calls == 0
    assert any(
        step.detail.get("reason") == "source_scope_unsafe_channels"
        for step in result.trace
    )

def test_checkbox_scope_skips_per_action_unsafe_channels_with_zero_io():
    """逐动作的纵深防御:模型即使提交了受限 run 下不该出现的动作,也不能落成 I/O。

    受限 run 的 reflect schema 根本不提供枚举/扩展分支,所以正常情况下走不到
    这里。但畸形模型响应、或将来有人把 allowed_actions 的构造改坏,都会让
    decision 落到 run() 循环里那几道逐动作闸上——它们一旦失效,执行器就会对
    **全库**跑枚举/扩展,把未勾选来源的证据连同 [k] 锚点送进答案。

    此前唯一覆盖这几道闸的是已删除的 test_reasoning_source_scope.py(那份文件
    专测已移除的模型判断来源特性),整支审查用变异验证确认了覆盖真空:把
    2716 行的 self._unsafe_scope_restricted() 改成 False,全套测试仍然全绿。
    这条测试按用户勾选范围补回该覆盖。
    """
    from app.services.reasoning_retrieval import (
        ENUMERATE_ELEMENTS_ACTION,
    )

    class _NoEnumeration:
        """任何真实枚举调用都是失败——闸必须在 I/O 之前拦住。"""

        def __getattr__(self, name):
            def _boom(*args, **kwargs):
                raise AssertionError(
                    f"受限 run 不得触发集合枚举 I/O: {name}"
                )
            return _boom

    for action, expected_summary_fragment in (
        (ENUMERATE_ELEMENTS_ACTION, "枚举"),
        ("expand_graph", "关系扩展"),
    ):
        retrieval = _ScopedRunRetrieval()
        retriever = ReasoningRetriever(
            retrieval=retrieval,
            model_clients=_ScopedRunModels(),
            communities=_ScopedRunCommunities(),
            settings=_ScopedRunSettings(),
        )
        retriever.collection_enumeration = _NoEnumeration()
        retriever.plan = lambda *a, **k: [SubQuery(query="plain question")]
        # 第一轮提交那个受限 run 下不该出现的动作,第二轮才收工。
        decisions = iter([
            ReflectDecision(
                next_action=action,
                expand_object_id="ko-A",
                enumerate_kind="formula",
            ),
            ReflectDecision(next_action="answer", sufficient=True),
        ])
        retriever.reflect = lambda *a, **k: next(
            decisions, ReflectDecision(next_action="answer", sufficient=True)
        )

        with source_scope_context(
            "nb", SourceScope(mode="include", source_ids=["A"])
        ):
            result = retriever.run("nb", "plain question")

        skipped = [
            step for step in result.trace
            if step.detail.get("reason") == "source_scope_unsafe_channel"
        ]
        assert skipped, f"{action} 未在受限 run 下留下逐动作跳过记录"
        assert any(
            expected_summary_fragment in step.summary for step in skipped
        ), f"{action} 的跳过文案未说明跳过的是什么"
        assert result.enumerations == []

from types import SimpleNamespace

from pydantic import ValidationError

from app.models.common import Evidence
from app.models.source_scope import (
    BaseNotebookScope,
    ResolvedSourceScope,
    SourceScope,
)
from app.models.notebooks import NotebookSummary
from app.api.ask_routes import _require_ask_available, _validate_source_scope
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
    def __init__(self, visible: list[str], count: int, hidden=None,
                 owner_hidden: "dict[str, list[str]] | None" = None,
                 user_id: str = "user-a"):
        self.visible = visible
        self.count = count
        # Hidden Memory/Knowhow projection sources — no checkbox, never in a
        # submitted selection, and (on an un-narrowed run) inside the ceiling.
        self.hidden = list(hidden or [])
        # Per-user hidden half, when a test needs to prove the boundary passes
        # the REQUESTING user's identity down. The real adapter answers this in
        # SQL; here it stands in as a lookup so a boundary that hard-coded the
        # wrong identity would fail visibly.
        self.owner_hidden = dict(owner_hidden or {})
        self.user_id = user_id
        self.hidden_calls: list[tuple[str, str]] = []

    def current_user(self):
        return SimpleNamespace(id=self.user_id)

    def visible_source_ids(self, _notebook_id, source_ids):
        return [source_id for source_id in source_ids if source_id in self.visible]

    def visible_source_count(self, _notebook_id):
        return self.count

    def visible_source_scope_snapshot(self, _notebook_id, source_ids):
        return self.visible_source_ids(_notebook_id, source_ids), self.count

    def all_visible_source_ids(self, _notebook_id):
        return list(self.visible)

    def hidden_source_ids(self, notebook_id, owner_id):
        self.hidden_calls.append((notebook_id, owner_id))
        if self.owner_hidden:
            return list(self.owner_hidden.get(owner_id, []))
        return list(self.hidden)


def _notebook(*, bases=None):
    return NotebookSummary(
        id="nb", name="n", purpose="", primary_domain="", status="ready",
        counts={}, created_label="", base_notebooks=bases or [],
    )


def test_validate_source_scope_no_longer_raises_409_on_its_own():
    """Superseded invariant: the "any evidence universe left?" check used to
    live inside _validate_source_scope, but it must also consider the frozen
    base-library scope (which this function cannot see), so it moved to
    ``_require_non_empty_scope``. This is now a pure freeze/422 helper."""
    resolved = _validate_source_scope(
        _ScopeRepo([], 0), _notebook(),
        SourceScope(mode="include", source_ids=[]),
    )
    # PrivateAttr `_hidden_source_ids` 参与 == 比较,期望值无从构造它;比公开契约面
    assert resolved.model_dump() == SourceScope(
        mode="include", source_ids=[], narrowed=False
    ).model_dump()


def test_empty_local_scope_requires_a_mounted_base():
    # Same scenario as before (empty local scope, zero mounted bases at all)
    # but now exercised through the combined authority, _require_ask_available,
    # since that is where the 409 decision now lives.
    with pytest.raises(HTTPException) as exc:
        _require_ask_available(
            _notebook(), _ScopeRepo([], 0),
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

    # Compare the wire shape: the server-only owner id is a private attribute
    # and pydantic equality would otherwise compare it too.
    assert resolved.model_dump() == SourceScope(
        mode="include", source_ids=["selected"], narrowed=True
    ).model_dump()


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
    # The wire shape is what travels; the owner id is a server-only private
    # attribute (pydantic equality would otherwise compare it too).
    assert resolved.model_dump() == SourceScope(
        mode="include", source_ids=["s1", "s3"], narrowed=True
    ).model_dump()
    assert resolved.scope_owner_id == "user-a"


def test_all_selected_is_frozen_but_not_misclassified_as_narrowed():
    """全选冻结出的 ``narrowed is False`` 既不影响 restricted 判断,也(R1 行为
    恢复,审计 ASK-1)不再让 ``scoped_allowed_source_ids`` 把全量可见+隐藏源
    物化成显式 tuple——它必须原样透传成 ``None``,同 unscoped 一样。"""
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
        # R1 行为恢复(审计 ASK-1,P0):全选冻结(narrowed=False)不再把全量
        # 可见+隐藏源物化成显式 tuple——下游一切按
        # `allowed_source_ids is not None` 驱动的快路径(语料语言闸/GiST KNN/
        # 未降级 FTS)必须看到与「无 scope」相同的 None,候选宇宙才能恢复到
        # 全选本该有的样子。最终证据仍由 source_allowed 等结果侧防线裁剪,
        # 见本文件下方 source_allowed 断言。
        assert scoped_allowed_source_ids("nb") is None
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


def test_large_all_selected_snapshot_is_not_limited_by_client_request_cap():
    source_ids = [f"s{index}" for index in range(10_001)]

    # The compact browser submission remains safely below the public request
    # cap, while the server-owned complement is allowed to reflect the real
    # notebook size.
    resolved = _validate_source_scope(
        _ScopeRepo(source_ids, len(source_ids)),
        _notebook(),
        SourceScope(mode="exclude", source_ids=[]),
    )

    assert isinstance(resolved, ResolvedSourceScope)
    assert resolved.mode == "include"
    assert resolved.source_ids == source_ids
    assert resolved.narrowed is False

    # Deep reports persist this wire payload and parse it again at intent and
    # generation gates; that trusted round-trip must not reapply the cap.
    restored = ResolvedSourceScope.model_validate(resolved.model_dump())
    assert restored.source_ids == source_ids

    # The separate public request model still rejects a client that submits
    # the same oversized list directly.
    with pytest.raises(ValidationError):
        SourceScope(mode="include", source_ids=source_ids)


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


def test_scoped_allowed_source_ids_narrowed_tri_state():
    """R1 行为恢复(审计 ASK-1,P0)的显式三态钉,外加 ``explicit`` 这一维:
    同一份冻结的 include 清单(可见 ``s1`` + 隐藏 ``hidden-memory``),
    ``narrowed`` 三个取值下 ``scoped_allowed_source_ids`` 必须给出三种不同的
    返回形状——

    * ``True``  真收窄:原样物化冻结清单(交集/ceiling),行为不变。
    * ``False`` 浏览器默认全选冻结:**不带显式清单**时退化成「无 scope」的
      返回形状 ``None``。下游一切按 ``allowed_source_ids is not None`` 驱动的
      分支(语料语言闸、GiST KNN 快路径、未降级 FTS)才能因此恢复到无 scope
      时的快路径,这正是 R1 要恢复的行为。
    * ``None``  历史直接构造 / 早于该字段的遗留 scope:必须保持改动前的
      字节兼容物化行为不变,``is False`` 判断刻意不匹配 ``None``。

    快路径**只**属于 ``explicit is None``。带显式清单时(narrowed 取值无关)
    必须与冻结清单取交集:调用方那份清单是**实时**枚举出来的(插件端口就在
    构造时枚举 ``all_visible_source_ids``),原样透传等于把冻结之后新增的来源
    放进 run。这一条是 R1 首版的语义洞:元素检索那条路没有结果侧防线
    (``RetrievedElement`` 无 notebook_id、``filter_retrieval_items`` 不套它),
    这里的交集就是它唯一的执法点。

    **变异锚点**:把 ``narrowed is False`` 的早退恢复成不看 ``explicit``
    (即显式清单也直通)→ 下面 ``("s1",)`` 那条报红。
    """
    def _scope(narrowed):
        scope = ResolvedSourceScope(
            mode="include", source_ids=["s1"], narrowed=narrowed
        )
        scope._hidden_source_ids = ["hidden-memory"]
        return scope

    with source_scope_context("nb", _scope(True)):
        assert set(scoped_allowed_source_ids("nb")) == {"s1", "hidden-memory"}, (
            "真收窄:仍然原样物化冻结清单"
        )
        assert scoped_allowed_source_ids("nb", ["s1", "drifted-in"]) == ("s1",)

    with source_scope_context("nb", _scope(False)):
        assert scoped_allowed_source_ids("nb") is None, (
            "R1:全选冻结不带显式清单时必须退化成 None,同无 scope 一样"
        )
        assert scoped_allowed_source_ids("nb", ["s1", "drifted-in"]) == ("s1",), (
            "narrowed=False 带显式清单时必须与冻结清单取交集——原样透传就是"
            "把冻结后新增的来源放进 run(实时枚举的清单不是天花板)"
        )
        assert scoped_allowed_source_ids(
            "nb", ["s1", "hidden-memory"]
        ) == ("s1", "hidden-memory"), (
            "交集只丢冻结清单之外的 id:隐藏半在天花板内,不得被顺手裁掉"
        )

    with source_scope_context("nb", _scope(None)):
        assert set(scoped_allowed_source_ids("nb")) == {"s1", "hidden-memory"}, (
            "narrowed=None 是遗留直接构造路径,字节兼容不许破——原样物化"
        )
        assert scoped_allowed_source_ids("nb", ["s1", "drifted-in"]) == ("s1",)


def test_candidate_detects_hidden_participant_drift_through_source_store():
    from app.services.retrieval_candidates import CandidateRetrievalService

    class _Sources:
        visible = ["s1"]
        hidden = ["hidden-memory"]
        owner_calls: list[str] = []

        def all_visible_source_ids(self, _notebook_id):
            return list(self.visible)

        def hidden_source_ids(self, _notebook_id, owner_id):
            self.owner_calls.append(owner_id)
            return list(self.hidden)

    class _Candidates:
        sources = _Sources()

    scope = SourceScope(mode="include", source_ids=["s1"], narrowed=False)
    scope._hidden_source_ids = ["hidden-memory"]
    scope._scope_owner_id = "user-a"
    with source_scope_context("nb", scope):
        assert CandidateRetrievalService._unsafe_source_scope_restricted(
            _Candidates(), "nb"
        ) is False
        _Candidates.sources.hidden.append("new-hidden")
        assert CandidateRetrievalService._unsafe_source_scope_restricted(
            _Candidates(), "nb"
        ) is True
    # The live re-read must use the identity the freeze was taken with: the
    # hidden half is owner-scoped, so probing as anyone else would compare two
    # different partitions and report drift on every request forever.
    assert _Candidates.sources.owner_calls == ["user-a", "user-a"]


# ───── 漂移探针**必须逐调用现探**:权威契约钉(docs/product-and-api.md) ──
def _drift_probe_double():
    """记账版的 source store double —— 数两次可见/隐藏来源读各自被打了几轮。"""
    from app.services.retrieval_candidates import CandidateRetrievalService

    class _Sources:
        def __init__(self):
            self.visible = ["s1"]
            self.hidden = ["hidden-memory"]
            self.visible_calls = 0
            self.hidden_calls = 0

        def all_visible_source_ids(self, _notebook_id):
            self.visible_calls += 1
            return list(self.visible)

        def hidden_source_ids(self, _notebook_id, _owner_id):
            self.hidden_calls += 1
            return list(self.hidden)

    class _Candidates:
        def __init__(self):
            self.sources = _Sources()

        def probe(self, notebook_id="nb"):
            return CandidateRetrievalService._unsafe_source_scope_restricted(
                self, notebook_id
            )

    return _Candidates()


def _frozen_all_selected_scope():
    scope = SourceScope(mode="include", source_ids=["s1"], narrowed=False)
    scope._hidden_source_ids = ["hidden-memory"]
    scope._scope_owner_id = "user-a"
    return scope


def test_scope_drift_probe_runs_on_every_call_inside_one_retrieval_run():
    """⛔ 契约钉:漂移探针不许被 memo 到 run / 请求 / 进程上。

    判据来源是权威文档 ``docs/product-and-api.md`` 的检索范围一节:
    「A visible-source or hidden-participant addition/deletion after validation
    disables unsafe graph channels **before I/O**」,以及同段说明的
    「post-filtering alone is not authority because excluded candidates can
    consume Top-K or supply hidden graph premises」。

    热路径修复批 2 的 R2-3 曾把它 memo 到 ``current_retrieval_run()``,codex 对
    PR #634 第 1 轮判 P1 并整体回退。这条测试就是那次回退留下的守卫:一次 run
    里,首探之后新增的来源必须立刻在**后续每一个**调用点上被看见,否则
    whole-graph / PPR / relation expansion / exact-lookup 会带着陈旧的 False
    放行越界候选。

    **变异锚点**:把探针重新 memo 化(run 级、请求级或进程级都算)→ 第二次调用
    返回陈旧的 False、读计数停在 1,这条报红。
    """
    from app.services.retrieval_run import retrieval_run

    candidates = _drift_probe_double()
    with source_scope_context("nb", _frozen_all_selected_scope()):
        with retrieval_run(run_kind="ask_graph"):
            assert candidates.probe() is False
            # run 执行到一半有人上传了新来源 —— 后续调用点必须立刻改判。
            candidates.sources.visible.append("s2-uploaded-mid-run")
            assert candidates.probe() is True, (
                "run 内首探之后的来源新增必须在下一个调用点就被看见"
                "(docs/product-and-api.md:检索范围——unsafe I/O 之前关闭通道)")
            assert candidates.probe() is True

    assert candidates.sources.visible_calls == 3, (
        f"每次调用都必须现探,实际只探了 {candidates.sources.visible_calls} 轮")


def test_scope_drift_probe_runs_on_every_call_outside_a_run():
    """同一条契约在 run 之外(直接服务调用、后台作业)一样成立。"""
    candidates = _drift_probe_double()
    with source_scope_context("nb", _frozen_all_selected_scope()):
        assert candidates.probe() is False
        candidates.sources.visible.append("s2-uploaded-mid-flight")
        assert candidates.probe() is True
    assert candidates.sources.visible_calls == 2


def test_scope_drift_probe_reprobes_across_scope_reinstalls():
    """一次 run 里 scope 可以被重新安装(插件引擎就这么做,见 ask_service 的
    ``scope_stack.enter_context(source_scope_context(...))``)。新 scope 必须按
    它自己的冻结快照重新判定,绝不能沿用上一份 scope 的结论。"""
    from app.services.retrieval_run import retrieval_run

    candidates = _drift_probe_double()
    with retrieval_run(run_kind="ask_graph"):
        with source_scope_context("nb", _frozen_all_selected_scope()):
            assert candidates.probe() is False
        drifted = SourceScope(
            mode="include", source_ids=["s1", "s-that-is-gone"], narrowed=False)
        drifted._hidden_source_ids = ["hidden-memory"]
        drifted._scope_owner_id = "user-a"
        with source_scope_context("nb", drifted):
            assert candidates.probe() is True

    assert candidates.sources.visible_calls == 2


def _drift_lane_notebook(tmp_path, monkeypatch):
    """真库最小场:一个 notebook,按需插入可见来源与带 evidence 的 KG 对象。

    刻意用真 ``SQLiteRepository`` 而不是 double:这条钉子要走的是
    ``_retrieve_scored`` 的**真实**候选路由(受限词法 lane 的 site 与它收到的
    allowed_source_ids),而 double 只能证明我自己写的 if 分支自洽。
    """
    from app.core.config import Settings
    from app.models.schemas import NotebookCreate
    from app.services.sqlite_repository import SQLiteRepository

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'drift-lane.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings(_env_file=None))
    notebook_id = repo.create_notebook(NotebookCreate(name="漂移")).id

    def add_source(source_id: str) -> None:
        repo._runtime.source_store.insert_source(
            source_id=source_id, notebook_id=notebook_id, title=source_id,
            source_type="pdf", status="active", parse_status="parsed",
            file_name="", file_path="", file_size=0, file_hash="",
            summary="", doc_type="",
        )

    def add_object(local_id: str, source_id: str, name: str) -> None:
        repo.store_kg(notebook_id, None, [{
            "local_id": local_id,
            "object_type": "concept",
            "payload": {"name": name},
            "evidence": [{
                "source_id": source_id, "source_title": source_id,
                "element_id": f"el-{local_id}", "element_type": "paragraph",
                "location_label": "1", "quoted_span": name, "confidence": 1.0,
            }],
        }], [])

    return repo, notebook_id, add_source, add_object


def _lexical_lane_spy(candidates, monkeypatch):
    """记下 ``_lexical_object_hits`` 每次调用的 (site, allowed_source_ids)。"""
    seen: list[tuple[str, object]] = []
    original = candidates._lexical_object_hits

    def _spy(db, notebook_id, query, recall, **kwargs):
        seen.append((kwargs.get("site"), kwargs.get("allowed_source_ids")))
        return original(db, notebook_id, query, recall, **kwargs)

    monkeypatch.setattr(candidates, "_lexical_object_hits", _spy)
    return seen


def _drift_probe_spy(candidates, monkeypatch):
    """记下漂移探针每次调用的判定结果(仍然真调,不改判)。"""
    verdicts: list[bool] = []
    original = candidates._unsafe_source_scope_restricted

    def _spy(notebook_id):
        verdict = original(notebook_id)
        verdicts.append(verdict)
        return verdict

    monkeypatch.setattr(candidates, "_unsafe_source_scope_restricted", _spy)
    return verdicts


def test_all_selected_freeze_reopens_the_restricted_lane_once_sources_drift(
    tmp_path, monkeypatch
):
    """⛔ 契约钉(docs/product-and-api.md 检索范围一节):全选冻结之后有来源完成
    抽取时,来源可分区检索必须**在 I/O 前**按冻结清单继续,而不是让漂移来源先
    去争 Top-K 再在结果侧丢掉——「post-filtering alone is not authority because
    excluded candidates can consume Top-K or supply hidden graph premises」。

    R1 首版把 ``narrowed is False`` 一律早退成 None,于是 ``_retrieve_scored``
    的 ``source_filter is not None and (...)`` 直接短路,漂移探针一次都不被调,
    受限词法 lane 死掉——正是 #634 R2-3 留档明令禁止的形态。

    **变异锚点**:去掉 ``_scoped_allowed_with_drift_guard`` 的漂移回落(直接调
    ``scoped_allowed_source_ids``)→ 探针不再被调、``kg_source_scoped_fts`` 不再
    出现,本条报红。
    """
    from app.models.source_scope import ResolvedSourceScope

    repo, notebook_id, add_source, add_object = _drift_lane_notebook(
        tmp_path, monkeypatch
    )
    add_source("src-frozen")
    add_object("frozen", "src-frozen", "bandgap reference")
    candidates = repo.retrieval.candidates

    frozen = ResolvedSourceScope(
        mode="include", source_ids=["src-frozen"], narrowed=False
    )
    # 冻结之后才完成抽取的来源(并发上传):它不在冻结快照里。
    add_source("src-drifted")
    add_object("drifted", "src-drifted", "bandgap reference")

    lane = _lexical_lane_spy(candidates, monkeypatch)
    probe = _drift_probe_spy(candidates, monkeypatch)
    with source_scope_context(notebook_id, frozen):
        hits = candidates._retrieve_scored(notebook_id, "bandgap")

    assert probe, "漂移探针必须被调用——短路掉它就是 #634 R2-3 的违规形态"
    assert probe[-1] is True, "冻结后新增可见来源必须被判为漂移"
    assert ("kg_source_scoped_fts", ("src-frozen",)) in lane, (
        "漂移后受限词法 lane 必须收到物化的冻结清单(在 LIMIT 之前),"
        f"实际 lane 调用为 {lane}"
    )
    drifted_sources = {
        evidence.source_id
        for hit in hits for evidence in hit.evidence
    }
    assert "src-drifted" not in drifted_sources, (
        "漂移来源的对象不得进入候选结果"
    )


def test_all_selected_freeze_without_drift_keeps_the_unscoped_fast_path(
    tmp_path, monkeypatch
):
    """对照臂(R1 的等价面,不许因漂移回落而回归):宇宙没漂移时探针可以被调,
    但判定为 False,天花板保持 ``None``——受限词法 lane 一次都不能出现。"""
    from app.models.source_scope import ResolvedSourceScope

    repo, notebook_id, add_source, add_object = _drift_lane_notebook(
        tmp_path, monkeypatch
    )
    add_source("src-frozen")
    add_object("frozen", "src-frozen", "bandgap reference")
    candidates = repo.retrieval.candidates

    frozen = ResolvedSourceScope(
        mode="include", source_ids=["src-frozen"], narrowed=False
    )
    lane = _lexical_lane_spy(candidates, monkeypatch)
    probe = _drift_probe_spy(candidates, monkeypatch)
    with source_scope_context(notebook_id, frozen):
        candidates._retrieve_scored(notebook_id, "bandgap")

    assert all(verdict is False for verdict in probe), (
        f"未漂移的全选冻结不得被判为受限,实际 {probe}"
    )
    assert all(site != "kg_source_scoped_fts" for site, _allowed in lane), (
        f"未漂移时必须保持无 scope 的快路径候选路由,实际 lane 调用为 {lane}"
    )
    assert all(allowed is None for _site, allowed in lane), (
        f"未漂移时不得下推任何 allow-list,实际 lane 调用为 {lane}"
    )


def test_library_exclusion_still_denies_before_the_all_selected_fast_path():
    """顺序不变量(P2-2):库维度排除必须在本地全选快路径**之前**判定。

    ``covers_notebook`` 的拒绝分支返回 ``()``(显式拒绝,SQL 侧在 LIMIT 前就空),
    而全选快路径返回 ``None``(无天花板)。两者的返回形状语义相反,所以把快路径
    分支上移到拒绝分支之前,会把「这个库整体不参与」变成「这个库没有限制」。

    **变异锚点**:把 ``narrowed is False`` 的早退挪到 ``covers_notebook`` 判定
    之前 → 下面第一条断言从 ``()`` 变成 ``None``,本条报红。
    """
    local = SourceScope(mode="include", source_ids=["s1"], narrowed=False)
    base = BaseNotebookScope(
        mode="include", notebook_ids=["kept-base"], narrowed=True
    )
    with source_scope_context("nb", local, base):
        assert scoped_allowed_source_ids("excluded-base") == (), (
            "被排除的参考库是显式拒绝,绝不能因为本地维度全选而变成「无限制」"
        )
        assert scoped_allowed_source_ids("excluded-base", ["b1"]) == ()
        # 对照:参与的库不受本地复选框影响,本地全选也仍然是 None。
        assert scoped_allowed_source_ids("kept-base", ["b1"]) == ("b1",)
        assert scoped_allowed_source_ids("nb") is None


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
    """按 AST 源序遍历定位一次真实调用的先后位置（返回遍历下标）。

    刻意不做 `source.index(name)` 那种文本查找:那样连 docstring 和注释里的
    同名字样都算数,于是「把真实调用挪到后面、同时在函数开头的注释里提到它」
    就能骗过顺序断言。这两个函数的 docstring 正好就在上方,不是臆想的场景。
    位置用 NodeVisitor 的源序遍历下标而非行号表达——测试架构策略禁止把行号
    当作身份/顺序断言的载体（line-number-identity），遍历序对语句体同样保序。
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    matches: list[int] = []
    counter = [0]

    class _Calls(ast.NodeVisitor):
        def visit_Call(self, node: "ast.Call") -> None:
            index = counter[0]
            counter[0] += 1
            if (
                getattr(node.func, "attr", None) == callee
                or getattr(node.func, "id", None) == callee
            ):
                matches.append(index)
            self.generic_visit(node)

    _Calls().visit(tree)
    assert matches, f"未找到对 {callee} 的调用"
    return min(matches)


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
    reasoning_neighbor_expand_limit = 1000
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
    assert not any(
        step.detail.get("reason") == "source_scope_unsafe_channels"
        for step in result.trace
    )


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
    assert not any(
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


# ---------------------------------------------------------------------------
# 冻结上限里的隐藏证据**按种类分别定范围**。
#
# 把隐藏投影源整批冻进未收窄请求的上限,而取数那一步没有 owner 过滤 —— 共享笔记本里,
# 任何一位成员发出的默认全选请求都会把**其他成员**的私有 Memory 投影源冻进自己的上限。
# 那些源持有 Memory 派生的元素与知识对象,于是普通候选生成(以及未收窄时重新打开的
# 全图/PPR 通道)可以把别人的私有记忆检索出来。
#
# 正确的口径两半不同,且都有既有代码作准:
#   * Knowhow 投影源是 **notebook 级共享**内容(表格本身对每位成员可见),每位成员的
#     上限都该收下它;
#   * Memory 是**按创建者私有**的 —— `MemoryStore.memory_retrieval_rows` 的
#     `m.created_by=?`、`memory_for_user`,以及 `SourceStore.source_change_signal_rows`
#     把 Memory 整类排除出清单,用的都是同一条归属判据。
#
# 过滤发生在**取数处的 SQL**里:别人的 Memory 源 id 根本不进本进程,后来的改动也无法
# 靠删掉一个结果侧的 if 悄悄把洞重新打开。
# ---------------------------------------------------------------------------

_SCOPE_OWNER_NOW = "2026-08-04T00:00:00+00:00"


def _shared_notebook_with_two_members(tmp_path, monkeypatch):
    """一个共享笔记本:1 份可见来源、1 份 Knowhow 投影源,两位成员各 1 条已确认
    Memory 及其投影源。直接落库(不走确认端点):本条断言的是取数谓词,而真实确认
    路径会拉起解析/向量后台 job,与被测的那一条 SQL 无关。"""
    from app.core.config import Settings
    from app.models.schemas import NotebookCreate
    from app.services.sqlite_repository import (
        SQLiteRepository,
        reset_request_user,
        set_request_user,
    )

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'scope-owner.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings())
    alice = repo.create_user("a00123456", "password-12")
    bob = repo.create_user("b00123456", "password-12")
    token = set_request_user(alice)
    try:
        notebook_id = repo.create_notebook(NotebookCreate(name="共享库")).id
    finally:
        reset_request_user(token)
    repo._runtime.sharing.add_member(notebook_id, bob.id)

    def _insert(source_id: str, source_type: str, memory_id: str = "") -> None:
        repo._runtime.source_store.insert_source(
            source_id=source_id, notebook_id=notebook_id, title=source_id,
            source_type=source_type, status="active", parse_status="parsed",
            file_name="", file_path="", file_size=0, file_hash="",
            summary="", doc_type="", memory_id=memory_id,
        )

    _insert("src-visible", "pdf")
    _insert("src-knowhow", "knowhow")
    for user, memory_id, source_id in (
        (alice, "mem-alice", "src-memory-alice"),
        (bob, "mem-bob", "src-memory-bob"),
    ):
        with repo._write() as db:
            db.execute(
                "INSERT INTO memory_items"
                "(id,notebook_id,created_by,agent_profile_id,source_answer_id,"
                "origin,status,title,content_md,created_at,updated_at) "
                "VALUES (?,?,?,NULL,NULL,'ask_answer','confirmed',?,?,?,?)",
                (memory_id, notebook_id, user.id, "私有记忆", "私有内容",
                 _SCOPE_OWNER_NOW, _SCOPE_OWNER_NOW),
            )
        _insert(source_id, "memory", memory_id=memory_id)
    return repo, notebook_id, alice, bob


def test_hidden_half_never_carries_another_members_private_memory(
    tmp_path, monkeypatch
):
    """取数谓词本身:隐藏那一半按请求用户定范围,且两位成员都拿得到 Knowhow。

    对称地各查一次 —— 只查一位,一个「恒返回自己那条」的错误实现也能通过。
    """
    repo, notebook_id, alice, bob = _shared_notebook_with_two_members(
        tmp_path, monkeypatch
    )

    assert repo.all_visible_source_ids(notebook_id) == ["src-visible"], (
        "可见那一半不受隐藏源归属影响"
    )
    hidden = repo.hidden_source_ids(notebook_id, bob.id)
    assert "src-memory-alice" not in hidden, (
        "共享笔记本里,别人的私有 Memory 投影源绝不能进入本次冻结上限"
    )
    assert set(hidden) == {"src-knowhow", "src-memory-bob"}

    hidden_alice = repo.hidden_source_ids(notebook_id, alice.id)
    assert "src-memory-bob" not in hidden_alice
    assert set(hidden_alice) == {"src-knowhow", "src-memory-alice"}, (
        "Knowhow 投影源是 notebook 级共享内容,对每位成员都在上限内"
    )

    # 陌生身份只拿到共享的 Knowhow,一条 Memory 都没有 —— 孤儿 Memory 源同理
    # (EXISTS 不成立即丢弃),fail closed。
    assert repo.hidden_source_ids(notebook_id, "user-nobody") == ["src-knowhow"]


def test_boundary_ceiling_is_owner_scoped_end_to_end(tmp_path, monkeypatch):
    """入口层到消费侧的整条链:B 的默认全选请求冻出的上限里没有 A 的私有 Memory,
    而 B 自己的 Memory 与共享的 Knowhow 都在。R1 行为恢复(审计 ASK-1):这份
    上限仍然是结果侧 source_allowed 的裁剪依据,但 scoped_allowed_source_ids
    这个 producer 入口在 narrowed=False 时不再把它物化成显式 tuple——必须
    原样退化成 None,让候选生成侧看到与「无 scope」相同的形状。"""
    from app.services.sqlite_repository import reset_request_user, set_request_user

    repo, notebook_id, _alice, bob = _shared_notebook_with_two_members(
        tmp_path, monkeypatch
    )
    token = set_request_user(bob)
    try:
        resolved = _validate_source_scope(
            repo, repo.get_notebook(notebook_id),
            SourceScope(mode="exclude", source_ids=[]),
        )
    finally:
        reset_request_user(token)

    assert resolved is not None
    assert resolved.narrowed is False, "默认全选不是收窄"
    assert resolved.source_ids == ["src-visible"]
    assert set(resolved.hidden_source_ids) == {"src-knowhow", "src-memory-bob"}

    # 上限就是这份清单 —— 下推给候选生成的那份也一样。
    with source_scope_context(notebook_id, resolved):
        assert source_allowed(notebook_id, "src-memory-bob") is True
        assert source_allowed(notebook_id, "src-knowhow") is True
        assert source_allowed(notebook_id, "src-memory-alice") is False, (
            "别人的私有 Memory 投影证据不得参与"
        )
        # R1 行为恢复:narrowed=False(默认全选)不再把这份上限物化给
        # producer——None 才是「恢复到无 scope 时的候选宇宙」该有的返回值,
        # 结果侧的 source_allowed(上面已断言)才是真正裁掉别人私有 Memory 的
        # 防线,不靠这里的物化清单。
        assert scoped_allowed_source_ids(notebook_id) is None


def test_shared_notebook_is_not_permanently_judged_drifted(tmp_path, monkeypatch):
    """另一位成员的私有 Memory 不得把共享笔记本判成「已漂移」。

    冻结按 owner 过滤而实时探测不过滤(或按别人过滤),两边永远不等 —— 于是每一次
    默认全选请求都被判成漂移,全图/PPR/关系/精确通道被永久关掉。这是把安全修复做成
    功能退化的那条路;它不会让任何行为断言变红,只会让检索悄悄变差。
    """
    from app.services.retrieval_candidates import CandidateRetrievalService
    from app.services.sqlite_repository import reset_request_user, set_request_user

    repo, notebook_id, _alice, bob = _shared_notebook_with_two_members(
        tmp_path, monkeypatch
    )
    token = set_request_user(bob)
    try:
        resolved = _validate_source_scope(
            repo, repo.get_notebook(notebook_id),
            SourceScope(mode="exclude", source_ids=[]),
        )
    finally:
        reset_request_user(token)

    candidates = SimpleNamespace(sources=repo._runtime.source_store)
    with source_scope_context(notebook_id, resolved):
        assert CandidateRetrievalService._unsafe_source_scope_restricted(
            candidates, notebook_id
        ) is False, (
            "共享笔记本里别人的 Memory 不是漂移,通道不得因此被永久关闭"
        )
        # 真正的漂移仍然要抓到。
        repo._runtime.source_store.insert_source(
            source_id="src-added-mid-run", notebook_id=notebook_id,
            title="并发新增", source_type="pdf", status="active",
            parse_status="parsed", file_name="", file_path="", file_size=0,
            file_hash="", summary="", doc_type="",
        )
        assert CandidateRetrievalService._unsafe_source_scope_restricted(
            candidates, notebook_id
        ) is True, "冻结之后新增的可见来源仍必须判为漂移"


def test_boundary_reads_the_hidden_half_for_the_requesting_user():
    """入口层把**请求用户**的身份传下去 —— 不是笔记本 owner,也不是某个常量。

    真适配器在 SQL 里回答这件事;这里的 double 按 owner 分桶,于是一个把身份写死
    的入口层会直接取到另一位成员那一桶。
    """
    repo = _ScopeRepo(
        ["s1"], 1,
        owner_hidden={
            "user-a": ["src-memory-alice"],
            "user-b": ["src-memory-bob"],
        },
        user_id="user-b",
    )
    resolved = _validate_source_scope(
        repo, _notebook(), SourceScope(mode="exclude", source_ids=[]),
    )
    assert repo.hidden_calls == [("nb", "user-b")], (
        "取数必须带上请求用户的身份,过滤不能挪到结果侧;而且只发一次"
    )
    assert resolved.hidden_source_ids == ["src-memory-bob"]
    assert resolved.scope_owner_id == "user-b", (
        "身份必须随冻结一起带走,漂移探测要在同一个身份的取景里复读"
    )


def test_both_adapters_filter_memory_ownership_inside_the_single_query():
    """两个适配器都必须**在 SQL 里**按 owner 过滤,而且仍然只发一条查询。

    这条是结构守卫,不是行为断言 —— 因为「先把整本库的隐藏源取回来、再在 Python 里
    丢掉别人的」在行为上与正确实现无法区分,但它把另一位成员的私有 Memory 源 id
    读进了本进程,而且下一次改动删掉那个 `if` 就悄悄把洞重新打开。同一条断言顺带
    钉住「一次读取返回两个分区」的往返合同:多出第二条 execute 就报红。
    """
    import ast
    import inspect
    import textwrap

    from app.repositories.postgres.source_store import (
        SourceStore as PostgresSourceStore,
    )
    from app.repositories.sqlite.source_store import (
        SourceStore as SqliteSourceStore,
    )

    for store in (SqliteSourceStore, PostgresSourceStore):
        source = inspect.getsource(store.hidden_source_ids)
        # textwrap.dedent, not inspect.cleandoc: cleandoc lstrips the first
        # line and dedents the rest independently, which drops a single-line
        # `def` to column 0 while its body keeps an indent it no longer needs.
        tree = ast.parse(textwrap.dedent(source))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ]
        assert len(calls) == 1, (
            f"{store.__module__}: 隐藏那一半必须只发一条查询"
        )
        call = calls[0]
        sql = call.args[0].value
        assert "FROM sources" in sql
        assert "memory_items" in sql and "created_by" in sql, (
            f"{store.__module__}: Memory 归属过滤必须在这条 SQL 里,"
            "不能取回全部再在结果侧丢弃"
        )
        # 归属必须是**参数绑定的谓词**,不是取回来的一列:把 created_by 选进结果、
        # 再在 Python 里丢掉别人的行,行为上与正确实现无法区分,却把别人的私有
        # Memory 源 id 读进了本进程 —— 那正是这条守卫要拦的「挪到结果侧」。
        projection = sql.split(" FROM ", 1)[0]
        assert "created_by" not in projection, (
            f"{store.__module__}: 归属不能被选进结果集,必须在 WHERE 里判掉"
        )
        assert projection.replace("SELECT ", "").strip() == "s.id", (
            f"{store.__module__}: 这条查询只该取它要返回的那一列"
        )
        assert (
            isinstance(call.args[1], ast.Tuple) and len(call.args[1].elts) == 2
        ), f"{store.__module__}: notebook 与 owner 两个参数都必须绑进这条语句"

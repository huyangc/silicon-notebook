"""参考库按库勾选（检索范围的第二个维度）。

真机事故：挂着 84 篇论文参考库的 notebook 里，勾定**单篇**本地文章提问，16 条引用
全部来自参考库、目标文章零证据。根因是来源复选框只约束当前 notebook 自己的来源，
挂载的参考库**无条件全量参与**。

本文件按四层组织：
  T0 契约层（模型/冻结/409 判据/正交性）；
  T1 消费边界（四类候选、装配点、图漫游、社区枚举、KG 可用性闸）；
  T2 真机形状端到端（真 SQLiteRepository + 真挂载参考库）；
  T3 只读回执。

头号红线（R1）：库维度与 `source_scope_restricted()` **正交**。后者关的是**当前库
自己**的 PPR / 私有 Memory / 社区报告 / 弱支撑关系 / 精确章节 / 报告整库画像；用户
只是少借一个参考库，不该为此付出「当前库检索能力被砍」的代价。
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.ask_routes import (
    _require_ask_available,
    _scope_receipt,
    _validate_base_scope,
    _validate_source_scope,
)
from app.models.ask import AskRequest
from app.models.common import Evidence
from app.models.notebooks import NotebookRef, NotebookSummary
from app.models.source_scope import BaseNotebookScope, SourceScope
from app.services.retrieval import RetrievedChunk, RetrievedKnowledge
from app.services.kg.follow_chain import ChainHop, FollowChainResult, InferredChain
from app.services.retrieval_service import RetrievalService
from app.services.source_scope import (
    ActiveSourceScope,
    base_scope_ceiling_active,
    base_scope_restricted,
    current_base_scope_payload,
    current_source_scope,
    current_source_scope_payload,
    evidence_json_allowed,
    filter_retrieval_items,
    scoped_allowed_source_ids,
    scoped_conversation_history,
    source_allowed,
    source_scope_ceiling_active,
    source_scope_context,
    source_scope_restricted,
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


class _ScopeRepo:
    def __init__(self, visible: list[str], count: int, hidden=None):
        self.visible = visible
        self.count = count
        self.hidden = list(hidden or [])

    def visible_source_ids(self, _notebook_id, source_ids):
        return [source_id for source_id in source_ids if source_id in self.visible]

    def visible_source_count(self, _notebook_id):
        return self.count

    def all_visible_source_ids(self, _notebook_id):
        return list(self.visible)

    def all_hidden_source_ids(self, _notebook_id):
        return list(self.hidden)


def _notebook_ref(nb_id: str, name: str = "") -> NotebookRef:
    return NotebookRef(id=nb_id, name=name or nb_id)


def _notebook(*, bases=None, sources: int = 0,
              local_evidence: bool = False) -> NotebookSummary:
    return NotebookSummary(
        id="nb", name="n", purpose="", primary_domain="", status="ready",
        counts={"sources": sources}, created_label="",
        base_notebooks=bases or [],
        local_evidence_available=local_evidence,
    )


# ---------------------------------------------------------------------------
# T0 — contract layer: model shape, the exclude→include freeze, R1/R2.
# ---------------------------------------------------------------------------


def test_r2_omitted_base_scope_is_byte_identical_to_before_the_field_existed():
    """R2: a caller that never supplies base_scope must observe unchanged
    behavior — both the two-arg ``source_scope_context`` call shape AND an
    explicit ``base_scope=None`` must agree, and mounted base libraries stay
    fully open."""
    chunks = [
        RetrievedChunk("cb", "base-source", "base", "", "base", notebook_id="base"),
    ]
    with source_scope_context("nb", SourceScope(mode="include", source_ids=["s1"])):
        omitted = (
            source_allowed("base", "base-source"),
            scoped_allowed_source_ids("base", ["b1"]),
            [row.chunk_id for row in filter_retrieval_items("nb", "chunk", chunks)],
        )
    with source_scope_context(
        "nb", SourceScope(mode="include", source_ids=["s1"]), None
    ):
        explicit_none = (
            source_allowed("base", "base-source"),
            scoped_allowed_source_ids("base", ["b1"]),
            [row.chunk_id for row in filter_retrieval_items("nb", "chunk", chunks)],
        )
    assert omitted == explicit_none == (True, ("b1",), ["cb"])


def test_r1_deselecting_a_base_library_does_not_trip_local_restricted():
    """R1 (头号红线): unchecking a mounted base library while local sources stay
    fully selected must NOT flip ``source_scope_restricted()`` — that flag gates
    the ACTIVE notebook's own PPR/graph expansion and private Memory
    (``ask_service._memory_hits`` returns [] when restricted), which is
    orthogonal to which reference libraries participate.

    ``scoped_conversation_history`` is DELIBERATELY not pinned to the historical
    "unchanged" value here: it is a cross-library value (a prior answer can
    quote the library the user has just unchecked), so clearing it is the
    intended behavior for this same context. Conflating that with ``restricted``
    staying False would defeat the point of this test.
    """
    with source_scope_context(
        "nb", None, BaseNotebookScope(mode="include", notebook_ids=[]),
    ):
        assert source_scope_restricted() is False
        assert scoped_conversation_history("prior answer") == ""
    # Same when the local scope is explicitly the historical "all" shape.
    with source_scope_context(
        "nb", SourceScope(mode="exclude", source_ids=[]),
        BaseNotebookScope(mode="exclude", notebook_ids=["base"]),
    ):
        assert source_scope_restricted() is False


def test_restricted_never_folds_in_the_library_dimension():
    """M20 变异守卫:`restricted` 只答本地维度,库维度收窄绝不能把它翻成 True。

    构造必须**两维都带 `narrowed`**:`restricted` 先读 `narrowed`,它是 None 时整条
    boundary 判据都不执行,`self.narrowed or bool(self.base_narrowed)` 这类变异也就
    永远到不了。
    """
    with source_scope_context(
        "nb",
        SourceScope(mode="include", source_ids=["s1", "s2"], narrowed=False),
        BaseNotebookScope(mode="include", notebook_ids=["b1"], narrowed=True),
    ):
        assert base_scope_restricted() is True, "库维度确实收窄了"
        assert source_scope_restricted() is False, "库维度收窄绝不能关掉当前库的本地通道"
    # 反向:本地真收窄时该 True(证明上面不是恒 False 的死断言)。
    with source_scope_context(
        "nb",
        SourceScope(mode="include", source_ids=["s1"], narrowed=True),
        BaseNotebookScope(mode="include", notebook_ids=["b1"], narrowed=False),
    ):
        assert source_scope_restricted() is True


def test_base_only_narrowing_also_drops_prior_conversation_history():
    """History is inherently CROSS-library: a prior answer can quote content
    from a reference library the user has just unchecked, so gating it on the
    local question alone would let that content ride back into the next turn's
    prompt. Deliberately accepted trade-off (one library checkbox clears the
    multi-turn context)."""
    with source_scope_context(
        "nb", None,
        BaseNotebookScope(mode="include", notebook_ids=["b1"], narrowed=True),
    ):
        assert scoped_conversation_history("prior answer") == ""
    with source_scope_context(
        "nb", None,
        BaseNotebookScope(mode="include", notebook_ids=["b1"], narrowed=False),
    ):
        assert scoped_conversation_history("prior answer") == "prior answer"


def test_full_library_selection_keeps_its_frozen_mount_ceiling():
    """R2 的库维度版本:全选挂载库 ⇒ 不收窄,但冻结的挂载集仍是硬上限。"""
    frozen = ActiveSourceScope(
        notebook_id="nb", mode="exclude", source_ids=frozenset(),
        base_mode="include", base_notebook_ids=frozenset({"b1"}),
        base_narrowed=False,
    )
    assert frozen.base_restricted is False
    assert frozen.base_ceiling_active is True
    assert frozen.covers_notebook("b1") is True
    assert frozen.covers_notebook("b2") is False, (
        "冻结之后新挂载的参考库必须被这份快照挡住"
    )


def test_covers_notebook_exclude_branch_drops_only_the_listed_libraries():
    """Direct coverage of ``covers_notebook``'s exclude arm (every scope built
    through the API is frozen to include, so it is otherwise unreached and a
    ``return True`` mutation there would survive)."""
    scope = ActiveSourceScope(
        notebook_id="nb", mode="exclude", source_ids=frozenset(),
        base_mode="exclude", base_notebook_ids=frozenset({"dropped-base"}),
    )
    assert scope.covers_notebook("dropped-base") is False
    assert scope.covers_notebook("other-base") is True
    assert scope.covers_notebook("nb") is True          # active notebook, always
    assert scope.covers_notebook("") is True            # "this run's notebook"
    assert scope.allows("dropped-base", "s1") is False
    assert scope.allows("other-base", "s1") is True


def test_collapsed_paths_agree_on_an_excluded_base_library():
    """Collapsing-point consistency: ``allows()``/``source_allowed()``,
    ``scoped_allowed_source_ids()`` and ``evidence_json_allowed()`` must all
    treat an excluded mounted base library the same way."""
    base_scope = BaseNotebookScope(mode="include", notebook_ids=["kept-base"])
    with source_scope_context("nb", None, base_scope):
        assert source_allowed("excluded-base", "s1") is False
        assert source_allowed("kept-base", "s1") is True
        # Must be an explicit empty tuple, never None: None reads as
        # "unrestricted" to every `is not None` caller.
        excluded_ids = scoped_allowed_source_ids("excluded-base", ["s1"])
        assert excluded_ids == ()
        assert excluded_ids is not None
        assert scoped_allowed_source_ids("kept-base", ["s1"]) == ("s1",)
        assert evidence_json_allowed("excluded-base", [{"source_id": "s1"}]) is False
        assert evidence_json_allowed("kept-base", [{"source_id": "s1"}]) is True


def test_base_scope_default_include_empty_means_no_mounted_base_participates():
    with source_scope_context(
        "nb", None, BaseNotebookScope(mode="include", notebook_ids=[]),
    ):
        assert source_allowed("any-base", "s1") is False
        assert scoped_allowed_source_ids("any-base", ["s1"]) == ()


def test_validate_base_scope_omitted_returns_none():
    assert _validate_base_scope(_notebook(bases=[_notebook_ref("b1")]), None) is None


def test_validate_base_scope_freezes_exclude_to_include():
    notebook = _notebook(bases=[_notebook_ref("b1"), _notebook_ref("b2")])
    resolved = _validate_base_scope(
        notebook, BaseNotebookScope(mode="exclude", notebook_ids=["b2"])
    )
    # 1 of 2 mounted libraries selected -> a real narrowing.
    assert resolved == BaseNotebookScope(
        mode="include", notebook_ids=["b1"], narrowed=True
    )


def test_validate_base_scope_exclude_nothing_freezes_every_mounted_library():
    """R6: ``exclude`` + an EMPTY list (the browser's compact "select all")
    must expand into an explicit ``include`` list naming every currently-mounted
    library, never short-circuit to ``None``.

    ``None`` reads as "no scope was submitted at all", which is only harmless
    for a synchronous Ask. For a report this return value is PERSISTED and
    re-applied at confirm and generate, so ``None`` at create time would be
    re-interpreted against the mounts AT THAT LATER MOMENT — letting a library
    mounted after creation silently join a report the user never scoped it into
    (see ``test_report_base_scope_freezes_mount_set_at_create_time``).
    """
    notebook = _notebook(bases=[_notebook_ref("b1"), _notebook_ref("b2")])
    resolved = _validate_base_scope(
        notebook, BaseNotebookScope(mode="exclude", notebook_ids=[])
    )
    assert resolved == BaseNotebookScope(
        mode="include", notebook_ids=["b1", "b2"], narrowed=False
    )


def test_validate_base_scope_expands_beyond_a_thousand_mounts():
    """The mount API imposes no limit on how many libraries a notebook may
    mount, and the browser's default "select all" state expands into an
    explicit list — so the cap has to comfortably exceed any real mount count
    or validating an ordinary request raises pydantic's ValidationError."""
    bases = [_notebook_ref(f"b{i}") for i in range(1200)]
    resolved = _validate_base_scope(
        _notebook(bases=bases), BaseNotebookScope(mode="exclude", notebook_ids=[])
    )
    assert resolved is not None
    assert resolved.mode == "include"
    assert resolved.notebook_ids == [f"b{i}" for i in range(1200)]


def test_validate_base_scope_rejects_ids_not_mounted_on_this_notebook():
    with pytest.raises(HTTPException) as exc:
        _validate_base_scope(
            _notebook(bases=[_notebook_ref("b1")]),
            BaseNotebookScope(mode="include", notebook_ids=["not-mounted"]),
        )
    assert exc.value.status_code == 422


def test_validate_base_scope_never_trusts_a_client_supplied_narrowed():
    """Same rule as the source dimension: ``narrowed`` is a relation between
    the validated selection and the server's mount roster, recomputed
    unconditionally."""
    notebook = _notebook(bases=[_notebook_ref("b1"), _notebook_ref("b2")])
    forged = _validate_base_scope(
        notebook,
        BaseNotebookScope(mode="include", notebook_ids=["b1"], narrowed=False),
    )
    assert forged.narrowed is True
    honest = _validate_base_scope(
        notebook,
        BaseNotebookScope(mode="include", notebook_ids=["b1", "b2"], narrowed=True),
    )
    assert honest.narrowed is False


# ---------------------------------------------------------------------------
# 409: the emptiness judgement is "which libraries did this request check",
# not "which libraries are mounted".
# ---------------------------------------------------------------------------


def test_all_bases_excluded_and_no_local_sources_is_409():
    with pytest.raises(HTTPException) as exc:
        _require_ask_available(
            _notebook(bases=[_notebook_ref("b1")]), _ScopeRepo([], 0),
            SourceScope(mode="include", source_ids=[]),
            BaseNotebookScope(mode="include", notebook_ids=[]),
        )
    assert exc.value.status_code == 409


def test_base_only_submission_on_a_library_only_notebook_is_409():
    """A request that submits ONLY the library dimension.

    "本地维度没提交" 曾被当成 "本地非空",于是一个零本地来源、靠挂载参考库才
    ask_available 的笔记本,把每个参考库都取消勾选后仍然放行:Ask 白跑一轮零证据,
    报告更糟——落一行 + 照常调意图模型。省略的那一维必须按**真实证据宇宙**判空。
    """
    notebook = _notebook(bases=[_notebook_ref("b1")])
    assert notebook.counts.get("sources", 0) == 0
    with pytest.raises(HTTPException) as exc:
        _require_ask_available(
            notebook, _ScopeRepo([], 0),
            None,                                    # ← 刻意省略本地维度
            BaseNotebookScope(mode="include", notebook_ids=[]),
        )
    assert exc.value.status_code == 409


def test_knowhow_only_notebook_excluding_every_library_is_not_409():
    """本地证据宇宙 ≠ 可见导入来源数。

    只有 Knowhow 表(或只有已确认 Memory)的笔记本可见来源恒为 0,于是浏览器**默认**
    发出的 `exclude:[]` 被冻结成 `include:[]`、`narrowed=False` —— 这一维没有表达任何
    收窄意图,却会被「来源数为 0」当成本地为空。再把参考库全部取消勾选,整个请求就被
    409 掉,而那些 Knowhow 格子照常可搜。这是**界面可达**的误拒。
    """
    notebook = _notebook(
        bases=[_notebook_ref("b1")], sources=0, local_evidence=True,
    )
    resolved_source, resolved_base = _require_ask_available(
        notebook, _ScopeRepo([], 0),
        SourceScope(mode="exclude", source_ids=[]),   # ← 浏览器的「全选」表示法
        BaseNotebookScope(mode="include", notebook_ids=[]),
    )
    assert resolved_source == SourceScope(
        mode="include", source_ids=[], narrowed=False
    )
    assert resolved_base == BaseNotebookScope(
        mode="include", notebook_ids=[], narrowed=True
    )


def test_clearing_every_local_source_is_still_409():
    """反向:本地维度**真被收窄**(3 选 0)时以冻结选择为准,本地证据信号不得把用户
    主动点下的「清空」翻回来。"""
    notebook = _notebook(
        bases=[_notebook_ref("b1")], sources=3, local_evidence=True,
    )
    with pytest.raises(HTTPException) as exc:
        _require_ask_available(
            notebook, _ScopeRepo(["s1", "s2", "s3"], 3),
            SourceScope(mode="include", source_ids=[]),
            BaseNotebookScope(mode="include", notebook_ids=[]),
        )
    assert exc.value.status_code == 409


def test_base_only_submission_with_local_sources_present_is_not_409():
    notebook = _notebook(bases=[_notebook_ref("b1")], sources=3)
    resolved_source, resolved_base = _require_ask_available(
        notebook, _ScopeRepo(["s1", "s2", "s3"], 3),
        None,
        BaseNotebookScope(mode="include", notebook_ids=[]),
    )
    assert resolved_source is None
    assert resolved_base == BaseNotebookScope(
        mode="include", notebook_ids=[], narrowed=True
    )


def test_scoping_neither_dimension_is_never_rejected_for_emptiness():
    """两维都没提交的请求不是一次「选择」,``ask_available`` 才是权威。

    它看得见 Knowhow 格子、confirmed Memory 和参考库的 KG —— 这个函数一样都看不见。
    按 counts["sources"] 判空会把这些库的历史无范围调用直接判死。
    """
    _require_ask_available(_notebook(), _ScopeRepo([], 0), None, None)
    _require_ask_available(
        _notebook(bases=[_notebook_ref("b1")]), _ScopeRepo([], 0), None, None
    )


def test_deselecting_only_bases_with_local_sources_present_is_not_409():
    notebook = _notebook(bases=[_notebook_ref("b1")], sources=1)
    resolved_source, resolved_base = _require_ask_available(
        notebook, _ScopeRepo(["s1"], 1),
        SourceScope(mode="include", source_ids=["s1"]),
        BaseNotebookScope(mode="include", notebook_ids=[]),
    )
    assert resolved_source == SourceScope(
        mode="include", source_ids=["s1"], narrowed=False
    )
    assert resolved_base == BaseNotebookScope(
        mode="include", notebook_ids=[], narrowed=True
    )


# ---------------------------------------------------------------------------
# Consumption boundaries (pure helpers + service seams).
# ---------------------------------------------------------------------------


def test_base_only_scope_still_filters_candidates_at_the_result_boundary():
    """``filter_retrieval_items`` must NOT short-circuit on a base-only run.

    A run that unchecks only reference libraries leaves the LOCAL ``restricted``
    flag False by design (R1), so the short-circuit has to test BOTH ceilings.
    """
    chunks = [
        RetrievedChunk("c-local", "s1", "local", "", "local"),
        RetrievedChunk("c-kept", "bs", "kept", "", "kept", notebook_id="kept-base"),
        RetrievedChunk("c-gone", "bs", "gone", "", "gone", notebook_id="dropped-base"),
    ]
    with source_scope_context(
        "nb", None, BaseNotebookScope(mode="include", notebook_ids=["kept-base"]),
    ):
        assert source_scope_restricted() is False        # R1 still holds
        assert source_scope_ceiling_active() is False    # 本地维度没提交
        assert base_scope_ceiling_active() is True
        kept = [row.chunk_id for row in filter_retrieval_items("nb", "chunk", chunks)]
    assert kept == ["c-local", "c-kept"]


def test_filter_short_circuit_is_ceiling_driven_not_narrowing_driven():
    """变异守卫:短路必须问**上限**,不是问收窄。

    浏览器默认全选 ⇒ 两维都被冻结成 explicit include 且 `narrowed=False`,于是
    `restricted` 与 `base_restricted` 都是 False —— 把短路判据写成它们,整个循环在
    **每一个** UI 请求上都被跳过,冻结快照形同虚设。
    """
    chunks = [
        RetrievedChunk("c-in", "s1", "in", "", "in"),
        RetrievedChunk("c-new", "s-new", "new", "", "new"),      # 冻结后上传
        RetrievedChunk("cb-in", "bs", "kept", "", "kept", notebook_id="b1"),
        RetrievedChunk("cb-new", "bs", "new", "", "new", notebook_id="b-new"),
    ]
    with source_scope_context(
        "nb",
        SourceScope(mode="include", source_ids=["s1"], narrowed=False),
        BaseNotebookScope(mode="include", notebook_ids=["b1"], narrowed=False),
    ):
        assert source_scope_restricted() is False
        assert base_scope_restricted() is False
        assert source_scope_ceiling_active() is True
        assert base_scope_ceiling_active() is True
        kept = [row.chunk_id for row in filter_retrieval_items("nb", "chunk", chunks)]
    assert kept == ["c-in", "cb-in"], (
        "全选时冻结上限必须照常过滤;短路判据写成 restricted 会放行 c-new/cb-new"
    )


def test_excluded_library_knowledge_and_relations_are_dropped_not_stripped():
    """R8: an unchecked library's KG node must be REMOVED, not just emptied of
    evidence — ``evidence_context.knowledge_context()`` never reads
    ``hit.evidence``; it re-queries ``node_context(origin, object_id)`` and
    assigns the hit its own ``k{n}`` anchor, so a kept-but-empty node still
    enters the answer prompt and stays citable, merely untraceable."""
    base_scope = BaseNotebookScope(mode="include", notebook_ids=["kept-base"])
    with source_scope_context("nb", None, base_scope):
        for kind in ("knowledge", "relation"):
            dropped = filter_retrieval_items(
                "nb", kind, [_knowledge("s1", notebook_id="dropped-base")]
            )
            assert dropped == [], kind
            kept = filter_retrieval_items(
                "nb", kind, [_knowledge("s1", notebook_id="kept-base")]
            )
            assert len(kept) == 1 and kept[0].evidence, kind


def test_base_only_scope_keeps_evidence_less_active_nodes():
    """Within a base-only run the local dimension was never narrowed, so
    nothing local was filtered and an active-notebook node that simply carries
    no evidence must survive — exactly as before this loop could be entered."""
    empty = RetrievedKnowledge(
        object_id="ko-active", object_type="claim", payload={"name": "x"},
        evidence=[], notebook_id="nb",
    )
    with source_scope_context(
        "nb", None, BaseNotebookScope(mode="include", notebook_ids=["kept-base"]),
    ):
        assert len(filter_retrieval_items("nb", "knowledge", [empty])) == 1
    # But a LOCAL ceiling still drops it — that branch is unchanged.
    with source_scope_context("nb", SourceScope(mode="include", source_ids=["s1"])):
        assert filter_retrieval_items("nb", "knowledge", [empty]) == []


def test_base_only_scope_never_fabricates_a_local_scope_payload():
    """``current_source_scope_payload()`` is re-persisted into a report's
    understanding contract and re-frozen on confirm. Returning a synthetic
    ``exclude:[]`` for a base-only run would be frozen into
    ``include:[every visible source]`` — freezing a local ceiling onto a report
    whose author only unchecked a reference library (R1 across persistence)."""
    with source_scope_context(
        "nb", None, BaseNotebookScope(mode="include", notebook_ids=["b1"]),
    ):
        assert current_source_scope_payload() is None
        assert current_base_scope_payload() == {
            "mode": "include", "notebook_ids": ["b1"], "narrowed": None,
        }
    # Symmetric: a local-only run must not fabricate a base payload either —
    # persisting one would freeze the libraries mounted at that instant and
    # lock out any reference library mounted later.
    with source_scope_context("nb", SourceScope(mode="include", source_ids=["s1"])):
        assert current_base_scope_payload() is None
        assert current_source_scope_payload() == {
            "mode": "include", "source_ids": ["s1"], "narrowed": None,
        }
    with source_scope_context(
        "nb", SourceScope(mode="include", source_ids=["s1"]),
        BaseNotebookScope(mode="exclude", notebook_ids=["b2"]),
    ):
        assert current_source_scope_payload() == {
            "mode": "include", "source_ids": ["s1"], "narrowed": None,
        }
        assert current_base_scope_payload() == {
            "mode": "exclude", "notebook_ids": ["b2"], "narrowed": None,
        }


def test_follow_chain_drops_hops_carried_by_an_unchecked_library():
    """``follow_chain``'s cross-notebook arm keeps a hop's evidence verbatim —
    correct for a library that is still checked, a leak for one that is not.
    The gate also cannot be ``source_scope_restricted()``: a library-only run
    leaves that False on purpose (R1)."""
    def hop(relation_id: str, notebook_id: str, source: str, target: str):
        return ChainHop(
            relation_id=relation_id, notebook_id=notebook_id, tier="personal",
            source_object_id=source, target_object_id=target,
            edge_type="precedes", source_name=source, target_name=target,
            evidence=[{"source_id": f"s-{notebook_id}",
                       "quoted_span": f"from {notebook_id}"}],
        )

    def chain(via_notebook: str, suffix: str):
        return InferredChain(
            source_object_id="a", via_object_id=f"b{suffix}",
            target_object_id=f"c{suffix}", source_name="a",
            via_name=f"b{suffix}", target_name=f"c{suffix}",
            inferred_edge_type="precedes",
            hops=(hop(f"r1{suffix}", "nb", "a", f"b{suffix}"),
                  hop(f"r2{suffix}", via_notebook, f"b{suffix}", f"c{suffix}")),
        )

    kept, dropped = chain("kept-base", "-k"), chain("dropped-base", "-d")
    nodes = [
        {"object_id": oid, "notebook_id": "nb", "evidence": [{"source_id": "s1"}]}
        for oid in ("a", "b-k", "c-k", "b-d", "c-d")
    ]

    class _Graph:
        def follow_chain(self, *_args, **_kwargs):
            return FollowChainResult(inferences=[kept, dropped], nodes=nodes)

    retrieval = RetrievalService(
        candidates=object(), graph=_Graph(), community_queries=lambda: []
    )
    with source_scope_context(
        "nb", None, BaseNotebookScope(mode="include", notebook_ids=["kept-base"]),
    ):
        result = retrieval.follow_chain("nb", "a")

    assert [item.target_object_id for item in result.inferences] == ["c-k"]
    assert all(
        item.notebook_id != "dropped-base"
        for inference in result.inferences for item in inference.hops
    )


def test_node_context_drops_a_row_from_an_unchecked_library():
    """``retrieval.node_context`` is reasoning's query-time chain hydration
    reader — and ONLY that. It is deliberately not the gate for
    ``knowledge_context()``, which is wired to the graph service directly."""
    class _Graph:
        def node_context(self, notebook_id, _object_id):
            return {"notebook_id": notebook_id,
                    "definition": f"text from {notebook_id}",
                    "evidence": [{"source_id": f"s-{notebook_id}"}]}

    retrieval = RetrievalService(
        candidates=object(), graph=_Graph(), community_queries=lambda: []
    )
    with source_scope_context(
        "nb", None, BaseNotebookScope(mode="include", notebook_ids=["kept-base"]),
    ):
        assert retrieval.node_context("dropped-base", "ko-1") == {}
        assert retrieval.node_context("kept-base", "ko-1")["definition"]
        assert retrieval.node_context("nb", "ko-1")["definition"]
    # R2: with no scope in context the row is handed back untouched.
    assert retrieval.node_context("dropped-base", "ko-1")["definition"]


def test_comparison_peers_are_not_borrowed_from_an_unchecked_library():
    """R4 — 泄漏面包括查询词本身。

    横向对比(共提/社区)按库枚举取「兄弟实体名」,那些名字是从库内容里读出来的:
    它们进可见轨迹、进 used_queries、并被回喂进 reflect prompt。结果侧的过滤救不了
    这条通道 —— 泄漏的是**查询词本身**,不是命中。
    """
    from app.services.communities import CommunityQueryService

    class _Kg:
        def mounted_base_ids(self, _active):
            return ["kept-base", "dropped-base"]

    service = CommunityQueryService(
        notebooks=object(), unified_kg=_Kg(), event_log=object()
    )
    assert service.mounted_base_ids("nb") == ["kept-base", "dropped-base"]

    with source_scope_context(
        "nb", None, BaseNotebookScope(mode="include", notebook_ids=["kept-base"]),
    ):
        assert source_scope_restricted() is False        # R1 still holds
        assert service.mounted_base_ids("nb") == ["kept-base"]

    with source_scope_context(
        "nb", None, BaseNotebookScope(mode="include", notebook_ids=[]),
    ):
        assert service.mounted_base_ids("nb") == []


# ---------------------------------------------------------------------------
# KG availability gate — a JUDGEMENT, not a result filter.
# ---------------------------------------------------------------------------


class _KgProbeCandidates:
    """``RetrievalService.any_base_has_kg`` 需要的三个协作者,各自记账被调了几次。

    ``_any_base_notebook_has_kg`` 模拟真实实现:一条跨**全部有效挂载库**的 mount-join
    EXISTS —— 它答不了「这次勾了的库里有没有图」,这正是被测的缺陷。
    """

    def __init__(self, *, mounted, with_kg):
        self._mounted = list(mounted)
        self._with_kg = set(with_kg)
        self.mount_join_calls = 0
        self.probed: list[str] = []
        self.notebooks = SimpleNamespace(
            participant_notebook_ids=self._participant_notebook_ids
        )

    def _participant_notebook_ids(self, active_notebook_id):
        # resolve_participants 的形状:首项恒为 active 本身。
        return [active_notebook_id, *self._mounted]

    def _notebook_has_kg(self, notebook_id):
        self.probed.append(notebook_id)
        return notebook_id in self._with_kg

    def _any_base_notebook_has_kg(self, notebook_id):
        self.mount_join_calls += 1
        return any(base_id in self._with_kg for base_id in self._mounted)


def _kg_gate(candidates):
    return RetrievalService(
        candidates=candidates, graph=object(), community_queries=lambda: []
    )


def test_kg_availability_ignores_an_unchecked_reference_library():
    """KG 可用性闸是**判据**而非结果过滤。

    本库自己没图、唯一带图的参考库被取消勾选时,旧实现仍走那条遍历**全部**挂载库的
    mount-join EXISTS,于是报「有图可用」:ask_service 的无图早退不触发、
    ``kg_required`` 不翻真、graph 路径照常跑一整轮 —— 跑在一份这次根本不许读的图上。
    """
    candidates = _KgProbeCandidates(mounted=["b1"], with_kg={"b1"})
    gate = _kg_gate(candidates)
    with source_scope_context(
        "nb", None,
        BaseNotebookScope(mode="include", notebook_ids=[], narrowed=True),
    ):
        assert gate.any_base_has_kg("nb") is False
    # 全库盲查那条路一次都不能走,也不该去探本库自己的图(R1)。
    assert candidates.mount_join_calls == 0
    assert candidates.probed == []


def test_kg_availability_keeps_a_checked_reference_library():
    """反向护栏:同样的配置下**勾着**那个库,仍判为可用 —— 证明这不是一刀切关掉。"""
    candidates = _KgProbeCandidates(mounted=["b1"], with_kg={"b1"})
    gate = _kg_gate(candidates)
    with source_scope_context(
        "nb", None, BaseNotebookScope(mode="include", notebook_ids=["b1"]),
    ):
        assert gate.any_base_has_kg("nb") is True
    assert candidates.probed == ["b1"]
    assert "nb" not in candidates.probed


def test_kg_availability_follows_which_library_was_checked():
    """挂两个库、只有一个有图:勾中没图的那个判不可用,勾中有图的那个判可用。

    这一对把判据钉成「勾了的库里有没有图」,而不是「勾了几个库」。
    """
    gate_without = _kg_gate(_KgProbeCandidates(mounted=["b1", "b2"], with_kg={"b2"}))
    with source_scope_context(
        "nb", None, BaseNotebookScope(mode="include", notebook_ids=["b1"]),
    ):
        assert gate_without.any_base_has_kg("nb") is False
    gate_with = _kg_gate(_KgProbeCandidates(mounted=["b1", "b2"], with_kg={"b2"}))
    with source_scope_context(
        "nb", None, BaseNotebookScope(mode="include", notebook_ids=["b2"]),
    ):
        assert gate_with.any_base_has_kg("nb") is True


def test_kg_availability_reads_the_frozen_selection_not_the_live_mount_set():
    """判据是 ``base_ceiling_active``(提交过库维度)而不是 ``base_restricted``
    (收窄过库维度)。差别只在「全选」这一种形状上,而全选也是一份**冻结**的选择:
    请求进来之后新挂上的库不在其中,每个候选生产者都会把它滤掉。这时若回头去问那条
    实时 mount-join,新库的图会被算进可用性,闸与检索当场分叉。"""
    candidates = _KgProbeCandidates(
        mounted=["b1", "b2"], with_kg={"b2"},   # b2 是冻结之后才挂上的
    )
    gate = _kg_gate(candidates)
    with source_scope_context(
        "nb", None,
        BaseNotebookScope(mode="include", notebook_ids=["b1"], narrowed=False),
    ):
        assert gate.any_base_has_kg("nb") is False
    assert candidates.mount_join_calls == 0
    assert candidates.probed == ["b1"]


def test_kg_availability_without_a_base_scope_costs_the_same_one_query():
    """R10:没提交库维度时逐字回到改前 —— 一条 mount-join EXISTS,零新增往返。

    同时覆盖「只收窄了本地来源」:R1 要求库维度之外的收窄不得改变这道闸的取数方式,
    否则一次本地勾选会顺带给每个挂载库各买一次探测。
    """
    candidates = _KgProbeCandidates(mounted=["b1"], with_kg={"b1"})
    gate = _kg_gate(candidates)
    assert gate.any_base_has_kg("nb") is True          # 无 scope 上下文
    with source_scope_context(
        "nb", SourceScope(mode="include", source_ids=["s1"], narrowed=True), None,
    ):
        assert gate.any_base_has_kg("nb") is True
    assert candidates.mount_join_calls == 2
    assert candidates.probed == []


def test_kg_availability_exclude_shaped_base_scope_is_also_narrowed():
    """``exclude`` 形状只有服务层直接构造得出(API 入口一律冻结成 include),但它必须
    同样被认:收窄发生在 ``scoped_participants``/``covers_notebook`` 这一个收口上。"""
    candidates = _KgProbeCandidates(mounted=["b1", "b2"], with_kg={"b2"})
    gate = _kg_gate(candidates)
    with source_scope_context(
        "nb", None, BaseNotebookScope(mode="exclude", notebook_ids=["b2"]),
    ):
        assert gate.any_base_has_kg("nb") is False
    assert candidates.mount_join_calls == 0
    assert candidates.probed == ["b1"]


# ---------------------------------------------------------------------------
# Graph walk — the federated graph is process-cached under a scope-blind key,
# so the ceiling reaches the walk's RESULT (R9).
# ---------------------------------------------------------------------------


def test_chunk_overlay_graph_walk_drops_nodes_from_an_unchecked_library():
    from app.services.kg.graph_reason import build_rx_graph
    from app.services.retrieval_candidates import CandidateRetrievalService

    local_hit = _knowledge("s1")
    nodes = {
        "ko-s1": {"type": "concept", "name": "local-concept",
                  "tier": "personal", "notebook_id": "nb"},
        "ko-base": {"type": "concept", "name": "library-only-concept",
                    "tier": "base", "notebook_id": "dropped-base"},
    }
    relations = [{
        "id": "r1", "source_object_id": "ko-s1", "target_object_id": "ko-base",
        "edge_type": "kind_of", "evidence": [], "notebook_id": "nb",
        "review_status": "verified",
    }]
    graph = build_rx_graph(
        nodes, relations, tier="personal",
        tier_map={"nb": "personal", "dropped-base": "base"},
    )

    class _Candidates:
        _MIX_NODE_SEEDS = 8
        _MIX_REL_SEEDS = 8
        _MIX_FANOUT = 8

        def federated_retrieve(self, *_args, **_kwargs):
            return [local_hit]

        def _federated_graph_is_large(self, *_args, **_kwargs):
            return False

        def federated_retrieve_relations(self, *_args, **_kwargs):
            return []

        def _federated_rx_graph(self, *_args, **_kwargs):
            return graph

    def _overlay(base_scope):
        with source_scope_context("nb", None, base_scope):
            return CandidateRetrievalService._chunk_kg_overlay(
                _Candidates(), "nb", "question", "", 1000
            )

    block, id_map, _hits, _supports = _overlay(
        BaseNotebookScope(mode="include", notebook_ids=["dropped-base"])
    )
    assert "library-only-concept" in block, (
        "baseline: a CHECKED library's neighbour must still be walked in"
    )
    assert any(row["object_id"] == "ko-base" for row in id_map.values())

    block, id_map, _hits, _supports = _overlay(
        BaseNotebookScope(mode="include", notebook_ids=[])
    )
    assert "local-concept" in block
    assert "library-only-concept" not in block
    assert all(row["object_id"] != "ko-base" for row in id_map.values())


# ---------------------------------------------------------------------------
# T3 — read-only receipt.
# ---------------------------------------------------------------------------


def _receipt_notebook(*, sources: int = 0, bases=None) -> NotebookSummary:
    return NotebookSummary(
        id="nb", name="n", purpose="", primary_domain="", status="ready",
        counts={"sources": sources}, created_label="", base_notebooks=bases or [],
    )


def test_receipt_is_absent_when_the_request_narrowed_neither_dimension():
    """With no receipt the serialized answer is byte-identical to every payload
    written before this field existed. The browser submits BOTH scopes on every
    request, so the rule has to be "did anything actually narrow?", not "was a
    scope submitted?" — otherwise every ordinary question grows a receipt
    reading "5 of 5 来源"."""
    from app.models.ask import AskResponse

    notebook = _receipt_notebook(
        sources=3, bases=[NotebookRef(id="b1", name="论文库")]
    )
    assert _scope_receipt(notebook, None, None) is None
    assert _scope_receipt(
        notebook,
        SourceScope(mode="include", source_ids=["s1", "s2", "s3"], narrowed=False),
        BaseNotebookScope(mode="include", notebook_ids=["b1"], narrowed=False),
    ) is None
    assert "retrieval_scope" not in AskResponse(conclusion="c").model_dump()


def test_receipt_reports_the_untouched_dimension_as_whole():
    """One narrowed dimension still reports the other, truthfully. That pairing
    IS the incident: a single checked local source next to a fully
    participating reference library."""
    notebook = _receipt_notebook(
        sources=3, bases=[NotebookRef(id="b1", name="论文库")]
    )
    local_only = _scope_receipt(
        notebook,
        SourceScope(mode="include", source_ids=["s1"], narrowed=True),
        BaseNotebookScope(mode="include", notebook_ids=["b1"], narrowed=False),
    )
    assert local_only.local.selected == 1 and local_only.local.total == 3
    assert [(b.notebook_id, b.name, b.included) for b in local_only.bases] == [
        ("b1", "论文库", True)
    ]

    base_only = _scope_receipt(
        notebook, None,
        BaseNotebookScope(mode="include", notebook_ids=[], narrowed=True),
    )
    # An unscoped local dimension searched every visible source: selected ==
    # total says "all of them", never a narrowing the user did not request.
    assert base_only.local.selected == 3 and base_only.local.total == 3
    assert [b.included for b in base_only.bases] == [False]


def test_receipt_marks_each_mounted_library_included_or_not():
    notebook = _receipt_notebook(sources=2, bases=[
        NotebookRef(id="b1", name="论文库"),
        NotebookRef(id="b2", name="手册库"),
    ])
    receipt = _scope_receipt(
        notebook,
        SourceScope(mode="include", source_ids=["s1"], narrowed=True),
        BaseNotebookScope(mode="include", notebook_ids=["b2"], narrowed=True),
    )
    assert [(b.name, b.included) for b in receipt.bases] == [
        ("论文库", False), ("手册库", True),
    ]


def test_receipt_snapshots_library_names_so_replay_survives_unmounting():
    """The persisted receipt must not depend on the notebook's CURRENT mounts.
    Re-deriving names at read time would drop precisely the library that
    explains a past answer once it is unmounted."""
    from app.models.ask import AskResponse

    receipt = _scope_receipt(
        _receipt_notebook(sources=1, bases=[NotebookRef(id="b1", name="论文库")]),
        SourceScope(mode="include", source_ids=["s1"], narrowed=True),
        BaseNotebookScope(mode="include", notebook_ids=["b1"], narrowed=False),
    )
    stored = AskResponse(conclusion="c", retrieval_scope=receipt).model_dump()
    # The library is unmounted afterwards; the payload still names it.
    assert _receipt_notebook(sources=1).base_notebooks == []
    replayed = AskResponse(**stored)
    assert replayed.retrieval_scope.bases[0].name == "论文库"
    assert replayed.retrieval_scope.bases[0].included is True


def test_legacy_answer_payload_without_the_receipt_still_loads():
    from app.models.ask import AskResponse

    legacy = {"conclusion": "c", "answer": "a", "grounded": True}
    assert AskResponse(**legacy).retrieval_scope is None


def test_receipt_exposes_only_display_safe_fields():
    """The disclosure surface is names and counts. Pinned as a field set so a
    later edit cannot quietly add a file path or an error string to a payload
    that is persisted and replayed."""
    from app.models.source_scope import (
        RetrievalScopeBaseReceipt,
        RetrievalScopeLocalReceipt,
        RetrievalScopeReceipt,
    )

    assert set(RetrievalScopeReceipt.model_fields) == {"local", "bases"}
    assert set(RetrievalScopeLocalReceipt.model_fields) == {"selected", "total"}
    assert set(RetrievalScopeBaseReceipt.model_fields) == {
        "notebook_id", "name", "included",
    }


def test_receipt_reads_an_unfrozen_exclude_scope_by_its_mode():
    """The routes always freeze to ``include`` first, so this shape cannot
    arrive over HTTP — but reading ``exclude`` as if it were ``include`` would
    INVERT the receipt (naming the UNCHECKED libraries as the searched ones),
    and a scope report that can lie about the scope is worth less than none."""
    notebook = _receipt_notebook(sources=4, bases=[
        NotebookRef(id="b1", name="论文库"),
        NotebookRef(id="b2", name="手册库"),
    ])
    receipt = _scope_receipt(
        notebook,
        SourceScope(mode="exclude", source_ids=["s1"], narrowed=True),
        BaseNotebookScope(mode="exclude", notebook_ids=["b1"], narrowed=True),
    )
    assert receipt.local.selected == 3 and receipt.local.total == 4
    assert [(b.name, b.included) for b in receipt.bases] == [
        ("论文库", False), ("手册库", True),
    ]


def test_receipt_does_not_truncate_below_the_model_cap():
    """回执是**披露**载体:被裁过的列表当成完整的,会让读者算错分母,还可能藏起对某个
    被省略库的收窄。构造处的截断上限必须跟随 ``RetrievalScopeReceipt.bases`` 的模型
    上限 —— 更低的值等于静默丢掉模型本可接受的库。"""
    bases = [NotebookRef(id=f"b{i}", name=f"参考库{i}") for i in range(1200)]
    receipt = _scope_receipt(
        _receipt_notebook(sources=1, bases=bases), None,
        BaseNotebookScope(mode="include", notebook_ids=["b0"], narrowed=True),
    )
    assert receipt is not None
    assert len(receipt.bases) == 1200, "1000 处截断会让回执谎报参考库总数"
    assert sum(1 for item in receipt.bases if item.included) == 1


def test_save_answer_stamps_the_receipt_from_the_run_context():
    """The single answer-persistence seam stamps the receipt BEFORE the payload
    is written, so a reopened turn replays it; with no receipt in context the
    response is left exactly as the handler built it."""
    from app.models.ask import AskResponse
    from app.models.source_scope import (
        RetrievalScopeBaseReceipt,
        RetrievalScopeLocalReceipt,
        RetrievalScopeReceipt,
    )
    from app.services.ask_service import AskService
    from app.services.source_scope import retrieval_scope_receipt_context

    saved: list = []

    class _State:
        def save_answer(self, notebook_id, conversation_id, question,
                        response, user_id):
            saved.append(response.model_dump())
            return "ans-1"

    service = SimpleNamespace(_needs_index=lambda _nb: False, ask_state=_State())
    receipt = RetrievalScopeReceipt(
        local=RetrievalScopeLocalReceipt(selected=1, total=84),
        bases=[RetrievalScopeBaseReceipt(
            notebook_id="b1", name="论文库", included=False
        )],
    )

    response = AskResponse(conclusion="c")
    with retrieval_scope_receipt_context(receipt):
        AskService._save_answer(service, "nb", "q", response, None, user_id="u")
    assert response.retrieval_scope == receipt
    assert saved[-1]["retrieval_scope"]["bases"][0]["name"] == "论文库"

    unscoped = AskResponse(conclusion="c")
    AskService._save_answer(service, "nb", "q", unscoped, None, user_id="u")
    assert unscoped.retrieval_scope is None
    assert "retrieval_scope" not in saved[-1]


def test_receipt_context_is_display_only_and_read_at_exactly_one_seam():
    """The receipt must never become an input to a gate. A docstring cannot
    enforce that, so pin the reader/writer sets — any new retrieval owner that
    starts consulting it reports here."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"
    readers, writers = [], []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "current_retrieval_scope_receipt" in text:
            readers.append(str(path.relative_to(root)))
        if "retrieval_scope_receipt_context" in text:
            writers.append(str(path.relative_to(root)))
    assert sorted(readers) == [
        "services/ask_service.py", "services/source_scope.py",
    ]
    assert sorted(writers) == ["api/ask_routes.py", "services/source_scope.py"]


def test_receipt_never_reaches_the_retrieval_gates():
    """Same requirement from the behavioural side: a receipt in context on its
    own changes no filtering decision at all."""
    from app.models.source_scope import (
        RetrievalScopeBaseReceipt,
        RetrievalScopeLocalReceipt,
        RetrievalScopeReceipt,
    )
    from app.services.source_scope import retrieval_scope_receipt_context

    chunks = [
        RetrievedChunk("c1", "s1", "one", "", "one"),
        RetrievedChunk("c2", "s2", "two", "", "two"),
    ]
    with retrieval_scope_receipt_context(RetrievalScopeReceipt(
        local=RetrievalScopeLocalReceipt(selected=1, total=2),
        bases=[RetrievalScopeBaseReceipt(
            notebook_id="b1", name="论文库", included=False
        )],
    )):
        assert current_source_scope() is None
        assert source_scope_restricted() is False
        assert base_scope_restricted() is False
        assert filter_retrieval_items("nb", "chunk", chunks) == chunks
        assert scoped_allowed_source_ids("nb") is None
        assert source_allowed("b1", "s9") is True


def test_sync_ask_route_carries_the_receipt_into_the_service(monkeypatch):
    """Route wiring, sync path: the receipt built from the NotebookSummary the
    route already loaded must be in context while ``repo.ask`` runs — that is
    the only reason it can reach the answer that gets persisted."""
    from app.api import ask_routes
    from app.models.ask import AskResponse
    from app.services.source_scope import current_retrieval_scope_receipt

    seen: list = []

    class _Repo:
        def get_notebook(self, _notebook_id):
            return _receipt_notebook(
                sources=2, bases=[NotebookRef(id="b1", name="论文库")]
            )

        def current_user(self):
            return SimpleNamespace(id="user-a")

        def all_visible_source_ids(self, _notebook_id):
            return ["s1", "s2"]

        def all_hidden_source_ids(self, _notebook_id):
            return []

        def ask(self, _notebook_id, _payload):
            seen.append(current_retrieval_scope_receipt())
            return AskResponse(conclusion="c")

    monkeypatch.setattr(ask_routes, "repository", lambda: _Repo())
    ask_routes.ask("nb", AskRequest(
        question="q", mode="chunk",
        source_scope=SourceScope(mode="include", source_ids=["s1"]),
        base_scope=BaseNotebookScope(mode="include", notebook_ids=[]),
    ))
    assert seen and seen[0] is not None
    assert seen[0].local.selected == 1 and seen[0].local.total == 2
    assert [(b.name, b.included) for b in seen[0].bases] == [("论文库", False)]
    # The context does not leak past the request.
    assert current_retrieval_scope_receipt() is None

    seen.clear()
    ask_routes.ask("nb", AskRequest(question="q", mode="chunk"))
    assert seen == [None]        # scoped nothing -> nothing to report


def test_streaming_receipt_reaches_the_detached_worker():
    """Streaming path: the worker outlives the connection, so the receipt has
    to be in context at the ``submit()`` that snapshots it — not merely inside
    the coroutine that returns the StreamingResponse, which has already
    returned by the time the generator runs."""
    import asyncio
    import logging
    import threading

    from app.api.ask_routes import _stream_ask_events
    from app.models.ask import AskResponse
    from app.models.source_scope import (
        RetrievalScopeBaseReceipt,
        RetrievalScopeLocalReceipt,
        RetrievalScopeReceipt,
    )
    from app.services import background_jobs
    from app.services.ask_execution import (
        AskCancellationRegistry,
        AskExecutionCoordinator,
    )
    from app.services.source_scope import current_retrieval_scope_receipt

    seen: list = []
    done = threading.Event()

    class _State:
        def begin_durable_job(self, notebook_id, payload, mode, user_id):
            payload.conversation_id = "conv-1"
            return "askjob-1", "conv-1"

        def append_trace(self, *_a, **_kw):
            pass

        def finish_job(self, job_id, status, *, answer_id="", error=""):
            done.set()
            return "conv-1"

        def cleanup_empty_conversation(self, _conversation_id):
            pass

    class _Service:
        def ask(self, _nb, _payload, *, user_id, job_id="", on_trace=None,
                cancel_event=None):
            seen.append(current_retrieval_scope_receipt())
            return AskResponse(conclusion="c", answer_id="ans-1")

    coordinator = AskExecutionCoordinator(
        ask_state=_State(),
        cancellations=AskCancellationRegistry(),
        job_submitter=background_jobs,
        event_log=SimpleNamespace(logger=logging.getLogger("base-scope-stream")),
        ask=lambda: _Service(),
    )

    class _Repo:
        def current_user(self):
            return SimpleNamespace(id="user-x")

        def start_ask_stream(self, notebook_id, payload, mode, *, user_id):
            return coordinator.start(notebook_id, payload, mode, user_id=user_id)

    class _Disconnected:
        async def is_disconnected(self):
            return True

    receipt = RetrievalScopeReceipt(
        local=RetrievalScopeLocalReceipt(selected=1, total=84),
        bases=[RetrievalScopeBaseReceipt(
            notebook_id="b1", name="论文库", included=False
        )],
    )

    async def drive():
        stream = _stream_ask_events(
            _Repo(), "nb", AskRequest(question="q", mode="chunk"),
            SimpleNamespace(id="chunk", handler="ask_chunk", streaming=False),
            _Disconnected(), scope_receipt=receipt,
        )
        try:
            while True:
                await stream.__anext__()
        except StopAsyncIteration:
            pass

    asyncio.run(drive())
    assert done.wait(2)
    assert seen == [receipt]

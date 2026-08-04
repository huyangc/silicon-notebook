from app.models.common import Evidence
from app.models.ask import AskRequest
from app.models.source_scope import BaseNotebookScope, SourceScope
from app.models.notebooks import NotebookRef, NotebookSummary
from app.api.ask_routes import (
    _require_ask_available,
    _validate_base_scope,
    _validate_reasoning_scope_preflight,
    _validate_source_scope,
)
from types import SimpleNamespace
from fastapi import HTTPException
import pytest
from app.services.retrieval import RetrievedChunk, RetrievedKnowledge
from app.services.kg.follow_chain import ChainHop, FollowChainResult, InferredChain
from app.services.retrieval_service import RetrievalService
from app.services.source_scope import (
    ActiveSourceScope,
    current_base_scope_payload,
    current_source_scope,
    current_source_scope_payload,
    evidence_json_allowed,
    filter_retrieval_items,
    scoped_allowed_source_ids,
    scoped_conversation_history,
    source_allowed,
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


def test_omitted_or_default_exclude_scope_preserves_historical_behavior():
    chunks = [
        RetrievedChunk("c1", "s1", "one", "", "one"),
        RetrievedChunk("c2", "s2", "two", "", "two"),
    ]
    assert filter_retrieval_items("nb", "chunk", chunks) == chunks
    with source_scope_context("nb", SourceScope(mode="exclude", source_ids=[])):
        assert filter_retrieval_items("nb", "chunk", chunks) == chunks


def test_direct_construction_without_narrowed_matches_pre_field_behavior():
    """Fallback-path guarantee for the `narrowed` field itself: a caller that
    constructs `ActiveSourceScope` directly -- bypassing the API boundary
    that computes `source_narrowed`/`base_narrowed` (e.g. a direct
    service-layer call, or any caller that never went through
    `_validate_source_scope`/`_validate_base_scope`) -- must observe EXACTLY
    the old value-driven `restricted`/`base_restricted` judgment, byte-for-
    byte unchanged by this field's introduction. `source_narrowed` /
    `base_narrowed` default to `None` and must never be guessed."""
    # mode="include" always meant restricted, regardless of the id list.
    assert ActiveSourceScope(
        notebook_id="nb", mode="include", source_ids=frozenset(),
    ).restricted is True
    # mode="exclude" + empty ids is the historical "all sources" shape.
    assert ActiveSourceScope(
        notebook_id="nb", mode="exclude", source_ids=frozenset(),
    ).restricted is False
    # mode="exclude" + a non-empty id list still reads as restricted (shape-
    # based judgment predates this field and must be untouched).
    assert ActiveSourceScope(
        notebook_id="nb", mode="exclude", source_ids=frozenset({"s1"}),
    ).restricted is True
    # Same three shapes, mirrored on the base-library dimension.
    assert ActiveSourceScope(
        notebook_id="nb", mode="exclude", source_ids=frozenset(),
        base_mode="include", base_notebook_ids=frozenset(),
    ).base_restricted is True
    assert ActiveSourceScope(
        notebook_id="nb", mode="exclude", source_ids=frozenset(),
        base_mode="exclude", base_notebook_ids=frozenset(),
    ).base_restricted is False
    assert ActiveSourceScope(
        notebook_id="nb", mode="exclude", source_ids=frozenset(),
        base_mode="exclude", base_notebook_ids=frozenset({"b1"}),
    ).base_restricted is True


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


def test_base_only_narrowing_also_drops_prior_conversation_history():
    """P1 fix (codex PR #431 round-2 review): unchecking a reference library
    while local sources stay fully selected must ALSO clear conversation
    history. A prior answer can quote content from ANY participant library,
    so history is a cross-library value -- unlike `source_scope_restricted()`
    itself, which stays local-only by design (R1, see
    `ActiveSourceScope.restricted`'s docstring) and must never be the sole
    gate here."""
    with source_scope_context(
        "nb", None, BaseNotebookScope(mode="include", notebook_ids=["kept-base"]),
    ):
        # R1 still holds: the local dimension is untouched.
        assert source_scope_restricted() is False
        assert scoped_conversation_history("prior answer") == ""
    # Control case: neither dimension restricted leaves history untouched.
    with source_scope_context(
        "nb", SourceScope(mode="exclude", source_ids=[]),
        BaseNotebookScope(mode="exclude", notebook_ids=[]),
    ):
        assert scoped_conversation_history("prior answer") == "prior answer"


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
    def __init__(self, visible: list[str], count: int):
        self.visible = visible
        self.count = count

    def visible_source_ids(self, _notebook_id, source_ids):
        return [source_id for source_id in source_ids if source_id in self.visible]

    def visible_source_count(self, _notebook_id):
        return self.count

    def all_visible_source_ids(self, _notebook_id):
        return list(self.visible)


def _notebook(*, bases=None):
    return NotebookSummary(
        id="nb", name="n", purpose="", primary_domain="", status="ready",
        counts={}, created_label="", base_notebooks=bases or [],
    )


def test_validate_source_scope_no_longer_raises_409_on_its_own():
    # (a) superseded invariant: the D12 "any evidence universe left?" check
    # used to live inside _validate_source_scope, but it must also consider
    # the frozen base-library scope (see _require_ask_available below), which
    # _validate_source_scope cannot see. It is now a pure freeze/422 helper.
    resolved = _validate_source_scope(
        _ScopeRepo([], 0), _notebook(),
        SourceScope(mode="include", source_ids=[]),
    )
    # universe = notebook.counts["sources"] = 0, selected = [] -> not narrowed.
    assert resolved == SourceScope(mode="include", source_ids=[], narrowed=False)


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


def test_exclusion_scope_is_frozen_to_an_explicit_allow_list():
    resolved = _validate_source_scope(
        _ScopeRepo(["s1", "s2", "s3"], 3),
        _notebook(),
        SourceScope(mode="exclude", source_ids=["s2"]),
    )
    # 2 of 3 visible sources selected -> a real narrowing.
    assert resolved == SourceScope(
        mode="include", source_ids=["s1", "s3"], narrowed=True
    )


def test_real_narrowing_of_local_sources_is_restricted():
    """The flip side of the default-full-select fix: a genuine subset
    selection (``narrowed=True``, exactly as ``_validate_source_scope`` would
    freeze it) must still gate PPR/private Memory/community reports/corpus
    profile. Making the full-select case unrestricted must not accidentally
    make EVERY local-source selection unrestricted."""
    with source_scope_context(
        "nb", SourceScope(mode="include", source_ids=["s1"], narrowed=True),
    ):
        assert source_scope_restricted() is True


def test_real_narrowing_of_base_libraries_is_restricted():
    """Base-library mirror of the test above."""
    with source_scope_context(
        "nb", None,
        BaseNotebookScope(mode="include", notebook_ids=["b1"], narrowed=True),
    ):
        scope = current_source_scope()
        assert scope is not None
        assert scope.base_restricted is True


def test_checkbox_ceiling_intersects_model_allow_list_and_leaves_base_alone():
    with source_scope_context(
        "nb", SourceScope(mode="include", source_ids=["s1", "s2"])
    ):
        assert scoped_allowed_source_ids("nb") == ("s1", "s2")
        assert scoped_allowed_source_ids("nb", ["s2", "s3"]) == ("s2",)
        assert scoped_allowed_source_ids("base", ["b1"]) == ("b1",)


# ---------------------------------------------------------------------------
# Base-library scope (library-level checkbox; T1 contract layer only)
# ---------------------------------------------------------------------------


def test_r2_omitted_base_scope_is_byte_identical_to_before_the_field_existed():
    """R2: a caller that never supplies base_scope must observe unchanged
    behavior -- both the two-arg source_scope_context call shape AND explicit
    base_scope=None must agree, and mounted base libraries stay fully open."""
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
    """R1: unchecking a mounted base library while local sources stay fully
    selected must NOT flip source_scope_restricted() -- that flag gates the
    ACTIVE notebook's own PPR/graph expansion and private Memory
    (ask_service._memory_hits returns [] when restricted), which is
    orthogonal to which reference libraries participate.

    `scoped_conversation_history` is DELIBERATELY not pinned to the
    historical "unchanged" value here: it is cross-library (see
    `test_base_only_narrowing_also_drops_prior_conversation_history`) and, as
    of the codex #431 round-2 P1 fix, clearing it is exactly the intended
    behavior for this same base-only-narrowed context -- conflating that with
    `restricted` staying False would defeat the point of this test."""
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


def test_collapsed_paths_agree_on_an_excluded_base_library():
    """Collapsing-point consistency: allows()/source_allowed(),
    scoped_allowed_source_ids(), and evidence_json_allowed() must all treat
    an excluded mounted base library the same way (this is exactly the
    "three independent duplicated checks" the source_scope module docstring
    warns about drifting apart)."""
    base_scope = BaseNotebookScope(mode="include", notebook_ids=["kept-base"])
    with source_scope_context("nb", None, base_scope):
        # 1) ActiveSourceScope.allows() via source_allowed()/filter_evidence()
        assert source_allowed("excluded-base", "s1") is False
        assert source_allowed("kept-base", "s1") is True
        # 2) scoped_allowed_source_ids() -- must be an explicit empty tuple,
        #    never None (R4: None means "unrestricted" to `is not None` callers).
        excluded_ids = scoped_allowed_source_ids("excluded-base", ["s1"])
        assert excluded_ids == ()
        assert excluded_ids is not None
        assert scoped_allowed_source_ids("kept-base", ["s1"]) == ("s1",)
        # 3) evidence_json_allowed()
        assert evidence_json_allowed("excluded-base", [{"source_id": "s1"}]) is False
        assert evidence_json_allowed("kept-base", [{"source_id": "s1"}]) is True


def test_base_scope_default_include_empty_means_no_mounted_base_participates():
    with source_scope_context(
        "nb", None, BaseNotebookScope(mode="include", notebook_ids=[]),
    ):
        assert source_allowed("any-base", "s1") is False
        assert scoped_allowed_source_ids("any-base", ["s1"]) == ()


def _notebook_ref(nb_id: str) -> NotebookRef:
    return NotebookRef(id=nb_id, name=nb_id)


def test_validate_base_scope_freezes_exclude_to_include():
    notebook = _notebook(bases=[_notebook_ref("b1"), _notebook_ref("b2")])
    resolved = _validate_base_scope(
        notebook, BaseNotebookScope(mode="exclude", notebook_ids=["b2"])
    )
    # 1 of 2 mounted libraries selected -> a real narrowing.
    assert resolved == BaseNotebookScope(
        mode="include", notebook_ids=["b1"], narrowed=True
    )


def test_validate_base_scope_omitted_returns_none():
    notebook = _notebook(bases=[_notebook_ref("b1")])
    assert _validate_base_scope(notebook, None) is None


def test_validate_base_scope_exclude_nothing_freezes_every_mounted_library():
    """codex #431 R4 (P1, reverting R3): ``exclude`` + an EMPTY list means
    "exclude nothing" -- every mounted library participates -- which reads
    the same as omitting ``base_scope`` for a *synchronous* Ask/preview
    request. But it must NOT be resolved to ``None``: for a report, this
    return value is what gets persisted and re-applied, unchanged, at
    confirmation and generation, phases that can run long after this call.
    ``None`` at create time would be re-interpreted later against the
    notebook's mounts AT THAT LATER MOMENT, letting a library mounted after
    report creation silently join a run the user never scoped it into (see
    ``test_report_base_scope_freezes_mount_set_at_create_time`` in
    ``test_report_api.py``).

    R3 short-circuited this to ``None`` specifically to dodge overflowing
    ``BaseNotebookScope.notebook_ids``'s old ``max_length=1000`` cap -- see
    the scale regression right below this test -- but that overflow is now
    fixed at the cap (raised to 10,000) instead, so the function is free to
    do the semantically correct thing: expand and freeze."""
    notebook = _notebook(bases=[_notebook_ref("b1"), _notebook_ref("b2")])
    resolved = _validate_base_scope(
        notebook, BaseNotebookScope(mode="exclude", notebook_ids=[])
    )
    # "exclude nothing" selects every mounted library -> not narrowed.
    assert resolved == BaseNotebookScope(
        mode="include", notebook_ids=["b1", "b2"], narrowed=False
    )


def test_validate_base_scope_exclude_nothing_survives_beyond_the_1000_mount_cap():
    """Regression for the actual production crash (codex #431 R3): a notebook
    with MORE mounted reference libraries than ``BaseNotebookScope
    .notebook_ids``'s OLD ``max_length=1000`` cap must not raise when the
    browser's default "select all" checkbox state (``exclude`` + ``[]``) is
    validated. The mount API imposes no limit on how many libraries a
    notebook may mount, so this is reachable in production.

    The fix (codex #431 R4) is NOT to dodge the expansion -- freezing to
    ``None`` broke report scope persistence across create/confirm/generate,
    see the test above -- but to raise the cap itself to 10,000, a soft
    ceiling against pathological requests rather than a supported mount-scale
    limit. This asserts the crash is gone AND that the expansion actually
    happened: validation must complete and return the full 1200-id ``include``
    snapshot, not merely "not crash with some other value"."""
    bases = [_notebook_ref(f"b{i}") for i in range(1200)]
    notebook = _notebook(bases=bases)
    resolved = _validate_base_scope(
        notebook, BaseNotebookScope(mode="exclude", notebook_ids=[])
    )
    assert resolved is not None
    assert resolved.mode == "include"
    assert resolved.notebook_ids == [f"b{i}" for i in range(1200)]


def test_validate_base_scope_rejects_ids_not_mounted_on_this_notebook():
    notebook = _notebook(bases=[_notebook_ref("b1")])
    with pytest.raises(HTTPException) as exc:
        _validate_base_scope(
            notebook, BaseNotebookScope(mode="include", notebook_ids=["not-mounted"])
        )
    assert exc.value.status_code == 422


def test_all_bases_excluded_and_no_local_sources_is_409():
    notebook = _notebook(bases=[_notebook_ref("b1")])
    with pytest.raises(HTTPException) as exc:
        _require_ask_available(
            notebook, _ScopeRepo([], 0),
            SourceScope(mode="include", source_ids=[]),
            BaseNotebookScope(mode="include", notebook_ids=[]),
        )
    assert exc.value.status_code == 409


def test_base_only_submission_on_a_library_only_notebook_is_409():
    """D12 的漏判:请求**只**提交库维度、省略 source_scope。

    「没提交本地范围」曾被当成「本地非空」,于是一个零本地来源、靠挂载参考库
    才 ask_available 的笔记本,把每个参考库都取消勾选后仍然放行:Ask 白跑一轮
    零证据,报告更糟——落一行 + 照常调意图模型,而这个函数存在的理由正是拦住
    这笔开销。省略的那一维必须按**真实可见来源宇宙**判空(counts["sources"],
    与 all_visible_source_ids 同一谓词、且随 summary 一起来,不多付往返)。
    """
    notebook = _notebook(bases=[_notebook_ref("b1")])
    assert notebook.counts.get("sources", 0) == 0       # 零本地来源
    with pytest.raises(HTTPException) as exc:
        _require_ask_available(
            notebook, _ScopeRepo([], 0),
            None,                                        # ← 刻意省略本地维度
            BaseNotebookScope(mode="include", notebook_ids=[]),
        )
    assert exc.value.status_code == 409


def test_base_only_submission_with_local_sources_present_is_not_409():
    """同样只提交库维度,但本地确有来源 —— 必须放行,否则整条特性把「只想少借
    一个参考库」的用户挡在门外。"""
    notebook = NotebookSummary(
        id="nb", name="n", purpose="", primary_domain="", status="ready",
        counts={"sources": 3}, created_label="",
        base_notebooks=[_notebook_ref("b1")],
    )
    resolved_source, resolved_base = _require_ask_available(
        notebook, _ScopeRepo(["s1", "s2", "s3"], 3),
        None,
        BaseNotebookScope(mode="include", notebook_ids=[]),
    )
    assert resolved_source is None
    # Explicit include:[] selects 0 of the 1 mounted library -> a real
    # narrowing (unlike exclude:[], which means "exclude nothing").
    assert resolved_base == BaseNotebookScope(
        mode="include", notebook_ids=[], narrowed=True
    )


def test_scoping_neither_dimension_is_never_rejected_for_emptiness():
    """R2:两维都没提交的请求不是一次「选择」,ask_available 才是权威。

    它看得见 Knowhow 格子、confirmed Memory 和参考库的 KG——这个函数一样都
    看不见。按 counts["sources"] 判空会把这些库的历史无范围调用直接判死。
    """
    _require_ask_available(_notebook(), _ScopeRepo([], 0), None, None)
    _require_ask_available(
        _notebook(bases=[_notebook_ref("b1")]), _ScopeRepo([], 0), None, None
    )


def test_deselecting_only_bases_with_local_sources_present_is_not_409():
    notebook = _notebook(bases=[_notebook_ref("b1")])
    resolved_source, resolved_base = _require_ask_available(
        notebook, _ScopeRepo(["s1"], 1),
        SourceScope(mode="include", source_ids=["s1"]),
        BaseNotebookScope(mode="include", notebook_ids=[]),
    )
    # notebook.counts["sources"] is unset (0) on this fixture, so 1 selected
    # id never reads as "less than the universe" -- not narrowed.
    assert resolved_source == SourceScope(
        mode="include", source_ids=["s1"], narrowed=False
    )
    # Explicit include:[] selects 0 of the 1 mounted library -> a real
    # narrowing (unlike exclude:[], which means "exclude nothing").
    assert resolved_base == BaseNotebookScope(
        mode="include", notebook_ids=[], narrowed=True
    )


def test_base_only_scope_still_filters_candidates_at_the_result_boundary():
    """`filter_retrieval_items` must NOT short-circuit on a base-only run.

    A run that unchecks only reference libraries leaves the LOCAL `restricted`
    flag False by design (R1), so the short-circuit has to test both
    dimensions.  Mutating it back to `not scope.restricted` makes this test
    keep every excluded-library candidate.
    """
    chunks = [
        RetrievedChunk("c-local", "s1", "local", "", "local"),
        RetrievedChunk("c-kept", "bs", "kept", "", "kept", notebook_id="kept-base"),
        RetrievedChunk("c-gone", "bs", "gone", "", "gone", notebook_id="dropped-base"),
    ]
    with source_scope_context(
        "nb", None, BaseNotebookScope(mode="include", notebook_ids=["kept-base"]),
    ):
        assert source_scope_restricted() is False       # R1 still holds
        kept = [row.chunk_id for row in filter_retrieval_items("nb", "chunk", chunks)]
    assert kept == ["c-local", "c-kept"]


def test_covers_notebook_exclude_branch_drops_only_the_listed_libraries():
    """Direct coverage of `covers_notebook`'s exclude arm (every scope built
    through the API is frozen to include, so it is otherwise unreached and a
    `return True` mutation there survives)."""
    scope = ActiveSourceScope(
        notebook_id="nb", mode="exclude", source_ids=frozenset(),
        base_mode="exclude", base_notebook_ids=frozenset({"dropped-base"}),
    )
    assert scope.covers_notebook("dropped-base") is False
    assert scope.covers_notebook("other-base") is True
    assert scope.covers_notebook("nb") is True          # active notebook, always
    assert scope.covers_notebook("") is True            # "this run's notebook"
    # ...and the same answer must reach every consumer of the collapsing point.
    assert scope.allows("dropped-base", "s1") is False
    assert scope.allows("other-base", "s1") is True


def test_excluded_library_knowledge_and_relations_are_dropped_not_stripped():
    """An unchecked library's KG node must be REMOVED, not just emptied of
    evidence: evidence_context.knowledge_context() never reads hit.evidence --
    it re-queries node_context(origin, object_id) and assigns the hit its own
    `k{n}` anchor, so a kept-but-empty node still enters the answer prompt and
    stays citable, merely untraceable."""
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
    """R2 within a base-only run: the local dimension was never narrowed, so
    nothing local was filtered and an active-notebook node that simply carries
    no evidence must survive -- exactly as it did before this loop could be
    entered at all."""
    empty = RetrievedKnowledge(
        object_id="ko-active", object_type="claim", payload={"name": "x"},
        evidence=[], notebook_id="nb",
    )
    with source_scope_context(
        "nb", None, BaseNotebookScope(mode="include", notebook_ids=["kept-base"]),
    ):
        assert len(filter_retrieval_items("nb", "knowledge", [empty])) == 1
    # But a LOCAL narrowing still drops it -- that branch is unchanged.
    with source_scope_context("nb", SourceScope(mode="include", source_ids=["s1"])):
        assert filter_retrieval_items("nb", "knowledge", [empty]) == []


def test_base_only_scope_never_fabricates_a_local_scope_payload():
    """`current_source_scope_payload()` is re-persisted into a report's
    understanding contract and re-frozen on confirm.  Returning a synthetic
    `exclude:[]` for a base-only run would be frozen into
    `include:[every visible source]`, whose `restricted` is True -- silently
    disabling private Memory, PPR, community reports and the corpus profile for
    a user who only unchecked a reference library (R1 across persistence)."""
    # All scopes below are constructed directly, bypassing
    # _validate_source_scope/_validate_base_scope -- narrowed is therefore
    # always None (not computed), never a guessed True/False.
    with source_scope_context(
        "nb", None, BaseNotebookScope(mode="include", notebook_ids=["b1"]),
    ):
        assert current_source_scope_payload() is None
        assert current_base_scope_payload() == {
            "mode": "include", "notebook_ids": ["b1"], "narrowed": None
        }
    # Symmetric: a local-only run must not fabricate a base payload either --
    # persisting one would freeze the libraries mounted at that instant and
    # lock out any reference library mounted later.
    with source_scope_context("nb", SourceScope(mode="include", source_ids=["s1"])):
        assert current_base_scope_payload() is None
        assert current_source_scope_payload() == {
            "mode": "include", "source_ids": ["s1"], "narrowed": None
        }
    # Both supplied -> both payloads round-trip.
    with source_scope_context(
        "nb", SourceScope(mode="include", source_ids=["s1"]),
        BaseNotebookScope(mode="exclude", notebook_ids=["b2"]),
    ):
        assert current_source_scope_payload() == {
            "mode": "include", "source_ids": ["s1"], "narrowed": None
        }
        assert current_base_scope_payload() == {
            "mode": "exclude", "notebook_ids": ["b2"], "narrowed": None
        }


def test_reasoning_preflight_uses_the_submitted_checkbox_ceiling():
    class _Repo:
        def validate_reasoning_submission(self, notebook_id, _payload):
            if not source_allowed(notebook_id, "s2"):
                raise ValueError("指定来源超出当前勾选范围")

    payload = AskRequest(
        question="q",
        mode="reasoning",
        source_scope=SourceScope(mode="include", source_ids=["s1"]),
    )
    with pytest.raises(HTTPException, match="当前勾选范围") as exc:
        _validate_reasoning_scope_preflight(
            _Repo(), "nb", SimpleNamespace(id="reasoning"), payload
        )
    assert exc.value.status_code == 409


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


# ---------------------------------------------------------------------------
# T2 -- the library checkbox in the RETRIEVAL CHAIN (not the pure helpers).
#
# These run against a real SQLiteRepository with a real mounted reference
# library, and exercise the actual federated producers rather than doubles:
# every candidate type a mounted library can contribute has one case here
# (knowledge object / relation / element / chunk), plus the shape of the
# production incident that motivated the feature.
# ---------------------------------------------------------------------------

_MOE = "Mixture-of-Experts"
_QUERY = "Mixture-of-Experts routing"


def _seed_library(repo, notebook_id: str, prefix: str, *, name: str = _MOE) -> str:
    """One parsed source (2 elements -> real chunks) + two concepts joined by a
    relation, all evidence-bound to those elements.

    `name` is shared across libraries on purpose: rebuild_unified_kg derives
    concept_clusters' canonical_id from it, which is what lets PPR bridge a
    chunk from the mounted library into an active-notebook question.
    """
    from app.services.sqlite_repository import _now

    source_id = f"src-{prefix}"
    element_ids = [f"el-{prefix}-{index}" for index in (1, 2)]
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,"
            "parse_status,created_at,updated_at) "
            "VALUES (?,?,?,'markdown','ready',?,'',0,?,'','','parsed',?,?)",
            (source_id, notebook_id, f"Doc {prefix}", f"{prefix}.md",
             f"hash-{prefix}", now, now),
        )
        for index, element_id in enumerate(element_ids, 1):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,metadata,created_at) "
                "VALUES (?,?,'paragraph',?,?,'{}',?)",
                (element_id, source_id, f"p{index}",
                 f"{name} routing is described in document {prefix} part {index}.",
                 now),
            )
    repo._chunk_and_embed_source(source_id)

    def _evidence(element_id: str) -> list[dict]:
        return [{
            "source_id": source_id, "source_title": f"Doc {prefix}",
            "element_id": element_id, "element_type": "paragraph",
            "location_label": "p1", "quoted_span": f"{name} routing",
            "confidence": 1.0,
        }]

    repo.store_kg(notebook_id, source_id, [
        {"local_id": f"{prefix}-A", "object_type": "concept",
         "payload": {"name": name, "section_path": "1"},
         "evidence": _evidence(element_ids[0])},
        {"local_id": f"{prefix}-B", "object_type": "concept",
         "payload": {"name": f"{name} routing gate", "section_path": "1"},
         "evidence": _evidence(element_ids[1])},
    ], [
        {"source_local_id": f"{prefix}-A", "target_local_id": f"{prefix}-B",
         "edge_type": "kind_of", "evidence": _evidence(element_ids[0])},
    ])
    repo.rebuild_unified_kg(notebook_id)
    return source_id


@pytest.fixture
def federated_corpus(tmp_path, monkeypatch):
    """(repo, active_id, base_id, local_source_id, base_source_ids)."""
    from app.core.config import Settings
    from app.models.schemas import NotebookCreate
    from app.services.embedding import FakeEmbedder
    from app.services.sqlite_repository import SQLiteRepository
    from tests.model_testkit import bind_all_embedding_clients

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(repo, FakeEmbedder(dim=16))

    base = repo.create_notebook(NotebookCreate(name="reference library"))
    repo.mark_notebook_base(base.id)
    active = repo.create_notebook(NotebookCreate(name="my notebook"))
    # The incident shape: one local document, a mounted library holding more.
    base_sources = [_seed_library(repo, base.id, f"base{n}") for n in (1, 2, 3)]
    local_source = _seed_library(repo, active.id, "local")
    repo.replace_notebook_bases(active.id, [base.id], "user-local")
    return repo, active.id, base.id, local_source, base_sources


def _base_excluded(active_id: str):
    """The library checkbox as the API freezes it: include-nothing."""
    return source_scope_context(
        active_id, None, BaseNotebookScope(mode="include", notebook_ids=[])
    )


def test_chain_knowledge_objects_from_an_unchecked_library_are_gone(
    federated_corpus,
):
    repo, active, base, _local_source, _base_sources = federated_corpus

    unscoped = repo.retrieval.federated_retrieve(active, _QUERY)
    assert {hit.notebook_id for hit in unscoped} == {active, base}, (
        "baseline: the mounted library must be reachable at all, otherwise the "
        "scoped assertion below proves nothing"
    )

    with _base_excluded(active):
        scoped = repo.retrieval.federated_retrieve(active, _QUERY)
    assert scoped, "unchecking a reference library must not empty the local hits"
    assert {hit.notebook_id for hit in scoped} == {active}


def test_chain_relations_from_an_unchecked_library_are_gone(federated_corpus):
    repo, active, base, _local_source, _base_sources = federated_corpus

    unscoped = repo.retrieval.federated_retrieve_relations(active, _QUERY)
    assert {hit.notebook_id for hit in unscoped} == {active, base}

    with _base_excluded(active):
        scoped = repo.retrieval.federated_retrieve_relations(active, _QUERY)
    assert scoped
    assert {hit.notebook_id for hit in scoped} == {active}


def test_chain_elements_from_an_unchecked_library_are_gone(federated_corpus):
    """`RetrievedElement` carries no notebook_id, so `filter_retrieval_items`
    can only ever judge one against the ACTIVE notebook -- the per-library skip
    is what keeps the origin known.

    What this asserts is the OUTCOME, and the outcome survives deleting that
    skip: the inner `_retrieve_elements` intersects the same per-notebook
    allow-list and comes back empty either way. The skip is a cost guard, and
    the test that actually pins it is
    test_chain_never_queries_an_unchecked_library (query probes) -- not this
    one.
    """
    repo, active, base, local_source, base_sources = federated_corpus
    keys = [(active, local_source)] + [(base, sid) for sid in base_sources]

    unscoped = repo.retrieval.federated_retrieve_elements(
        active, _QUERY, allowed_source_keys=keys, limit=8,
    )
    assert {item.source_id for item in unscoped} & set(base_sources)

    with _base_excluded(active):
        scoped = repo.retrieval.federated_retrieve_elements(
            active, _QUERY, allowed_source_keys=keys, limit=8,
        )
    assert scoped
    assert {item.source_id for item in scoped} == {local_source}


def test_chain_chunks_from_an_unchecked_library_are_gone(federated_corpus):
    repo, active, base, local_source, base_sources = federated_corpus

    unscoped = repo.retrieval.ppr_retrieve(active, _QUERY)
    assert {chunk.source_id for chunk in unscoped} & set(base_sources), (
        "baseline: PPR must actually bridge a mounted-library chunk in"
    )

    with _base_excluded(active):
        scoped = repo.retrieval.ppr_retrieve(active, _QUERY)
    # `<=` (subset) would also pass on an empty result -- PPR over-filtering
    # to nothing is a different bug than a library leak, and this assertion
    # must not paper over it. Narrowing genuinely returns ["src-local"] here.
    assert {chunk.source_id for chunk in scoped} == {local_source}


def test_chain_never_queries_an_unchecked_library(federated_corpus, monkeypatch):
    """Efficiency half of the contract: the excluded library is not searched
    and then discarded -- it is never searched."""
    repo, active, base, local_source, base_sources = federated_corpus
    candidates = repo.retrieval.candidates
    seen: dict[str, list[str]] = {}

    for method in ("_retrieve_scored", "_retrieve_relations_scored",
                   "_retrieve_elements"):
        original = getattr(candidates, method)
        seen[method] = []

        def _spy(notebook_id, *args, _m=method, _o=original, **kwargs):
            seen[_m].append(notebook_id)
            return _o(notebook_id, *args, **kwargs)

        monkeypatch.setattr(candidates, method, _spy)

    keys = [(active, local_source)] + [(base, sid) for sid in base_sources]
    with _base_excluded(active):
        repo.retrieval.federated_retrieve(active, _QUERY)
        repo.retrieval.federated_retrieve_relations(active, _QUERY)
        repo.retrieval.federated_retrieve_elements(
            active, _QUERY, allowed_source_keys=keys, limit=8,
        )

    for method, notebook_ids in seen.items():
        assert notebook_ids, f"{method} was never reached -- the spy is blind"
        assert base not in notebook_ids, (
            f"{method} still queried the unchecked library {base}: {notebook_ids}"
        )


def test_incident_shape_local_document_is_not_drowned_by_the_mounted_library(
    federated_corpus,
):
    """The production incident: a question scoped to the user's own document
    came back with every citation from the mounted 84-paper library.  With the
    library unchecked, no candidate of any type may carry one of its sources.
    """
    repo, active, base, local_source, base_sources = federated_corpus
    keys = [(active, local_source)] + [(base, sid) for sid in base_sources]

    unscoped_sources = {
        evidence.source_id
        for hit in repo.retrieval.federated_retrieve(active, _QUERY)
        for evidence in hit.evidence
    }
    assert unscoped_sources & set(base_sources), (
        "baseline: without the checkbox the mounted library does reach the answer"
    )

    with _base_excluded(active):
        surfaced = {
            evidence.source_id
            for hit in repo.retrieval.federated_retrieve(active, _QUERY)
            for evidence in hit.evidence
        }
        surfaced |= {
            evidence.source_id
            for hit in repo.retrieval.federated_retrieve_relations(active, _QUERY)
            for evidence in hit.evidence
        }
        surfaced |= {
            item.source_id
            for item in repo.retrieval.federated_retrieve_elements(
                active, _QUERY, allowed_source_keys=keys, limit=8,
            )
        }
        surfaced |= {
            chunk.source_id for chunk in repo.retrieval.ppr_retrieve(active, _QUERY)
        }

    assert surfaced == {local_source}, (
        f"mounted-library sources leaked into the answer: "
        f"{sorted(surfaced - {local_source})}"
    )


def test_ask_payload_base_scope_alone_narrows_without_manual_context(federated_corpus):
    """Every other scoped case in this file enters ``source_scope_context``
    itself. The ONE real seam that turns a submitted ``AskRequest.base_scope``
    into that context for Ask is ``AskService.ask()``'s
    ``with source_scope_context(notebook_id, ..., getattr(payload,
    "base_scope", None)):`` -- a mutation that drops that second argument
    (replacing it with ``None``) makes the whole feature a no-op for Ask
    while every ContextVar-driven test above stays green. This test relies
    on the payload alone, exactly like a real HTTP request would, to catch
    that regression.
    """
    import json
    import re

    from tests.model_testkit import bind_chat_client, bind_rerank_client

    class _IdentityReranker:
        configured = True

        def rerank(self, query, documents, on_error=None):
            return list(range(len(documents)))

    class _MirrorLLM:
        """Cites back every `kN` evidence key the real prompt actually
        offered, so anchor-binding (which gates mix-mode citations) never
        hides which library the retrieval boundary let through."""
        configured = True
        model = "test"

        def chat_json(self, messages, schema_hint, **kwargs):
            prompt = messages[0]["content"] if messages else ""
            keys = sorted(
                {int(m) for m in re.findall(r"\bk(\d+):", prompt)}
            )
            markers = " ".join(f"[k{k}]" for k in keys)
            return json.dumps({"answer": f"summary. {markers}", "grounded": True})

    repo, active, base, local_source, base_sources = federated_corpus
    repo.settings.query_rewrite_enabled = False
    # chunk mode only crosses into a mounted library through the "mix"
    # overlay's PPR path (kg_overlay_enabled AND a configured reranker AND a
    # KG on either side) -- without it, `_retrieve_chunks` is single-notebook
    # by construction and this test could not prove anything either way. Mix
    # citations are also gated on the LLM actually citing the evidence key
    # (`anchors`), so the fake model must echo back every key it was offered.
    bind_rerank_client(repo, _IdentityReranker())
    bind_chat_client(repo, "ask_answer", _MirrorLLM())

    baseline = repo.ask(active, AskRequest(question=_QUERY, mode="chunk"))
    baseline_sources = {c.source_id for c in baseline.citations}
    assert baseline_sources & set(base_sources), (
        "baseline: without narrowing, the mounted library must reach the "
        "chunk-mode answer -- otherwise this test cannot prove anything"
    )

    narrowed = repo.ask(active, AskRequest(
        question=_QUERY, mode="chunk",
        base_scope=BaseNotebookScope(mode="include", notebook_ids=[]),
    ))
    narrowed_sources = {c.source_id for c in narrowed.citations}
    assert narrowed_sources, "local evidence must still come back"
    assert narrowed_sources == {local_source}


def test_follow_chain_drops_hops_carried_by_an_unchecked_library():
    """`follow_chain`'s cross-notebook arm keeps a hop's evidence verbatim --
    correct for a library that is still checked, a leak for one that is not.
    The gate also cannot be `source_scope_restricted()`: a library-only run
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

    assert [chain.target_object_id for chain in result.inferences] == ["c-k"]
    assert all(
        hop.notebook_id != "dropped-base"
        for inference in result.inferences for hop in inference.hops
    )


def test_node_context_drops_a_row_from_an_unchecked_library():
    """`retrieval.node_context` is reasoning's query-time chain hydration
    reader -- and ONLY that.  It is deliberately not the gate for
    `knowledge_context()`, which is wired to the graph service directly; see
    test_knowledge_context_gates_the_library_at_the_assembly_point."""
    class _Graph:
        def node_context(self, notebook_id, _object_id):
            return {"notebook_id": notebook_id, "definition": f"text from {notebook_id}",
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


def test_knowledge_context_gates_the_library_at_the_assembly_point(federated_corpus):
    """`knowledge_context()` is where a KG hit BECOMES a prompt line and a live
    `k{n}` anchor, so that is where the library ceiling has to sit.

    Gating its `node_context()` re-read instead is not enough and this is the
    case that proves it: `node_context` supplies only the definition/snippet,
    while the object's NAME comes off the hit itself -- an emptied row still
    renders `k1: [concept][base] <library's concept name>` and still resolves as
    a citation.

    Run against the REAL composition (`EvidenceContextService` is constructed
    with `knowledge=GraphRetrievalService`, not with this service): a double
    would prove a gate works while proving nothing about whether this path
    reaches it.
    """
    repo, active, base, _local_source, _base_sources = federated_corpus
    library_hits = [
        hit for hit in repo.retrieval.federated_retrieve(active, _QUERY)
        if hit.notebook_id == base
    ]
    assert library_hits, "baseline: the mounted library must produce a hit at all"
    names = {str(hit.payload.get("name") or "") for hit in library_hits}
    assert all(names), "baseline: the library's hits must carry renderable names"

    block, id_map = repo._answer_context(active, library_hits)
    assert id_map and any(name in block for name in names), (
        "baseline: unscoped, the library's node does render into the prompt"
    )

    with _base_excluded(active):
        block, id_map = repo._answer_context(active, library_hits)
    assert not id_map, f"unchecked library became citable: {id_map}"
    # "(none)" is knowledge_context's own empty-context sentinel.
    assert not [name for name in names if name in block], (
        f"unchecked library reached the prompt: {block!r}"
    )


def test_chunk_overlay_graph_walk_drops_nodes_from_an_unchecked_library():
    """A library-only run keeps the whole-graph walk (unlike a locally
    narrowed one), and the federated graph is process-cached under a
    scope-blind key -- so the ceiling has to reach the walk's RESULT."""
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


def test_ask_graph_walk_never_renders_a_node_from_an_unchecked_library(
    federated_corpus, monkeypatch,
):
    """graph 模式跑的是整张联邦图的 BFS —— 库维度暴露面最大的漫游出口。

    断言落在 ``render_subgraph_context`` 的**入参**上,而不是落在响应或某个更
    下游的读取上,因为 render 就是子图变成「答案 prompt 的一段 + 一批活的
    `k{n}` 锚点」的那一步:任何在它之后才做的过滤都已经晚了。每一次 render
    调用都查(不只第一次)——mix 分支会用同一份 subgraph 再渲染一遍。

    PPR 先关掉:它命中就会带着 chunk 答案提前 return,整个全图漫游分支根本不
    执行(实测如此),留着它这条用例会在不触发目标代码的情况下变绿。
    """
    from app.models.schemas import AskRequest
    from app.services.kg import graph_reason

    repo, active, base, _local_source, _base_sources = federated_corpus
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False, raising=False)

    rendered: list[list[dict]] = []
    id_maps: list[dict] = []
    original = graph_reason.render_subgraph_context

    def _spy(subgraph, **kwargs):
        rendered.append([dict(node) for node, _edge, _src in subgraph])
        block, id_map = original(subgraph, **kwargs)
        id_maps.append(id_map)
        return block, id_map

    monkeypatch.setattr(graph_reason, "render_subgraph_context", _spy)

    repo.ask(active, AskRequest(question=_QUERY, mode="graph"))
    assert rendered, "the whole-graph walk never ran -- the spy is blind"
    assert any(
        node.get("notebook_id") == base for call in rendered for node in call
    ), "baseline: the walk must actually reach the mounted library's nodes"

    rendered.clear()
    id_maps.clear()
    with _base_excluded(active):
        repo.ask(active, AskRequest(question=_QUERY, mode="graph"))

    assert rendered, "the whole-graph walk never ran under the library scope"
    assert any(call for call in rendered), (
        "the walk came back empty -- unchecking a library must not empty the graph"
    )
    for call in rendered:
        assert {node.get("notebook_id") for node in call} <= {active}, (
            f"an unchecked library's node reached the prompt: "
            f"{[n for n in call if n.get('notebook_id') != active]}"
        )
    for id_map in id_maps:
        assert all(row.get("notebook_id") != base for row in id_map.values()), (
            f"an unchecked library's node became citable: {id_map}"
        )


def test_comparison_peers_are_not_borrowed_from_an_unchecked_library():
    """横向对比(共提/社区)按库枚举取「兄弟实体名」,那些名字是从库内容里读
    出来的:它们进可见轨迹、进 used_queries、并被回喂进 reflect prompt。

    结果侧的过滤救不了这条通道 —— 泄漏的是**查询词本身**,不是命中。这里同时
    覆盖 reasoning 的 expand_community 与 chunk 模式的对比子查询:两者共用
    CommunityQueryService.mounted_base_ids 这一个库枚举入口。
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
        assert source_scope_restricted() is False       # R1 still holds
        assert service.mounted_base_ids("nb") == ["kept-base"]

    with source_scope_context(
        "nb", None, BaseNotebookScope(mode="include", notebook_ids=[]),
    ):
        assert service.mounted_base_ids("nb") == []


# ---------------------------------------------------------------------------
# T3: read-only "what this run actually searched" receipt on the answer
# ---------------------------------------------------------------------------


def _receipt_notebook(*, sources: int = 0, bases=None) -> NotebookSummary:
    return NotebookSummary(
        id="nb", name="n", purpose="", primary_domain="", status="ready",
        counts={"sources": sources}, created_label="",
        base_notebooks=bases or [],
    )


def test_receipt_is_absent_when_the_request_scoped_neither_dimension():
    """Requirement 5 (omitted means absent): the incident's fix must not
    manufacture a receipt for a run that made no selection.  Also R2 -- with no
    receipt the serialized answer is byte-identical to every payload written
    before this field existed."""
    from app.api.ask_routes import _scope_receipt
    from app.models.ask import AskResponse

    notebook = _receipt_notebook(
        sources=3, bases=[NotebookRef(id="b1", name="论文库")]
    )
    assert _scope_receipt(notebook, None, None) is None
    assert "retrieval_scope" not in AskResponse(conclusion="c").model_dump()


def test_receipt_reports_the_untouched_dimension_as_whole():
    """One scoped dimension still reports the other, truthfully.  That pairing
    IS the incident: a single checked local source next to a fully
    participating reference library."""
    from app.api.ask_routes import _scope_receipt

    notebook = _receipt_notebook(
        sources=3, bases=[NotebookRef(id="b1", name="论文库")]
    )
    local_only = _scope_receipt(
        notebook, SourceScope(mode="include", source_ids=["s1"]), None
    )
    assert local_only.local.selected == 1 and local_only.local.total == 3
    assert [(b.notebook_id, b.name, b.included) for b in local_only.bases] == [
        ("b1", "论文库", True)
    ]

    base_only = _scope_receipt(
        notebook, None, BaseNotebookScope(mode="include", notebook_ids=[])
    )
    # An unscoped local dimension searched every visible source: selected ==
    # total says "all of them", never a narrowing the user did not request.
    assert base_only.local.selected == 3 and base_only.local.total == 3
    assert [b.included for b in base_only.bases] == [False]


def test_receipt_marks_each_mounted_library_included_or_not():
    from app.api.ask_routes import _scope_receipt

    notebook = _receipt_notebook(sources=2, bases=[
        NotebookRef(id="b1", name="论文库"),
        NotebookRef(id="b2", name="手册库"),
    ])
    receipt = _scope_receipt(
        notebook,
        SourceScope(mode="include", source_ids=["s1"]),
        BaseNotebookScope(mode="include", notebook_ids=["b2"]),
    )
    assert [(b.name, b.included) for b in receipt.bases] == [
        ("论文库", False), ("手册库", True),
    ]


def test_receipt_snapshots_library_names_so_replay_survives_unmounting():
    """Requirement 1: the persisted receipt must not depend on the notebook's
    CURRENT mounts.  Re-deriving names at read time would drop precisely the
    library that explains a past answer once it is unmounted."""
    from app.api.ask_routes import _scope_receipt
    from app.models.ask import AskResponse

    receipt = _scope_receipt(
        _receipt_notebook(sources=1, bases=[NotebookRef(id="b1", name="论文库")]),
        SourceScope(mode="include", source_ids=["s1"]),
        BaseNotebookScope(mode="include", notebook_ids=["b1"]),
    )
    stored = AskResponse(conclusion="c", retrieval_scope=receipt).model_dump()
    # The library is unmounted afterwards; the payload still names it.
    assert _receipt_notebook(sources=1).base_notebooks == []
    replayed = AskResponse(**stored)
    assert replayed.retrieval_scope.bases[0].name == "论文库"
    assert replayed.retrieval_scope.bases[0].included is True


def test_legacy_answer_payload_without_the_receipt_still_loads():
    """Requirement 2: historical answers have no such key."""
    from app.models.ask import AskResponse

    legacy = {"conclusion": "c", "answer": "a", "grounded": True}
    assert AskResponse(**legacy).retrieval_scope is None


def test_receipt_exposes_only_display_safe_fields():
    """Requirement 4: the disclosure surface is names and counts.  Pinned as a
    field set so a later edit cannot quietly add a file path or an error
    string to a payload that is persisted and replayed."""
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

    service = SimpleNamespace(
        _needs_index=lambda _nb: False, ask_state=_State()
    )
    receipt = RetrievalScopeReceipt(
        local=RetrievalScopeLocalReceipt(selected=1, total=84),
        bases=[RetrievalScopeBaseReceipt(
            notebook_id="b1", name="论文库", included=False
        )],
    )

    response = AskResponse(conclusion="c")
    with retrieval_scope_receipt_context(receipt):
        AskService._save_answer(
            service, "nb", "q", response, None, user_id="u",
        )
    assert response.retrieval_scope == receipt
    assert saved[-1]["retrieval_scope"]["bases"][0]["name"] == "论文库"

    unscoped = AskResponse(conclusion="c")
    AskService._save_answer(service, "nb", "q", unscoped, None, user_id="u")
    assert unscoped.retrieval_scope is None
    assert "retrieval_scope" not in saved[-1]


def test_receipt_context_is_display_only_and_read_at_exactly_one_seam():
    """Requirement 3: the receipt must never become an input to a gate.  A
    docstring cannot enforce that, so pin the reader/writer sets -- any new
    retrieval owner that starts consulting it reports here."""
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
    from app.services.source_scope import (
        current_source_scope,
        retrieval_scope_receipt_context,
    )

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
        assert filter_retrieval_items("nb", "chunk", chunks) == chunks
        assert scoped_allowed_source_ids("nb") is None
        assert source_allowed("b1", "s9") is True


def test_sync_ask_route_carries_the_receipt_into_the_service(monkeypatch):
    """Route wiring, sync path: the receipt built from the NotebookSummary the
    route already loaded must be in context while repo.ask runs -- that is the
    only reason it can reach the answer that gets persisted."""
    from app.api import ask_routes
    from app.models.ask import AskResponse
    from app.services.source_scope import current_retrieval_scope_receipt

    seen: list = []

    class _Repo:
        def get_notebook(self, _notebook_id):
            return _receipt_notebook(
                sources=2, bases=[NotebookRef(id="b1", name="论文库")]
            )

        def visible_source_ids(self, _notebook_id, source_ids):
            return list(source_ids)

        def all_visible_source_ids(self, _notebook_id):
            return ["s1", "s2"]

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
    to be in context at the `submit()` that snapshots it -- not merely inside
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
        event_log=SimpleNamespace(logger=logging.getLogger("t3-stream")),
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


def test_receipt_reads_an_unfrozen_exclude_scope_by_its_mode():
    """The routes always freeze to `include` first, so this shape cannot arrive
    over HTTP -- but reading `exclude` as if it were `include` would invert the
    receipt (naming the UNCHECKED libraries as the searched ones), and a scope
    report that can lie about the scope is worth less than none."""
    from app.api.ask_routes import _scope_receipt

    notebook = _receipt_notebook(sources=4, bases=[
        NotebookRef(id="b1", name="论文库"),
        NotebookRef(id="b2", name="手册库"),
    ])
    receipt = _scope_receipt(
        notebook,
        SourceScope(mode="exclude", source_ids=["s1"]),
        BaseNotebookScope(mode="exclude", notebook_ids=["b1"]),
    )
    assert receipt.local.selected == 3 and receipt.local.total == 4
    assert [(b.name, b.included) for b in receipt.bases] == [
        ("论文库", False), ("手册库", True),
    ]


def test_receipt_does_not_truncate_below_the_model_cap():
    """回执是**披露**载体:被裁过的列表当成完整的,会让前端算错分母,还可能藏起对某个
    被省略库的收窄(codex #431 P3)。构造处的截断上限必须跟随
    ``RetrievalScopeReceipt.bases`` 的模型上限——更低的值等于静默丢掉模型本可接受的库。
    """
    from app.api.ask_routes import _scope_receipt

    bases = [NotebookRef(id=f"b{i}", name=f"参考库{i}") for i in range(1200)]
    notebook = _receipt_notebook(sources=1, bases=bases)
    receipt = _scope_receipt(
        notebook, None, BaseNotebookScope(mode="include", notebook_ids=["b0"]),
    )
    assert receipt is not None
    assert len(receipt.bases) == 1200, "1000 处截断会让回执谎报参考库总数"
    assert sum(1 for item in receipt.bases if item.included) == 1

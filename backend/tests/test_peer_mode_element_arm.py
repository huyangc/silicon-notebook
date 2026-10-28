"""对等(全局)模式下原文元素检索臂的逐库联邦化合同。

``CandidateRetrievalService.retrieve_elements`` 在对等模式下不再整条返回 ``[]``,而是
对每个参与库各跑一次**同一套**单库元素检索(``_retrieve_elements``,天花板为该库自己
的冻结天花板 ∩ 可见清单),借语义腿的联邦调度(``chunk_federation._run_tasks``),按
库轮转合并、去重,以单库元素上限(``limit``)封顶。本文件钉住:

* 每个库各查一次、传的是**该库自己的**冻结天花板、声明为上下文天花板
  (``producer_explicit=False``)、跑在补召回腿位(``_CHUNK_SUPPLEMENT_LEG``)下;
* 合并按轮次:每库第 1 条、再每库第 2 条……轮内按分数排(不偏名义 active),
  去重、封顶;每条命中打上所属库;参与集同语义腿(8 库上限);
* 结果边界按**元素自己的库**的天花板复核;没有可见来源/不在冻结天花板里的库零查询;
* 某库失败/超时只让那一库为空:不抛、不进覆盖回执、不上横幅;取消照常上抛;
  它的全文超时不打开共享的按库 FTS 熔断;
* 交出去的那批元素按读到的全文登记检索时刻证据(``attest_read``);
* 开关关掉 → 对等模式回到 ``[]``,一条查询都不发;
* 引用卡:``notebook_id`` 是元素所在库、``tier`` 按该库查表;单库逐值不变;
* reasoning:对等 run 的 ``search_elements`` 外层不取扇出槽,首轮全空补腿按开关;
* **单库路径守卫**:非对等模式下调用形状与改动前逐项相同,开关不被读。
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from app.domain.evidence_fingerprint import element_text_sha
from app.domain.retrieval import RetrievedElement
from app.models.ask import AnswerAnchor
from app.repositories.ports import ChunkLexicalSearchTimeout
from app.repositories.read_budget import ReadBudgetExceeded
from app.services.cancellation import AskCancelled
from app.services.retrieval_candidates import (
    CandidateRetrievalService, element_arm_available,
)
from app.services.retrieval_run import retrieval_run
from app.services.source_scope import source_scope_context
from tests.test_peer_mode_keyword_arm import (
    _ACTOR, Receipts, _global_run, _peer_run,
)


_RECALL = 7


def _borrow(name: str):
    return getattr(CandidateRetrievalService, name)


class ElementProbe:
    """元素臂的最小替身:四个被测方法从真实类上原样借来,底下只换掉
    ``_retrieve_chunks``(召回)与 ``_retrieve_elements_from_chunks``(回表打分)。

    ``hits`` 是 ``{notebook_id: [(element_id, score[, source_id]), ...]}`` 或一个
    要抛出的异常;``source_id`` 缺省为 ``src-<库>``(在该库冻结天花板内)。
    """

    retrieve_elements = _borrow("retrieve_elements")
    _peer_retrieve_elements = _borrow("_peer_retrieve_elements")
    _peer_element_leg = _borrow("_peer_element_leg")
    _retrieve_elements = _borrow("_retrieve_elements")
    _element_chunk_recall = _borrow("_element_chunk_recall")

    def _chunk_recall_slot(self, *args, **kwargs):
        # This double has no ceiling machinery: no run-level recall memo.
        return None

    def __init__(self, participants=("nb-a",), *, hits=None, visible=None,
                 enabled=True, max_participants=8):
        self._participants = tuple((nid, "personal") for nid in participants)
        self._hits = hits or {}
        self._visible = visible if visible is not None else {
            nid: (f"src-{nid}", f"src-{nid}-extra") for nid in participants
        }
        self.settings = SimpleNamespace(
            chunk_recall=_RECALL,
            chunk_federation_enabled=True,
            chunk_federation_max_participants=max_participants,
            chunk_fanout_max_workers=4,
            global_ask_element_arm_enabled=enabled,
        )
        self.events: list = []
        self.event_log = SimpleNamespace(emit=self.events.append)
        self.chunk_calls: list = []
        self.model_errors: list = []
        self.seat_reads = 0
        self._lock = threading.Lock()
        self.sources = SimpleNamespace(
            all_visible_source_ids=lambda nid: self._visible.get(nid, ()),
        )

    def _retrieval_participants(self, active_notebook_id):
        self.seat_reads += 1
        return self._participants

    def notebook_copy_stats(self, notebook_id):
        return {"copyable": True}

    def _unsafe_source_scope_restricted(self, notebook_id):
        return False

    def _note_model_error(self, stage, model, exc=None, *args, **kwargs):
        with self._lock:
            self.model_errors.append(stage)

    def _retrieve_chunks(self, notebook_id, query, recall=0, *,
                         allowed_source_ids=None, **extra):
        from app.services.chunk_lane import _CHUNK_SUPPLEMENT_LEG

        with self._lock:
            self.chunk_calls.append((
                notebook_id, query, recall, allowed_source_ids, extra,
                _CHUNK_SUPPLEMENT_LEG.get(),
            ))
        planned = self._hits.get(notebook_id, [])
        if isinstance(planned, BaseException):
            raise planned
        return [SimpleNamespace(notebook_id=notebook_id)], [], None

    def _retrieve_elements_from_chunks(self, query, chunks, *, limit, notebook_id):
        assert notebook_id == chunks[0].notebook_id
        notebook_id = chunks[0].notebook_id
        rows = [
            RetrievedElement(
                element_id=row[0],
                source_id=row[2] if len(row) > 2 else f"src-{notebook_id}",
                source_title=f"title-{notebook_id}", location_label="p1",
                element_type="paragraph", text=f"text-{row[0]}", score=row[1],
            )
            for row in self._hits.get(notebook_id, [])
        ]
        return rows[:limit]

    def summary(self):
        rows = [event for event in self.events
                if event.get("stage") == "global_element_arm"]
        assert len(rows) == 1, self.events
        return rows[0]


def _ids(hits):
    return [(hit.notebook_id, hit.element_id) for hit in hits]


# ---------------------------------------------------------------------------
# 1. 每个库各一次、各自的天花板;按轮次合并
# ---------------------------------------------------------------------------

def test_each_participant_is_searched_once_under_its_own_ceiling():
    probe = ElementProbe(
        ("nb-a", "nb-b", "nb-c"),
        hits={
            "nb-a": [("a1", 0.9), ("a2", 0.5), ("a3", 0.4)],
            "nb-b": [("b1", 0.95), ("b2", 0.8)],
            "nb-c": [("c1", 0.3)],
        },
    )

    with _peer_run(("nb-a", "nb-b", "nb-c")):
        merged = probe.retrieve_elements("nb-a", "低温增益")

    # 可见清单 ∩ 该库冻结天花板;单库元素检索的召回窗;上下文天花板、补召回腿位。
    assert sorted(probe.chunk_calls) == [
        (nid, "低温增益", 32, (f"src-{nid}",), {}, True)
        for nid in ("nb-a", "nb-b", "nb-c")
    ]
    # 第 1 轮按分数:b1 > a1 > c1;第 2 轮:b2 > a2;第 3 轮:a3。每条都打上所属库。
    assert _ids(merged) == [
        ("nb-b", "b1"), ("nb-a", "a1"), ("nb-c", "c1"),
        ("nb-b", "b2"), ("nb-a", "a2"), ("nb-a", "a3"),
    ]
    summary = probe.summary()
    assert summary == {
        "kind": "ask_stage", "stage": "global_element_arm",
        "site": "global_element_arm", "participants": 3,
        "libraries_with_hits": 3, "merged": 6, "failed_libraries": 0,
        "latency_ms": summary["latency_ms"],
    }
    assert all("低温" not in repr(event) for event in probe.events)


def test_the_total_is_the_single_library_cap_and_a_mid_round_cut_drops_the_weakest():
    """封顶 = 单库元素上限;截在一轮中间时留下的是这一轮里分数高的,
    名义 active(参与集第一个)不因位置占先。"""
    probe = ElementProbe(
        ("nb-a", "nb-b", "nb-c"),
        hits={
            "nb-a": [("a1", 0.9), ("a2", 0.2)],
            "nb-b": [("b1", 0.8), ("b2", 0.7)],
            "nb-c": [("c1", 0.6), ("c2", 0.65)],
        },
    )

    with _peer_run(("nb-a", "nb-b", "nb-c")):
        merged = probe.retrieve_elements("nb-a", "q", 4)

    assert _ids(merged) == [
        ("nb-a", "a1"), ("nb-b", "b1"), ("nb-c", "c1"), ("nb-b", "b2"),
    ]
    # 每个库自己的窗口也是同一个上限。
    assert {call[2] for call in probe.chunk_calls} == {max(_RECALL, 4 * 4, 4)}


def test_eight_libraries_bound_and_cap():
    """参与集同语义腿(``chunk_federation_max_participants``=8):第 9、10 个库不查;
    合并总量仍是单库上限 8,每库各一条——没有哪个库能靠数量占满。"""
    ids = tuple(f"nb-{index}" for index in range(10))
    probe = ElementProbe(ids, hits={
        nid: [(f"{nid}-e{rank}", 0.9 - rank * 0.01) for rank in range(8)]
        for nid in ids
    })

    with _peer_run(ids):
        merged = probe.retrieve_elements(ids[0], "q")

    assert sorted(call[0] for call in probe.chunk_calls) == sorted(ids[:8])
    assert len(merged) == 8
    assert sorted(hit.notebook_id for hit in merged) == sorted(ids[:8])
    assert probe.summary()["participants"] == 8


def test_duplicate_element_ids_are_taken_once():
    probe = ElementProbe(
        ("nb-a", "nb-b"),
        hits={"nb-a": [("same", 0.5), ("a2", 0.4)], "nb-b": [("same", 0.9)]},
    )
    with _peer_run(("nb-a", "nb-b")):
        merged = probe.retrieve_elements("nb-a", "q")
    assert [hit.element_id for hit in merged] == ["same", "a2"]


def test_a_one_library_peer_set_still_takes_the_federated_branch():
    probe = ElementProbe(("nb-a",), hits={"nb-a": [("a1", 0.9)]})
    with _peer_run(("nb-a",)):
        merged = probe.retrieve_elements("nb-a", "q")
    assert [call[3] for call in probe.chunk_calls] == [("src-nb-a",)]
    assert _ids(merged) == [("nb-a", "a1")]


# ---------------------------------------------------------------------------
# 2. 权限:只读得到冻结天花板内、按元素自己的库复核
# ---------------------------------------------------------------------------

def test_libraries_without_a_readable_source_issue_no_query():
    """没有可见来源的库、不在本 run 冻结天花板里的库(用户读不到 / 没选中):零查询。"""
    probe = ElementProbe(
        ("nb-a", "nb-b", "nb-x"),
        hits={"nb-a": [("a1", 0.9)], "nb-b": [("b1", 0.9)], "nb-x": [("x1", 0.9)]},
        visible={"nb-a": ("src-nb-a",), "nb-b": (), "nb-x": ("src-nb-x",)},
    )
    from tests.test_peer_mode_keyword_arm import _override
    from app.services.retrieval_participants import participant_override

    ids = ("nb-a", "nb-b", "nb-x")
    with retrieval_run(run_kind="ask_chunk", actor_id=_ACTOR):
        with source_scope_context(
            "nb-a", None, None,
            # nb-x 不在冻结天花板里:它的天花板是空集。
            notebook_source_ceilings={"nb-a": {"src-nb-a"}, "nb-b": {"src-nb-b"},
                                      "nb-x": set()},
            subjectless=True,
        ):
            with participant_override(_override(ids)):
                merged = probe.retrieve_elements("nb-a", "q")

    assert [call[0] for call in probe.chunk_calls] == ["nb-a"]
    assert _ids(merged) == [("nb-a", "a1")]


def test_the_result_boundary_judges_each_element_by_its_own_library():
    """兜底复核按元素**自己的库**:nb-b 的元素对着 nb-b 的天花板(放行),天花板外
    来源的元素被丢——若按名义 active 判,nb-b 的元素会被整条误丢。"""
    probe = ElementProbe(
        ("nb-a", "nb-b"),
        hits={
            "nb-a": [("a1", 0.9), ("a-out", 0.8, "src-nb-a-extra")],
            "nb-b": [("b1", 0.7)],
        },
    )
    with _peer_run(("nb-a", "nb-b")):
        merged = probe.retrieve_elements("nb-a", "q")
    assert _ids(merged) == [("nb-a", "a1"), ("nb-b", "b1")]


def test_a_caller_list_only_narrows():
    probe = ElementProbe(
        ("nb-a", "nb-b"), hits={"nb-a": [("a1", 0.9)], "nb-b": [("b1", 0.9)]},
    )
    with _peer_run(("nb-a", "nb-b")):
        merged = probe.retrieve_elements(
            "nb-a", "q", 8, allowed_source_ids=["src-nb-b"])
    assert [call[:4] for call in probe.chunk_calls] == [
        ("nb-b", "q", 32, ("src-nb-b",)),
    ]
    assert _ids(merged) == [("nb-b", "b1")]


# ---------------------------------------------------------------------------
# 3. 失败 fail-open;取消上抛;证据登记
# ---------------------------------------------------------------------------

def test_failing_libraries_leave_the_rest_register_only_what_is_handed_out():
    receipts = Receipts()
    probe = ElementProbe(
        ("nb-a", "nb-b", "nb-c", "nb-d"),
        hits={
            "nb-a": [("a1", 0.9), ("a2", 0.1)],
            "nb-b": ReadBudgetExceeded(),
            "nb-c": ChunkLexicalSearchTimeout(),
            "nb-d": RuntimeError("boom"),
        },
    )

    with _global_run(("nb-a", "nb-b", "nb-c", "nb-d"), receipts):
        merged = probe.retrieve_elements("nb-a", "q", 1)

    assert _ids(merged) == [("nb-a", "a1")]
    # 覆盖口径只由语义腿决定:元素腿一个回执都不发。
    assert receipts.libraries == []
    # 检索时刻证据:恰好是交出去的那批,按读到的全文取指纹;截掉的 a2 不登记。
    assert receipts.evidence == [{"a1": ("src-nb-a", element_text_sha("text-a1"))}]
    assert probe.model_errors == []
    assert probe.summary()["failed_libraries"] == 3
    skipped = sorted(
        (event["notebook_id"], event.get("arm"))
        for event in probe.events
        if event.get("kind") == "chunk_federation_skipped"
    )
    assert skipped == [("nb-b", "element"), ("nb-c", "element"),
                       ("nb-d", "element")]
    assert "boom" not in repr(probe.events)


def test_nothing_is_registered_without_hits_or_without_a_plan():
    receipts = Receipts()
    probe = ElementProbe(("nb-a", "nb-b"))
    with _global_run(("nb-a", "nb-b"), receipts):
        assert probe.retrieve_elements("nb-a", "q") == []
    assert receipts.evidence == []
    # 没有运行计划(普通笔记本路径不可能进到这里,这里只证明零副作用)。
    probe = ElementProbe(("nb-a",), hits={"nb-a": [("a1", 0.9)]})
    with _peer_run(("nb-a",)):
        assert _ids(probe.retrieve_elements("nb-a", "q")) == [("nb-a", "a1")]


def test_cancellation_propagates_from_an_element_leg():
    probe = ElementProbe(
        ("nb-a", "nb-b"), hits={"nb-a": [("a1", 0.9)], "nb-b": AskCancelled()},
    )
    with _peer_run(("nb-a", "nb-b")):
        with pytest.raises(AskCancelled):
            probe.retrieve_elements("nb-a", "q")


def test_a_cancelled_global_run_raises_instead_of_answering():
    cancel = threading.Event()
    cancel.set()
    probe = ElementProbe(("nb-a", "nb-b"), hits={"nb-a": [("a1", 0.9)]})
    with _global_run(("nb-a", "nb-b"), Receipts(), cancel=cancel):
        with pytest.raises(AskCancelled):
            probe.retrieve_elements("nb-a", "q")
    assert probe.chunk_calls == []


class _FtsProbe:
    """真实 ``_chunk_fts_hits`` + 真实 run 级熔断,底下换掉适配器。"""

    _chunk_fts_hits = _borrow("_chunk_fts_hits")

    def __init__(self):
        self.events: list = []
        self.event_log = SimpleNamespace(emit=self.events.append)
        self.knowledge = SimpleNamespace(chunk_fts_search=self._search)
        self.timeouts = 0

    def _search(self, db, notebook_id, query, **kwargs):
        self.timeouts += 1
        raise ChunkLexicalSearchTimeout()


def test_an_element_leg_fts_timeout_never_opens_the_shared_circuit():
    """补召回腿位下的全文超时只遵守、不打开共享的按库熔断;对照臂:同一次超时
    在语义腿位下照旧打开它。"""
    from app.services.chunk_lane import _CHUNK_SUPPLEMENT_LEG
    from app.services.retrieval_run import current_retrieval_run

    probe = _FtsProbe()
    with retrieval_run(run_kind="ask_chunk", actor_id=_ACTOR):
        token = _CHUNK_SUPPLEMENT_LEG.set(True)
        try:
            with pytest.raises(ChunkLexicalSearchTimeout):
                probe._chunk_fts_hits("db", "nb-a", "q", k=5)
        finally:
            _CHUNK_SUPPLEMENT_LEG.reset(token)
        assert current_retrieval_run().chunk_fts_permitted("nb-a") is True
        with pytest.raises(ChunkLexicalSearchTimeout):
            probe._chunk_fts_hits("db", "nb-a", "q", k=5)
        assert current_retrieval_run().chunk_fts_permitted("nb-a") is False


def test_the_element_leg_runs_its_search_under_the_supplement_flag_only():
    probe = ElementProbe(("nb-a",), hits={"nb-a": [("a1", 0.9)]})
    from app.services.chunk_lane import _CHUNK_SUPPLEMENT_LEG

    with _peer_run(("nb-a",)):
        probe.retrieve_elements("nb-a", "q")
        assert _CHUNK_SUPPLEMENT_LEG.get() is False
    assert [call[5] for call in probe.chunk_calls] == [True]


# ---------------------------------------------------------------------------
# 4. 开关与单库路径
# ---------------------------------------------------------------------------

def test_switch_off_restores_the_empty_peer_arm_and_queries_nothing():
    probe = ElementProbe(("nb-a", "nb-b"), hits={"nb-a": [("a1", 0.9)]},
                         enabled=False)
    with _peer_run(("nb-a", "nb-b")):
        assert probe.retrieve_elements("nb-a", "q") == []
        assert element_arm_available(probe.settings) is False
    assert probe.chunk_calls == [] and probe.seat_reads == 0
    assert probe.events == []


@pytest.mark.parametrize("enabled", [True, False])
def test_single_notebook_path_is_unchanged(enabled):
    """非对等模式(挂着参考库):调用形状同改动前——一次 ``_retrieve_elements``,
    参与集座位与开关都不读,不发汇总事件,命中不打库标。"""
    probe = ElementProbe(
        ("nb-a", "nb-ref"), hits={"nb-a": [("a1", 0.9)], "nb-ref": [("r1", 0.99)]},
        enabled=enabled,
    )
    calls: list = []
    real = probe._retrieve_elements

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    probe._retrieve_elements = _spy
    with retrieval_run(run_kind="ask_chunk", actor_id=_ACTOR):
        with source_scope_context(
            "nb-a", None, None, notebook_source_ceilings={"nb-a": {"src-nb-a"}},
        ):
            merged = probe.retrieve_elements("nb-a", "q")

    assert calls == [(("nb-a", "q"), {})]
    assert _ids(merged) == [("", "a1")]
    assert probe.seat_reads == 0
    assert [event["kind"] for event in probe.events] == ["element_scoring_bounded"]
    # 读开关就会 AttributeError:单库路径根本不读它。
    assert element_arm_available(SimpleNamespace()) is True


# ---------------------------------------------------------------------------
# 5. 引用卡:库与 tier 按元素所在库
# ---------------------------------------------------------------------------

class _TierLog:
    def __init__(self):
        self.calls: list = []

    def tier_map(self, notebook_ids):
        self.calls.append(list(notebook_ids))
        tiers = {"active": "personal", "base": "base"}
        return {nid: tiers[nid] for nid in notebook_ids if nid in tiers}


def _evidence_service(tiers):
    from app.core.config import Settings
    from app.services.evidence_context import EvidenceContextService
    from tests.test_evidence_context_service import _Knowledge, _Sources

    return EvidenceContextService(
        notebooks=tiers, sources=_Sources(), knowledge=_Knowledge(),
        settings=Settings(_env_file=None),
    )


def _element(element_id, notebook_id=""):
    return RetrievedElement(
        element_id=element_id, source_id=f"src-{element_id}",
        source_title=f"T-{element_id}", location_label="p. 1",
        element_type="paragraph", text=f"text {element_id}", score=0.5,
        notebook_id=notebook_id,
    )


def _anchors(*element_ids):
    return [AnswerAnchor(key=f"k{index}", object_id=element_id,
                         object_type="element", label=element_id)
            for index, element_id in enumerate(element_ids, 1)]


def test_peer_element_cards_name_their_own_library_and_its_tier():
    tiers = _TierLog()
    service = _evidence_service(tiers)
    elements = [_element("e1", "active"), _element("e2", "base")]

    with _peer_run(("active", "base")):
        block, evidence = service.element_context(elements, notebook_id="active")
        cards = service.element_citations(
            elements, _anchors("e1", "e2"), notebook_id="active")

    assert [(value["notebook_id"], value["tier"]) for value in evidence.values()] == [
        ("active", "personal"), ("base", "base"),
    ]
    assert "[source-element][personal]" in block and "[source-element][base]" in block
    assert [(card.notebook_id, card.tier) for card in cards] == [
        ("active", "personal"), ("base", "base"),
    ]
    # 每次装配一次 tier 查表,覆盖池里涉及的每一本库。
    assert tiers.calls == [["active", "base"], ["active", "base"]]


def test_single_library_element_cards_are_unchanged():
    tiers = _TierLog()
    service = _evidence_service(tiers)
    elements = [_element("e1")]

    _block, evidence = service.element_context(elements, notebook_id="active")
    cards = service.element_citations(elements, _anchors("e1"), notebook_id="active")

    assert [(value["notebook_id"], value["tier"]) for value in evidence.values()] == [
        ("", "personal"),
    ]
    assert [(card.notebook_id, card.tier) for card in cards] == [("", "personal")]
    # 与改动前同一次调用:只问传进来的这一本。
    assert tiers.calls == [["active"], ["active"]]


# ---------------------------------------------------------------------------
# 6. reasoning 侧:外层不取槽,首轮全空补腿按开关
# ---------------------------------------------------------------------------

def test_peer_search_elements_holds_no_outer_fanout_slot(monkeypatch):
    """对等 run 的联邦腿在叶子上自己取扇出槽;外层再持一个会在 ``fanout_limit=1``
    时自锁。单库对照臂照旧圈住那一次调用。"""
    import app.services.reasoning_retrieval as rr_mod
    from app.services.reasoning_retrieval import ReasoningRetriever

    held: list = []

    class _Slot:
        def __enter__(self):
            held.append("slot")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(rr_mod, "retrieval_fanout_slot", lambda: _Slot())
    retriever = object.__new__(ReasoningRetriever)
    retriever.candidate_filter = None
    retriever.retrieval = SimpleNamespace(
        retrieve_elements=lambda notebook_id, query: [_element("e1", "nb-a")])

    with _peer_run(("nb-a", "nb-b")):
        assert [hit.element_id for hit in ReasoningRetriever.search_elements(
            retriever, "nb-a", "q")] == ["e1"]
    assert held == []
    ReasoningRetriever.search_elements(retriever, "nb-a", "q")
    assert held == ["slot"]


@pytest.mark.parametrize("enabled", [True, False])
def test_first_round_empty_fallback_follows_the_switch_in_peer_mode(enabled):
    from app.services.reasoning_retrieval import ReasoningRetriever

    searched: list = []
    retriever = object.__new__(ReasoningRetriever)
    retriever.settings = SimpleNamespace(reasoning_max_element_searches=2)
    retriever.retrieval = SimpleNamespace(element_arm_available=lambda: enabled)
    retriever.search_elements = lambda notebook_id, query: (
        searched.append((notebook_id, query)) or [_element("e1", "nb-b")])
    trace: list = []
    state = SimpleNamespace(
        record=trace.append, notebook_id="nb-a", question="问题",
        collected={}, elements=[], chunks=[], elements_searches=0,
    )

    with _peer_run(("nb-a", "nb-b")):
        ReasoningRetriever._first_round_empty_fallback(retriever, state)

    if enabled:
        assert searched == [("nb-a", "问题")]
        assert state.elements_searches == 1
        assert [hit.notebook_id for hit in state.elements] == ["nb-b"]
        assert [step.detail["reason"] for step in trace] == ["initial_evidence_empty"]
    else:
        assert searched == [] and trace == [] and state.elements_searches == 0


# ---------------------------------------------------------------------------
# 7. 真实通道:元素腿内部的 fail-open 记错不进回答横幅
# ---------------------------------------------------------------------------

class _BrokenQueryEmbedder:
    """只换掉查询向量适配器:每次都抛,其余(ANN/FTS/回表)全是真实通道。"""

    configured = True
    model = "fake"

    def embed_query(self, text):
        raise RuntimeError("embedding endpoint down")

    def embed_documents(self, texts):
        raise RuntimeError("embedding endpoint down")


def _real_provider_libraries(tmp_path, monkeypatch, count=2):
    """真实 ``SQLiteRepository`` + **真实** ``ModelProvider``(它的 ``note_model_error``
    才写 ``_ASK_MODEL_ERRORS``;测试用的 RecordingModelProvider 只记账),几个处理过
    的库,每库一篇带「低温增益」原文的文档。"""
    from app.core.config import Settings
    from app.models.notebooks import NotebookCreate
    from app.repositories.ports import UploadedSourceFile
    from app.services.embedding import FakeEmbedder
    from app.services.sqlite_repository import SQLiteRepository
    from tests.model_testkit import bind_all_embedding_clients

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'leg.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    repo = SQLiteRepository(Settings())
    bind_all_embedding_clients(repo, FakeEmbedder(dim=16))
    notebooks = []
    for index in range(count):
        notebook = repo.create_notebook(NotebookCreate(name=f"库{index}"))
        source = repo.upload_sources(notebook.id, [UploadedSourceFile(
            file_name=f"手册{index}.md", content_type="text/markdown",
            content=(f"# 手册{index}\n\n## 低温性能\n\n低温下的增益随温度下降而上升，"
                     f"零下四十度仍然满足指标{index}。").encode("utf-8"),
        )], scheduler=lambda _source_id: None)[0]
        repo.process_source(source.id)
        notebooks.append(notebook)
    return repo, notebooks


def test_an_element_leg_model_error_is_an_event_not_a_banner(tmp_path, monkeypatch):
    """真实 ``_retrieve_elements`` → ``_retrieve_chunks`` → ``_embed_query`` →
    真实 ``ModelProvider.note_model_error``:查询向量失败时,元素腿照旧经全文检索
    交回元素;``model_error`` 事件照写,但本次回答的横幅 sink(``_ASK_MODEL_ERRORS``)
    一条都不进。对照臂:同一个 run 里语义侧直接走同一条通道,同一次失败照旧进
    sink——屏蔽只在元素腿上。"""
    from app.core.ask_context import _ASK_MODEL_ERRORS
    from app.services.retrieval_participants import (
        ParticipantOverride, participant_override,
    )
    from tests import global_citation_e2e_kit as kit
    from tests.model_testkit import bind_embedding_client

    repo, notebooks = _real_provider_libraries(tmp_path, monkeypatch)
    try:
        bind_embedding_client(repo, _BrokenQueryEmbedder(),
                              workload_ids=("retrieval_query_embedding",))
        events = kit.capture_events(repo, monkeypatch)
        candidates = repo._runtime.retrieval_component.candidates
        actor = repo.current_user().id
        ids = tuple(nb.id for nb in notebooks)
        ceilings = {
            nid: set(repo._runtime.source_store.all_visible_source_ids(nid))
            for nid in ids
        }
        sink: list = []
        token = _ASK_MODEL_ERRORS.set(sink)
        try:
            with retrieval_run(run_kind="ask_reasoning", actor_id=actor):
                with source_scope_context(
                    ids[0], None, None, notebook_source_ceilings=ceilings,
                    subjectless=True,
                ):
                    with participant_override(ParticipantOverride(
                        notebook_ids=ids, tiers={}, attested_actor_id=actor,
                    )):
                        hits = candidates.retrieve_elements(ids[0], "低温 增益 指标")
                        leg_sink = list(sink)
                        # 对照臂:语义侧同一条通道、同一次失败,照旧进 sink。
                        candidates._retrieve_chunks(ids[0], "低温 增益 指标")
        finally:
            _ASK_MODEL_ERRORS.reset(token)
    finally:
        repo.close()

    assert leg_sink == []
    assert hits and {hit.notebook_id for hit in hits} == set(ids)
    embed_errors = [e for e in events
                    if e.get("kind") == "model_error" and e.get("stage") == "embed"]
    # 每条元素腿一条 + 对照臂一条:事件一条不少。
    assert len(embed_errors) >= len(ids) + 1
    assert sink and {entry["stage"] for entry in sink} == {"embed"}

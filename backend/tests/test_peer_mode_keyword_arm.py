"""对等(全局)模式下双语关键词补召回臂的逐库联邦化合同。

``_keyword_chunk_candidates`` 在对等模式下不再整条返回 ``[]``,而是对每个参与库各跑
一次**同一套**单库关键词检索(``_keyword_chunk_candidates_one``),借语义腿的联邦
调度(``chunk_federation._run_tasks``),按库轮转交错、去重、以
``GLOBAL_ASK_CANDIDATE_LIMIT`` 封顶。本文件钉住五件事:

* 每个库各查一次、传的是**该库自己的**冻结天花板;单库集合同样走联邦分支;
* 合并是轮转交错 + 去重 + 封顶,每条命中打上所属库;
* 某库失败/超时只让那一库这一路为空:不抛、不进覆盖回执、超时不上横幅;
  ``AskCancelled`` 照常上抛;
* 开关关掉 → 对等模式回到 ``[]``,一条查询都不发;
* **单库路径守卫**:非对等模式(含带挂载参考库、含冻结来源勾选)下,发出的查询
  次数、参数与探针顺序与改动前逐项相同,开关也不被读。

替身只实现关键词臂走到 FTS 所需的最小面,真实方法从 ``CandidateRetrievalService``
上按原样借过来;``score_chunks`` 换成按预置分数排序的替身,使库内顺序可断言。
"""
from __future__ import annotations

import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.domain.retrieval import RetrievedChunk
from app.models.source_scope import SourceScope
from app.repositories.ports import ChunkLexicalSearchTimeout
from app.repositories.read_budget import ReadBudgetExceeded
from app.services import retrieval as retrieval_module
from app.services.cancellation import AskCancelled
from app.services.federated_run import DetachedAskTurn, FederatedRunPlan
from app.services.global_run import global_ask_run
from app.services.retrieval_candidates import CandidateRetrievalService
from app.services.retrieval_participants import (
    ParticipantOverride, participant_override,
)
from app.services.retrieval_run import retrieval_run
from app.services.source_scope import source_scope_context


_ACTOR = "user-peer-keyword"
_NEEDLE = "低噪声放大器 low noise amplifier"
_RECALL = 7
_CANDIDATE_LIMIT = 64


def _borrow(name: str):
    return getattr(CandidateRetrievalService, name)


class KeywordProbe:
    """``CandidateRetrievalService`` 关键词臂的最小替身,逐步记账。

    三个被测方法按原样从真实类上借;其余是这条臂读到的表面。``hits`` 是
    ``{notebook_id: [(chunk_id, relevance), ...]}`` 或一个要抛出的异常。
    """

    _keyword_chunk_candidates = _borrow("_keyword_chunk_candidates")
    _peer_keyword_leg = _borrow("_peer_keyword_leg")
    _keyword_chunk_candidates_one = _borrow("_keyword_chunk_candidates_one")
    _real_gate = _borrow("_lexical_gate_source_scoped")

    def __init__(self, participants=("nb-a",), *, hits=None, visible=None,
                 limit=_CANDIDATE_LIMIT, enabled=True):
        self._participants = tuple((nid, "personal") for nid in participants)
        self._hits = hits or {}
        self._visible = visible if visible is not None else {
            nid: (f"src-{nid}", f"src-{nid}-extra") for nid in participants
        }
        self.settings = SimpleNamespace(
            chunk_recall=_RECALL,
            chunk_federation_enabled=True,
            chunk_federation_max_participants=8,
            chunk_fanout_max_workers=4,
            global_ask_candidate_limit=limit,
            global_ask_keyword_arm_enabled=enabled,
        )
        self.events: list = []
        self.event_log = SimpleNamespace(emit=self.events.append)
        self.steps: list = []
        self.fts: list = []
        self.model_errors: list = []
        self.seat_reads = 0
        self._lock = threading.Lock()
        self.sources = SimpleNamespace(
            all_visible_source_ids=lambda nid: self._visible.get(nid, ()),
        )

    # --- 参与集座位(非对等路径绝不该读它) ---------------------------------
    def _retrieval_participants(self, active_notebook_id):
        self.seat_reads += 1
        return self._participants

    def notebook_copy_stats(self, notebook_id):
        return {"copyable": True}

    # --- 关键词臂走到 FTS 的那一串 ----------------------------------------
    def _record(self, step):
        with self._lock:
            self.steps.append(step)

    def _unsafe_source_scope_restricted(self, notebook_id):
        self._record(("restricted_probe", notebook_id))
        return False

    def _lexical_gate_source_scoped(self, allowed_source_ids, notebook_id, *,
                                    explicit=False, drifted=False):
        verdict = self._real_gate(
            allowed_source_ids, notebook_id, explicit=explicit, drifted=drifted,
        )
        self._record(("gate", notebook_id, allowed_source_ids, explicit,
                      drifted, verdict))
        return verdict

    def _lexical_corpus_langs(self, notebook_id, *, source_scoped=False):
        self._record(("corpus_langs", notebook_id, source_scoped))
        return None if source_scoped else ["en", "zh"]

    def _connect(self):
        return contextlib.nullcontext("db")

    def _chunk_fts_hits(self, db, notebook_id, needle, *, k,
                        allowed_source_ids, corpus_langs):
        call = ("chunk_fts", notebook_id, needle, k, allowed_source_ids,
                corpus_langs)
        self._record(call)
        with self._lock:
            self.fts.append(call)
        planned = self._hits.get(notebook_id, [])
        if isinstance(planned, BaseException):
            raise planned
        return [{"chunk_id": chunk_id} for chunk_id, _rel in planned]

    def _hydrate_chunk_candidates(self, chunk_ids):
        self._record(("hydrate", tuple(chunk_ids)))
        relevance = {
            chunk_id: rel
            for rows in self._hits.values() if isinstance(rows, list)
            for chunk_id, rel in rows
        }
        return (
            [{"chunk_id": cid, "relevance": relevance[cid]} for cid in chunk_ids],
            list(chunk_ids), None,
        )

    def _note_model_error(self, stage, model, exc=None, *args, **kwargs):
        with self._lock:
            self.model_errors.append((stage, type(exc).__name__))

    def summary(self):
        rows = [event for event in self.events
                if event.get("stage") == "global_keyword_arm"]
        assert len(rows) == 1, self.events
        return rows[0]


@pytest.fixture(autouse=True)
def ranked_scoring(monkeypatch):
    """库内顺序只由预置分数决定;调用参数也记下来供单库守卫断言。"""
    calls: list = []

    def _score(query, chunks, query_vector=None, chunk_sims=None, limit=150):
        calls.append((query, query_vector, chunk_sims, limit))
        ranked = sorted(chunks, key=lambda row: -row["relevance"])[:limit]
        return [
            RetrievedChunk(
                chunk_id=row["chunk_id"], source_id=f"src-{row['chunk_id']}",
                source_title="t", section_path="", text=f"text-{row['chunk_id']}",
                score=row["relevance"], relevance=row["relevance"],
            )
            for row in ranked
        ]

    monkeypatch.setattr(retrieval_module, "score_chunks", _score)
    return calls


def _ceilings(notebook_ids) -> dict:
    return {nid: {f"src-{nid}"} for nid in notebook_ids}


def _override(notebook_ids) -> ParticipantOverride:
    return ParticipantOverride(
        notebook_ids=tuple(notebook_ids), tiers={}, attested_actor_id=_ACTOR,
    )


@contextlib.contextmanager
def _peer_run(notebook_ids):
    """覆盖 + 逐库天花板 + 无主体位,没有运行计划(模块自建线程池那一支)。"""
    ids = tuple(notebook_ids)
    with retrieval_run(run_kind="ask_chunk", actor_id=_ACTOR):
        with source_scope_context(
            ids[0], None, None, notebook_source_ceilings=_ceilings(ids),
            subjectless=True,
        ):
            with participant_override(_override(ids)):
                yield ids


class Receipts:
    def __init__(self):
        self.libraries: list = []
        self.evidence: list = []
        self.groups: list = []

    def on_library(self, notebook_id, outcome):
        self.libraries.append((notebook_id, outcome))

    def on_evidence(self, fingerprints):
        self.evidence.append(dict(fingerprints))

    def on_evidence_groups(self, groups):
        self.groups.append(list(groups))


@contextlib.contextmanager
def _global_run(notebook_ids, receipts, *, cancel=None):
    """生产安装形状:``global_ask_run`` 一次装齐覆盖/天花板/回合/计划。"""
    ids = tuple(notebook_ids)
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="kw-test")
    plan = FederatedRunPlan(
        phase_timeout_seconds=60.0,
        notebook_timeout_seconds=5.0,
        executor=executor,
        window=lambda: 4,
        cancel=cancel if cancel is not None else threading.Event(),
        on_library=receipts.on_library,
        on_evidence=receipts.on_evidence,
        on_evidence_groups=receipts.on_evidence_groups,
    )
    try:
        with retrieval_run(run_kind="ask_chunk", actor_id=_ACTOR):
            with global_ask_run(
                _override(ids), _ceilings(ids),
                DetachedAskTurn(conversation_id="conv-kw"), plan,
                nominal_active=ids[0],
            ):
                yield ids
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def _ids(hits):
    return [(hit.notebook_id, hit.chunk_id) for hit in hits]


# ---------------------------------------------------------------------------
# 1. 每个库各一次、各自的天花板;合并轮转交错
# ---------------------------------------------------------------------------

def test_each_participant_is_searched_once_under_its_own_ceiling():
    probe = KeywordProbe(
        ("nb-a", "nb-b", "nb-c"),
        hits={
            "nb-a": [("a1", 0.9), ("a2", 0.8), ("a3", 0.7)],
            "nb-b": [("b2", 0.5), ("b1", 0.95)],
            "nb-c": [("c1", 0.4)],
        },
    )

    with _peer_run(("nb-a", "nb-b", "nb-c")):
        merged = probe._keyword_chunk_candidates("nb-a", f"  {_NEEDLE} ")

    by_library = sorted(probe.fts)
    assert by_library == [
        # 可见清单 ∩ 该库冻结天花板,不是名义 active 的,也不是 None。
        ("chunk_fts", nid, _NEEDLE, _RECALL, (f"src-{nid}",), ["en", "zh"])
        for nid in ("nb-a", "nb-b", "nb-c")
    ]
    # 每库按自己的关键词分数排序后轮转交错;每条都打上所属库。
    assert _ids(merged) == [
        ("nb-a", "a1"), ("nb-b", "b1"), ("nb-c", "c1"),
        ("nb-a", "a2"), ("nb-b", "b2"),
        ("nb-a", "a3"),
    ]
    assert all(
        [support.origin for support in hit.retrieval_supports] == ["lexical"]
        for hit in merged
    )
    summary = probe.summary()
    assert summary == {
        "kind": "ask_stage",
        "stage": "global_keyword_arm",
        "site": "global_keyword_arm",
        "participants": 3,
        "libraries_with_hits": 3,
        "merged": 6,
        "failed_libraries": 0,
        "latency_ms": summary["latency_ms"],
    }
    # 内容无关:关键词文本不进任何事件。
    assert all("低噪声" not in repr(event) and "amplifier" not in repr(event)
               for event in probe.events)
    # 漂移探针整条臂只探一次(名义 active),不是每库一次。
    assert [s for s in probe.steps if s[0] == "restricted_probe"] == [
        ("restricted_probe", "nb-a"),
    ]


def test_merge_dedups_and_is_capped_by_the_global_candidate_limit():
    probe = KeywordProbe(
        ("nb-a", "nb-b"),
        hits={
            "nb-a": [("shared", 0.9), ("a2", 0.8), ("a3", 0.7), ("a4", 0.6)],
            "nb-b": [("shared", 0.95), ("b2", 0.5), ("b3", 0.4)],
        },
        limit=4,
    )

    with _peer_run(("nb-a", "nb-b")):
        merged = probe._keyword_chunk_candidates("nb-a", _NEEDLE)

    # 同一 chunk 只留先出现的那条(nb-a 先轮到);一个库不能靠数量占满。
    assert _ids(merged) == [
        ("nb-a", "shared"), ("nb-a", "a2"), ("nb-b", "b2"), ("nb-a", "a3"),
    ]
    assert probe.summary()["merged"] == 4


def test_a_one_library_peer_set_still_takes_the_federated_branch():
    """用户只选了一个库:与 ``federated_chunk_candidates`` 的对等语义一致,仍走联邦
    分支——天花板下推、命中打上所属库、发汇总事件。"""
    probe = KeywordProbe(("nb-a",), hits={"nb-a": [("a1", 0.9)]})

    with _peer_run(("nb-a",)):
        merged = probe._keyword_chunk_candidates("nb-a", _NEEDLE)

    assert probe.fts == [
        ("chunk_fts", "nb-a", _NEEDLE, _RECALL, ("src-nb-a",), ["en", "zh"]),
    ]
    assert _ids(merged) == [("nb-a", "a1")]
    assert probe.summary()["participants"] == 1


def test_a_library_with_no_visible_source_issues_no_query():
    probe = KeywordProbe(
        ("nb-a", "nb-b"),
        hits={"nb-a": [("a1", 0.9)], "nb-b": [("b1", 0.9)]},
        visible={"nb-a": ("src-nb-a",), "nb-b": ()},
    )

    with _peer_run(("nb-a", "nb-b")):
        merged = probe._keyword_chunk_candidates("nb-a", _NEEDLE)

    assert [call[1] for call in probe.fts] == ["nb-a"]
    assert _ids(merged) == [("nb-a", "a1")]
    summary = probe.summary()
    assert (summary["libraries_with_hits"], summary["failed_libraries"]) == (1, 0)


# ---------------------------------------------------------------------------
# 2. 失败 fail-open:不抛、不进回执、超时不上横幅;取消照常上抛
# ---------------------------------------------------------------------------

def test_failing_libraries_leave_the_rest_and_never_reach_receipts():
    receipts = Receipts()
    probe = KeywordProbe(
        ("nb-a", "nb-b", "nb-c", "nb-d"),
        hits={
            "nb-a": [("a1", 0.9)],
            "nb-b": ReadBudgetExceeded(),
            "nb-c": ChunkLexicalSearchTimeout(),
            "nb-d": RuntimeError("boom"),
        },
    )

    with _global_run(("nb-a", "nb-b", "nb-c", "nb-d"), receipts):
        merged = probe._keyword_chunk_candidates("nb-a", _NEEDLE)

    assert _ids(merged) == [("nb-a", "a1")]
    # 覆盖口径只由语义腿决定:关键词腿一个回执、一份证据指纹都不发。
    assert receipts.libraries == []
    assert receipts.evidence == []
    assert receipts.groups == []
    # 全局模式里任何一个库的关键词腿失败都不走横幅(补召回臂缺席不影响结果的
    # 可信度),未分类的 RuntimeError 也一样;失败只以 skip 事件记下。
    assert probe.model_errors == []
    assert probe.summary()["failed_libraries"] == 3
    skipped = sorted(
        (event["notebook_id"], event.get("arm"))
        for event in probe.events
        if event.get("kind") == "chunk_federation_skipped"
    )
    assert skipped == [("nb-b", "keyword"), ("nb-c", "keyword"),
                       ("nb-d", "keyword")]


def test_cancellation_propagates_from_a_keyword_leg():
    probe = KeywordProbe(
        ("nb-a", "nb-b"),
        hits={"nb-a": [("a1", 0.9)], "nb-b": AskCancelled()},
    )

    with _peer_run(("nb-a", "nb-b")):
        with pytest.raises(AskCancelled):
            probe._keyword_chunk_candidates("nb-a", _NEEDLE)
    assert probe.model_errors == []


def test_a_cancelled_global_run_raises_instead_of_answering():
    cancel = threading.Event()
    cancel.set()
    probe = KeywordProbe(("nb-a", "nb-b"), hits={"nb-a": [("a1", 0.9)]})

    with _global_run(("nb-a", "nb-b"), Receipts(), cancel=cancel):
        with pytest.raises(AskCancelled):
            probe._keyword_chunk_candidates("nb-a", _NEEDLE)
    assert probe.fts == []


# ---------------------------------------------------------------------------
# 3. 开关
# ---------------------------------------------------------------------------

def test_switch_off_restores_the_empty_peer_arm_and_queries_nothing():
    probe = KeywordProbe(("nb-a", "nb-b"), hits={"nb-a": [("a1", 0.9)]},
                         enabled=False)

    with _peer_run(("nb-a", "nb-b")):
        assert probe._keyword_chunk_candidates("nb-a", _NEEDLE) == []

    assert probe.steps == []
    assert probe.seat_reads == 0
    assert probe.events == []


# ---------------------------------------------------------------------------
# 4. 单库路径守卫
# ---------------------------------------------------------------------------

def _single_path_steps(allowed, *, source_scoped=False):
    """改动前那条函数体发出的逐步调用,一字不差。"""
    return [
        ("restricted_probe", "nb-a"),
        ("gate", "nb-a", allowed, False, False, source_scoped),
        ("corpus_langs", "nb-a", source_scoped),
        ("chunk_fts", "nb-a", _NEEDLE, _RECALL, allowed,
         None if source_scoped else ["en", "zh"]),
        ("hydrate", ("a1", "a2")),
    ]


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("scope", ["none", "frozen_all", "narrowed"])
def test_single_notebook_path_is_unchanged(scope, enabled, ranked_scoring):
    """非对等模式(含挂载参考库、含冻结/收窄的来源勾选):一次 FTS、参数与探针
    顺序同改动前,参与集座位与开关都不读,不发汇总事件,命中不打库标。"""
    probe = KeywordProbe(
        # 挂着两个参考库:单库路径不许因为它们多发任何一条查询。
        ("nb-a", "nb-ref-1", "nb-ref-2"),
        hits={"nb-a": [("a1", 0.9), ("a2", 0.8)], "nb-ref-1": [("r1", 0.99)]},
        enabled=enabled,
    )
    local = {
        "none": None,
        "frozen_all": SourceScope(
            mode="include", source_ids=["s1", "s2"], narrowed=False,
        ),
        "narrowed": SourceScope(mode="include", source_ids=["s1"], narrowed=True),
    }[scope]
    expected_allowed = {
        "none": None, "frozen_all": ("s1", "s2"), "narrowed": ("s1",),
    }[scope]

    with retrieval_run(run_kind="ask_chunk", actor_id=_ACTOR):
        with source_scope_context("nb-a", local):
            merged = probe._keyword_chunk_candidates("nb-a", _NEEDLE)

    assert probe.steps == _single_path_steps(
        expected_allowed, source_scoped=(scope == "narrowed"),
    )
    assert len(probe.fts) == 1
    assert ranked_scoring == [(_NEEDLE, None, None, _RECALL)]
    assert [(hit.notebook_id, hit.chunk_id) for hit in merged] == [
        ("", "a1"), ("", "a2"),
    ]
    assert probe.seat_reads == 0
    assert probe.events == []


def test_single_notebook_failure_keeps_its_historical_handling():
    """单库路径:词法超时静默 ``[]``、其它故障记 ``chunk_keyword_union`` 后 ``[]``。"""
    timeout = KeywordProbe(hits={"nb-a": ChunkLexicalSearchTimeout()})
    assert timeout._keyword_chunk_candidates("nb-a", _NEEDLE) == []
    assert timeout.model_errors == []

    broken = KeywordProbe(hits={"nb-a": RuntimeError("boom")})
    assert broken._keyword_chunk_candidates("nb-a", _NEEDLE) == []
    assert broken.model_errors == [("chunk_keyword_union", "RuntimeError")]


def test_blank_keywords_query_nothing_in_either_mode():
    probe = KeywordProbe(("nb-a", "nb-b"))
    assert probe._keyword_chunk_candidates("nb-a", "   ") == []
    with _peer_run(("nb-a", "nb-b")):
        assert probe._keyword_chunk_candidates("nb-a", "") == []
    assert probe.steps == [] and probe.seat_reads == 0

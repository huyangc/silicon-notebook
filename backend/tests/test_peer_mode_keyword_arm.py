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


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


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
        self.elements: dict = {}
        self.snapshot_reads: list = []
        self.passage_error = None
        self.sources = SimpleNamespace(
            all_visible_source_ids=lambda nid: self._visible.get(nid, ()),
            passage_evidence_snapshot=self._passage_evidence_snapshot,
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
                        allowed_source_ids, corpus_langs, **peer_only):
        # ``peer_only`` 只可能是联邦关键词腿传的 ``trip_circuit=False``;单库路径
        # 一个多余的关键字都不许有,所以它被原样记进调用元组。
        call = ("chunk_fts", notebook_id, needle, k, allowed_source_ids,
                corpus_langs) + ((peer_only,) if peer_only else ())
        self._record(call)
        with self._lock:
            self.fts.append(call)
        planned = self._hits.get(notebook_id, [])
        if isinstance(planned, BaseException):
            raise planned
        return [{"chunk_id": chunk_id} for chunk_id, _rel in planned]

    def _rows(self, chunk_ids):
        relevance = {
            chunk_id: rel
            for rows in self._hits.values() if isinstance(rows, list)
            for chunk_id, rel in rows
        }
        return [
            {"chunk_id": cid, "relevance": relevance[cid],
             "element_ids": self.elements_of(cid)}
            for cid in chunk_ids
        ]

    # --- 检索时刻取证(``_report_evidence`` 读的那一口) ------------------------
    def elements_of(self, chunk_id):
        return list(self.elements.get(chunk_id, (f"el-{chunk_id}",)))

    def _passage_evidence_snapshot(self, chunk_ids):
        """默认:段落原文未动,元素指纹 ``fp-<element>``;``passage_error`` 让整批读失败。"""
        with self._lock:
            self.snapshot_reads.append(list(chunk_ids))
        if self.passage_error is not None:
            raise self.passage_error
        return {
            cid: {
                "text_sha": _sha(f"text-{cid}"),
                "elements": {
                    element_id: (f"src-{cid}", f"fp-{element_id}")
                    for element_id in self.elements_of(cid)
                },
            }
            for cid in chunk_ids
        }

    def _hydrate_chunk_candidates(self, chunk_ids):
        self._record(("hydrate", tuple(chunk_ids)))
        return self._rows(chunk_ids), list(chunk_ids), None

    def _hydrate_chunk_texts(self, chunk_ids):
        self._record(("hydrate_texts", tuple(chunk_ids)))
        return self._rows(chunk_ids)

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
                element_ids=list(row.get("element_ids", ())),
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
        # 联邦腿只遵守、不触发共享的按库 FTS 熔断。
        ("chunk_fts", nid, _NEEDLE, _RECALL, (f"src-{nid}",), ["en", "zh"],
         {"trip_circuit": False})
        for nid in ("nb-a", "nb-b", "nb-c")
    ]
    # 关键词打分不读向量:联邦腿只回表取文本,不走带向量矩阵的 hydrate。
    assert not [s for s in probe.steps if s[0] == "hydrate"]
    assert sorted(s[1] for s in probe.steps if s[0] == "hydrate_texts") == [
        ("a1", "a2", "a3"), ("b2", "b1"), ("c1",),
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
        ("chunk_fts", "nb-a", _NEEDLE, _RECALL, ("src-nb-a",), ["en", "zh"],
         {"trip_circuit": False}),
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
    # 检索时刻取证只覆盖交给调用方的那批段落(codex #787 r1),与回执无关。
    assert receipts.evidence == [{"el-a1": ("src-a1", "fp-el-a1")}]
    assert receipts.groups == [[]]
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
    # 关键词腿的词法超时归 ``timeout``,不是 ``unavailable``。
    reasons = {
        event["notebook_id"]: event.get("reason")
        for event in probe.events
        if event.get("kind") == "chunk_federation_skipped"
    }
    assert reasons == {
        "nb-b": "timeout", "nb-c": "timeout", "nb-d": "unavailable",
    }


def test_the_lexical_timeout_reclassification_is_keyword_arm_only():
    """语义腿的分类不变:同一个 ``ChunkLexicalSearchTimeout`` 在语义腿上不走这条。"""
    import contextvars

    from app.services import chunk_federation as cf

    def _task(arm):
        return cf._Task("nb-a", "personal", "q", contextvars.copy_context(),
                        True, (), None, arm)

    timeout = ChunkLexicalSearchTimeout()
    assert cf._keyword_lexical_timeout(_task("keyword"), timeout) is True
    assert cf._keyword_lexical_timeout(_task(""), timeout) is False
    assert cf._keyword_lexical_timeout(_task("keyword"), RuntimeError()) is False


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


# ---------------------------------------------------------------------------
# 5. 共享的按库 FTS 熔断:关键词腿只遵守、不触发
# ---------------------------------------------------------------------------

class RealFtsProbe(KeywordProbe):
    """真实的 ``_chunk_fts_hits``(连同它读写的真实 ``RetrievalRunState`` 熔断),
    底下只换掉适配器的 ``chunk_fts_search``。``timeouts`` 里的库第一次查询超时。"""

    _chunk_fts_hits = _borrow("_chunk_fts_hits")

    def __init__(self, participants, *, timeouts=(), **kwargs):
        super().__init__(participants, **kwargs)
        self._timeouts = set(timeouts)
        self.searches: list = []
        self.knowledge = SimpleNamespace(chunk_fts_search=self._search)

    def _search(self, db, notebook_id, query, *, k, allowed_source_ids=None,
                corpus_langs=None):
        with self._lock:
            self.searches.append(notebook_id)
        if notebook_id in self._timeouts:
            self._timeouts.discard(notebook_id)
            raise ChunkLexicalSearchTimeout()
        return [{"chunk_id": chunk_id}
                for chunk_id, _rel in self._hits.get(notebook_id, [])]


def test_a_keyword_leg_timeout_does_not_open_the_semantic_fts_circuit():
    """P1:关键词腿超时后,同一 run 里同库语义侧的下一次 FTS 仍真的发出查询。"""
    probe = RealFtsProbe(
        ("nb-a", "nb-b"),
        hits={"nb-a": [("a1", 0.9)], "nb-b": [("b1", 0.9)]},
        timeouts={"nb-a"},
    )

    with _peer_run(("nb-a", "nb-b")):
        merged = probe._keyword_chunk_candidates("nb-a", _NEEDLE)
        assert _ids(merged) == [("nb-b", "b1")]
        assert sorted(probe.searches) == ["nb-a", "nb-b"]
        # 语义腿的调用形状:不带 ``trip_circuit``。
        semantic = probe._chunk_fts_hits("db", "nb-a", "sub query", k=5)

    assert semantic == [{"chunk_id": "a1"}]
    assert sorted(probe.searches) == ["nb-a", "nb-a", "nb-b"]
    assert not [
        event for event in probe.events
        if event.get("status") == "skipped_circuit_open"
    ]


def test_a_semantic_timeout_still_opens_the_circuit_and_keyword_legs_obey_it():
    """对照臂:语义侧超时照旧打开熔断;已打开的熔断关键词腿同样遵守、不发查询。"""
    probe = RealFtsProbe(
        ("nb-a", "nb-b"),
        hits={"nb-a": [("a1", 0.9)], "nb-b": [("b1", 0.9)]},
        timeouts={"nb-a"},
    )

    with _peer_run(("nb-a", "nb-b")):
        with pytest.raises(ChunkLexicalSearchTimeout):
            probe._chunk_fts_hits("db", "nb-a", "sub query", k=5)
        merged = probe._keyword_chunk_candidates("nb-a", _NEEDLE)

    assert _ids(merged) == [("nb-b", "b1")]
    assert sorted(probe.searches) == ["nb-a", "nb-b"]
    assert [
        event["notebook_id"] for event in probe.events
        if event.get("status") == "skipped_circuit_open"
    ] == ["nb-a"]


# ---------------------------------------------------------------------------
# 6. 整条臂的时限:一份逐库预算,不是整段阶段时限
# ---------------------------------------------------------------------------

class _Clock:
    """贴着真钟、整数基准的注入时钟(与 ``test_federated_global_budget`` 同形)。"""

    def __init__(self):
        import math
        import time

        self.base = float(math.floor(time.monotonic()))
        self.offset = 0.0

    def now(self) -> float:
        return self.base + self.offset


def test_keyword_arm_deadline_is_one_notebook_budget_capped_by_the_phase(monkeypatch):
    import time

    from app.services import chunk_federation as cf

    clock = _Clock()
    monkeypatch.setattr(cf, "time", SimpleNamespace(
        monotonic=clock.now, perf_counter=time.perf_counter,
    ))

    def _plan(phase, notebook):
        return SimpleNamespace(
            phase_timeout_seconds=phase, notebook_timeout_seconds=notebook,
        )

    assert cf._keyword_arm_deadline(None) == 0.0
    assert cf._keyword_arm_deadline(_plan(60, 5)) == clock.base + 5
    assert cf._keyword_arm_deadline(_plan(3, 5)) == clock.base + 3


def test_a_leg_started_late_is_still_bounded_by_the_arm_deadline(monkeypatch):
    """准备期用掉 3 秒后才起跑的腿,预算止于「臂起点 + 逐库预算」,而不是
    「起跑时刻 + 逐库预算」——整条臂按一份逐库预算计,不吃满阶段时限。"""
    import time

    from app.repositories.read_budget import current_read_budget
    from app.services import chunk_federation as cf

    clock = _Clock()
    monkeypatch.setattr(cf, "time", SimpleNamespace(
        monotonic=clock.now, perf_counter=time.perf_counter,
    ))
    budgets: list = []

    class _Probe(KeywordProbe):
        def _chunk_fts_hits(self, *args, **kwargs):
            budgets.append(current_read_budget().deadline)
            return super()._chunk_fts_hits(*args, **kwargs)

    probe = _Probe(("nb-a",), hits={"nb-a": [("a1", 0.9)]})

    def _visible(notebook_id):
        # 准备期的逐库读取:这里让时钟走 3 秒。
        clock.offset += 3.0
        return ("src-nb-a",)

    probe.sources = SimpleNamespace(all_visible_source_ids=_visible)
    receipts = Receipts()
    with _global_run(("nb-a",), receipts):
        merged = probe._keyword_chunk_candidates("nb-a", _NEEDLE)

    assert _ids(merged) == [("nb-a", "a1")]
    # ``_global_run`` 的计划:阶段 60 秒、逐库 5 秒。
    assert budgets == [clock.base + 5.0]


# ---------------------------------------------------------------------------
# 7. 调用方层:reasoning 外层不持槽;chunk 模式入口剔除天花板外的命中
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("peer", [True, False])
def test_reasoning_keyword_chunks_holds_no_outer_slot_in_peer_mode(peer):
    """``fanout_limit=1`` 下外层若持着唯一的槽,内层联邦腿取槽就会自锁。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    from app.services.retrieval_run import current_retrieval_run

    observed: list = []

    def _keyword_chunk_candidates(notebook_id, keywords):
        semaphore = current_retrieval_run()._fanout
        free = semaphore.acquire(blocking=False)
        if free:
            semaphore.release()
        observed.append(free)
        return ["hit"]

    retriever = object.__new__(ReasoningRetriever)
    retriever.retrieval = SimpleNamespace(
        keyword_chunk_candidates=_keyword_chunk_candidates,
    )
    retriever._filter_candidates = lambda kind, hits: list(hits)

    with retrieval_run(run_kind="ask_chunk", actor_id=_ACTOR, fanout_limit=1):
        scope = (
            source_scope_context(
                "nb-a", None, None,
                notebook_source_ceilings=_ceilings(("nb-a", "nb-b")),
                subjectless=True,
            ) if peer else contextlib.nullcontext()
        )
        with scope:
            assert retriever.keyword_chunks("nb-a", _NEEDLE) == ["hit"]

    # 对等:槽是空的(外层没取);单库:外层照旧持着那一个槽。
    assert observed == [peer]


def test_chunk_mode_entry_returns_per_library_hits_and_drops_unchecked_sources():
    """经 ``RetrievalService.keyword_chunk_candidates``(chunk 模式调用方的入口),
    在真实 ``global_ask_run`` 下拿到逐库命中;不在该库冻结天花板里的来源被剔除。

    替身 FTS 故意不按天花板下推(替身的 ``score_chunks`` 把 ``src-<chunk_id>``
    当来源),以证明结果边界那道 ``filter_retrieval_items`` 兜得住。"""
    from app.services.retrieval_service import RetrievalService

    probe = KeywordProbe(
        ("nb-a", "nb-b"),
        hits={
            # 来源 src-nb-a / src-nb-b 在各自天花板里;src-a-stray 不在。
            "nb-a": [("nb-a", 0.9), ("a-stray", 0.8)],
            "nb-b": [("nb-b", 0.7)],
        },
    )
    service = SimpleNamespace(candidates=probe)

    with _global_run(("nb-a", "nb-b"), Receipts()):
        hits = RetrievalService.keyword_chunk_candidates(service, "nb-a", _NEEDLE)

    assert _ids(hits) == [("nb-a", "nb-a"), ("nb-b", "nb-b")]
    assert sorted(call[1] for call in probe.fts) == ["nb-a", "nb-b"]


# ---------------------------------------------------------------------------
# 8. 检索时刻取证(codex #787 r1):关键词段落登记证据,但不发覆盖回执
# ---------------------------------------------------------------------------

def _run_state_plan_sink(state):
    """把计划的三条回传接到真实 ``global_ask._RunState`` 上(回执另记)。"""
    receipts: list = []
    return SimpleNamespace(
        on_library=lambda nid, outcome: receipts.append((nid, outcome)),
        on_evidence=state.record_evidence,
        on_evidence_groups=state.record_evidence_groups,
        receipts=receipts,
    )


def test_keyword_only_passages_publish_retrieval_time_evidence_without_receipts():
    receipts = Receipts()
    probe = KeywordProbe(
        ("nb-a", "nb-b"),
        hits={"nb-a": [("a1", 0.9), ("a2", 0.8)], "nb-b": [("b1", 0.7)]},
        limit=2,
    )
    probe.elements = {"a1": ("el-a1-0", "el-a1-1")}

    with _global_run(("nb-a", "nb-b"), receipts):
        merged = probe._keyword_chunk_candidates("nb-a", _NEEDLE)

    # 登记的恰是交给调用方的那批(封顶后 a2 不在其中),一次读。
    assert _ids(merged) == [("nb-a", "a1"), ("nb-b", "b1")]
    assert probe.snapshot_reads == [["a1", "b1"]]
    assert receipts.evidence == [{
        "el-a1-0": ("src-a1", "fp-el-a1-0"),
        "el-a1-1": ("src-a1", "fp-el-a1-1"),
        "el-b1": ("src-b1", "fp-el-b1"),
    }]
    assert receipts.groups == [[("el-a1-0", "el-a1-1")]]
    # 覆盖回执只由语义腿决定。
    assert receipts.libraries == []


def test_unreadable_keyword_evidence_fails_closed():
    receipts = Receipts()
    probe = KeywordProbe(("nb-a",), hits={"nb-a": [("a1", 0.9)]})
    probe.passage_error = RuntimeError("store down")

    with _global_run(("nb-a",), receipts):
        merged = probe._keyword_chunk_candidates("nb-a", _NEEDLE)

    assert _ids(merged) == [("nb-a", "a1")]
    assert receipts.evidence == [{"el-a1": None}]
    assert receipts.libraries == []


def test_keyword_registration_never_overwrites_a_semantic_snapshot():
    """同一 element:语义腿先登记真实快照,关键词腿后登记(这里读失败 → None),
    ``_RunState.record_evidence`` 的有方向合并保住前者。"""
    from app.services.global_ask import _RunState

    state = _RunState(("nb-a",))
    state.record_evidence({"el-a1": ("src-a1", "fp-semantic")})
    probe = KeywordProbe(("nb-a",), hits={"nb-a": [("a1", 0.9)]})
    probe.passage_error = RuntimeError("store down")

    with _global_run(("nb-a",), _run_state_plan_sink(state)):
        probe._keyword_chunk_candidates("nb-a", _NEEDLE)

    assert state.evidence == {"el-a1": ("src-a1", "fp-semantic")}


def test_no_plan_registers_no_keyword_evidence():
    probe = KeywordProbe(("nb-a", "nb-b"), hits={"nb-a": [("a1", 0.9)]})

    with _peer_run(("nb-a", "nb-b")):
        merged = probe._keyword_chunk_candidates("nb-a", _NEEDLE)

    assert _ids(merged) == [("nb-a", "a1")]
    assert probe.snapshot_reads == []


def test_a_keyword_only_citation_is_refused_after_its_source_is_reingested():
    """贴近真实:只被关键词臂召回的段落被引用;合成前来源被重新入库(同一 element
    id、指纹变化)→ 引用复核拒绝它。对照:同一变化在没有检索时刻快照时会被放行——
    这正是修复前的洞。"""
    from app.services.global_ask import GlobalAskService, _RunState

    state = _RunState(("nb-a",))
    # chunk id 取 ``nb-a``,于是替身给它的来源是 ``src-nb-a``,在该库天花板内。
    probe = KeywordProbe(("nb-a",), hits={"nb-a": [("nb-a", 0.9)]})
    with _global_run(("nb-a",), _run_state_plan_sink(state)):
        merged = probe._keyword_chunk_candidates("nb-a", _NEEDLE)
    assert _ids(merged) == [("nb-a", "nb-a")]
    assert state.evidence == {"el-nb-a": ("src-nb-a", "fp-el-nb-a")}

    live = {"el-nb-a": ("src-nb-a", "fp-after-reingest")}
    service = object.__new__(GlobalAskService)
    service.sources = SimpleNamespace(
        evidence_fingerprints=lambda ids: {i: live[i] for i in ids if i in live},
        all_visible_source_ids=lambda nid: ("src-nb-a",),
    )
    service.settings = SimpleNamespace(global_ask_notebook_timeout_seconds=5.0)
    citation = SimpleNamespace(
        element_id="el-nb-a", notebook_id="nb-a", source_id="src-nb-a",
        tier="personal", url="",
    )
    ceiling = {"nb-a": {"src-nb-a"}}

    assert service._validate_citations(
        [citation], state.evidence, ceiling, siblings=state.siblings,
    ) == "changed"
    # 对照:缺快照(修复前的状态)时,同源的指纹变化被当成非联邦证据放行。
    assert service._validate_citations([citation], {}, ceiling) == ""
    # 对照:未被改动时照常通过。
    live["el-nb-a"] = ("src-nb-a", "fp-el-nb-a")
    assert service._validate_citations(
        [citation], state.evidence, ceiling, siblings=state.siblings,
    ) == ""

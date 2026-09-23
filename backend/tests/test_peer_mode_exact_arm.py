"""对等(全局)模式下精确标识符臂的逐库联邦化合同。

``_exact_lookup_chunks`` 在对等模式下不再整条返回 ``[]``:问题里有可探测名称时,对
每个参与库各跑一次**同一套**单库精确查找(``exact_lookup.exact_lookup_sections``),
借语义腿的联邦调度(``chunk_federation._run_supplement_arm``),按库以**整节**轮转
交错、去重、以 ``GLOBAL_ASK_CANDIDATE_LIMIT`` 封顶且不切断一节。本文件钉住:

* 每个库各查一次、各自冻结天花板;某库来源漂移(真正收窄)只跳过该库;
* 无标识符零开销:不读参与集座位、不派任务、不发事件;
* 合并以整节为单位轮转、去重、封顶不切节;命中保留 ``exact_lookup`` 与所属库;
* 失败/超时 fail-open:不抛、不进回执、不上横幅;取消照常上抛;
* 检索时刻证据登记(含读失败 fail closed、无计划不登记);
* 开关;单库路径调用序列守卫;reasoning 外层不持槽;
* 两个按库席位纯函数(``exact_section_reserve_rules`` 与
  ``promote_bounded_prefix_by_library``)的单库等价与 k=2/3/5、余数分配。

替身只实现精确查找走到存储所需的最小面,被测方法从 ``CandidateRetrievalService``
上按原样借过来;存储三件套(``chunk_exact_search`` / ``chunks_by_section`` /
``hydrate_rows``)按预置小节数据回答并逐步记账。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.domain.retrieval import RetrievedChunk
from app.models.source_scope import SourceScope
from app.repositories.read_budget import ReadBudgetExceeded
from app.services import chunk_federation as cf
from app.services.cancellation import AskCancelled
from app.services.federated_run import DetachedAskTurn, FederatedRunPlan
from app.services.global_run import global_ask_run
from app.services.retrieval import (
    exact_section_reserve_rule,
    exact_section_reserve_rules,
    library_seats,
    promote_bounded_prefix,
    promote_bounded_prefix_by_library,
)
from app.services.retrieval_candidates import CandidateRetrievalService
from app.services.retrieval_participants import (
    ParticipantOverride, participant_override,
)
from app.services.retrieval_run import retrieval_run
from app.services.source_scope import source_scope_context, source_scope_restricted


_ACTOR = "user-peer-exact"
_QUERY = "set_db 与 report_timing 的参数"
_PLAIN = "低噪声放大器的增益是多少"
_CANDIDATE_LIMIT = 64

# 每库的小节:(section_path, [chunk_id, ...], source_id 或 None → src-<库>)。
_SECTIONS = {
    "nb-a": [("Cmds > set_db", ["a1", "a2", "a3"], None),
             ("Cmds > report_timing", ["a4"], None)],
    "nb-b": [("Ref > set_db", ["b1", "b2"], None)],
    "nb-c": [("Ref > report_timing", ["c1"], None)],
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _borrow(name: str):
    return getattr(CandidateRetrievalService, name)


class ExactProbe:
    """``CandidateRetrievalService`` 精确查找臂的最小替身,逐步记账。"""

    _exact_lookup_chunks = _borrow("_exact_lookup_chunks")
    _exact_lookup_chunks_one = _borrow("_exact_lookup_chunks_one")
    _peer_exact_lookup_chunks = _borrow("_peer_exact_lookup_chunks")
    _peer_exact_leg = _borrow("_peer_exact_leg")
    _peer_exact_scope_drifted = _borrow("_peer_exact_scope_drifted")
    _exact_lookup_deps = _borrow("_exact_lookup_deps")
    _exact_lookup_limits = _borrow("_exact_lookup_limits")

    def __init__(self, participants=("nb-a",), *, sections=None, visible=None,
                 failures=None, limit=_CANDIDATE_LIMIT, enabled=True,
                 lookup_enabled=True):
        self._participants = tuple((nid, "personal") for nid in participants)
        self._sections = sections if sections is not None else {
            nid: _SECTIONS.get(nid, []) for nid in participants
        }
        self._failures = dict(failures or {})
        self._visible = visible if visible is not None else {
            nid: (f"src-{nid}",) for nid in participants
        }
        self.settings = SimpleNamespace(
            chunk_federation_enabled=True,
            chunk_federation_max_participants=8,
            chunk_fanout_max_workers=4,
            global_ask_candidate_limit=limit,
            global_ask_exact_arm_enabled=enabled,
            exact_lookup_enabled=lookup_enabled,
            exact_lookup_max_identifiers=3,
            exact_lookup_fts_k=50,
            exact_lookup_max_sections=3,
            exact_lookup_max_chunks_per_section=12,
        )
        self.events: list = []
        self.event_log = SimpleNamespace(emit=self.events.append)
        self.steps: list = []
        self.model_errors: list = []
        self.seat_reads = 0
        self.visible_reads: list = []
        self._lock = threading.Lock()
        self.snapshot_reads: list = []
        self.passage_error = None
        self.sources = SimpleNamespace(
            all_visible_source_ids=self._all_visible,
            passage_evidence_snapshot=self._passage_evidence_snapshot,
        )
        self.knowledge = SimpleNamespace(chunk_exact_search=self._exact_search)
        self.chunks = SimpleNamespace(
            chunks_by_section=self._section_rows, hydrate_rows=self._hydrate_rows,
        )

    # --- 参与集座位 ---------------------------------------------------------
    def _retrieval_participants(self, active_notebook_id):
        self.seat_reads += 1
        return self._participants

    def notebook_copy_stats(self, notebook_id):
        return {"copyable": True}

    def _all_visible(self, notebook_id):
        with self._lock:
            self.visible_reads.append(notebook_id)
        return self._visible.get(notebook_id, ())

    # --- 单库路径的来源闸 ---------------------------------------------------
    def _record(self, step):
        with self._lock:
            self.steps.append(step)

    def _unsafe_source_scope_restricted(self, notebook_id):
        verdict = source_scope_restricted()
        self._record(("restricted_probe", notebook_id, verdict))
        return verdict

    def _connect(self):
        return contextlib.nullcontext("db")

    # --- 存储三件套 ---------------------------------------------------------
    def _chunk(self, notebook_id):
        for path, chunk_ids, source in self._sections.get(notebook_id, []):
            for chunk_id in chunk_ids:
                yield path, chunk_id, source or f"src-{notebook_id}"

    def _row(self, notebook_id, path, chunk_id, source):
        return {
            "id": chunk_id, "source_id": source, "text": f"text-{chunk_id}",
            "section_path": path, "source_title": "t",
            "element_ids": json.dumps([f"el-{chunk_id}"]),
        }

    def _exact_search(self, db, notebook_id, term, k):
        self._record(("exact_search", notebook_id, term, k))
        failure = self._failures.get(notebook_id)
        if failure is not None:
            raise failure
        return [
            {"chunk_id": chunk_id, "source_id": source, "section_path": path}
            for path, chunk_id, source in self._chunk(notebook_id)
            if term in path
        ][:k]

    def _section_rows(self, db, notebook_id, source_id, section_path, limit):
        self._record(("section_rows", notebook_id, section_path, limit))
        return [
            self._row(notebook_id, path, chunk_id, source)
            for path, chunk_id, source in self._chunk(notebook_id)
            if source == source_id and (
                path == section_path or path.startswith(section_path + " > "))
        ][:limit]

    def _hydrate_rows(self, db, chunk_ids):
        self._record(("hydrate_rows", tuple(chunk_ids)))
        rows = {
            chunk_id: self._row(nid, path, chunk_id, source)
            for nid in self._sections
            for path, chunk_id, source in self._chunk(nid)
        }
        return [rows[cid] for cid in chunk_ids if cid in rows]

    # --- 检索时刻取证 -------------------------------------------------------
    def _passage_evidence_snapshot(self, chunk_ids):
        with self._lock:
            self.snapshot_reads.append(list(chunk_ids))
        if self.passage_error is not None:
            raise self.passage_error
        return {
            cid: {
                "text_sha": _sha(f"text-{cid}"),
                "elements": {f"el-{cid}": (f"src-{cid}", f"fp-el-{cid}")},
            }
            for cid in chunk_ids
        }

    def _note_model_error(self, stage, model, exc=None, *args, **kwargs):
        with self._lock:
            self.model_errors.append((stage, type(exc).__name__))

    def summary(self):
        rows = [event for event in self.events
                if event.get("stage") == "global_exact_arm"]
        assert len(rows) == 1, self.events
        return rows[0]

    def searched(self):
        return sorted({step[1] for step in self.steps if step[0] == "exact_search"})


def _ceilings(notebook_ids) -> dict:
    return {nid: {f"src-{nid}"} for nid in notebook_ids}


def _override(notebook_ids) -> ParticipantOverride:
    return ParticipantOverride(
        notebook_ids=tuple(notebook_ids), tiers={}, attested_actor_id=_ACTOR,
    )


@contextlib.contextmanager
def _peer_run(notebook_ids):
    """覆盖 + 逐库天花板 + 无主体位,没有运行计划。"""
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
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="exact-test")
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
                DetachedAskTurn(conversation_id="conv-exact"), plan,
                nominal_active=ids[0],
            ):
                yield ids
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def _ids(hits):
    return [(hit.notebook_id, hit.chunk_id) for hit in hits]


_ABC = ("nb-a", "nb-b", "nb-c")


# ---------------------------------------------------------------------------
# 1. 每个库各一次、各自天花板;整节轮转合并
# ---------------------------------------------------------------------------

def test_each_participant_is_looked_up_once_and_merged_section_by_section():
    probe = ExactProbe(_ABC)

    with _peer_run(_ABC):
        merged = probe._exact_lookup_chunks("nb-a", _QUERY)

    # 每库每个名称恰好一次探测(上界同单库路径:fts_k=50)。
    assert sorted(s for s in probe.steps if s[0] == "exact_search") == [
        ("exact_search", nid, term, 50)
        for nid in _ABC for term in ("report_timing", "set_db")
    ]
    # 按库以整节轮转:a 的第一节、b 的第一节、c 的第一节,再轮到 a 的第二节。
    assert _ids(merged) == [
        ("nb-a", "a1"), ("nb-a", "a2"), ("nb-a", "a3"),
        ("nb-b", "b1"), ("nb-b", "b2"),
        ("nb-c", "c1"),
        ("nb-a", "a4"),
    ]
    assert all(hit.exact_lookup for hit in merged)
    summary = probe.summary()
    assert summary == {
        "kind": "ask_stage",
        "stage": "global_exact_arm",
        "site": "global_exact_arm",
        "participants": 3,
        "libraries_with_hits": 3,
        "merged": 7,
        "sections": 4,
        "failed_libraries": 0,
        "latency_ms": summary["latency_ms"],
    }
    # 内容无关:标识符、问题、路径都不进任何事件。
    assert all(
        "set_db" not in repr(event) and "report_timing" not in repr(event)
        and "Cmds" not in repr(event)
        for event in probe.events
    )
    # 对等模式按库判漂移(每库一次现读),不走名义 active 的单库闸。
    assert not [s for s in probe.steps if s[0] == "restricted_probe"]
    assert probe.visible_reads.count("nb-b") >= 1


def test_the_merge_never_cuts_a_section_and_stops_at_the_first_that_does_not_fit():
    probe = ExactProbe(_ABC, limit=4)

    with _peer_run(_ABC):
        merged = probe._exact_lookup_chunks("nb-a", _QUERY)

    # a 的整节(3)放得下;b 的整节(2)放不下 → 停,即使后面 c 的一块本来放得下。
    assert _ids(merged) == [("nb-a", "a1"), ("nb-a", "a2"), ("nb-a", "a3")]
    assert (probe.summary()["merged"], probe.summary()["sections"]) == (3, 1)


def test_interleave_sections_dedups_across_libraries_and_skips_emptied_sections():
    def hit(chunk_id):
        return SimpleNamespace(chunk_id=chunk_id)

    columns = [
        [[hit("x"), hit("a1")], [hit("a2")]],
        [[hit("x"), hit("x")], [hit("b2"), hit("a2")]],
    ]
    merged, sections = cf._interleave_sections_capped(columns, 10)
    # b 的第一节去重后为空 → 跳过、不计节;b 的第二节里已被 a 取走的 a2 被剔除。
    assert [h.chunk_id for h in merged] == ["x", "a1", "a2", "b2"]
    assert sections == 3
    assert cf._interleave_sections_capped([], 5) == ([], 0)


def test_a_one_library_peer_set_still_takes_the_federated_branch():
    probe = ExactProbe(("nb-b",))

    with _peer_run(("nb-b",)):
        merged = probe._exact_lookup_chunks("nb-b", _QUERY)

    assert _ids(merged) == [("nb-b", "b1"), ("nb-b", "b2")]
    assert probe.summary()["participants"] == 1


# ---------------------------------------------------------------------------
# 2. 无标识符零开销;开关
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("planned", [False, True])
def test_a_question_without_an_identifier_costs_nothing(planned):
    probe = ExactProbe(_ABC)
    run = (_global_run(_ABC, Receipts()) if planned else _peer_run(_ABC))

    with run:
        assert probe._exact_lookup_chunks("nb-a", _PLAIN) == []

    assert probe.steps == []
    assert probe.seat_reads == 0
    assert probe.visible_reads == []
    assert probe.events == []


@pytest.mark.parametrize("switch", ["arm", "lookup"])
def test_either_switch_off_restores_the_empty_peer_arm(switch):
    probe = ExactProbe(
        _ABC, enabled=(switch != "arm"), lookup_enabled=(switch != "lookup"),
    )

    with _peer_run(_ABC):
        assert probe._exact_lookup_chunks("nb-a", _QUERY) == []

    assert probe.steps == [] and probe.seat_reads == 0 and probe.events == []


# ---------------------------------------------------------------------------
# 3. 来源闸按库判;无可见来源的库不发查询
# ---------------------------------------------------------------------------

def test_a_drifted_library_is_skipped_alone():
    """nb-b 冻结后多了一个来源(真正越出天花板):只跳过 nb-b,其余照查。"""
    probe = ExactProbe(
        _ABC,
        visible={"nb-a": ("src-nb-a",), "nb-b": ("src-nb-b", "src-new"),
                 "nb-c": ("src-nb-c",)},
    )

    with _peer_run(_ABC):
        merged = probe._exact_lookup_chunks("nb-a", _QUERY)

    assert probe.searched() == ["nb-a", "nb-c"]
    assert [nid for nid, _cid in _ids(merged)] == [
        "nb-a", "nb-a", "nb-a", "nb-c", "nb-a",
    ]
    summary = probe.summary()
    assert (summary["participants"], summary["libraries_with_hits"],
            summary["failed_libraries"]) == (3, 2, 0)


def test_a_library_with_no_visible_source_issues_no_query():
    probe = ExactProbe(
        ("nb-a", "nb-b"), visible={"nb-a": ("src-nb-a",), "nb-b": ()},
    )

    with _peer_run(("nb-a", "nb-b")):
        merged = probe._exact_lookup_chunks("nb-a", _QUERY)

    assert probe.searched() == ["nb-a"]
    assert {nid for nid, _cid in _ids(merged)} == {"nb-a"}


# ---------------------------------------------------------------------------
# 4. 失败 fail-open:不抛、不进回执、不上横幅;取消照常上抛
# ---------------------------------------------------------------------------

def test_failing_libraries_leave_the_rest_and_never_reach_receipts_or_banner():
    receipts = Receipts()
    probe = ExactProbe(
        _ABC,
        failures={"nb-b": ReadBudgetExceeded(), "nb-c": RuntimeError("boom")},
    )

    with _global_run(_ABC, receipts):
        merged = probe._exact_lookup_chunks("nb-a", _QUERY)

    assert _ids(merged) == [
        ("nb-a", "a1"), ("nb-a", "a2"), ("nb-a", "a3"), ("nb-a", "a4"),
    ]
    assert receipts.libraries == []
    assert probe.model_errors == []
    assert probe.summary()["failed_libraries"] == 2
    reasons = {
        event["notebook_id"]: (event.get("arm"), event.get("reason"))
        for event in probe.events
        if event.get("kind") == "chunk_federation_skipped"
    }
    assert reasons == {
        "nb-b": ("exact", "timeout"), "nb-c": ("exact", "unavailable"),
    }


def test_cancellation_propagates_from_an_exact_leg():
    probe = ExactProbe(("nb-a", "nb-b"), failures={"nb-b": AskCancelled()})

    with _peer_run(("nb-a", "nb-b")):
        with pytest.raises(AskCancelled):
            probe._exact_lookup_chunks("nb-a", _QUERY)
    assert probe.model_errors == []


def test_a_cancelled_global_run_raises_instead_of_answering():
    cancel = threading.Event()
    cancel.set()
    probe = ExactProbe(("nb-a", "nb-b"))

    with _global_run(("nb-a", "nb-b"), Receipts(), cancel=cancel):
        with pytest.raises(AskCancelled):
            probe._exact_lookup_chunks("nb-a", _QUERY)
    assert not [s for s in probe.steps if s[0] == "exact_search"]


def test_an_exact_leg_runs_inside_one_arm_budget(monkeypatch):
    """整条臂一份逐库预算(与关键词臂共用 ``_supplement_arm_deadline``)。"""
    import math
    import time

    from app.repositories.read_budget import current_read_budget

    base = float(math.floor(time.monotonic()))
    monkeypatch.setattr(cf, "time", SimpleNamespace(
        monotonic=lambda: base, perf_counter=time.perf_counter,
    ))
    budgets: list = []

    class _Probe(ExactProbe):
        def _exact_search(self, db, notebook_id, term, k):
            budgets.append(current_read_budget().deadline)
            return super()._exact_search(db, notebook_id, term, k)

    probe = _Probe(("nb-b",))
    with _global_run(("nb-b",), Receipts()):
        probe._exact_lookup_chunks("nb-b", _QUERY)

    assert budgets and set(budgets) == {base + 5.0}


# ---------------------------------------------------------------------------
# 5. 检索时刻取证
# ---------------------------------------------------------------------------

def test_exact_passages_publish_retrieval_time_evidence_without_receipts():
    receipts = Receipts()
    probe = ExactProbe(_ABC, limit=5)

    with _global_run(_ABC, receipts):
        merged = probe._exact_lookup_chunks("nb-a", _QUERY)

    # 登记的恰是交给调用方的那批(封顶后 c1/a4 不在其中),一次读。
    assert [cid for _nid, cid in _ids(merged)] == ["a1", "a2", "a3", "b1", "b2"]
    assert probe.snapshot_reads == [["a1", "a2", "a3", "b1", "b2"]]
    assert receipts.evidence == [{
        f"el-{cid}": (f"src-{cid}", f"fp-el-{cid}")
        for cid in ("a1", "a2", "a3", "b1", "b2")
    }]
    assert receipts.libraries == []


def test_unreadable_exact_evidence_fails_closed():
    receipts = Receipts()
    probe = ExactProbe(("nb-b",))
    probe.passage_error = RuntimeError("store down")

    with _global_run(("nb-b",), receipts):
        merged = probe._exact_lookup_chunks("nb-b", _QUERY)

    assert _ids(merged) == [("nb-b", "b1"), ("nb-b", "b2")]
    assert receipts.evidence == [{"el-b1": None, "el-b2": None}]
    assert receipts.libraries == []


def test_no_plan_registers_no_exact_evidence():
    probe = ExactProbe(("nb-a", "nb-b"))

    with _peer_run(("nb-a", "nb-b")):
        assert probe._exact_lookup_chunks("nb-a", _QUERY)

    assert probe.snapshot_reads == []


# ---------------------------------------------------------------------------
# 6. 单库路径守卫
# ---------------------------------------------------------------------------

def _single_path_steps(narrowed: bool) -> list:
    """改动前那条函数体发出的逐步调用,一字不差。"""
    if narrowed:
        return [("restricted_probe", "nb-a", True)]
    return [
        ("restricted_probe", "nb-a", False),
        ("exact_search", "nb-a", "set_db", 50),
        ("exact_search", "nb-a", "report_timing", 50),
        ("section_rows", "nb-a", "Cmds > set_db", 12),
        ("section_rows", "nb-a", "Cmds > report_timing", 12),
    ]


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("scope", ["none", "frozen_all", "narrowed"])
def test_single_notebook_path_is_unchanged(scope, enabled):
    """非对等(含挂载参考库、含冻结/收窄勾选):探针顺序与存储调用同改动前,
    参与集座位与联邦开关都不读,不发事件,命中不打库标。"""
    probe = ExactProbe(
        ("nb-a", "nb-ref-1"),
        sections={"nb-a": _SECTIONS["nb-a"], "nb-ref-1": _SECTIONS["nb-b"]},
        enabled=enabled,
    )
    local = {
        "none": None,
        "frozen_all": SourceScope(
            mode="include", source_ids=["src-nb-a"], narrowed=False,
        ),
        "narrowed": SourceScope(
            mode="include", source_ids=["src-nb-a"], narrowed=True,
        ),
    }[scope]

    with retrieval_run(run_kind="ask_chunk", actor_id=_ACTOR):
        with source_scope_context("nb-a", local):
            merged = probe._exact_lookup_chunks("nb-a", _QUERY)

    assert probe.steps == _single_path_steps(scope == "narrowed")
    assert probe.seat_reads == 0
    assert probe.visible_reads == []
    assert probe.events == []
    expected = [] if scope == "narrowed" else [
        ("", "a1"), ("", "a2"), ("", "a3"), ("", "a4"),
    ]
    assert _ids(merged) == expected


def test_single_notebook_failure_keeps_its_historical_handling():
    broken = ExactProbe(failures={"nb-a": RuntimeError("boom")})
    assert broken._exact_lookup_chunks("nb-a", _QUERY) == []
    assert broken.model_errors == [("chunk_exact_lookup", "RuntimeError")]


# ---------------------------------------------------------------------------
# 7. 调用方层:reasoning 外层不持槽;chunk 模式入口剔除天花板外的命中
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("peer", [True, False])
def test_reasoning_exact_lookup_holds_no_outer_slot_in_peer_mode(peer):
    from app.services.reasoning_retrieval import ReasoningRetriever
    from app.services.retrieval_run import current_retrieval_run

    observed: list = []

    def _exact_lookup_chunks(notebook_id, query):
        semaphore = current_retrieval_run()._fanout
        free = semaphore.acquire(blocking=False)
        if free:
            semaphore.release()
        observed.append(free)
        return ["hit"]

    retriever = object.__new__(ReasoningRetriever)
    retriever.retrieval = SimpleNamespace(exact_lookup_chunks=_exact_lookup_chunks)
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
            assert retriever.exact_lookup("nb-a", _QUERY) == ["hit"]

    assert observed == [peer]


def test_chunk_mode_entry_returns_per_library_hits_and_drops_unchecked_sources():
    """经 ``RetrievalService.exact_lookup_chunks`` 在真实 ``global_ask_run`` 下拿到
    逐库命中;小节里来源不在该库冻结天花板内的块被结果边界剔除。"""
    from app.services.retrieval_service import RetrievalService

    probe = ExactProbe(
        ("nb-a", "nb-b"),
        sections={
            "nb-a": [("Cmds > set_db", ["a1"], None),
                     ("Cmds > report_timing", ["stray"], "src-stray")],
            "nb-b": [("Ref > set_db", ["b1"], None)],
        },
    )
    service = SimpleNamespace(candidates=probe)

    with _global_run(("nb-a", "nb-b"), Receipts()):
        hits = RetrievalService.exact_lookup_chunks(service, "nb-a", _QUERY)

    assert _ids(hits) == [("nb-a", "a1"), ("nb-b", "b1")]


# ---------------------------------------------------------------------------
# 8. 按库席位纯函数
# ---------------------------------------------------------------------------

def _chunk(chunk_id, notebook_id="", *, exact=True):
    return RetrievedChunk(
        chunk_id=chunk_id, source_id="s", source_title="t", section_path="",
        text=chunk_id, element_ids=[], score=0.5, relevance=0.5,
        notebook_id=notebook_id, exact_lookup=exact,
    )


@pytest.mark.parametrize("reserve,libraries,expected", [
    (4, ["a"], [("a", 4)]),
    (4, ["a", "b"], [("a", 2), ("b", 2)]),
    (4, ["a", "b", "c"], [("a", 2), ("b", 1), ("c", 1)]),
    (5, ["a", "b", "c"], [("a", 2), ("b", 2), ("c", 1)]),
    (4, ["a", "b", "c", "d", "e"], [("a", 1), ("b", 1), ("c", 1), ("d", 1)]),
    (0, ["a", "b"], []),
    (3, [], []),
])
def test_library_seats_share_the_reserve_and_keep_its_total(
    reserve, libraries, expected,
):
    seats = library_seats(reserve, libraries)
    assert seats == expected
    assert sum(count for _lib, count in seats) == (reserve if libraries else 0)


def _rule_shape(rule, universe):
    return (rule.reserve,
            [c.chunk_id for c in universe if rule.holds(c)],
            [c.chunk_id for c in universe if rule.admits(c)])


@pytest.mark.parametrize("stamp", ["", "nb-a"])
@pytest.mark.parametrize("reserve", [0, 1, 4, 7])
def test_exact_reserve_rules_single_library_is_the_historical_rule(stamp, reserve):
    hits = [_chunk(f"c{i}", stamp) for i in range(6)]
    universe = hits + [_chunk("other", stamp), _chunk("x", "nb-z")]
    rules = exact_section_reserve_rules(reserve, hits)
    historical = exact_section_reserve_rule(reserve, {c.chunk_id for c in hits})
    assert len(rules) == 1
    assert _rule_shape(rules[0], universe) == _rule_shape(historical, universe)
    # 空命中:同一条惰性规则。
    assert _rule_shape(exact_section_reserve_rules(reserve, [])[0], universe) == (
        reserve, [], [],
    )


@pytest.mark.parametrize("k,expected_seats", [
    (2, [2, 2]),
    (3, [2, 1, 1]),
    (5, [1, 1, 1, 1]),
])
def test_exact_reserve_rules_split_per_library(k, expected_seats):
    libraries = [f"nb-{i}" for i in range(k)]
    hits = [_chunk(f"{nid}-{j}", nid) for nid in libraries for j in range(3)]
    rules = exact_section_reserve_rules(4, hits)
    assert [rule.reserve for rule in rules] == expected_seats
    assert sum(rule.reserve for rule in rules) == 4
    for rule, nid in zip(rules, libraries):
        assert [c.chunk_id for c in hits if rule.admits(c)] == [
            f"{nid}-{j}" for j in range(3)
        ]


def test_exact_reserve_rules_remainder_goes_to_the_library_seen_first():
    hits = [_chunk("b1", "nb-b"), _chunk("a1", "nb-a"), _chunk("c1", "nb-c"),
            _chunk("a2", "nb-a")]
    rules = exact_section_reserve_rules(4, hits)
    assert [(r.reserve, [c.chunk_id for c in hits if r.holds(c)])
            for r in rules] == [(2, ["b1"]), (1, ["a1", "a2"]), (1, ["c1"])]


def _marked(chunk):
    return chunk.exact_lookup


@pytest.mark.parametrize("reserve", [0, 1, 2, 4, 9])
@pytest.mark.parametrize("stamp", ["", "nb-a"])
def test_promote_by_library_single_library_equals_the_historical_prefix(
    reserve, stamp,
):
    chunks = [
        _chunk("p1", stamp, exact=False), _chunk("e1", stamp),
        _chunk("p2", stamp, exact=False), _chunk("e2", stamp),
        _chunk("e3", stamp), _chunk("p3", "nb-other", exact=False),
    ]
    assert promote_bounded_prefix_by_library(chunks, _marked, reserve) == (
        promote_bounded_prefix(chunks, _marked, reserve)
    )
    none = [_chunk("p1", exact=False), _chunk("p2", "nb-b", exact=False)]
    assert promote_bounded_prefix_by_library(none, _marked, reserve) == none


@pytest.mark.parametrize("k,expected", [
    (2, ["nb-0-e0", "nb-1-e0", "nb-0-e1", "nb-1-e1"]),
    (3, ["nb-0-e0", "nb-1-e0", "nb-2-e0", "nb-0-e1"]),
    (5, ["nb-0-e0", "nb-1-e0", "nb-2-e0", "nb-3-e0"]),
])
def test_promote_by_library_shares_the_prefix(k, expected):
    # 相关度序:先是每库一块、再每库第二块……;库 0 的块排最前 → 余数归它。
    chunks = [_chunk("p0", exact=False)] + [
        _chunk(f"nb-{i}-e{j}", f"nb-{i}") for j in range(3) for i in range(k)
    ]
    out = promote_bounded_prefix_by_library(chunks, _marked, 4)
    promoted = [c.chunk_id for c in out[:4]]
    assert sorted(promoted) == sorted(expected)
    # 稳定的重排:被提前的保持相对顺序,其余保持相对顺序,一块不丢。
    assert promoted == [c.chunk_id for c in chunks if c.chunk_id in expected]
    assert sorted(c.chunk_id for c in out) == sorted(c.chunk_id for c in chunks)
    assert [c.chunk_id for c in out[4:]] == [
        c.chunk_id for c in chunks if c.chunk_id not in expected
    ]


def test_promote_by_library_does_not_let_one_library_take_every_seat():
    big = [_chunk(f"a{i}", "nb-a") for i in range(6)]
    small = [_chunk("b0", "nb-b")]
    out = promote_bounded_prefix_by_library(big + small, _marked, 4)
    assert [c.chunk_id for c in out[:3]] == ["a0", "a1", "b0"]
    # 单库形态下同样的输入会把前 4 块 nb-a 全提上来——这正是要防的。
    assert [c.chunk_id for c in promote_bounded_prefix(big + small, _marked, 4)][:4] == [
        "a0", "a1", "a2", "a3",
    ]

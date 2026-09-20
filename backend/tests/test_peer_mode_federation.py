"""D0-4 -- 对等模式下联邦 chunk 通道的分叉合同。

PR-B 给联邦通道确立了「**当前库是主体**」三件套:保底份额、当前库命中不打标、
引用归属按 active 归零。全局问答没有主体库——名义 active 只是
``ParticipantOverride.notebook_ids[0]`` 这个命名锚点,不该有任何检索特权。本文件
钉住那三件套在覆盖在场时的分叉,以及跨库合并阈值换轨。

生产上今天不可达(没有任何地方安装覆盖),所以入口一律是
``retrieval_run(actor_id=…)`` + ``participant_override(…)``。覆盖**缺席**时逐字
不变由既有的 ``test_chunk_federation*.py`` / ``test_search_chunks_federation.py``
零改动全绿承担,本文件只在每条分叉旁补一条对照臂。

替身沿用 ``test_chunk_federation.py`` 的最小形状(不起数据库、不碰 scale
runtime),但 producer 多做一件事:不是 peek-only 时才去 ``_scale_index``——这正是
``_CHUNK_PEEK_ONLY`` 在真实 chunk 通道里管住的那个动作,所以「大库只借不载」才能
被断言,而不是只断言一个 ContextVar 的取值。
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from app.domain.retrieval import RetrievedChunk
from app.services import chunk_federation as cf
from app.services.retrieval_candidates import _CHUNK_PEEK_ONLY, _CHUNK_PEER_LEG
from app.services.retrieval_participants import (
    ParticipantOverride,
    ParticipantOverrideError,
    federated_ask_active,
    participant_override,
)
from app.services.retrieval_run import retrieval_run
from app.services.source_scope import (
    peer_scope_ceiling_active, source_scope_context, subjectless_run_active,
)


_ACTOR = "user-peer"
# 全局栏杆的文档值。这里写成字面量而不是读 ``Settings``:断言要证明的是
# 「``peer_evidence`` 收到的是 GLOBAL_ASK_* 而不是调用方传进来的 0/0」,
# 用同一个来源取值会让这条断言对「两边都读错字段」恒真。
_GLOBAL_MIN = 0.25
_GLOBAL_REL = 0.6
_GLOBAL_BUDGET = 64


class ScaleIndexColdLoaded(AssertionError):
    """替身 producer 在非 peek-only 腿上真的去加载了 scale 索引。"""


def make_chunk(chunk_id: str, relevance: float, text: str = "",
               source_id: str = "") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, source_id=source_id or f"src-{chunk_id}",
        source_title="t", section_path="", text=text or f"text-{chunk_id}",
        score=relevance, relevance=relevance,
    )


class FakeCandidates:
    """``CandidateRetrievalService`` 的最小替身,带一把 scale 索引探针。"""

    def __init__(self, participants, *, retrieve=None, visible=None,
                 copyable=None, multi_result=None, recall=200,
                 active_reserve=0.0, peer_floor=0.5, mmr_k=16,
                 cold_load_fails=False, producer_fails=False):
        self._participants = tuple(participants)
        self._retrieve = retrieve or (
            lambda nid, query: ([make_chunk(f"c-{nid}", 0.9)], [f"c-{nid}"],
                                np.eye(1, 3))
        )
        self._visible = visible or {}
        self._copyable = copyable or {}
        self._cold_load_fails = cold_load_fails
        self._producer_fails = producer_fails
        self.multi_result = multi_result
        self.settings = SimpleNamespace(
            chunk_federation_enabled=True,
            chunk_federation_max_participants=8,
            chunk_fanout_max_workers=4,
            chunk_recall=recall,
            chunk_federation_peer_floor=peer_floor,
            chunk_federation_active_reserve=active_reserve,
            chunk_mmr_k=mmr_k,
            embed_runtime_dim=0,
            global_ask_min_relevance=_GLOBAL_MIN,
            global_ask_relative_relevance=_GLOBAL_REL,
            global_ask_candidate_limit=_GLOBAL_BUDGET,
        )
        self.events: list = []
        self.event_log = SimpleNamespace(emit=self.events.append)
        self.calls: list = []
        self.visible_reads: list = []
        self.scale_loads: list = []
        self.sources = SimpleNamespace(
            all_visible_source_ids=self._all_visible_source_ids,
        )
        self._lock = threading.Lock()

    # --- 参与集座位 -----------------------------------------------------
    def _retrieval_participants(self, active_notebook_id: str):
        return self._participants

    def notebook_copy_stats(self, notebook_id: str) -> dict:
        return {"copyable": self._copyable.get(notebook_id, True)}

    def _all_visible_source_ids(self, notebook_id: str):
        with self._lock:
            self.visible_reads.append(notebook_id)
        return self._visible.get(notebook_id, ())

    def _unsafe_source_scope_restricted(self, notebook_id: str) -> bool:
        return False

    # --- producer -------------------------------------------------------
    def _scale_index(self, notebook_id: str):
        with self._lock:
            self.scale_loads.append(notebook_id)
        if self._cold_load_fails:
            raise ScaleIndexColdLoaded(notebook_id)
        return None

    def _retrieve_chunks(self, notebook_id, query, recall=0, *,
                         allowed_source_ids=None, producer_explicit=False,
                         drifted=None):
        if self._producer_fails:
            raise AssertionError(
                f"producer ran for {notebook_id} before the override pre-check"
            )
        with self._lock:
            self.calls.append({
                "notebook_id": notebook_id, "query": query,
                "allowed_source_ids": allowed_source_ids,
                "producer_explicit": producer_explicit,
                "peer_leg": _CHUNK_PEER_LEG.get(),
            })
        if not _CHUNK_PEEK_ONLY.get():
            # 真实 chunk 腿在这一步才可能冷加载共享 scale 索引;peek-only 的
            # 承诺就是「只借已经暖着的,绝不加载」。
            self._scale_index(notebook_id)
        return self._retrieve(notebook_id, query)

    def _retrieve_chunks_multi(self, notebook_id, sub_queries, *, drifted=None):
        raise AssertionError(
            "single-library short circuit was taken in peer mode"
        )

    def call_for(self, notebook_id: str) -> dict:
        matched = [call for call in self.calls
                   if call["notebook_id"] == notebook_id]
        assert matched, f"{notebook_id} was never searched: {self.calls}"
        return matched[0]


def _override(notebook_ids, *, actor=_ACTOR) -> ParticipantOverride:
    return ParticipantOverride(
        notebook_ids=tuple(notebook_ids), tiers={},
        attested_actor_id=actor,
    )


def _ceilings(notebook_ids) -> dict:
    return {notebook_id: {f"src-{notebook_id}"} for notebook_id in notebook_ids}


class _peer_run:
    """名义 active + 覆盖 + 逐库天花板 + 无主体位,与 PR-D1 的安装形状同形。

    ``source_scope_context`` 只提交第三个维度,所以 ``restricted`` /
    ``ceiling_active`` 恒 False、两份 payload 恒 None——1.5 冻结的那组后果。

    生产上这四件事只由 ``global_run.global_ask_run`` 一次装齐;这里逐件装是为了
    让 ``with_ceilings=False`` / ``with_override=False`` 两条负对照能只拆掉其中
    一件,证明「必须由一个管理器同时安装」不是一句空话。
    """

    def __init__(self, notebook_ids, *, actor=_ACTOR, override_actor=None,
                 with_ceilings=True, with_override=True):
        self.notebook_ids = tuple(notebook_ids)
        self.actor = actor
        self.override_actor = override_actor or actor
        self.with_ceilings = with_ceilings
        self.with_override = with_override
        self._stack: list = []

    def __enter__(self):
        active = self.notebook_ids[0]
        managers = [retrieval_run(run_kind="ask_global", actor_id=self.actor)]
        if self.with_ceilings:
            managers.append(source_scope_context(
                active, None, None,
                notebook_source_ceilings=_ceilings(self.notebook_ids),
                subjectless=True,
            ))
        if self.with_override:
            managers.append(participant_override(
                _override(self.notebook_ids, actor=self.override_actor)
            ))
        for manager in managers:
            manager.__enter__()
            self._stack.append(manager)
        return self

    def __exit__(self, *exc):
        for manager in reversed(self._stack):
            manager.__exit__(*exc)
        self._stack.clear()
        return False


def _participants(notebook_ids):
    return tuple((notebook_id, "personal") for notebook_id in notebook_ids)


# --------------------------------------------------------------------------
# 验收 1:保底份额在对等模式恒 0
# --------------------------------------------------------------------------

def test_active_reserve_is_zero_under_override(monkeypatch):
    """三条消费路一处覆盖:``_withheld_active`` 不保留、``FederatedCollected``
    不带 ``active_reserve``、``enforce_active_floor`` 恒以 0 被调用。

    对照臂在同一个用例里:同一份替身、同样的 ``CHUNK_FEDERATION_ACTIVE_RESERVE
    =0.5``,没有覆盖时保底照常生效。
    """
    from app.services import retrieval as retrieval_module

    floors: list = []
    monkeypatch.setattr(
        retrieval_module, "enforce_active_floor",
        lambda selected, pool, floor: floors.append(floor) or selected,
    )
    ids = ("nb-a", "nb-b")
    # 名义 active 的命中弱、peer 的强:没有保底时 active 会被挤出预算=1 的池子。
    scores = {"nb-a": 0.10, "nb-b": 0.95}
    candidates = FakeCandidates(
        _participants(ids), active_reserve=0.5, mmr_k=2, recall=1,
        retrieve=lambda nid, q: (
            [make_chunk(f"c-{nid}", scores[nid], text=f"t-{nid}")],
            [f"c-{nid}"], np.eye(1, 3),
        ),
    )

    with _peer_run(ids):
        peer = cf.federated_chunk_candidates(candidates, "nb-a", ["q"])
        cf.apply_active_reserve(candidates.settings, ["x"], ["x", "y"], 2)

    assert floors == [0]
    assert not isinstance(peer.collected, cf.FederatedCollected)
    assert getattr(peer.collected, "active_reserve", 0) == 0
    # 预算 1 的池子完全按分数竞争:名义 active 没有保留席位。
    assert list(peer.collected) == ["c-nb-b"]

    floors.clear()
    plain = FakeCandidates(
        _participants(ids), active_reserve=0.5, mmr_k=2, recall=1,
        retrieve=lambda nid, q: (
            [make_chunk(f"c-{nid}", scores[nid], text=f"t-{nid}")],
            [f"c-{nid}"], np.eye(1, 3),
        ),
    )
    with retrieval_run(run_kind="ask_chunk", actor_id=_ACTOR):
        single = cf.federated_chunk_candidates(plain, "nb-a", ["q"])
        cf.apply_active_reserve(plain.settings, ["x"], ["x", "y"], 2)

    assert floors == [1]
    assert single.collected.active_reserve == 1
    assert list(single.collected) == ["c-nb-a"]


# --------------------------------------------------------------------------
# 验收 2:每条命中都打真实 notebook_id
# --------------------------------------------------------------------------

def test_every_hit_is_stamped_under_override():
    """8 库覆盖 → ``collected`` 里没有任何 ``notebook_id == ""`` 的命中。

    「空 = active」是单库模式的干净谓词;对等模式没有 active,空串会让那条命中在
    每个下游(引用徽章、``filter_retrieval_items`` 的 origin、tier 查表)都被当成
    名义 active 自己的,而它可能来自另外七个库中的任何一个。
    """
    ids = tuple(f"nb-{index}" for index in range(8))
    candidates = FakeCandidates(_participants(ids))

    with _peer_run(ids):
        result = cf.federated_chunk_candidates(candidates, ids[0], ["q"])

    assert len(result.collected) == 8
    assert all(hit.notebook_id for hit in result.collected.values())
    assert {hit.notebook_id for hit in result.collected.values()} == set(ids)
    assert result.participants == ids


def test_absent_override_keeps_the_active_leg_unstamped():
    """对照臂:没有覆盖时名义 active 的命中仍然不打标,调用形状仍是裸位置参数。"""
    ids = ("nb-0", "nb-1")
    candidates = FakeCandidates(_participants(ids), visible={"nb-1": ("s1",)})

    with retrieval_run(run_kind="ask_chunk", actor_id=_ACTOR):
        result = cf.federated_chunk_candidates(candidates, "nb-0", ["q"])

    stamps = {hit.chunk_id: hit.notebook_id for hit in result.collected.values()}
    assert stamps == {"c-nb-0": "", "c-nb-1": "nb-1"}
    assert candidates.call_for("nb-0")["allowed_source_ids"] is None
    assert candidates.call_for("nb-0")["producer_explicit"] is False
    assert candidates.call_for("nb-0")["peer_leg"] is False


# --------------------------------------------------------------------------
# 验收 3:名义 active 的来源天花板也下推到生产者
# --------------------------------------------------------------------------

def test_nominal_active_ceiling_reaches_producer():
    """名义 active 走 peer 腿 → 它的可见来源清单显式下推,``producer_explicit``
    仍是 False。

    这一条是「看起来 fail-closed 实则 fail-open」的那一处:少了它,名义 active
    是唯一一个天花板从未进到 ``LIMIT`` 之前的参与者——它的行只能靠结果边界的
    ``filter_retrieval_items`` 兜,而那时它们已经占过 Top-K 了。
    ``producer_explicit=False`` 同样是合同的一半:这是上下文化的活体枚举,不是
    生产者自己就窄的宇宙,attest 成 True 会关掉语料语言探针并换掉词法臂。
    """
    ids = ("nb-a", "nb-b")
    candidates = FakeCandidates(
        _participants(ids),
        visible={"nb-a": ("s-a1", "s-a2"), "nb-b": ("s-b1",)},
    )

    with _peer_run(ids):
        cf.federated_chunk_candidates(candidates, "nb-a", ["q"])

    active_call = candidates.call_for("nb-a")
    assert active_call["allowed_source_ids"] == ("s-a1", "s-a2")
    assert active_call["producer_explicit"] is False
    assert active_call["peer_leg"] is True
    assert candidates.call_for("nb-b")["allowed_source_ids"] == ("s-b1",)
    # 名义 active 的清单与 peer 走同一条 run-local memo,各枚举一次。
    assert sorted(candidates.visible_reads) == ["nb-a", "nb-b"]


def test_every_leg_is_a_peer_leg_under_override():
    """``_CHUNK_PEER_LEG`` 对全部腿为真 = generated-question 补召回整体关闭。"""
    ids = ("nb-a", "nb-b", "nb-c")
    candidates = FakeCandidates(_participants(ids))

    with _peer_run(ids):
        cf.federated_chunk_candidates(candidates, "nb-a", ["q"])

    assert all(call["peer_leg"] for call in candidates.calls)


# --------------------------------------------------------------------------
# 验收 4:名义 active 也是 peek-only(大库只借不载)
# --------------------------------------------------------------------------

def test_nominal_active_is_peek_only_under_override():
    """名义 active 是大库时,producer 一旦去加载 scale 索引即 fail。

    八库 run 冷加载任何一个大库的共享索引,都会把单库路径依赖的暖索引冲掉。
    单库模式下名义 active 是例外(用户正在用的库,这笔成本本来就存在);对等模式
    没有「正在用的库」,所以例外一并取消。
    """
    ids = ("nb-big", "nb-small")
    candidates = FakeCandidates(
        _participants(ids), copyable={"nb-big": False, "nb-small": True},
        cold_load_fails=True,
    )

    with _peer_run(ids):
        cf.federated_chunk_candidates(candidates, "nb-big", ["q"])

    assert candidates.scale_loads == ["nb-small"]


def test_absent_override_still_loads_the_active_index():
    """对照臂:没有覆盖时名义 active 仍然允许冷加载自己的索引。"""
    ids = ("nb-big", "nb-small")
    candidates = FakeCandidates(
        _participants(ids), copyable={"nb-big": False, "nb-small": True},
    )

    with retrieval_run(run_kind="ask_chunk", actor_id=_ACTOR):
        cf.federated_chunk_candidates(candidates, "nb-big", ["q"])

    assert candidates.scale_loads == ["nb-big", "nb-small"]


# --------------------------------------------------------------------------
# 验收 5:跨库合并阈值换轨
# --------------------------------------------------------------------------

def test_thresholds_switch_to_global_rails(monkeypatch):
    """``peer_evidence`` 收到 0.25 / 0.6 / ``peer_floor=0``,预算 =
    ``GLOBAL_ASK_CANDIDATE_LIMIT``。

    两组旋钮正交,给法也不同:合格线换成写进产品文档的全局栏杆;``peer_floor``
    归 0,因为 ``CHUNK_FEDERATION_PEER_FLOOR`` 是为「用户没主动选、只是挂在那里的
    参考库」发明的,而全局的库是用户为这道题逐个选的。
    """
    seen: list = []

    def _spy(pools, limit, *, min_relevance, relative_relevance, peer_floor):
        seen.append({
            "limit": limit, "min_relevance": min_relevance,
            "relative_relevance": relative_relevance, "peer_floor": peer_floor,
        })
        return [hit for pool in pools for hit in pool]

    monkeypatch.setattr(cf, "peer_evidence", _spy)
    ids = ("nb-a", "nb-b")
    candidates = FakeCandidates(_participants(ids), recall=200, peer_floor=0.5)

    with _peer_run(ids):
        cf.federated_chunk_candidates(
            candidates, "nb-a", ["q"], min_relevance=0.0, relative_relevance=0.0,
        )

    assert seen == [{
        "limit": _GLOBAL_BUDGET, "min_relevance": _GLOBAL_MIN,
        "relative_relevance": _GLOBAL_REL, "peer_floor": 0.0,
    }]

    seen.clear()
    with retrieval_run(run_kind="ask_chunk", actor_id=_ACTOR):
        cf.federated_chunk_candidates(
            candidates, "nb-a", ["q"], min_relevance=0.0, relative_relevance=0.0,
        )

    assert seen == [{
        "limit": 200, "min_relevance": 0.0,
        "relative_relevance": 0.0, "peer_floor": 0.5,
    }]


def test_merge_thresholds_returns_its_inputs_without_an_override():
    """``_merge_thresholds`` 的纯函数面:无覆盖时逐值原样返回。"""
    settings = SimpleNamespace(
        chunk_federation_peer_floor=0.5,
        global_ask_min_relevance=_GLOBAL_MIN,
        global_ask_relative_relevance=_GLOBAL_REL,
    )
    assert cf._merge_thresholds(settings, 0.1, 0.2) == (0.1, 0.2, 0.5)

    with _peer_run(("nb-a", "nb-b")):
        assert cf._merge_thresholds(settings, 0.1, 0.2) == (
            _GLOBAL_MIN, _GLOBAL_REL, 0.0,
        )


# --------------------------------------------------------------------------
# 验收 6:单参与者覆盖不短路
# --------------------------------------------------------------------------

def test_single_participant_does_not_short_circuit_under_override():
    """只选 1 个库的全局提问仍走联邦通道。

    单库那条路没有逐库预算、没有 ``peer_evidence`` 合格线、不打标,所以「选一个
    库」与「选两个库」会被两套规则回答。替身的 ``_retrieve_chunks_multi`` 一被调用
    就 fail,所以这条断言不靠计数。
    """
    ids = ("nb-only",)
    candidates = FakeCandidates(
        _participants(ids), visible={"nb-only": ("s-1",)},
    )

    with _peer_run(ids):
        result = cf.federated_chunk_candidates(
            candidates, "nb-only", ["q1", "q2"],
        )

    assert result.participants == ("nb-only",)
    assert [call["query"] for call in candidates.calls] == ["q1", "q2"]
    assert all(hit.notebook_id == "nb-only"
               for hit in result.collected.values())
    assert candidates.call_for("nb-only")["allowed_source_ids"] == ("s-1",)


# --------------------------------------------------------------------------
# 验收 7:预检落在扇出之前
# --------------------------------------------------------------------------

def test_precheck_fails_before_any_producer_runs():
    """覆盖在场 + actor 错配 → 任何生产者被调用之前就抛。

    替身的 producer 一被调用即 fail,来源枚举也记账,所以这条断言证明的是「预检
    在扇出之前」,而不只是「最终抛了」。``_bounded_participants`` 跑在父线程、不在
    任何 ``try`` 里,所以它不必(也不得)登记进 ``_SEAT_FAILSOFT_SITES``。
    """
    ids = ("nb-a", "nb-b")
    candidates = FakeCandidates(_participants(ids), producer_fails=True)

    with _peer_run(ids, actor=_ACTOR, override_actor="someone-else"):
        with pytest.raises(ParticipantOverrideError):
            cf.federated_chunk_candidates(candidates, "nb-a", ["q"])

    assert candidates.calls == []
    assert candidates.visible_reads == []
    assert candidates.events == []


def test_precheck_also_guards_the_membership_only_consumer():
    """``federation_participant_ids``(KG 归属表那条消费路)同样过预检。

    它与向量腿共用 ``_bounded_participants``,所以一处预检覆盖每个消费方——这正是
    预检落在座位入口而不是某个调用方的理由。
    """
    ids = ("nb-a", "nb-b")
    candidates = FakeCandidates(_participants(ids), producer_fails=True)

    with _peer_run(ids, override_actor="someone-else"):
        with pytest.raises(ParticipantOverrideError):
            cf.federation_participant_ids(candidates, "nb-a")


def test_precheck_is_a_no_op_without_an_override():
    """对照臂:没有覆盖时预检是一次 ContextVar 读,不改变任何返回值。"""
    ids = ("nb-a", "nb-b")
    candidates = FakeCandidates(_participants(ids))

    with retrieval_run(run_kind="ask_chunk", actor_id=_ACTOR):
        assert cf.federation_participant_ids(candidates, "nb-a") == frozenset(ids)


# --------------------------------------------------------------------------
# 1.1:两个判据必须恒等
# --------------------------------------------------------------------------

def test_peer_mode_predicates_agree():
    """检索层的 ``federated_ask_active()`` 与引用/提示词侧的
    ``subjectless_run_active()`` 在装好的全局 run 内同为 True、都不装时同为
    False。``peer_scope_ceiling_active()``(过滤口径)在这组形状下与它们同值,
    但那是巧合而非合同——见
    ``test_global_run.py::test_single_notebook_ceiling_is_not_subjectless``。

    两者刻意不是同一个判据:``ask_service`` 是三处已登记 fail-soft handler 的宿主
    也是鉴权相邻面,把它放进覆盖模块的读者白名单等于把「能替换参与集」的权限交给
    整条 Ask 主干;而 ``subjectless_run_active()`` 只回答「这次 run 有没有当前库」,
    它替换不了任何集合。它们不漂移是因为 ``global_run.global_ask_run`` 这一个上下文
    管理器同时安装两者、且安装时对全映射响亮断言——这条用例是那个构造前提的守卫。
    """
    ids = ("nb-a", "nb-b")

    assert federated_ask_active() is False
    assert peer_scope_ceiling_active() is False
    assert subjectless_run_active() is False

    with _peer_run(ids):
        assert federated_ask_active() is True
        assert peer_scope_ceiling_active() is True
        assert subjectless_run_active() is True

    assert federated_ask_active() is False
    assert peer_scope_ceiling_active() is False
    assert subjectless_run_active() is False

    # 负对照:只装一半时两者会分歧——这正是「必须由一个管理器同时安装」的理由,
    # 也证明上面的相等不是两个恒真的表达式凑出来的。
    with _peer_run(ids, with_ceilings=False):
        assert federated_ask_active() is True
        assert peer_scope_ceiling_active() is False
        assert subjectless_run_active() is False
    with _peer_run(ids, with_override=False):
        assert federated_ask_active() is False
        assert peer_scope_ceiling_active() is True
        assert subjectless_run_active() is True

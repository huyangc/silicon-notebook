"""D0-5 -- 对等模式下 ``AskService`` 各步骤与三条检索腿的处置合同。

计划 1.6 的处置表落到代码上是九条闸。它们today**在生产上不可达**(没有任何地方
安装覆盖 / 构造逐库天花板),所以每条用例都自己把「覆盖 + 逐库天花板」装起来,
形状与 PR-D1 冻结的安装形状同形(见 ``_peer_scope``);对照臂在同一个用例里,不装
任何东西,断言与今天逐值相等。

判据刻意是两个,分工是硬约束:

* ``ask_service.py`` 读 ``source_scope.subjectless_run_active()`` —— 它是三处
  已登记 fail-soft handler 的宿主、也是鉴权相邻面,不许进覆盖模块的读者白名单;
* 检索层(``retrieval_candidates`` / ``graph_retrieval`` / ``communities``)读
  ``retrieval_participants.federated_ask_active()`` —— 它们已经在白名单上。

⚠ D1-2 起 ask 侧读的是**显式无主体位**而不是 ``peer_scope_ceiling_active()``:
后者对「单库 run 冻结自己的来源清单」同样为真,而那种 run 仍然有当前库。两者的
分工由 ``test_global_run.py::test_single_notebook_ceiling_is_not_subjectless``
反向钉住,本文件的安装形状因此多了一个 ``subjectless=True``。

两者恒等由 ``test_peer_mode_federation.py::test_peer_mode_predicates_agree`` 钉住,
本文件不重复。
"""
from __future__ import annotations

import ast
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.domain.retrieval import RetrievedChunk
from app.services.ask_service import AskService
from app.services.communities import CommunityQueryService
from app.services.graph_retrieval import GraphRetrievalService
from app.services.retrieval_candidates import CandidateRetrievalService
from app.services.retrieval_participants import (
    ParticipantOverride,
    federated_ask_active,
    participant_override,
)
from app.services.retrieval_run import retrieval_run
from app.services.source_scope import (
    current_source_scope,
    peer_scope_ceiling_active,
    source_scope_context,
    subjectless_run_active,
)


_ACTOR = "user-peer-steps"
_ROOT = Path(__file__).resolve().parents[2]
_ASK_SERVICE = _ROOT / "backend" / "app" / "services" / "ask_service.py"


def _override(notebook_ids) -> ParticipantOverride:
    return ParticipantOverride(
        notebook_ids=tuple(notebook_ids), tiers={}, attested_actor_id=_ACTOR,
    )


def _ceilings(notebook_ids) -> dict:
    return {notebook_id: {f"src-{notebook_id}"} for notebook_id in notebook_ids}


@contextlib.contextmanager
def _peer_scope(notebook_ids=("nb-a", "nb-b", "nb-c")):
    """名义 active + 覆盖 + 逐库天花板 + 无主体位,PR-D1 冻结的那一组安装形状。

    ``source_scope_context`` 只提交第三个维度,所以 ``restricted`` /
    ``ceiling_active`` 恒 False、两份 payload 恒 None —— 这正是 1.5 列出的五个
    后果,也是本文件每条闸能独立于「收窄」被断言的前提。

    ``subjectless=True`` 不是装饰:ask 侧每条闸读的就是它。生产上这四件事只由
    ``global_run.global_ask_run`` 一次装齐,用例里逐件装是为了让对照臂能只拆掉
    其中一件。
    """
    ids = tuple(notebook_ids)
    with retrieval_run(run_kind="ask_chunk", actor_id=_ACTOR):
        with source_scope_context(
            ids[0], None, None, notebook_source_ceilings=_ceilings(ids),
            subjectless=True,
        ):
            with participant_override(_override(ids)):
                yield ids


def make_chunk(chunk_id: str, relevance: float = 0.9) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, source_id=f"src-{chunk_id}", source_title="t",
        section_path="", text=f"text-{chunk_id}", score=relevance,
        relevance=relevance,
    )


# ---------------------------------------------------------------------------
# 1. ``_memory_hits`` -- 对等模式关(chunk 与 reasoning 两个调用点一处覆盖)
# ---------------------------------------------------------------------------

class _RecordingMemory:
    def __init__(self) -> None:
        self.calls: list = []

    def notebook_memory_hits(self, user_id, notebook_id, query, limit):
        self.calls.append((user_id, notebook_id, query, limit))
        return ["memory-hit"]


def test_memory_channel_is_closed_in_peer_mode():
    """私有记忆属于**一个**笔记本;答一个集合的 run 没有哪个库的私有层该折进来。

    一处闸覆盖两个调用点:``ask_chunk`` 与 ``_run_reasoning_stage`` 都经这里取数,
    而 ``reasoning_retrieval`` 自己没有任何 memory 取数点(``consult_memory`` 读的
    是 reflect 的经验库,不是笔记本 Memory 投影)。
    """
    retriever = _RecordingMemory()
    service = SimpleNamespace(memory_retriever=retriever)

    with _peer_scope():
        assert AskService._memory_hits(service, "u", "nb-a", "q") == []
    assert retriever.calls == [], retriever.calls

    # 对照臂:不装任何东西,与今天逐值相等。
    assert AskService._memory_hits(service, "u", "nb-a", "q") == ["memory-hit"]
    assert retriever.calls == [("u", "nb-a", "q", 8)]


# ---------------------------------------------------------------------------
# 2. ``_needs_index`` -- 对等模式恒 False
# ---------------------------------------------------------------------------

def test_index_required_is_never_raised_in_peer_mode():
    """``index_required`` 是一句**对当前库**的行动号召,对等 run 没有承接方。"""
    probes: list = []
    service = SimpleNamespace(
        scale_index_probe=lambda notebook_id: probes.append(notebook_id) or False,
        scale_profiles=lambda: SimpleNamespace(
            requires_index=lambda notebook_id, has_disk_index: True
        ),
    )

    with _peer_scope():
        assert AskService._needs_index(service, "nb-a") is False
    assert probes == [], "对等模式下连索引探针都不该跑"

    assert AskService._needs_index(service, "nb-a") is True
    assert probes == ["nb-a"]


# ---------------------------------------------------------------------------
# 3. ``_activate_selected_source_graph`` -- 对等模式直接返回 (chunks, None)
# ---------------------------------------------------------------------------

class _ExplodingLaneHost:
    """整条「所选来源图」通道的替身:被碰一下就是一次失败。"""

    def __getattr__(self, name):  # pragma: no cover - 只在闸漏掉时触发
        raise AssertionError(
            f"selected-source-graph lane touched host.{name} in peer mode"
        )


def test_selected_source_graph_lane_is_closed_in_peer_mode():
    """整条通道定义在「用户在**这个库**里勾选的来源」之上,对等 run 没提交本地勾选。

    返回形状与今天的 dormant 分支逐字相同:``(list(chunks), None)``。
    """
    chunks = [make_chunk("c1"), make_chunk("c2")]
    hostile = SimpleNamespace(
        selected_source_graph=None,
        retrieval_contributors=_ExplodingLaneHost(),
        retrieval_connection_probe=lambda: True,
    )

    with _peer_scope():
        selected, status = AskService._activate_selected_source_graph(
            hostile, "nb-a", chunks,
        )
    assert selected == chunks and selected is not chunks
    assert status is None

    # 对照臂之一:同一份敌意替身,不装天花板 -> 通道真的会去碰 host。
    with pytest.raises(AssertionError, match="touched host"):
        AskService._activate_selected_source_graph(hostile, "nb-a", chunks)

    # 对照臂之二:今天的 dormant 形状逐值不变。
    benign = SimpleNamespace(
        selected_source_graph=None, retrieval_contributors=None,
        retrieval_connection_probe=None,
    )
    assert AskService._activate_selected_source_graph(
        benign, "nb-a", chunks,
    ) == (chunks, None)


# ---------------------------------------------------------------------------
# 4. ``peer_notebooks=`` -- 四个合成调用点全部开
# ---------------------------------------------------------------------------

def _answer_prompt_call_nodes() -> list:
    tree = ast.parse(_ASK_SERVICE.read_text(encoding="utf-8"), filename="ask_service.py")
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "answer_prompt"
    ]


def test_every_answer_prompt_call_site_passes_the_peer_flag():
    """``_answer_chunks`` / ``_answer_mix`` / ``_answer_reasoning`` /
    ``_try_document_overview`` 四处——**以及以后新增的任何一处**。

    结构断言而不是四条行为断言:后三处要跑起来得先立起半个 AskService,而漏掉的
    风险恰恰在「又加了第五个合成入口」那一天。``peer_notebooks`` 的实参必须就是
    ``subjectless_run_active()`` 的调用,写成 ``True``/``False`` 常量同样报红,
    写成 ``peer_scope_ceiling_active()`` 也报红——后者对「单库 run 冻结自己的来源
    清单」为真,而那种 run 的提示词必须保留 base/personal 权威序。
    """
    calls = _answer_prompt_call_nodes()
    assert len(calls) >= 4, f"合成调用点只剩 {len(calls)} 个,入口被改名了吗?"

    violations: list = []
    for node in calls:
        flags = [kw for kw in node.keywords if kw.arg == "peer_notebooks"]
        if len(flags) != 1:
            violations.append(
                f"line {node.lineno}: 没有 peer_notebooks= 实参"
            )
            continue
        value = flags[0].value
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "subjectless_run_active"
        ):
            violations.append(
                f"line {node.lineno}: peer_notebooks 不是 "
                f"subjectless_run_active() 的调用"
            )
    assert not violations, violations


def test_answer_chunks_switches_the_prompt_to_peer_authority(monkeypatch):
    """行为对照:同一次合成,装了天花板就拿到 ``peer_notebooks=True``。"""
    from app.services import ask_service as module

    seen: list = []

    def fake_answer_prompt(question, context_block, history_block="", **kwargs):
        seen.append(kwargs.get("peer_notebooks"))
        return "prompt"

    monkeypatch.setattr(module, "answer_prompt", fake_answer_prompt)
    client = SimpleNamespace(
        chat_json=lambda messages, hint, **kwargs: json.dumps(
            {"answer": "a", "grounded": True}
        ),
    )
    service = SimpleNamespace(
        _chunk_answer_context=lambda chunks, notebook_id="", **kwargs: ("ctx", {}),
        _append_memory_context=lambda block, id_map, hits: (block, id_map),
        _parse_answer_anchors=lambda answer, id_map: [],
    )

    with _peer_scope():
        AskService._answer_chunks(service, "q", [], llm_client=client)
    AskService._answer_chunks(service, "q", [], llm_client=client)

    assert seen == [True, False]


# ---------------------------------------------------------------------------
# 5. ``_mix_retrieve`` 的 PPR 路 -- 对等模式关
# ---------------------------------------------------------------------------

def _mix_stub(ppr_chunks):
    calls: list = []
    return calls, SimpleNamespace(
        settings=SimpleNamespace(
            chunk_kg_overlay_enabled=False, graph_ppr_enabled=True,
        ),
        _gather_vector_chunks=lambda notebook_id, sub_queries: [],
        _unsafe_source_scope_restricted=lambda notebook_id: False,
        _ppr_retrieve=lambda notebook_id, query: (
            calls.append(notebook_id) or list(ppr_chunks)
        ),
    )


def test_mix_retrieve_drops_the_ppr_leg_in_peer_mode():
    """PPR 是**一张图**上的随机游走,按名义 active 的参与集缓存、种子也只从它出发。"""
    calls, service = _mix_stub([make_chunk("ppr-1")])

    with _peer_scope():
        merged, _block, _map, _hits, ppr_count = (
            CandidateRetrievalService._mix_retrieve(service, "nb-a", "q", "", ["q"])
        )
    assert ppr_count == 0 and merged == []
    assert calls == [], "PPR 在对等模式下连调用都不该发生"

    merged, _block, _map, _hits, ppr_count = (
        CandidateRetrievalService._mix_retrieve(service, "nb-a", "q", "", ["q"])
    )
    assert ppr_count == 1 and [c.chunk_id for c in merged] == ["ppr-1"]
    assert calls == ["nb-a"]


# ---------------------------------------------------------------------------
# 6. ``_ppr_retrieve`` 自己 -- 防御性第二道
# ---------------------------------------------------------------------------

def test_ppr_retrieve_returns_empty_in_peer_mode():
    """reasoning 的种子/动作腿与报告引擎都直调本函数,闸只放在 mix 那一处会漏掉。"""
    events: list = []
    probes: list = []
    service = SimpleNamespace(
        settings=SimpleNamespace(ppr_top_chunks=5, ppr_damping=0.5),
        scale_ppr=lambda notebook_id, question, max_results=None: (
            probes.append(notebook_id) or []
        ),
        _federated_graph_is_large=lambda notebook_id: True,
        event_log=SimpleNamespace(emit=events.append),
    )

    with _peer_scope():
        assert GraphRetrievalService._ppr_retrieve(service, "nb-a", "q") == []
    assert probes == [] and events == []

    assert GraphRetrievalService._ppr_retrieve(service, "nb-a", "q") == []
    assert probes == ["nb-a"]
    assert [event["kind"] for event in events] == ["ppr_fallback_refused"]


# ---------------------------------------------------------------------------
# 7. 关键词臂 / 精确查找臂 -- 对等模式下的 active-only 检索特权
# ---------------------------------------------------------------------------

class _LexicalProbe:
    """``_keyword_chunk_candidates`` 走到 FTS 所需的最小面,逐步记账。"""

    def __init__(self) -> None:
        self.calls: list = []
        self.settings = SimpleNamespace(chunk_recall=5, exact_lookup_enabled=True)
        self.event_log = SimpleNamespace(emit=lambda event: None)

    def _unsafe_source_scope_restricted(self, notebook_id):
        self.calls.append("restricted_probe")
        return False

    def _lexical_gate_source_scoped(self, allowed_source_ids, notebook_id, *,
                                    drifted=None):
        return False

    def _lexical_corpus_langs(self, notebook_id, *, source_scoped=False):
        self.calls.append("corpus_langs")
        return None

    def _connect(self):
        return contextlib.nullcontext(None)

    def _chunk_fts_hits(self, db, notebook_id, needle, *, k, allowed_source_ids,
                        corpus_langs):
        self.calls.append("chunk_fts")
        return []


def test_keyword_arm_is_closed_in_peer_mode():
    """这条臂是 active-only 的(每次 ask 一次,不随联邦腿分叉),对等模式下它等于
    凭空给命名锚点多一条别人没有的腿。"""
    probe = _LexicalProbe()

    with _peer_scope():
        assert CandidateRetrievalService._keyword_chunk_candidates(
            probe, "nb-a", "关键词 keyword",
        ) == []
    assert probe.calls == [], probe.calls

    assert CandidateRetrievalService._keyword_chunk_candidates(
        probe, "nb-a", "关键词 keyword",
    ) == []
    assert probe.calls == ["restricted_probe", "corpus_langs", "chunk_fts"]


def test_element_arm_is_closed_in_peer_mode():
    """第三条 active-only 补召回腿(D1-4):元素检索没有联邦通道,对等模式整条关。

    它比另外两条还多一层代价:命中会经 ``evidence_context.element_citations``
    出引用卡,而那条装配一直是单库口径——对等 run 里就是一条没法对任何一本库的
    天花板复核的引用。
    """
    calls: list = []

    class _Elements:
        def _retrieve_elements(self, *args, **kwargs):
            calls.append(args[0] if args else kwargs.get("notebook_id"))
            return ["hit"]

    probe = _Elements()

    with _peer_scope():
        assert CandidateRetrievalService.retrieve_elements(probe, "nb-a", "q") == []
    assert calls == []

    # 对照臂:不装覆盖时这条腿照常跑,命中逐值原样交回。
    assert CandidateRetrievalService.retrieve_elements(
        probe, "nb-a", "q",
    ) == ["hit"]
    assert calls == ["nb-a"]


def test_the_reasoning_element_action_is_skipped_in_peer_mode():
    """动作执行处同一把闸:轨迹如实说明,而不是报一次「查了但一段都没有」。"""
    from app.services.reasoning_retrieval import ReasoningRetriever

    retriever = object.__new__(ReasoningRetriever)
    retriever.settings = SimpleNamespace(reasoning_max_element_searches=3)

    with _peer_scope():
        skip = ReasoningRetriever._element_search_skip(retriever, 0)
    assert skip is not None and skip[1] == "peer_mode"

    assert ReasoningRetriever._element_search_skip(retriever, 0) is None
    assert ReasoningRetriever._element_search_skip(retriever, 3)[1] == (
        "element_search_cap"
    )


def test_exact_lookup_arm_is_closed_in_peer_mode():
    """同上。它一关,``exact_section_reserve`` 的 ``exact_ids`` 恒空、该保底规则
    自动 inert,所以下游不需要第二道闸。"""
    probe = _LexicalProbe()
    probe.settings.exact_lookup_enabled = False

    with _peer_scope():
        assert CandidateRetrievalService._exact_lookup_chunks(
            probe, "nb-a", "set_db",
        ) == []
    assert probe.calls == []

    assert CandidateRetrievalService._exact_lookup_chunks(
        probe, "nb-a", "set_db",
    ) == []
    assert probe.calls == ["restricted_probe"]


# ---------------------------------------------------------------------------
# 8. ``communities.mounted_base_ids`` -- 对等模式不剥名义 active
# ---------------------------------------------------------------------------

class _FakeUnifiedKg:
    def __init__(self, peers_by_notebook) -> None:
        self._peers = dict(peers_by_notebook)
        self.comention_queries: list = []

    def mounted_base_ids(self, active_notebook_id):
        return ()

    def resolve_focal(self, notebook_id, key):
        return f"focal::{notebook_id}"

    def comention_peers(self, notebook_id, focal, min_bridge, top_k, **kwargs):
        self.comention_queries.append(notebook_id)
        return [(name, 3) for name in self._peers.get(notebook_id, ())]

    def top_community_for(self, notebook_id, focal):
        # 共提为空的库照常回退社区路径;这个替身没有社区,回退当场空手——
        # ``resolve_comparison_peers`` 的两路编排与今天逐字相同。
        return ""

    def source_index_backfilled(self, notebook_id):
        return True


def _community_service(unified_kg):
    return CommunityQueryService(
        notebooks=SimpleNamespace(),
        unified_kg=unified_kg,
        event_log=SimpleNamespace(emit=lambda event: None),
    )


def test_comparison_peer_libraries_keep_the_nominal_active_in_peer_mode():
    """剥掉首项的理由是「当前库不是自己的参考库」——那要有一个主体库才成立。

    第二半同样重要:不剥**不会**把名义 active 重复计。两个消费点
    (``ask_chunk`` 的对比子查询、reasoning 的 ``_action_expand_community``)都是
    逐库调 ``resolve_comparison_peers(base_nb, …)``,每一轮只查那一个库自己的共提
    行,拿回来的名字再按名字去重——所以多一个库只是多一轮查询。
    """
    ids = ("nb-a", "nb-b", "nb-c")
    unified = _FakeUnifiedKg({
        "nb-a": ("锚点兄弟", "共享兄弟"),
        "nb-b": ("共享兄弟", "B 兄弟"),
        "nb-c": (),
    })
    service = _community_service(unified)

    with _peer_scope(ids):
        assert service.mounted_base_ids(ids[0]) == list(ids)

        # 抄 ``ask_chunk`` 的消费形状,逐库一轮、按名字去重。
        sub_queries = ["原始问题"]
        for base_nb in service.mounted_base_ids(ids[0]):
            peers, _source = service.resolve_comparison_peers(
                base_nb, "焦点", "原始问题", top_k=8, candidates=8,
            )
            for name in peers:
                if name not in sub_queries:
                    sub_queries.append(name)

    # 名义 active 恰好被查一次(第二次 ``mounted_base_ids`` 不额外查库)。
    assert unified.comention_queries == list(ids)
    assert sub_queries == ["原始问题", "锚点兄弟", "共享兄弟", "B 兄弟"]
    assert len(sub_queries) == len(set(sub_queries))


def test_comparison_peer_libraries_still_strip_the_active_without_an_override():
    """对照臂:没有覆盖时首项照旧剥掉,与今天逐值相等。"""
    unified = _FakeUnifiedKg({})
    unified.mounted_base_ids = lambda active: ("base-1", "base-2")
    service = _community_service(unified)

    assert federated_ask_active() is False
    assert service.mounted_base_ids("nb-a") == ["base-1", "base-2"]


# ---------------------------------------------------------------------------
# 9. 自查出来的两处「名义 active 的隐性特权」
# ---------------------------------------------------------------------------

def test_document_overview_citations_keep_the_nominal_active(monkeypatch):
    """``_try_document_overview`` 尾部那圈就地归一是唯一一处**不经**
    ``foreign_notebook_id`` 的引用归属改写(它改的是属性、不是
    ``notebook_id=`` 关键字,所以 ``test_citation_notebook_id_guard`` 看不见它)。

    ``prepare_catalog_overview`` / ``collection_item_citations`` 已经按
    ``citation_active_id`` 把真实归属带回来了,这圈再按**原始** ``notebook_id``
    比一次,就会把名义 active 的每条引用重新抹成空串——界面上那几条引用就不再
    显示来自哪个库,而对等模式的整个卖点就是「每条引用说清来自哪个库」。
    """
    from app.models.schemas import Citation
    from app.services import document_catalog_overview, document_overview

    monkeypatch.setattr(
        document_overview, "overview_intent",
        lambda question: document_overview.OverviewIntent("catalog"),
    )

    def _catalog():
        return SimpleNamespace(
            result_sets=[], items=[], id_map={"1": {}},
            citations=[
                Citation(label="锚点库的文档", source_id="s1", element_id="e1",
                         location_label="p1", quoted_span="x", tier="personal",
                         notebook_id="nb-a"),
                Citation(label="兄弟库的文档", source_id="s2", element_id="e2",
                         location_label="p1", quoted_span="y", tier="personal",
                         notebook_id="nb-b"),
            ],
            coverage_note="", context_block="ctx",
        )

    monkeypatch.setattr(
        document_catalog_overview, "prepare_catalog_overview",
        lambda *args, **kwargs: _catalog(),
    )

    service = SimpleNamespace(
        collection_enumeration=object(), overview_sources=None,
        evidence_context=object(),
        settings=SimpleNamespace(chunk_answer_budget_chars=4000,
                                 document_overview_max_elements=20),
        model_clients=SimpleNamespace(
            chat=lambda name: SimpleNamespace(configured=True, model="m")
        ),
        overview_source_generation=None,
        _answer_with_retry=lambda synthesize, model: ("答案", None, [], True),
        _parse_answer_anchors=lambda answer, id_map: [],
        _save_answer=lambda *args, **kwargs: "",
    )
    payload = SimpleNamespace(question="有哪些文档", retrieval_effort="standard",
                              asked_at=None)

    with _peer_scope():
        response = AskService._try_document_overview(
            service, "nb-a", payload, "conv", "", "",
            user_id="u", job_id="", cancel_event=None,
        )
    assert [c.notebook_id for c in response.citations] == ["nb-a", "nb-b"]

    # 对照臂:单库模式下名义 active 的引用照旧归零(前端按空串显示「本库」)。
    response = AskService._try_document_overview(
        service, "nb-a", payload, "conv", "", "",
        user_id="u", job_id="", cancel_event=None,
    )
    assert [c.notebook_id for c in response.citations] == ["", "nb-b"]


def test_workbook_lane_drops_libraries_outside_the_run():
    """表格分析臂走的是**真实挂载谓词**(鉴权级座位,绝不许变成覆盖感知)。

    对等模式下那份清单答的是名义 active 自己的挂载表——与用户逐个选的那几个库
    毫无关系。挂在命名锚点下、却不在本次选择里的参考库,会成为唯一一个仍然向
    答案供证据的库外来源。这里按**逐库天花板**把它收窄掉(只减不增,是消费边界
    的过滤,不是第二条学参与集的路);把 peer 的工作簿也分析进来是联邦化那一半,
    登记在 ``fangan_todo.md``。
    """
    analyzed: list = []
    service = SimpleNamespace(
        spreadsheet_analysis=SimpleNamespace(
            analyze=lambda **kwargs: (
                analyzed.append(kwargs["source_refs"]) or ([], None)
            )
        ),
        ask_engine_hidden_sources=lambda notebook_id, user_id: (),
        ask_engine_participant_notebooks=lambda notebook_id: ("nb-b", "outsider"),
        ask_engine_visible_sources=lambda notebook_id: (f"src-{notebook_id}",),
        _tier_map_for=lambda notebook_ids: {},
        model_clients=SimpleNamespace(chat=lambda name: None),
        event_log=SimpleNamespace(logger=SimpleNamespace(warning=lambda *a: None)),
    )
    prepared = SimpleNamespace(notebook_id="nb-a", user_id="u",
                               research_question="q")
    runtime = SimpleNamespace(scope=None, cancellation=None, trace_sink=None)

    with _peer_scope():
        AskService._spreadsheet_reasoning_results(service, prepared, runtime, [])
    assert [ref[0] for ref in analyzed[-1]] == ["nb-a", "nb-b"], analyzed[-1]

    AskService._spreadsheet_reasoning_results(service, prepared, runtime, [])
    assert [ref[0] for ref in analyzed[-1]] == ["nb-a", "nb-b", "outsider"]


# ---------------------------------------------------------------------------
# 10. 1.6 表里「自动满足」的那条关键事实
# ---------------------------------------------------------------------------

def test_inner_scope_context_is_a_passthrough():
    """``AskService.ask`` 内层的 ``source_scope_context(nb, None, None)`` 三者全 None
    时 **yield 而不设 scope**,外层的全局 scope 原样存活。

    这是整个 D1 方案成立的关键事实:全局入口在 ``ask()`` **之外**装好逐库天花板,
    而 ``ask()`` 内部照旧为请求自己的 scope 开一层——如果那一层无条件覆盖,外层
    天花板会在检索开始前当场消失,本文件每一条闸都会在生产里失效。
    """
    with _peer_scope(("nb-a", "nb-b")) as ids:
        outer = current_source_scope()
        assert outer is not None and outer.peer_ceiling_active is True

        with source_scope_context(ids[0], None, None):
            assert current_source_scope() is outer
            assert peer_scope_ceiling_active() is True
            assert subjectless_run_active() is True
            assert federated_ask_active() is True

        # 退出内层没有把外层一起重置掉。
        assert current_source_scope() is outer

    assert current_source_scope() is None

    # 负对照:没有外层时它同样什么都不装(而不是装一个空 scope)。
    with source_scope_context("nb-a", None, None):
        assert current_source_scope() is None
        assert peer_scope_ceiling_active() is False
        assert subjectless_run_active() is False


# ---------------------------------------------------------------------------
# 11. 行数天花板:被改的函数一个都不在表里,表里的函数一行没动
# ---------------------------------------------------------------------------

def test_length_ceiling_functions_untouched():
    """复用 ``test_phase0_architecture_guard`` 的零松弛判据,不重写。

    D0-5 碰的函数(``_memory_hits`` / ``_needs_index`` /
    ``_activate_selected_source_graph`` / 四个合成入口 / 三条检索腿 /
    ``mounted_base_ids``)一个都不在 ``function_length_ceiling`` 里,所以这条应当
    在**不改 baseline** 的前提下全绿;它一旦红,要么是改到了热函数,要么是热函数
    被顺手缩了而没同 diff 下调基线。
    """
    from tests.test_phase0_architecture_guard import (
        BASELINE_PATH, ROOT, function_length_violations,
    )

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert function_length_violations(
        ROOT, baseline["function_length_ceiling"]
    ) == []

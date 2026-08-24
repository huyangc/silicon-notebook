import json
import threading

import pytest
from tests.model_testkit import bind_all_embedding_clients
from tests.model_testkit import bind_chat_client


def test_trace_step_model_shape():
    from app.models.schemas import TraceStep
    t = TraceStep(step_type="plan", summary="规划了 2 个子查询", detail={"n": 2})
    d = t.model_dump()
    assert d["step_type"] == "plan"
    assert d["summary"].startswith("规划")
    assert d["detail"] == {"n": 2}
    assert d["duration_ms"] is None            # 默认无耗时,record() 时才回填
    t.duration_ms = 1234
    assert t.model_dump()["duration_ms"] == 1234


def test_trace_recorder_duration_is_delta_between_adjacent_records(monkeypatch):
    """`_TraceRecorder` 的耗时语义(B8 quality P3-1):`duration_ms` = 相邻两次
    记账的墙钟差,且 `_last_ts` 必须在**记账之后**才更新——构造那一刻算作
    "上一次记账",所以首步耗时含构造到首次 record 之间的时间。"""
    import app.services.reasoning_retrieval as rr
    from app.models.schemas import TraceStep

    ticks = iter([0.0, 0.25, 0.9, 2.4])  # 构造 / 首步 / 第二步 / 第三步

    def fake_perf_counter():
        return next(ticks)

    monkeypatch.setattr(rr.time, "perf_counter", fake_perf_counter)

    trace: list = []
    recorder = rr._TraceRecorder(trace, None, None)  # 构造消费第一个刻度(0.0)

    recorder(TraceStep(step_type="plan", summary="s1"))       # 消费 0.25
    recorder(TraceStep(step_type="retrieve", summary="s2"))   # 消费 0.9
    recorder(TraceStep(step_type="answer", summary="s3"))     # 消费 2.4

    assert [step.duration_ms for step in trace] == [250, 650, 1500]


def test_trace_recorder_invokes_on_step_and_propagates_cancellation():
    import app.services.reasoning_retrieval as rr
    from app.domain.cancellation import AskCancelled
    from app.models.schemas import TraceStep

    seen = []
    recorder = rr._TraceRecorder([], None, seen.append)
    step = TraceStep(step_type="plan", summary="s1")
    recorder(step)
    assert seen == [step]

    cancel_event = threading.Event()
    cancel_event.set()
    cancelled_recorder = rr._TraceRecorder([], cancel_event, None)
    with pytest.raises(AskCancelled):
        cancelled_recorder(TraceStep(step_type="plan", summary="s2"))


def test_ask_request_mode_defaults_chunk():
    from app.models.schemas import AskRequest
    assert AskRequest(question="x").mode == "chunk"
    assert AskRequest(question="x", mode="fast").mode == "fast"
    assert AskRequest(question="x", mode="reasoning").mode == "reasoning"


def test_ask_response_reasoning_trace_defaults_none_and_dumps():
    from app.models.schemas import AskResponse
    r = AskResponse(conclusion="x")
    assert r.reasoning_trace is None
    assert "reasoning_trace" in r.model_dump()


def test_reasoning_settings_knobs():
    from app.core.config import Settings
    s = Settings()
    assert s.reasoning_max_steps == 50
    assert s.reasoning_max_subqueries == 5
    assert s.reasoning_max_ppr_retrieves == 3
    assert s.reasoning_max_exact_lookups == 3
    assert s.reasoning_max_follow_chain_actions == 3
    assert s.reasoning_community_peers_cap_factor == 2
    assert s.reasoning_max_outline_updates == 6


def test_reasoning_action_policy_settings_env(monkeypatch):
    from app.core.config import Settings
    from app.services.reports.policy import reasoning_action_policy

    monkeypatch.setenv("REASONING_MAX_PPR_RETRIEVES", "1")
    monkeypatch.setenv("REASONING_MAX_EXACT_LOOKUPS", "2")
    monkeypatch.setenv("REASONING_MAX_FOLLOW_CHAIN_ACTIONS", "4")
    monkeypatch.setenv("REASONING_COMMUNITY_PEERS_CAP_FACTOR", "3")
    monkeypatch.setenv("REASONING_MAX_OUTLINE_UPDATES", "5")
    policy = reasoning_action_policy(Settings(_env_file=None))
    assert (
        policy.max_ppr_retrieves,
        policy.max_exact_lookups,
        policy.max_follow_chain_actions,
        policy.community_peers_cap_factor,
        policy.max_outline_updates,
        policy.max_pending_outline_evidence,
    ) == (1, 2, 4, 3, 5, 48)


def test_adaptive_top_n_settings_defaults():
    from app.core.config import Settings
    s = Settings()
    assert s.retrieval_top_n == 20            # base floor(旧 12 从未校准,提到 20)
    assert s.reasoning_top_n_per_query == 3
    assert s.reasoning_top_n_cap == 36


def test_adaptive_top_n_env(monkeypatch):
    monkeypatch.setenv("REASONING_TOP_N_PER_QUERY", "5")
    monkeypatch.setenv("REASONING_TOP_N_CAP", "20")
    from app.core.config import Settings
    s = Settings()
    assert s.reasoning_top_n_per_query == 5
    assert s.reasoning_top_n_cap == 20


def test_effective_top_n_scales_with_aspects():
    """证据预算=clamp(每方面席位×方面数, floor=retrieval_top_n(20), cap(36))。
    简单/少方面题=floor(20);对比题(如 3+8 兄弟=11 方面)→ 3×11=33,不再被总数挤薄;
    显式传入(报告逐节独立预算)直通。"""
    from app.services.reasoning_retrieval import effective_top_n

    class _S:
        retrieval_top_n = 20
        reasoning_top_n_per_query = 3
        reasoning_top_n_cap = 36

    s = _S()
    assert effective_top_n(s, None, 1) == 20    # 单方面:floor
    assert effective_top_n(s, None, 6) == 20    # 3×6=18 < floor → 仍 20
    assert effective_top_n(s, None, 7) == 21    # 3×7=21 > floor → 自适应接管
    assert effective_top_n(s, None, 11) == 33   # 对比题:3×11,自动扩容
    assert effective_top_n(s, None, 20) == 36   # 封顶 cap
    assert effective_top_n(s, 12, 11) == 12     # 显式传入(报告逐节)直通,不受方面数影响
    assert effective_top_n(s, None, 0) == 20    # 防御:0 方面按 1 算 → floor


@pytest.mark.parametrize(
    ("effort", "n_queries", "expected"),
    [
        ("overview", 1, 8),
        ("overview", 10, 12),
        ("standard", 1, 20),
        ("standard", 7, 21),
        ("standard", 20, 36),
        ("deep", 10, 40),
        ("thorough", 20, 64),
        ("exhaustive", 20, 96),
    ],
)
def test_effective_top_n_uses_retrieval_effort_thresholds(
    effort, n_queries, expected
):
    """档位预算使用集中阈值表；settings 值不应混入已选档位。"""
    from app.core.ask_retrieval_policy import ask_retrieval_limits
    from app.services.reasoning_retrieval import effective_top_n

    class _PoisonSettings:
        retrieval_top_n = 999
        reasoning_top_n_per_query = 999
        reasoning_top_n_cap = 999

    limits = ask_retrieval_limits(effort)
    assert effective_top_n(_PoisonSettings(), None, n_queries, limits) == expected
    # 报告/调用方显式 top_n 始终直通，档位不能改写它。
    assert effective_top_n(_PoisonSettings(), 7, n_queries, limits) == 7


def test_reasoning_quota_enabled_default():
    from app.core.config import Settings
    assert Settings().reasoning_quota_enabled is True


def test_reasoning_quota_enabled_env(monkeypatch):
    monkeypatch.setenv("REASONING_QUOTA_ENABLED", "false")
    from app.core.config import Settings
    assert Settings().reasoning_quota_enabled is False


def test_reasoning_timeout_retry_knobs_defaults():
    from app.core.config import Settings
    s = Settings()
    assert s.reasoning_timeout_seconds == 90
    assert s.reasoning_max_retries == 1


def test_reasoning_timeout_retry_knobs_env(monkeypatch):
    monkeypatch.setenv("REASONING_TIMEOUT_SECONDS", "33")
    monkeypatch.setenv("REASONING_MAX_RETRIES", "4")
    from app.core.config import Settings
    s = Settings()
    assert s.reasoning_timeout_seconds == 33
    assert s.reasoning_max_retries == 4


def test_plan_prompt_contains_question_and_schema():
    from app.services.prompts import plan_prompt, PLAN_SCHEMA_HINT
    p = plan_prompt("innovus 的 PR 流程", "User: ...\nAssistant: ...")
    assert "innovus 的 PR 流程" in p
    assert "User: ..." in p  # history_block 被插值进 prompt
    assert "sub_queries" in PLAN_SCHEMA_HINT
    assert "prefer" in PLAN_SCHEMA_HINT


def test_reflect_prompt_contains_summary_and_schema():
    from app.services.prompts import reflect_prompt, REFLECT_SCHEMA_HINT
    p = reflect_prompt("问题X", "- [claim] A (id=k1)")
    assert "问题X" in p
    assert "id=k1" in p
    assert "next_action" in REFLECT_SCHEMA_HINT
    for a in ("answer", "expand_graph", "add_subquery", "search_elements"):
        assert a in REFLECT_SCHEMA_HINT
    assert "follow_chain" in REFLECT_SCHEMA_HINT
    assert "start_object_id" in REFLECT_SCHEMA_HINT


def test_answer_prompt_has_derivation_rigor_rules():
    """机理/推导题三条严谨性条款:分层组织+推断桥接、量纲一致+电路可实现形式、单源数值给区间。"""
    from app.services.prompts import answer_prompt
    p = answer_prompt("q", "ctx")
    assert "layer by layer" in p
    assert "dimensionally consistent" in p
    assert "that source's stated value" in p


def test_reflect_prompt_checks_coverage_aspect_by_aspect():
    """sufficient 判据升级为逐层/逐方面核查;ppr 指引扩到跨文档多层推导。"""
    from app.services.prompts import reflect_prompt
    p = reflect_prompt("q", "s")
    assert "aspect by aspect" in p
    assert "multi-layer derivation" in p


def test_answer_prompt_requires_full_enumeration_for_list_questions():
    """规则 11(PR-1 止血):枚举/列举类问题必须把每个不同的匹配条目逐条列出、
    各自挂 [k],不得抽样/合并;证据可能不覆盖全集时须明确说明。"""
    from app.services.prompts import answer_prompt
    p = answer_prompt("q", "ctx")
    assert "list EVERY distinct matching item" in p
    assert "do NOT sample, merge similar ones together" in p
    # 披露必须是无条件形态(you MUST state …),旧的条件式措辞("If the evidence
    # may not cover … say the list may be incomplete")同样含 "may be
    # incomplete" 子串,只断言它会假绿。
    assert "Unless a coverage line" in p
    assert "you MUST state that the list may be incomplete" in p
    # hybrid Knowhow 的 structured_prompt_block 注入的行没有 kN id(不在
    # id_map 里):对它们强挂 [k] 是不可满足合同(codex PR#391 P2),必须豁免
    # 且禁止捏造不存在的 [k]。
    assert "cite that exact row with its [kN] marker" in p
    assert "never invent a [k] id that does not exist" in p


def test_reflect_prompt_forbids_claiming_full_retrieval():
    """PR-1 止血:reflect 的 reason 不得声称"所有/全部 X 已检索到"——相关性检索
    无法证明完整性,只能陈述实际找到了什么、还缺什么。"""
    from app.services.prompts import reflect_prompt
    p = reflect_prompt("q", "s")
    assert "NEVER claim that 'all/every X have been retrieved'" in p
    assert "cannot prove completeness" in p


from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.services.knowledge_contracts import USABLE_STATUSES
from app.services.retrieval import NeighborExpansion
from app.models.schemas import NotebookCreate


@pytest.fixture
def rrepo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    # 隔离 LLM/推理端点：清空真实 key，避免本地 .env(env_file=../.env) 让 reasoning
    # 测试打真实网络(reasoning_llm_client 不 configured 时回退到测试桩 llm_client)。
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL"):
        monkeypatch.setenv(_k, "")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _seed_two_nodes(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "RTL到GDSII流程概述", "section_path": "1"}, "evidence": []},
        {"local_id": "P1", "object_type": "procedure",
         "payload": {"name": "布局布线步骤", "section_path": "2"}, "evidence": []},
    ], [
        {"source_local_id": "C1", "target_local_id": "P1",
         "edge_type": "depends_on", "evidence": []},
    ])
    return nb


def test_retrieve_scored_returns_sorted_hits(rrepo):
    nb = _seed_two_nodes(rrepo)
    hits = rrepo._retrieve_scored(nb.id, "RTL到GDSII流程")
    assert hits and hits[0].score >= (hits[-1].score if len(hits) > 1 else 0)
    assert any(h.object_type == "claim" for h in hits)


def test_retrieve_scored_filters_types(rrepo):
    nb = _seed_two_nodes(rrepo)
    hits = rrepo._retrieve_scored(nb.id, "布局布线", types=["procedure"])
    assert all(h.object_type == "procedure" for h in hits)


def test_retrieve_neighbors_follows_edges(rrepo):
    nb = _seed_two_nodes(rrepo)
    claim = next(h for h in rrepo._retrieve_scored(nb.id, "RTL到GDSII流程")
                 if h.object_type == "claim")
    expansion = rrepo._retrieve_neighbors(nb.id, claim.object_id)
    assert expansion.truncated is False
    neigh = expansion.hits
    assert any(n.object_type == "procedure" for n in neigh)
    # 邻居 relevance/score 为占位 0,最终由 run() 用原问题统一重打分(见 Task 8)
    assert all(n.relevance == 0.0 and n.score == 0.0 for n in neigh)


def test_retrieve_neighbors_edge_type_filter(rrepo):
    nb = _seed_two_nodes(rrepo)
    claim = next(h for h in rrepo._retrieve_scored(nb.id, "RTL到GDSII流程")
                 if h.object_type == "claim")
    assert rrepo._retrieve_neighbors(
        nb.id, claim.object_id, edge_type="nonexistent").hits == []


def _seed_hub(repo, fan_out: int = 4):
    """一个中心节点 + fan_out 个出边邻居(用于邻居展开上限的边界断言)。"""
    nb = repo.create_notebook(NotebookCreate(name="hub"))
    objects = [{"local_id": "H", "object_type": "claim",
                "payload": {"name": "枢纽节点"}, "evidence": []}]
    relations = []
    for i in range(fan_out):
        objects.append({"local_id": f"N{i}", "object_type": "procedure",
                        "payload": {"name": f"邻居{i}"}, "evidence": []})
        relations.append({"source_local_id": "H", "target_local_id": f"N{i}",
                          "edge_type": "depends_on", "evidence": []})
    repo.store_kg(nb.id, None, objects, relations)
    hub = next(h for h in repo._retrieve_scored(nb.id, "枢纽节点")
               if h.payload.get("name") == "枢纽节点")
    return nb, hub.object_id


def test_retrieve_neighbors_bounds_each_direction_and_reports_truncation(rrepo):
    """病态枢纽节点的邻居展开必须按上限有界,并如实报告被截断。"""
    nb, hub = _seed_hub(rrepo, fan_out=4)
    rrepo.settings.reasoning_neighbor_expand_limit = 2
    expansion = rrepo._retrieve_neighbors(nb.id, hub)
    assert expansion.truncated is True
    assert len(expansion.hits) == 2


def test_retrieve_neighbors_at_exactly_the_limit_is_not_truncated(rrepo):
    """恰好 limit 条邻居不算截断——哨兵行(limit+1)存在就是为了区分这两种。"""
    nb, hub = _seed_hub(rrepo, fan_out=2)
    rrepo.settings.reasoning_neighbor_expand_limit = 2
    expansion = rrepo._retrieve_neighbors(nb.id, hub)
    assert expansion.truncated is False
    assert len(expansion.hits) == 2


def test_neighbor_ids_without_limit_keeps_historical_unbounded_behaviour(rrepo):
    """limit=None 是其余调用方的既有口径:不加 ORDER BY/LIMIT,全量返回。"""
    nb, hub = _seed_hub(rrepo, fan_out=4)
    knowledge = rrepo.retrieval.candidates.knowledge
    with rrepo._connect() as db:
        rows = knowledge.neighbor_ids(
            db, nb.id, hub, endpoint="source_object_id")
        bounded = knowledge.neighbor_ids(
            db, nb.id, hub, endpoint="source_object_id", limit=3)
    assert len(rows) == 4
    assert len(bounded) == 3


def test_expand_path_pushes_the_bound_into_the_store_query(rrepo, monkeypatch):
    """上限必须真的到达 SQL,而不是取回全部再在 Python 里切。

    只断言 `truncated`/`len(hits)` 兜不住这条:在 Python 侧切片同样能让那两个
    断言变绿,而本特性要救的正是「先把百万邻接边取回内存」那一步。这里用 spy
    钉住两件事——每方向都带 `(上限+1) × _NEIGHBOR_ROW_OVERSCAN` 的读取界,且
    数据库**实际返回**的行数不超过它。
    """
    from app.services.retrieval_candidates import _NEIGHBOR_ROW_OVERSCAN

    nb, hub = _seed_hub(rrepo, fan_out=6)
    rrepo.settings.reasoning_neighbor_expand_limit = 2
    read_cap = (2 + 1) * _NEIGHBOR_ROW_OVERSCAN
    knowledge = rrepo.retrieval.candidates.knowledge
    original = knowledge.neighbor_ids
    calls = []

    def spy(db, notebook_id, object_id, *, endpoint, edge_type=None, limit=None,
            usable_statuses=None):
        rows = original(db, notebook_id, object_id, endpoint=endpoint,
                        edge_type=edge_type, limit=limit,
                        usable_statuses=usable_statuses)
        calls.append((endpoint, limit, tuple(usable_statuses or ()), len(rows)))
        return rows

    monkeypatch.setattr(knowledge, "neighbor_ids", spy)
    expansion = rrepo._retrieve_neighbors(nb.id, hub)

    assert [endpoint for endpoint, *_ in calls] == [
        "source_object_id", "target_object_id"]
    assert [limit for _, limit, _, _ in calls] == [read_cap, read_cap]
    # 状态合格性也要下推:事后过滤会让 deprecated 邻居白占读取窗口。
    assert [statuses for *_, statuses, _ in calls] == [
        tuple(USABLE_STATUSES), tuple(USABLE_STATUSES)]
    assert all(returned <= read_cap for *_, returned in calls)  # SQL 真的截了
    assert expansion.truncated is True
    assert len(expansion.hits) == 2      # 产出界仍是配置的上限


def _seed_hub_with_explicit_rows(repo, *, neighbours: int, relations,
                                 deprecated=()):
    """枢纽节点 + N 个邻居,关系 id **显式给定**。

    行序必须确定:`store_kg` 生成的是随机 id,而 `ORDER BY r.id` 决定「读取窗口
    里装的是哪几行」——行序随机的用例证明不了「靠后的合法邻居没被略过」。
    `relations` 是 (关系 id, 邻居序号, edge_type) 列表,`deprecated` 是要标成
    不可用状态的邻居序号。
    """
    nb = repo.create_notebook(NotebookCreate(name="hub-rows"))
    objects = [{"local_id": "H", "object_type": "claim",
                "payload": {"name": "枢纽节点"}, "evidence": []}]
    for index in range(neighbours):
        objects.append({"local_id": f"N{index}", "object_type": "concept",
                        "payload": {"name": f"邻居{index}"}, "evidence": []})
    repo.store_kg(nb.id, None, objects, [])

    ids = {}
    with repo._connect() as db:
        for row in db.execute(
            "SELECT id, payload FROM knowledge_objects WHERE notebook_id=?",
            (nb.id,),
        ):
            ids[json.loads(row["payload"])["name"]] = row["id"]
    hub = ids["枢纽节点"]

    knowledge = repo.retrieval.candidates.knowledge
    with repo._connect() as db:
        knowledge.insert_relation_chunk(db, [
            (rel_id, nb.id, None, hub, ids[f"邻居{index}"], edge_type, "[]",
             "2026-01-01T00:00:00+00:00")
            for rel_id, index, edge_type in relations
        ])
        for index in deprecated:
            db.execute(
                "UPDATE knowledge_objects SET status='deprecated' WHERE id=?",
                (ids[f"邻居{index}"],),
            )
    return nb, hub


def test_duplicate_and_unqueryable_relations_do_not_eat_the_neighbour_budget(
    rrepo,
):
    """预算的单位是唯一合格邻居,不是关系行。

    重复/佐证关系与不可查边都会消耗关系行;按行截断时它们把预算吃光,展开数
    远少于配置值,而靠后的合法邻居被整个略过——`visited` 又禁止重复展开,这一
    轮丢掉就再也捡不回来。
    """
    # 行序(按 id):邻居0 的三条重复关系 + 一条不可查边(claim→concept 上的
    # part_of 不在契约里),然后才是邻居1/2/3 各一条。旧的「按行截断」在
    # limit=3 时只读 4 行,全部落在邻居0 与那条废边上 → 只展开 1 个邻居。
    nb, hub = _seed_hub_with_explicit_rows(rrepo, neighbours=4, relations=[
        ("rel-01", 0, "defines"),
        ("rel-02", 0, "about"),
        ("rel-03", 0, "depends_on"),
        ("rel-04", 0, "part_of"),      # 不可查:被过扫描吸收
        ("rel-05", 1, "defines"),
        ("rel-06", 2, "defines"),
        ("rel-07", 3, "defines"),
    ])
    rrepo.settings.reasoning_neighbor_expand_limit = 3
    expansion = rrepo._retrieve_neighbors(nb.id, hub)

    names = sorted(hit.payload["name"] for hit in expansion.hits)
    assert names == ["邻居0", "邻居1", "邻居2"]   # 取满 3 个唯一邻居
    assert expansion.truncated is True            # 第 4 个唯一邻居仍在外面


def test_unusable_neighbours_do_not_eat_the_bounded_read_window(rrepo):
    """状态不合格的邻居必须在 SQL 里、LIMIT 之前就被排除。

    事后按 status 丢弃时,前排指向 deprecated 对象的关系白占了有界读取窗口,
    行序靠后的可用邻居被整个漏掉——相对旧的无界行为那是回归:可用邻居明明
    存在,却可能返回很少甚至零。
    """
    nb, hub = _seed_hub_with_explicit_rows(
        rrepo, neighbours=4,
        relations=[
            ("rel-01", 0, "defines"),   # ↓ 前三条都指向不可用对象
            ("rel-02", 1, "defines"),
            ("rel-03", 2, "defines"),
            ("rel-04", 3, "defines"),   # 唯一可用的邻居排在最后
        ],
        deprecated=(0, 1, 2),
    )
    rrepo.settings.reasoning_neighbor_expand_limit = 2
    expansion = rrepo._retrieve_neighbors(nb.id, hub)

    names = [hit.payload["name"] for hit in expansion.hits]
    assert names == ["邻居3"]          # 事后过滤的实现在这里拿到 0 个
    assert expansion.truncated is False  # 可用邻居只有一个,没有「还有更多」


def test_usable_object_rows_on_chunks_the_in_list(rrepo):
    """IN 列表分片:一条平铺的 IN 会在老 SQLite 构建上撞变量数上限。"""
    nb, hub = _seed_hub(rrepo, fan_out=3)
    knowledge = rrepo.retrieval.candidates.knowledge
    with rrepo._connect() as db:
        neighbours = [row["target_object_id"] for row in knowledge.neighbor_ids(
            db, nb.id, hub, endpoint="source_object_id")]
        rows = knowledge.usable_object_rows_on(
            db, neighbours, USABLE_STATUSES, batch_size=1)
    # 每片一条语句,结果与不分片时等价(顺序按片拼接)。
    assert {row["id"] for row in rows} == set(neighbours)


def test_retrieve_elements_degrades_gracefully(rrepo):
    nb = _seed_two_nodes(rrepo)
    # 无 source_elements 时返回空列表,不报错
    assert rrepo._retrieve_elements(nb.id, "任意查询") == []


def test_toolbox_delegates_to_repo(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    hits = rr.search(nb.id, "RTL到GDSII流程", types=["claim"], prefer="keyword")
    assert all(h.object_type == "claim" for h in hits)
    claim = hits[0]
    neigh = rr.neighbors(nb.id, claim.object_id).hits
    assert any(n.object_type == "procedure" for n in neigh)
    ctx = rr.get(nb.id, claim.object_id)
    assert ctx.get("object_type") == "claim"
    assert rr.get(nb.id, "no-such-id") == {}     # KeyError 吞掉
    assert rr.search_elements(nb.id, "x") == []   # 无原文不报错


class _StubLLM:
    """按 schema_hint 返回预置 JSON;configured 可控。"""
    def __init__(self, plan=None, reflect=None, configured=True):
        self._plan = plan
        self._reflect = reflect
        self.configured = configured
    def chat_json(self, messages, schema_hint, **kwargs):
        if "sub_queries" in schema_hint:
            return json.dumps(self._plan)
        return json.dumps(self._reflect)


def _rr_with_llm(repo, **llm):
    from app.services.reasoning_retrieval import ReasoningRetriever
    bind_chat_client(repo, "reasoning_agent", _StubLLM(**llm))
    return ReasoningRetriever.from_repository(repo, repo.settings)


class _KwargsRecordingLLM:
    """Records every chat_json call's kwargs so we can assert plan/reflect
    forward the reasoning-specific timeout/max_retries. Accepting **kwargs is
    itself part of the contract: the call sites must be passing them."""
    configured = True

    def __init__(self, plan, reflect):
        self._plan = plan
        self._reflect = reflect
        self.calls = []  # list of (schema_hint, kwargs)

    def chat_json(self, messages, schema_hint, **kwargs):
        self.calls.append((schema_hint, kwargs))
        if "sub_queries" in schema_hint:
            return json.dumps(self._plan)
        return json.dumps(self._reflect)


class _AnswerRecordingLLM:
    """Fake llm_client for repository answer paths: records chat_json kwargs,
    returns a minimal valid answer JSON."""
    configured = True

    def __init__(self):
        self.calls = []  # list of kwargs dicts

    def chat_json(self, messages, schema_hint, **kwargs):
        self.calls.append(kwargs)
        return json.dumps({"answer": "ok", "grounded": False})


def test_answer_reasoning_passes_reasoning_timeout_and_retries(rrepo):
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_timeout_seconds = 88
    rrepo.settings.reasoning_max_retries = 2
    llm = _AnswerRecordingLLM()
    bind_chat_client(rrepo, "ask_answer", llm)
    rrepo._answer_reasoning(nb.id, "问题", [], [], "")
    assert llm.calls, "_answer_reasoning must call chat_json"
    assert llm.calls[0].get("timeout") == 88
    assert llm.calls[0].get("max_retries") == 2


def test_refine_context_passes_reasoning_kwargs(rrepo):
    """Boundary guard: _refine_context passes timeout+max_retries (from settings)
    to the client — so the refine call inherits the same overrides as the
    reasoning answer call, keeping the two tightly coupled."""
    rrepo.settings.reasoning_timeout_seconds = 77
    rrepo.settings.reasoning_max_retries = 3
    rrepo.settings.kg_query_refine_enabled = True
    llm = _AnswerRecordingLLM()
    # Call _refine_context directly with a non-empty context block.
    result = rrepo._refine_context("问题", "k1: RTL到GDSII流程概述", llm)
    assert llm.calls, "_refine_context must call chat_json"
    assert llm.calls[0].get("timeout") == 77
    assert llm.calls[0].get("max_retries") == 3


def test_plan_passes_reasoning_timeout_and_retries(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    rrepo.settings.reasoning_timeout_seconds = 90
    rrepo.settings.reasoning_max_retries = 1
    llm = _KwargsRecordingLLM(plan={"sub_queries": [{"query": "q"}]}, reflect={})
    bind_chat_client(rrepo, "reasoning_agent", llm)
    ReasoningRetriever.from_repository(rrepo, rrepo.settings).plan("问题", "")
    assert llm.calls, "plan must call chat_json"
    _, kwargs = llm.calls[0]
    assert kwargs.get("timeout") == rrepo.settings.reasoning_timeout_seconds
    assert kwargs.get("max_retries") == rrepo.settings.reasoning_max_retries


def test_reflect_passes_reasoning_timeout_and_retries(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    rrepo.settings.reasoning_timeout_seconds = 77
    rrepo.settings.reasoning_max_retries = 3
    llm = _KwargsRecordingLLM(
        plan={}, reflect={"next_action": "answer", "sufficient": True})
    bind_chat_client(rrepo, "reasoning_agent", llm)
    ReasoningRetriever.from_repository(rrepo, rrepo.settings).reflect("问题", "summary")
    assert llm.calls, "reflect must call chat_json"
    _, kwargs = llm.calls[0]
    assert kwargs.get("timeout") == 77
    assert kwargs.get("max_retries") == 3


def test_plan_parses_subqueries(rrepo):
    rr = _rr_with_llm(rrepo, plan={"sub_queries": [
        {"query": "RTL综合", "types": ["claim"], "prefer": "keyword", "reason": "r"},
        {"query": "布线", "types": ["bogus"], "prefer": "weird"},
    ]})
    subs = rr.plan("问题", "")
    assert [s.query for s in subs] == ["RTL综合", "布线"]
    assert subs[0].types == ["claim"] and subs[0].prefer == "keyword"
    assert subs[1].types == [] and subs[1].prefer == "balanced"  # 非法值被清洗


def test_plan_truncates_to_max_subqueries(rrepo):
    rrepo.settings.reasoning_max_subqueries = 2
    rr = _rr_with_llm(rrepo, plan={"sub_queries": [
        {"query": "a"}, {"query": "b"}, {"query": "c"}]})
    assert len(rr.plan("q", "")) == 2


def test_plan_accepts_effort_specific_initial_query_cap(rrepo):
    """run 选档后可覆盖 planner 上限；未传时仍由 settings 管理。"""
    rrepo.settings.reasoning_max_subqueries = 5
    rr = _rr_with_llm(rrepo, plan={"sub_queries": [
        {"query": "a"}, {"query": "b"}, {"query": "c"}]})
    assert len(rr.plan("q", "", max_subqueries=2)) == 2
    assert len(rr.plan("q", "")) == 3


def test_plan_falls_back_on_bad_json(rrepo):
    rr = _rr_with_llm(rrepo, plan={"garbage": 1})
    subs = rr.plan("原问题X", "")
    assert len(subs) == 1 and subs[0].query == "原问题X"


def test_plan_falls_back_when_llm_unconfigured(rrepo):
    rr = _rr_with_llm(rrepo, configured=False)
    subs = rr.plan("原问题Y", "")
    assert len(subs) == 1 and subs[0].query == "原问题Y"


def test_reflect_parses_expand(rrepo):
    rr = _rr_with_llm(rrepo, reflect={
        "sufficient": False, "next_action": "expand_graph",
        "expand": {"object_id": "ko-1", "edge_type": "relates", "direction": "out"},
        "reason": "深挖"})
    d = rr.reflect("q", "summary")
    assert d.next_action == "expand_graph" and d.expand_object_id == "ko-1"
    assert d.expand_edge_type == "relates" and d.expand_direction == "out"


def test_reflect_bad_json_becomes_answer(rrepo):
    rr = _rr_with_llm(rrepo, reflect=["not", "a", "dict"])
    d = rr.reflect("q", "s")
    assert d.next_action == "answer" and d.sufficient is True


def test_reflect_falls_back_when_llm_unconfigured(rrepo):
    rr = _rr_with_llm(rrepo, reflect={"next_action": "expand_graph"}, configured=False)
    d = rr.reflect("q", "s")
    assert d.next_action == "answer" and d.sufficient is True


def test_reflect_parses_add_subquery(rrepo):
    rr = _rr_with_llm(rrepo, reflect={
        "next_action": "add_subquery",
        "new_sub_query": {"query": "补充查询", "types": ["procedure"], "prefer": "semantic"}})
    d = rr.reflect("q", "s")
    assert d.next_action == "add_subquery"
    assert d.new_sub_query is not None
    assert d.new_sub_query.query == "补充查询"
    assert d.new_sub_query.types == ["procedure"] and d.new_sub_query.prefer == "semantic"


def test_reflect_parses_search_elements(rrepo):
    rr = _rr_with_llm(rrepo, reflect={
        "next_action": "search_elements", "elements_query": "原文检索词"})
    d = rr.reflect("q", "s")
    assert d.next_action == "search_elements"
    assert d.elements_query == "原文检索词"


def test_reflect_parses_follow_chain(rrepo):
    rr = _rr_with_llm(rrepo, reflect={
        "next_action": "follow_chain",
        "follow_chain": {
            "start_object_id": "ko-a", "target_object_id": "ko-c",
            "edge_type": "derived_from", "direction": "in",
        },
    })
    d = rr.reflect("q", "s")
    assert d.next_action == "follow_chain"
    assert d.chain_start_object_id == "ko-a"
    assert d.chain_target_object_id == "ko-c"
    assert d.chain_edge_type == "derived_from"
    assert d.chain_direction == "in"


def _seed_follow_chain(repo):
    nb = repo.create_notebook(NotebookCreate(name="follow-chain"))
    repo.store_kg(nb.id, None, [
        {"local_id": "A", "object_type": "claim",
         "payload": {"name": "Premise A", "section_path": "A"}, "evidence": []},
        {"local_id": "B", "object_type": "claim",
         "payload": {"name": "Bridge B", "section_path": "B"}, "evidence": []},
        {"local_id": "C", "object_type": "claim",
         "payload": {"name": "Conclusion C", "section_path": "C"}, "evidence": []},
    ], [
        {"source_local_id": "A", "target_local_id": "B",
         "edge_type": "derived_from", "evidence": [{"quote": "A directly yields B"}]},
        {"source_local_id": "B", "target_local_id": "C",
         "edge_type": "derived_from", "evidence": [{"quote": "B directly yields C"}]},
    ])
    with repo._connect() as db:
        ids = {json.loads(r["payload"])["name"]: r["id"] for r in db.execute(
            "SELECT id,payload FROM knowledge_objects WHERE notebook_id=?", (nb.id,))}
    return nb, ids


class _SeqLLM:
    """plan 固定;reflect 按序列返回(耗尽后默认 answer)。"""
    configured = True
    def __init__(self, plan, reflects):
        self._plan = plan
        self._reflects = list(reflects)
    def chat_json(self, messages, schema_hint, **kwargs):
        if "sub_queries" in schema_hint:
            return json.dumps(self._plan)
        if self._reflects:
            return json.dumps(self._reflects.pop(0))
        return json.dumps({"next_action": "answer", "sufficient": True})


def test_run_plan_then_answer(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True, "reason": "够了"}]))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")
    assert res.top_hits  # 召回到候选
    kinds = [t.step_type for t in res.trace]
    assert kinds[0] == "plan" and "retrieve" in kinds and kinds[-1] == "answer"
    # record() 给每步回填墙钟耗时 —— 全部为非负整数,直达前端展示
    assert all(isinstance(t.duration_ms, int) and t.duration_ms >= 0 for t in res.trace)


def test_run_expand_graph_records_trace(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    claim = next(h for h in rrepo._retrieve_scored(nb.id, "RTL到GDSII流程")
                 if h.object_type == "claim")
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程", "types": ["claim"]}]},
        reflects=[
            {"next_action": "expand_graph", "expand": {"object_id": claim.object_id},
             "reason": "深挖关系"},
            {"next_action": "answer", "sufficient": True}]))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")
    assert any(t.step_type == "expand" for t in res.trace)
    assert any(h.object_type == "procedure" for h in res.top_hits)  # 邻居被纳入


def test_run_follow_chain_records_trace_and_keeps_transient_chain(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb, ids = _seed_follow_chain(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    streamed = []
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "Premise A derived conclusion"}]},
        reflects=[
            {"next_action": "follow_chain", "follow_chain": {
                "start_object_id": ids["Premise A"],
                "edge_type": "derived_from", "direction": "out"}},
            {"next_action": "answer", "sufficient": True},
        ],
    ))
    with rrepo._connect() as db:
        before = db.execute(
            "SELECT COUNT(*) c FROM knowledge_relations WHERE notebook_id=?", (nb.id,)
        ).fetchone()["c"]
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(
        nb.id, "Premise A 如何推到 Conclusion C", "", on_step=streamed.append)
    assert len(res.chains) == 1
    assert res.chains[0].target_name == "Conclusion C"
    assert res.chains[0].query_relevance > 0
    step = next(t for t in res.trace if t.step_type == "follow_chain")
    assert step.detail["hops"] == 2 and step.detail["count"] == 1
    assert step.detail["paths"] == [{
        "source": "Premise A", "via": "Bridge B", "target": "Conclusion C",
        "edge_type": "derived_from",
        "trust": pytest.approx(res.chains[0].chain_trust),
        "validity_scope": {},
    }]
    assert any(t.step_type == "follow_chain" for t in streamed)
    with rrepo._connect() as db:
        after = db.execute(
            "SELECT COUNT(*) c FROM knowledge_relations WHERE notebook_id=?", (nb.id,)
        ).fetchone()["c"]
    assert after == before == 2


def test_run_follow_chain_rejects_start_outside_current_candidates(rrepo, monkeypatch):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[
            {"next_action": "follow_chain", "follow_chain": {
                "start_object_id": "ko-guessed-not-a-candidate",
                "edge_type": "derived_from", "direction": "out"}},
            {"next_action": "answer", "sufficient": True},
        ],
    ))
    rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)

    def unexpected_follow_chain(*_args, **_kwargs):
        pytest.fail("follow_chain must not run for an arbitrary non-candidate id")

    monkeypatch.setattr(rr, "follow_chain", unexpected_follow_chain)
    result = rr.run(nb.id, "RTL到GDSII流程", "")
    skip = next(t for t in result.trace
                if t.detail.get("reason") == "chain_start_not_candidate")
    assert skip.step_type == "skip"
    assert result.chains == []


def test_answer_reasoning_renders_citable_hops_and_uncited_inference(rrepo):
    nb, ids = _seed_follow_chain(rrepo)
    chain_result = rrepo._follow_chain(
        nb.id, ids["Premise A"], edge_type="derived_from")
    assert chain_result.inferences

    class _ChainAnswerLLM:
        configured = True

        def __init__(self):
            self.prompt = ""

        def chat_json(self, messages, schema_hint, **kwargs):
            self.prompt = messages[-1]["content"]
            return json.dumps({
                "answer": (
                    "Premise A directly yields Bridge B and Bridge B directly "
                    "yields Conclusion C.[k2001, k2002] "
                    "（推断）Premise A therefore indirectly yields Conclusion C."
                ),
                "grounded": True,
            })

    llm = _ChainAnswerLLM()
    bind_chat_client(rrepo, "ask_answer", llm)
    rrepo.settings.kg_query_refine_enabled = False
    answer, grounded, anchors, _counts = rrepo._answer_reasoning(
        nb.id, "derive", chain_result.nodes, [], "",
        chains=chain_result.inferences)
    assert grounded is True
    assert "[Query-time typed inference; NOT directly stated]" in llm.prompt
    assert "Premise A --derived_from--> Bridge B" in llm.prompt
    assert "Bridge B --derived_from--> Conclusion C" in llm.prompt
    assert "attach NO [k] marker" in llm.prompt
    assert "（推断）" in answer
    assert [a.object_type for a in anchors] == ["relation", "relation"]
    assert [a.snippet for a in anchors] == ["A directly yields B", "B directly yields C"]


def test_grouped_answer_markers_fail_closed_when_any_key_is_unknown(rrepo):
    id_map = {
        "k2001": {
            "object_id": "rel-ab", "object_type": "relation",
            "name": "A --derived_from--> B",
        },
        "k2002": {
            "object_id": "rel-bc", "object_type": "relation",
            "name": "B --derived_from--> C",
        },
    }
    anchors = rrepo._parse_answer_anchors("premises [k2001, k2002]", id_map)
    assert [a.key for a in anchors] == ["k2001", "k2002"]
    assert rrepo._parse_answer_anchors("mixed [k2001, k9999]", id_map) == []


def test_run_dedups_expand_and_respects_step_cap(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    claim = next(h for h in rrepo._retrieve_scored(nb.id, "RTL到GDSII流程")
                 if h.object_type == "claim")
    rrepo.settings.reasoning_max_steps = 3
    # 始终要求 expand 同一节点 → 去重后无新增,且步数撞上限强制收尾
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "expand_graph",
                   "expand": {"object_id": claim.object_id}}] * 10))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")
    reflect_steps = [t for t in res.trace if t.step_type == "reflect"]
    assert len(reflect_steps) <= 3                 # circuit breaker 生效
    assert res.trace[-1].step_type == "answer"     # 仍正常收尾


def test_expand_of_already_collected_neighbors_attributes_nothing(rrepo):
    """codex #538 R3 P2:展开得到的邻居若早已在 collected 里(此处被初检索
    收过),这次 expand 什么都没贡献——found 照报原始邻居数,但 result_ids
    必须为空、零命中计数按空手轮累加。变异回「按原始 neigh 计」即红。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    claim = next(h for h in rrepo._retrieve_scored(nb.id, "RTL到GDSII流程")
                 if h.object_type == "claim")
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "expand_graph",
                   "expand": {"object_id": claim.object_id}},
                  {"next_action": "answer", "sufficient": True}]))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(
        nb.id, "RTL到GDSII流程", "")
    expand_step = next(t for t in res.trace if t.step_type == "expand")
    assert expand_step.detail["found"] >= 1, "前提:确实查到了邻居"
    assert expand_step.detail["result_ids"] == [], (
        "邻居已被初检索收集,这次 expand 零贡献,不得计入归因"
    )


def test_run_add_subquery_without_payload_continues(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    # add_subquery 但缺 new_sub_query: 应记 skip 并继续(不提前 break),下一轮才 answer
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "add_subquery"},
                  {"next_action": "answer", "sufficient": True}]))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")
    reflect_steps = [t for t in res.trace if t.step_type == "reflect"]
    assert len(reflect_steps) == 2                  # 两轮 reflect 都执行,未提前终止
    assert any(t.step_type == "skip" for t in res.trace)
    assert res.trace[-1].step_type == "answer"


def test_run_feeds_no_progress_signal_to_reflect_after_fruitless_retrieval(rrepo):
    """复现根因:某次检索动作未带来任何新证据时,下一轮 reflect 的输入必须携带
    '无新进展'信号,让模型据此自主决定是否直接作答;否则模型盲目重复同一动作,
    一路空转到 reasoning_max_steps —— 这正是推理模式'整体运行很久'的根因。

    注意:本用例不替模型拍板(不强制 answer),只断言信号被喂回 reflect。
    是否作答仍由模型决定(契合用户要求:始终进 reflect,由模型判断)。
    """
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)  # 该库无 source_elements → search_elements 恒返回 []

    captured_reflect_prompts: list[str] = []

    class _RecordingLLM:
        configured = True

        def __init__(self):
            # 第1轮 search_elements(必 0 新增,因无原文段),第2轮 answer
            self._reflects = [
                {"next_action": "search_elements", "elements_query": "q"},
                {"next_action": "answer", "sufficient": True},
            ]

        def chat_json(self, messages, schema_hint, **kwargs):
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [{"query": "RTL到GDSII流程"}]})
            captured_reflect_prompts.append(messages[-1]["content"])
            nxt = self._reflects.pop(0) if self._reflects else {
                "next_action": "answer", "sufficient": True}
            return json.dumps(nxt)

    bind_chat_client(rrepo, "reasoning_agent", _RecordingLLM())
    ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")

    assert len(captured_reflect_prompts) == 2
    # 初检索命中 KG 节点(有新增)→ 首轮 reflect 不应带"无新进展"信号
    assert "未带来新证据" not in captured_reflect_prompts[0]
    # search_elements 零新增 → 次轮 reflect 必须带"无新进展"信号(待实现)
    assert "未带来新证据" in captured_reflect_prompts[1]


def _mk_rk(object_id, name):
    """构造一个可辨识的 RetrievedKnowledge(payload.name 带标记,便于断言去重保留了哪条)。"""
    from app.services.retrieval import RetrievedKnowledge
    return RetrievedKnowledge(object_id=object_id, object_type="claim",
                              payload={"name": name})


def test_run_initial_retrieval_is_parallel(rrepo, monkeypatch):
    """并发性测试:≥3 个子查询的初检索必须并发执行。

    用 threading.Barrier(parties=子查询数) 证明:每个子查询的 search 调用内
    都 barrier.wait(timeout)。串行实现下只有 1 个线程能到达 barrier,wait 超时
    抛 BrokenBarrierError → 本测试 RED;并行实现下所有线程同时到达 → GREEN。

    reflect 桩首步即 answer,让 reflect 循环立刻结束,聚焦初检索。
    """
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_nodes(rrepo)
    subq = [{"query": "q1"}, {"query": "q2"}, {"query": "q3"}]
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": subq},
        reflects=[{"next_action": "answer", "sufficient": True}]))

    expected_parallel_queries = {item["query"] for item in subq}
    barrier = threading.Barrier(len(expected_parallel_queries))
    first_arrivals: set[str] = set()
    arrivals_lock = threading.Lock()

    def fake_search(self, notebook_id, query, types=None, prefer="balanced"):
        # Only each planned query's first call participates. Quota reranking
        # legitimately calls search() for the same queries again; feeding those
        # calls into the reusable Barrier started a second generation serially
        # and added a deterministic 3-second timeout to every successful run.
        with arrivals_lock:
            first_arrival = query not in first_arrivals
            first_arrivals.add(query)
        if query in expected_parallel_queries and first_arrival:
            # 串行:第一个线程在此 wait,无人来汇合 → 超时抛 BrokenBarrierError。
            # 并行:三个线程同时到达 → 全部放行。
            barrier.wait(timeout=1)
        return [_mk_rk(f"id-{query}", query)]

    monkeypatch.setattr(ReasoningRetriever, "search", fake_search)
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "原问题", "")

    # 并发成立才能跑到这里(否则 search 抛 BrokenBarrierError 被吞 → 三条都丢)。
    # 三个子查询各贡献一个不同 id → 初检索计数为 3。
    retrieve_steps = [t for t in res.trace if t.step_type == "retrieve"]
    assert retrieve_steps and retrieve_steps[0].detail["count"] == 3


def test_run_initial_retrieval_preserves_order_and_dedup(rrepo, monkeypatch):
    """顺序/去重确定性测试:并发后,重复 object_id 仍保留"按子查询顺序的第一个"版本。

    两个子查询命中有重叠 id 的结果但顺序不同:
      sq1 -> [shared(标记A), only1]
      sq2 -> [shared(标记B), only2]
    并发收集后,shared 必须保留 sq1 的版本(标记A),证明纳入顺序按子查询原序、
    而非线程完成顺序。用注入的可辨识 payload 直接断言去重保留了哪一条。

    注意:run() 末尾会用原问题对 collected 统一重打分,但这里注入的 id
    不在 _seed_two_nodes 的真实库内 → _retrieve_scored 取不到 → 回退到 collected
    版本,故 top_hits 里 shared 的 payload 即为去重保留的那条。
    """
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_nodes(rrepo)
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "first"}, {"query": "second"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))

    returns = {
        "first": [_mk_rk("shared", "A-from-first"), _mk_rk("only1", "only1")],
        "second": [_mk_rk("shared", "B-from-second"), _mk_rk("only2", "only2")],
    }

    def fake_search(self, notebook_id, query, types=None, prefer="balanced"):
        return returns[query]

    monkeypatch.setattr(ReasoningRetriever, "search", fake_search)
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "原问题", "")

    by_id = {h.object_id: h for h in res.top_hits}
    assert set(by_id) == {"shared", "only1", "only2"}      # 去重:shared 只一份
    # 第一个出现(sq1=first)的版本胜出
    assert by_id["shared"].payload["name"] == "A-from-first"


def test_run_initial_retrieval_swallows_single_search_failure(rrepo, monkeypatch):
    """容错:任一子查询 search 抛异常不应让整个 run 崩,失败者记空结果,其余正常。"""
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_nodes(rrepo)
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "boom"}, {"query": "ok"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))

    def fake_search(self, notebook_id, query, types=None, prefer="balanced"):
        if query == "boom":
            raise RuntimeError("search blew up")
        return [_mk_rk("ok-id", "ok")]

    monkeypatch.setattr(ReasoningRetriever, "search", fake_search)
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "原问题", "")

    retrieve_steps = [t for t in res.trace if t.step_type == "retrieve"]
    assert retrieve_steps[0].detail["count"] == 1          # 只剩成功者
    assert {h.object_id for h in res.top_hits} == {"ok-id"}


# ---- 退化循环熔断 (reasoning loop guard) ----

def test_reasoning_loop_guard_knobs():
    from app.core.config import Settings
    s = Settings()
    assert s.reasoning_stale_limit == 3
    assert s.reasoning_max_element_searches == 5


def test_run_stale_breaker_on_repeated_visited_expand(rrepo, monkeypatch):
    """模式A: reflect 反复请求展开同一已访问节点 → 连续无进展, stale 熔断提前收尾
    (远早于 reasoning_max_steps=50, 不再空转几十轮)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_max_steps = 50
    rrepo.settings.reasoning_stale_limit = 3
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": [_mk_rk("A", "nodeA")])
    monkeypatch.setattr(
        ReasoningRetriever, "neighbors",
        lambda self, n, oid, edge_type=None, direction="both": NeighborExpansion(
            [_mk_rk("B", "nodeB")]))
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "q"}]},
        reflects=[{"next_action": "expand_graph", "expand": {"object_id": "A"}}] * 40))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "q", "")
    reflect_steps = [t for t in res.trace if t.step_type == "reflect"]
    assert len(reflect_steps) <= 5             # stale 熔断: 远小于 50
    assert res.trace[-1].step_type == "answer" # 仍正常收尾


def test_run_caps_repeated_element_search(rrepo, monkeypatch):
    """模式B: reflect 反复 search_elements 且每次都有"新"原文段(no_progress 不触发),
    靠 element 搜索次数上限熔断, 不空转到 reasoning_max_steps。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    from app.services.retrieval import RetrievedElement
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_max_steps = 50
    rrepo.settings.reasoning_max_element_searches = 4
    rrepo.settings.reasoning_stale_limit = 3
    counter = {"n": 0}

    def fake_elements(self, n, q):
        counter["n"] += 1
        return [RetrievedElement(element_id=f"e{counter['n']}", source_id="s",
                                 source_title="src", location_label="L",
                                 element_type="paragraph", text="原文段")]

    monkeypatch.setattr(ReasoningRetriever, "search_elements", fake_elements)
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "q"}]},
        reflects=[{"next_action": "search_elements", "elements_query": "q"}] * 40))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "q", "")
    assert counter["n"] <= 4                   # 实际执行的 element 检索不超过上限
    reflect_steps = [t for t in res.trace if t.step_type == "reflect"]
    assert len(reflect_steps) < 20             # 远小于 50
    assert res.trace[-1].step_type == "answer"


def test_run_does_not_break_while_progressing(rrepo, monkeypatch):
    """熔断不误杀: 只要每轮 expand 带来新节点(有进展), stale 一直重置, 不提前终止
    —— 保证有效深挖不被熔断打断。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_max_steps = 50
    rrepo.settings.reasoning_stale_limit = 3
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": [_mk_rk("seed", "seed")])
    seq = {"n": 0}

    def fake_neighbors(self, n, oid, edge_type=None, direction="both"):
        seq["n"] += 1
        # 每轮全新邻居
        return NeighborExpansion([_mk_rk(f"nb{seq['n']}", f"nb{seq['n']}")])

    monkeypatch.setattr(ReasoningRetriever, "neighbors", fake_neighbors)
    reflects = [{"next_action": "expand_graph", "expand": {"object_id": f"x{i}"}}
                for i in range(5)]                          # 5 轮深挖不同节点
    reflects.append({"next_action": "answer", "sufficient": True})
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(plan={"sub_queries": [{"query": "q"}]}, reflects=reflects))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "q", "")
    reflect_steps = [t for t in res.trace if t.step_type == "reflect"]
    assert len(reflect_steps) == 6             # 5 轮有进展深挖 + 1 轮 answer, 未误熔断


def test_run_feeds_visited_nodes_to_reflect(rrepo, monkeypatch):
    """已访问节点回喂: 展开过的节点应出现在后续 reflect 的输入里, 提示模型勿重复请求
    (治模式A的根源——模型反复请求同一节点)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_stale_limit = 10  # 调高避免熔断先于断言触发
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": [_mk_rk("A", "nodeA")])
    monkeypatch.setattr(
        ReasoningRetriever, "neighbors",
        lambda self, n, oid, edge_type=None, direction="both": NeighborExpansion(
            [_mk_rk("B", "nodeB")]))
    prompts = []

    class _RecLLM:
        configured = True

        def __init__(self):
            self._r = [{"next_action": "expand_graph", "expand": {"object_id": "A"}},
                       {"next_action": "answer", "sufficient": True}]

        def chat_json(self, messages, schema_hint, **kw):
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [{"query": "q"}]})
            prompts.append(messages[-1]["content"])
            return json.dumps(self._r.pop(0) if self._r else {"next_action": "answer", "sufficient": True})

    bind_chat_client(rrepo, "reasoning_agent", _RecLLM())
    ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "q", "")
    assert len(prompts) >= 2
    # 第1轮 expand A 后, 第2轮 reflect 输入应带"已展开/已访问"节点提示, 含节点标识
    assert "nodeA" in prompts[1] or "A" in prompts[1]
    assert ("已展开" in prompts[1] or "已访问" in prompts[1] or "visited" in prompts[1].lower())


def _run_expand_once(rrepo, monkeypatch, *, truncated: bool):
    """跑一轮 expand_graph → answer,返回 (trace, 第二轮 reflect 的输入 prompt)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_stale_limit = 10
    monkeypatch.setattr(
        ReasoningRetriever, "search",
        lambda self, n, q, types=None, prefer="balanced": [_mk_rk("A", "nodeA")])
    monkeypatch.setattr(
        ReasoningRetriever, "neighbors",
        lambda self, n, oid, edge_type=None, direction="both": NeighborExpansion(
            [_mk_rk("B", "nodeB")], truncated))
    prompts = []

    class _RecLLM:
        configured = True

        def __init__(self):
            self._r = [{"next_action": "expand_graph", "expand": {"object_id": "A"}},
                       {"next_action": "answer", "sufficient": True}]

        def chat_json(self, messages, schema_hint, **kw):
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [{"query": "q"}]})
            prompts.append(messages[-1]["content"])
            return json.dumps(
                self._r.pop(0) if self._r
                else {"next_action": "answer", "sufficient": True})

    bind_chat_client(rrepo, "reasoning_agent", _RecLLM())
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "q", "")
    return res.trace, prompts


def test_run_discloses_neighbor_truncation_in_trace_and_reflect(rrepo, monkeypatch):
    """邻居被上限截断必须两处都可见:轨迹 detail 给用户,回喂账目给模型。
    模型看不到就会把「只展开了前 N 个」当成「这个节点只有这些邻居」。"""
    rrepo.settings.reasoning_neighbor_expand_limit = 7
    trace, prompts = _run_expand_once(rrepo, monkeypatch, truncated=True)
    expand = [t for t in trace if t.step_type == "expand"]
    assert expand and expand[0].detail["neighbor_truncated"] is True
    assert expand[0].detail["neighbor_limit"] == 7
    assert len(prompts) >= 2
    assert "超过单次展开的每方向上限7" in prompts[1]


def test_run_omits_neighbor_truncation_keys_when_not_truncated(rrepo, monkeypatch):
    """未截断的 expand 步 detail 逐键不变(不得无条件写 False),回喂也零变化。"""
    trace, prompts = _run_expand_once(rrepo, monkeypatch, truncated=False)
    expand = [t for t in trace if t.step_type == "expand"]
    assert expand
    assert "neighbor_truncated" not in expand[0].detail
    assert "neighbor_limit" not in expand[0].detail
    assert all("超过单次展开的每方向上限" not in p for p in prompts)


def _rk(oid, rel, otype="claim"):
    """构造带 relevance 的 RetrievedKnowledge(配额测试用)。"""
    from app.services.retrieval import RetrievedKnowledge
    return RetrievedKnowledge(object_id=oid, object_type=otype,
                              payload={"name": oid}, relevance=rel)


def test_quota_rerank_rescues_weak_group(rrepo, monkeypatch):
    """配额核心: 弱势子查询组(分数低)也保底进 top-N, 不被强势组通吃。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    per_q = {
        "qV3": [_rk("A", 0.5), _rk("B", 0.45)],
        "qR1": [_rk("C", 0.95), _rk("D", 0.9), _rk("E", 0.85), _rk("F", 0.8)],
    }
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": per_q.get(q, []))
    rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    collected = {oid: _rk(oid, 0.0) for oid in ["A", "B", "C", "D", "E", "F"]}
    hits, counts = rr._quota_rerank(nb.id, collected, ["qV3", "qR1"], top_n=2)
    ids = [h.object_id for h in hits]
    assert "A" in ids and "C" in ids       # 两组各贡献队首(全局会是 C,D)
    assert counts == [1, 1, 0]               # [qV3, qR1, 兜底组]: 各子查询 1 条、兜底 0


def test_quota_rerank_roundrobin_balance(rrepo, monkeypatch):
    """组大小悬殊(4 vs 2)时 top_n=4 内两组都有名额, 不被大组占满。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    per_q = {
        "qA": [_rk("a1", .9), _rk("a2", .8), _rk("a3", .7), _rk("a4", .6)],
        "qB": [_rk("b1", .95), _rk("b2", .85)],
    }
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": per_q.get(q, []))
    rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    collected = {oid: _rk(oid, 0.0) for oid in ["a1","a2","a3","a4","b1","b2"]}
    hits, counts = rr._quota_rerank(nb.id, collected, ["qA", "qB"], top_n=4)
    ids = {h.object_id for h in hits}
    assert "b1" in ids and "b2" in ids       # 小组的 2 条都进(round-robin 保底)
    assert counts == [2, 2, 0]                # [qA, qB, 兜底组]: 4 名额两组均分、兜底 0


def test_quota_rerank_tolerates_subquery_failure(rrepo, monkeypatch):
    """某子查询 search 抛错 → 该组空, 其余组正常出候选, 不崩。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    def fake_search(self, n, q, types=None, prefer="balanced"):
        if q == "boom":
            raise RuntimeError("search blew up")
        return [_rk("C", 0.9), _rk("D", 0.8)]
    monkeypatch.setattr(ReasoningRetriever, "search", fake_search)
    rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    collected = {oid: _rk(oid, 0.0) for oid in ["C", "D"]}
    hits, counts = rr._quota_rerank(nb.id, collected, ["boom", "ok"], top_n=2)
    ids = {h.object_id for h in hits}
    assert ids == {"C", "D"}                  # 失败组空, ok 组正常
    assert counts[0] == 0                      # 失败组贡献 0


def test_quota_rerank_fallback_group_last(rrepo, monkeypatch):
    """所有子查询都查不到的候选(relevance 全 0)进兜底组, 优先级最低但仍可入选。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": [_rk("A", 0.9)])
    rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    # X 不在任何子查询结果 → 兜底组
    collected = {"A": _rk("A", 0.0), "X": _rk("X", 0.0)}
    hits, counts = rr._quota_rerank(nb.id, collected, ["qA"], top_n=2)
    ids = [h.object_id for h in hits]
    assert ids[0] == "A"                       # 子查询组优先
    assert "X" in ids                          # 兜底组仍入选(名额没满时)
    assert counts[-1] == 1                     # 最后一个 count 是兜底组


def test_run_quota_path_keeps_both_groups(rrepo, monkeypatch):
    """复合(≥2 子查询)+ 开关开 → 走配额, top_hits 同时含两组候选(弱势组不被挤掉)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_quota_enabled = True
    rrepo.settings.retrieval_top_n = 2
    # 自适应预算下,"紧预算"由 cap 表达(retrieval_top_n 只是 floor,2 方面会被
    # per_query×2=6 抬高):cap=2 钉住总预算,保住本测试「配额救弱势组」的原意。
    rrepo.settings.reasoning_top_n_cap = 2
    # 用纯字母 key 避免 expand_query 内 normalize_terms 插入空格后 dict 查找失效
    per_q = {
        "subV": [_rk("A", 0.5), _rk("B", 0.45)],
        "subR": [_rk("C", 0.95), _rk("D", 0.9), _rk("E", 0.85)],
    }
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": per_q.get(q, []))
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "subV"}, {"query": "subR"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "subV subR", "")
    ids = {h.object_id for h in res.top_hits}
    assert "A" in ids and "C" in ids          # 配额救回弱势组 A(全局 top-2 会是 C,D)
    ans = next(t for t in res.trace if t.step_type == "answer")
    assert ans.detail.get("quota") == [1, 1]  # 可观测: 每子查询贡献数


def test_run_single_subquery_uses_global(rrepo, monkeypatch):
    """单子查询 → 不进配额, 走原全局重排(行为不变)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_quota_enabled = True
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": [_rk("A", 0.9)])
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "only"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "only", "")
    ans = next(t for t in res.trace if t.step_type == "answer")
    assert "quota" not in (ans.detail or {})   # 全局路径不带 quota


def test_run_quota_disabled_uses_global(rrepo, monkeypatch):
    """开关关 → 复合问题也走全局重排。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_quota_enabled = False
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": [_rk("A", 0.9)])
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "q1"}, {"query": "q2"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "q1 q2", "")
    ans = next(t for t in res.trace if t.step_type == "answer")
    assert "quota" not in (ans.detail or {})   # 开关关 → 全局路径


def test_plan_uses_expand_query(rrepo, monkeypatch):
    import app.services.query_rewrite as qr
    monkeypatch.setattr(qr, "expand_query", lambda *a, **k: qr.ExpandedQuery(
        query="x", sub_queries=[qr.SubQuerySpec("sub A", types=["concept"]),
                                qr.SubQuerySpec("sub B")]))
    from app.services.reasoning_retrieval import ReasoningRetriever
    r = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    subs = r.plan("中文复合问题")
    assert [s.query for s in subs] == ["sub A", "sub B"] and subs[0].types == ["concept"]


def test_fail_closed_reasoning_rejects_provider_and_malformed_reflection(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever

    class Broken:
        configured = True

        def chat_json(self, *_args, **_kwargs):
            raise RuntimeError("provider unavailable")

    bind_chat_client(rrepo, "reasoning_agent", Broken())
    retriever = ReasoningRetriever.from_repository(
        rrepo, rrepo.settings, fail_closed=True
    )
    with pytest.raises(RuntimeError, match="provider unavailable"):
        retriever.plan("q")
    with pytest.raises(RuntimeError, match="provider unavailable"):
        retriever.reflect("q", "evidence")

    class Invalid:
        configured = True

        def chat_json(self, *_args, **_kwargs):
            return '{"next_action":"bogus","sufficient":"false"}'

    bind_chat_client(rrepo, "reasoning_agent", Invalid())
    retriever = ReasoningRetriever.from_repository(
        rrepo, rrepo.settings, fail_closed=True
    )
    with pytest.raises(ValueError, match="invalid action"):
        retriever.reflect("q", "evidence")


def test_non_fail_closed_reflection_does_not_treat_string_false_as_true(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever

    class StringBoolean:
        configured = True

        def chat_json(self, *_args, **_kwargs):
            return '{"next_action":"answer","sufficient":"false","reason":"more"}'

    bind_chat_client(rrepo, "reasoning_agent", StringBoolean())
    decision = ReasoningRetriever.from_repository(
        rrepo, rrepo.settings, fail_closed=False
    ).reflect("q", "evidence")

    assert decision.sufficient is False


def test_run_expand_summary_uses_node_name_not_id(rrepo):
    """trace 可读性: expand step 的 summary 应显示节点名(人读), 而非裸 object_id。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    claim = next(h for h in rrepo._retrieve_scored(nb.id, "RTL到GDSII流程")
                 if h.object_type == "claim")
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程", "types": ["claim"]}]},
        reflects=[{"next_action": "expand_graph", "expand": {"object_id": claim.object_id}},
                  {"next_action": "answer", "sufficient": True}]))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")
    expand = next(t for t in res.trace if t.step_type == "expand")
    assert "RTL到GDSII流程概述" in expand.summary          # 人读名
    assert claim.object_id not in expand.summary           # 不再暴露裸 id
    assert expand.detail.get("name") == "RTL到GDSII流程概述"  # detail 带 name
    assert expand.detail.get("object_id") == claim.object_id  # detail 仍保留 id(机器/调试)


def test_run_duplicate_subquery_skipped_not_rerun(rrepo, monkeypatch):
    """add_subquery 重复已试过的子查询(含与初始 plan 重复、归一化等价)→ 硬跳过:
    不再执行 search,记 skip trace(reason=duplicate_subquery)。治「反复补充同一条
    子查询白烧检索」;跳过属零新增,stale 熔断语义不变。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)

    class _RepeatLLM:
        configured = True

        def __init__(self):
            self._reflects = [
                # 与 plan 子查询同文本 → 应跳过
                {"next_action": "add_subquery",
                 "new_sub_query": {"query": "RTL到GDSII流程"}},
                # 仅大小写/空白差异,归一化后仍重复 → 也应跳过
                {"next_action": "add_subquery",
                 "new_sub_query": {"query": "  rtl到gdsii流程 "}},
                {"next_action": "answer", "sufficient": True},
            ]

        def chat_json(self, messages, schema_hint, **kwargs):
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [{"query": "RTL到GDSII流程"}]})
            nxt = self._reflects.pop(0) if self._reflects else {
                "next_action": "answer", "sufficient": True}
            return json.dumps(nxt)

    bind_chat_client(rrepo, "reasoning_agent", _RepeatLLM())
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    calls: list[str] = []
    orig_search = retriever.search

    def _spy(nb_id, query, types=None, prefer="balanced"):
        calls.append(query)
        return orig_search(nb_id, query, types, prefer)

    monkeypatch.setattr(retriever, "search", _spy)
    steps = []
    retriever.run(nb.id, "RTL到GDSII流程", "", on_step=steps.append)

    # search 只在初检索执行 1 次;两次重复 add_subquery 均被跳过、未重跑
    assert calls == ["RTL到GDSII流程"]
    skips = [s for s in steps if s.step_type == "skip"
             and s.detail.get("reason") == "duplicate_subquery"]
    assert len(skips) == 2
    assert "跳过重复子查询" in skips[0].summary


def test_run_feeds_attempted_subqueries_to_reflect(rrepo):
    """已执行过的子查询账目(文本+新增证据数+尝试次数)必须回喂 reflect prompt:
    ①首轮即含初始 plan 的子查询与各自新增数(治「plan 对 reflect 不可见→首轮就
    复述 plan 已跑过的」);②重复被跳过后,下一轮 prompt 含尝试次数(账目变化使
    prompt 非不动点,LLM 缓存不会原样吐回上一轮决策)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)

    captured: list[str] = []

    class _RecordingRepeatLLM:
        configured = True

        def __init__(self):
            self._reflects = [
                {"next_action": "add_subquery",
                 "new_sub_query": {"query": "RTL到GDSII流程"}},  # 重复 plan → 跳过
                {"next_action": "answer", "sufficient": True},
            ]

        def chat_json(self, messages, schema_hint, **kwargs):
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [
                    {"query": "RTL到GDSII流程"}, {"query": "时序收敛方法"}]})
            captured.append(messages[-1]["content"])
            nxt = self._reflects.pop(0) if self._reflects else {
                "next_action": "answer", "sufficient": True}
            return json.dumps(nxt)

    bind_chat_client(rrepo, "reasoning_agent", _RecordingRepeatLLM())
    ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")

    assert len(captured) == 2
    # ① 首轮:初始 plan 两条子查询都在账目里,且带新增数与去重告诫
    assert "已执行过的子查询" in captured[0]
    assert "RTL到GDSII流程" in captured[0] and "时序收敛方法" in captured[0]
    assert "新增" in captured[0] and "勿重复" in captured[0]
    assert "已试" not in captured[0]          # 首轮各 1 次,不显示次数
    # ② 重复被跳过后:该条账目显示已试 2 次 → 两轮 prompt 必不同(破缓存不动点)
    assert "已试2次" in captured[1]
    assert captured[0] != captured[1]


def test_window_helper_head_tail_split():
    """_window: 超窗保留最早 head 条+最新 tail 条并报省略数;不超窗原样返回。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    head, tail, omitted = ReasoningRetriever._window(list(range(15)), 6, 4)
    assert head == list(range(6))
    assert tail == [11, 12, 13, 14]
    assert omitted == 5
    head, tail, omitted = ReasoningRetriever._window(list(range(10)), 6, 4)
    assert head == list(range(10)) and tail == [] and omitted == 0


def test_summarize_shows_recent_tail_when_over_window(rrepo):
    """collected 超 30 条时,summary 必须含最近加入的尾段(修「新增证据落在
    前 30 条窗口外 → summary 不变 → reflect 误判无进展/重复请求」盲区);
    ≤30 条时输出与旧行为完全一致(无省略标记)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    r = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    big = {f"ko-{i}": _mk_rk(f"ko-{i}", f"节点{i}") for i in range(45)}
    out = r._summarize(big, [], [])
    assert "节点0" in out and "节点19" in out        # 头段(最早 20 条)
    assert "节点35" in out and "节点44" in out       # 尾段(最新 10 条)
    assert "节点25" not in out                       # 中间被省略
    assert "省略中间 15 条" in out
    small = {f"ko-{i}": _mk_rk(f"ko-{i}", f"节点{i}") for i in range(30)}
    out2 = r._summarize(small, [], [])
    assert "省略" not in out2 and "节点29" in out2


def test_reflect_prompt_warns_against_resubmitting_tried_subqueries():
    """静态指令层也要有勿重复告诫(动态账目回喂之外的第二层):expand_graph
    文案明写可反复展开,add_subquery 原本连'勿重复'都没有——治理不对称。"""
    from app.services.prompts import reflect_prompt
    p = reflect_prompt("q", "s")
    assert "Never re-submit" in p


def test_run_exposes_attempted_ledger_and_top_n_override(rrepo):
    """报告管线依赖:run() 返回 attempted 账目(query/new/tries),且 top_n 参数
    覆盖 settings.retrieval_top_n(每节独立预算)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)

    class _PlanOnlyLLM:
        configured = True
        def chat_json(self, messages, schema_hint, **kwargs):
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [{"query": "RTL到GDSII流程"}]})
            return json.dumps({"next_action": "answer", "sufficient": True})

    bind_chat_client(rrepo, "reasoning_agent", _PlanOnlyLLM())
    result = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(
        nb.id, "RTL到GDSII流程", "", top_n=1)
    assert len(result.top_hits) <= 1                       # top_n 覆盖生效
    assert result.attempted and result.attempted[0]["query"] == "RTL到GDSII流程"
    assert set(result.attempted[0]) == {"query", "new", "tries"}


def test_run_max_steps_override_caps_reflect_loop(rrepo):
    """max_steps 覆盖 settings.reasoning_max_steps,封顶 reflect 轮数(报告滑块用)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    calls = {"reflect": 0}

    class _LoopLLM:
        configured = True
        def chat_json(self, messages, schema_hint, **kw):
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [{"query": "RTL到GDSII流程"}]})
            calls["reflect"] += 1
            return json.dumps({"next_action": "search_elements", "elements_query": "q"})  # 永不 answer

    bind_chat_client(rrepo, "reasoning_agent", _LoopLLM())
    ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", max_steps=2)
    assert calls["reflect"] <= 2      # 被 max_steps=2 封顶(而非 settings 的 50)


def test_run_overview_limits_reviewed_seeds_and_per_query_take(rrepo, monkeypatch):
    """首位完整问题保护种子必须保留；总种子数和每查询纳入数由档位封顶。"""
    from app.core.ask_retrieval_policy import ask_retrieval_limits
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_nodes(rrepo)
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "planner 不应执行"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
    ))
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)

    def fake_search(notebook_id, query, types=None, prefer="balanced"):
        return [_mk_rk(f"{query}-{i}", f"{query}-{i}") for i in range(10)]

    monkeypatch.setattr(retriever, "search", fake_search)
    result = retriever.run(
        nb.id,
        "完整问题",
        intent_queries=["完整问题", "方向一", "方向二"],
        limits=ask_retrieval_limits("overview"),
    )

    # overview:**首轮**最多 2 个查询,每查询纳入 4 条,所以初检索最多 8 条。
    # 首轮上限只约束首轮;第 3 条已确认方向由补种在步骤预算内顺延执行(见
    # test_run_covers_overflow_intent_directions_after_seed_passes),所以它出现在
    # attempted 里、但不计入首轮那一步的 count。
    retrieve = next(step for step in result.trace if step.step_type == "retrieve")
    assert retrieve.detail["count"] == 8
    assert [row["query"] for row in result.attempted] == ["完整问题", "方向一", "方向二"]
    answer = result.trace[-1]
    assert answer.detail["top_n"] == 8


class _RecordingSeqLLM(_SeqLLM):
    """_SeqLLM + 留存每轮 reflect 的完整 prompt(断言回喂账目段用)。"""

    def __init__(self, plan, reflects):
        super().__init__(plan, reflects)
        self.reflect_prompts = []

    def chat_json(self, messages, schema_hint, **kwargs):
        if "sub_queries" not in schema_hint:
            self.reflect_prompts.append(messages[-1]["content"])
        return super().chat_json(messages, schema_hint, **kwargs)


def _coverage_retrieves(trace):
    """补种步(retrieve + source=confirmed_intent),按轨迹顺序。"""
    return [s for s in trace
            if s.step_type == "retrieve"
            and (s.detail or {}).get("source") == "confirmed_intent"]


_UNCOVERED_MARK = "尚未执行"


def _uncovered_block(prompt):
    """reflect prompt 里"未执行的已确认方向"那一段(不含其他账目段)。

    只断言整份 prompt 里有没有某个方向名是不够的:被模型补上的方向照样出现在
    「已执行过的子查询」账目里(那是正确的),只有这一段该把它摘掉。"""
    if _UNCOVERED_MARK not in prompt:
        return ""
    tail = prompt.split(_UNCOVERED_MARK, 1)[1]
    return tail.split("）", 1)[0]


def test_intent_direction_label_keeps_only_the_reviewed_first_line():
    """方向种子是「方向 + 整份已确认问题契约」的复合串(最长 8000 字符)。
    轨迹与 reflect 回喂只能拿首行的有界简称,否则一条方向就顶掉半屏。"""
    from app.services.reasoning_retrieval import intent_direction_label

    seed = "ICC2 中的布局优化命令\n\n检索必须服从以下已确认问题契约：\n" + "契" * 4000
    label = intent_direction_label(seed)
    assert label == "ICC2 中的布局优化命令"
    long_head = "名" * 200
    assert len(intent_direction_label(long_head)) == 61      # 60 字符 + 省略号
    assert intent_direction_label(long_head).endswith("…")
    assert intent_direction_label("\n\n  前有空行  \n后续") == "前有空行"
    assert intent_direction_label("") == ""


def test_run_covers_overflow_intent_directions_after_seed_passes(rrepo, monkeypatch):
    """首轮装不下的已确认方向必须在同一次 run 内补齐,且排在 seed pass 之后、
    reflect 之前 —— 「延迟但保证」而不是「截断即丢弃」。

    问题里带一个可探测标识符(set_db),让 PPR 与精确查找两条 seed pass
    都真实触发 —— 原问题「完整问题」不含标识符,exact_lookup 半支的顺序断言
    因 `if seed_kind in kinds` 恒假而从未真正执行过(F5)。"""
    from app.core.ask_retrieval_policy import ask_retrieval_limits
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_nodes(rrepo)
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "planner 不应执行"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
    ))
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    monkeypatch.setattr(
        retriever, "search",
        lambda notebook_id, query, types=None, prefer="balanced": [
            _mk_rk(f"{query}-{i}", f"{query}-{i}") for i in range(4)
        ])

    result = retriever.run(
        nb.id,
        "完整问题 set_db",
        # overview:首轮 2 个 → 主题二/主题三 溢出到补种(步骤上限 4,够用)。
        intent_queries=["完整问题", "主题一方向", "主题二方向", "主题三方向"],
        limits=ask_retrieval_limits("overview"),
    )

    kinds = [s.step_type for s in result.trace]
    covered = _coverage_retrieves(result.trace)
    assert covered, "首轮装不下的已确认方向必须补种"
    # 顺序:首轮 → (PPR/精确)seed pass → 补种 → reflect。先断顺序再断内容 ——
    # 把补种挪到 reflect 循环之后同样能"补齐",却已经不是本特性要的东西了。
    positions = [result.trace.index(s) for s in covered]
    assert kinds.index("retrieve") < min(positions)       # 首轮初检索在前
    # 两条 seed pass 都必须真实进入轨迹(而非"没进入就跳过断言"的假覆盖)。
    assert "ppr" in kinds and "exact_lookup" in kinds
    for seed_kind in ("ppr", "exact_lookup"):
        assert kinds.index(seed_kind) < min(positions)
    assert max(positions) < kinds.index("reflect")        # 整段都在 reflect 之前
    assert [s.detail["query"] for s in covered] == ["主题二方向", "主题三方向"]
    # 三个主题方向全部真的执行过(账目是执行的证据,不是意图的证据)。
    assert [row["query"] for row in result.attempted] == [
        "完整问题", "主题一方向", "主题二方向", "主题三方向"]
    # 预算充足 → 不应出现"未能执行"的披露步。
    assert not [s for s in result.trace if s.step_type == "skip"]


def test_run_discloses_and_feeds_back_uncovered_intent_directions(rrepo, monkeypatch):
    """步骤预算不足时:未执行方向必须上轨迹披露,并回喂 reflect 让模型优先补齐。"""
    from app.core.ask_retrieval_policy import ask_retrieval_limits
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_nodes(rrepo)
    llm = _RecordingSeqLLM(
        plan={"sub_queries": [{"query": "planner 不应执行"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
    )
    bind_chat_client(rrepo, "reasoning_agent", llm)
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    monkeypatch.setattr(
        retriever, "search",
        lambda notebook_id, query, types=None, prefer="balanced": [
            _mk_rk(f"{query}-0", query)])

    result = retriever.run(
        nb.id,
        "完整问题",
        # overview:首轮 2 条、max_steps=4 → 补种预算 = 4//2 = 2(另一半留给
        # reflect,回喂才到得了模型);待补 4 条 → 执行 2 条、披露 2 条。
        intent_queries=["完整问题", "方向一", "方向二", "方向三", "方向四", "方向五"],
        limits=ask_retrieval_limits("overview"),
    )

    assert [s.detail["query"] for s in _coverage_retrieves(result.trace)] == [
        "方向二", "方向三"]
    skips = [s for s in result.trace
             if s.step_type == "skip"
             and (s.detail or {}).get("reason") == "intent_coverage_incomplete"]
    assert len(skips) == 1
    assert skips[0].detail["pending"] == 2
    assert skips[0].detail["directions"] == ["方向四", "方向五"]
    assert "方向四" in skips[0].summary and "方向五" in skips[0].summary
    # 同一事实回喂 reflect:模型知道哪些方向没跑过、该优先补哪个。
    assert llm.reflect_prompts
    block = _uncovered_block(llm.reflect_prompts[0])
    assert "「方向四」" in block and "「方向五」" in block
    assert "add_subquery" in block


def test_run_discloses_pending_directions_beyond_disclose_cap(rrepo, monkeypatch):
    """_INTENT_PENDING_DISCLOSE=8:未执行方向数超过 8 个时,披露步与 reflect 回喂
    都必须走"列出前 8 个 + 等 N 个"的截断措辞,而不是把全部方向不加节制地铺满
    一屏。上一条用例(2 个未执行)从没走到过这条截断分支。"""
    from app.core.ask_retrieval_policy import ask_retrieval_limits
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_nodes(rrepo)
    llm = _RecordingSeqLLM(
        plan={"sub_queries": [{"query": "planner 不应执行"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
    )
    bind_chat_client(rrepo, "reasoning_agent", llm)
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    monkeypatch.setattr(
        retriever, "search",
        lambda notebook_id, query, types=None, prefer="balanced": [
            _mk_rk(f"{query}-0", query)])

    directions = [f"方向{i}" for i in range(1, 14)]      # 13 个方向
    result = retriever.run(
        nb.id,
        "完整问题",
        # overview:首轮 2 → 补种预算 2 → 待补 12 个,执行 2、剩 10 个未执行
        # (>_INTENT_PENDING_DISCLOSE=8,真正触及截断分支)。
        intent_queries=["完整问题", *directions],
        limits=ask_retrieval_limits("overview"),
    )

    skips = [s for s in result.trace
             if s.step_type == "skip"
             and (s.detail or {}).get("reason") == "intent_coverage_incomplete"]
    assert len(skips) == 1
    skip = skips[0]
    assert skip.detail["pending"] == 10
    assert len(skip.detail["directions"]) == 8             # 只展开前 8 个
    assert "等 10 个" in skip.summary
    # reflect 回喂同一份账目,受同一个上界约束(镜像披露步)。
    assert llm.reflect_prompts
    block = _uncovered_block(llm.reflect_prompts[0])
    assert block.count("「") == 8                          # 只列前 8 个方向
    assert "等 10 个" in block


def test_uncovered_intent_feedback_drops_directions_the_model_covered(
    rrepo, monkeypatch
):
    """模型用 add_subquery 补上某条未执行方向后,回喂账目必须当场把它摘掉 ——
    账目撒谎(说没跑过其实跑过了)会让模型在同一条上空转。

    生产形状:意图种子不是裸短字符串,而是「方向 + 完整已确认问题契约」的复合串
    (confirmed_intent_queries 产出,截到 8000 字符,见 query_intent.py);模型在
    prompt 里只见过 intent_direction_label 截出的简称,回喂时也只能用简称
    add_subquery。匹配必须在简称空间做,直接比较复合串原文永远不相等——这正是
    两轮评审实测复现的回归(第二轮 prompt 会同时说"没执行过"又"别重复提交",
    模型无所适从、重提被防重跳过白烧一轮)。"""
    from app.core.ask_retrieval_policy import ask_retrieval_limits
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_nodes(rrepo)
    authoritative = "完整问题"

    def compound(direction):
        # 与 query_intent.confirmed_intent_queries 逐字同形。
        return f"{direction}\n\n检索必须服从以下已确认问题契约：\n{authoritative}"

    llm = _RecordingSeqLLM(
        plan={"sub_queries": [{"query": "planner 不应执行"}]},
        reflects=[
            {"next_action": "add_subquery", "new_sub_query": {"query": "方向四"}},
            {"next_action": "answer", "sufficient": True},
        ],
    )
    bind_chat_client(rrepo, "reasoning_agent", llm)
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    monkeypatch.setattr(
        retriever, "search",
        lambda notebook_id, query, types=None, prefer="balanced": [
            _mk_rk(f"{query}-0", query)])

    retriever.run(
        nb.id,
        authoritative,
        intent_queries=[
            authoritative, compound("方向一"), compound("方向二"),
            compound("方向三"), compound("方向四"), compound("方向五"),
        ],
        limits=ask_retrieval_limits("overview"),
    )

    assert len(llm.reflect_prompts) >= 2
    first = llm.reflect_prompts[0]
    assert "「方向四」" in _uncovered_block(first)
    later = llm.reflect_prompts[1]
    uncovered_later = _uncovered_block(later)
    assert "「方向四」" not in uncovered_later           # 已被模型补上 → 摘掉
    assert "「方向五」" in uncovered_later               # 仍未覆盖 → 保留
    # 不是只看 uncovered 段摘没摘掉:第二轮 prompt 的"已执行过的子查询"段必须
    # 认下这次补交,两段不能自相矛盾(一段说没执行、另一段假装什么都没发生)。
    assert "已执行过的子查询" in later
    tried_block = later.split("已执行过的子查询", 1)[1].split("）", 1)[0]
    assert "方向四" in tried_block


def test_reflect_feedback_renders_intent_seed_labels_not_full_contract_text(
    rrepo, monkeypatch
):
    """「已执行过的子查询」回喂账目对来自已确认意图种子的条目必须只渲染
    intent_direction_label 简称,不能把「方向 + 完整已确认问题契约」的复合串
    原样重放——那会让契约尾巴随条目数线性重复(实测 +150%,契约上限 8000
    字符/条,一条方向就能顶掉半屏)。"""
    from app.core.ask_retrieval_policy import ask_retrieval_limits
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_nodes(rrepo)
    authoritative = "完整问题"
    contract_marker = "检索必须服从以下已确认问题契约"

    def compound(direction):
        return f"{direction}\n\n{contract_marker}：\n{authoritative}"

    llm = _RecordingSeqLLM(
        plan={"sub_queries": [{"query": "planner 不应执行"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
    )
    bind_chat_client(rrepo, "reasoning_agent", llm)
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    # 候选证据的 payload.name 刻意不回显 query 本身(用稳定字符串 + query 的哈希
    # 只保区分度),这样 prompt 里出现契约标志串只可能来自"已执行过的子查询"
    # 账目渲染,不会被候选摘要段落里的"名字恰好等于查询"混进来。
    monkeypatch.setattr(
        retriever, "search",
        lambda notebook_id, query, types=None, prefer="balanced": [
            _mk_rk(f"id-{abs(hash(query))}", "候选证据")])

    retriever.run(
        nb.id,
        authoritative,
        # overview 首轮宽度=2:方向一进首轮、方向二溢出到补种 —— 覆盖"首轮切片"
        # 与"补种"两个 label 写入点。
        intent_queries=[authoritative, compound("方向一"), compound("方向二")],
        limits=ask_retrieval_limits("overview"),
    )

    assert llm.reflect_prompts
    prompt = llm.reflect_prompts[0]
    assert contract_marker not in prompt
    assert "「方向一」" in prompt         # 首轮切片的 label
    assert "「方向二」" in prompt         # 补种的 label


def test_direction_registry_disambiguates_same_prefix_labels():
    """两个方向的默认 60 字符简称完全相同(截断点之前逐字一致)时,注册表必须
    先加宽展示窗口消解碰撞——不能让两个不同方向共用同一个展示身份,否则覆盖
    账目会把两者的执行状态并成一个(PR#400 codex R1 P2-3)。"""
    from app.services.reasoning_retrieval import (
        _build_direction_registry, _norm_query, intent_direction_label,
    )

    shared_head = "共享前缀" * 20            # 80 字符,超过 60 字符默认截断点
    q1 = shared_head + "甲\n\n检索必须服从以下已确认问题契约：\n" + "契" * 200
    q2 = shared_head + "乙\n\n检索必须服从以下已确认问题契约：\n" + "契" * 200

    # 前提:默认宽度下两者的简称确实碰撞(否则本用例没有测到该分支)。
    assert intent_direction_label(q1) == intent_direction_label(q2)

    label_of, direction_of = _build_direction_registry([q1, q2])

    assert label_of[q1] != label_of[q2]
    assert direction_of[_norm_query(label_of[q1])] == q1
    assert direction_of[_norm_query(label_of[q2])] == q2


def test_run_tracks_colliding_default_labels_independently(rrepo, monkeypatch):
    """验收 1:两个已确认方向撞出同一默认简称时,注册表给出互异简称,且覆盖
    账目必须独立追踪二者——一个被执行后,另一个仍然出现在未覆盖清单里(不能
    因为简称一度相同就把两者的执行状态并成一个)。"""
    from app.core.ask_retrieval_policy import ask_retrieval_limits
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_nodes(rrepo)
    authoritative = "完整问题"
    shared_head = "共享前缀" * 20

    def compound(tail):
        return f"{shared_head}{tail}\n\n检索必须服从以下已确认问题契约：\n{authoritative}"

    q_a = compound("甲")
    q_b = compound("乙")

    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "planner 不应执行"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
    )
    bind_chat_client(rrepo, "reasoning_agent", llm)
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    monkeypatch.setattr(
        retriever, "search",
        lambda notebook_id, query, types=None, prefer="balanced": [
            _mk_rk(f"id-{abs(hash(query))}", "候选证据")])

    # authoritative、"占位方向" 装满首轮(overview 首轮宽度 2),q_a/q_b 都溢出到
    # 补种;max_steps=3 → 补种预算 3//2=1,只够覆盖 q_a,q_b 留在未覆盖清单。
    result = retriever.run(
        nb.id,
        authoritative,
        intent_queries=[authoritative, "占位方向", q_a, q_b],
        max_steps=3,
        limits=ask_retrieval_limits("overview"),
    )

    attempted_queries = [row["query"] for row in result.attempted]
    assert q_a in attempted_queries
    assert q_b not in attempted_queries

    skips = [s for s in result.trace
             if (s.detail or {}).get("reason") == "intent_coverage_incomplete"]
    assert len(skips) == 1
    shown = skips[0].detail["directions"]
    assert len(shown) == 1
    # 未覆盖的是 q_b(含"乙"),展示的简称必须是它独有的、区别于 q_a(含"甲")的
    # 简称——而不是两者共享的默认截断前缀。
    assert "乙" in shown[0]
    assert "甲" not in shown[0]


def test_add_subquery_resubmitting_label_of_seeded_direction_is_treated_as_duplicate(
    rrepo, monkeypatch
):
    """验收 2:补种(coverage pass)已经执行过某个已确认方向后,模型若按
    prompt 里看到的简称用 add_subquery 重提同一方向,必须被识别为重复、检索
    零执行——即使 attempted 账目实际按完整 compound 串记账、模型提交的只是
    简称。否则模型换用简称就能绕过 duplicate_subquery,白烧一轮检索预算
    (PR#400 codex R1 P2-2)。"""
    from app.core.ask_retrieval_policy import ask_retrieval_limits
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_nodes(rrepo)
    authoritative = "完整问题"

    def compound(direction):
        return f"{direction}\n\n检索必须服从以下已确认问题契约：\n{authoritative}"

    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "planner 不应执行"}]},
        reflects=[
            {"next_action": "add_subquery", "new_sub_query": {"query": "方向二"}},
            {"next_action": "answer", "sufficient": True},
        ],
    )
    # 收尾配额重排(_quota_rerank)会为每个 used_query 再打一遍分,那是既有的
    # 独立行为、与本用例要证明的"简称重提不重跑检索"无关——关掉它才能让
    # calls 只反映检索循环本身发起的调用。
    rrepo.settings.reasoning_quota_enabled = False
    bind_chat_client(rrepo, "reasoning_agent", llm)
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    calls: list[str] = []

    def fake_search(notebook_id, query, types=None, prefer="balanced"):
        calls.append(query)
        return [_mk_rk(f"id-{abs(hash(query))}", "候选证据")]

    monkeypatch.setattr(retriever, "search", fake_search)

    # overview 首轮宽度 2:authoritative + compound("方向一") 进首轮,
    # compound("方向二") 溢出到补种;补种预算 4//2=2 足够覆盖它。
    result = retriever.run(
        nb.id,
        authoritative,
        intent_queries=[authoritative, compound("方向一"), compound("方向二")],
        limits=ask_retrieval_limits("overview"),
    )

    assert calls.count(compound("方向二")) == 1        # 补种已经真的执行过一次
    dup_skips = [s for s in result.trace if s.step_type == "skip"
                 and (s.detail or {}).get("reason") == "duplicate_subquery"]
    assert len(dup_skips) == 1
    assert dup_skips[0].detail["query"] == "方向二"
    # 简称重提没有触发第二次 search 调用 —— 检索零执行,步数不浪费。
    assert calls.count(compound("方向二")) == 1


def test_add_subquery_label_matches_uncovered_direction_executes_compound(
    rrepo, monkeypatch
):
    """验收 3:模型用简称补交一个尚未执行的已确认方向时,必须以完整 compound
    (方向+已确认问题契约)执行、账目记在 compound 身份上而非裸简称;该方向
    随即从未覆盖清单摘除,且后续再用相同简称重提会被防重拦下(不重跑检索)。"""
    from app.core.ask_retrieval_policy import ask_retrieval_limits
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_nodes(rrepo)
    authoritative = "完整问题"

    def compound(direction):
        return f"{direction}\n\n检索必须服从以下已确认问题契约：\n{authoritative}"

    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "planner 不应执行"}]},
        reflects=[
            {"next_action": "add_subquery", "new_sub_query": {"query": "方向四"}},
            {"next_action": "add_subquery", "new_sub_query": {"query": "方向四"}},
        ],
    )
    # 关闭收尾配额重排(与上一条用例同理:那是独立行为,不是本用例要证明的东西)。
    rrepo.settings.reasoning_quota_enabled = False
    bind_chat_client(rrepo, "reasoning_agent", llm)
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    calls: list[str] = []

    def fake_search(notebook_id, query, types=None, prefer="balanced"):
        calls.append(query)
        return [_mk_rk(f"id-{abs(hash(query))}", "候选证据")]

    monkeypatch.setattr(retriever, "search", fake_search)

    result = retriever.run(
        nb.id,
        authoritative,
        intent_queries=[
            authoritative, compound("方向一"), compound("方向二"),
            compound("方向三"), compound("方向四"), compound("方向五"),
        ],
        limits=ask_retrieval_limits("overview"),
    )

    # 第一次简称补交必须真的按完整 compound 执行(不是裸简称 "方向四")。
    assert calls.count(compound("方向四")) == 1
    assert "方向四" not in calls
    # 账目记在 compound 身份上,不是裸简称。
    attempted_queries = [row["query"] for row in result.attempted]
    assert compound("方向四") in attempted_queries
    assert "方向四" not in attempted_queries
    # 第二次用相同简称重提必须被防重拦下,不再调用 search。
    dup_skips = [s for s in result.trace if s.step_type == "skip"
                 and (s.detail or {}).get("reason") == "duplicate_subquery"]
    assert len(dup_skips) == 1
    assert dup_skips[0].detail["query"] == "方向四"
    assert calls.count(compound("方向四")) == 1


def test_run_terminal_disclosure_reflects_final_coverage_after_reflect_covers_one(
    rrepo, monkeypatch
):
    """验收 4(部分补齐):预算耗尽留 2 个未执行方向,reflect 轮用 add_subquery
    补上其中 1 个后,最终轨迹的 skip 步必须只列剩下的 1 个——不能还停留在
    "预算刚耗尽那一刻"的旧账(PR#400 codex R1 P2-1)。"""
    from app.core.ask_retrieval_policy import ask_retrieval_limits
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_nodes(rrepo)
    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "planner 不应执行"}]},
        reflects=[
            {"next_action": "add_subquery", "new_sub_query": {"query": "方向四"}},
            {"next_action": "answer", "sufficient": True},
        ],
    )
    bind_chat_client(rrepo, "reasoning_agent", llm)
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    monkeypatch.setattr(
        retriever, "search",
        lambda notebook_id, query, types=None, prefer="balanced": [
            _mk_rk(f"{query}-0", query)])

    result = retriever.run(
        nb.id,
        "完整问题",
        # overview:首轮 2、补种预算 2 → 方向二/方向三 补种执行,方向四/方向五
        # 未覆盖;reflect 第一轮用 add_subquery 把方向四补上。
        intent_queries=["完整问题", "方向一", "方向二", "方向三", "方向四", "方向五"],
        limits=ask_retrieval_limits("overview"),
    )

    skips = [s for s in result.trace
             if s.step_type == "skip"
             and (s.detail or {}).get("reason") == "intent_coverage_incomplete"]
    assert len(skips) == 1
    assert skips[0].detail["pending"] == 1
    assert skips[0].detail["directions"] == ["方向五"]
    assert "方向四" not in skips[0].summary
    assert "方向五" in skips[0].summary


def test_run_terminal_disclosure_has_no_skip_when_reflect_covers_all_pending(
    rrepo, monkeypatch
):
    """验收 4(全部补齐):全部未执行方向都被 reflect 用 add_subquery 补上后,
    终态不应再出现 intent_coverage_incomplete 的 skip 步。"""
    from app.core.ask_retrieval_policy import ask_retrieval_limits
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_nodes(rrepo)
    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "planner 不应执行"}]},
        reflects=[
            {"next_action": "add_subquery", "new_sub_query": {"query": "方向四"}},
            {"next_action": "add_subquery", "new_sub_query": {"query": "方向五"}},
        ],
    )
    bind_chat_client(rrepo, "reasoning_agent", llm)
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    monkeypatch.setattr(
        retriever, "search",
        lambda notebook_id, query, types=None, prefer="balanced": [
            _mk_rk(f"{query}-0", query)])

    result = retriever.run(
        nb.id,
        "完整问题",
        intent_queries=["完整问题", "方向一", "方向二", "方向三", "方向四", "方向五"],
        limits=ask_retrieval_limits("overview"),
    )

    assert not [s for s in result.trace
                if (s.detail or {}).get("reason") == "intent_coverage_incomplete"]


@pytest.mark.parametrize("intent_queries", [None, ["完整问题", "方向一"]])
def test_intent_coverage_is_neutral_without_overflow(
    rrepo, monkeypatch, intent_queries
):
    """中性回归:intent_queries 为空、或长度未超过首轮上限时,补种账目恒空 ——
    不多一个检索、不多一步轨迹、reflect prompt 里不多一个字。"""
    from app.core.ask_retrieval_policy import ask_retrieval_limits
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_nodes(rrepo)
    llm = _RecordingSeqLLM(
        plan={"sub_queries": [{"query": "计划查询"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
    )
    bind_chat_client(rrepo, "reasoning_agent", llm)
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    searched = []

    def fake_search(notebook_id, query, types=None, prefer="balanced"):
        searched.append(query)
        return [_mk_rk(f"{query}-0", query)]

    monkeypatch.setattr(retriever, "search", fake_search)
    # 收尾的配额重排会按 used_queries 再打一遍分,那是既有行为;要证明"补种没有
    # 多发一次检索",必须在补种窗口关闭的那一刻取样 —— 即第一个 reflect 步。
    at_reflect = []

    def on_step(step):
        if step.step_type == "reflect" and not at_reflect:
            at_reflect.extend(searched)

    result = retriever.run(
        nb.id,
        "完整问题",
        on_step=on_step,
        intent_queries=intent_queries,
        limits=ask_retrieval_limits("overview"),
    )

    expected = list(intent_queries or ["计划查询"])
    assert at_reflect == expected                     # 一次多余检索都没有
    assert not _coverage_retrieves(result.trace)
    assert not [s for s in result.trace
                if (s.detail or {}).get("reason") == "intent_coverage_incomplete"]
    assert [row["query"] for row in result.attempted] == expected
    assert llm.reflect_prompts and not _uncovered_block(llm.reflect_prompts[0])


def test_run_effort_caps_reasoning_steps_even_with_larger_explicit_override(
    rrepo, monkeypatch
):
    """调用方显式 max_steps 可继续收紧档位，但不能把档位硬上限抬高。"""
    from app.core.ask_retrieval_policy import ask_retrieval_limits
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_nodes(rrepo)
    reflects = [{
        "next_action": "add_subquery",
        "new_sub_query": {"query": f"补充-{i}"},
    } for i in range(10)]
    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "种子"}]},
        reflects=reflects,
    )
    bind_chat_client(rrepo, "reasoning_agent", llm)
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)

    def fake_search(notebook_id, query, types=None, prefer="balanced"):
        return [_mk_rk(f"{query}-id", query)]

    monkeypatch.setattr(retriever, "search", fake_search)
    result = retriever.run(
        nb.id,
        "问题",
        max_steps=40,
        limits=ask_retrieval_limits("overview"),
    )
    assert len([step for step in result.trace if step.step_type == "reflect"]) == 4


def test_run_expand_community_fans_out_peers(rrepo, monkeypatch):
    """expand_community 动作:对焦点社区兄弟发子查询、记 expand_community trace。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    import app.services.communities as C
    nb = _seed_two_nodes(rrepo)
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "DeepSeek-V4"}]},
        reflects=[
            {"next_action": "expand_community", "community_focal": "DeepSeek-V4",
             "reason": "需要同类"},
            {"next_action": "answer", "sufficient": True}]))
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    monkeypatch.setattr(retriever.communities, "mounted_base_ids", lambda *a: [nb.id])
    monkeypatch.setattr(
        retriever.communities,
        "resolve_comparison_peers",
        lambda *a, **k: (["RTL综合", "布线"], "community"),
    )
    res = retriever.run(nb.id, "DeepSeek-V4 相比其他", "")
    assert any(t.step_type == "expand_community" for t in res.trace)
    attempted_q = [a["query"] for a in res.attempted]
    assert "RTL综合" in attempted_q and "布线" in attempted_q


def test_run_expand_community_fans_out_across_multiple_mounted_bases(rrepo, monkeypatch):
    """多领域基准库:挂了 ≥2 个参考库时,expand_community 逐个查询并去重合并——
    不是「只用列表第一个」就沿用了单库时代的行为。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "DeepSeek-V4"}]},
        reflects=[
            {"next_action": "expand_community", "community_focal": "DeepSeek-V4",
             "reason": "需要同类"},
            {"next_action": "answer", "sufficient": True}]))
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    monkeypatch.setattr(
        retriever.communities, "mounted_base_ids",
        lambda *a: ["base-sim", "base-digital"],
    )
    calls = []

    def _resolve(base_nb, *a, **k):
        calls.append(base_nb)
        if base_nb == "base-sim":
            return (["RTL综合"], "comention")
        return (["RTL综合", "布线"], "community")  # 与另一库重叠一个名字,验证去重

    monkeypatch.setattr(retriever.communities, "resolve_comparison_peers", _resolve)
    res = retriever.run(nb.id, "DeepSeek-V4 相比其他", "")
    assert calls == ["base-sim", "base-digital"]  # 两个挂载库都被查询,顺序一致
    attempted_q = [a["query"] for a in res.attempted]
    assert attempted_q.count("RTL综合") == 1       # 跨库重复的名字只搜一次(去重)
    assert "布线" in attempted_q


def test_run_expand_community_source_sticky_prefers_comention(rrepo, monkeypatch):
    """peer_source 展示口径:一旦某个挂载库以共提(comention,高精度)命中,不应被
    后续遍历到的库的社区(community)回退结果覆盖。改前是「最后一个贡献了非空
    结果的库」口径——若 comention 命中的库先被遍历、community 命中的库后被
    遍历,会把前者的高精度贡献在展示文案上错误降级成「同社区实体」。改后
    sticky-prefer comention:一旦命中过 comention 就不再被后写的 community 覆盖。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "DeepSeek-V4"}]},
        reflects=[
            {"next_action": "expand_community", "community_focal": "DeepSeek-V4",
             "reason": "需要同类"},
            {"next_action": "answer", "sufficient": True}]))
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    # comention 命中的库排在遍历顺序的第一个,community 命中的库在其后——
    # 若代码退化回「后写覆盖」,source 会被第二个库拖回 "community"。
    monkeypatch.setattr(
        retriever.communities, "mounted_base_ids",
        lambda *a: ["base-comention-first", "base-community-second"],
    )

    def _resolve(base_nb, *a, **k):
        if base_nb == "base-comention-first":
            return (["RTL综合"], "comention")
        return (["布线"], "community")

    monkeypatch.setattr(retriever.communities, "resolve_comparison_peers", _resolve)
    res = retriever.run(nb.id, "DeepSeek-V4 相比其他", "")
    step = next(t for t in res.trace if t.step_type == "expand_community")
    assert step.detail["source"] == "comention"
    assert "共提" in step.summary and "同社区实体" not in step.summary


def test_run_expand_community_caps_merged_peers_across_bases(rrepo, monkeypatch):
    """总量帽:多个挂载库合并去重后的兄弟实体数不能无界增长——按
    community_peers_topk × _COMMUNITY_PEERS_CAP_FACTOR 截断(而不是任其随挂载
    库数线性到 topk×N)。截断在「合并各库结果后」发生(三个库仍都被查询,不是
    凑够帽值就提前跳过后面的库),且结果确定——取遍历顺序的前 N 个,不受
    dict/set 顺序影响。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.community_peers_topk = 3          # cap = 3 × factor(2) = 6
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "DeepSeek-V4"}]},
        reflects=[
            {"next_action": "expand_community", "community_focal": "DeepSeek-V4",
             "reason": "需要同类"},
            {"next_action": "answer", "sufficient": True}]))
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    base_ids = ["base-1", "base-2", "base-3"]
    monkeypatch.setattr(retriever.communities, "mounted_base_ids", lambda *a: base_ids)
    calls = []

    def _resolve(base_nb, *a, **k):
        calls.append(base_nb)
        i = base_ids.index(base_nb)
        return ([f"peer-{i}-{j}" for j in range(3)], "community")  # 每库 3 个互不重叠

    monkeypatch.setattr(retriever.communities, "resolve_comparison_peers", _resolve)
    res = retriever.run(nb.id, "DeepSeek-V4 相比其他", "")
    assert calls == base_ids                        # 三个库都被查询(截断发生在合并后,非提前跳过)
    step = next(t for t in res.trace if t.step_type == "expand_community")
    assert len(step.detail["peers"]) == 6            # 9 个去重(无重叠)后截到帽值 6
    assert step.detail["peers"] == [
        "peer-0-0", "peer-0-1", "peer-0-2", "peer-1-0", "peer-1-1", "peer-1-2",
    ]                                                 # 确定性:遍历顺序前 6 个,非随机子集


def test_from_repository_passes_configured_sibling_threshold(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    from app.repositories.ports import ReasoningModelProvider

    rrepo.settings.sibling_min_bridge = 5
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)

    assert retriever.communities.sibling_min_bridge == 5
    assert isinstance(retriever.model_clients, ReasoningModelProvider)


def test_compatibility_factory_constructs_replacement_without_classmethod(
    rrepo, monkeypatch
):
    from app.services import reasoning_retrieval as module

    class Replacement:
        def __init__(self, *, retrieval, model_clients, communities, settings,
                     cancel_event=None, collection_catalog=None,
                     collection_enumeration=None):
            self.retrieval = retrieval
            self.model_clients = model_clients
            self.communities = communities
            self.settings = settings
            self.cancel_event = cancel_event
            self.collection_catalog = collection_catalog
            self.collection_enumeration = collection_enumeration

    monkeypatch.setattr(module, "ReasoningRetriever", Replacement)
    retriever = module.reasoning_retriever_from_repository(
        rrepo, rrepo.settings, "cancel-token"
    )

    assert isinstance(retriever, Replacement)
    assert retriever.retrieval is rrepo.retrieval
    assert retriever.model_clients is rrepo
    assert retriever.communities.sibling_min_bridge == rrepo.settings.sibling_min_bridge
    assert retriever.cancel_event == "cancel-token"
    # 集合地图/清单同样经这个冻结工厂接线,且必须是仓库里**那两个**实例:
    # 各自新建一份会让地图计数与清单枚举读到两份缓存(「地图报 12、清单列 8」)。
    assert retriever.collection_catalog is rrepo.collection_catalog
    assert retriever.collection_enumeration is rrepo.collection_enumeration


def test_run_expand_community_no_base_noop(rrepo, monkeypatch):
    """无 base 库 → 不 fan-out,优雅继续(fail-open)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    import app.services.communities as C
    nb = _seed_two_nodes(rrepo)
    called = {"peers": 0}
    def _peers(*a, **k):
        called["peers"] += 1
        return []
    monkeypatch.setattr(C, "community_peers", _peers)
    monkeypatch.setattr(C, "mounted_base_ids", lambda *a, **k: [])
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "X"}]},
        reflects=[
            {"next_action": "expand_community", "community_focal": "X"},
            {"next_action": "answer", "sufficient": True}]))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "X 相比其他", "")
    assert called["peers"] == 0                     # base 为 None → 根本不调 community_peers
    assert any(t.step_type == "expand_community" for t in res.trace)


def test_merge_element_hits_keeps_max_score_across_queries():
    """codex PR#391 round-2: 同一元素被多次 search_elements 命中时保留最高分——
    合成阶段按分数裁 answer_element_items,只留首个(可能偏低的)查询分会把
    强命中挤出上限;更低分的重复命中也不得降低既有分。"""
    from app.services.reasoning_retrieval import merge_element_hits
    from app.services.retrieval import RetrievedElement

    def el(eid, score):
        return RetrievedElement(
            element_id=eid, source_id="s", source_title="S",
            location_label="p", element_type="paragraph", text=eid, score=score)

    elements = [el("a", 0.3), el("b", 0.5)]
    added = merge_element_hits(
        elements, [el("a", 0.9), el("c", 0.4), el("a", 0.1)])
    assert [e.element_id for e in added] == ["c"]      # 只有 c 是真正新增
    assert len(elements) == 3
    scores = {e.element_id: e.score for e in elements}
    assert scores["a"] == 0.9                          # 高分覆盖首个低分
    assert scores["b"] == 0.5
    merge_element_hits(elements, [el("b", 0.2)])
    assert {e.element_id: e.score for e in elements}["b"] == 0.5


def test_merge_element_hits_collapses_same_source_text_across_queries():
    """Repeated running headers returned under different physical element ids
    are one new fact, while an identical line from another source remains an
    independent provenance item."""
    from app.services.reasoning_retrieval import merge_element_hits
    from app.services.retrieval import RetrievedElement

    def el(eid, source, text, score):
        return RetrievedElement(
            element_id=eid, source_id=source, source_title=source,
            location_label=eid, element_type="paragraph", text=text, score=score)

    elements = [el("page-1", "paper-a", "Paper Title", 0.3)]
    added = merge_element_hits(elements, [
        el("page-2", "paper-a", " paper\n title ", 0.9),
        el("abstract", "paper-a", "We introduce the model.", 0.7),
        el("other-paper", "paper-b", "Paper Title", 0.6),
    ])

    assert [item.element_id for item in added] == ["abstract", "other-paper"]
    assert [item.element_id for item in elements] == [
        "page-1", "abstract", "other-paper",
    ]
    assert elements[0].score == 0.9


def test_reflection_summary_uses_the_same_diverse_element_cap_as_synthesis(rrepo):
    """The agent must not declare sufficiency from passages that the final
    single-synthesis cap will replace with duplicate running headers."""
    from app.services.reasoning_retrieval import ReasoningRetriever
    from app.services.retrieval import RetrievedElement

    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    elements = [RetrievedElement(
        element_id=f"header-{index}", source_id="paper", source_title="Paper",
        location_label=f"p{index}", element_type="paragraph",
        text="Cosmos 3: Omnimodal World Models for Physical AI",
        score=0.99 - index / 100,
    ) for index in range(6)] + [RetrievedElement(
        element_id="abstract", source_id="paper", source_title="Paper",
        location_label="p1", element_type="paragraph",
        text="We introduce Cosmos 3, a family of omnimodal world models.",
        score=0.70,
    )]

    summary = retriever._summarize(
        {}, elements, [], element_limit=2,
    )

    assert summary.count("Cosmos 3: Omnimodal World Models") == 1
    assert "We introduce Cosmos 3" in summary


# ----------------------------------------------------------- 精确查找(exact_lookup)
# 命令类问题的确定性通道:seed pass 无条件按问题里的名称取齐整节(不赌 agent 选动作),
# reflect 动作让模型补查证据里缺的名称。零模型调用、零 embedding。

_MANUAL_SECTIONS = [
    ("ck-main", "Manual > Commands > set_db",
     "[Commands > set_db] set_db 用于设置数据库属性。"),
    ("ck-args", "Manual > Commands > set_db > Arguments",
     "[set_db > Arguments] -name 属性名。-value 属性值。"),
    ("ck-timing", "Manual > Commands > report_timing",
     "[Commands > report_timing] report_timing 输出时序报告。"),
    ("ck-place", "Manual > Commands > place_opt_design",
     "[Commands > place_opt_design] place_opt_design 执行布局优化。"),
    ("ck-get", "Manual > Commands > get_db",
     "[Commands > get_db] get_db 读取数据库属性。"),
]


def _seed_manual_notebook(repo):
    """KG 两节点(让 plan/初检索照常有候选)+ 一份分节手册的 chunk 行。

    直接写 chunk 行而不过分块器:本组用例考的是「给定这样的小节布局,检索会怎么
    做」,把布局写出来,fixture 读起来就是它所代表的那份手册。
    """
    from app.services.sqlite_repository import _now
    nb = _seed_two_nodes(repo)
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,"
            "file_path,file_size,file_hash,summary,doc_type,parse_status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("src-manual", nb.id, "Tool Manual", "document", "m.md",
             "/tmp/m.md", 0, "h-src-manual", "", "", "extracted", now, now))
        for index, (chunk_id, section_path, text) in enumerate(_MANUAL_SECTIONS, 1):
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                "element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                (chunk_id, nb.id, "src-manual", text, section_path,
                 json.dumps([f"el-{index:04d}"]), now))
            db.execute(
                "INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES (?,?,?)",
                (chunk_id, nb.id, text))
    return nb


def _retriever_counting_exact_lookup(repo, calls):
    """构造 retriever 并记录每次精确查找实际收到的检索串(空列表=一次都没调)。

    串里每个名称都带英文双引号:通道会对收到的串**重新**抽名称,而多词短语
    (用户用引号锁定的那种)裸拼进去就再也抽不回来。逐个加引号是让「调用方已
    选定的名称」原样往返的编码(`exact_probe_query`),下面的断言因此看的是
    `"set_db"` 而不是 `set_db`——探测的仍是同一个名称。
    """
    from app.services.reasoning_retrieval import ReasoningRetriever
    rr = ReasoningRetriever.from_repository(repo, repo.settings)
    original = rr.exact_lookup

    def _counted(notebook_id, query):
        calls.append(query)
        return original(notebook_id, query)

    rr.exact_lookup = _counted
    return rr


def test_reflect_prompt_and_schema_expose_exact_lookup():
    from app.services.prompts import reflect_prompt, REFLECT_SCHEMA_HINT
    assert "exact_lookup" in REFLECT_SCHEMA_HINT
    assert "exact_term" in REFLECT_SCHEMA_HINT
    p = reflect_prompt("set_db 命令是怎样的", "- [chunk] Tool Manual · set_db: ...")
    assert "- exact_lookup:" in p
    assert "exact_term" in p
    # 既有 7 个动作的说明不能被挤掉。
    for action in ("answer", "expand_graph", "add_subquery", "search_elements",
                   "ppr_retrieve", "expand_community", "follow_chain"):
        assert f"- {action}:" in p


def test_clean_exact_term_unwraps_without_truncating():
    """item 6:截长挪到 fail_closed 硬闸(:485 一带)之后 + 使用点,解析阶段
    (clean_exact_term)只做标点清洗——先截到 256 会让那条 2000 字符硬闸对
    exact_term 恒不可达。"""
    from app.services.reasoning_retrieval import clean_exact_term
    assert clean_exact_term("  set_db  ") == "set_db"
    assert clean_exact_term('"set_db"') == "set_db"
    assert clean_exact_term("`set_db`") == "set_db"
    assert clean_exact_term("「set_db」。") == "set_db"
    # 名称内部的分隔符是名称的一部分,不能被当包裹标点吃掉。
    assert clean_exact_term("place_opt_design") == "place_opt_design"
    assert clean_exact_term("state-of-the-art") == "state-of-the-art"
    assert clean_exact_term(None) == ""
    long_raw = "a_" * 5000
    assert clean_exact_term(long_raw) == long_raw
    assert len(clean_exact_term(long_raw)) == 10000


def test_reflect_accepts_exact_lookup_and_keeps_invalid_actions_rejected(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever

    class _OneShot:
        configured = True

        def __init__(self, payload):
            self._payload = payload

        def chat_json(self, messages, schema_hint, **kwargs):
            return json.dumps(self._payload)

    bind_chat_client(rrepo, "reasoning_agent",
                     _OneShot({"next_action": "exact_lookup",
                               "exact_term": " 「set_db」 "}))
    decision = ReasoningRetriever.from_repository(rrepo, rrepo.settings).reflect("q", "s")
    assert decision.next_action == "exact_lookup"
    assert decision.exact_term == "set_db"      # 解析时就清洗好

    # fail_closed: 非法动作语义不变(仍抛),exact_lookup 缺名称同样抛。
    bind_chat_client(rrepo, "reasoning_agent",
                     _OneShot({"next_action": "teleport_to_answer"}))
    strict = ReasoningRetriever.from_repository(
        rrepo, rrepo.settings, fail_closed=True)
    with pytest.raises(ValueError):
        strict.reflect("q", "s")
    bind_chat_client(rrepo, "reasoning_agent",
                     _OneShot({"next_action": "exact_lookup", "exact_term": "  "}))
    with pytest.raises(ValueError):
        ReasoningRetriever.from_repository(
            rrepo, rrepo.settings, fail_closed=True).reflect("q", "s")


def test_reflect_fail_closed_rejects_overlong_exact_term(rrepo):
    """item 6:截长挪到硬闸之后才有意义——验证硬闸真的能对超长 exact_term
    生效(此前 clean_exact_term 先截到 256,这条 2000 字符闸恒不可达)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever

    class _OneShot:
        configured = True

        def __init__(self, payload):
            self._payload = payload

        def chat_json(self, messages, schema_hint, **kwargs):
            return json.dumps(self._payload)

    bind_chat_client(rrepo, "reasoning_agent",
                     _OneShot({"next_action": "exact_lookup",
                               "exact_term": "a_" * 5000}))
    strict = ReasoningRetriever.from_repository(
        rrepo, rrepo.settings, fail_closed=True)
    with pytest.raises(ValueError, match="too long"):
        strict.reflect("q", "s")


def test_run_exact_lookup_seed_pass_takes_the_whole_named_section(rrepo):
    """问题点名 set_db → 不等 agent 决定,seed pass 直接把整节取齐。"""
    nb = _seed_manual_notebook(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "set_db"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))
    calls = []
    res = _retriever_counting_exact_lookup(rrepo, calls).run(
        nb.id, "set_db 命令是怎样的", "")

    step = next(t for t in res.trace if t.step_type == "exact_lookup")
    assert step.detail == {"terms": ["set_db"], "found": 2, "phase": "seed",
                           "result_ids": ["ck-main", "ck-args"]}
    assert step.summary == "按名称精确查找:新增 2 段原文"
    # 主描述与参数表都在——分块把它们切开、普通检索只留其一,正是本通道要治的。
    assert [c.chunk_id for c in res.chunks] == ["ck-main", "ck-args"]
    # item 1:打分串是抽出的名称本身(" ".join(seed_terms)),不是整句问题——
    # 与 action 同构。命中节的章节路径+正文都包含名称,关键词覆盖率是 1.0;
    # 整句问题打分会被问题里一堆不相关词拖到约 0.286(评审实测的回归值),把
    # 这个精确命中挤到合成排序垫底、还可能拖过 grounded 判定阈值。
    assert [c.relevance for c in res.chunks] == [1.0, 1.0]
    assert calls == ['"set_db"']
    # seed 步排在初检索之后,天然被「轨迹覆盖整轮」包住。
    kinds = [t.step_type for t in res.trace]
    assert kinds.index("retrieve") < kinds.index("exact_lookup") < kinds.index("reflect")


def test_run_without_an_identifier_makes_zero_exact_lookup_calls_and_no_step(rrepo):
    """中性回归:问题不含名称 → 一次调用都不发,轨迹里也没有多出来的步。"""
    nb = _seed_manual_notebook(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "布局布线"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))
    calls = []
    res = _retriever_counting_exact_lookup(rrepo, calls).run(
        nb.id, "这个流程是怎样的", "")
    assert calls == []
    assert [t.step_type for t in res.trace] == ["plan", "retrieve", "reflect", "answer"]


def test_run_seed_pass_does_not_probe_a_digitless_hyphen_word(rrepo):
    """整支评审阻塞项 3 的复现:`state-of-the-art` 不配一次精确探测。

    词法召回侧仍认它是标识符(多一个 OR 词项无害),但精确通道每个词都要付一次
    真探测:2 万块的库上 16ms/50 命中,而报告引擎每节的问题恒含这批词——等于
    每节白付一次。命中章节标题时还会把整章 12 块以 1.0 分推进证据。
    """
    nb = _seed_manual_notebook(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "对比"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))
    calls = []
    res = _retriever_counting_exact_lookup(rrepo, calls).run(
        nb.id, "这个方法与 state-of-the-art 相比如何", "")
    assert calls == []
    assert not any(t.step_type == "exact_lookup" for t in res.trace)
    # 同一把闸下,真名称照常触发——否则这条断言只是把通道关掉了。
    calls_named = []
    _retriever_counting_exact_lookup(rrepo, calls_named).run(
        nb.id, "set_db 与 state-of-the-art 方案相比如何", "")
    assert calls_named == ['"set_db"']


def test_run_exact_lookup_action_rejects_a_digitless_hyphen_word_with_a_teaching_note(rrepo):
    """模型给普通英文词组时,skip 要带「该给什么」的措辞回喂,而不是只说不行。"""
    nb = _seed_manual_notebook(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    prompts: list[str] = []

    class _CapturingLLM(_SeqLLM):
        def chat_json(self, messages, schema_hint, **kwargs):
            if "sub_queries" not in schema_hint:
                prompts.append(messages[-1]["content"])
            return super().chat_json(messages, schema_hint, **kwargs)

    bind_chat_client(rrepo, "reasoning_agent", _CapturingLLM(
        plan={"sub_queries": [{"query": "布局布线"}]},
        reflects=[{"next_action": "exact_lookup", "exact_term": "real-time"},
                  {"next_action": "answer", "sufficient": True}]))
    calls = []
    res = _retriever_counting_exact_lookup(rrepo, calls).run(
        nb.id, "这个命令怎么用", "")

    assert calls == []
    skip = next(t for t in res.trace
                if t.detail.get("reason") == "exact_term_not_identifier")
    assert skip.detail["term"] == "real-time"
    assert "只用连字符连接的词还需带数字" in skip.summary
    assert "只用连字符连接的词还需带数字" in prompts[-1]


def test_run_exact_lookup_action_pulls_the_section_the_model_named(rrepo):
    nb = _seed_manual_notebook(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "布局布线"}]},
        reflects=[{"next_action": "exact_lookup", "exact_term": "set_db"},
                  {"next_action": "answer", "sufficient": True}]))
    calls = []
    res = _retriever_counting_exact_lookup(rrepo, calls).run(
        nb.id, "这个命令怎么用", "")           # 问题本身无名称 → seed 不触发

    step = next(t for t in res.trace if t.step_type == "exact_lookup")
    assert step.detail == {"term": "set_db", "terms": ["set_db"],
                           "found": 2, "phase": "reflect",
                           "result_ids": ["ck-main", "ck-args"]}
    assert step.summary == "按名称精确查找「set_db」:新增 2 段原文"
    assert [c.chunk_id for c in res.chunks] == ["ck-main", "ck-args"]
    assert calls == ['"set_db"']


def test_run_exact_lookup_action_skips_a_term_the_seed_already_probed(rrepo):
    """seed 与动作共用防重账目:问题里已经查过的名称,agent 不必再花一轮。"""
    nb = _seed_manual_notebook(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "set_db"}]},
        reflects=[{"next_action": "exact_lookup", "exact_term": "set_db 的参数"},
                  {"next_action": "answer", "sufficient": True}]))
    calls = []
    res = _retriever_counting_exact_lookup(rrepo, calls).run(
        nb.id, "set_db 命令是怎样的", "")

    assert len(calls) == 1                      # 只有 seed 那一次真的落到检索层
    lookups = [t for t in res.trace if t.step_type == "exact_lookup"]
    assert [t.detail["phase"] for t in lookups] == ["seed"]
    skip = next(t for t in res.trace
                if t.detail.get("reason") == "duplicate_exact_lookup")
    assert skip.step_type == "skip"
    assert skip.detail["terms"] == ["set_db"]   # 防重按名称,不按请求串


def test_run_exact_lookup_action_repeats_are_fed_back_to_reflect(rrepo):
    """账目回喂:模型看得到查过哪些名称、各自新增多少,重复请求让账目变化。"""
    nb = _seed_manual_notebook(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    prompts: list[str] = []

    class _CapturingLLM(_SeqLLM):
        def chat_json(self, messages, schema_hint, **kwargs):
            if "sub_queries" not in schema_hint:
                prompts.append(messages[-1]["content"])
            return super().chat_json(messages, schema_hint, **kwargs)

    bind_chat_client(rrepo, "reasoning_agent", _CapturingLLM(
        plan={"sub_queries": [{"query": "布局布线"}]},
        reflects=[{"next_action": "exact_lookup", "exact_term": "set_db"},
                  {"next_action": "exact_lookup", "exact_term": "set_db"},
                  {"next_action": "answer", "sufficient": True}]))
    _retriever_counting_exact_lookup(rrepo, []).run(nb.id, "这个命令怎么用", "")

    assert "已按名称精确查找过" not in prompts[0]
    assert "「set_db」(新增2段)" in prompts[1]
    # 重复被跳过时账目仍变化 → prompt 不是不动点,LLM 缓存不会逐字重放同一决策。
    assert "「set_db」(新增2段,已试2次)" in prompts[2]


def test_run_exact_lookup_action_rejects_a_low_selectivity_term(rrepo):
    """名称形状闸与 seed 通道共用:模型不能拿一个短串把精确通道变成全库子串扫描。"""
    nb = _seed_manual_notebook(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "布局布线"}]},
        reflects=[{"next_action": "exact_lookup", "exact_term": "2.1"},
                  {"next_action": "answer", "sufficient": True}]))
    calls = []
    res = _retriever_counting_exact_lookup(rrepo, calls).run(
        nb.id, "这个命令怎么用", "")
    assert calls == []
    skip = next(t for t in res.trace
                if t.detail.get("reason") == "exact_term_not_identifier")
    assert skip.step_type == "skip" and skip.detail["term"] == "2.1"
    assert not any(t.step_type == "exact_lookup" for t in res.trace)


def test_run_exact_lookup_action_caps_at_three_and_seed_does_not_count(rrepo):
    from app.services.reasoning_retrieval import _MAX_EXACT_LOOKUPS
    assert _MAX_EXACT_LOOKUPS == 3
    nb = _seed_manual_notebook(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "set_db"}]},
        reflects=[{"next_action": "exact_lookup", "exact_term": t} for t in (
            "report_timing", "place_opt_design", "get_db", "write_db")]
        + [{"next_action": "answer", "sufficient": True}]))
    calls = []
    res = _retriever_counting_exact_lookup(rrepo, calls).run(
        nb.id, "set_db 命令是怎样的", "")

    # seed(问题里的 set_db)不占动作额度 → 3 个动作全部执行,第 4 个才被上限拦。
    # seed 打分串是抽出的名称本身("set_db"),不是整句问题(item 1)。
    assert calls == ['"set_db"', '"report_timing"', '"place_opt_design"',
                     '"get_db"']
    lookups = [t for t in res.trace if t.step_type == "exact_lookup"]
    assert [t.detail["phase"] for t in lookups] == ["seed", "reflect", "reflect", "reflect"]
    skip = next(t for t in res.trace
                if t.detail.get("reason") == "exact_lookup_cap")
    assert skip.step_type == "skip" and skip.detail["term"] == "write_db"


def test_exact_lookup_disabled_turns_off_both_the_seed_and_the_action(rrepo):
    """总开关关 → seed 与动作都零调用;白名单仍收该动作(它在执行处被 skip)。"""
    nb = _seed_manual_notebook(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    rrepo.settings.exact_lookup_enabled = False
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "set_db"}]},
        reflects=[{"next_action": "exact_lookup", "exact_term": "set_db"},
                  {"next_action": "answer", "sufficient": True}]))
    calls = []
    res = _retriever_counting_exact_lookup(rrepo, calls).run(
        nb.id, "set_db 命令是怎样的", "")
    assert calls == []
    assert not any(t.step_type == "exact_lookup" for t in res.trace)
    skip = next(t for t in res.trace
                if t.detail.get("reason") == "exact_lookup_disabled")
    assert skip.step_type == "skip"
    reflect = next(t for t in res.trace if t.step_type == "reflect")
    assert reflect.detail["next_action"] == "exact_lookup"   # 白名单未剔除


def test_exact_lookup_seed_respects_the_identifier_budget(rrepo, monkeypatch):
    """轨迹里的 terms 必须是真正探测过的那几个,不是问题里出现的全部标识符。"""
    nb = _seed_manual_notebook(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    monkeypatch.setattr(rrepo.settings, "exact_lookup_max_identifiers", 2)
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "set_db"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))
    res = _retriever_counting_exact_lookup(rrepo, []).run(
        nb.id, "set_db、report_timing、get_db 分别是什么", "")
    step = next(t for t in res.trace if t.step_type == "exact_lookup")
    assert step.detail["terms"] == ["set_db", "report_timing"]


def test_exact_lookup_goes_through_the_candidate_policy_boundary(rrepo):
    """新通道不得绕过 `_filter_candidates`。

    knowhow 智能补全就是靠这条边界剔除私有 Memory 与当前表自身投影的;精确查找
    直连 `self.retrieval.exact_lookup_chunks` 会在那条流程里开一个后门。
    """
    nb = _seed_manual_notebook(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "set_db"}]},
        reflects=[{"next_action": "exact_lookup", "exact_term": "report_timing"},
                  {"next_action": "answer", "sufficient": True}]))
    from app.services.reasoning_retrieval import ReasoningRetriever
    rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    seen: list[tuple] = []

    def _policy(kind, items):
        values = list(items)
        seen.append((kind, tuple(getattr(v, "chunk_id", "") for v in values)))
        # 策略钩子把 set_db 那一节整个挡掉,seed 与动作两条路径都必须服从。
        return [v for v in values if not getattr(v, "chunk_id", "").startswith("ck-main")]

    rr.candidate_filter = _policy
    res = rr.run(nb.id, "set_db 命令是怎样的", "")

    assert ("chunk", ("ck-main", "ck-args")) in seen          # seed 经过策略
    assert ("chunk", ("ck-timing",)) in seen                  # 动作也经过策略
    assert [c.chunk_id for c in res.chunks] == ["ck-args", "ck-timing"]
    seed_step = next(t for t in res.trace
                     if t.step_type == "exact_lookup" and t.detail["phase"] == "seed")
    assert seed_step.detail["found"] == 1                     # 记账记的是过滤后的真实新增


def test_allow_exact_lookup_policy_flag_disables_seed_and_action(rrepo):
    """item 2:knowhow 智能补全传进来的 question 是 JSON 信封文本,
    identifier_terms 恒抽出 `table_title`/`known_cells`/`content_md` 这类信封
    键——不加策略位,每次补全请求都会白发探测、烧掉标识符名额、把内部键经
    轨迹上屏。`allow_exact_lookup` 与 `allow_ppr` 同构:False 时 seed 与
    reflect 动作都零 I/O(动作复用既有 exact_lookup_disabled skip 语义);
    True(默认)行为不变——seed 仍按信封键探测,即便在这份手册库里零命中。
    """
    nb = _seed_manual_notebook(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    completion_question = json.dumps({
        "table_title": "参数表", "known_cells": ["a"], "content_md": "x"})

    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "参数"}]},
        reflects=[{"next_action": "exact_lookup", "exact_term": "table_title"},
                  {"next_action": "answer", "sufficient": True}]))
    calls = []
    rr = _retriever_counting_exact_lookup(rrepo, calls)
    rr.allow_exact_lookup = False
    res = rr.run(nb.id, completion_question, "")

    assert calls == []                                        # 零探测
    assert not any(t.step_type == "exact_lookup" for t in res.trace)   # 零轨迹步
    skip = next(t for t in res.trace
                if t.detail.get("reason") == "exact_lookup_disabled")
    assert skip.step_type == "skip"

    # True(默认):seed 照常按信封键探测(这份手册库里没有这几个键对应的
    # 章节,零命中,但通道本身照常发起探测、记轨迹步——策略位关闭前的现状)。
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "参数"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))
    calls2 = []
    res2 = _retriever_counting_exact_lookup(rrepo, calls2).run(
        nb.id, completion_question, "")
    assert calls2 == ['"table_title" "known_cells" "content_md"']
    seed_step = next(t for t in res2.trace if t.step_type == "exact_lookup")
    assert seed_step.detail == {
        "terms": ["table_title", "known_cells", "content_md"],
        "found": 0, "phase": "seed", "result_ids": [],
    }


def test_run_exact_lookup_skip_feeds_teaching_note_to_reflect(rrepo):
    """item 3:四类 skip(未启用/缺名称/非标识符/超上限)不再只留 TraceStep
    沉默,也写进 `exact_lookup_log` 账本并回喂 reflect——否则模型看不到"为
    什么",只能在同一非法输入上反复请求(评审实测:连续 3 轮同 prompt,只能
    靠 stale 熔断兜底)。这里钉住 exact_term_not_identifier 这条:同一非
    标识符 term 连续两轮提交 → 第二轮起 prompt 带教学措辞,且账本按 term
    去重(措辞不随重复请求线性增长,只在末尾追加已尝试次数)。"""
    nb = _seed_manual_notebook(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    prompts: list[str] = []

    class _CapturingLLM(_SeqLLM):
        def chat_json(self, messages, schema_hint, **kwargs):
            if "sub_queries" not in schema_hint:
                prompts.append(messages[-1]["content"])
            return super().chat_json(messages, schema_hint, **kwargs)

    bind_chat_client(rrepo, "reasoning_agent", _CapturingLLM(
        plan={"sub_queries": [{"query": "布局布线"}]},
        reflects=[{"next_action": "exact_lookup", "exact_term": "2.1"},
                  {"next_action": "exact_lookup", "exact_term": "2.1"},
                  {"next_action": "answer", "sufficient": True}]))
    _retriever_counting_exact_lookup(rrepo, []).run(nb.id, "这个命令怎么用", "")

    assert "不是可精确查找的名称" not in prompts[0]
    assert prompts[1].count("「2.1」不是可精确查找的名称") == 1
    # 第二轮仍是同一条账目(tries 递增),不是新追加的第二条——教学措辞在
    # prompt 里只出现一次,不随重复请求线性增长,只在末尾多出已尝试次数。
    assert prompts[2].count("「2.1」不是可精确查找的名称") == 1
    assert "已尝试2次" in prompts[2]


def test_run_ppr_seed_precedes_exact_seed_and_shares_chunk_dedup(rrepo):
    """item 4:模块内注释声称"exact seed 排在 PPR seed 之后保 PPR 去重/计数
    逐位不变",但既有 13 条用例全部 graph_ppr_enabled=False,两个 seed 块整块
    对调后测试照样全绿——没有一条用例在 PPR 开着的时候真正跑过标识符问题。
    这里补上:graph_ppr_enabled=True(默认)+ 问题含标识符,钉住 trace 里
    ppr 先于 exact_lookup,且两条通道对同一 chunk_id 的重叠按 PPR 先到者得
    (PPR 的 seen_chunks 去重发生在先,精确查找侧的新增数随之减去重叠)。
    移动变异(对调两个 seed 块)会让此断言翻转,见任务验证要求。"""
    from app.services.retrieval import RetrievedChunk
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_manual_notebook(rrepo)
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "set_db"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))

    rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    # 与精确查找命中的 ck-main 重叠一段(考验去重顺序)+ 一段只有 PPR 命中。
    ppr_overlap_chunk = RetrievedChunk(
        chunk_id="ck-main", source_id="src-manual", source_title="Tool Manual",
        section_path="Manual > Commands > set_db", text="ppr 命中的重叠段",
        relevance=0.5, score=0.5)
    ppr_only_chunk = RetrievedChunk(
        chunk_id="ppr-only", source_id="src-manual", source_title="Tool Manual",
        section_path="Manual > PPR", text="仅 PPR 命中的段",
        relevance=0.4, score=0.4)
    rr.ppr_retrieve = lambda notebook_id, query: [ppr_overlap_chunk, ppr_only_chunk]

    res = rr.run(nb.id, "set_db 命令是怎样的", "")

    kinds = [t.step_type for t in res.trace]
    ppr_idx = kinds.index("ppr")
    exact_idx = kinds.index("exact_lookup")
    assert ppr_idx < exact_idx                                # PPR seed 排在精确查找 seed 之前

    ppr_step = res.trace[ppr_idx]
    assert ppr_step.detail == {"found": 2, "phase": "seed",   # PPR 先到,两段都算它的新增
                               "result_ids": ["ck-main", "ppr-only"]}

    exact_step = res.trace[exact_idx]
    # 精确查找命中 ck-main + ck-args,但 ck-main 已被 PPR 领走 → 只剩 ck-args 算新增。
    assert exact_step.detail["found"] == 1

    # 两通道去重后的并集,顺序即两个 seed 块各自 extend 的顺序:
    # PPR 的两段(重叠段 + 独占段)在前,精确查找真正新增的一段在后。
    assert [c.chunk_id for c in res.chunks] == ["ck-main", "ppr-only", "ck-args"]


def test_attempted_marks_search_failures_sparsely(rrepo, monkeypatch):
    """检索本身炸掉的种子在账目里带稀疏 `failed` 标;成功/零命中不带。

    attempted 记的是「发起过」——报告的 run 后方向兜底以它为判据跳过重复,
    不打标的话,一次瞬态数据库故障就让该方向的 KG 证据被静默永久丢弃
    (质量评审 P2-3)。同一查询稍后重试成功则清除标记。
    """
    from app.core.ask_retrieval_policy import ask_retrieval_limits
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_nodes(rrepo)
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "planner 不应执行"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
    ))
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)

    def flaky_search(notebook_id, query, types=None, prefer="balanced"):
        if query == "会炸的方向":
            raise RuntimeError("transient database failure")
        if query == "零命中方向":
            return []
        return [_mk_rk(f"{query}-0", f"{query}-0")]

    monkeypatch.setattr(retriever, "search", flaky_search)
    result = retriever.run(
        nb.id,
        "完整问题",
        intent_queries=["完整问题", "会炸的方向", "零命中方向"],
        limits=ask_retrieval_limits("deep"),
    )

    rows = {row["query"]: row for row in result.attempted}
    assert rows["会炸的方向"].get("failed") is True
    # 稀疏键:成功与「检索过、空手而归」都**不带** failed——零命中是合法结果,
    # 打上标会让报告兜底把每个空方向都白跑一遍。
    assert "failed" not in rows["完整问题"]
    assert "failed" not in rows["零命中方向"]

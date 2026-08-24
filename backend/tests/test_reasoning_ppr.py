import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest
from tests.model_testkit import bind_all_embedding_clients
from tests.model_testkit import bind_chat_client


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _seed_two_doc_moe(repo):
    """两个源,各一个 MoE 概念节点,经 concept_clusters(canonical_id=K-moe)桥接;
    每节点 evidence 指向本源的 chunk。复刻 test_ppr_retrieve.py 同名助手。"""
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    with repo._write() as db:
        now = "2026-06-22T00:00:00"
        for sid, title in [("src-A", "DeepSeek paper"), ("src-B", "GLM paper")]:
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (sid, nb.id, title, "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("cA", nb.id, "src-A", "DeepSeek-V3 uses a Mixture-of-Experts (MoE) architecture.",
                    "Arch", json.dumps(["elA"]), now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("cB", nb.id, "src-B", "GLM-4.5 is a Mixture-of-Experts (MoE) model.",
                    "Arch", json.dumps(["elB"]), now))
        for oid, sid, el in [("e1", "src-A", "elA"), ("e2", "src-B", "elB")]:
            ev = json.dumps([{"source_id": sid, "source_title": "", "element_id": el,
                              "element_type": "paragraph", "location_label": "p1",
                              "quoted_span": "MoE", "confidence": 1.0}])
            db.execute("INSERT INTO knowledge_objects "
                       "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "",
                        json.dumps({"name": "Mixture-of-Experts (MoE)"}), ev, sid, now, now))
        for oid in ("e1", "e2"):
            db.execute("INSERT INTO concept_clusters "
                       "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (f"cl-{oid}", nb.id, "K-moe", oid, "Mixture-of-Experts (MoE)", "concept", now))
    return nb


def test_reflect_prompt_and_schema_expose_ppr():
    from app.services.prompts import reflect_prompt, REFLECT_SCHEMA_HINT
    assert "ppr_retrieve" in REFLECT_SCHEMA_HINT
    assert "ppr_query" in REFLECT_SCHEMA_HINT
    p = reflect_prompt("对比 DeepSeek 与 GLM", "- [concept] MoE (id=k1)")
    assert "ppr_retrieve" in p
    # 既有 4 动作不丢
    for a in ("answer", "expand_graph", "add_subquery", "search_elements"):
        assert a in REFLECT_SCHEMA_HINT


def test_reflect_parses_ppr_retrieve_decision():
    from app.services.reasoning_retrieval import ReasoningRetriever
    from app.core.config import Settings

    class _LLM:
        configured = True
        def chat_json(self, messages, schema_hint, **kw):
            return json.dumps({"next_action": "ppr_retrieve",
                               "ppr_query": "DeepSeek vs GLM MoE", "reason": "需跨文档对比"})

    class _Retrieval:
        pass

    class _Models:
        def chat(self, workload_id):
            assert workload_id == "reasoning_agent"
            return _LLM()

    class _Communities:
        pass

    rr = ReasoningRetriever(
        retrieval=_Retrieval(), model_clients=_Models(), communities=_Communities(),
        settings=Settings(_env_file=None),
    )
    d = rr.reflect("对比题", "候选摘要")
    assert d.next_action == "ppr_retrieve"
    assert d.ppr_query == "DeepSeek vs GLM MoE"


class _AnswerOnlyLLM:
    """plan 出单子查询;reflect 永远 answer(不选 ppr_retrieve)→ 只靠 seed pass。"""
    configured = True
    def chat_json(self, messages, schema_hint, **kw):
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "DeepSeek MoE"}]})
        if "next_action" in schema_hint:
            return json.dumps({"next_action": "answer", "sufficient": True})
        return json.dumps({"answer": "都用 MoE [k1].", "grounded": True})


def test_run_seed_pass_populates_cross_doc_chunks_when_flag_on(repo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_doc_moe(repo)
    bind_chat_client(repo, "reasoning_agent", _AnswerOnlyLLM())
    assert repo.settings.graph_ppr_enabled is True   # 默认开
    result = ReasoningRetriever.from_repository(repo, repo.settings).run(nb.id, "DeepSeek-V3 MoE 对比")
    ids = {c.chunk_id for c in result.chunks}
    assert "cA" in ids and "cB" in ids               # seed pass 拉到跨文档 chunk
    assert any(s.step_type == "ppr" for s in result.trace)


def test_run_no_seed_when_flag_off(repo, monkeypatch):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_doc_moe(repo)
    bind_chat_client(repo, "reasoning_agent", _AnswerOnlyLLM())
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)
    result = ReasoningRetriever.from_repository(repo, repo.settings).run(nb.id, "DeepSeek-V3 MoE 对比")
    assert result.chunks == []
    assert not any(s.step_type == "ppr" for s in result.trace)


class _ScriptedReflectLLM:
    """plan 单子查询;reflect 按 reflects 列表逐轮弹出;其余=answer。"""
    configured = True
    def __init__(self, reflects):
        self._reflects = list(reflects)
    def chat_json(self, messages, schema_hint, **kw):
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "DeepSeek MoE"}]})
        if "next_action" in schema_hint:
            return json.dumps(self._reflects.pop(0) if self._reflects
                              else {"next_action": "answer", "sufficient": True})
        return json.dumps({"answer": "ok [k1].", "grounded": True})


def test_ppr_retrieve_action_caps_at_max(repo, monkeypatch):
    """连发 4 次 ppr_retrieve 决策 → 仅前 3 次执行(phase=action),第 4 次写 skip
    (ppr_retrieve_cap)。flag 保持默认开(seed 跑,但 seed 是 phase=seed,按 phase 过滤
    不污染动作计数)。抬高 stale_limit 防『动作 0 新增→stale 早熔断』掩盖 cap。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_doc_moe(repo)
    monkeypatch.setattr(repo.settings, "reasoning_stale_limit", 99)
    bind_chat_client(repo, "reasoning_agent", _ScriptedReflectLLM(
        reflects=[{"next_action": "ppr_retrieve", "ppr_query": f"q{i}"} for i in range(4)]
        + [{"next_action": "answer", "sufficient": True}]))
    result = ReasoningRetriever.from_repository(repo, repo.settings).run(nb.id, "对比题")
    ppr_actions = [s for s in result.trace
                   if s.step_type == "ppr" and s.detail.get("phase") == "action"]
    caps = [s for s in result.trace
            if s.step_type == "skip" and s.detail.get("reason") == "ppr_retrieve_cap"]
    assert len(ppr_actions) == 3       # 上限 _MAX_PPR_RETRIEVES=3
    assert len(caps) >= 1              # 第 4 次被 cap


def test_ppr_retrieve_action_uses_deployment_policy(repo, monkeypatch):
    """The centralized setting must govern runtime actions, not only exist."""
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_doc_moe(repo)
    monkeypatch.setattr(repo.settings, "reasoning_stale_limit", 99)
    monkeypatch.setattr(repo.settings, "reasoning_max_ppr_retrieves", 1)
    bind_chat_client(repo, "reasoning_agent", _ScriptedReflectLLM(
        reflects=[
            {"next_action": "ppr_retrieve", "ppr_query": "q1"},
            {"next_action": "ppr_retrieve", "ppr_query": "q2"},
            {"next_action": "answer", "sufficient": True},
        ]
    ))

    result = ReasoningRetriever.from_repository(repo, repo.settings).run(
        nb.id, "对比题"
    )

    ppr_actions = [
        step for step in result.trace
        if step.step_type == "ppr" and step.detail.get("phase") == "action"
    ]
    caps = [
        step for step in result.trace
        if step.step_type == "skip"
        and step.detail.get("reason") == "ppr_retrieve_cap"
    ]
    assert len(ppr_actions) == 1
    assert caps


def test_ppr_retrieve_action_skipped_when_flag_off(repo, monkeypatch):
    """flag 关 → 即便 agent 显式选 ppr_retrieve 也被 skip(ppr_disabled),不跑 PPR。
    honors GRAPH_PPR_ENABLED 作为 reasoning 的 PPR 总开关(off=零 PageRank)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_doc_moe(repo)
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)
    bind_chat_client(repo, "reasoning_agent", _ScriptedReflectLLM(
        reflects=[{"next_action": "ppr_retrieve", "ppr_query": "q"},
                  {"next_action": "answer", "sufficient": True}]))
    result = ReasoningRetriever.from_repository(repo, repo.settings).run(nb.id, "对比题")
    assert any(s.step_type == "skip" and s.detail.get("reason") == "ppr_disabled"
               for s in result.trace)
    assert result.chunks == []


def test_answer_context_id_offset_shifts_keys(repo):
    """加 id_offset 后 KG 键从 k1001 起(与 chunk 段 k1..N 不撞)。"""
    nb = _seed_two_doc_moe(repo)
    hits = repo._retrieve_scored(nb.id, "Mixture-of-Experts")[:2]
    assert hits
    block, id_map = repo._answer_context(nb.id, hits, id_offset=repo._MIX_KG_KEY_BASE)
    assert all(int(k[1:]) > repo._MIX_KG_KEY_BASE for k in id_map)   # k1001+
    # 默认 offset=0 保持旧行为
    _b0, id0 = repo._answer_context(nb.id, hits)
    assert "k1" in id0


def test_answer_reasoning_mixes_chunks_as_citable(repo):
    """chunks 非空 → 上下文含 chunk 段、id_map 含 chunk 锚(object_type=chunk)、
    答案 [k1] 解析为 chunk 锚。"""
    nb = _seed_two_doc_moe(repo)
    hits = repo._retrieve_scored(nb.id, "Mixture-of-Experts")[:2]
    chunks = repo._ppr_retrieve(nb.id, "DeepSeek-V3 Mixture-of-Experts")
    assert chunks

    class _Echo:
        configured = True
        def chat_json(self, messages, schema_hint, **kw):
            return json.dumps({"answer": "跨文档证据 [k1].", "grounded": True})
    bind_chat_client(repo, "ask_answer", _Echo())

    answer, grounded, anchors, _counts = repo._answer_reasoning(
        nb.id, "对比 MoE", hits, [], chunks=chunks)
    assert anchors and anchors[0].object_type == "chunk"   # k1 = chunk 段一等引用


def test_answer_reasoning_empty_chunks_unchanged(repo):
    """chunks 为空 → 走旧 KG-only 路径,k1 是 KG 锚。"""
    nb = _seed_two_doc_moe(repo)
    hits = repo._retrieve_scored(nb.id, "Mixture-of-Experts")[:2]

    class _Echo:
        configured = True
        def chat_json(self, messages, schema_hint, **kw):
            return json.dumps({"answer": "KG 证据 [k1].", "grounded": True})
    bind_chat_client(repo, "ask_answer", _Echo())

    answer, grounded, anchors, _counts = repo._answer_reasoning(nb.id, "MoE", hits, [], chunks=None)
    assert anchors and anchors[0].object_type == "concept"  # k1 = KG 锚(旧行为)


def test_answer_reasoning_makes_direct_source_element_citable(repo):
    from app.services.retrieval import RetrievedElement

    nb = _seed_two_doc_moe(repo)
    element = RetrievedElement(
        element_id="el-direct", source_id="src-A", source_title="DeepSeek paper",
        location_label="Arch / p. 2", element_type="paragraph",
        text="The routed experts are selected per token.", score=0.92,
    )

    class _Echo:
        configured = True
        prompt = ""

        def chat_json(self, messages, schema_hint, **kw):
            self.prompt = messages[-1]["content"]
            return json.dumps({"answer": "逐 token 路由专家。[k4001]", "grounded": True})

    llm = _Echo()
    bind_chat_client(repo, "ask_answer", llm)
    repo.settings.kg_query_refine_enabled = False
    answer, grounded, anchors, _counts = repo._answer_reasoning(
        nb.id, "如何路由", [], [element], chunks=None,
    )
    assert "[Direct source elements]" in llm.prompt
    assert "k4001:" in llm.prompt and element.text in llm.prompt
    assert grounded is True
    assert [(item.object_type, item.object_id) for item in anchors] == [
        ("element", "el-direct")
    ]


def test_answer_reasoning_final_evidence_respects_combined_hard_budget(
    repo, monkeypatch
):
    from app.services.retrieval import RetrievedElement

    nb = _seed_two_doc_moe(repo)
    hits = repo._retrieve_scored(nb.id, "Mixture-of-Experts")[:2]
    chunks = repo._ppr_retrieve(nb.id, "DeepSeek-V3 Mixture-of-Experts")
    element = RetrievedElement(
        element_id="el-budget", source_id="src-A", source_title="DeepSeek paper",
        location_label="Arch", element_type="paragraph", text="E" * 500, score=0.9,
    )

    class _Capture:
        configured = True
        prompt = ""

        def chat_json(self, messages, schema_hint, **kwargs):
            self.prompt = messages[-1]["content"]
            return json.dumps({"answer": "bounded", "grounded": False})

    llm = _Capture()
    bind_chat_client(repo, "ask_answer", llm)
    repo.settings.kg_query_refine_enabled = False
    monkeypatch.setattr(
        "app.services.ask_service.answer_prompt",
        # **_section 吞掉按节合成新增的 section_title/index/total(单次合成下它们
        # 恒为缺省值);本用例量的是证据块字节数,与 prompt 外壳无关。
        lambda _question, context, _history, **_section: context,
    )

    repo._runtime.ask_service()._answer_reasoning(
        nb.id, "budget", hits, [element], chunks=chunks,
        structured_block="S" * 100,
        chunk_context_chars=120,
        kg_context_chars=100,
    )

    assert len(llm.prompt) <= 220


def test_answer_reasoning_orders_equal_relevance_chunks_by_insertion(repo, monkeypatch):
    """同分不得按 chunk_id 打破平手——那是随机 128 位代理键。

    精确通道整节取齐后,那一节的每一块都是 1.0 同分,而 `_chunk_answer_context`
    的字符预算恰好切在这个同分组里:按 id 排等于在组内洗牌,「参数表进不进
    prompt」变成掷骰子。稳定排序保留插入序(=检索序 / 节内文档序)。
    """
    from app.services.retrieval import RetrievalSupport, RetrievedChunk

    nb = repo.create_notebook(NotebookCreate(name="kb"))

    def _chunk(chunk_id, relevance):
        return RetrievedChunk(
            chunk_id=chunk_id, source_id="src-A", source_title="Tool Manual",
            section_path="Manual > Commands > set_db", text=f"{chunk_id} body",
            element_ids=[], score=relevance, relevance=relevance,
            retrieval_supports=(
                RetrievalSupport("lexical", "chunk", chunk_id, relevance),),
        )

    # 检索序 = 节内文档序(主描述 → 参数表 → 示例);id 序刻意与之相反,
    # 于是「按 id tie-break」与「保留插入序」必然给出不同答案,不会巧合排对。
    chunks = [_chunk("zzz-main", 1.0), _chunk("mmm-args", 1.0),
              _chunk("aaa-examples", 1.0), _chunk("kkk-unrelated", 0.4)]

    ask = repo._runtime.ask_service()
    captured: dict = {}
    real = ask._chunk_answer_context

    def _spy(ordered, **kwargs):
        captured["ids"] = [chunk.chunk_id for chunk in ordered]
        return real(ordered, **kwargs)

    monkeypatch.setattr(ask, "_chunk_answer_context", _spy)

    class _Stub:
        configured = True

        def chat_json(self, messages, schema_hint, **kwargs):
            return json.dumps({"answer": "ok", "grounded": True})

    bind_chat_client(repo, "ask_answer", _Stub())
    repo.settings.kg_query_refine_enabled = False
    ask._answer_reasoning(nb.id, "set_db 命令是怎样的", [], [], chunks=chunks)

    assert captured["ids"] == [
        "zzz-main", "mmm-args", "aaa-examples", "kkk-unrelated"]


def test_answer_reasoning_admits_top_elements_by_relevance_desc(repo, monkeypatch):
    """PR-1 止血(元素装配保真):不再是插入序 elements[:6] 的裸切——装配前按
    RetrievedElement.score 降序排序(tie-break element_id),只取前 element_items 条。
    构造乱序且超过 cap 的元素列表,monkeypatch element_context 捕获实际入参,断言
    恰好是排序后的 top-N,而不是原始插入序的前 N 个。"""
    from app.services.retrieval import RetrievedElement

    nb = repo.create_notebook(NotebookCreate(name="kb"))
    # 刻意乱序 + 与 element_id 字母序不一致,防止「巧合排对」的假绿。
    # el-a/el-b 同分 0.9,靠 element_id tie-break 排定次序;el-b 在插入序中排在
    # el-a 之前——若实现退化成"只按 score 的稳定排序"(即去掉 element_id
    # tie-break),稳定排序会保留插入序,tied 组会输出 el-b 先于 el-a,与本用例
    # 断言的 el-a 先于 el-b 冲突,从而真正检出「tie-break 被删」这个变异
    # (此前 el-a 恰好也排在插入序更前,tie-break 有没有都巧合得到同一结果,
    # 是假绿;这里刻意让两者分歧)。
    elements = [
        RetrievedElement(element_id="el-c", source_id="s", source_title="S",
                         location_label="p1", element_type="paragraph", text="C", score=0.5),
        RetrievedElement(element_id="el-b", source_id="s", source_title="S",
                         location_label="p1", element_type="paragraph", text="B", score=0.9),
        RetrievedElement(element_id="el-f", source_id="s", source_title="S",
                         location_label="p1", element_type="paragraph", text="F", score=0.1),
        RetrievedElement(element_id="el-a", source_id="s", source_title="S",
                         location_label="p1", element_type="paragraph", text="A", score=0.9),
        RetrievedElement(element_id="el-d", source_id="s", source_title="S",
                         location_label="p1", element_type="paragraph", text="D", score=0.3),
        RetrievedElement(element_id="el-e", source_id="s", source_title="S",
                         location_label="p1", element_type="paragraph", text="E", score=0.7),
    ]

    ask = repo._runtime.ask_service()
    real_element_context = ask.evidence_context.element_context
    captured: dict = {}

    def _capture(elements_arg, **kwargs):
        captured["elements"] = list(elements_arg)
        return real_element_context(elements_arg, **kwargs)

    monkeypatch.setattr(ask.evidence_context, "element_context", _capture)

    class _Echo:
        configured = True
        def chat_json(self, messages, schema_hint, **kw):
            return json.dumps({"answer": "ok", "grounded": False})
    bind_chat_client(repo, "ask_answer", _Echo())
    repo.settings.kg_query_refine_enabled = False

    ask._answer_reasoning(nb.id, "q", [], elements, "", chunks=None, element_items=3)

    got_ids = [e.element_id for e in captured["elements"]]
    assert got_ids == ["el-a", "el-b", "el-e"]   # top-3 by score desc, tie-break element_id


def test_answer_reasoning_backfills_body_after_duplicate_running_headers(repo, monkeypatch):
    """Cosmos-3 regression: six physical copies of one paper title must occupy
    one synthesis slot, leaving the bounded second slot for the abstract."""
    from app.services.retrieval import RetrievedElement

    nb = repo.create_notebook(NotebookCreate(name="kb"))
    elements = [
        RetrievedElement(
            element_id=f"header-{index}", source_id="paper", source_title="Paper",
            location_label=f"p{index}", element_type="paragraph",
            text="Cosmos 3: Omnimodal World Models for Physical AI",
            score=0.99 - index / 100,
        )
        for index in range(6)
    ] + [RetrievedElement(
        element_id="abstract", source_id="paper", source_title="Paper",
        location_label="p1", element_type="paragraph",
        text="We introduce Cosmos 3, a family of omnimodal world models.",
        score=0.70,
    )]

    ask = repo._runtime.ask_service()
    captured = {}
    real_element_context = ask.evidence_context.element_context

    def _capture(elements_arg, **kwargs):
        captured["ids"] = [element.element_id for element in elements_arg]
        return real_element_context(elements_arg, **kwargs)

    monkeypatch.setattr(ask.evidence_context, "element_context", _capture)

    class _Echo:
        configured = True

        def chat_json(self, messages, schema_hint, **kwargs):
            return json.dumps({"answer": "ok", "grounded": False})

    bind_chat_client(repo, "ask_answer", _Echo())
    repo.settings.kg_query_refine_enabled = False

    ask._answer_reasoning(nb.id, "贡献", [], elements, "", element_items=2)

    assert captured["ids"] == ["header-0", "abstract"]


def test_answer_reasoning_reports_actual_included_counts(repo):
    """PR-1 code-quality-review 修复: _answer_reasoning 第 4 个返回值必须精确
    反映"真正进入 prompt"的计数,且在有非空 memory_hits/chains/超 cap
    elements 同时存在时,included_kg 只统计 KG 对象,不被 memory/chains 污染;
    included_elements 必须等于 element_items(严格小于候选池)。

    两段场景合成一个用例(而非机械按行号切):
    - 场景 1 验证 KG 去重(< 候选池)与 elements 超 cap 截断(< 候选池)在
      memory_hits/chains 都非空时仍然精确;chunk 预算充裕,chunks 全部进入
      (精确等于候选池,不是"恰好又和候选池数字相同"的巧合断言，而是本场景
      故意不挤压 chunk 预算的直接推论)。
    - 场景 2 单独针对 included_chunks 的"map 大小 → 幸存数"口径 bug 做回归:
      6 个 1 字符 chunk + chunk_context_chars=30——内层 chunk_context 记账
      会把 5 条收进 chunk_id_map,append 阶段再加 19 字符标题重截断后只有
      2 行 "kN:" 幸存;pre-append 口径报 5,幸存口径必须报 2,两种口径在此
      数据下强可区分(改回 len(chunk_id_map) 必红)。
      **没有与场景 1 合并**是刻意的:"至少一个 chunk 被挤出"与"chunk 分区
      仍能为 elements 真正渲染一行"在当前实现下互斥。机制有两支:①字符级
      重截断分支下,已接受内容距满预算至多一个 "kN: " 前缀宽度,加 19 字符
      标题后剩余归零;②整块丢弃分支(remaining <= prefix)下 source_context
      不变长,剩余最多约 21 字符——此时 elements 守卫虽然放行,但
      element_context 单行最短前缀(k4001: [source-element][tier] …)约 40
      字符,一行都渲染不出。已用穷举脚本(chunk 数 2..8、长度 1..80 多档、
      预算 20..400、含非空前置 source_context)验证 34146 种组合零反例。
      ⚠边界:该互斥的安全裕度只有约 19 字符(21 vs 40),依赖元素行前缀不被
      精简;谁把元素行前缀缩到 22 字符以下,这个"不可能"就失效,届时应把
      场景 2 升级为联合断言。
    """
    from app.services.retrieval import RetrievedChunk, RetrievedElement
    from app.models.memory import MemoryHit
    from app.services.kg.follow_chain import ChainHop, InferredChain

    class _Echo:
        configured = True
        def chat_json(self, messages, schema_hint, **kw):
            return json.dumps({"answer": "ok [k1].", "grounded": True})

    # --- 场景 1: KG 去重 + elements 超 cap,memory_hits/chains 非空 ---
    nb = _seed_two_doc_moe(repo)
    hits = repo._retrieve_scored(nb.id, "Mixture-of-Experts")[:2]
    chunks = repo._ppr_retrieve(nb.id, "DeepSeek-V3 Mixture-of-Experts")
    assert hits and chunks
    assert len(hits) == 2   # 候选池;下方断言 included_kg(1) < 2

    # 5 个候选 element,element_items=2 → 必须严格小于候选池 5。
    elements = [
        RetrievedElement(
            element_id=f"el-{i}", source_id="src-A", source_title="DeepSeek paper",
            location_label="Arch", element_type="paragraph",
            text=f"Routed experts variant {i}.", score=0.9 - i * 0.1,
        )
        for i in range(5)
    ]

    memory_hits = [MemoryHit(
        memory_id="mem-1", title="Confirmed MoE note",
        text="Routing keeps expert load balanced.",
        status="confirmed", authority=3, score=0.8, provenance={},
    )]

    hop1 = ChainHop(
        relation_id="rel-1", notebook_id=nb.id, tier="personal",
        source_object_id="A", target_object_id="B", edge_type="derived_from",
        source_name="A", target_name="B",
        evidence=[{"quote": "A derives B", "location_label": "p1"}], trust=0.9,
    )
    hop2 = ChainHop(
        relation_id="rel-2", notebook_id=nb.id, tier="personal",
        source_object_id="B", target_object_id="C", edge_type="derived_from",
        source_name="B", target_name="C",
        evidence=[{"quote": "B derives C", "location_label": "p2"}], trust=0.9,
    )
    chains = [InferredChain(
        source_object_id="A", via_object_id="B", target_object_id="C",
        source_name="A", via_name="B", target_name="C",
        inferred_edge_type="derived_from", hops=(hop1, hop2),
        chain_trust=0.9, notebook_id=nb.id, tier="personal", query_relevance=0.8,
    )]

    bind_chat_client(repo, "ask_answer", _Echo())
    repo.settings.kg_query_refine_enabled = False

    ask = repo._runtime.ask_service()
    answer, grounded, anchors, counts = ask._answer_reasoning(
        nb.id, "对比 MoE", hits, elements, "",
        chunks=chunks, chains=chains, memory_hits=memory_hits, element_items=2,
    )
    assert counts["included_kg"] == 1
    assert counts["included_kg"] < len(hits)                 # 1 < 2
    assert counts["included_elements"] == 2
    assert counts["included_elements"] < len(elements)       # 2 < 5
    # chunk 预算充裕(未挤压),chunks 应全部进入——精确值,不是巧合。
    assert counts["included_chunks"] == len(chunks)

    # --- 场景 2: 窄 chunk 预算下,included_chunks 取 append 后幸存数而非
    # pre-append 的 chunk_id_map 大小(内层记账收 5 条,标题重截断后仅 2 行
    # "kN:" 幸存;len(chunk_id_map) 口径会报 5) ---
    narrow_chunks = [
        RetrievedChunk(chunk_id=f"c{i}", source_id="s", source_title="S",
                       section_path=f"p{i}", text=chr(ord("A") + i),
                       relevance=0.9 - i * 0.1)
        for i in range(6)
    ]
    _, _, _, narrow_counts = ask._answer_reasoning(
        nb.id, "q2", [], [], "", chunks=narrow_chunks, chunk_context_chars=30,
    )
    assert narrow_counts["included_chunks"] == 2
    assert narrow_counts["included_chunks"] < len(narrow_chunks)   # 2 < 6
    assert narrow_counts["included_elements"] == 0


def test_ask_reasoning_wires_effort_element_items_into_answer_reasoning(repo, monkeypatch):
    """PR-1 code-quality-review 修复(接线守卫): ask_reasoning 必须把按
    retrieval_effort 解析出的 limits.answer_element_items 传给
    _answer_reasoning 的 element_items 形参——不能让默认值 6 悄悄生效。
    用 overview 档(answer_element_items=4,见 ask_retrieval_policy.py)驱动
    一次真实 ask_reasoning,monkeypatch _answer_reasoning 捕获调用 kwargs。"""
    nb = _seed_two_doc_moe(repo)
    ask = repo._runtime.ask_service()
    real_answer_reasoning = ask._answer_reasoning
    captured: dict = {}

    def _capture(*args, **kwargs):
        captured["element_items"] = kwargs.get("element_items")
        return real_answer_reasoning(*args, **kwargs)

    monkeypatch.setattr(ask, "_answer_reasoning", _capture)
    llm = _AnswerOnlyLLM()
    bind_chat_client(repo, "ask_answer", llm)
    bind_chat_client(repo, "reasoning_agent", llm)

    resp = repo.ask(nb.id, AskRequest(
        question="DeepSeek-V3 MoE 相比其他模型", mode="reasoning",
        retrieval_effort="overview",
    ))
    assert resp.mode == "reasoning"
    assert "element_items" in captured, "_answer_reasoning must have been called"
    assert captured["element_items"] == 4


def test_reasoning_ask_seed_grounds_in_cross_doc_chunk_end_to_end(repo):
    """端到端:flag 开 + reflect 只 answer(纯靠 seed pass)→ 跨文档 chunk 被升为可引用证据
    ([k1] 落在 chunk 段 → resp.anchors 含 chunk 锚),且 seed pass 在轨迹里。"""
    nb = _seed_two_doc_moe(repo)
    llm = _AnswerOnlyLLM()
    bind_chat_client(repo, "ask_answer", llm)
    bind_chat_client(repo, "reasoning_agent", llm)
    resp = repo.ask(nb.id, AskRequest(question="DeepSeek-V3 MoE 相比其他模型", mode="reasoning"))
    assert resp.mode == "reasoning"
    assert any(s.step_type == "ppr" for s in (resp.reasoning_trace or []))   # seed pass 跑了
    assert any(a.object_type == "chunk" for a in resp.anchors)               # 跨文档 chunk 成了可引用证据


def test_reasoning_ask_flag_off_no_ppr(repo, monkeypatch):
    """flag 关 → 无 ppr 轨迹,回到今天行为(无跨文档 chunk 注入)。"""
    nb = _seed_two_doc_moe(repo)
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)
    llm = _AnswerOnlyLLM()
    bind_chat_client(repo, "ask_answer", llm)
    bind_chat_client(repo, "reasoning_agent", llm)
    resp = repo.ask(nb.id, AskRequest(question="MoE", mode="reasoning"))
    assert not any(s.step_type == "ppr" for s in (resp.reasoning_trace or []))


def test_chunk_relevance_within_unit_interval(repo):
    """守 [0,1]:reasoning 累积的 chunk relevance 全在单位区间。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_doc_moe(repo)
    bind_chat_client(repo, "reasoning_agent", _AnswerOnlyLLM())
    result = ReasoningRetriever.from_repository(repo, repo.settings).run(nb.id, "MoE 对比")
    assert all(0.0 <= c.relevance <= 1.0 for c in result.chunks)


def test_answer_reasoning_fills_counts_sink_before_model_call(repo):
    """codex PR#391 round-2: 合成模型抛错/吐畸形 JSON 时,counts_sink 必须已带
    真实装配计数——sink 在 chat_json 之前填充,失败轨迹不得报全零。"""
    from app.services.retrieval import RetrievedElement

    class _Boom:
        configured = True
        def chat_json(self, messages, schema_hint, **kw):
            raise RuntimeError("synthesis exploded")

    nb = _seed_two_doc_moe(repo)
    hits = repo._retrieve_scored(nb.id, "Mixture-of-Experts")[:1]
    assert hits
    elements = [RetrievedElement(
        element_id="el-x", source_id="src-A", source_title="DeepSeek paper",
        location_label="Arch", element_type="paragraph",
        text="Routed experts.", score=0.9)]
    bind_chat_client(repo, "ask_answer", _Boom())
    repo.settings.kg_query_refine_enabled = False
    ask = repo._runtime.ask_service()
    sink: dict = {}
    with pytest.raises(RuntimeError):
        ask._answer_reasoning(nb.id, "MoE", hits, elements, "",
                              element_items=2, counts_sink=sink)
    assert sink == {
        "included_kg": 1,
        "included_chunks": 0,
        "included_elements": 1,
        "included_collections": 0,
    }

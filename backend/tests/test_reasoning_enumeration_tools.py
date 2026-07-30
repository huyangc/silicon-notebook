"""逐步推理接入类型化集合枚举工具(PR-2 T4)。

覆盖 reflect 动作空间的接入面:动作分发与 trace、同集合续跑、run 级预算池、
已列全/冲突/非法值的三种跳过、集合地图注入(规划 + 每轮反思)、总开关关闭态、
取消传播,以及「清单条目算进展」的账目。执行器与地图层本身的合同在
``test_collection_enumeration`` / ``test_collection_catalog``,这里只测接入。
"""
from __future__ import annotations

import json
import threading

import pytest

from dataclasses import replace

from app.core.ask_retrieval_policy import ask_retrieval_limits
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services import collection_enumeration as enum_module
from app.services.cancellation import AskCancelled
from app.services.collection_enumeration import (
    TRUNCATED_BUDGET,
    TRUNCATED_CONCURRENT_CHANGE,
    TRUNCATED_PAYLOAD,
    ElementEnumeration,
    EnumerationCoverage,
)
from app.services.embedding import FakeEmbedder
from app.services.reasoning_retrieval import ReasoningRetriever
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client


NOW = "2026-07-28T00:00:00+08:00"

# 接入枚举工具**之前**的 reflect schema,逐字冻结在这里。关闭态必须回到它。
# 抄自 PR-2 T4 之前的 prompts.REFLECT_SCHEMA_HINT 字面量;它现在由
# reflect_schema_hint() 生成,所以对照物必须是这份独立副本。
# 基线随 master 前进:精确查找通道(exact_lookup 动作 + exact_term 字段)在本分支
# rebase 时已经是「接入前」的现状,所以它属于冻结串的基础部分——关闭枚举工具
# 必须逐字回到含 exact_lookup 的这一份,而不是它出现之前的那一份。
FROZEN_REFLECT_SCHEMA_HINT = (
    '{"sufficient":false,"next_action":"answer|expand_graph|add_subquery|'
    'search_elements|ppr_retrieve|expand_community|follow_chain|exact_lookup",'
    '"expand":{"object_id":"","edge_type":null,'
    '"direction":"out|in|both"},"new_sub_query":{"query":"","types":[],'
    '"prefer":"balanced","reason":""},"follow_chain":{"start_object_id":"",'
    '"target_object_id":"","edge_type":null,"direction":"out|in|both"},'
    '"community_focal":"","elements_query":"","ppr_query":"","exact_term":"",'
    '"reason":""}'
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    # 与 test_reasoning_retrieval 的 rrepo 同款隔离:本机 .env 里的真实推理端点
    # 会让这些 run 打真实网络。
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    # PPR 与本任务无关,关掉省一整段跨文档检索。
    instance.settings.graph_ppr_enabled = False
    return instance


def _seed(repo, *, formulas=3, tables=0):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(notebook.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "版图设计要点", "section_path": "1"}, "evidence": []},
    ], [])
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("s1", notebook.id, "论文一", "pdf", "extracted", "extracted", NOW, NOW),
        )
        index = 0
        for element_type, count in (("formula", formulas), ("table", tables)):
            for _ in range(count):
                index += 1
                db.execute(
                    "INSERT INTO source_elements (id,source_id,element_type,"
                    "location_label,text,metadata,created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (f"el-{index:03d}", "s1", element_type, f"p{index}",
                     f"公式 {index}", "{}", NOW),
                )
    repo.collection_catalog.invalidate()
    return notebook


class _SeqLLM:
    """plan 固定;reflect 按序列返回(耗尽后默认 answer)。记录每次 prompt 正文。"""

    configured = True

    def __init__(self, reflects, plan=None):
        self._plan = plan or {"sub_queries": [{"query": "版图设计要点"}]}
        self._reflects = list(reflects)
        self.plan_prompts: list[str] = []
        self.reflect_prompts: list[str] = []
        self.schema_hints: list[str] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        content = messages[-1]["content"]
        self.schema_hints.append(schema_hint)
        if "sub_queries" in schema_hint:
            self.plan_prompts.append(content)
            return json.dumps(self._plan)
        self.reflect_prompts.append(content)
        if self._reflects:
            return json.dumps(self._reflects.pop(0))
        return json.dumps({"next_action": "answer", "sufficient": True})


def _enumerate_action(kind="formula", **extra):
    request = {"kind": kind}
    request.update(extra)
    return {"next_action": "enumerate_elements", "enumerate": request,
            "reason": "列出公式"}


def _enumerate_sources_action(reason="先看库里有哪几篇", **extra):
    """来源清单不是第 11 个动作:它是 enumerate 动作的一个参数值(design doc
    §6.2 用户拍板)。这个 helper 就是那份合同的唯一拼写——测试里到处手写
    ``{"collection": "sources"}`` 的话,哪天形态变了会有十几处各自漂移。"""
    request = {"collection": "sources"}
    request.update(extra)
    return {"next_action": "enumerate_elements", "enumerate": request,
            "reason": reason}


def _retriever(repo, llm, *, limits_overrides=None, fail_closed=False):
    bind_chat_client(repo, "reasoning_agent", llm)
    retriever = ReasoningRetriever.from_repository(
        repo, repo.settings, fail_closed=fail_closed
    )
    limits = ask_retrieval_limits("standard")
    if limits_overrides:
        limits = replace(limits, **limits_overrides)
    return retriever, limits


def _steps(result, step_type):
    return [step for step in result.trace if step.step_type == step_type]


def _skips(result):
    return {step.detail.get("reason"): step for step in _steps(result, "skip")}


class _StubEnumeration:
    """按脚本回放执行器结果,并记录每次调用的参数(游标/取消句柄/预算)。"""

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[dict] = []

    def _next(self, payload):
        self.calls.append(payload)
        if not self._results:
            raise AssertionError("stub enumeration called more times than scripted")
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def enumerate_elements(self, notebook_id, kind, *, source_id="", budget,
                           cursor=None, cancel_event=None):
        return self._next({"collection": "elements", "kind": kind,
                           "source_id": source_id, "budget": budget,
                           "cursor": cursor, "cancel_event": cancel_event})

    def enumerate_kg_objects(self, notebook_id, object_type, *, budget,
                             cursor=None, cancel_event=None):
        return self._next({"collection": "kg_objects", "kind": object_type,
                           "budget": budget, "cursor": cursor,
                           "cancel_event": cancel_event})

    def enumerate_sources(self, notebook_id, *, budget, cursor=None,
                          cancel_event=None):
        return self._next({"collection": "sources", "kind": "",
                           "budget": budget, "cursor": cursor,
                           "cancel_event": cancel_event})


def _coverage(**overrides):
    base = dict(returned=1, returned_total=1, scanned=1, total=1, has_more=False,
                complete=True, truncated_reason="", overflow_semantics="")
    base.update(overrides)
    return EnumerationCoverage(**base)


# ------------------------------------------------------------ 清单名唯一性

def test_collection_labels_are_globally_unique():
    """两张标签表的**渲染结果**在全域不得重名。

    ``formula`` 在两侧都存在。若都渲染成「公式清单」,回喂给模型的账目里会同时
    出现「公式清单已完整列出 12 条」和「公式清单已列出 40 条,尚未列完」——模型
    有理由据此认为那条未完的已经列全而放弃续跑;trace 上也是两步同名却配着互相
    矛盾的数字。断言在**渲染后**的标签上,所以改后缀、改任一张表都会被抓到。
    """
    from app.services.collection_catalog import (
        ENUMERABLE_ELEMENT_KINDS, ENUMERABLE_KG_OBJECT_TYPES,
    )
    from app.services.reasoning_retrieval import _collection_label

    labels = (
        [_collection_label("elements", kind) for kind in ENUMERABLE_ELEMENT_KINDS]
        + [_collection_label("kg_objects", object_type)
           for object_type in ENUMERABLE_KG_OBJECT_TYPES]
        + [_collection_label("sources", "")]
    )
    assert len(labels) == len(set(labels)), labels
    assert len(labels) == 9, "白名单变了就要重新审这条守卫"
    # 撞名的那一对必须真的分开(把它写死,免得「唯一」靠的是别处的偶然差异)。
    assert _collection_label("elements", "formula") != _collection_label(
        "kg_objects", "formula")
    # 来源清单的 kind 恒为空串:它必须由 collection 决定标签,而不是落进
    # 「不是 elements 就查 KG 表」的分支拼出一个光秃秃的「清单」。
    assert _collection_label("sources", "") == "来源清单"


# ------------------------------------------------------------- prompt 合同

def test_reflect_prompt_and_schema_offer_the_tools_only_when_supplied():
    from app.services.collection_catalog import (
        ENUMERABLE_ELEMENT_KINDS, ENUMERABLE_KG_OBJECT_TYPES,
    )
    from app.services.prompts import (
        REFLECT_SCHEMA_HINT, reflect_prompt, reflect_schema_hint,
    )

    off = reflect_prompt("q", "s")
    on = reflect_prompt("q", "s", element_kinds=ENUMERABLE_ELEMENT_KINDS,
                        object_types=ENUMERABLE_KG_OBJECT_TYPES)

    assert "enumerate_elements" not in off
    assert "enumerate_kg_objects" not in off
    assert "enumerate_elements" in on and "enumerate_kg_objects" in on
    # 与 search_elements 的分工必须写清楚,否则模型会继续用相关性检索凑清单。
    assert "PREFER this over search_elements" in on
    # 大集合不该硬翻页,而该报计数 + 样例 + 建议缩小范围。
    assert "do NOT try to page through it" in on
    assert "narrow the request" in on
    # 再次请求 = 续跑,不是重来。
    assert "CONTINUES from where the previous call stopped" in on
    for kind in ENUMERABLE_ELEMENT_KINDS:
        assert kind in on
    for object_type in ENUMERABLE_KG_OBJECT_TYPES:
        assert object_type in on

    assert "enumerate" not in REFLECT_SCHEMA_HINT
    schema = reflect_schema_hint(ENUMERABLE_ELEMENT_KINDS,
                                 ENUMERABLE_KG_OBJECT_TYPES)
    assert '"enumerate":{"kind":"' in schema
    assert '"object_type":"' in schema and '"source_id":""' in schema
    # 模型看不到内部 source id(候选摘要与引用里只有标题),所以限定单一来源
    # 必须能按**名字**表达,由服务端解析(codex 第 1 轮 P2-4)。
    assert '"source_title":""' in schema
    assert "set enumerate.source_title" in on
    assert "copied EXACTLY as it appears in the candidates" in on
    assert "enumerate_elements|enumerate_kg_objects" in schema
    # 关闭态必须与接入前**逐字**一致,否则「回到现状」只是说法。对照的是钉死的
    # 字面量,不是 REFLECT_SCHEMA_HINT ——后者现在就是 reflect_schema_hint() 的
    # 返回值,拿它比等于让常量跟自己比,恒真。
    assert reflect_schema_hint() == FROZEN_REFLECT_SCHEMA_HINT
    assert REFLECT_SCHEMA_HINT == FROZEN_REFLECT_SCHEMA_HINT


def test_completeness_claim_rule_follows_the_tools():
    from app.services.prompts import reflect_prompt

    off = reflect_prompt("q", "s")
    on = reflect_prompt("q", "s", element_kinds=("formula",))

    # PR-1 的无条件禁令在没有工具时原样保留(相关性检索确实证明不了完整)。
    assert "NEVER claim that 'all/every X have been retrieved'" in off
    # 有工具时改写成「以工具报的 coverage 为准」——仍然是禁令,只是多了唯一出口。
    assert "NEVER claim that 'all/every X have been retrieved'" not in on
    assert "ONLY when an enumerate action has reported its coverage as complete" in on
    assert "neither can a partial or interrupted enumeration" in on


def test_reflect_prompt_offers_the_sources_collection(repo):
    """来源清单从**参数**进来,动作空间维持 10 个(design doc §6.2 用户拍板)。

    这条同时是那个决定的回归门:模型面若哪天多出一个 `enumerate_sources` 动作 id,
    下面第一组断言会红。
    """
    from app.services.collection_catalog import (
        ENUMERABLE_ELEMENT_KINDS, ENUMERABLE_KG_OBJECT_TYPES,
    )
    from app.services.prompts import reflect_prompt, reflect_schema_hint

    off = reflect_prompt("q", "s")
    on = reflect_prompt("q", "s", element_kinds=ENUMERABLE_ELEMENT_KINDS,
                        object_types=ENUMERABLE_KG_OBJECT_TYPES)
    schema_on = reflect_schema_hint(ENUMERABLE_ELEMENT_KINDS,
                                    ENUMERABLE_KG_OBJECT_TYPES)

    # 不是第三个动作 id:动作串里没有它,prompt 也不把它当动作名教。
    assert "enumerate_sources" not in schema_on
    assert "enumerate_sources" not in on
    assert schema_on.count("enumerate_") == 2
    # 是 enumerate 分支的一个参数值,与 kind/object_type 并列。
    assert '"collection":"sources"' in schema_on
    assert 'enumerate.collection to "sources"' in on
    assert "collection" not in reflect_schema_hint()   # 关掉工具就整段不存在
    assert "enumerate.collection" not in off
    # 「先拿目录、再按标题逐篇深挖」——这条分工必须写清楚,否则模型会拿相关性
    # 检索去凑「有哪几篇」。
    assert "add_subquery using that document's TITLE" in on
    assert "never the roster" in on


def test_scope_deixis_grounding_reaches_every_query_writing_prompt():
    """指示语接地(design doc §6.1):四份把用户问题变成检索文本的 prompt
    ——意图契约、两份规划拼写、反思——必须都带这一段。

    少任何一份,那一层就会继续把「当前notebook」「知识图谱」当关键词发出去:
    没有文档含有装着它的那个库的名字,所以那是一次注定零命中的探测。
    """
    from app.services.prompts import (
        SCOPE_DEIXIS_GROUNDING, expand_query_prompt, plan_prompt,
        query_intent_prompt, reflect_prompt,
    )

    assert SCOPE_DEIXIS_GROUNDING in query_intent_prompt("q")
    assert SCOPE_DEIXIS_GROUNDING in plan_prompt("q")
    assert SCOPE_DEIXIS_GROUNDING in expand_query_prompt("q")
    assert SCOPE_DEIXIS_GROUNDING in reflect_prompt("q", "s")
    # 关掉枚举工具也不影响这一段:它讲的是子查询卫生,与工具无关。
    assert SCOPE_DEIXIS_GROUNDING in reflect_prompt(
        "q", "s", element_kinds=("formula",))


def test_scope_deixis_grounding_names_the_phrases_and_the_rule():
    """内容断言:中英两组指示语 + 「剥掉,别当关键词」+ 「别把问题改成另一个」。"""
    from app.services.prompts import SCOPE_DEIXIS_GROUNDING as text

    for phrase in ("当前notebook", "这个库", "本库", "整个库",
                   "the current notebook", "this library", "知识图谱", "KG"):
        assert phrase in text, phrase
    assert "not content inside it" in text
    assert "DROP it" in text
    # 剥词不等于换题:去掉范围词之后问题本身必须留着(否则「库里的文章讲了
    # 什么」会被剥成空)。
    assert "must never turn it into a different question" in text


def test_kg_is_a_scope_word_only_in_the_possessive_form():
    """「知识图谱 / KG」的收窄 + **反向豁免**必须同时钉住(quality P1-1)。

    此前这段把 KG 无条件判成非话题。库里就是 GraphRAG/LightRAG 论文时,
    「这些论文里知识图谱是怎么构建的」的检索词恰恰是「知识图谱」——剥掉它不是去噪,
    是把查询本身删了。这一段还不受枚举 kill switch 约束、深度报告每节每步都付,
    所以读错的代价正好落在最在意它的那批语料上。

    只钉「要剥」的一半是不够的:那正是回归会发生的地方(把豁免顺手删掉,
    「要剥」那半仍然全绿)。
    """
    from app.services.prompts import SCOPE_DEIXIS_GROUNDING as text

    # ① 收窄:只在领属/指示形式下才算范围词。
    assert "ONLY in that possessive form" in text
    for form in ("本库的知识图谱", "这个库的图谱",
                 "the knowledge graph of this library"):
        assert form in text, form
    # ② 反向豁免:文档本身讨论知识图谱时,它是正当话题与检索词。
    assert "ordinary TOPIC and stays a search term" in text
    assert "这些论文里知识图谱是怎么构建的" in text
    assert "must keep 知识图谱" in text
    # 两半都必须真的到得了四份 prompt(豁免只写在常量里、没被拼进去等于没写)。
    from app.services.prompts import (
        expand_query_prompt, plan_prompt, query_intent_prompt, reflect_prompt,
    )
    for rendered in (query_intent_prompt("q"), plan_prompt("q"),
                     expand_query_prompt("q"), reflect_prompt("q", "s")):
        assert "ordinary TOPIC and stays a search term" in rendered


def test_scope_deixis_grounding_precedes_the_question_it_governs():
    """位置断言(移动变异的另一半):这一段必须排在 `Question:` / `User request:`
    之前。

    删除变异由上面那条覆盖;把它挪到问题**之后**则是删不掉也测不出的另一种坏法
    ——规则出现在被它约束的输入后面,模型已经读完问题才被告知怎么处理范围词。
    """
    from app.services.prompts import (
        expand_query_prompt, plan_prompt, query_intent_prompt,
    )

    for rendered, marker in (
        (query_intent_prompt("q"), "User request:"),
        (plan_prompt("q"), "Question:"),
        (expand_query_prompt("q"), "Question:"),
    ):
        grounding_at = rendered.index("Scope words are not search terms")
        question_at = rendered.index(marker)
        assert grounding_at < question_at, marker


def test_reflect_names_the_fields_a_scope_word_could_leak_into():
    """reflect 是唯一有四个自由文本检索字段的 prompt,必须逐个点名。

    exact_term 尤其:它是字面匹配,一个范围词进去就是保证零命中的探测。
    """
    from app.services.prompts import reflect_prompt

    on = reflect_prompt("q", "s", element_kinds=("formula",))
    for field in ("new_sub_query.query", "elements_query", "ppr_query",
                  "exact_term"):
        assert field in on, field
    # 有工具时才教「问库本身的规模看计数行」——没有工具时那行计数根本不存在。
    assert "[Collections in scope] counts and the enumerate actions" in on
    assert "[Collections in scope] counts and the enumerate actions" not in (
        reflect_prompt("q", "s"))


def test_query_intent_prompt_keeps_a_library_wide_request_as_the_topic():
    """「当前notebook有哪几篇」不是歧义,而是一个范围明确的完整枚举请求。"""
    from app.services.prompts import query_intent_prompt

    text = query_intent_prompt("q")
    assert "the open one is the answer" in text
    assert "complete or hybrid" in text


def test_plan_side_prompts_take_the_collection_map():
    from app.services.prompts import expand_query_prompt, plan_prompt

    line = "[Collections in scope] elements: formula 12 | KG objects: concept 3"
    assert line not in plan_prompt("q")
    assert line in plan_prompt("q", "", line)
    # plan() 实际发出的是 expand_query_prompt;地图必须落在**这一份**上,
    # 否则「注入规划上下文」只是注释里的说法。
    assert line not in expand_query_prompt("q")
    assert line in expand_query_prompt("q", collection_map=line)


# --------------------------------------------------------------- 动作分发

def test_enumerate_elements_action_lists_the_whole_collection(repo):
    notebook = _seed(repo, formulas=3)
    llm = _SeqLLM([_enumerate_action(), {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "库里有哪些公式", "", limits=limits)

    assert len(result.enumerations) == 1
    outcome = result.enumerations[0]
    assert outcome.collection == "elements" and outcome.kind == "formula"
    assert [item.element_id for item in outcome.items] == [
        "el-001", "el-002", "el-003"]
    assert outcome.coverage.complete is True
    assert outcome.coverage.returned_total == 3 and outcome.coverage.total == 3
    # 清单绝不混进相关性候选池(那里会被 top_n / element_items 截断)。
    assert result.elements == []
    step = _steps(result, "enumerate")[0]
    assert step.summary == "枚举公式清单: 已全部列出 3 条"
    assert step.detail["collection"] == "elements"
    assert step.detail["kind"] == "formula"
    assert step.detail["returned"] == 3
    assert step.detail["returned_total"] == 3
    assert step.detail["total"] == 3
    assert step.detail["complete"] is True
    assert step.detail["has_more"] is False
    assert step.detail["truncated_reason"] == ""
    # 前端 enumerate 分支按 detail.scanned_rows 渲染「N/M 行」——本步刻意不产出
    # 那个字段(单位是「条目」且分母可能未知),否则会渲染成 12/0 行。
    assert "scanned_rows" not in step.detail
    assert "known_total_rows" not in step.detail


def test_enumerate_kg_objects_action_uses_the_object_type_whitelist(repo):
    notebook = _seed(repo, formulas=1)
    llm = _SeqLLM([
        {"next_action": "enumerate_kg_objects",
         "enumerate": {"object_type": "claim"}},
        {"next_action": "answer", "sufficient": True},
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "有哪些论断", "", limits=limits)

    outcome = result.enumerations[0]
    assert outcome.collection == "kg_objects" and outcome.kind == "claim"
    assert len(outcome.items) == 1
    # KG 侧一律带「知识对象」限定,与文档元素侧的同名类型区分开(见唯一性守卫)。
    assert _steps(result, "enumerate")[0].summary == (
        "枚举论断知识对象清单: 已全部列出 1 条")


def test_enumerate_sources_action_lists_the_library_roster(repo):
    """来源清单:`enumerate.collection="sources"` 产出一份文档目录,trace 说
    「枚举来源清单」。"""
    notebook = _seed(repo, formulas=1)
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,doc_type,summary,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("s2", notebook.id, "论文二", "pdf", "extracted", "extracted",
             "academic_paper", "第二篇摘要", NOW, NOW),
        )
    repo.collection_catalog.invalidate()
    llm = _SeqLLM([
        _enumerate_sources_action(),
        {"next_action": "answer", "sufficient": True},
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "当前notebook的文章分析", "", limits=limits)

    outcome = result.enumerations[0]
    assert outcome.collection == "sources"
    assert outcome.kind == ""            # 没有子类型
    assert outcome.source_id == ""
    assert [item.source_id for item in outcome.items] == ["s1", "s2"]
    assert [item.source_title for item in outcome.items] == ["论文一", "论文二"]
    assert outcome.coverage.complete is True
    assert outcome.coverage.returned_total == 2 and outcome.coverage.total == 2
    step = _steps(result, "enumerate")[0]
    assert step.summary == "枚举来源清单: 已全部列出 2 条"
    assert step.detail["collection"] == "sources"
    assert step.detail["kind"] == ""
    # 「没指定条目类型」那条 skip 不得对来源清单生效。
    assert "enumeration_kind" not in _skips(result)


def test_the_collection_parameter_decides_not_the_action_id(repo):
    """动作空间维持 10 个的直接推论:选哪个 enumerate 动作 id 都不影响结果——
    `collection="sources"` 一旦给出,这一轮列的就是文档目录。

    钉住它是因为反过来的写法(按动作 id 判 sources)会让模型选中 kg_objects +
    collection=sources 时静默列出概念清单,而模型明确说了它要哪个集合。
    """
    notebook = _seed(repo, formulas=1)
    llm = _SeqLLM([
        {"next_action": "enumerate_kg_objects",
         "enumerate": {"collection": "sources", "object_type": "claim"}},
        {"next_action": "answer", "sufficient": True},
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "有哪几篇", "", limits=limits)

    outcome = result.enumerations[0]
    assert outcome.collection == "sources"
    # 同时给了 object_type 也不生效:集合选择器优先,否则同一个请求有两种解释。
    assert outcome.kind == ""
    assert _steps(result, "enumerate")[0].summary.startswith("枚举来源清单")


def test_an_unrecognized_collection_value_teaches_the_legal_one(repo):
    """非法 collection 值 + 没给 kind ⇒ skip 文案要教「该给什么」。

    沿用 exact_lookup 那条实测教训:只说「没指定类型」的话,模型下一轮往往换一个
    同样非法的集合名再试一次。所以点名唯一合法值 sources,并把模型给的原值记进
    detail 供排查(原值只进 detail,不进面向用户的措辞之外的任何地方)。
    """
    notebook = _seed(repo, formulas=1)
    llm = _SeqLLM([
        {"next_action": "enumerate_elements",
         "enumerate": {"collection": "documents"}},
        {"next_action": "answer", "sufficient": True},
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "库里有哪几篇", "", limits=limits)

    skip = _skips(result)["enumeration_kind"]
    assert "documents" in skip.summary
    assert "sources" in skip.summary          # 唯一合法值必须点名
    assert skip.detail["requested_collection"] == "documents"
    assert result.enumerations == []
    # 合法值那条不得被这段教学文案污染(它根本不该走到这个 skip)。
    assert "不是可枚举的集合名" not in "".join(
        step.summary for step in result.trace if step.step_type == "enumerate")


def test_an_unrecognized_collection_value_falls_back_to_the_action_id(repo):
    """`collection` 只识别 "sources"。其他值(含 "elements"/"kg_objects" 与垃圾
    值)在解析期就清成空串,落回按动作 id 的 kind 分派——与本分支其他非白名单值
    的 fail-open 处理同形,而不是废掉整个动作。"""
    notebook = _seed(repo, formulas=2)
    llm = _SeqLLM([
        _enumerate_action(kind="formula", collection="everything"),
        {"next_action": "answer", "sufficient": True},
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "库里有哪些公式", "", limits=limits)

    outcome = result.enumerations[0]
    assert outcome.collection == "elements" and outcome.kind == "formula"
    assert len(outcome.items) == 2


def test_sources_action_shares_the_one_run_budget_pool(repo):
    """三个集合共用同一个行预算池:来源清单也从同一个池里扣。"""
    notebook = _seed(repo, formulas=4)
    with repo._write() as db:
        for index in range(2, 5):
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"s{index}", notebook.id, f"来源{index}", "pdf", "extracted",
                 "extracted", NOW, NOW),
            )
    repo.collection_catalog.invalidate()
    llm = _SeqLLM([
        _enumerate_sources_action(),
        _enumerate_action(),
        {"next_action": "answer", "sufficient": True},
    ])
    retriever, limits = _retriever(repo, llm, limits_overrides={
        "enum_rows_per_run": 4,
    })

    result = retriever.run(notebook.id, "库里有哪几篇、都有哪些公式", "",
                           limits=limits)

    sources_outcome = result.enumerations[0]
    assert sources_outcome.collection == "sources"
    assert sources_outcome.coverage.returned_total == 4        # 池被它用光
    # 第二个动作只剩 0 行额度 → 必须 skip,而不是请求 0 行(那会 ValueError)。
    assert "enumeration_budget" in _skips(result)
    assert len(result.enumerations) == 1


def test_sources_chain_resumes_on_a_second_request(repo):
    """同一个集合再次请求 = 续跑(键含 collection,与元素/KG 链互不串台)。"""
    from app.services.collection_enumeration import SourceEnumeration

    notebook = _seed(repo, formulas=1)
    partial = SourceEnumeration(
        items=(), cursor="cursor-1", extra_pages=0, payload_chars=10,
        coverage=_coverage(returned=1, returned_total=1, complete=False,
                           has_more=True, total=2,
                           truncated_reason=TRUNCATED_BUDGET),
    )
    done = SourceEnumeration(
        items=(), cursor=None, extra_pages=0, payload_chars=10,
        coverage=_coverage(returned=1, returned_total=2, total=2),
    )
    stub = _StubEnumeration([partial, done])
    llm = _SeqLLM([
        _enumerate_sources_action(),
        _enumerate_sources_action(),
        {"next_action": "answer", "sufficient": True},
    ])
    retriever, limits = _retriever(repo, llm)
    retriever.collection_enumeration = stub

    result = retriever.run(notebook.id, "有哪几篇", "", limits=limits)

    assert len(result.enumerations) == 1
    assert result.enumerations[0].coverage.returned_total == 2
    assert stub.calls[0]["cursor"] is None
    assert stub.calls[1]["cursor"] == "cursor-1"
    assert stub.calls[1]["collection"] == "sources"


def test_completed_sources_listing_is_not_enumerated_twice(repo):
    notebook = _seed(repo, formulas=1)
    llm = _SeqLLM([
        _enumerate_sources_action(),
        _enumerate_sources_action(),
        {"next_action": "answer", "sufficient": True},
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "有哪几篇", "", limits=limits)

    skips = _skips(result)
    assert "already_enumerated" in skips
    assert skips["already_enumerated"].summary == "跳过枚举来源清单(本轮已全部列出)"


def test_second_request_of_the_same_collection_resumes_the_cursor(repo):
    """同集合再次请求 = 续跑:条目接着上次往下,returned_total 是链上累计。

    用 stub 驱动,因为三个预算池(行/页/载荷)都是 **run 级** 的:真执行器一次
    动作若因任一池截断,那个池当场就见底,第二次动作只会被 skip 拦下。续跑的
    机制本身仍必须成立(执行器可能因作用域外的理由停在半路),这里钉的就是
    run() 把上次的游标接回去、把两段并成同一个集合结果。
    """
    notebook = _seed(repo, formulas=4)
    partial = ElementEnumeration(
        kind="formula", items=(), cursor="cursor-1", extra_pages=0,
        payload_chars=10,
        coverage=_coverage(returned=2, returned_total=2, complete=False,
                           has_more=True, total=4,
                           truncated_reason=TRUNCATED_BUDGET),
    )
    done = ElementEnumeration(
        kind="formula", items=(), cursor=None, extra_pages=0, payload_chars=10,
        coverage=_coverage(returned=2, returned_total=4, total=4),
    )
    stub = _StubEnumeration([partial, done])
    llm = _SeqLLM([_enumerate_action(), _enumerate_action(),
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)
    retriever.collection_enumeration = stub

    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)

    assert len(result.enumerations) == 1, "续跑必须并进同一个集合结果,不是两条"
    outcome = result.enumerations[0]
    assert outcome.coverage.returned_total == 4
    assert outcome.coverage.complete is True
    steps = _steps(result, "enumerate")
    assert len(steps) == 2
    assert steps[0].detail["returned"] == 2
    assert steps[0].detail["returned_total"] == 2
    assert steps[0].detail["complete"] is False
    assert steps[1].detail["returned"] == 2
    assert steps[1].detail["returned_total"] == 4
    # 行池按**本次返回数**扣,不是按链上累计:后者会把第一次的 2 条重复计费
    # (2 + 4 = 6),预算表面上还剩、实际早就超发。
    answer_step = _steps(result, "answer")[0]
    assert answer_step.detail["enumerated_items"] == 4
    assert answer_step.detail["enumerations"] == 1


def test_payload_allowance_is_a_run_pool_not_a_per_action_grant(repo):
    """载荷上限是 **一次问答** 的公开契约(structured_payload_chars),不是每个
    动作各来一份。

    codex 第 1 轮 P2-3:每次动作都发全新满额时,deep/thorough/exhaustive 的多次
    枚举可以持久化并返回数倍于文档上限的结构化载荷。这里钉两件事——传给执行器
    的额度逐次递减(等于剩余),以及额度用尽后不再发起动作而是记 skip。
    """
    notebook = _seed(repo, formulas=4)
    first = ElementEnumeration(
        kind="formula", items=(), cursor="cursor-1", extra_pages=0,
        payload_chars=700,
        coverage=_coverage(returned=2, returned_total=2, complete=False,
                           has_more=True, total=4,
                           truncated_reason=TRUNCATED_PAYLOAD),
    )
    second = ElementEnumeration(
        kind="formula", items=(), cursor="cursor-2", extra_pages=0,
        payload_chars=300,
        coverage=_coverage(returned=1, returned_total=3, complete=False,
                           has_more=True, total=4,
                           truncated_reason=TRUNCATED_PAYLOAD),
    )
    stub = _StubEnumeration([first, second])
    llm = _SeqLLM([_enumerate_action()] * 3
                  + [{"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(
        repo, llm, limits_overrides={"structured_payload_chars": 1000}
    )
    retriever.collection_enumeration = stub

    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)

    granted = [call["budget"].max_payload_chars for call in stub.calls]
    assert granted == [1000, 300], granted
    # 第三次动作:剩余为 0,必须停在 skip 而不是再发一次满额。
    skip = _skips(result)["enumeration_budget"]
    assert skip.detail["payload_left"] == 0
    assert skip.detail["rows_left"] > 0, "拦住它的必须是载荷池,不是行池"
    assert len(stub.calls) == 2


def test_resumed_call_receives_the_previous_cursor(repo):
    """续跑必须把上次的游标原样传回执行器——不传就是从头重列。"""
    notebook = _seed(repo, formulas=1)
    partial = ElementEnumeration(
        kind="formula", items=(), cursor="cursor-1", extra_pages=0, payload_chars=0,
        coverage=_coverage(returned=0, returned_total=2, complete=False,
                           has_more=True, truncated_reason=TRUNCATED_BUDGET),
    )
    done = ElementEnumeration(
        kind="formula", items=(), cursor=None, extra_pages=0, payload_chars=0,
        coverage=_coverage(returned=0, returned_total=2),
    )
    stub = _StubEnumeration([partial, done])
    llm = _SeqLLM([_enumerate_action(), _enumerate_action(),
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)
    retriever.collection_enumeration = stub

    retriever.run(notebook.id, "哪些公式", "", limits=limits)

    assert [call["cursor"] for call in stub.calls] == [None, "cursor-1"]


# ----------------------------------------------------------------- 预算池

def test_row_budget_is_one_pool_shared_by_both_collections(repo):
    """行预算是主闸,且**跨元素与知识对象两类动作**共用一个池。

    第二个动作刻意换成 enumerate_kg_objects:若两类各记一份预算(常见的「顺手拆
    成两池」变异),元素池耗尽拦不住知识对象清单,这条就必须红。页大小取满(50)
    同样是刻意的——这样一次动作扫过的行数不足一页,页池一分不扣,拦住第二个动作
    的只可能是行池。
    """
    notebook = _seed(repo, formulas=5)
    llm = _SeqLLM([
        _enumerate_action("formula"),
        {"next_action": "enumerate_kg_objects",
         "enumerate": {"object_type": "claim"}},
        {"next_action": "answer", "sufficient": True},
    ])
    retriever, limits = _retriever(
        repo, llm,
        limits_overrides={"enum_page_size": 50, "enum_pages_per_run": 2,
                          "enum_rows_per_run": 2},
    )

    result = retriever.run(notebook.id, "哪些公式和论断", "", limits=limits)

    assert [step.detail["returned"] for step in _steps(result, "enumerate")] == [2]
    assert [outcome.collection for outcome in result.enumerations] == ["elements"]
    assert result.enumerations[0].coverage.complete is False
    assert result.enumerations[0].coverage.truncated_reason == TRUNCATED_BUDGET
    # 公式吃光额度后,知识对象清单一条都拿不到。
    skip = _skips(result)["enumeration_budget"]
    assert skip.detail["collection"] == "kg_objects"
    assert skip.detail["kind"] == "claim"
    assert skip.detail["rows_left"] == 0
    assert skip.detail["pages_left"] == 2, "页预算不该被扣,拦住它的必须是行池"
    assert skip.summary == "跳过枚举(已达本轮可列出的条目上限)"


def test_page_budget_is_charged_by_real_round_trips_not_scanned_rows(repo):
    """页预算按**执行器回传的真实额外往返数**计费,不是「扫过行数 ÷ 页大小」的
    上界折算。

    210 条公式、standard 档(行 200 / 页 4 / 页大小 50):一次动作走 4 页(首页免费
    + 3 次额外往返)后被行池拦停,页池应只被扣 3。若改回上界折算(200 // 50 = 4),
    页池当场归零,行池明明还没成为瓶颈的事实就被掩盖了——这条会红。
    """
    notebook = _seed(repo, formulas=210)
    llm = _SeqLLM([_enumerate_action()] * 2
                  + [{"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)
    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)

    outcome = result.enumerations[0]
    assert outcome.coverage.returned_total == limits.enum_rows_per_run == 200
    assert len(outcome.items) == 200
    assert outcome.coverage.truncated_reason == TRUNCATED_BUDGET
    budget_skip = _skips(result)["enumeration_budget"]
    assert budget_skip.detail["rows_left"] == 0
    assert budget_skip.detail["pages_left"] == 1, (
        "4 页只有 3 次额外往返;扣成 4 就是上界折算的痕迹")


def test_extra_page_budget_stops_enumeration_while_rows_remain(repo):
    """页池独立成立:行还有富余时,额外翻页数用尽同样必须停。"""
    notebook = _seed(repo, formulas=5)
    llm = _SeqLLM([_enumerate_action(), _enumerate_action(),
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(
        repo, llm,
        limits_overrides={"enum_page_size": 1, "enum_pages_per_run": 1,
                          "enum_rows_per_run": 10},
    )

    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)

    assert [step.detail["returned"] for step in _steps(result, "enumerate")] == [2]
    skip = _skips(result)["enumeration_budget"]
    assert skip.detail["rows_left"] == 8
    assert skip.detail["pages_left"] == 0


def test_budget_pool_never_requests_zero_rows(repo):
    """预算耗尽必须跳过动作,而不是拿 max_rows=0 去构造 EnumerationBudget
    (执行器对非正上限直接 ValueError)。"""
    notebook = _seed(repo, formulas=3)
    llm = _SeqLLM([_enumerate_action(), _enumerate_action(),
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(
        repo, llm,
        limits_overrides={"enum_page_size": 1, "enum_pages_per_run": 1,
                          "enum_rows_per_run": 1},
    )
    stub_calls: list = []
    real = retriever.collection_enumeration

    class _Recording:
        def enumerate_elements(self, *args, **kwargs):
            stub_calls.append(kwargs["budget"])
            return real.enumerate_elements(*args, **kwargs)

    retriever.collection_enumeration = _Recording()
    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)

    assert [budget.max_rows for budget in stub_calls] == [1]
    assert "enumeration_budget" in _skips(result)


# ------------------------------------------------------- 已列全 / 冲突 / 非法

def test_completed_collection_is_not_enumerated_twice(repo):
    notebook = _seed(repo, formulas=2)
    llm = _SeqLLM([_enumerate_action(), _enumerate_action(),
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)
    calls: list = []
    real = retriever.collection_enumeration

    class _Counting:
        def enumerate_elements(self, *args, **kwargs):
            calls.append(1)
            return real.enumerate_elements(*args, **kwargs)

    retriever.collection_enumeration = _Counting()
    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)

    assert len(calls) == 1
    skip = _skips(result)["already_enumerated"]
    assert skip.summary == "跳过枚举公式清单(本轮已全部列出)"


def test_concurrent_change_becomes_a_terminal_conflict(repo):
    notebook = _seed(repo, formulas=2)
    conflicted = ElementEnumeration(
        kind="formula", items=(), cursor=None, extra_pages=0, payload_chars=0,
        coverage=_coverage(returned=0, returned_total=7, complete=False,
                           has_more=True, total=9,
                           truncated_reason=TRUNCATED_CONCURRENT_CHANGE,
                           overflow_semantics="explicit_partial"),
    )
    stub = _StubEnumeration([conflicted])
    llm = _SeqLLM([_enumerate_action(), _enumerate_action(),
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)
    retriever.collection_enumeration = stub

    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)

    # 冲突后不再重开一条链(执行器只被调了一次),但 coverage 保留给 T5 披露。
    assert len(stub.calls) == 1
    outcome = result.enumerations[0]
    assert outcome.coverage.truncated_reason == TRUNCATED_CONCURRENT_CHANGE
    assert outcome.coverage.complete is False
    assert outcome.coverage.returned_total == 7
    conflict_step = _steps(result, "enumerate")[0]
    assert "无法确认是否完整" in conflict_step.summary
    assert _skips(result)["enumeration_conflict"].summary == (
        "跳过枚举公式清单(资料有变动,无法继续)")


def test_unknown_kind_fails_open_into_a_skip(repo):
    notebook = _seed(repo, formulas=2)
    stub = _StubEnumeration([])          # 一次都不该被调用
    llm = _SeqLLM([_enumerate_action("paragraph"),
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)
    retriever.collection_enumeration = stub

    result = retriever.run(notebook.id, "哪些段落", "", limits=limits)

    assert stub.calls == []
    assert result.enumerations == []
    assert _skips(result)["enumeration_kind"].summary == (
        "跳过枚举(没有指定可列出的条目类型)")


def test_unknown_kind_fails_closed_for_authoring_flows(repo):
    _seed(repo, formulas=1)
    llm = _SeqLLM([_enumerate_action("paragraph")])
    retriever, _limits = _retriever(repo, llm, fail_closed=True)

    with pytest.raises(ValueError, match="enumerate_elements"):
        retriever.reflect("哪些段落", "candidates")


def test_out_of_scope_source_id_fails_open_into_a_skip(repo):
    notebook = _seed(repo, formulas=2)
    llm = _SeqLLM([_enumerate_action(source_id="not-in-this-notebook"),
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)

    assert result.enumerations == []
    assert _skips(result)["enumeration_rejected"].summary == (
        "跳过枚举公式清单(请求的范围不可用)")


def _add_titled_source(repo, notebook_id, source_id, title, formulas=1):
    """再加一个带标题的来源(``_seed`` 的 s1 标题固定为「论文一」)。"""
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, title, "pdf", "extracted", "extracted",
             NOW, NOW),
        )
        for index in range(formulas):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,metadata,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"{source_id}-el-{index}", source_id, "formula",
                 f"p{index}", f"公式 {source_id} {index}", "{}", NOW),
            )
    repo.collection_catalog.invalidate()


def test_source_title_resolves_to_the_named_source(repo):
    """「列出《某某》里的公式」:模型只能给标题,服务端确定性解析成来源。"""
    notebook = _seed(repo, formulas=2)
    _add_titled_source(repo, notebook.id, "s2", "论文二", formulas=3)
    llm = _SeqLLM([_enumerate_action(source_title="  论文二 "),
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "《论文二》里有哪些公式", "", limits=limits)

    assert [outcome.source_id for outcome in result.enumerations] == ["s2"]
    outcome = result.enumerations[0]
    assert len(outcome.items) == 3
    assert {item.source_id for item in outcome.items} == {"s2"}
    assert outcome.coverage.complete is True


def test_unmatched_source_title_is_skipped_not_widened(repo):
    """名字对不上时必须 skip,绝不退成「那就枚举整个库」。"""
    notebook = _seed(repo, formulas=2)
    llm = _SeqLLM([_enumerate_action(source_title="不存在的论文"),
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)

    assert result.enumerations == []
    skip = _skips(result)["enumeration_source_unresolved"]
    assert skip.summary == "跳过枚举公式清单(没有名称匹配的来源)"
    assert skip.detail["matches"] == 0
    assert skip.detail["requested_title"] == "不存在的论文"


def test_ambiguous_source_title_is_skipped(repo):
    """两个来源同名 = 无法确定是哪一个,同样 skip(不挑第一个)。"""
    notebook = _seed(repo, formulas=2)
    _add_titled_source(repo, notebook.id, "s2", "同名论文", formulas=1)
    _add_titled_source(repo, notebook.id, "s3", "同名论文", formulas=1)
    llm = _SeqLLM([_enumerate_action(source_title="同名论文"),
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)

    assert result.enumerations == []
    skip = _skips(result)["enumeration_source_unresolved"]
    assert skip.summary == (
        "跳过枚举公式清单(名称匹配到多个来源,无法确定是哪一个)")
    assert skip.detail["matches"] == 2
    # 内部 id 不得出现在轨迹里。
    assert "s2" not in json.dumps(skip.detail, ensure_ascii=False)
    assert "s3" not in json.dumps(skip.detail, ensure_ascii=False)


def test_source_id_wins_over_source_title(repo):
    """两个都给时以 id 为准:id 只会是服务端发出去的,不需要再按名字猜。"""
    notebook = _seed(repo, formulas=2)
    _add_titled_source(repo, notebook.id, "s2", "论文二", formulas=3)
    llm = _SeqLLM([_enumerate_action(source_id="s1", source_title="论文二"),
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)

    assert [outcome.source_id for outcome in result.enumerations] == ["s1"]
    assert {item.source_id for item in result.enumerations[0].items} == {"s1"}


def test_source_title_resolution_is_exact_not_fuzzy(repo):
    """解析是精确匹配(仅 trim + 大小写折叠),不是检索。

    模糊匹配会悄悄枚举另一篇文档并把它报成完整——比不解析更糟。
    """
    notebook = _seed(repo, formulas=2)
    _add_titled_source(repo, notebook.id, "s2", "Layout Basics.pdf", formulas=1)
    service = repo.collection_enumeration

    assert service.resolve_source_title(
        notebook.id, "formula", "layout basics.pdf") == ("s2", 1, False)
    assert service.resolve_source_title(
        notebook.id, "formula", "Layout Basics") == ("", 0, False)
    assert service.resolve_source_title(
        notebook.id, "formula", "  ") == ("", 0, False)


def test_truncated_title_plan_refuses_to_assert_uniqueness(repo, monkeypatch):
    """作用域内含该 kind 的源多到超过解析上限时,前缀里的唯一命中**不算**唯一
    ——同名的第二个源可能就在上限之后(codex 第 2 轮 P2)。

    这里刻意让被找的名字**落在前缀里**(计划按 id 排序,s1「论文一」是第一个,
    上限压到 1 后前缀恰好只有它):截断前它是干净的唯一命中,所以这条用例区分的
    正是「从前缀断言唯一」与「拒绝断言」。必须照常 skip 并标 truncated,而不是
    拿前缀的结论去枚举一个可能选错了的文档、再把它报成完整。
    """
    notebook = _seed(repo, formulas=2)
    _add_titled_source(repo, notebook.id, "s2", "论文二", formulas=1)
    # 上限压到 1:计划里有两个带公式的源,必然被判为「超上限」。
    monkeypatch.setattr(enum_module, "_MAX_TITLE_RESOLVE_SOURCES", 1)
    assert repo.collection_enumeration.resolve_source_title(
        notebook.id, "formula", "论文一") == ("", 0, True)

    llm = _SeqLLM([_enumerate_action(source_title="论文一"),
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)
    result = retriever.run(notebook.id, "《论文一》里有哪些公式", "", limits=limits)

    assert result.enumerations == []
    skip = _skips(result)["enumeration_source_unresolved"]
    assert skip.summary == (
        "跳过枚举公式清单(可按名称查找的来源太多,无法确定是哪一个)")
    assert skip.detail["truncated"] is True
    assert skip.detail["requested_title"] == "论文一"


def test_source_scoped_enumeration_is_a_separate_cursor_chain(repo):
    """限定单源与全作用域是两条链:混用会被执行器直接判成并发变更。"""
    notebook = _seed(repo, formulas=2)
    llm = _SeqLLM([_enumerate_action(), _enumerate_action(source_id="s1"),
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)

    assert [outcome.source_id for outcome in result.enumerations] == ["", "s1"]
    assert all(outcome.coverage.complete for outcome in result.enumerations)


# ------------------------------------------------------------------ 地图注入

def test_collection_map_is_built_once_and_injected_into_plan_and_reflect(repo):
    notebook = _seed(repo, formulas=3, tables=1)
    llm = _SeqLLM([{"next_action": "add_subquery",
                    "new_sub_query": {"query": "另一个角度"}},
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)
    builds: list = []
    real = retriever.collection_catalog

    class _CountingCatalog:
        def __getattr__(self, name):
            return getattr(real, name)

        def collection_map_text(self, notebook_id):
            builds.append(notebook_id)
            return real.collection_map_text(notebook_id)

    retriever.collection_catalog = _CountingCatalog()
    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)

    assert len(builds) == 1, "地图每 run 只建一次"
    assert llm.plan_prompts and "[Collections in scope]" in llm.plan_prompts[0]
    assert "formula 3" in llm.plan_prompts[0]
    assert llm.reflect_prompts, "reflect 必须真的被调用过"
    for prompt in llm.reflect_prompts:
        assert "[Collections in scope]" in prompt
        assert "formula 3" in prompt
    assert not _steps(result, "skip")


def test_collection_map_travels_out_on_the_result(repo):
    """地图必须随 ReasoningResult 带出——合成层要拿它,而重建一次既浪费查询,
    又可能与本 run 实际注入 reflect 的那份不是同一个。"""
    notebook = _seed(repo, formulas=3, tables=1)
    llm = _SeqLLM([{"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)
    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)
    assert result.collection_map_text.startswith("[Collections in scope]")
    assert "formula 3" in result.collection_map_text
    # 同一份字符串,不是第二次构建的近似物。
    assert result.collection_map_text in llm.reflect_prompts[0]


def test_collection_map_is_absent_when_the_tools_are_off(repo):
    notebook = _seed(repo, formulas=3)
    repo.settings.reasoning_enum_tools_enabled = False
    llm = _SeqLLM([{"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)
    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)
    assert result.collection_map_text == ""


def test_collection_map_failure_is_fail_open(repo):
    notebook = _seed(repo, formulas=1)
    llm = _SeqLLM([{"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)

    class _BrokenCatalog:
        def collection_map_text(self, notebook_id):
            raise RuntimeError("counting is down")

    retriever.collection_catalog = _BrokenCatalog()
    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)

    assert result.trace[-1].step_type == "answer", "地图失败不得中断整轮检索"
    skip = _skips(result)["collection_map_unavailable"]
    assert skip.summary == "跳过内容清点(暂时读不到各类条目数量)"
    assert "[Collections in scope]" not in llm.plan_prompts[0]


# ------------------------------------------------------------------ 总开关

def test_kill_switch_removes_tools_map_and_prompt_text(repo):
    notebook = _seed(repo, formulas=3)
    repo.settings.reasoning_enum_tools_enabled = False
    stub = _StubEnumeration([])
    llm = _SeqLLM([_enumerate_action()])
    retriever, limits = _retriever(repo, llm)
    retriever.collection_enumeration = stub

    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)

    assert stub.calls == [], "关闭态下执行器绝不能被调用"
    assert result.enumerations == []
    assert _steps(result, "enumerate") == []
    # 关闭态 = 完全回到接入前:没有地图、没有动作说明、schema 里没有 enumerate
    # 分支,模型硬吐这个动作也只能按既有未知动作合同退成 answer。
    assert "[Collections in scope]" not in llm.plan_prompts[0]
    assert "enumerate_elements" not in llm.reflect_prompts[0]
    assert all("enumerate" not in hint for hint in llm.schema_hints[1:])
    assert result.trace[-1].step_type == "answer"


def test_kill_switch_also_covers_the_sources_collection(repo):
    """来源清单不新增开关(design doc §6.2):同一把闸关掉后,即使模型硬吐
    `collection="sources"`,解析期就不看这个字段,执行器一次都不会被调用。"""
    notebook = _seed(repo, formulas=1)
    repo.settings.reasoning_enum_tools_enabled = False
    stub = _StubEnumeration([])
    llm = _SeqLLM([_enumerate_sources_action()])
    retriever, limits = _retriever(repo, llm)
    retriever.collection_enumeration = stub

    result = retriever.run(notebook.id, "有哪几篇", "", limits=limits)

    assert stub.calls == []
    assert result.enumerations == []
    assert _steps(result, "enumerate") == []
    assert '"collection"' not in llm.schema_hints[-1]
    assert result.trace[-1].step_type == "answer"


def test_authoring_profile_can_opt_out_without_touching_settings(repo):
    """knowhow 智能补全那条路径关的是 allow_enumeration,不是全局开关。"""
    notebook = _seed(repo, formulas=2)
    llm = _SeqLLM([_enumerate_action()])
    retriever, limits = _retriever(repo, llm)
    retriever.allow_enumeration = False
    assert retriever.enumeration_active() is False

    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)

    assert result.enumerations == []
    assert "[Collections in scope]" not in llm.plan_prompts[0]


def test_unwired_caller_keeps_the_previous_behavior(repo):
    """没接线集合服务的调用方(深度报告逐节深挖)与接入前逐字相同。"""
    retriever = ReasoningRetriever(
        retrieval=repo.retrieval,
        model_clients=repo,
        communities=repo.retrieval.community_queries(),
        settings=repo.settings,
    )
    assert retriever.enumeration_active() is False


# --------------------------------------------------------- 取消 / 账目 / 文案

def test_cancel_event_reaches_the_executor(repo):
    notebook = _seed(repo, formulas=1)
    listed = ElementEnumeration(
        kind="formula", items=(), cursor=None, extra_pages=0, payload_chars=0,
        coverage=_coverage(returned=0, returned_total=0, total=0),
    )
    stub = _StubEnumeration([listed])
    llm = _SeqLLM([_enumerate_action(), {"next_action": "answer",
                                         "sufficient": True}])
    bind_chat_client(repo, "reasoning_agent", llm)
    token = threading.Event()
    retriever = ReasoningRetriever.from_repository(
        repo, repo.settings, cancel_event=token
    )
    retriever.collection_enumeration = stub

    retriever.run(notebook.id, "哪些公式", "",
                  limits=ask_retrieval_limits("standard"))

    assert stub.calls[0]["cancel_event"] is token


def test_executor_cancellation_propagates_out_of_the_run(repo):
    notebook = _seed(repo, formulas=1)
    stub = _StubEnumeration([AskCancelled("stopped")])
    llm = _SeqLLM([_enumerate_action()])
    retriever, limits = _retriever(repo, llm)
    retriever.collection_enumeration = stub

    with pytest.raises(AskCancelled):
        retriever.run(notebook.id, "哪些公式", "", limits=limits)


def test_listed_items_count_as_progress_and_are_fed_back(repo):
    notebook = _seed(repo, formulas=3)
    llm = _SeqLLM([_enumerate_action(), {"next_action": "answer",
                                         "sufficient": True}])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)

    reflects = _steps(result, "reflect")
    assert len(reflects) == 2
    # 枚举出的条目不进 collected/elements,所以只有把它计入账目,这一轮才不会
    # 被判成「无进展」并推进 stale 熔断。
    assert reflects[1].detail["no_progress"] is False
    assert reflects[1].detail["stale"] == 0
    # 账目回喂:模型能看到自己已经列过什么、列全了没有。
    assert "「公式清单」已完整列出 3 条" in llm.reflect_prompts[1]
    assert "已完整列出的不要再请求" in llm.reflect_prompts[1]


def test_repeating_a_finished_collection_trips_the_stale_breaker(repo):
    """列全之后反复请求同一集合 → 连续无进展 → 熔断收尾,不空转到步数上限。

    这条守的是 ``before`` 账目里的 ``enum_rows_used``:它是**累计**值,只从
    ``before`` 一侧拿掉(留在事后比较里),``after != before`` 就会因为一次早已
    发生的枚举而恒成立,no_progress 永远为假、stale 永不递增,循环一路空转到
    max_steps。
    """
    notebook = _seed(repo, formulas=2)
    llm = _SeqLLM([_enumerate_action()] * 10)
    retriever, limits = _retriever(repo, llm)
    assert repo.settings.reasoning_stale_limit == 3

    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)

    # 第 1 轮列全(有进展),其后每轮都是 already_enumerated 跳过 → 3 轮熔断。
    assert len(_steps(result, "reflect")) == 4
    assert len(_steps(result, "enumerate")) == 1
    assert _skips(result)["stale_circuit_breaker"].detail["stale"] == 3
    assert len(_steps(result, "reflect")) < limits.max_reasoning_steps


def test_reflect_map_carries_the_remaining_listing_allowance(repo):
    """地图行末尾必须带本轮剩余行额度,并随消耗递减。

    prompt 让模型「按额度判断这个集合值不值得全量列」,却从不告诉它额度是多少,
    它就只能猜。地图主体仍每 run 只构建一次,这个后缀是每轮现拼的纯算术。
    """
    notebook = _seed(repo, formulas=3)
    llm = _SeqLLM([_enumerate_action(), {"next_action": "answer",
                                         "sufficient": True}])
    retriever, limits = _retriever(repo, llm)

    retriever.run(notebook.id, "哪些公式", "", limits=limits)

    assert f"listing allowance left: {limits.enum_rows_per_run} rows" in (
        llm.reflect_prompts[0])
    assert f"listing allowance left: {limits.enum_rows_per_run - 3} rows" in (
        llm.reflect_prompts[1])


def test_broken_effort_values_degrade_to_a_skip(repo):
    """档位被配坏(某个 enum_* 为 0)只废掉这一个动作,不掀翻整轮检索。

    EnumerationBudget 对非正上限直接 ValueError。它若从 run() 穿出去,会被
    ask_service 的 broad except 吞成「检索整体失败」,用户看到的是「依据不足」,
    而不是「这一个动作没跑成」。
    """
    notebook = _seed(repo, formulas=2)
    llm = _SeqLLM([_enumerate_action(),
                   {"next_action": "add_subquery",
                    "new_sub_query": {"query": "换个角度"}},
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(
        repo, llm, limits_overrides={"enum_page_size": 0}
    )

    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)

    assert result.enumerations == []
    assert "enumeration_rejected" in _skips(result)
    # 其余循环照常:后一个动作仍然执行,轨迹仍以合成候选收尾。
    assert any(step.detail.get("query") == "换个角度"
               for step in _steps(result, "retrieve"))
    assert result.trace[-1].step_type == "answer"


def test_empty_enumeration_is_no_progress(repo):
    notebook = _seed(repo, formulas=0, tables=0)
    llm = _SeqLLM([_enumerate_action(), {"next_action": "answer",
                                         "sufficient": True}])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)

    assert result.enumerations[0].items == []
    assert _steps(result, "reflect")[1].detail["no_progress"] is True


def test_trace_copy_stays_in_interface_vocabulary(repo):
    """上屏文案不得出现内部黑话(PR-1 那轮 codex 抓过 summary 里的「KG」)。"""
    notebook = _seed(repo, formulas=5)
    llm = _SeqLLM([
        _enumerate_action(),
        _enumerate_action(),
        _enumerate_action("paragraph"),
        {"next_action": "enumerate_kg_objects", "enumerate": {"object_type": "claim"}},
        {"next_action": "answer", "sufficient": True},
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "哪些公式", "", limits=limits)

    banned = ("KG", "chunk", "schema", "projection", "canonical", "tier",
              "element", "object_type", "cursor", "enumerate_", "coverage")
    surfaced = [step.summary for step in result.trace
                if step.step_type in ("enumerate", "skip")]
    assert len(surfaced) >= 3
    for summary in surfaced:
        for word in banned:
            assert word not in summary, summary


# ------------------------------------------- 地图计数进入答案合成上下文

class _AskLLM:
    """一路把 plan / reflect / evidence_refine / ask_answer 都接了的替身。

    reflect 立刻 answer:这正是「集合远大于本轮清单额度,别翻页,直接报数」
    那条 prompt 指令下模型该做的事——于是合成模型手里除了地图什么都没有。
    """

    configured = True
    model = "stub"

    def __init__(self):
        self.answer_prompts: list[str] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        content = messages[-1]["content"]
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "本库有多少公式"}]})
        if "next_action" in schema_hint:
            return json.dumps({"next_action": "answer", "sufficient": True})
        if '"relevant"' in schema_hint:
            return json.dumps({"relevant": []})
        self.answer_prompts.append(content)
        return json.dumps({"answer": "本库共有 40 个公式。", "grounded": False})


def _ask_with_stub(repo, notebook, llm):
    from app.models.schemas import AskRequest

    for service in ("reasoning_agent", "evidence_refine", "ask_answer"):
        bind_chat_client(repo, service, llm)
    return repo._runtime.ask_service().ask_reasoning(
        notebook.id,
        AskRequest(question="本库有多少公式", mode="reasoning"),
        user_id=repo.current_user().id,
    )


def test_collection_counts_reach_the_answer_synthesis_prompt(repo):
    """reflect prompt 教模型「集合太大就别枚举,用地图计数作答」,那么那个数
    必须真的到得了写答案的那次模型调用手里。

    这里刻意构造零其它证据(库里没有知识图谱、检索什么都没捞到、一条清单也
    没列):地图不进合成上下文的话,合成压根不会触发,用户拿到空答案。
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("s1", notebook.id, "手册", "pdf", "extracted", "extracted", NOW, NOW),
        )
        for index in range(40):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,metadata,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"el-{index:03d}", "s1", "formula", f"p{index}",
                 f"公式 {index}", "{}", NOW),
            )
    repo.collection_catalog.invalidate()

    llm = _AskLLM()
    response = _ask_with_stub(repo, notebook, llm)

    assert llm.answer_prompts, "零证据 + 有地图 时合成仍必须运行"
    prompt = llm.answer_prompts[0]
    assert "[Collections in scope]" in prompt
    assert "formula 40" in prompt
    # 服务端确定性输出:可无 [k] 引用,且 prompt 明说了这一点。
    assert "WITHOUT a [k] marker" in prompt
    assert response.answer


def test_collection_counts_survive_a_full_evidence_partition(repo):
    """地图排在 source 分区最前:预算再紧也不该先牺牲它。"""
    notebook = _seed(repo, formulas=6, tables=2)
    llm = _AskLLM()
    service = repo._runtime.ask_service()
    for name in ("reasoning_agent", "evidence_refine", "ask_answer"):
        bind_chat_client(repo, name, llm)
    from app.services.collection_enumeration_answer import collection_map_block

    block = collection_map_block(
        repo.collection_catalog.collection_map_text(notebook.id)
    )
    service._answer_reasoning(
        notebook.id, "多少公式", [], [],
        structured_block="S" * 5_000,
        collection_map_block=block,
        chunk_context_chars=len(block) + 200,
        kg_context_chars=100,
    )
    prompt = llm.answer_prompts[0]
    # 地图整块存活,而 5 000 字的 knowhow 块被同一个 chunk 预算削掉大半:
    # 装配顺序反过来的话,先满的是地图这一块。
    assert block in prompt
    assert prompt.index(block) < prompt.index("SSS")
    assert prompt.count("S") < 5_000

"""Agentic Memory P2 (A / T6):检索打法块注入 plan/reflect。

覆盖注入面本身:条目按当前情境选中并进规划与每一轮反思、注入闸关闭态的**冻结
基线**、两把开关互相独立(蒸馏开+注入关是默认形态)、打法块绝不进答案合成上下
文、store 故障 fail-open、整块字符硬预算按**整行**丢弃、采用回写只记模型真的选
过的动作,以及进程级 memo。

隐私与词表的静态判据在 ``test_retrieval_experience_privacy_guard``;蒸馏侧在
``test_retrieval_experience_job``;store 合同在 ``test_retrieval_experience_store``。
"""
from __future__ import annotations

import json

import pytest

from app.core.ask_retrieval_policy import ask_retrieval_limits
from app.core.config import Settings
from app.models.ask import (
    AskIntentConfirmation,
    QueryIntentContract,
    QueryIntentTopic,
)
from app.models.schemas import AskRequest, NotebookCreate
from app.repositories.ports import RETRIEVAL_EXPERIENCE_RATIONALE_MAX_CHARS
from app.services.embedding import FakeEmbedder
from app.services.reasoning_retrieval import (
    ReasoningResult,
    ReasoningRetriever,
    experience_wiring_active,
)
from app.services.retrieval_experience_block import (
    RETRIEVAL_EXPERIENCE_BLOCK_MAX_CHARS,
    RETRIEVAL_EXPERIENCE_INJECT_TOP_K,
    _GUIDANCE,
    render_experience_block,
    rendered_row_count,
    select_experiences,
)
from app.services.retrieval_experience_projection import (
    current_situation,
    experience_id,
)
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client


NOW = "2026-08-19T00:00:00+08:00"
HEADER = "[Search tactics learned from earlier runs]"

#: 与 ``ask_service`` 写进 intent 轨迹步的那份 detail 同形。注入侧只经
#: ``project_run_step`` 读它的封闭字段;原文键放在这里正是为了证明这一点。
#:
#: ⚠ 五个键刻意都取**非默认**值(`result_scope` 非 unknown、两个清单非空、两个
#: 布尔为真):它们正是「有没有把 intent_detail 传下来」这件事的判据。全取默认值
#: 的话,不传 detail 的情境与传了的只差一个键、相似度仍在地板之上,于是「漏传
#: intent_detail」这个变异不会让任何用例变红。
INTENT_DETAIL = {
    "resolved_question": "版图设计要点有哪些",
    "result_scope": "ranked",
    "completeness_required": False,
    "retrieval_effort": "standard",
    "entities": ["版图"],
    "constraints": ["先进工艺"],
    "excluded_topics": ["封装"],
    "assumptions": [],
    "expected_output": "要点清单",
    "mandatory_topics": ["版图设计要点"],
}

RATIONALE = "Inventory-shaped questions are answered by listing, not hunting."
OTHER_RATIONALE = "The graph walk rarely changes which candidates get cited."


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    # 同 test_agent_profile_injection 的隔离:本机 .env 里的真实推理端点会让这些
    # run 打真实网络。
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    instance.settings.graph_ppr_enabled = False
    instance.settings.retrieval_experience_inject_enabled = True
    _reset_memo()
    return instance


def _reset_memo():
    """进程级 memo 是模块级状态,用例之间必须清干净。

    不清的话,前一个用例的表快照(签名恰好相同的空表)会被下一个用例读到——而
    那正是「memo 是否真的按签名失效」这条用例要证明的事情。
    """
    from app.services import reasoning_retrieval

    with reasoning_retrieval._EXPERIENCE_CACHE_LOCK:
        reasoning_retrieval._EXPERIENCE_CACHE.clear()


def _seed(repo):
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
    repo.collection_catalog.invalidate()
    return notebook


def _confirmed_intent() -> AskIntentConfirmation:
    """一份**已确认**的意图契约,逐字段对上 ``INTENT_DETAIL``。

    端到端用例必须走这条路而不是让服务端自己兜底出一份空契约:空契约的情境与
    「压根没传 intent_detail」的情境只差一个键,漏传就测不出来(见上面那条用例
    的说明)。
    """
    contract = QueryIntentContract(
        # ``objective`` 必须逐字等于提交的问题:服务端冻结时会比对这一条。
        objective="版图设计要点",
        resolved_question=INTENT_DETAIL["resolved_question"],
        result_scope="ranked",
        completeness_required=False,
        entities=list(INTENT_DETAIL["entities"]),
        mandatory_topics=[
            QueryIntentTopic(id="t1", title="要点", question="版图设计要点")
        ],
        constraints=list(INTENT_DETAIL["constraints"]),
        excluded_topics=list(INTENT_DETAIL["excluded_topics"]),
        expected_output=INTENT_DETAIL["expected_output"],
        confirmed=True,
    )
    return AskIntentConfirmation(
        contract=contract,
        resolved_question=INTENT_DETAIL["resolved_question"],
    )


def _situation(**overrides) -> dict:
    situation = current_situation(
        INTENT_DETAIL, mode="reasoning", retrieval_effort="standard")
    situation.update(overrides)
    return situation


def _write(repo, action, polarity, rationale, *, situation=None, support=1):
    situation = situation or _situation()
    entry_id = experience_id(situation, action)
    repo.retrieval_experiences.upsert_experience(
        entry_id,
        situation=situation,
        action=action,
        polarity=polarity,
        rationale=rationale,
        provenance=[f"run-{support}-{action}"],
        provenance_max=60,
        replace_conclusion=True,
    )
    _reset_memo()
    return entry_id


class _SeqLLM:
    """plan 固定;reflect 按序列返回(耗尽后默认 answer)。记录每次 prompt 正文。"""

    configured = True

    def __init__(self, reflects=None, plan=None):
        self._plan = plan or {"sub_queries": [{"query": "版图设计要点"}]}
        self._reflects = list(reflects or [])
        self.plan_prompts: list[str] = []
        self.reflect_prompts: list[str] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        content = messages[-1]["content"]
        if "sub_queries" in schema_hint:
            self.plan_prompts.append(content)
            return json.dumps(self._plan)
        self.reflect_prompts.append(content)
        if self._reflects:
            return json.dumps(self._reflects.pop(0))
        return json.dumps({"next_action": "answer", "sufficient": True})


def _retriever(repo, llm, *, wired=True):
    """Ask 形态的 retriever(显式接线,与生产 ask_service 同形)。

    ``wired=False`` 是**接入前**的构造形状:``_construct_reasoning_retriever``
    刻意不接这个座位,所以 ``from_repository`` 出来的实例逐字就是本特性存在之前
    的那一个。冻结基线用它当对照物。
    """
    bind_chat_client(repo, "reasoning_agent", llm)
    retriever = ReasoningRetriever.from_repository(repo, repo.settings)
    if wired:
        retriever.retrieval_experiences = repo.retrieval_experiences
    return retriever, ask_retrieval_limits("standard")


def _run(retriever, notebook, limits, **kwargs):
    return retriever.run(notebook.id, "版图设计要点", "", limits=limits,
                         intent_detail=INTENT_DETAIL, **kwargs)


def _steps(result, step_type):
    return [step for step in result.trace if step.step_type == step_type]


def _trace_shape(result):
    """轨迹的可比较投影:耗时随机器波动,不进比较。"""
    return [(step.step_type, step.summary, step.detail) for step in result.trace]


# --------------------------------------------------------------- ① 注入本身

def test_the_block_reaches_plan_and_every_reflect_round(repo):
    notebook = _seed(repo)
    _write(repo, "enumerate", "good", RATIONALE)

    llm = _SeqLLM([{"next_action": "add_subquery",
                    "new_sub_query": {"query": "另一个角度"}},
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)
    result = _run(retriever, notebook, limits)

    assert llm.plan_prompts, "plan 必须真的被调用过"
    assert HEADER in llm.plan_prompts[0]
    assert RATIONALE in llm.plan_prompts[0]
    # 渲染的是模型能选的**动作 id**,不是存储词表那个拼写。
    assert "enumerate_*" in llm.plan_prompts[0]

    assert llm.reflect_prompts, "reflect 必须真的被调用过"
    for prompt in llm.reflect_prompts:
        assert HEADER in prompt
        assert RATIONALE in prompt

    steps = _steps(result, "experience")
    assert len(steps) == 1, "打法块每 run 只读一次"
    assert steps[0].summary == "带上以往检索攒下的打法"
    assert steps[0].detail == {"entries": 1, "chars": len(
        render_experience_block(select_experiences(
            repo.retrieval_experiences.read_all(300), _situation())))}


def test_an_entry_about_a_different_shape_of_question_is_not_injected(repo):
    """相似度地板:条目讲的是另一种形状的问题时不注入。

    没有地板的话,攒了几百条的部署每次都能找出「最接近」的三条,把一段针对别的
    问题形状的建议递给模型——而模型分辨不出这两者,那比不注入更糟。
    """
    notebook = _seed(repo)
    far = _situation(
        mode="chunk", result_scope="aggregate", retrieval_effort="exhaustive",
        completeness_required=True, entity_count="many", topic_count="many",
        has_constraints=True, has_exclusions=True)
    _write(repo, "enumerate", "good", RATIONALE, situation=far)

    llm = _SeqLLM()
    retriever, limits = _retriever(repo, llm)
    result = _run(retriever, notebook, limits)

    assert not _steps(result, "experience")
    assert HEADER not in llm.plan_prompts[0]


def test_without_the_intent_contract_the_situation_stops_matching(repo):
    """``intent_detail`` 不是可有可无的装饰:它是情境八个键里六个的唯一来源。

    不传时只剩 ``mode`` 与档位两个键有值,相似度掉到地板以下,同一条经验就选不
    中了。这条与下面的端到端用例是一对——那条证明 Ask 侧真的把它传了下来,这条
    证明「传不传」确实有区别(否则那条会在漏传时照样绿)。

    ⚠ 报告的逐节深挖**不再**走这条完全不传的路(T6 修复轮裁决 3):节的
    ``intent_contract`` 里持久化的 ``result_scope``/``completeness_required``
    现在会经 ``report_engine._deep_dive`` 喂进 ``intent_detail``,该行为的用例
    见 ``test_report_engine.py::test_deep_dive_feeds_current_situation_the_sections_own_persisted_scope``。
    这条用例证明的只是「完全不传 intent_detail」这一种输入形状本身的行为——
    ``ReasoningRetriever.run`` 仍然被任何调用方以这种形状调用时,情境仍然
    诚实地退化。
    """
    notebook = _seed(repo)
    _write(repo, "enumerate", "good", RATIONALE)

    llm = _SeqLLM()
    retriever, limits = _retriever(repo, llm)
    result = retriever.run(notebook.id, "版图设计要点", "", limits=limits)

    assert not _steps(result, "experience")
    assert HEADER not in llm.plan_prompts[0]


def test_an_empty_library_records_no_step_and_leaves_the_prompt_alone(repo):
    """还没蒸出任何东西的部署(常态)不落空步——与 P1 理解块同口径。"""
    notebook = _seed(repo)
    llm = _SeqLLM()
    retriever, limits = _retriever(repo, llm)
    result = _run(retriever, notebook, limits)

    assert not _steps(result, "experience")
    assert not _steps(result, "skip")
    assert HEADER not in llm.plan_prompts[0]


# --------------------------------------------------------------- ② 冻结基线

def test_the_inject_switch_is_byte_for_byte_the_pre_integration_run(repo):
    """注入闸关闭 ⇒ prompt 与轨迹**逐字**等于接入前。

    刻意让库里**真有**可注入的条目、只把开关关掉:如果关闭态与「库里本来是空的」
    共享同一条空路径,这个测试就证明不了 kill switch 本身。
    """
    notebook = _seed(repo)
    _write(repo, "enumerate", "good", RATIONALE)

    reflects = [{"next_action": "add_subquery",
                 "new_sub_query": {"query": "另一个角度"}},
                {"next_action": "answer", "sufficient": True}]

    repo.settings.retrieval_experience_inject_enabled = False
    off_llm = _SeqLLM(list(reflects))
    off_retriever, limits = _retriever(repo, off_llm)
    off_result = _run(off_retriever, notebook, limits)

    baseline_llm = _SeqLLM(list(reflects))
    baseline_retriever, _ = _retriever(repo, baseline_llm, wired=False)
    baseline_result = _run(baseline_retriever, notebook, limits)

    assert off_llm.plan_prompts == baseline_llm.plan_prompts
    assert off_llm.reflect_prompts == baseline_llm.reflect_prompts
    assert _trace_shape(off_result) == _trace_shape(baseline_result)
    assert not _steps(off_result, "experience")
    assert HEADER not in off_llm.plan_prompts[0]
    assert all(HEADER not in prompt for prompt in off_llm.reflect_prompts)


def test_the_two_switches_are_independent(repo):
    """蒸馏开、注入关 —— 这是**默认**形态,不是一个边角。

    两把闸共用一个判据的话,「先攒数据、观测好了再注入」这条本特性明确要支持的
    分期就表达不出来了。
    """
    settings = repo.settings
    settings.retrieval_experience_enabled = True
    settings.retrieval_experience_inject_enabled = False
    assert experience_wiring_active(settings, repo.retrieval_experiences) is False

    settings.retrieval_experience_inject_enabled = True
    assert experience_wiring_active(settings, repo.retrieval_experiences) is True
    assert experience_wiring_active(settings, None) is False


def test_prompt_functions_are_frozen_without_a_block():
    """空打法块 ⇒ 两份规划拼写的输出与不传这个参数逐字节相同。

    「关闭态回到接入前」在 prompts 层的判据:注入靠一个条件 section 拼进去,只要
    空串不产出 section,新参数就在字节层面不可见。两份拼写都要测——``plan_prompt``
    是备份拼写,production 走 ``expand_query_prompt``,而合同是「要到达规划模型的
    东西必须两份都加」。
    """
    from app.services.prompts import expand_query_prompt, plan_prompt

    collection_map = "[Collections in scope] elements: formula 3 | sources: 2"
    profile = "[What the agent knows about this library]\nx\n- corpus shape (shared): y"
    for history in ("", "user: 前一句"):
        for cmap in ("", collection_map):
            for prof in ("", profile):
                assert plan_prompt(
                    "q", history, cmap, prof
                ) == plan_prompt("q", history, cmap, prof, experience_block="")
                assert expand_query_prompt(
                    "q", history, True, 4, None, cmap, prof
                ) == expand_query_prompt(
                    "q", history, True, 4, None, cmap, prof, experience_block="")


def test_both_planning_spellings_render_the_block():
    """「要到达规划模型的东西必须两份拼写都加」——正向那一半。

    只测整轮 run 的话,``plan_prompt``(备份拼写,production 不走它)把注入删掉
    不会有任何测试变红,而它的存在理由恰恰是「两份绝不各说各的」。
    """
    from app.services.prompts import expand_query_prompt, plan_prompt

    block = render_experience_block([
        {"id": "rx_1", "situation": _situation(), "action": "enumerate",
         "polarity": "good", "rationale": RATIONALE, "support": 3},
    ])
    assert block
    assert block in plan_prompt("q", experience_block=block)
    assert block in expand_query_prompt("q", experience_block=block)


def test_knowhow_completion_path_is_untouched(repo):
    """``from_repository``(knowhow 智能补全走的那条路)不接打法库。"""
    retriever = ReasoningRetriever.from_repository(repo, repo.settings)
    assert retriever.retrieval_experiences is None


# ---------------------------------------------------- ③ 绝不进答案合成上下文

def test_the_block_never_travels_out_on_the_result(repo):
    """硬边界:``ReasoningResult`` 不带这个块,合成层因此拿不到它。

    与 P1 理解块同一条边界,理由也同:集合地图是**反例**(它刻意随 result 带出去
    进合成上下文),因为地图是可作答的计数,而打法是不可引用的背景。
    """
    notebook = _seed(repo)
    _write(repo, "enumerate", "good", RATIONALE)

    llm = _SeqLLM()
    retriever, limits = _retriever(repo, llm)
    result = _run(retriever, notebook, limits)

    assert _steps(result, "experience"), "前提:这一轮确实注入了打法块"
    field_names = set(ReasoningResult.__dataclass_fields__)
    assert not any("experience" in name for name in field_names), (
        "ReasoningResult 不得为打法块新增字段——那是它到达合成层的唯一可能路径"
    )
    for name in field_names:
        value = getattr(result, name)
        if isinstance(value, str):
            assert RATIONALE not in value
            assert HEADER not in value


# ------------------------------------------------------------- ④ fail-open

def test_store_failure_is_fail_open_and_records_no_step(repo):
    notebook = _seed(repo)
    llm = _SeqLLM()
    retriever, limits = _retriever(repo, llm)

    class _BrokenStore:
        def version_signal(self):
            raise RuntimeError("experience library is down")

        def read_all(self, limit):
            raise RuntimeError("experience library is down")

    retriever.retrieval_experiences = _BrokenStore()
    result = _run(retriever, notebook, limits)

    assert result.trace[-1].step_type == "answer", "读不到打法不得中断整轮检索"
    assert not _steps(result, "experience")
    assert not _steps(result, "skip")
    assert HEADER not in llm.plan_prompts[0]


def test_cancellation_during_the_read_still_propagates(repo):
    """fail-open 的 except 不得吞掉取消——取消是控制流,不是「读不到」。"""
    from app.services.cancellation import AskCancelled

    notebook = _seed(repo)
    llm = _SeqLLM()
    retriever, limits = _retriever(repo, llm)

    class _CancellingStore:
        def version_signal(self):
            raise AskCancelled("user pressed stop")

    retriever.retrieval_experiences = _CancellingStore()
    with pytest.raises(AskCancelled):
        _run(retriever, notebook, limits)


# ------------------------------------------------------------- ⑤ 字符预算

def test_rows_that_do_not_fit_are_dropped_whole_rather_than_clipped():
    """整块 600 字符硬顶按**整行**丢弃,不截断某一行的正文。

    与 P1 理解块刻意不同:那里的值是对一个库的描述,截掉尾巴仍然是描述;这里的
    一行是一条**建议**,截断后的建议读起来自信而完整,只是丢了限定语。
    """
    situation = _situation()
    long_rationale = "x" * 150
    entries = [
        {"id": f"rx_{index}", "situation": situation, "action": action,
         "polarity": "good", "rationale": f"{index}{long_rationale}", "support": 9}
        for index, action in enumerate(("enumerate", "ppr", "exact_lookup"))
    ]
    rendered = render_experience_block(entries)
    assert len(rendered) <= RETRIEVAL_EXPERIENCE_BLOCK_MAX_CHARS
    assert rendered_row_count(rendered) < len(entries), "至少一行装不下"
    assert not rendered.endswith("…"), "整块不做尾部截断"
    # 留下的每一行都是完整的:它的 rationale 一个字符不少。
    for line in rendered.splitlines():
        if not line.startswith("- "):
            continue
        assert long_rationale in line


def test_a_foreign_oversized_rationale_is_clipped_before_it_eats_the_budget():
    """写侧**拒绝**超长 rationale,但跨部署并集(``scripts/merge_dbs.py``)能把
    另一份代码写的行搬进来。渲染侧因此自己再夹一次:不夹的话,一行 900 字符就
    能把后面每一行的位置全部吃掉。

    这也是「先按行夹、再按整块丢行」这个顺序的理由——顺序反过来的话,超长的第
    一行会把其余的行整片挤掉,而那几行可能才是这次真正有用的。
    """
    situation = _situation()
    entries = [
        {"id": "rx_1", "situation": situation, "action": "enumerate",
         "polarity": "good", "rationale": "y" * 900, "support": 9},
        {"id": "rx_2", "situation": situation, "action": "ppr",
         "polarity": "bad", "rationale": OTHER_RATIONALE, "support": 8},
    ]
    rendered = render_experience_block(entries)
    assert len(rendered) <= RETRIEVAL_EXPERIENCE_BLOCK_MAX_CHARS
    assert "y" * RETRIEVAL_EXPERIENCE_RATIONALE_MAX_CHARS not in rendered
    assert OTHER_RATIONALE in rendered, "超长的第一行不得把后面的行挤掉"


def test_render_of_nothing_is_the_empty_string():
    """零行时给空串,而不是「表头 + 框定语 + 零行」——那种形态会白花两百多字符
    告诉模型「有经验,但一条都给不了你」。畸形行(词表外的动作)一样按零行处理。
    """
    assert render_experience_block([]) == ""
    assert render_experience_block([
        {"id": "rx_1", "situation": _situation(), "action": "read_everything",
         "polarity": "good", "rationale": RATIONALE, "support": 1},
    ]) == ""


def test_the_framing_sentence_survives_into_every_non_empty_block():
    """T6 修复轮裁决 4:整块非空时,``_GUIDANCE`` 那两句框定语
    ——「这是以往检索攒的打法,只用来选查法,不是证据」——必须逐字在场。

    模型只读渲染出的字符串,读不到这个模块的 docstring:框定语是这个特性唯一的
    纵深防御(它与「动作词表里没有范围类动作」共同构成红线,而红线的另一半是
    结构性的、这一半只能是文案),漏渲染它不会有任何异常或类型错误,只会安静地
    把一句「不是证据、不许改范围」的提醒从每一次 plan/reflect 调用里抹掉。
    """
    situation = _situation()
    rendered = render_experience_block([
        {"id": "rx_1", "situation": situation, "action": "enumerate",
         "polarity": "good", "rationale": RATIONALE, "support": 1},
    ])
    assert rendered
    # ``_GUIDANCE`` 本身就是两句合成的一行:句一讲「这是打法,不是证据」,
    # 句二讲「绝不能改变检索范围」——逐字断言整句,而不是断言某个关键词,免得
    # 后来的人把措辞改得面目全非却仍然踩线通过。
    assert _GUIDANCE in rendered
    assert "NOT evidence" in _GUIDANCE and "WHICH sources" in _GUIDANCE, (
        "前提断言:_GUIDANCE 真的包含证据与范围两句限定语,否则上面那条"
        "逐字断言就证明不了任何事"
    )


def test_at_most_top_k_entries_are_selected(repo):
    situation = _situation()
    entries = [
        {"id": f"rx_{index}", "situation": situation, "action": action,
         "polarity": "good", "rationale": f"r{index}", "support": index}
        for index, action in enumerate(
            ("enumerate", "ppr", "exact_lookup", "expand", "follow_chain"))
    ]
    selected = select_experiences(entries, situation)
    assert len(selected) == RETRIEVAL_EXPERIENCE_INJECT_TOP_K
    # 同相似度下按 support 降序 —— 更多 run 支撑的结论先上。
    assert [entry["support"] for entry in selected] == [4, 3, 2]


# ------------------------------------------------------------- ⑥ 采用回写

def test_only_the_entries_whose_action_the_model_chose_are_marked_adopted(repo):
    """``adopted`` 记的是「模型**主动选**了这个动作」。

    ⚠ 关键的一半是**反向**断言:``enumerate`` 那条也被注入了、模型一次都没选它,
    它的 ``adopted`` 必须保持 0。按 trace 的 step_type 反推会让每条被注入的经验
    都涨——初检索、PPR/精查 seed pass 都是确定性发生的——而 ``adopted`` 是淘汰
    排序的第一个键,那样它就退化成「谁被注入得多」。
    """
    notebook = _seed(repo)
    walked = _write(repo, "ppr", "good", OTHER_RATIONALE)
    listed = _write(repo, "enumerate", "good", RATIONALE)

    llm = _SeqLLM([{"next_action": "ppr_retrieve"},
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)
    result = _run(retriever, notebook, limits)

    assert _steps(result, "experience"), "前提:这一轮确实注入了打法"
    assert repo.retrieval_experiences.read_experience(walked)["adopted"] == 1
    assert repo.retrieval_experiences.read_experience(listed)["adopted"] == 0


def test_choosing_an_action_whose_entry_the_char_cap_dropped_is_not_adopted(repo):
    """T6 修复轮裁决 2:采用只能记**送达**集,不是块渲染前的 top-k 选中集。

    三条条目的 rationale 都顶格 ``RETRIEVAL_EXPERIENCE_RATIONALE_MAX_CHARS``
    (160 字符,评审给的复现形状):整块 600 字符硬顶下,这样的行只装得下 1 条,
    另外两条进了 ``select_experiences`` 的 top-k、却被 ``render_experience_block``
    整行丢弃——模型压根没看见它们。若模型选中的动作恰好映射到其中一条被丢弃的
    条目,修复前的代码用未切片的选中集判定,会把它错记成「模型采用了这条建议」;
    修复后必须保持 0,因为模型从未在 prompt 里读到它。
    """
    notebook = _seed(repo)
    rationale = "A" * RETRIEVAL_EXPERIENCE_RATIONALE_MAX_CHARS
    delivered_id = _write(repo, "enumerate", "good", rationale)
    dropped_id = _write(repo, "ppr", "good", rationale)
    _write(repo, "exact_lookup", "good", rationale)

    llm = _SeqLLM([{"next_action": "ppr_retrieve"},
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)
    result = _run(retriever, notebook, limits)

    steps = _steps(result, "experience")
    assert steps, "前提:这一轮确实注入了打法"
    # 前提断言:三条都顶格 160 字符,真的只送达 1 行——否则下面的「未送达」断言
    # 就没有验证到它本该验证的形状。
    assert steps[0].detail["entries"] == 1
    # 模型选中的是 ``ppr``(未送达的那条,见上面的前提断言),而不是真正送达的
    # ``enumerate`` ——两者的 ``adopted`` 都必须保持 0。
    assert repo.retrieval_experiences.read_experience(dropped_id)["adopted"] == 0
    assert repo.retrieval_experiences.read_experience(delivered_id)["adopted"] == 0


def test_a_bad_entry_the_model_ignores_is_not_adopted_even_if_it_picks_the_action(repo):
    """T6 修复轮裁决 2:``polarity == "bad"`` 的条目永远不能被计入采用。

    一条 ``bad`` 条目说的是「这个动作在这类问题上很少有用」——如果模型偏偏选了
    这个动作,那是模型在**无视**这条建议(该动作本就可能出于别的理由被选中),
    不是在采用它。把它计入 ``adopted`` 会奖励那些被无视的建议,与淘汰排序想
    保留的东西正好相反。
    """
    notebook = _seed(repo)
    ignored_id = _write(repo, "ppr", "bad", OTHER_RATIONALE)

    llm = _SeqLLM([{"next_action": "ppr_retrieve"},
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)
    result = _run(retriever, notebook, limits)

    steps = _steps(result, "experience")
    assert steps, "前提:这一轮确实注入了这条 bad 条目"
    assert steps[0].detail["entries"] == 1
    assert repo.retrieval_experiences.read_experience(ignored_id)["adopted"] == 0


def test_nothing_is_written_back_when_the_model_adopts_nothing(repo):
    """模型一路 answer ⇒ 零写入。默认关闭态自然也是零写入(上一条已覆盖闸)。"""
    notebook = _seed(repo)
    listed = _write(repo, "enumerate", "good", RATIONALE)

    llm = _SeqLLM()
    retriever, limits = _retriever(repo, llm)
    _run(retriever, notebook, limits)

    assert repo.retrieval_experiences.read_experience(listed)["adopted"] == 0


def test_a_failing_adoption_write_does_not_break_the_run(repo):
    """采用回写 fail-open:``adopted`` 只是淘汰排序的一个键,一次记账失败不该
    让一次已经检索完的 run 报错。"""
    notebook = _seed(repo)
    _write(repo, "ppr", "good", OTHER_RATIONALE)

    llm = _SeqLLM([{"next_action": "ppr_retrieve"},
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm)
    real = repo.retrieval_experiences

    class _WriteFails:
        def version_signal(self):
            return real.version_signal()

        def read_all(self, limit):
            return real.read_all(limit)

        def note_adopted(self, ids, delta=1):
            raise RuntimeError("write path is down")

    retriever.retrieval_experiences = _WriteFails()
    result = _run(retriever, notebook, limits)
    assert result.trace[-1].step_type == "answer"


# ------------------------------------------------------------- ⑦ 进程级 memo

def test_the_library_is_read_once_per_version_not_once_per_run(repo):
    """memo:表没变时第二次 run 不再重读全表。

    它换掉的是「至多 300 行 + 每行两次 JSON 反序列化 + 逐行打分」,留下的是一次
    聚合查询。写入之后必须**立刻**失效——刻意不做 TTL,不然刚蒸出来的条目要等一
    段时间才生效。
    """
    notebook = _seed(repo)
    _write(repo, "enumerate", "good", RATIONALE)
    real = repo.retrieval_experiences

    class _Counting:
        def __init__(self):
            self.reads = 0

        def version_signal(self):
            return real.version_signal()

        def read_all(self, limit):
            self.reads += 1
            return real.read_all(limit)

        def note_adopted(self, ids, delta=1):
            return real.note_adopted(ids, delta)

    counting = _Counting()
    for _ in range(3):
        llm = _SeqLLM()
        retriever, limits = _retriever(repo, llm)
        retriever.retrieval_experiences = counting
        result = _run(retriever, notebook, limits)
        assert _steps(result, "experience")
    assert counting.reads == 1, "同一份表只该读一次"

    # 新条目落库 ⇒ 签名变 ⇒ 下一次 run 重读。
    _write(repo, "ppr", "bad", OTHER_RATIONALE)
    llm = _SeqLLM()
    retriever, limits = _retriever(repo, llm)
    retriever.retrieval_experiences = counting
    result = _run(retriever, notebook, limits)
    assert counting.reads == 2
    assert _steps(result, "experience")[0].detail["entries"] == 2


# --------------------------------------------------------------- ⑧ 接线座位

def test_the_ask_engine_factory_actually_fills_the_seat(repo):
    """runtime 组装出的 ``AskService`` 拿到的座位 is repository 的那一个——
    座位空着的话整条特性在 Ask 里是死的(镜像 P1 那条同名用例)。"""
    service = repo._runtime.ask_service()
    assert service.retrieval_experiences is repo.retrieval_experiences


def test_ask_reasoning_end_to_end_injects_the_block(repo):
    """端到端:走 ``AskService.ask_reasoning``(而不是直接构造
    ``ReasoningRetriever``)钉住 runtime 装进座位的 store 真的被消费,而且
    ``intent_detail`` 真的从 Ask 侧传了下来——不传的话情境会退成全 unknown,
    条目按相似度地板就选不中,这条会红。
    """
    notebook = _seed(repo)
    _write(repo, "enumerate", "good", RATIONALE)

    class _AskEndToEndLLM:
        configured = True

        def __init__(self):
            self.plan_prompts: list[str] = []
            self.reflect_prompts: list[str] = []

        def chat_json(self, messages, schema_hint, **kwargs):
            content = messages[-1]["content"]
            if "sub_queries" in schema_hint:
                self.plan_prompts.append(content)
                return json.dumps({"sub_queries": [{"query": "版图设计要点"}]})
            if "next_action" in schema_hint:
                self.reflect_prompts.append(content)
                return json.dumps({"next_action": "answer", "sufficient": True})
            if "relevant" in schema_hint:
                return json.dumps({"relevant": []})
            return json.dumps({"answer": "见资料 [k1].", "grounded": True})

    llm = _AskEndToEndLLM()
    for workload_id in ("reasoning_agent", "evidence_refine", "ask_answer"):
        bind_chat_client(repo, workload_id, llm)

    service = repo._runtime.ask_service()
    response = service.ask_reasoning(
        notebook.id,
        AskRequest(question="版图设计要点", mode="reasoning",
                   intent=_confirmed_intent()),
        user_id="alice",
    )

    assert response.reasoning_trace
    assert [
        step for step in response.reasoning_trace if step.step_type == "experience"
    ], "打法块必须真的注入到这次 Ask 的轨迹里"
    # ⚠ 这里**没有** plan prompt 可断言,那不是漏测:已确认意图的方向就是权威
    # 种子,run 不会再调一次规划模型(与 P1 报告侧同一条既有行为)。所以生产上
    # 这条路的落点是 reflect,断言也就落在那里。
    assert not llm.plan_prompts, "已确认方向 ⇒ 不再调规划模型"
    assert llm.reflect_prompts, "reflect 必须真的被调用过"
    for prompt in llm.reflect_prompts:
        assert HEADER in prompt
        assert RATIONALE in prompt


def test_the_report_engine_factory_actually_fills_the_seat(repo):
    """报告侧同款座位断言。报告的逐节深挖是本特性第二个(也是最后一个)消费点,
    而报告引擎没有任何自动贯通的路——座位漏填不会报错,只会永远不注入。"""
    from app.services.report_engine import ReportEngine

    engine = ReportEngine.from_repository(repo, repo.settings)
    assert (
        engine.dependencies.retrieval_experiences is repo.retrieval_experiences
    )


def test_the_memo_never_serves_another_stores_entries():
    """codex #524 R7 P2:同进程两个 runtime 的 store 可能给出相同 (行数, 时间)
    签名——缓存键必须含 store 身份,否则 A 库的打法注进 B 库的 run。"""
    from app.services.reasoning_retrieval import _cached_experiences

    _reset_memo()

    class _FixedStore:
        def __init__(self, rationale):
            self._rows = [{
                "id": f"rx-{rationale}", "action": "ppr", "polarity": "good",
                "rationale": rationale, "support": 1, "adopted": 0,
                "situation": _situation(),
            }]

        def version_signal(self):
            return (1, "2026-08-19T00:00:00+00:00")   # 两个 store 刻意同签名

        def read_all(self, limit):
            return list(self._rows)

    a = _FixedStore("A 库的打法")
    b = _FixedStore("B 库的打法")
    assert _cached_experiences(a)[0]["rationale"] == "A 库的打法"
    assert _cached_experiences(b)[0]["rationale"] == "B 库的打法"

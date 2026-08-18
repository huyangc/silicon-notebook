"""Agentic Memory P1 (T3):Agent 对这个库的已有理解注入 plan/reflect。

覆盖注入面本身:共享底座 + 本人覆盖层进规划与每一轮反思、别人的覆盖层拿不到、
总开关关闭态的**冻结基线**、理解块绝不进答案合成上下文、store 故障 fail-open、
字符硬预算截断,以及来源范围收窄时仍然注入(与集合地图刻意不同的口径)。

块渲染本身的合同在 ``render_profile_block`` 的单测部分(本文件下半),store 的
合同在 ``test_agent_profile_store``。
"""
from __future__ import annotations

import json

import pytest

from app.core.ask_retrieval_policy import ask_retrieval_limits
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.models.source_scope import SourceScope
from app.services.agent_profile_block import (
    AGENT_PROFILE_BLOCK_MAX_CHARS,
    AGENT_PROFILE_VALUE_MAX_CHARS,
    render_profile_block,
    selected_profile_blocks,
)
from app.services.embedding import FakeEmbedder
from app.services.reasoning_retrieval import (
    ReasoningResult,
    ReasoningRetriever,
    profile_wiring_active,
)
from app.services.source_scope import source_scope_context
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client


NOW = "2026-08-18T00:00:00+08:00"
HEADER = "[What the agent knows about this library]"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    # 同 test_reasoning_enumeration_tools 的隔离:本机 .env 里的真实推理端点会让
    # 这些 run 打真实网络。
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    instance.settings.graph_ppr_enabled = False
    return instance


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


def _write(repo, notebook_id, owner_id, label, value):
    return repo.agent_profile.write_block(
        notebook_id, owner_id, label,
        value=value, evidence=[], expected_revision=0,
        origin="job", actor="",
    )


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


def _retriever(repo, llm, *, owner_id="", wired=True):
    """构造一个 Ask 形态的 retriever(显式接线,与生产 ask_service 同形)。

    ``wired=False`` 是**接入前**的构造形状:``_construct_reasoning_retriever``
    刻意不接这两个参数,所以 ``from_repository`` 出来的实例逐字就是本特性存在
    之前的那一个。冻结基线用它当对照物。
    """
    bind_chat_client(repo, "reasoning_agent", llm)
    retriever = ReasoningRetriever.from_repository(repo, repo.settings)
    if wired:
        retriever.agent_profile = repo.agent_profile
        retriever.profile_owner_id = owner_id
    return retriever, ask_retrieval_limits("standard")


def _steps(result, step_type):
    return [step for step in result.trace if step.step_type == step_type]


def _trace_shape(result):
    """轨迹的可比较投影:耗时随机器波动,不进比较。"""
    return [(step.step_type, step.summary, step.detail) for step in result.trace]


# --------------------------------------------------------------- ① 注入与隔离

def test_base_and_own_overlay_reach_plan_and_reflect_but_not_another_users(repo):
    notebook = _seed(repo)
    _write(repo, notebook.id, "", "corpus_shape", "以模拟版图手册为主")
    _write(repo, notebook.id, "u1", "retrieval_notes", "这位成员常问时序收敛")
    _write(repo, notebook.id, "u2", "retrieval_notes", "别人的私有心得BBBB")

    llm = _SeqLLM([{"next_action": "add_subquery",
                    "new_sub_query": {"query": "另一个角度"}},
                   {"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(repo, llm, owner_id="u1")
    result = retriever.run(notebook.id, "版图设计要点", "", limits=limits)

    assert llm.plan_prompts, "plan 必须真的被调用过"
    plan_prompt = llm.plan_prompts[0]
    assert HEADER in plan_prompt
    assert "以模拟版图手册为主" in plan_prompt
    assert "这位成员常问时序收敛" in plan_prompt
    # 隐私边界:另一位成员的覆盖层在 store 的 SQL 谓词里就被挡住了。
    assert "别人的私有心得BBBB" not in plan_prompt
    assert "(shared)" in plan_prompt and "(yours)" in plan_prompt

    assert llm.reflect_prompts, "reflect 必须真的被调用过"
    for prompt in llm.reflect_prompts:
        assert HEADER in prompt
        assert "以模拟版图手册为主" in prompt
        assert "别人的私有心得BBBB" not in prompt

    steps = _steps(result, "profile")
    assert len(steps) == 1, "理解块每 run 只读一次"
    assert steps[0].summary == "带上对这个库的已有理解"
    assert steps[0].detail["blocks"] == 2
    assert steps[0].detail["chars"] > 0


def test_empty_owner_id_injects_only_the_shared_base(repo):
    """空 owner(后台/未登录形态)= 只注入底座,不碰任何人的覆盖层。"""
    notebook = _seed(repo)
    _write(repo, notebook.id, "", "corpus_shape", "共享底座内容")
    _write(repo, notebook.id, "u1", "retrieval_notes", "某成员的心得CCCC")

    llm = _SeqLLM()
    retriever, limits = _retriever(repo, llm, owner_id="")
    result = retriever.run(notebook.id, "版图设计要点", "", limits=limits)

    assert "共享底座内容" in llm.plan_prompts[0]
    assert "某成员的心得CCCC" not in llm.plan_prompts[0]
    assert _steps(result, "profile")[0].detail["blocks"] == 1


def test_no_profile_yet_records_no_step_and_leaves_the_prompt_alone(repo):
    """还没整理过的库(常态)不落空步——这与 memory 零命中记 skip 的口径分开。"""
    notebook = _seed(repo)
    llm = _SeqLLM()
    retriever, limits = _retriever(repo, llm, owner_id="u1")
    result = retriever.run(notebook.id, "版图设计要点", "", limits=limits)

    assert not _steps(result, "profile")
    assert not _steps(result, "skip")
    assert HEADER not in llm.plan_prompts[0]


def test_cleared_blocks_keep_their_row_but_do_not_reach_the_prompt(repo):
    """``clear_block`` 保留行、清空值:读回来是 5 行,进 prompt 的是 0 条。"""
    notebook = _seed(repo)
    block = _write(repo, notebook.id, "", "corpus_shape", "先写点什么")
    repo.agent_profile.clear_block(
        notebook.id, "", "corpus_shape",
        expected_revision=block["revision"], actor="u1")

    llm = _SeqLLM()
    retriever, limits = _retriever(repo, llm, owner_id="u1")
    result = retriever.run(notebook.id, "版图设计要点", "", limits=limits)

    assert repo.agent_profile.read_blocks(notebook.id, "u1"), "行仍在"
    assert not _steps(result, "profile")
    assert HEADER not in llm.plan_prompts[0]


# --------------------------------------------------------------- ② 冻结基线

def test_kill_switch_is_byte_for_byte_the_pre_integration_run(repo):
    """总开关关闭 ⇒ prompt 与轨迹**逐字**等于接入前。

    对照物构造:``_construct_reasoning_retriever``(``from_repository`` 走的那条
    路)刻意不接 ``agent_profile``/``profile_owner_id``,所以 ``wired=False`` 的
    实例执行的就是本特性存在之前的那份代码路径 —— 它是「接入前」在本进程里可
    直接跑出来的形态,不需要 git stash。

    这里刻意让**理解块非空**(库里真有可注入的内容)、只把开关关掉:如果关闭态
    与「库里本来就没有理解」共享同一条空路径,这个测试就证明不了 kill switch 本身。

    prompts 层还另有一道更强的证据:``profile_block=""`` 时
    ``plan_prompt``/``expand_query_prompt`` 的输出与 HEAD 版本逐字节相同(实测
    84 组入参零差异)。那一半在 ``test_prompt_functions_are_frozen_without_a_block``
    里以进程内可跑的形式钉住。
    """
    notebook = _seed(repo)
    _write(repo, notebook.id, "", "corpus_shape", "以模拟版图手册为主")
    _write(repo, notebook.id, "u1", "retrieval_notes", "这位成员常问时序收敛")

    reflects = [{"next_action": "add_subquery",
                 "new_sub_query": {"query": "另一个角度"}},
                {"next_action": "answer", "sufficient": True}]

    repo.settings.agent_profile_enabled = False
    off_llm = _SeqLLM(list(reflects))
    off_retriever, limits = _retriever(repo, off_llm, owner_id="u1")
    off_result = off_retriever.run(notebook.id, "版图设计要点", "", limits=limits)

    baseline_llm = _SeqLLM(list(reflects))
    baseline_retriever, _ = _retriever(repo, baseline_llm, wired=False)
    baseline_result = baseline_retriever.run(
        notebook.id, "版图设计要点", "", limits=limits)

    assert off_llm.plan_prompts == baseline_llm.plan_prompts
    assert off_llm.reflect_prompts == baseline_llm.reflect_prompts
    assert _trace_shape(off_result) == _trace_shape(baseline_result)
    assert not _steps(off_result, "profile")
    assert HEADER not in off_llm.plan_prompts[0]
    assert all(HEADER not in prompt for prompt in off_llm.reflect_prompts)


def test_prompt_functions_are_frozen_without_a_block():
    """空理解块 ⇒ 两份规划拼写的输出与不传这个参数逐字节相同。

    这是「关闭态回到接入前」在 prompts 层的判据:注入是靠一个条件 section 拼进
    去的,只要空串不产出 section,新参数就在字节层面不可见。两份拼写都要测——
    ``plan_prompt`` 是备份拼写,production 走 ``expand_query_prompt``,而合同是
    「要到达规划模型的东西必须两份都加」。
    """
    from app.services.prompts import expand_query_prompt, plan_prompt

    collection_map = "[Collections in scope] elements: formula 3 | sources: 2"
    for history in ("", "user: 前一句"):
        for cmap in ("", collection_map):
            assert plan_prompt("q", history, cmap) == plan_prompt(
                "q", history, cmap, profile_block="")
            for want_types in (True, False):
                assert expand_query_prompt(
                    "q", history, want_types, 4, None, cmap
                ) == expand_query_prompt(
                    "q", history, want_types, 4, None, cmap, profile_block="")


def test_both_planning_spellings_render_the_block():
    """「要到达规划模型的东西必须两份拼写都加」——正向那一半。

    ⚠ 这条是**变异验证驱动**加的:只测整轮 run 的话,``plan_prompt``(备份拼写,
    production 不走它)把注入删掉不会有任何测试变红,而它的存在理由恰恰是「两份
    绝不各说各的」。冻结用例只证明空串时两份都不变,证明不了非空时两份都带。
    """
    from app.services.prompts import expand_query_prompt, plan_prompt

    block = render_profile_block(
        [{"label": "corpus_shape", "owner_id": "", "value": "以工艺手册为主"}])
    assert block
    assert block in plan_prompt("q", profile_block=block)
    assert block in expand_query_prompt("q", profile_block=block)


def test_the_gate_is_one_predicate(repo):
    """总闸单点判定:开关关、或 store 没接线,都等于「不可用」。"""
    settings = repo.settings
    settings.agent_profile_enabled = True
    assert profile_wiring_active(settings, repo.agent_profile) is True
    assert profile_wiring_active(settings, None) is False
    settings.agent_profile_enabled = False
    assert profile_wiring_active(settings, repo.agent_profile) is False


def test_knowhow_completion_path_is_untouched(repo):
    """``from_repository``(knowhow 智能补全走的那条路)不接理解块。"""
    retriever = ReasoningRetriever.from_repository(repo, repo.settings)
    assert retriever.agent_profile is None
    assert retriever.profile_owner_id == ""


# ---------------------------------------------------- ③ 绝不进答案合成上下文

def test_the_block_never_travels_out_on_the_result(repo):
    """硬边界(设计 §5.2):``ReasoningResult`` 不带这个块,合成层因此拿不到它。

    集合地图是**反例**——它刻意随 result 带出去进合成上下文。两者的差别正是本
    条要钉住的:地图是计数(可作答的事实),理解块是印象(不可引用的背景)。
    """
    notebook = _seed(repo)
    _write(repo, notebook.id, "", "corpus_shape", "以模拟版图手册为主DDDD")

    llm = _SeqLLM()
    retriever, limits = _retriever(repo, llm, owner_id="u1")
    result = retriever.run(notebook.id, "版图设计要点", "", limits=limits)

    assert _steps(result, "profile"), "前提:这一轮确实注入了理解块"
    field_names = set(ReasoningResult.__dataclass_fields__)
    assert not any("profile" in name for name in field_names), (
        "ReasoningResult 不得为理解块新增字段——那是它到达合成层的唯一可能路径"
    )
    # 地图仍照常带出(反例对照),而理解块的正文不出现在 result 的任何字符串字段。
    assert result.collection_map_text.startswith("[Collections in scope]")
    for name in field_names:
        value = getattr(result, name)
        if isinstance(value, str):
            assert "以模拟版图手册为主DDDD" not in value
            assert HEADER not in value


# ------------------------------------------------------------- ④ fail-open

def test_store_failure_is_fail_open_and_records_no_step(repo):
    notebook = _seed(repo)
    llm = _SeqLLM()
    retriever, limits = _retriever(repo, llm, owner_id="u1")

    class _BrokenStore:
        def read_blocks(self, notebook_id, owner_id):
            raise RuntimeError("understanding store is down")

    retriever.agent_profile = _BrokenStore()
    result = retriever.run(notebook.id, "版图设计要点", "", limits=limits)

    assert result.trace[-1].step_type == "answer", "读不到理解不得中断整轮检索"
    assert not _steps(result, "profile")
    # 异常分支同样不记 skip:见 run() 注释,这是一次亚毫秒点查,没有耗时要交代。
    assert not _steps(result, "skip")
    assert HEADER not in llm.plan_prompts[0]


def test_cancellation_during_the_read_still_propagates(repo):
    """fail-open 的 except 不得吞掉取消——取消是控制流,不是「读不到」。"""
    from app.services.cancellation import AskCancelled

    notebook = _seed(repo)
    llm = _SeqLLM()
    retriever, limits = _retriever(repo, llm, owner_id="u1")

    class _CancellingStore:
        def read_blocks(self, notebook_id, owner_id):
            raise AskCancelled("user pressed stop")

    retriever.agent_profile = _CancellingStore()
    with pytest.raises(AskCancelled):
        retriever.run(notebook.id, "版图设计要点", "", limits=limits)


# ------------------------------------------------------------- ⑤ 字符预算

def test_the_block_is_hard_capped(repo):
    notebook = _seed(repo)
    # 五块各写超长内容:单块 400 的截断加起来仍会顶穿整块 1200 的硬顶。
    for index, label in enumerate((
            "corpus_shape", "key_entities", "corpus_gaps",
            "retrieval_notes", "usage_gaps")):
        _write(repo, notebook.id, "", label, f"{index}" * 900)

    llm = _SeqLLM()
    retriever, limits = _retriever(repo, llm, owner_id="u1")
    result = retriever.run(notebook.id, "版图设计要点", "", limits=limits)

    step = _steps(result, "profile")[0]
    assert step.detail["chars"] == AGENT_PROFILE_BLOCK_MAX_CHARS
    rendered = render_profile_block(
        repo.agent_profile.read_blocks(notebook.id, "u1"))
    assert len(rendered) == AGENT_PROFILE_BLOCK_MAX_CHARS
    assert rendered.endswith("…")
    assert rendered in llm.plan_prompts[0]


def test_one_oversized_block_is_clamped_before_the_whole_block_cap():
    """单块先各自截到 400,再整体截到 1200。

    顺序是重要的:只做整块截断的话,第一块超长就会把其余四块整片挤掉,而那四块
    可能才是这次真正有用的那几条。
    """
    blocks = [
        {"label": "corpus_shape", "owner_id": "", "value": "甲" * 900},
        {"label": "key_entities", "owner_id": "", "value": "乙短"},
    ]
    rendered = render_profile_block(blocks)
    assert len(rendered) < AGENT_PROFILE_BLOCK_MAX_CHARS
    assert "甲" * AGENT_PROFILE_VALUE_MAX_CHARS not in rendered
    assert "乙短" in rendered, "超长的第一块不得把后面的块挤掉"


# ------------------------------------------------------- ⑥ 来源范围收窄

def test_narrowed_source_scope_still_injects(repo):
    """收窄来源范围时**仍然注入**——与集合地图被清空的处理刻意不同。

    地图之所以要清,是因为它承诺了一批本次枚举不到的集合;理解块不开任何通道、
    不是证据、不能被引用,收窄来源并不会让「这个库主要是工艺手册」变得不成立。
    """
    notebook = _seed(repo)
    _write(repo, notebook.id, "", "corpus_shape", "以模拟版图手册为主EEEE")

    llm = _SeqLLM()
    retriever, limits = _retriever(repo, llm, owner_id="u1")
    with source_scope_context(
        notebook.id,
        SourceScope(mode="include", source_ids=["s1"], narrowed=True),
    ):
        result = retriever.run(notebook.id, "版图设计要点", "", limits=limits)

    assert _steps(result, "profile"), "收窄时理解块仍须注入"
    assert "以模拟版图手册为主EEEE" in llm.plan_prompts[0]
    # 对照:同一次 run 里集合地图被清空(它承诺的集合这次枚举不到)。
    assert result.collection_map_text == ""


# --------------------------------------------------------------- 渲染单测

def test_render_orders_labels_and_marks_provenance():
    blocks = [
        {"label": "usage_gaps", "owner_id": "u1", "value": "常问但答不好的"},
        {"label": "key_entities", "owner_id": "", "value": "ADC、PLL"},
        {"label": "corpus_shape", "owner_id": "", "value": "手册为主"},
    ]
    rendered = render_profile_block(blocks)
    lines = rendered.splitlines()
    assert lines[0] == HEADER
    assert [line.split(" (")[0] for line in lines[2:]] == [
        "- corpus shape", "- key entities", "- usage gaps",
    ]
    assert "(shared)" in lines[2] and "(yours)" in lines[4]


def test_render_drops_unknown_labels_and_empty_values():
    blocks = [
        {"label": "corpus_shape", "owner_id": "", "value": "  "},
        {"label": "brand_new_label", "owner_id": "", "value": "不该上屏"},
        {"label": "key_entities", "owner_id": "", "value": "ADC"},
    ]
    assert len(selected_profile_blocks(blocks)) == 1
    rendered = render_profile_block(blocks)
    assert "不该上屏" not in rendered
    assert "ADC" in rendered


def test_render_of_nothing_is_the_empty_string():
    assert render_profile_block([]) == ""
    assert render_profile_block(
        [{"label": "corpus_shape", "owner_id": "", "value": ""}]) == ""

"""Agentic Memory P1 (T3):深度报告逐节深挖也带上 Agent 的已有理解。

报告侧的接线是**显式**的,不是顺带贯通的:``_deep_dive`` 的
``ReasoningRetriever(...)`` 只传四个端口(集合枚举也刻意不传),没有任何一条
路会自动把这个 store 带进去。所以这里要钉三件事——engine factory 真的填了座位、
理解块真的到达节深挖的模型 prompt、以及 owner 取的是**报告创建者**而不是别人。

v1 的注入面只有逐节检索:意图理解与大纲规划两阶段刻意不接(设计 §12-Q5)。
"""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.report_engine import ReportEngine
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client


HEADER = "[What the agent knows about this library]"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL", "REWRITE_LLM_API_KEY",
                "REWRITE_LLM_BASE_URL", "REWRITE_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    instance.settings.graph_ppr_enabled = False
    return instance


def _notebook(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(notebook.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "版图设计要点", "section_path": "1"}, "evidence": []},
    ], [])
    return notebook


class _SectionLLM:
    """节深挖用的脚本模型:留档每一次 reflect / plan prompt 正文。"""

    configured = True

    def __init__(self):
        self.reflect_prompts: list[str] = []
        self.plan_prompts: list[str] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        content = messages[-1]["content"]
        if "sub_queries" in schema_hint:
            self.plan_prompts.append(content)
            return json.dumps({"sub_queries": [{"query": "版图设计要点"}]})
        if "next_action" in schema_hint:
            self.reflect_prompts.append(content)
            return json.dumps({"next_action": "answer", "sufficient": True})
        return json.dumps({})


def _engine(repo, llm):
    for workload_id in ("report_outline", "report_sufficiency", "report_section",
                        "report_summary", "query_rewrite", "reasoning_agent",
                        "ask_answer"):
        bind_chat_client(repo, workload_id, llm)
    return ReportEngine.from_repository(repo, repo.settings)


def _write(repo, notebook_id, owner_id, label, value):
    repo.agent_profile.write_block(
        notebook_id, owner_id, label,
        value=value, evidence=[], expected_revision=0, origin="job", actor="")


def test_the_engine_factory_actually_fills_the_seat(repo):
    """「零额外接线」在当前代码形态下不成立:座位空着的话整条特性在报告里是死的。"""
    engine = _engine(repo, _SectionLLM())
    assert engine.dependencies.agent_profile is repo.agent_profile


def test_deep_dive_prompts_carry_the_understanding_of_the_report_creator(repo):
    """节深挖的 reflect prompt 带底座 + **报告创建者**的覆盖层,不带别人的。

    这里刻意走带 ``sub_queries`` 的真实形态:方向非空 ⇒ run 采用已确认方向作
    首轮种子并跳过 plan LLM 调用,所以报告侧理解块的落点是 reflect 而不是 plan
    (那正是生产上会发生的事)。
    """
    notebook = _notebook(repo)
    creator = repo.current_user().id
    _write(repo, notebook.id, "", "corpus_shape", "以工艺手册为主AAAA")
    _write(repo, notebook.id, creator, "retrieval_notes", "创建者的心得BBBB")
    _write(repo, notebook.id, "someone-else", "retrieval_notes", "别人的心得CCCC")

    llm = _SectionLLM()
    engine = _engine(repo, llm)
    assert engine.user_id == creator

    engine._deep_dive(
        notebook.id,
        {"title": "A", "scope": "s", "sub_queries": ["版图设计要点"]},
        "报告问题", depth=2)

    assert llm.reflect_prompts, "节深挖必须真的走到 reflect"
    for prompt in llm.reflect_prompts:
        assert HEADER in prompt
        assert "以工艺手册为主AAAA" in prompt
        assert "创建者的心得BBBB" in prompt
        # owner 若回退到 ContextVar / 用错身份,这一条会红。
        assert "别人的心得CCCC" not in prompt
    # v1 的注入面只有逐节检索:方向非空时压根没有 plan 调用。
    assert not llm.plan_prompts


def test_the_kill_switch_removes_it_from_report_sections_too(repo):
    notebook = _notebook(repo)
    _write(repo, notebook.id, "", "corpus_shape", "以工艺手册为主AAAA")
    repo.settings.agent_profile_enabled = False

    llm = _SectionLLM()
    engine = _engine(repo, llm)
    engine._deep_dive(
        notebook.id,
        {"title": "A", "scope": "s", "sub_queries": ["版图设计要点"]},
        "报告问题", depth=2)

    assert llm.reflect_prompts
    assert all(HEADER not in prompt for prompt in llm.reflect_prompts)


def test_a_report_without_any_understanding_yet_is_unchanged(repo):
    notebook = _notebook(repo)
    llm = _SectionLLM()
    engine = _engine(repo, llm)
    result = engine._deep_dive(
        notebook.id,
        {"title": "A", "scope": "s", "sub_queries": ["版图设计要点"]},
        "报告问题", depth=2)

    assert llm.reflect_prompts
    assert all(HEADER not in prompt for prompt in llm.reflect_prompts)
    assert not [step for step in result.trace if step.step_type == "profile"]

"""Agentic Memory P3 (T8) — 用户检索/回答风格偏好注入 Ask 规划 + 合成。

覆盖:
  * 端到端(``AskService.ask_reasoning``):有 profile → 风格块同时出现在规划
    (``reasoning_agent`` workload)与合成(``ask_answer`` workload)两处 prompt;
    无 profile / 总闸关闭 → 两处 prompt 都不出现风格块(逐字回到接入前形状)。
  * 座位:runtime 装进 ``AskService`` 的 ``identity_store`` 座位 is repository
    自己的那一份(镜像 P1/P2 的同款座位测试)。
  * ``search_profile_wiring_active`` 单点判据(kill switch ∨ store 未接线)。
  * ``AskService._search_profile_style_block`` 的 fail-open 边界:空 user_id
    不触碰 store;store 抛异常时静默降级为空串。
  * 范围守卫(结构性):``render_style_block`` 对三个封闭枚举字段的全部取值组合
    (含 domain_terms 的代表值)渲染出的文本里不出现任何范围/参考库/检索档位
    类关键词——这条断言的钉住力由变异验证证明(报告里记录)。
  * 四个 ``answer_prompt(...)`` 调用点(``ask_service.py``)都携带
    ``style_block=``——AST 扫描,不依赖行号。

``render_style_block`` 本身的渲染合同(空 profile/auto 值不渲染/超预算逐条装入
domain_terms)在 ``test_search_profile_document.py``;本文件只覆盖“到达 prompt”
这一层。
"""
from __future__ import annotations

import ast
import itertools
import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.models.identity import (
    ANSWER_DETAIL_VALUES,
    ANSWER_LANGUAGE_VALUES,
    ANSWER_SHAPE_VALUES,
)
from app.models.schemas import AskRequest, NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.reasoning_retrieval import search_profile_wiring_active
from app.services.search_profile import render_style_block
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client


NOW = "2026-08-20T00:00:00+08:00"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    # 同 test_agent_profile_injection 的隔离:本机 .env 里的真实推理端点会让
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


class _AskEndToEndLLM:
    """区分 plan / reflect / evidence_refine / answer 四种 schema(镜像
    ``test_agent_profile_injection._AskEndToEndLLM``,同一份分类理由)。"""

    configured = True

    def __init__(self):
        self.plan_prompts: list[str] = []
        self.reflect_prompts: list[str] = []
        self.answer_prompts: list[str] = []

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
        self.answer_prompts.append(content)
        return json.dumps({"answer": "版图设计要点参见资料 [k1].", "grounded": True})


def _run_ask_reasoning(repo, notebook, user_id):
    llm = _AskEndToEndLLM()
    for workload_id in ("reasoning_agent", "evidence_refine", "ask_answer"):
        bind_chat_client(repo, workload_id, llm)
    service = repo._runtime.ask_service()
    response = service.ask_reasoning(
        notebook.id,
        AskRequest(question="版图设计要点", mode="reasoning"),
        user_id=user_id,
    )
    return llm, response


# --------------------------------------------------------------- ① 端到端注入


def test_style_block_reaches_both_plan_and_answer_prompts_when_profile_set(repo):
    notebook = _seed(repo)
    user = repo._runtime.identity.create_user("z00000101", "pw123456")
    repo._runtime.identity.set_user_search_profile(
        user.id,
        {"answer_language": "zh", "answer_shape": "table_first"},
        origin="user",
    )

    llm, response = _run_ask_reasoning(repo, notebook, user.id)

    assert llm.plan_prompts, "plan 必须真的被调用过"
    assert "answer in Chinese" in llm.plan_prompts[0]
    assert "prefer tables where the content fits one" in llm.plan_prompts[0]

    assert llm.answer_prompts, "answer 合成必须真的被调用过"
    assert "answer in Chinese" in llm.answer_prompts[0]
    # 框定语必须逐字在场——它是「这只是措辞/组织形态偏好，不是证据」这条边界
    # 唯一到达模型的地方。
    assert "not evidence, not retrieval scope" in llm.answer_prompts[0]
    assert response is not None


def test_no_profile_leaves_both_prompts_without_the_style_block(repo):
    notebook = _seed(repo)
    user = repo._runtime.identity.create_user("z00000102", "pw123456")

    llm, _ = _run_ask_reasoning(repo, notebook, user.id)

    assert llm.plan_prompts and llm.answer_prompts
    assert "User style preferences" not in llm.plan_prompts[0]
    assert "User style preferences" not in llm.answer_prompts[0]


def test_kill_switch_off_leaves_both_prompts_without_the_style_block(repo):
    """总闸关闭 ⇒ 即使 profile 已设置,两处 prompt 仍逐字不带风格块
    ——与 P1/P2 的 kill-switch 冻结基线同一条道理。"""
    notebook = _seed(repo)
    user = repo._runtime.identity.create_user("z00000103", "pw123456")
    repo._runtime.identity.set_user_search_profile(
        user.id, {"answer_language": "zh"}, origin="user")
    repo.settings.user_search_profile_enabled = False

    llm, _ = _run_ask_reasoning(repo, notebook, user.id)

    assert llm.plan_prompts and llm.answer_prompts
    assert "User style preferences" not in llm.plan_prompts[0]
    assert "User style preferences" not in llm.answer_prompts[0]


def test_the_ask_engine_factory_actually_fills_the_identity_store_seat(repo):
    """镜像 P1/P2 的座位测试:runtime 组装出的 ``AskService`` 拿到的
    ``identity_store`` 座位 is repository 的那一个——座位空着的话整条特性在
    Ask 里是死的。"""
    service = repo._runtime.ask_service()
    assert service.identity_store is repo._runtime.identity


# --------------------------------------------------------------- ② 单点判据


def test_the_gate_is_one_predicate(repo):
    settings = repo.settings
    settings.user_search_profile_enabled = True
    assert search_profile_wiring_active(settings, repo._runtime.identity) is True
    assert search_profile_wiring_active(settings, None) is False
    settings.user_search_profile_enabled = False
    assert search_profile_wiring_active(settings, repo._runtime.identity) is False


# --------------------------------------------------------------- ③ fail-open


def test_style_block_helper_needs_no_context_var_and_is_fail_open(repo):
    """``_search_profile_style_block`` 的两条边界:空 user_id 绝不触碰
    store(不回退 ``current_user()`` ContextVar);store 抛异常静默降级为空串。"""
    service = repo._runtime.ask_service()

    class _ExplodingIfCalled:
        def get_user_search_profile(self, user_id):
            raise AssertionError("must not be called for an empty user_id")

    service.identity_store = _ExplodingIfCalled()
    assert service._search_profile_style_block("") == ""

    class _BrokenIdentity:
        def get_user_search_profile(self, user_id):
            raise RuntimeError("identity store is down")

    service.identity_store = _BrokenIdentity()
    assert service._search_profile_style_block("someone") == ""


# --------------------------------------------------------------- ④ 范围守卫


_RANGE_KEYWORDS = ("scope", "source", "notebook", "base", "范围", "勾选", "档位")


def test_render_style_block_never_leaks_scope_or_tier_language():
    """结构性断言:三个封闭枚举字段的全部取值组合(含 domain_terms 的代表值,
    因为它是用户自由文本,不在这条守卫的覆盖范围内——见 render_style_block
    文档字符串)渲染出的**字段派生部分**里,不得出现任何范围/参考库/检索档位
    类关键词。

    ⚠ 只扫字段派生的那一段(整块去掉固定框定语 ``_BLOCK_PREAMBLE`` 之后的
    尾部),不扫整块:框定语本身逐字写死 "not evidence, not retrieval scope"
    ——这是 T8 点 3 要求的边界声明,故意提到 "scope" 这个词来否定它,不是一次
    泄漏。这条守卫要抓的是**动态**内容(语言/形态/详略短语与 domain_terms)
    意外带出范围类词汇,不是审查固定的、已经过评审的框定文案本身。

    变异验证(报告里记录):往 ``models/identity.py`` 的 ``ANSWER_SHAPE_VALUES``
    加一个 ``"only_this_notebook"`` 取值,并在 ``search_profile.py`` 的
    ``_SHAPE_PHRASES`` 里给它配一句含 "notebook" 的短语 ⇒ 本测试必须报红。
    """
    from app.services.search_profile import _BLOCK_PREAMBLE

    domain_term_variants = (None, ["PPA", "IRR"])
    for lang, shape, detail, terms in itertools.product(
        ANSWER_LANGUAGE_VALUES, ANSWER_SHAPE_VALUES, ANSWER_DETAIL_VALUES,
        domain_term_variants,
    ):
        fields = {
            "answer_language": {"value": lang, "origin": "user", "updated_at": NOW},
            "answer_shape": {"value": shape, "origin": "user", "updated_at": NOW},
            "answer_detail": {"value": detail, "origin": "user", "updated_at": NOW},
        }
        if terms is not None:
            fields["domain_terms"] = {
                "value": terms, "origin": "user", "updated_at": NOW}
        profile = {"version": 1, "fields": fields}
        block = render_style_block(profile)
        if not block:
            continue
        assert block.startswith(_BLOCK_PREAMBLE), (
            "framing preamble must be a fixed prefix — this test's exclusion "
            "of it would otherwise be unsound"
        )
        field_derived_tail = block[len(_BLOCK_PREAMBLE):].lower()
        for word in _RANGE_KEYWORDS:
            assert word.lower() not in field_derived_tail, (
                f"combo lang={lang!r} shape={shape!r} detail={detail!r} "
                f"terms={terms!r} leaked {word!r} into: {block!r}"
            )


# --------------------------------------------------------------- ⑤ AST 扫描


def test_every_answer_prompt_call_site_in_ask_service_passes_style_block():
    """AST 扫描 ``ask_service.py`` 的全部 ``answer_prompt(...)`` 调用点,逐一
    断言携带 ``style_block=`` 关键字参数——不依赖行号,源码改动后仍然成立。"""
    import app.services.ask_service as ask_service_module

    source = Path(ask_service_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "answer_prompt"
    ]
    assert len(calls) == 4, (
        "expected 4 answer_prompt(...) call sites in ask_service.py, found "
        f"{len(calls)} — update this test's expected count if that changed "
        "deliberately"
    )
    for call in calls:
        kw_names = {kw.arg for kw in call.keywords}
        assert "style_block" in kw_names, (
            f"answer_prompt(...) call at ask_service.py:{call.lineno} is "
            "missing style_block=..."
        )

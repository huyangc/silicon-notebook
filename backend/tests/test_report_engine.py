"""深度报告(report_engine)测试:配置项 / reports CRUD / 三 prompt / 引擎编排。"""
import json

import pytest
from tests.model_testkit import RecordingModelProvider, bind_chat_client


# ---------------------------------------------------------------------------
# Task 1: REPORT_* 配置项
# ---------------------------------------------------------------------------

def test_report_settings_defaults():
    from app.core.config import Settings
    s = Settings(_env_file=None)
    assert s.report_max_sections == 6
    assert not hasattr(s, "report_section_top_n")   # 已移除:逐节与 ask 统一走自适应预算
    assert s.report_section_chunk_budget == 20000
    assert s.report_section_concurrency == 5
    assert s.report_retrieval_fanout == 8
    assert s.report_probe_channel_concurrency == 2
    assert s.report_generation_concurrency == 1
    assert not hasattr(s, "report_context_window_tokens")
    assert not hasattr(s, "report_summary_context_window_tokens")
    assert s.report_section_max_tokens == 65536
    assert s.report_synthesis_max_tokens == 102400
    assert s.report_summary_max_tokens == 102400
    assert s.report_allow_parametric is True
    assert s.report_sufficiency_min_relevant_items == 3
    assert s.report_sufficiency_min_families == 2
    assert s.report_sufficiency_complete_min_families == 3
    assert s.report_sufficiency_max_top_family_share == 0.8


def test_report_settings_env(monkeypatch):
    from app.core.config import Settings
    monkeypatch.setenv("REPORT_MAX_SECTIONS", "4")
    monkeypatch.setenv("REPORT_ALLOW_PARAMETRIC", "false")
    monkeypatch.setenv("REPORT_SECTION_MAX_TOKENS", "70000")
    monkeypatch.setenv("REPORT_SYNTHESIS_MAX_TOKENS", "71000")
    monkeypatch.setenv("REPORT_SUMMARY_MAX_TOKENS", "33000")
    monkeypatch.setenv("REPORT_RETRIEVAL_FANOUT", "7")
    monkeypatch.setenv("REPORT_PROBE_CHANNEL_CONCURRENCY", "1")
    monkeypatch.setenv("REPORT_SUFFICIENCY_MIN_RELEVANT_ITEMS", "5")
    monkeypatch.setenv("REPORT_SUFFICIENCY_MIN_FAMILIES", "3")
    monkeypatch.setenv("REPORT_SUFFICIENCY_COMPLETE_MIN_FAMILIES", "4")
    monkeypatch.setenv("REPORT_SUFFICIENCY_MAX_TOP_FAMILY_SHARE", "0.65")
    s = Settings(_env_file=None)
    assert s.report_max_sections == 4
    assert s.report_allow_parametric is False
    assert s.report_section_max_tokens == 70000
    assert s.report_synthesis_max_tokens == 71000
    assert s.report_summary_max_tokens == 33000
    assert s.report_retrieval_fanout == 7
    assert s.report_probe_channel_concurrency == 1
    assert s.report_sufficiency_min_relevant_items == 5
    assert s.report_sufficiency_min_families == 3
    assert s.report_sufficiency_complete_min_families == 4
    assert s.report_sufficiency_max_top_family_share == 0.65


@pytest.mark.parametrize(
    ("name", "value"),
    (("REPORT_MAX_SECTIONS", "0"), ("REPORT_MAX_SUBQUERIES_PER_SECTION", "1")),
)
def test_report_outline_rails_reject_nonpositive_or_nonsensical_values(
    monkeypatch, name, value,
):
    from pydantic import ValidationError

    from app.core.config import Settings

    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


# ---------------------------------------------------------------------------
# Task 2: reports 表 + repo CRUD
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    # 隔离 LLM 端点:清空真实 key/model,避免 OS 环境让 reasoning/rewrite 专属
    # client 被构造出来打真实网络(不 configured 时回退到测试桩 llm_client)。
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL",
               "REWRITE_LLM_API_KEY", "REWRITE_LLM_BASE_URL", "REWRITE_LLM_MODEL"):
        monkeypatch.setenv(_k, "")
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    provider = RecordingModelProvider()
    repository = SQLiteRepository(
        Settings(_env_file=None), model_provider=provider
    )
    repository.recording_model_provider = provider
    return repository


def _mk_nb(repo):
    from app.models.schemas import NotebookCreate
    return repo.create_notebook(NotebookCreate(name="t", purpose="p", primary_domain="d"))


def test_report_crud_roundtrip(repo):
    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "为什么 bandgap 是 1.2V?")
    assert rid.startswith("rep-")
    detail = repo.get_report(nb.id, rid)
    assert detail["status"] == "pending" and detail["question"].startswith("为什么")
    repo.update_report(nb.id, rid, status="outline_ready", progress="大纲规划完成")
    assert repo.claim_report_generation(nb.id, rid)
    started_at = repo.get_report(nb.id, rid)["generation_started_at"]
    assert started_at
    repo.update_report(nb.id, rid, outline=[{"title": "机理", "scope": "s", "sub_queries": ["q1"]}],
                       sections=[{"title": "机理", "markdown": "md", "grounded": True}],
                       gaps=["缺 X"], content_md="# 报告", status="done", progress="完成")
    detail = repo.get_report(nb.id, rid)
    assert detail["status"] == "done" and detail["content_md"] == "# 报告"
    assert detail["generation_started_at"] == started_at
    assert detail["updated_at"] >= started_at
    assert detail["outline"][0]["title"] == "机理" and detail["gaps"] == ["缺 X"]
    lst = repo.list_reports(nb.id, created_by=None)
    assert len(lst) == 1 and lst[0]["id"] == rid and "content_md" not in lst[0]
    repo.delete_report(nb.id, rid)
    assert repo.list_reports(nb.id, created_by=None) == []
    with pytest.raises(KeyError):
        repo.get_report(nb.id, rid)


def test_failed_report_generation_claim_reuses_outline_and_clears_old_artifacts(repo):
    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "q")
    outline = [{"title": "A", "scope": "s", "sub_queries": ["q"]}]
    repo.update_report(
        nb.id, rid, status="failed", outline=outline,
        understanding={"confirmed": True, "credibility": {"synthesis_status": "failed_model"}},
        sections=[{"title": "old", "markdown": "stale"}],
        gaps=["old"], references=[{"key": "k1"}], content_md="# stale",
        section_status=[{"title": "old", "phase": "失败", "step": 0}],
        error="pool timeout",
    )

    assert repo.claim_report_generation(nb.id, rid) is True
    detail = repo.get_report(nb.id, rid)
    assert detail["status"] == "generating"
    assert detail["outline"] == outline
    assert detail["sections"] == [] and detail["gaps"] == []
    assert detail["references"] == [] and detail["content_md"] == ""
    assert detail["section_status"] == [] and detail["error"] == ""
    assert detail["understanding"]["confirmed"] is True
    assert "credibility" not in detail["understanding"]
    assert detail["generation_started_at"]


# ---------------------------------------------------------------------------
# Task 4: 报告三 prompt(大纲/节撰写/执行摘要)
# ---------------------------------------------------------------------------

def test_report_prompts_contract():
    from app.services.prompts import (
        query_intent_prompt, report_outline_prompt, report_section_prompt,
        report_summary_prompt, REPORT_INTENT_SCHEMA_HINT,
        REPORT_OUTLINE_SCHEMA_HINT, REPORT_SECTION_SCHEMA_HINT)
    op = report_outline_prompt("q", max_sections=5, history_block="h")
    assert "3-5" in op and "sub_queries" in op and "Prior conversation" in op
    sp = report_section_prompt("失效机理", "应力如何改变 VBE", "总问题", "CTX",
                               allow_parametric=True)
    assert "【通识】" in sp and "[k" in sp and "layer by layer" not in sp  # 独立文本,非复用 answer_prompt
    assert "ONLY this section" in sp
    # 两层库权威规则(镜像 answer_prompt 规则 5):base 权威 + 冲突以 base 为准 + 切题第一不强引。
    spl = sp.lower()
    assert "authoritative" in spl and "defer to the base" in spl, \
        "report_section_prompt 缺 base 权威/冲突以 base 为准 规则"
    assert "relevant" in spl and ("do not" in spl or "don't" in spl), \
        "report_section_prompt 缺『base 与本节无关不要硬引(切题第一)』规则"
    sp2 = report_section_prompt("t", "s", "q", "CTX", allow_parametric=False)
    assert "【通识】" not in sp2
    su = report_summary_prompt("总问题", "## 节1\nmd")
    assert "executive summary" in su.lower()
    intent = query_intent_prompt(
        "原始问题", max_topics=4, purpose="deep report",
    )
    assert "before seeing any corpus" in intent and "原始问题" in intent
    assert "mandatory_topics" in REPORT_INTENT_SCHEMA_HINT
    assert "ambiguities" in REPORT_INTENT_SCHEMA_HINT and "needs_clarification" in intent
    assert "source_refs" not in REPORT_INTENT_SCHEMA_HINT
    assert "source_refs" not in intent
    confirmed = query_intent_prompt(
        "已确认的问题", history_block="对象：PLL", purpose="deep report",
        confirmation_mode=True,
    )
    assert "authoritative" in confirmed and "needs_clarification=false" in confirmed


def test_report_prompts_propagate_inference_to_conclusions():
    """T2-c:推断状态传递规则同时落在节撰写与执行摘要两处 prompt。"""
    from app.services.prompts import report_section_prompt, report_summary_prompt

    sp = report_section_prompt("失效机理", "应力如何改变 VBE", "总问题", "CTX",
                               allow_parametric=True)
    extension = (
        "A conclusion or in-section summary that rests on any （推断） or "
        "【通识】 sentence is itself an inference"
    )
    assert extension in sp
    # 例外句是承重的:删掉它模型会给所有结论句无差别加（推断）。
    assert "only a conclusion whose every premise is [k]-cited may omit it" in sp
    # 落位:扩展句留在规则 2 内、规则 3(report_section.domain_conventions 片段,
    # 起始序号契约)之前——挪到规则 10/11 去测试要红。
    assert sp.index(extension) < sp.index("3. Keep the derivation chain")
    # 通识关闭时前提列表不提【通识】(test_report_prompts_contract 钉的既有契约),
    # 但传递规则本身仍在。
    sp_off = report_section_prompt("t", "s", "q", "CTX", allow_parametric=False)
    assert "rests on any （推断） sentence is itself an inference" in sp_off
    assert "【通识】" not in sp_off

    su = report_summary_prompt("总问题", "## 节1\nmd")
    keep = "are NOT citation markers: keep them"
    assert keep in su
    assert "opens with （推断）" in su
    assert (
        "Never promote an inferred or general-knowledge finding into an "
        "unmarked fact." in su
    )
    # 落位:在指令区(assumptions 那句之前),绝不落到 sections_block 之后。
    assert su.index(keep) < su.index("Any intent assumptions") < su.index("Report sections:")


# ---------------------------------------------------------------------------
# Task 5: report_engine——大纲 + 逐节并行深挖 + 撰写
# ---------------------------------------------------------------------------

def _mk_engine(repo, llm):
    # Task 25:引擎端口化——测试经 from_repository 冻结适配器构造(提取窄端口,
    # 不再持 facade);检索桩改打在 repo.retrieval / repo._runtime.* 的所有者上。
    from app.services.report_engine import ReportEngine
    _bind_report_llm(repo, llm)
    return ReportEngine.from_repository(repo, repo.settings)


def _bind_report_llm(repo, llm):
    for workload_id in (
        "report_outline", "report_sufficiency", "report_section",
        "report_summary", "query_rewrite", "reasoning_agent", "ask_answer",
    ):
        bind_chat_client(repo, workload_id, llm)


class _OutlineLLM:
    configured = True
    def __init__(self, n_fail_sections=0):
        self.section_calls = []
        self._fail_left = n_fail_sections
    def chat_json(self, messages, schema_hint, **kw):
        content = messages[-1]["content"]
        if "OUTLINE" in content:
            return json.dumps({"sections": [
                {"title": "A", "scope": "sa", "sub_queries": ["qa"]},
                {"title": "B", "scope": "sb", "sub_queries": ["qb"]}]})
        if "ONE section" in content:
            self.section_calls.append(content)
            # 持久失败:被选中的节(scope=sb)每次调用都抛。_draft_section 现对空/失败
            # 有界重试一次——单次失败会被重试恢复,故编排容错测试需持久失败的节;
            # 按 scope 定选(而非按调用序)保证并发下确定性。n_fail_sections>0 → B 节失败。
            if self._fail_left > 0 and "Section scope: sb" in content:
                raise RuntimeError("boom")
            return json.dumps({"markdown": "## X\nbody [k1]", "grounded": True})
        if "EXECUTIVE SUMMARY" in content:
            return json.dumps({"summary": "总结"})
        return json.dumps({})


def test_report_operations_bind_each_exact_workload(repo):
    from app.services.reasoning_retrieval import ReasoningResult

    engine = _mk_engine(repo, _OutlineLLM())
    notebook = _mk_nb(repo)
    report_id = repo.create_report(notebook.id, "q")
    provider = repo.recording_model_provider
    provider.calls.clear()

    engine._plan_outline(notebook.id, "q", "")
    engine._judge_sufficiency(
        "q", [{"title": "A"}],
        [{"title": "A", "hits": 0, "base_hits": 0}],
    )
    engine._draft_section(
        notebook.id,
        {"title": "A", "scope": "s", "sub_queries": ["q"]},
        "q", ReasoningResult(),
    )
    engine._assemble(
        notebook.id, report_id, "q", [],
        [{"title": "A", "scope": "s", "markdown": "body", "id_map": {}}],
    )

    called = {workload for kind, workload in provider.calls if kind == "chat"}
    assert {
        "report_outline", "report_sufficiency", "report_section", "report_summary"
    } <= called

    class _Bad:
        configured = True

        def chat_json(self, *args, **kwargs):
            return "not json"

    bind_chat_client(repo, "report_outline", _Bad())
    bind_chat_client(repo, "query_rewrite", _Bad())
    provider.calls.clear()
    engine._plan_outline(notebook.id, "fallback", "")
    assert ("chat", "query_rewrite") in provider.calls


def test_deep_dive_feeds_current_situation_the_sections_own_persisted_scope(
    repo, monkeypatch,
):
    """T6 修复轮裁决 3(Agentic Memory P2):``_deep_dive`` 必须把
    ``section["intent_contract"]``——``_bind_outline_to_intent`` 在大纲阶段写进
    每一节、随 ``reports.outline_json`` 一起持久化的那份报告行自己的意图契约
    副本——里的 ``result_scope``/``completeness_required`` 喂给
    ``ReasoningRetriever.run(intent_detail=...)``。

    修复前这里恒不传 ``intent_detail``:任何档位的报告节都会让
    ``current_situation`` 在这两个键上落回同一个 ``unknown``/``False``,「要求
    完整枚举」的节与「只要相关性排序」的节因而共享同一份诚实降级出来的广义
    形状,一条蒸馏自 ``result_scope="ranked"`` 问题的经验条目会在这两类节上
    同样「匹配」。修复后两类节在这两个键上分道。
    """
    from app.services.report_engine import ReportEngine
    from app.services.retrieval_experience_projection import current_situation

    captured: list[dict | None] = []

    class _CapturingRetriever:
        def __init__(self, **kwargs):
            pass

        def run(self, *args, **kwargs):
            from app.services.reasoning_retrieval import ReasoningResult

            captured.append(kwargs.get("intent_detail"))
            return ReasoningResult()

    monkeypatch.setattr(
        "app.services.reasoning_retrieval.ReasoningRetriever", _CapturingRetriever,
    )

    eng = ReportEngine.from_repository(repo, repo.settings)
    nb = _mk_nb(repo)

    complete_section = {
        "title": "完整清单",
        "scope": "完整清单",
        "intent_contract": {
            "result_scope": "complete", "completeness_required": True,
        },
    }
    ranked_section = {
        "title": "相关性问答",
        "scope": "相关性问答",
        "intent_contract": {
            "result_scope": "ranked", "completeness_required": False,
        },
    }

    eng._deep_dive(nb.id, complete_section, "问题")
    eng._deep_dive(nb.id, ranked_section, "问题")

    # 两节各自把**自己**持久化的 result_scope/completeness_required 原样带下去
    # ——不是 self 上规划阶段留下的瞬态属性,也不是伪造出的完整意图契约。
    assert captured == [
        {"result_scope": "complete", "completeness_required": True},
        {"result_scope": "ranked", "completeness_required": False},
    ]

    complete_situation = current_situation(
        captured[0], mode="reasoning", retrieval_effort="standard")
    ranked_situation = current_situation(
        captured[1], mode="reasoning", retrieval_effort="standard")
    assert complete_situation["result_scope"] == "complete"
    assert complete_situation["completeness_required"] is True
    assert ranked_situation["result_scope"] == "ranked"
    assert ranked_situation["completeness_required"] is False
    # 报告的 complete 语义不再匹配 ranked 蒸出的条目:修复前两节都会落在同一个
    # unknown/False 上,这条断言在修复前不成立。
    assert complete_situation["result_scope"] != ranked_situation["result_scope"]
    assert (
        complete_situation["completeness_required"]
        != ranked_situation["completeness_required"]
    )
    # 诚实降级从 6 键 unknown 收窄到 4 键:mode/retrieval_effort 本就由显式参数
    # 给出,result_scope/completeness_required 现在也不是 unknown/False 了——
    # 只剩 entity_count/topic_count/has_constraints/has_exclusions 四个键仍然
    # 诚实地落在未知(节问题没有实体/主题/约束/排除项那份结构)。
    assert complete_situation["entity_count"] == "none"
    assert complete_situation["topic_count"] == "none"
    assert complete_situation["has_constraints"] is False
    assert complete_situation["has_exclusions"] is False


def test_engine_outline_fallback_on_bad_json(repo):
    nb = _mk_nb(repo)
    class _Bad:
        configured = True
        def chat_json(self, *a, **k):
            return "not json"
    eng = _mk_engine(repo, _Bad())
    outline = eng._plan_outline(nb.id, "q", "")
    assert len(outline) >= 1 and outline[0]["sub_queries"]      # 回退骨架仍可跑


def test_engine_runs_sections_in_parallel_and_tolerates_one_failure(repo, monkeypatch):
    # 两阶段:run(auto_generate=True) 规划后直出。STORM 回退到 _plan_outline 的
    # A/B 两节;stub 语料侦察/探针以与检索解耦(编排测试聚焦并行深挖与容错)。
    from app.services.report_engine import ReportEngine
    nb = _mk_nb(repo)
    llm = _OutlineLLM(n_fail_sections=1)
    eng = _mk_engine(repo, llm)
    monkeypatch.setattr(ReportEngine, "_build_corpus_map", lambda self, n, q: "MAP")
    monkeypatch.setattr(repo.retrieval, "federated_retrieve", lambda a, q: [])
    # stub 每节深挖:不跑真检索,返回空 ReasoningResult(编排测试与检索解耦)
    from app.services.reasoning_retrieval import ReasoningResult
    monkeypatch.setattr(eng, "_deep_dive",
                        lambda nb_id, section, question, depth=None, on_step=None: ReasoningResult())
    rid = repo.create_report(nb.id, "q")
    eng.run(nb.id, rid, "q", "", auto_generate=True)
    detail = repo.get_report(nb.id, rid)
    assert detail["status"] == "done"
    secs = detail["sections"]
    assert len(secs) == 2
    ok = [s for s in secs if not s.get("failed")]
    bad = [s for s in secs if s.get("failed")]
    assert len(ok) == 1 and len(bad) == 1          # 单节失败不拖垮整报告
    assert detail["content_md"].startswith("#")     # 汇总仍生成


def test_engine_cancel_marks_cancelled(repo, monkeypatch):
    # 两阶段:规划到 outline_ready 后 auto_generate 进生成,深挖中途取消 → cancelled。
    import threading
    nb = _mk_nb(repo)
    llm = _OutlineLLM()
    cancel = threading.Event()
    from app.services.report_engine import ReportEngine
    _bind_report_llm(repo, llm)
    eng = ReportEngine.from_repository(repo, repo.settings, cancel_event=cancel)
    monkeypatch.setattr(ReportEngine, "_build_corpus_map", lambda self, n, q: "MAP")
    monkeypatch.setattr(repo.retrieval, "federated_retrieve", lambda a, q: [])
    from app.services.reasoning_retrieval import ReasoningResult
    def _dd(nb_id, section, question, depth=None, on_step=None):
        cancel.set()                                # 深挖中途被取消
        from app.services.cancellation import raise_if_cancelled
        raise_if_cancelled(cancel)
    monkeypatch.setattr(eng, "_deep_dive", _dd)
    rid = repo.create_report(nb.id, "q")
    eng.run(nb.id, rid, "q", "", auto_generate=True)
    assert repo.get_report(nb.id, rid)["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Task 6: Stage D 汇总——执行摘要 + 章节 + 参考文献 +(结尾)局限
# 报告体例:不堆砌「知识缺口」诊断、不外显「分析计划」子查询;仅结尾一行局限。
# ---------------------------------------------------------------------------

def test_assemble_builds_report_body_only(repo):
    """content_md = 执行摘要 + 章节 + 参考文献 + 结尾局限行;无知识缺口/分析计划段,
    无概念对连通性罗列。gaps 仅精简保留「库内证据不足的章节」信号(供未来覆盖度面板)。"""
    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _OutlineLLM())
    outline = [{"title": "A", "scope": "sa", "sub_queries": ["qa"]},
               {"title": "B", "scope": "sb", "sub_queries": ["qb"]}]
    sections = [
        {"title": "A", "scope": "sa", "markdown": "## A\nbody [k1]", "grounded": True,
         "id_map": {"k1": {"object_id": "c1", "object_type": "chunk", "name": "BGR",
                           "source_title": "Razavi", "location_label": "§11",
                           "tier": "base"}},
         "attempted": [{"query": "qa-dry", "new": 0, "tries": 2}]},
        {"title": "B", "scope": "sb", "markdown": "## B\n【通识】x", "grounded": False,
         "id_map": {}, "attempted": []},
    ]
    rid = repo.create_report(nb.id, "q")
    md, gaps, references = eng._assemble(nb.id, rid, "q", outline, sections)
    # 报告主体
    assert "## 执行摘要" in md and "总结" in md
    assert "## 资料基础" in md
    assert "## A" in md and "## B" in md
    assert "## 参考文献" in md and "Razavi" in md
    assert "> 引证分布：" in md
    assert references[0]["label"] == "Razavi"
    # 移除项:无诊断堆砌 / 无内部机制外显
    assert "## 知识缺口" not in md and "## 分析计划" not in md
    assert "qa-dry" not in md and "qa" not in md.split("## 参考文献")[0]  # 子查询不外显
    assert "尚无关联边" not in md                                        # 概念对连通性已删
    # 结尾一行局限:点名库内证据不足的章节(B,grounded=False)
    assert "局限" in md and "B" in md.split("局限")[1][:40]
    # gaps 字段精简:仅弱证据章节(供覆盖度面板),不含概念对/原始子查询
    assert gaps == ["「B」库内证据不足,内容偏推断/通识"]


def test_assemble_multikey_citation_renumbered(repo):
    """LLM 常吐逗号复合引用 [k1, k3](而非 prompt 要求的 [k1][k3]):_assemble 须逐 key
    全局重编号、登记两条 reference,正文输出仍是逗号复合 [k1, k2](全局编号)。"""
    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _OutlineLLM())
    outline = [{"title": "A", "scope": "sa", "sub_queries": ["qa"]}]
    sections = [
        {"title": "A", "scope": "sa",
         "markdown": "## A\n超越开源并与闭源持平 [k1, k3]。", "grounded": True,
         "id_map": {
             "k1": {"object_id": "c1", "object_type": "chunk", "name": "N1",
                    "source_title": "SrcA", "location_label": "§1", "tier": "base"},
             "k3": {"object_id": "c3", "object_type": "chunk", "name": "N3",
                    "source_title": "SrcB", "location_label": "§3", "tier": "personal"},
         },
         "attempted": []},
    ]
    rid = repo.create_report(nb.id, "q")
    md, gaps, references = eng._assemble(nb.id, rid, "q", outline, sections)
    body = md.split("## 参考文献")[0]
    assert len(references) == 2                       # 两来源都登记
    assert "[k1, k2]" in body                         # 复合引用逐 key 重编号(逗号形式保留)
    assert "k3" not in body                           # 节内局部 key 不残留
    assert "SrcA" in md and "SrcB" in md


def test_assemble_chinese_bracket_citations_are_bound_and_renumbered(repo):
    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _OutlineLLM())
    outline = [{"title": "A", "scope": "sa", "sub_queries": ["qa"]}]
    sections = [{
        "title": "A", "scope": "sa",
        "markdown": "## A\n中文括号引用【k1，k3】。", "grounded": True,
        "id_map": {
            "k1": {"object_id": "c1", "object_type": "chunk", "name": "N1",
                   "source_title": "SrcA", "location_label": "§1"},
            "k3": {"object_id": "c3", "object_type": "chunk", "name": "N3",
                   "source_title": "SrcB", "location_label": "§3"},
        },
        "attempted": [],
    }]
    rid = repo.create_report(nb.id, "q")
    md, _gaps, references = eng._assemble(nb.id, rid, "q", outline, sections)
    body = md.split("## 参考文献")[0]
    assert "[k1, k2]" in body
    assert len(references) == 2


def test_assemble_keeps_unknown_sources_visible_and_uses_conservative_top1(repo, monkeypatch):
    """Unknown bibliography entries stay source-addressable, not silently merged.

    Seven anchors from A, one from B, and six unresolved source ids have a
    conservative concentration upper bound of 13/14: each unknown anchor may
    be another copy of A, but none may inflate the independent-source count.
    """
    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _OutlineLLM())
    unknown_ids = [f"source-u-{index}" for index in range(6)]
    monkeypatch.setattr(
        eng,
        "_resolve_source_families",
        lambda source_ids: {
            "family_by_source": {
                "source-a": "hash:a",
                "source-b": "hash:b",
            },
            "uncertain_source_ids": unknown_ids,
            "unresolved_source_ids": [],
        },
    )
    source_ids = [*("source-a" for _ in range(7)), "source-b", *unknown_ids]
    id_map = {
        f"k{index + 1}": {
            "object_id": f"object-{index + 1}",
            "object_type": "chunk",
            "source_id": source_id,
            "source_title": (
                "Source A" if source_id == "source-a" else
                "Source B" if source_id == "source-b" else
                f"Unknown {index - 7}"
            ),
            "location_label": f"§{index + 1}",
            "tier": "personal",
        }
        for index, source_id in enumerate(source_ids)
    }
    markers = ", ".join(id_map)
    sections = [{
        "title": "A",
        "scope": "sa",
        "markdown": f"## A\nEvidence [{markers}]",
        "grounded": True,
        "id_map": id_map,
        "attempted": [],
    }]
    rid = repo.create_report(nb.id, "q")
    md, _gaps, references = eng._assemble(
        nb.id, rid, "q", [{"title": "A", "scope": "sa"}], sections
    )

    bibliography = md.split("## 参考文献", 1)[1].split("> 引证分布：", 1)[0]
    assert len([line for line in bibliography.splitlines() if line.startswith("- ")]) == 8
    assert {reference["family_key"] for reference in references if reference["source_id"] in unknown_ids} == {
        f"source:{source_id}" for source_id in unknown_ids
    }
    credibility = repo.get_report(nb.id, rid)["understanding"]["credibility"]
    assert credibility["independent_cited_families"] == 2
    assert credibility["identity_uncertain_anchors"] == 6
    assert credibility["top1_family_share"] == pytest.approx(13 / 14)
    assert "上界为 92.9%" in md


def test_assemble_credibility_counts_partial_ledgers_as_available_and_tracks_them(repo):
    """A partial ledger still audited some rows, so it counts toward the usable
    total alongside a fully `available` one; `claim_ledgers_partial` breaks out
    how many of those usable ledgers had rows dropped."""
    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _OutlineLLM())
    outline = [{"title": t, "scope": "s", "sub_queries": [t]} for t in ("A", "B", "C")]
    sections = [
        {"title": "A", "scope": "s", "markdown": "## A\nx", "grounded": False,
         "id_map": {}, "attempted": [], "claim_ledger_status": "available"},
        {"title": "B", "scope": "s", "markdown": "## B\nx", "grounded": False,
         "id_map": {}, "attempted": [], "claim_ledger_status": "partial"},
        {"title": "C", "scope": "s", "markdown": "## C\nx", "grounded": False,
         "id_map": {}, "attempted": [], "claim_ledger_status": "missing"},
    ]
    rid = repo.create_report(nb.id, "q")
    eng._assemble(nb.id, rid, "q", outline, sections)
    credibility = repo.get_report(nb.id, rid)["understanding"]["credibility"]
    assert credibility["claim_ledgers_available"] == 2
    assert credibility["claim_ledgers_partial"] == 1
    assert credibility["claim_ledgers_total"] == 3


def test_assemble_multikey_fails_closed_if_any_key_is_unknown(repo):
    """任一本节未知 key 使整个复合 marker fail closed，且不产生部分 reference。"""
    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _OutlineLLM())
    outline = [{"title": "A", "scope": "sa", "sub_queries": ["qa"]}]
    sections = [
        {"title": "A", "scope": "sa",
         "markdown": "## A\n不完整前提 [k2001, k9999]。", "grounded": True,
         "id_map": {"k2001": {
             "object_id": "rel-ab", "object_type": "relation",
             "name": "A --derived_from--> B", "source_title": "SrcA",
             "location_label": "§1", "tier": "base",
         }},
         "attempted": []},
    ]
    rid = repo.create_report(nb.id, "q")
    md, gaps, references = eng._assemble(nb.id, rid, "q", outline, sections)
    body = md.split("## 参考文献")[0]
    assert "k2001" not in body and "k9999" not in body
    assert "不完整前提 。" in body or "不完整前提  。" in body
    assert references == []
    assert "## 参考文献" not in md


def test_assemble_all_grounded_has_no_limitation_note(repo):
    """全部章节有库内支撑时,报告无「局限」行、gaps 为空。"""
    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _OutlineLLM())
    outline = [{"title": "A", "scope": "sa", "sub_queries": ["qa"]}]
    sections = [{"title": "A", "scope": "sa", "markdown": "## A\nbody [k1]",
                 "grounded": True,
                 "id_map": {"k1": {"object_id": "c1", "object_type": "chunk",
                                   "name": "X", "source_title": "Src",
                                   "location_label": "", "tier": "base"}},
                 "attempted": []}]
    rid = repo.create_report(nb.id, "q")
    md, gaps, references = eng._assemble(nb.id, rid, "q", outline, sections)
    assert "局限" not in md and gaps == []


def test_assemble_global_citation_renumber_and_references(repo, monkeypatch):
    """跨节 [k] 按具体证据锚点全局重编号:同源不同 chunk 仍是两条引用;未知
    marker 被剥除;references 结构化有序;content_md 内联与参考文献段一致。"""
    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _OutlineLLM())
    outline = [{"title": "A", "scope": "sa", "sub_queries": ["qa"]},
               {"title": "B", "scope": "sb", "sub_queries": ["qb"]}]
    # A 引用 Razavi(k1)+Gray(k2)且有个幻觉 k9;B 再次引用 Razavi(节内 k1)
    razavi = {"object_id": "c1", "object_type": "chunk", "name": "BGR",
              "source_title": "Razavi Analog CMOS", "location_label": "§11", "tier": "base"}
    gray = {"object_id": "c2", "object_type": "chunk", "name": "PN",
            "source_title": "Gray & Meyer", "location_label": "§1", "tier": "base"}
    razavi_b = {"object_id": "c9", "object_type": "chunk", "name": "curv",
                "source_title": "Razavi Analog CMOS", "location_label": "§11.4", "tier": "base"}
    sections = [
        {"title": "A", "scope": "sa", "grounded": True,
         "markdown": "## A\nCTAT+PTAT 抵消 [k1]。指数式 [k2]。幻觉 [k9]。",
         "id_map": {"k1": razavi, "k2": gray},
         "attempted": [], "top_concepts": []},
        {"title": "B", "scope": "sb", "grounded": True,
         "markdown": "## B\n曲率补偿 [k1]。",
         "id_map": {"k1": razavi_b},          # 同 Razavi 来源,节内也叫 k1
         "attempted": [], "top_concepts": []},
    ]
    rid = repo.create_report(nb.id, "q")
    md, gaps, references = eng._assemble(nb.id, rid, "q", outline, sections)

    # 精确锚点去重:同一来源的 c1/c9 不折叠,避免把曲率补偿错绑到 §11。
    assert [r["key"] for r in references] == ["k1", "k2", "k3"]
    assert references[0]["label"] == "Razavi Analog CMOS"
    assert references[1]["label"] == "Gray & Meyer"
    assert references[2]["location_label"] == "§11.4"
    # A 段:k1/k2 保留、幻觉 k9 被剥除
    assert "[k1]" in md and "[k2]" in md and "[k9]" not in md and "幻觉 。" in md
    # B 段:节内 k1(Razavi c9)→ 全局 k3。仅取 B 正文
    # (到下一个 ## 标题止,避免命中「参考文献」段里罗列的 [k2]）。
    b_seg = md.split("## B")[1].split("\n## ")[0]
    assert "[k3]" in b_seg and "[k1]" not in b_seg and "[k2]" not in b_seg
    # 参考文献段列出三个精确锚点
    assert "## 参考文献" in md
    bibliography = md.split("## 参考文献")[1]
    assert "[k1]" in bibliography and "[k3]" in bibliography
    assert "Razavi Analog CMOS" in bibliography
    bibliography_rows = [
        line for line in bibliography.splitlines() if line.startswith("- ")
    ]
    assert len(bibliography_rows) == 2
    assert "[k1] [k3]" in bibliography_rows[0]


def test_assemble_prefers_parsed_paper_title_over_upload_name(repo):
    nb = _mk_nb(repo)
    store = repo._runtime.source_store
    store.insert_source(
        source_id="src-paper", notebook_id=nb.id, title="2407.00123v2.pdf",
        source_type="document", status="extracted", parse_status="extracted",
        file_name="2407.00123v2.pdf", file_path="/tmp/2407.00123v2.pdf",
        file_size=0, file_hash="paper-hash", summary="", doc_type="academic_paper",
    )
    store.upsert_paper_meta(
        "src-paper", nb.id,
        {
            "is_paper": True, "paper_title": "Reliable Analog Design Methods",
            "venue": None, "pub_year": None, "doi": None, "keywords": [],
            "authors": [], "model": "test", "raw_json": "{}",
        },
    )
    eng = _mk_engine(repo, _OutlineLLM())
    sections = [{
        "title": "A", "scope": "sa", "markdown": "## A\nbody [k1]",
        "grounded": True, "attempted": [],
        "id_map": {"k1": {
            "object_id": "e-paper", "object_type": "element",
            "name": "p. 2", "source_id": "src-paper",
            "source_title": "2407.00123v2.pdf", "location_label": "p. 2",
            "tier": "personal", "snippet": "evidence",
        }},
    }]

    md, _gaps, references = eng._assemble(
        nb.id, repo.create_report(nb.id, "q"), "q",
        [{"title": "A", "scope": "sa", "sub_queries": ["qa"]}], sections,
    )

    assert references[0]["source_title"] == "Reliable Analog Design Methods"
    assert references[0]["source_file_name"] == "2407.00123v2.pdf"
    assert references[0]["label"] == "Reliable Analog Design Methods"
    assert "Reliable Analog Design Methods" in md
    assert "2407.00123v2.pdf" not in md


def test_assemble_keeps_same_source_relation_anchors_distinct(repo):
    """同一来源里的两跳关系证据是两条不同前提，必须按 relation id 分别编号；
    不能沿用 chunk 的按来源去重而把第二跳吞掉，且引用 metadata 要逐条保留。"""
    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _OutlineLLM())
    outline = [{"title": "A", "scope": "sa", "sub_queries": ["qa"]}]
    sections = [{
        "title": "A", "scope": "sa", "grounded": True,
        "markdown": "## A\n前提一 [k2001]，前提二 [k2002]。",
        "id_map": {
            "k2001": {
                "object_id": "rel-ab", "object_type": "relation",
                "name": "A --derived_from--> B", "source_id": "source-1",
                "source_title": "同一篇论文", "location_label": "§1",
                "tier": "base",
            },
            "k2002": {
                "object_id": "rel-bc", "object_type": "relation",
                "name": "B --derived_from--> C", "source_id": "source-1",
                "source_title": "同一篇论文", "location_label": "§2",
                "tier": "personal",
            },
        },
        "attempted": [],
    }]

    md, gaps, references = eng._assemble(
        nb.id, repo.create_report(nb.id, "q"), "q", outline, sections)

    assert gaps == []
    assert "[k1]" in md.split("## 参考文献")[0]
    assert "[k2]" in md.split("## 参考文献")[0]
    assert [r["key"] for r in references] == ["k1", "k2"]
    assert [r["object_id"] for r in references] == ["rel-ab", "rel-bc"]
    assert [r["object_type"] for r in references] == ["relation", "relation"]
    assert [r["name"] for r in references] == [
        "A --derived_from--> B", "B --derived_from--> C"]
    assert [r["label"] for r in references] == [
        "同一篇论文 · A --derived_from--> B",
        "同一篇论文 · B --derived_from--> C",
    ]
    assert [r["location_label"] for r in references] == ["§1", "§2"]
    assert [r["tier"] for r in references] == ["base", "personal"]


def test_assemble_no_citations_omits_references(repo):
    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _OutlineLLM())
    outline = [{"title": "A", "scope": "s", "sub_queries": ["q"]}]
    sections = [{"title": "A", "scope": "s", "grounded": False,
                 "markdown": "## A\n全是【通识】x。", "id_map": {},
                 "attempted": [], "top_concepts": []}]
    md, gaps, references = eng._assemble(nb.id, rid := repo.create_report(nb.id, "q"),
                                         "q", outline, sections)
    assert references == [] and "## 参考文献" not in md


# ---------------------------------------------------------------------------
# Task 2(perf): depth 穿透 + 模型工作负载并行度 + 节内实时进度
# ---------------------------------------------------------------------------

def test_run_sections_concurrency_caps_model_parallelism_for_database(repo, monkeypatch):
    """模型并行度是上限；单报告数据库扇出另受 report section gate 限制。"""
    original_parallelism = repo._runtime.models.parallelism
    monkeypatch.setattr(
        repo._runtime.models, "parallelism",
        lambda workload_id: 5 if workload_id == "report_section"
        else original_parallelism(workload_id),
    )
    eng = _mk_engine(repo, _OutlineLLM())
    # Make the database fan-out ceiling explicit.  The production default is
    # intentionally larger and may change independently of this three-party
    # rendezvous; leaving it implicit once created a fourth worker that waited
    # alone for the barrier timeout while the test happened to stay green.
    monkeypatch.setattr(eng.settings, "postgres_pool_max_size", 5)
    seen = {"max": 0, "cur": 0}
    import threading as _t
    lk = _t.Lock()
    saturated = _t.Event()
    release = _t.Event()
    from app.services.reasoning_retrieval import ReasoningResult
    def _dd(nb_id, section, question, depth=None, on_step=None):
        with lk:
            seen["cur"] += 1
            seen["max"] = max(seen["max"], seen["cur"])
            if seen["cur"] == 3:
                saturated.set()
        try:
            assert release.wait(timeout=5)
            return ReasoningResult()
        finally:
            with lk: seen["cur"] -= 1
    monkeypatch.setattr(eng, "_deep_dive", _dd)
    nb = _mk_nb(repo); rid = repo.create_report(nb.id, "q")
    outline = [{"title": f"S{i}", "scope": "s", "sub_queries": ["q"]} for i in range(4)]
    # Drive the synchronous orchestrator from a controller thread so the test
    # can release the first wave after it has observed the exact cap.  A cyclic
    # barrier strands the fourth section in a second generation and turns this
    # assertion into a five-second timeout-dependent pass.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as controller:
        future = controller.submit(
            eng._run_sections, nb.id, rid, outline, "q", 2
        )
        assert saturated.wait(timeout=5)
        release.set()
        future.result(timeout=5)
    assert seen["max"] == 3


def test_detailed_report_retrieves_all_sections_then_synthesizes_once_before_writing(
    repo, monkeypatch,
):
    """The report-wide barrier is the anti-stitching contract, not prompt advice."""
    from types import SimpleNamespace

    eng = _mk_engine(repo, _OutlineLLM())
    monkeypatch.setattr(
        eng.dependencies.model_clients, "parallelism", lambda _workload: 3
    )
    events = []
    import threading as _t
    barrier = _t.Barrier(3, timeout=5)
    lock = _t.Lock()

    def _deep(_nb, section, _question, depth=None, on_step=None):
        barrier.wait()
        with lock:
            events.append(f"retrieve:{section['title']}")
        return SimpleNamespace(top_hits=[], elements=[], chunks=[])

    blueprint = {
        "central_answer": "one argument", "shared_definitions": [],
        "claims": [],
        "sections": [
            {"section_id": f"section-{i}", "thesis": title,
             "claim_ids": [], "must_contrast": [], "handoff": "",
             "do_not_repeat": []}
            for i, title in enumerate(("A", "B", "C"), 1)
        ],
    }

    def _synthesize(outline, results, question, frame):
        assert len([event for event in events if event.startswith("retrieve:")]) == 3
        events.append("synthesis")
        return blueprint

    def _draft(_nb, section, _question, _result, depth=None, **kwargs):
        assert "synthesis" in events
        assert kwargs["synthesis"]["section"]["thesis"] == section["title"]
        with lock:
            events.append(f"draft:{section['title']}")
        return {"title": section["title"], "scope": "s", "markdown": "## x",
                "grounded": False, "id_map": {}, "claims": [],
                "claim_ledger_status": "missing"}

    monkeypatch.setattr(eng, "_deep_dive", _deep)
    monkeypatch.setattr(eng, "_synthesize_report_blueprint", _synthesize)
    monkeypatch.setattr(eng, "_draft_section", _draft)
    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "q")
    outline = [
        {"title": title, "scope": "s", "sub_queries": [title]}
        for title in ("A", "B", "C")
    ]
    sections = eng._run_sections(nb.id, rid, outline, "q", depth=8)
    assert events.count("synthesis") == 1
    assert max(events.index(event) for event in events if event.startswith("retrieve:")) < events.index("synthesis")
    assert events.index("synthesis") < min(
        events.index(event) for event in events if event.startswith("draft:")
    )
    assert sections[0]["_synthesis_blueprint"] == blueprint


def test_standard_report_adds_no_report_wide_model_call(repo, monkeypatch):
    from types import SimpleNamespace

    eng = _mk_engine(repo, _OutlineLLM())
    monkeypatch.setattr(
        eng, "_deep_dive",
        lambda *args, **kwargs: SimpleNamespace(top_hits=[], elements=[], chunks=[]),
    )
    monkeypatch.setattr(
        eng, "_synthesize_report_blueprint",
        lambda *args, **kwargs: pytest.fail("standard depth must not synthesize"),
    )
    monkeypatch.setattr(
        eng, "_draft_section",
        lambda _nb, section, *_args, **_kwargs: {
            "title": section["title"], "scope": "s", "markdown": "## x",
            "grounded": False, "id_map": {}, "claims": [],
            "claim_ledger_status": "missing",
        },
    )
    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "q")
    sections = eng._run_sections(
        nb.id, rid, [{"title": "A", "scope": "s", "sub_queries": ["A"]}],
        "q", depth=2,
    )
    assert "_synthesis_blueprint" not in sections[0]


def _record_retrieve_and_draft(eng, monkeypatch, events, draft_kwargs=None):
    from types import SimpleNamespace

    def _deep(_nb, section, *_args, **_kwargs):
        events.append(f"retrieve:{section['title']}")
        return SimpleNamespace(top_hits=[], elements=[], chunks=[])

    def _draft(_nb, section, *_args, **kwargs):
        events.append(f"draft:{section['title']}")
        if draft_kwargs is not None:
            draft_kwargs.append(kwargs)
        return {"title": section["title"], "scope": "s", "markdown": "## x",
                "grounded": False, "id_map": {}, "claims": [],
                "claim_ledger_status": "missing"}

    monkeypatch.setattr(eng, "_deep_dive", _deep)
    monkeypatch.setattr(eng, "_draft_section", _draft)


def test_multi_section_report_retrieves_everything_before_drafting_at_any_depth(
    repo, monkeypatch
):
    """低档不再逐节流水线:综合读的是全篇证据,所以撰写必须等在检索屏障之后。

    这条曾经断言相反的行为(标准档先写完 A 再检索 B)。取消低档流水线是明确的
    产品决定:每份多节报告都换到「检索屏障 → 一次全篇综合 → 并行撰写」。
    """
    eng = _mk_engine(repo, _OutlineLLM())
    events, draft_kwargs = [], []
    _record_retrieve_and_draft(eng, monkeypatch, events, draft_kwargs)
    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "q")

    sections = eng._run_sections(
        nb.id, rid,
        [{"title": "A", "scope": "s"}, {"title": "B", "scope": "s"}],
        "q", depth=2,
    )

    assert [section["title"] for section in sections] == ["A", "B"]
    # depth=2 曾经完全不请求综合;现在章节数才是判据。
    assert all(row["synthesis_requested"] is True for row in draft_kwargs)
    last_retrieval = max(
        index for index, event in enumerate(events)
        if event.startswith("retrieve:")
    )
    first_draft = min(
        index for index, event in enumerate(events) if event.startswith("draft:")
    )
    assert last_retrieval < first_draft


def test_single_section_report_keeps_the_pipeline_and_skips_synthesis(
    repo, monkeypatch
):
    """一节报告没有跨章节一致性可综合,所以它保留流水线、不付那次调用。"""
    llm = _OutlineLLM()
    llm.synthesis_calls = 0
    original_chat_json = llm.chat_json

    def _counting_chat_json(messages, schema_hint, **kwargs):
        if "EVIDENCE SYNTHESIZER" in messages[-1]["content"]:
            llm.synthesis_calls += 1
        return original_chat_json(messages, schema_hint, **kwargs)

    llm.chat_json = _counting_chat_json
    eng = _mk_engine(repo, llm)
    events, draft_kwargs = [], []
    _record_retrieve_and_draft(eng, monkeypatch, events, draft_kwargs)
    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "q")

    eng._run_sections(nb.id, rid, [{"title": "A", "scope": "s"}], "q", depth=16)

    assert llm.synthesis_calls == 0
    assert draft_kwargs[0]["synthesis_requested"] is False
    assert events == ["retrieve:A", "draft:A"]


def test_malformed_report_synthesis_fails_open_to_independent_drafting(repo, monkeypatch):
    from types import SimpleNamespace

    class _MalformedBlueprintLLM(_OutlineLLM):
        def __init__(self):
            super().__init__()
            self.synthesis_calls = 0

        def chat_json(self, messages, schema_hint, **kwargs):
            if "EVIDENCE SYNTHESIZER" in messages[-1]["content"]:
                self.synthesis_calls += 1
                return '{"claims":[{"evidence_keys":["invented"]}]}'
            return super().chat_json(messages, schema_hint, **kwargs)

    llm = _MalformedBlueprintLLM()
    eng = _mk_engine(repo, llm)
    notes = []
    events = []
    repo._runtime.models.note_model_error = (
        lambda stage, error, *, workload_id: notes.append((stage, workload_id))
    )
    monkeypatch.setattr(
        eng.dependencies.event_log, "emit", lambda event: events.append(event)
    )
    hit = SimpleNamespace(
        object_id="o1", object_type="Claim", relevance=0.9,
        payload={"name": "claim", "definition": "evidence"},
    )
    monkeypatch.setattr(
        eng, "_deep_dive",
        lambda *args, **kwargs: SimpleNamespace(
            top_hits=[hit], elements=[], chunks=[]
        ),
    )
    draft_options = []

    def _draft(_nb, section, *_args, **kwargs):
        draft_options.append(kwargs)
        return {"title": section["title"], "scope": "s", "markdown": "## x",
                "grounded": False, "id_map": {}, "claims": [],
                "claim_ledger_status": "missing"}

    monkeypatch.setattr(eng, "_draft_section", _draft)
    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "q")
    sections = eng._run_sections(
        nb.id, rid,
        [{"title": "A", "scope": "s", "sub_queries": ["A"]},
         {"title": "B", "scope": "s", "sub_queries": ["B"]}],
        "q", depth=8,
    )
    assert llm.synthesis_calls == 1
    assert "synthesis" not in draft_options[0]
    assert "_synthesis_blueprint" not in sections[0]
    assert sections[0]["_synthesis_status"] == "failed_validation"
    assert ("report_synthesis", "report_summary") in notes
    # By kind, not by position: stage-timing events also land here, and the
    # failure signal must not depend on being the last one emitted.
    assert any(event["kind"] == "report_synthesis_failed" for event in events)


def test_facet_tag_repairs_are_observed_and_stripped_before_model_context(
    repo, monkeypatch,
):
    """Repair counts are internal: observable, never part of a model prompt."""
    from types import SimpleNamespace

    eng = _mk_engine(repo, _OutlineLLM())
    events = []
    monkeypatch.setattr(
        eng.dependencies.event_log, "emit", lambda event: events.append(event)
    )
    monkeypatch.setattr(
        eng, "_deep_dive",
        lambda *args, **kwargs: SimpleNamespace(top_hits=[], elements=[], chunks=[]),
    )
    blueprint = {
        "version": 1, "central_answer": "one argument",
        "shared_definitions": [],
        "claims": [{
            "id": "c1", "statement": "s", "type": "general", "facet_id": "mixer",
            "evidence_keys": [], "counterevidence_keys": [], "conditions": [],
            "owner_section_id": "section-1",
        }],
        "sections": [
            {"section_id": f"section-{index}", "thesis": "t",
             "claim_ids": ["c1"] if index == 1 else [],
             "must_contrast": [], "handoff": "", "do_not_repeat": []}
            for index in (1, 2)
        ],
        "_facet_tag_stats": {"repaired": 2, "cleared": 1},
    }
    monkeypatch.setattr(
        eng, "_synthesize_report_blueprint",
        lambda *args, **kwargs: (blueprint, "available", None),
    )
    def _draft(_nb, section, *_args, **kwargs):
        return {"title": section["title"], "scope": "s", "markdown": "## x",
                "grounded": False, "id_map": {}, "claims": [],
                "claim_ledger_status": "missing"}

    monkeypatch.setattr(eng, "_draft_section", _draft)
    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "q")
    sections = eng._run_sections(
        nb.id, rid,
        [{"title": "A", "scope": "s", "sub_queries": ["A"]},
         {"title": "B", "scope": "s", "sub_queries": ["B"]}],
        "q", depth=8,
    )

    tagged = [
        event for event in events
        if event["kind"] == "report_synthesis_facet_tags"
    ]
    assert tagged == [{
        "kind": "report_synthesis_facet_tags",
        "report_id": rid, "repaired": 2, "cleared": 1,
    }]
    # The stored blueprint feeds fair_editor_context, which serializes it
    # wholesale into the editor prompt — that is the one real leak surface.
    # Drafting needs no assertion: blueprint_for_section rebuilds a
    # whitelisted dict, so a private key cannot reach section writers.
    assert "_facet_tag_stats" not in sections[0]["_synthesis_blueprint"]


def test_report_stages_use_their_independent_output_budgets(repo):
    from types import SimpleNamespace
    from app.services.reasoning_retrieval import ReasoningResult

    class _BudgetClient:
        configured = True

        def __init__(self):
            self.settings = repo.settings
            self.calls = []

        def chat_json(self, messages, schema_hint, **kwargs):
            self.calls.append((messages[-1]["content"], kwargs.get("max_tokens")))
            if "write ONE section" in messages[-1]["content"]:
                return json.dumps({
                    "markdown": "## A\nbody", "grounded": False, "claims": [],
                })
            if "EVIDENCE SYNTHESIZER" in messages[-1]["content"]:
                return json.dumps({
                    "central_answer": "a", "shared_definitions": [],
                    "claims": [],
                    "sections": [{
                        "section_id": "section-1", "thesis": "t",
                        "claim_ids": [], "must_contrast": [], "handoff": "",
                        "do_not_repeat": [],
                    }],
                })
            return json.dumps({"summary": "s", "coverage": [], "contradictions": []})

    client = _BudgetClient()
    _bind_report_llm(repo, client)
    eng = _mk_engine(repo, client)
    nb = _mk_nb(repo)
    eng._draft_section(
        nb.id, {"title": "A", "scope": "s", "sub_queries": ["A"]},
        "q", ReasoningResult(), depth=1,
    )
    hit = SimpleNamespace(
        object_id="o1", object_type="Claim", relevance=0.9,
        payload={"name": "n", "definition": "e"},
    )
    outline = [{"title": "A", "scope": "s", "sub_queries": ["A"]}]
    blueprint, status, error = eng._synthesize_report_blueprint(
        outline,
        [SimpleNamespace(top_hits=[hit], elements=[], chunks=[])],
        "q", None,
    )
    assert blueprint and status == "available" and error is None
    rid = repo.create_report(nb.id, "q")
    eng._assemble(
        nb.id, rid, "q", outline,
        [{"title": "A", "scope": "s", "markdown": "## A\nbody",
          "grounded": False, "id_map": {}}],
    )

    assert [budget for _prompt, budget in client.calls] == [65536, 102400, 102400]


def test_synthesize_blueprint_passes_frame_facet_ids_to_synthesis_prompt(repo):
    """接线守卫:漏传 facet_ids 就该让这条测试变红(见变异验证)。"""
    from types import SimpleNamespace
    from app.services.report_synthesis import normalize_report_frame

    class _FacetProbeClient:
        configured = True

        def __init__(self):
            self.settings = repo.settings
            self.calls = []

        def chat_json(self, messages, schema_hint, **kwargs):
            content = messages[-1]["content"]
            self.calls.append(content)
            if "EVIDENCE SYNTHESIZER" in content:
                return json.dumps({
                    "central_answer": "a", "shared_definitions": [],
                    "claims": [],
                    "sections": [{
                        "section_id": "section-1", "thesis": "t",
                        "claim_ids": [], "must_contrast": [], "handoff": "",
                        "do_not_repeat": [],
                    }],
                })
            return json.dumps({"summary": "s", "coverage": [], "contradictions": []})

    client = _FacetProbeClient()
    _bind_report_llm(repo, client)
    eng = _mk_engine(repo, client)
    outline = [{"title": "A", "scope": "s", "sub_queries": ["A"]}]
    hit = SimpleNamespace(
        object_id="o1", object_type="Claim", relevance=0.9,
        payload={"name": "n", "definition": "e"},
    )
    results = [SimpleNamespace(top_hits=[hit], elements=[], chunks=[])]

    frame = normalize_report_frame({
        "subject_kind": "模型实例",
        "facets": [{
            "id": "mixer", "name": "序列建模机制",
            "values": ["Attention"], "exclusive": True,
        }],
    })
    blueprint, status, error = eng._synthesize_report_blueprint(
        outline, results, "q", frame,
    )
    assert blueprint and status == "available" and error is None
    synthesis_prompt = next(c for c in client.calls if "EVIDENCE SYNTHESIZER" in c)
    assert "legal facet ids are exactly" in synthesis_prompt
    assert "`mixer`" in synthesis_prompt

    client.calls.clear()
    blueprint2, status2, error2 = eng._synthesize_report_blueprint(
        outline, results, "q", None,
    )
    assert blueprint2 and status2 == "available" and error2 is None
    synthesis_prompt2 = next(c for c in client.calls if "EVIDENCE SYNTHESIZER" in c)
    assert "legal facet ids" not in synthesis_prompt2


def test_detailed_report_without_evidence_discloses_skip_without_model_error(repo, monkeypatch):
    from types import SimpleNamespace

    eng = _mk_engine(repo, _OutlineLLM())
    notes = []
    repo._runtime.models.note_model_error = (
        lambda stage, error, *, workload_id: notes.append((stage, workload_id))
    )
    monkeypatch.setattr(
        eng, "_deep_dive",
        lambda *args, **kwargs: SimpleNamespace(top_hits=[], elements=[], chunks=[]),
    )
    monkeypatch.setattr(
        eng, "_draft_section",
        lambda _nb, section, *_args, **_kwargs: {
            "title": section["title"], "scope": "s", "markdown": "## x",
            "grounded": False, "id_map": {}, "claims": [],
            "claim_ledger_status": "missing",
        },
    )
    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "q")
    sections = eng._run_sections(
        nb.id, rid,
        [{"title": "A", "scope": "s"}, {"title": "B", "scope": "s"}],
        "q", depth=8,
    )
    assert sections[0]["_synthesis_status"] == "skipped_no_evidence"
    assert notes == []


def test_run_sections_writes_section_status(repo, monkeypatch):
    """每节完成后 section_status 落库,各节 phase=完成。"""
    eng = _mk_engine(repo, _OutlineLLM())
    from app.services.reasoning_retrieval import ReasoningResult
    monkeypatch.setattr(eng, "_deep_dive", lambda *a, **k: ReasoningResult())
    nb = _mk_nb(repo); rid = repo.create_report(nb.id, "q")
    outline = [{"title": "A", "scope": "s", "sub_queries": ["q"]},
               {"title": "B", "scope": "s", "sub_queries": ["q"]}]
    eng._run_sections(nb.id, rid, outline, "q", depth=2)
    detail = repo.get_report(nb.id, rid)
    assert len(detail["section_status"]) == 2
    assert all(x["phase"] == "完成" for x in detail["section_status"])


def test_run_sections_stale_snapshot_never_overwrites_newer(repo, monkeypatch):
    """并发落库:取快照的顺序必须等于写库的顺序,陈旧快照不得覆盖新快照。

    确定性复现:B 节故意慢 → A 先完成,A 的收尾写就是首个「部分完成」快照。
    卡住这一写(模拟取快照后被调度走),放 B 跑完写下全完成快照;若写在锁外,
    A 醒来会用陈旧快照盖掉它 —— 而 _run_sections 之后再没人写 section_status,
    这份陈旧快照会永久留库(报告已完成,进度视图却停在「规划」)。
    """
    import threading
    import time as _time
    eng = _mk_engine(repo, _OutlineLLM())
    from app.services.reasoning_retrieval import ReasoningResult

    def _dd(notebook_id, section, question, depth, on_step):
        if section["title"] == "B":
            _time.sleep(0.05)           # 保证 A 先完成
        return ReasoningResult()
    monkeypatch.setattr(eng, "_deep_dive", _dd)

    store = eng.dependencies.reports
    real_update = store.update_report
    all_done = threading.Event()        # 全完成快照已落库
    gate = threading.Lock()
    stalled = [False]                   # 只卡首个「部分完成」快照一次

    def _racy_update(nb_id, r_id, **kw):
        snap = kw.get("section_status")
        if snap:
            n_done = sum(1 for x in snap if x["phase"] == "完成")
            if n_done == len(snap):
                all_done.set()
            elif n_done:
                with gate:
                    first, stalled[0] = not stalled[0], True
                if first:
                    all_done.wait(0.5)  # 写在锁内时无人能推进 → 超时后照常写
        return real_update(nb_id, r_id, **kw)

    monkeypatch.setattr(store, "update_report", _racy_update)
    nb = _mk_nb(repo); rid = repo.create_report(nb.id, "q")
    outline = [{"title": "A", "scope": "s", "sub_queries": ["q"]},
               {"title": "B", "scope": "s", "sub_queries": ["q"]}]
    eng._run_sections(nb.id, rid, outline, "q", depth=2)
    detail = repo.get_report(nb.id, rid)
    assert all(x["phase"] == "完成" for x in detail["section_status"])
    # progress 与 section_status 同源同写,一并守住(此处不经 generate 覆盖)
    assert detail["progress"].startswith("章节 2/2 完成")


# ---------------------------------------------------------------------------
# Task 1(STORM): Corpus map 0-LLM 语料侦察(来源 + KG + chunk 路径)
# ---------------------------------------------------------------------------

def test_build_corpus_map_grounds_on_corpus(repo, monkeypatch):
    from app.services.report_engine import ReportEngine
    from app.services.retrieval import RetrievedKnowledge, RetrievedChunk
    nb = _mk_nb(repo)
    eng = ReportEngine.from_repository(repo, repo.settings)
    # 来源
    with repo._write() as db:
        db.execute("INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,created_at,updated_at)"
                   " VALUES('s1',?, 'Razavi Analog CMOS','file','uploaded','parsed',?,?)",
                   (nb.id, "2026", "2026"))
    def _fed(active, q):
        h = RetrievedKnowledge(object_id="ko1", object_type="concept", payload={"name": "Bandgap Reference"})
        h.tier = "base"; h.notebook_id = "nb-base"
        return [h]
    def _ppr(nbid, q):
        return [RetrievedChunk(chunk_id="c1", source_id="s2", source_title="Gray & Meyer",
                               section_path="§11.2", text="……很长的正文不该进 map……")]
    monkeypatch.setattr(repo.retrieval, "federated_retrieve", _fed)
    monkeypatch.setattr(repo.retrieval, "ppr_retrieve", _ppr)
    m = eng._build_corpus_map(nb.id, "why is bandgap 1.2V")
    assert "Razavi Analog CMOS" in m            # 来源标题
    assert "Bandgap Reference" in m and "[base]" in m   # KG + tier
    assert "Gray & Meyer" in m and "§11.2" in m         # chunk 来源·路径
    assert "不该进 map" not in m                # 不含 chunk 正文
    assert len(m) <= 4000


@pytest.mark.parametrize("cancelled_channel", ["knowledge", "ppr"])
def test_build_corpus_map_propagates_retrieval_cancellation(
    repo, monkeypatch, cancelled_channel,
):
    from app.services.cancellation import AskCancelled
    from app.services.report_engine import ReportEngine

    eng = ReportEngine.from_repository(repo, repo.settings)
    monkeypatch.setattr(eng, "_probe_knowledge_hits", lambda *_args: [])
    monkeypatch.setattr(repo.retrieval, "ppr_retrieve", lambda *_args: [])
    if cancelled_channel == "knowledge":
        monkeypatch.setattr(
            eng, "_probe_knowledge_hits",
            lambda *_args: (_ for _ in ()).throw(AskCancelled()),
        )
    else:
        monkeypatch.setattr(
            repo.retrieval, "ppr_retrieve",
            lambda *_args: (_ for _ in ()).throw(AskCancelled()),
        )

    with pytest.raises(AskCancelled):
        eng._build_corpus_map("nb", "q", profile={})


# ---------------------------------------------------------------------------
# Task 2(STORM): 多视角预写作大纲 prompt(接地 + 张力 + MECE)
# ---------------------------------------------------------------------------

def test_storm_outline_prompt_contract():
    from app.services.prompts import report_storm_outline_prompt, REPORT_STORM_SCHEMA_HINT
    p = report_storm_outline_prompt(
        "Q问题", "CORPUSMAP内容", max_sections=5, history_block="H历史",
        intent_block="INTENT-CONTRACT", coverage_block="COVERAGE-PROBE",
    )
    for kw in ("expert perspectives", "raise", "cluster", "tension", "MECE",
               "vocabulary", "CORPUSMAP内容", "Q问题", "H历史", "3-5"):
        assert kw in p
    assert "perspectives" in REPORT_STORM_SCHEMA_HINT and "tensions" in REPORT_STORM_SCHEMA_HINT
    assert "intent_ids" in REPORT_STORM_SCHEMA_HINT
    assert "higher priority than the corpus" in p
    assert "INTENT-CONTRACT" in p and "COVERAGE-PROBE" in p


# ---------------------------------------------------------------------------
# Task 3(STORM): 充分性探针(0-LLM 命中数)+ Judge prompt
# ---------------------------------------------------------------------------

def test_probe_sufficiency_counts_hits(repo, monkeypatch):
    from app.services.report_engine import ReportEngine
    from app.services.retrieval import RetrievedKnowledge
    eng = ReportEngine.from_repository(repo, repo.settings)
    def _fed(active, q):
        h = RetrievedKnowledge(object_id="k-"+q, object_type="concept", payload={})
        h.notebook_id = "nb-base" if "base" in q else "nb-x"; h.tier="base" if "base" in q else "personal"
        return [h]
    monkeypatch.setattr(repo.retrieval, "federated_retrieve", _fed)
    from app.services.retrieval import RetrievedElement
    monkeypatch.setattr(
        repo.retrieval, "retrieve_elements",
        lambda active, q, limit=8: [RetrievedElement(
            element_id="el-" + q, source_id="src-" + q,
            source_title="S", location_label="p1", element_type="text", text=q,
        )],
    )
    out = eng._probe_sufficiency("nb", [{"title":"A","sub_queries":["base-x","y"]},
                                        {"title":"B","sub_queries":[]}])
    assert out[0]["title"]=="A" and out[0]["hits"]==2 and out[0]["base_hits"]==1
    assert out[0]["element_hits"] == 2 and out[0]["source_hits"] == 2
    assert out[1]["hits"]==0


def test_outline_binding_cannot_drop_mandatory_user_topic(repo):
    from app.services.report_engine import ReportEngine

    eng = ReportEngine.from_repository(repo, repo.settings)
    contract = {
        "mandatory_topics": [
            {"id": "intent-1", "title": "机理", "question": "解释工作机理",
             "retrieval_queries": ["mechanism"]},
            {"id": "intent-2", "title": "失效", "question": "列出失效边界",
             "retrieval_queries": ["failure boundary"]},
        ]
    }
    # 模型被语料带偏，只产出一个无 intent 绑定的“历史综述”。
    sections = [{"title": "历史综述", "scope": "沿革", "sub_queries": ["history"],
                 "intent_ids": [], "perspectives": [], "tensions": []}]
    probe = [
        {"intent_id": "intent-1", "title": "机理", "hits": 2, "base_hits": 1,
         "element_hits": 3, "source_hits": 2},
        {"intent_id": "intent-2", "title": "失效", "hits": 0, "base_hits": 0,
         "element_hits": 0, "source_hits": 0},
    ]
    bound = eng._bind_outline_to_intent(sections, contract, probe)
    assert {item for section in bound for item in section["intent_ids"]} == {
        "intent-1", "intent-2"
    }
    assert any(section["scope"] == "列出失效边界" for section in bound)
    assert all(section["intent_catalog"] == contract["mandatory_topics"] for section in bound)

def test_sufficiency_prompt_contract():
    from app.services.prompts import report_sufficiency_prompt, REPORT_SUFFICIENCY_SCHEMA_HINT
    p = report_sufficiency_prompt("Q", "PROBEBLOCK")
    assert "sufficiency" in p and "PROBEBLOCK" in p and "Q" in p
    assert "gap_note" in REPORT_SUFFICIENCY_SCHEMA_HINT and "action" in REPORT_SUFFICIENCY_SCHEMA_HINT


# ---------------------------------------------------------------------------
# Task 4(STORM): plan_outline 编排(map→STORM→探针→Judge→富大纲→outline_ready)
# ---------------------------------------------------------------------------

def test_run_stops_at_intent_review_before_any_corpus_access(repo, monkeypatch):
    from app.services.report_engine import ReportEngine

    class _IntentLLM:
        configured = True

        def chat_json(self, messages, schema_hint, **kwargs):
            return json.dumps({
                "normalized_question": "PLL 环路稳定性的机理与设计约束是什么？",
                "intent_type": "explain",
                "entities": ["PLL"],
                "mandatory_topics": [{
                    "title": "环路稳定性",
                    "question": "PLL 环路稳定性的机理与设计约束是什么？",
                    "retrieval_queries": ["PLL loop stability"],
                }],
                "constraints": ["面向电路设计"],
                "assumptions": ["讨论线性锁定附近"],
                "confidence": 0.92,
                "needs_clarification": False,
                "ambiguities": [],
            })

    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _IntentLLM())
    for name in ("federated_retrieve", "retrieve_elements", "ppr_retrieve"):
        monkeypatch.setattr(
            repo.retrieval,
            name,
            lambda *args, _name=name, **kwargs: pytest.fail(
                f"{_name} must not run before intent confirmation"
            ),
        )
    rid = repo.create_report(nb.id, "分析 PLL 稳定性")
    eng.run(
        nb.id, rid, "分析 PLL 稳定性", require_intent_review=True
    )

    detail = repo.get_report(nb.id, rid)
    assert detail["status"] == "intent_ready"
    assert detail["understanding"]["resolved_question"].startswith("PLL")
    assert detail["understanding"]["confirmed"] is False
    assert detail["understanding"]["needs_clarification"] is False


def test_prepare_intent_rechecks_cancellation_after_model_returns(repo):
    import threading
    from app.services.report_engine import ReportEngine

    cancel = threading.Event()

    class _LateCancelIntentLLM:
        configured = True

        def chat_json(self, *args, **kwargs):
            cancel.set()
            return json.dumps({
                "normalized_question": "不应发布的问题理解",
                "mandatory_topics": [{
                    "title": "主题", "question": "问题",
                    "retrieval_queries": ["query"],
                }],
                "ambiguities": [],
            })

    nb = _mk_nb(repo)
    _bind_report_llm(repo, _LateCancelIntentLLM())
    eng = ReportEngine.from_repository(
        repo, repo.settings, cancel_event=cancel
    )
    rid = repo.create_report(nb.id, "q")

    eng.prepare_intent(nb.id, rid, "q")

    detail = repo.get_report(nb.id, rid)
    assert detail["status"] == "cancelled"
    assert detail["understanding"] == {}


def test_vague_or_unresolved_report_question_requires_clarification(repo):
    class _IntentLLM:
        configured = True

        def chat_json(self, *args, **kwargs):
            return json.dumps({
                "normalized_question": "分析这个问题",
                "intent_type": "other",
                "mandatory_topics": [],
                "ambiguities": [],
                "confidence": 0.8,
                "needs_clarification": False,
            })

    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _IntentLLM())
    rid = repo.create_report(nb.id, "分析一下这个问题")
    eng.run(
        nb.id, rid, "分析一下这个问题", require_intent_review=True
    )

    understanding = repo.get_report(nb.id, rid)["understanding"]
    assert understanding["needs_clarification"] is True
    assert understanding["ambiguities"][0]["id"] == "ambiguity-input"
    assert "对象" in understanding["ambiguities"][0]["question"]


def test_confirmed_clarification_answers_are_part_of_the_research_question():
    from app.services.report_engine import ReportEngine

    effective = ReportEngine._confirmed_research_question({
        "resolved_question": "分析这个问题",
        "clarification_answers": [{
            "id": "ambiguity-input",
            "question": "具体研究对象是什么？",
            "answer": "对象是电荷泵 PLL",
        }],
    }, "fallback")

    assert effective.startswith("分析这个问题")
    assert "具体研究对象是什么" in effective
    assert "电荷泵 PLL" in effective


def test_confirming_intent_freezes_every_reviewed_contract_field(repo, monkeypatch):
    from app.services.report_engine import ReportEngine

    eng = ReportEngine.from_repository(repo, repo.settings)
    reviewed = {
        "objective": "比较 A 和 B",
        "resolved_question": "比较 A 和 B 的性能",
        "mandatory_topics": [{
            "id": "intent-1", "title": "延迟", "question": "延迟如何？",
            "retrieval_queries": ["A B latency"],
        }],
        "comparison_axes": ["延迟"],
        "constraints": ["同工艺"],
        "excluded_topics": ["成本"],
        "ambiguities": [{"id": "ambiguity-1", "question": "哪种负载？"}],
        "confirmed_input": {
            "resolved_question": "比较 A 和 B 的性能与功耗",
            "answers": [{
                "id": "ambiguity-1", "question": "哪种负载？",
                "answer": "10 pF",
            }],
        },
    }
    monkeypatch.setattr(
        eng, "_plan_intent_contract",
        lambda *args, **kwargs: pytest.fail("confirmation must not reinterpret"),
    )

    frozen = eng._finalize_confirmed_intent(reviewed)

    for field in (
        "mandatory_topics", "comparison_axes", "constraints", "excluded_topics"
    ):
        assert frozen[field] == reviewed[field]
    assert frozen["resolved_question"] == "比较 A 和 B 的性能与功耗"
    assert frozen["clarification_answers"][0]["answer"] == "10 pF"
    assert frozen["confirmed"] is True
    assert "confirmed_input" not in frozen


def test_intent_coverage_probe_includes_confirmed_question_and_answers(repo, monkeypatch):
    from app.services.report_engine import ReportEngine

    eng = ReportEngine.from_repository(repo, repo.settings)
    observed = []
    monkeypatch.setattr(
        eng,
        "_load_probe_query_results",
        lambda notebook_id, query_groups, max_queries=4:
        observed.extend(query_groups) or [[([], [])] for _ in query_groups],
    )
    contract = {
        "objective": "分析这个问题",
        "resolved_question": "分析 PLL 稳定性",
        "clarification_answers": [{
            "id": "ambiguity-input",
            "question": "具体研究对象是什么？",
            "answer": "电荷泵 PLL",
        }],
        "mandatory_topics": [{
            "id": "intent-1", "title": "旧主题", "question": "分析什么？",
            "retrieval_queries": ["分析这个问题"],
        }],
    }

    eng._probe_intent_coverage("nb", contract)

    assert len(observed) == 1
    assert "分析 PLL 稳定性" in observed[0][0]
    assert "电荷泵 PLL" in observed[0][0]
    assert "分析这个问题" in observed[0]

def test_plan_outline_produces_enriched_outline_ready(repo, monkeypatch):
    from app.services.report_engine import ReportEngine
    nb = _mk_nb(repo)
    class _LLM:
        configured = True
        def chat_json(self, messages, schema_hint, **kw):
            c = messages[-1]["content"]
            if "PRE-WRITING" in c or "expert PERSPECTIVES" in c:
                return json.dumps({"sections":[{"title":"机理","scope":"s","sub_queries":["bandgap"],
                                                "perspectives":["领域专家"],"tensions":[]}]})
            if "ENOUGH evidence" in c:
                return json.dumps({"verdicts":[{"title":"机理","sufficiency":"薄弱",
                                                "gap_note":"缺实测","action":"supplement"}]})
            return "{}"
    _bind_report_llm(repo, _LLM())
    monkeypatch.setattr(ReportEngine, "_build_corpus_map", lambda self,n,q: "MAP")
    monkeypatch.setattr(repo.retrieval, "federated_retrieve", lambda a,q: [])
    eng = ReportEngine.from_repository(repo, repo.settings)
    # depth=4(deep 档):充分性 LLM 精修在该档运行——本测试钉的正是精修语义
    # (「薄弱」覆盖确定性「缺失」)。overview/standard 跳过 LLM 半,由
    # test_sufficiency_llm_skipped_at_low_tiers 另行钉住。
    rid = repo.create_report(nb.id, "why bandgap 1.2V", depth=4)
    eng.plan_outline(nb.id, rid, "why bandgap 1.2V")
    d = repo.get_report(nb.id, rid)
    assert d["status"] == "outline_ready"
    sec = d["outline"][0]
    assert sec["title"]=="机理" and sec["perspectives"]==["领域专家"]
    assert sec["sufficiency"]=="薄弱" and sec["action"]=="supplement"


def test_storm_comparison_frame_is_bounded_and_attached_to_the_outline(repo):
    class _FrameLLM(_OutlineLLM):
        def chat_json(self, messages, schema_hint, **kwargs):
            if "PRE-WRITING" in messages[-1]["content"]:
                return json.dumps({
                    "sections": [{"title": "比较", "scope": "同口径比较",
                                  "sub_queries": ["A", "B"],
                                  "intent_ids": ["intent-1"]}],
                    "frame": {
                        "subject_kind": "模型实例",
                        "facets": [{"id": "mixer", "name": "序列建模机制",
                                    "values": ["Attention", "SSM"],
                                    "exclusive": True}],
                        "axes": [{"id": "cost", "name": "效率",
                                  "condition_fields": ["上下文长度"]}],
                        "instance_policy": "实例可组合不同层级机制",
                    },
                })
            return super().chat_json(messages, schema_hint, **kwargs)

    eng = _mk_engine(repo, _FrameLLM())
    sections = eng._storm_outline(
        "nb", "compare A and B", "", "MAP",
        intent_contract={"intent_type": "compare", "entities": ["A", "B"]},
        intent_probe=[],
    )
    assert sections[0]["report_frame"]["facets"][0]["id"] == "mixer"


def test_plan_outline_freezes_intent_before_corpus_scout(repo, monkeypatch):
    from app.services.report_engine import ReportEngine

    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "用户原问题")
    eng = ReportEngine.from_repository(repo, repo.settings)
    calls = []
    contract = {
        "objective": "用户原问题",
        "mandatory_topics": [{
            "id": "intent-1", "title": "原问题", "question": "用户原问题",
            "retrieval_queries": ["original query"],
        }],
    }
    monkeypatch.setattr(
        eng, "_plan_intent_contract",
        lambda question, history: calls.append("intent") or contract,
    )
    monkeypatch.setattr(
        eng, "_probe_intent_coverage",
        lambda notebook_id, value, max_queries=4: calls.append("intent_probe") or [{
            "intent_id": "intent-1", "title": "原问题", "hits": 0,
            "base_hits": 0, "element_hits": 1, "source_hits": 1,
        }],
    )
    monkeypatch.setattr(
        eng, "_build_corpus_map",
        lambda notebook_id, question: calls.append("corpus") or "CORPUS",
    )
    monkeypatch.setattr(
        eng, "_storm_outline",
        lambda *args, **kwargs: calls.append("storm") or [{
            "title": "附近但不同的话题", "scope": "drift", "sub_queries": ["drift"],
            "intent_ids": [],
        }],
    )
    monkeypatch.setattr(eng, "_probe_sufficiency", lambda *args, **kwargs: [])
    monkeypatch.setattr(eng, "_judge_sufficiency", lambda question, sections, probe, use_llm=True: sections)
    eng.plan_outline(nb.id, rid, "用户原问题")
    assert calls == ["intent", "intent_probe", "corpus", "storm"]
    outline = repo.get_report(nb.id, rid)["outline"]
    assert outline[0]["intent_ids"] == ["intent-1"]
    assert outline[0]["intent_contract"]["objective"] == "用户原问题"


def test_plan_outline_records_corpus_profile_failure_instead_of_silently_hiding_it(
    repo, monkeypatch
):
    from app.services.report_engine import ReportEngine

    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "用户原问题")
    eng = ReportEngine.from_repository(repo, repo.settings)
    events = []
    monkeypatch.setattr(eng.dependencies.event_log, "emit", events.append)
    monkeypatch.setattr(
        eng, "_plan_intent_contract",
        lambda *_args, **_kwargs: {
            "objective": "用户原问题",
            "mandatory_topics": [{
                "id": "intent-1", "title": "原问题", "question": "用户原问题",
                "retrieval_queries": ["用户原问题"],
            }],
        },
    )
    monkeypatch.setattr(
        eng, "_corpus_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bad SQL")),
    )
    monkeypatch.setattr(eng, "_probe_intent_coverage", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(eng, "_build_corpus_map", lambda *_args: "MAP")
    monkeypatch.setattr(
        eng, "_storm_outline",
        lambda *_args, **_kwargs: [{
            "title": "原问题", "scope": "s", "sub_queries": ["用户原问题"],
            "intent_ids": ["intent-1"],
        }],
    )
    monkeypatch.setattr(eng, "_probe_sufficiency", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(eng, "_judge_sufficiency", lambda _q, sections, _p, use_llm=True: sections)

    eng.plan_outline(nb.id, rid, "用户原问题")

    failures = [
        event for event in events
        if event.get("kind") == "report_corpus_profile_failed"
    ]
    assert failures == [{
        "kind": "report_corpus_profile_failed",
        "notebook_id": nb.id,
        "report_id": rid,
    }]
    new_events = [
        event for event in events
        if event.get("kind") in {"report_stage_timing", "retrieval_run_stats"}
    ]
    planning_stages = {
        event["stage"] for event in new_events
        if event.get("kind") == "report_stage_timing"
    }
    assert {
        "planning_intent",
        "planning_corpus_profile",
        "planning_intent_probe",
        "planning_corpus_map",
        "planning_outline_model",
        "planning_sufficiency_probe",
        "planning_sufficiency_judge",
    } <= planning_stages
    assert any(
        event.get("kind") == "retrieval_run_stats"
        and event.get("run_kind") == "report_planning"
        and event.get("correlation_id") == rid
        for event in new_events
    )
    assert all("notebook_id" not in event for event in new_events)
    serialized = json.dumps(new_events, ensure_ascii=False)
    assert "用户原问题" not in serialized
    assert nb.id not in serialized
    detail = repo.get_report(nb.id, rid)
    # 终态断言防守卫被掏空:此前探针桩不收新 kwarg 时,TypeError 被
    # plan_outline 吞成 failed,而上面的断言全部在探针之前写入、照样通过
    # ——本测试证明的 fail-open 语义实际根本没跑到(质量评审 P1-1 实测)。
    assert detail["status"] == "outline_ready"
    assert detail["understanding"]["corpus_profile"] == {
        "unavailable_reason": "failed"
    }


def test_scoped_report_skips_whole_corpus_profile_and_ppr(repo, monkeypatch):
    from app.models.source_scope import SourceScope
    from app.services.report_engine import ReportEngine
    from app.services.source_scope import source_scope_context

    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "用户原问题")
    eng = ReportEngine.from_repository(repo, repo.settings)
    monkeypatch.setattr(
        eng, "_plan_intent_contract",
        lambda *_args, **_kwargs: {
            "objective": "用户原问题",
            "mandatory_topics": [{
                "id": "intent-1", "title": "原问题", "question": "用户原问题",
                "retrieval_queries": ["用户原问题"],
            }],
        },
    )
    monkeypatch.setattr(
        eng, "_corpus_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("scoped report must not read whole-corpus profile")
        ),
    )
    monkeypatch.setattr(
        repo.retrieval, "ppr_retrieve",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("scoped report must not run whole-graph PPR")
        ),
    )
    monkeypatch.setattr(repo.retrieval, "federated_retrieve", lambda *_args: [])
    monkeypatch.setattr(eng, "_probe_intent_coverage", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        eng, "_storm_outline",
        lambda *_args, **_kwargs: [{
            "title": "原问题", "scope": "s", "sub_queries": ["用户原问题"],
            "intent_ids": ["intent-1"],
        }],
    )
    monkeypatch.setattr(eng, "_probe_sufficiency", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        eng, "_judge_sufficiency", lambda _q, sections, _p, use_llm=True: sections
    )

    with source_scope_context(
        nb.id, SourceScope(mode="include", source_ids=["s1"])
    ):
        eng.plan_outline(nb.id, rid, "用户原问题")

    detail = repo.get_report(nb.id, rid)
    # 终态断言防守卫被掏空:探针桩不收新 kwarg 时 plan_outline 早退成 failed,
    # ppr_retrieve 的 AssertionError 哨兵永远不会再被触发,一条来源范围泄漏的
    # 红线守卫变成空壳(质量评审 P1-1 实测)。
    assert detail["status"] == "outline_ready"
    # A deliberate skip must not be persisted as the failure state: both used to
    # collapse into `{}` and the reader was told the statistics had broken.
    assert detail["understanding"]["corpus_profile"] == {
        "unavailable_reason": "scope_restricted"
    }


def test_stage_timing_attributes_retrieval_synthesis_and_drafting(repo, monkeypatch):
    """Wall clock must be attributable to a stage, not just to the model log.

    A production report spent ~59 minutes of wall clock against ~27 minutes of
    parallel model time; nothing recorded where the other half went.
    """
    eng = _mk_engine(repo, _OutlineLLM())
    events = []
    monkeypatch.setattr(
        eng.dependencies.event_log, "emit", lambda event: events.append(event)
    )
    _record_retrieve_and_draft(eng, monkeypatch, [])
    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "q")

    eng._run_sections(
        nb.id, rid,
        [{"title": "A", "scope": "s"}, {"title": "B", "scope": "s"}],
        "q", depth=2,
    )

    timings = [e for e in events if e.get("kind") == "report_stage_timing"]
    by_stage = {}
    for event in timings:
        by_stage.setdefault(event["stage"], []).append(event)
    assert len(by_stage["retrieve"]) == 2
    assert len(by_stage["draft"]) == 2
    assert len(by_stage["synthesis"]) == 1
    assert all(isinstance(e["ms"], int) and e["ms"] >= 0 for e in timings)
    assert all(e["report_id"] == rid and "notebook_id" not in e for e in timings)
    assert sorted(e["section_index"] for e in by_stage["retrieve"]) == [0, 1]
    # Diagnostics must not carry content: indices and durations only.
    assert not any(
        "A" in str(value) or "B" in str(value)
        for event in timings for key, value in event.items()
        if key not in {"kind", "stage"}
    )


def test_stage_timing_does_not_echo_an_unregistered_stage():
    import time

    from app.services.reports.observability import emit_stage_timing

    events = []

    class _Log:
        def emit(self, event):
            events.append(event)

    emit_stage_timing(
        _Log(), report_id="rep-safe", stage="SECRET-USER-CONTENT",
        started=time.monotonic(),
    )
    assert events[0]["stage"] == "unknown"
    assert "SECRET-USER-CONTENT" not in json.dumps(events[0])


def test_planning_stage_timing_classifies_cancellation():
    from app.services.cancellation import AskCancelled
    from app.services.reports.observability import observe_stage

    events = []

    class _Log:
        def emit(self, event):
            events.append(event)

    with pytest.raises(AskCancelled):
        with observe_stage(
            _Log(), report_id="rep-safe", stage="planning_corpus_map"
        ):
            raise AskCancelled()

    assert len(events) == 1
    assert events[0] == {
        "kind": "report_stage_timing",
        "report_id": "rep-safe",
        "stage": "planning_corpus_map",
        "ms": events[0]["ms"],
        "cancelled": True,
    }
    assert isinstance(events[0]["ms"], int)


def test_stage_timing_is_recorded_when_a_stage_is_cancelled(repo, monkeypatch):
    """A cancelled stage is the one most worth attributing — the user gave up."""
    from app.services.cancellation import AskCancelled

    eng = _mk_engine(repo, _OutlineLLM())
    events = []
    monkeypatch.setattr(
        eng.dependencies.event_log, "emit", lambda event: events.append(event)
    )

    def _cancel(*_args, **_kwargs):
        raise AskCancelled()

    monkeypatch.setattr(eng, "_deep_dive", _cancel)
    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "q")

    with pytest.raises(AskCancelled):
        eng._run_sections(
            nb.id, rid,
            [{"title": "A", "scope": "s"}, {"title": "B", "scope": "s"}],
            "q", depth=2,
        )

    timings = [e for e in events if e.get("kind") == "report_stage_timing"]
    assert timings, "cancelled retrieval left no wall-clock attribution"
    assert all(e["stage"] == "retrieve" for e in timings)
    assert all(e["cancelled"] is True for e in timings)


def test_corpus_map_re_probes_an_unavailable_profile_instead_of_formatting_it(
    repo, monkeypatch
):
    """A cached unavailable marker must re-probe, exactly as a bare `{}` did.

    The marker is a non-empty dict, so restoring the plain `or` chain makes it
    truthy: planning would skip the re-probe and format the marker into a corpus
    summary whose every count is 0 — measured statistics that never existed.
    """
    from app.services.report_corpus_profile import (
        PROFILE_FAILED, unavailable_profile,
    )
    from app.services.report_engine import ReportEngine

    nb = _mk_nb(repo)
    eng = ReportEngine.from_repository(repo, repo.settings)
    monkeypatch.setattr(repo.retrieval, "federated_retrieve", lambda *_a: [])
    monkeypatch.setattr(
        repo.retrieval, "ppr_retrieve", lambda *_a, **_k: []
    )
    probes = []

    def _still_failing(*_args, **_kwargs):
        probes.append(1)
        raise RuntimeError("bad SQL")

    monkeypatch.setattr(eng, "_corpus_profile", _still_failing)
    eng._planning_corpus_profile = unavailable_profile(PROFILE_FAILED)

    corpus_map = eng._build_corpus_map(nb.id, "用户原问题")

    assert "资料基础" not in corpus_map
    assert "可见来源 0 份" not in corpus_map
    # A stale unavailable marker must still re-probe, exactly as `{}` used to.
    assert probes == [1]


def test_plan_outline_falls_back_on_bad_storm_json(repo, monkeypatch):
    from app.services.report_engine import ReportEngine
    nb = _mk_nb(repo)
    class _Bad:
        configured=True
        def chat_json(self, *a, **k): return "not json"
    _bind_report_llm(repo, _Bad())
    monkeypatch.setattr(ReportEngine, "_build_corpus_map", lambda self,n,q:"MAP")
    monkeypatch.setattr(repo.retrieval, "federated_retrieve", lambda a,q: [])
    eng=ReportEngine.from_repository(repo, repo.settings)
    rid=repo.create_report(nb.id,"q")
    eng.plan_outline(nb.id, rid, "q")
    d=repo.get_report(nb.id, rid)
    assert d["status"]=="outline_ready" and len(d["outline"])>=1   # 回退骨架


# ---------------------------------------------------------------------------
# Task 5(两阶段): 引擎拆 generate 阶段(读 outline_json→深挖→汇总→done)+ run 编排
# ---------------------------------------------------------------------------

def test_generate_runs_sections_on_stored_outline(repo, monkeypatch):
    from app.services.report_engine import ReportEngine
    from app.services.reasoning_retrieval import ReasoningResult
    nb=_mk_nb(repo)
    rid=repo.create_report(nb.id,"q")
    repo.update_report(nb.id, rid, outline=[{"title":"A","scope":"s","sub_queries":["q"]}],
                       status="outline_ready")
    eng=ReportEngine.from_repository(repo, repo.settings)
    monkeypatch.setattr(eng, "_deep_dive", lambda *a,**k: ReasoningResult())
    class _S:
        configured=True
        def chat_json(self, messages, schema_hint, **kwargs):
            if "ONLY this section" in messages[-1]["content"]:
                return json.dumps({"markdown": "## A\n正文", "grounded": False})
            return json.dumps({"summary":"总"})
    _bind_report_llm(repo, _S())
    eng.generate(nb.id, rid, "q", depth=2)
    d=repo.get_report(nb.id, rid)
    assert d["status"]=="done" and d["content_md"].startswith("#")


def test_generate_marks_all_empty_sections_failed_instead_of_done(repo, monkeypatch):
    from app.services.report_engine import ReportEngine

    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "q")
    repo.update_report(
        nb.id, rid, status="outline_ready",
        outline=[{"title": "A", "scope": "s", "sub_queries": ["q"]}],
    )
    eng = ReportEngine.from_repository(repo, repo.settings)
    monkeypatch.setattr(eng, "_run_sections", lambda *args, **kwargs: [{
        "title": "A", "scope": "s", "markdown": "", "grounded": False,
        "failed": True, "error": "pool timeout", "id_map": {}, "claims": [],
        "claim_ledger_status": "missing",
    }])

    eng.generate(nb.id, rid, "q", depth=8)

    detail = repo.get_report(nb.id, rid)
    assert detail["status"] == "failed"
    assert "所有章节均未产出有效正文" in detail["error"]
    assert "本节生成失败" in detail["content_md"]


def test_generate_keeps_clarifications_out_of_visible_report_title(repo, monkeypatch):
    from app.services.report_engine import ReportEngine

    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "分析这个问题")
    repo.update_report(
        nb.id,
        rid,
        status="outline_ready",
        outline=[{"title": "A", "scope": "s", "sub_queries": ["q"]}],
        understanding={
            "resolved_question": "分析 PLL 稳定性",
            "clarification_answers": [{
                "id": "ambiguity-input",
                "question": "具体研究对象是什么？",
                "answer": "电荷泵 PLL",
            }],
            "confirmed": True,
        },
    )
    eng = ReportEngine.from_repository(repo, repo.settings)
    seen = {}

    def _sections(_notebook_id, _rid, _outline, research_question, _depth):
        seen["research_question"] = research_question
        return [{
            "title": "A", "scope": "s", "markdown": "## A\n正文",
            "grounded": True, "failed": False, "id_map": {},
        }]

    monkeypatch.setattr(eng, "_run_sections", _sections)
    class _Summary:
        configured = True
        def chat_json(self, *args, **kwargs):
            return json.dumps({"summary": "总结"})
    _bind_report_llm(repo, _Summary())

    eng.generate(nb.id, rid, "分析这个问题")

    detail = repo.get_report(nb.id, rid)
    assert detail["content_md"].splitlines()[0] == "# 深度报告:分析 PLL 稳定性"
    assert "用户确认的补充信息" in seen["research_question"]
    assert "电荷泵 PLL" in seen["research_question"]

def test_run_backcompat_plans_then_generates(repo, monkeypatch):
    from app.services.report_engine import ReportEngine
    eng=ReportEngine.from_repository(repo, repo.settings)
    calls=[]
    monkeypatch.setattr(eng,"plan_outline", lambda *a,**k: calls.append("plan"))
    monkeypatch.setattr(eng,"generate", lambda *a,**k: calls.append("gen"))
    # outline_ready 由 stub plan_outline 不写,run 需自己判定;这里断言两阶段都被调用
    # (Task 25:引擎读 reports 端口 = runtime 的 ReportStore,桩打在所有者上)
    monkeypatch.setattr(repo._runtime.report_store, "get_report",
                        lambda n,r: {"status":"outline_ready","outline":[{"title":"A"}]})
    monkeypatch.setattr(repo._runtime.report_store, "claim_report_generation",
                        lambda n, r: True)
    eng.run("nb","rid","q", auto_generate=True)
    assert calls==["plan","gen"]


def test_draft_section_empty_content_marks_failed_and_observable(repo, monkeypatch):
    # 思考型模型偶发 content 空 → chat_json "{}" → markdown 空。修复前本节在 _assemble
    # 里静默消失;现应有界重试→仍空则标 failed(渲染「本节生成失败」note)+ 补 model_error。
    from app.services.reasoning_retrieval import ReasoningResult

    class _EmptyLLM:
        configured = True
        model = "m"
        def __init__(self):
            self.calls = 0
        def chat_json(self, *a, **k):
            self.calls += 1
            return "{}"

    stub = _EmptyLLM()
    eng = _mk_engine(repo, stub)
    nb = _mk_nb(repo)
    notes = []
    events = []
    monkeypatch.setattr(eng.dependencies.event_log, "emit", events.append)
    # spy 可观测(Task 25:引擎经 ModelErrorSink 端口 = runtime 的模型 provider)
    repo._runtime.models.note_model_error = (
        lambda stage, error, *, workload_id: notes.append((stage, workload_id))
    )
    out = eng._draft_section(
        nb.id,
        {"title": "SECRET-TITLE", "scope": "SECRET-SCOPE"},
        "SECRET-QUESTION",
        ReasoningResult(),
        report_id="rep-safe-id",
        section_index=3,
    )
    assert stub.calls == 2                                   # 空 markdown 触发重试
    assert out["markdown"] == ""
    assert out.get("failed") is True and out.get("error")   # 不再静默:标 failed→渲染 note
    assert ("report_section", "report_section") in notes   # 精确工作负载可观测
    attempts = [
        event for event in events
        if event.get("kind") == "report_section_attempt"
    ]
    assert [(event["attempt"], event["status"]) for event in attempts] == [
        (1, "empty"), (2, "empty"),
    ]
    assert all(
        event["report_id"] == "rep-safe-id"
        and event["section_index"] == 3
        and isinstance(event["ms"], int)
        and not ({"notebook_id", "source_id", "query", "title", "text"} & set(event))
        for event in attempts
    )
    serialized = json.dumps(attempts, ensure_ascii=False)
    assert "SECRET-TITLE" not in serialized
    assert "SECRET-QUESTION" not in serialized


def test_report_section_uses_direct_element_and_recomputes_grounding(repo):
    from app.services.reasoning_retrieval import ReasoningResult
    from app.services.retrieval import RetrievedElement

    class _ElementLLM:
        configured = True
        model = "element-model"

        def chat_json(self, messages, schema_hint, **kwargs):
            prompt = messages[-1]["content"]
            assert "[Direct source elements]" in prompt
            assert "k4001:" in prompt
            return json.dumps({"markdown": "## 机理\n阈值会变化。[k4001]",
                               "grounded": True})

    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _ElementLLM())
    result = ReasoningResult(elements=[RetrievedElement(
        element_id="el-1", source_id="src-1", source_title="Device notes",
        location_label="p. 4", element_type="paragraph",
        text="Body effect changes the threshold voltage.", score=0.9,
    )])
    out = eng._draft_section(
        nb.id,
        {"title": "机理", "scope": "解释阈值变化", "sub_queries": ["body effect"],
         "intent_ids": ["intent-1"]},
        "为什么阈值变化", result,
    )
    assert out["grounded"] is True and out["evidence_level"] == "grounded"
    assert out["id_map"]["k4001"]["element_id"] == "el-1"
    assert out["intent_ids"] == ["intent-1"]


def test_report_section_high_risk_downgrade_is_opt_in(repo):
    from app.services.reasoning_retrieval import ReasoningResult
    from app.services.retrieval import RetrievedElement

    class _RiskyLLM:
        configured = True
        model = "risk-audit-model"

        def chat_json(self, *args, **kwargs):
            return json.dumps({
                "markdown": "## 结果\n机理来自资料 [k4001]。吞吐提升 25%。",
                "grounded": True,
            })

    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _RiskyLLM())
    result = ReasoningResult(elements=[RetrievedElement(
        element_id="el-risk", source_id="src-risk", source_title="Risk notes",
        location_label="p. 1", element_type="paragraph",
        text="The mechanism is documented.", score=0.9,
    )])
    section = {"title": "结果", "scope": "解释结果", "sub_queries": ["result"]}

    out = eng._draft_section(nb.id, section, "解释结果", result)
    assert out["evidence_level"] == "grounded"
    assert out["citation_audit"]["threshold_exceeded"] is True
    assert out["citation_audit"]["downgrade_applied"] is False

    eng.settings.report_high_risk_downgrade_enabled = True
    downgraded = eng._draft_section(nb.id, section, "解释结果", result)
    assert downgraded["evidence_level"] == "overview"
    assert downgraded["citation_audit"]["downgrade_applied"] is True


def test_report_section_model_grounded_flag_cannot_validate_fake_marker(repo):
    from app.services.reasoning_retrieval import ReasoningResult

    class _FakeCitationLLM:
        configured = True
        model = "fake-citation"

        def chat_json(self, *args, **kwargs):
            return json.dumps({"markdown": "## T\n未经支持。[k999]", "grounded": True})

    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _FakeCitationLLM())
    out = eng._draft_section(
        nb.id, {"title": "T", "scope": "S", "sub_queries": ["q"]},
        "q", ReasoningResult(),
    )
    assert out["grounded"] is False and out["evidence_level"] == "inferred"


def test_report_section_does_not_read_memory_under_selected_source_scope(repo):
    from dataclasses import replace

    from app.models.source_scope import SourceScope
    from app.services.reasoning_retrieval import ReasoningResult
    from app.services.source_scope import source_scope_context

    class _Memory:
        def notebook_memory_hits(self, *_args, **_kwargs):
            raise AssertionError("selected source scope must not query Memory")

    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _OutlineLLM())
    eng.dependencies = replace(eng.dependencies, memory_retriever=_Memory())

    with source_scope_context(
        nb.id, SourceScope(mode="include", source_ids=["src-selected"])
    ):
        out = eng._draft_section(
            nb.id,
            {"title": "T", "scope": "S", "sub_queries": ["q"]},
            "q",
            ReasoningResult(),
        )

    assert out["markdown"]


def test_final_editor_reports_uncovered_intent_without_rewriting_sections(repo, monkeypatch):
    class _EditorLLM:
        configured = True

        def chat_json(self, messages, schema_hint, **kwargs):
            assert "no new facts" in messages[-1]["content"]
            return json.dumps({
                "summary": "只概括已有正文。",
                "coverage": [
                    {"intent_id": "intent-1", "covered": True, "note": ""},
                    {"intent_id": "intent-2", "covered": False, "note": "缺少边界条件"},
                ],
                "contradictions": [],
            })

    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _EditorLLM())
    events = []
    monkeypatch.setattr(eng.dependencies.event_log, "emit", events.append)
    catalog = [
        {"id": "intent-1", "title": "机理", "question": "解释机理",
         "retrieval_queries": ["mechanism"]},
        {"id": "intent-2", "title": "边界", "question": "说明边界",
         "retrieval_queries": ["boundary"]},
    ]
    outline = [
        {"title": "机理", "scope": "s1", "sub_queries": ["q1"],
         "intent_ids": ["intent-1"], "intent_catalog": catalog},
        {"title": "边界", "scope": "s2", "sub_queries": ["q2"],
         "intent_ids": ["intent-2"], "intent_catalog": catalog},
    ]
    sections = [
        {"title": "机理", "markdown": "## 机理\n正文", "grounded": True,
         "intent_ids": ["intent-1"], "id_map": {}},
        {"title": "边界", "markdown": "## 边界\n内容不完整", "grounded": True,
         "intent_ids": ["intent-2"], "id_map": {}},
    ]
    rid = repo.create_report(nb.id, "q")
    md, gaps, _ = eng._assemble(nb.id, rid, "q", outline, sections)
    assert "内容不完整" in md                         # editor 不重写正文
    assert "只概括已有正文" in md
    assert "必答主题「边界」回答不完整:缺少边界条件" in gaps
    assert "范围与证据局限" in md
    editor_timings = [
        event for event in events
        if event.get("kind") == "report_stage_timing"
        and event.get("stage") == "final_editor"
    ]
    assert len(editor_timings) == 1
    assert editor_timings[0]["report_id"] == rid
    assert editor_timings[0]["failed"] is False
    assert editor_timings[0]["cancelled"] is False
    assert "notebook_id" not in editor_timings[0]


def test_report_section_queue_failure_stays_a_failed_section(repo):
    from app.services.model_work import ModelQueueFull
    from app.services.reasoning_retrieval import ReasoningResult

    class _Busy:
        configured = True

        def chat_json(self, *args, **kwargs):
            raise ModelQueueFull(support_id="mdl-report-full")

    engine = _mk_engine(repo, _Busy())
    notebook = _mk_nb(repo)
    result = engine._draft_section(
        notebook.id,
        {"title": "T", "scope": "S", "sub_queries": ["q"]},
        "q", ReasoningResult(),
    )

    assert result["failed"] is True
    assert result["markdown"] == ""


def test_deep_dive_uses_configured_sibling_threshold(repo, monkeypatch):
    from app.services.reasoning_retrieval import ReasoningResult, ReasoningRetriever

    repo.settings.sibling_min_bridge = 5
    captured = {}

    def _run(self, notebook_id, question, **kwargs):
        captured["sibling_min_bridge"] = self.communities.sibling_min_bridge
        return ReasoningResult()

    monkeypatch.setattr(ReasoningRetriever, "run", _run)
    engine = _mk_engine(repo, _OutlineLLM())
    engine._deep_dive(
        "nb", {"title": "t", "scope": "s", "sub_queries": ["q"]},
        "Q", depth=3,
    )

    assert captured["sibling_min_bridge"] == 5


def test_from_repository_honors_explicit_settings_override(repo):
    from app.services.report_engine import ReportEngine

    custom = repo.settings.model_copy(
        update={"sibling_min_bridge": repo.settings.sibling_min_bridge + 7}
    )

    engine = ReportEngine.from_repository(repo, custom)

    assert engine.settings is custom
    assert engine.dependencies.settings is custom
    assert engine.dependencies.communities.sibling_min_bridge == custom.sibling_min_bridge


# --- 大纲阶段提速四件套(探针 memo / 档位宽度 / 方向种子化 / 进度可见) ---------


def test_probe_memo_dedupes_repeated_queries_within_one_planning_run(repo, monkeypatch):
    """覆盖探针把同一条确认问题放在每个主题头部——memo 后同一查询只检索一次。

    memo 只在 plan_outline 设置后生效;没有 memo 属性的裸调用保持逐次检索的
    历史行为(变异守卫:把 memo 读写删掉,本测试的计数断言立刻红)。
    """
    from app.services.report_engine import ReportEngine

    eng = ReportEngine.from_repository(repo, repo.settings)
    calls = {"federated": [], "elements": []}
    monkeypatch.setattr(
        repo.retrieval, "federated_retrieve",
        lambda nb, q: calls["federated"].append(q) or [],
    )
    monkeypatch.setattr(
        repo.retrieval, "retrieve_elements",
        lambda nb, q, limit=8: calls["elements"].append(q) or [],
    )

    # 无 memo(plan_outline 之外的裸调用):历史行为,逐次检索。
    eng._probe_queries("nb", ["shared", "a"])
    eng._probe_queries("nb", ["shared", "b"])
    assert calls["federated"].count("shared") == 2

    # 有 memo(plan_outline 会在探针前设置):重复查询只打一次。
    calls["federated"].clear(); calls["elements"].clear()
    eng._probe_retrieval_memo = {}
    eng._probe_queries("nb", ["shared", "a"])
    eng._probe_queries("nb", ["shared", "b"])
    eng._probe_queries("nb", ["shared"])
    # The two leaf channels run concurrently.  Memoization promises one call
    # per distinct query; worker start order is deliberately not a contract.
    assert sorted(calls["federated"]) == ["a", "b", "shared"]
    assert sorted(calls["elements"]) == ["a", "b", "shared"]

    # A single parallel probe batch also preserves the old serial memo's
    # duplicate-query behavior instead of racing two identical leaves.
    calls["federated"].clear(); calls["elements"].clear()
    eng._probe_retrieval_memo = {}
    eng._probe_queries("nb", ["duplicate", "duplicate"])
    assert calls["federated"] == ["duplicate"]
    assert calls["elements"] == ["duplicate"]


def test_probe_memo_does_not_cache_failures(repo, monkeypatch):
    """失败不进 memo:瞬态错误保持逐探针重试,而不是被冻结成空结果。"""
    from app.services.report_engine import ReportEngine

    eng = ReportEngine.from_repository(repo, repo.settings)
    attempts = []

    def flaky(nb, q):
        attempts.append(q)
        if len(attempts) == 1:
            raise RuntimeError("transient")
        return []

    monkeypatch.setattr(repo.retrieval, "federated_retrieve", flaky)
    monkeypatch.setattr(
        repo.retrieval, "retrieve_elements", lambda nb, q, limit=8: [],
    )
    eng._probe_retrieval_memo = {}
    eng._probe_queries("nb", ["q"])   # 第一次:失败,吞掉,不缓存
    eng._probe_queries("nb", ["q"])   # 第二次:必须真的重试
    assert attempts == ["q", "q"]


def test_probe_queries_propagates_cancellation(repo, monkeypatch):
    """Cancellation is control flow and must not degrade into an empty probe."""
    from app.services.cancellation import AskCancelled
    from app.services.report_engine import ReportEngine

    eng = ReportEngine.from_repository(repo, repo.settings)
    monkeypatch.setattr(
        eng, "_probe_knowledge_hits",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AskCancelled()),
    )
    monkeypatch.setattr(eng, "_probe_element_hits", lambda *_args, **_kwargs: [])

    with pytest.raises(AskCancelled):
        eng._probe_queries("nb", ["q"])


def test_probe_width_maps_report_depth_to_shared_tiers():
    """探针宽度按 depth→档位映射;None(行上缺 depth)保持历史宽度 4。

    数值契约(2/2/3/4/4)登记在 docs/product-and-api*.md;这里钉映射本身,
    以及「宽度真的限制了探测的查询数」。
    """
    from app.services.report_engine import report_probe_query_width

    assert report_probe_query_width(1) == 2      # overview(快速预览语义)
    # standard 刻意是 3 不是 2:表头恒为共享确认问题,宽度 2 = 每主题只剩
    # 1 条专属探针,默认档的用户可见充分性结论会明显变保守(质量评审 P2-4)。
    assert report_probe_query_width(2) == 3      # standard
    assert report_probe_query_width(4) == 3      # deep
    assert report_probe_query_width(8) == 4      # thorough
    assert report_probe_query_width(16) == 4     # exhaustive
    assert report_probe_query_width(None) == 4   # 历史行为


def test_probe_queries_honors_max_queries(repo, monkeypatch):
    from app.services.report_engine import ReportEngine

    eng = ReportEngine.from_repository(repo, repo.settings)
    probed = []
    monkeypatch.setattr(
        repo.retrieval, "federated_retrieve",
        lambda nb, q: probed.append(q) or [],
    )
    monkeypatch.setattr(
        repo.retrieval, "retrieve_elements", lambda nb, q, limit=8: [],
    )
    eng._probe_queries("nb", ["q1", "q2", "q3", "q4"], max_queries=2)
    # Batch execution may start independent probes in either order; the
    # first-N membership remains the contract.
    assert sorted(probed) == ["q1", "q2"]


def test_probe_sufficiency_batches_shared_queries_once_and_preserves_order(
    repo, monkeypatch,
):
    """Separate section aggregates share one ordered, bounded leaf batch."""
    from app.services.report_engine import ReportEngine

    eng = ReportEngine.from_repository(repo, repo.settings)
    calls = {"knowledge": [], "element": []}
    monkeypatch.setattr(
        repo.retrieval, "federated_retrieve",
        lambda nb, query: calls["knowledge"].append(query) or [],
    )
    monkeypatch.setattr(
        repo.retrieval, "retrieve_elements",
        lambda nb, query, limit=8: calls["element"].append(query) or [],
    )

    result = eng._probe_sufficiency("nb", [
        {"title": "first", "sub_queries": ["shared", "first-only"]},
        {"title": "second", "sub_queries": ["shared", "second-only"]},
    ])

    assert [row["title"] for row in result] == ["first", "second"]
    assert sorted(calls["knowledge"]) == ["first-only", "second-only", "shared"]
    assert sorted(calls["element"]) == ["first-only", "second-only", "shared"]


def test_probe_batch_retries_failed_shared_leaf_for_later_section(repo, monkeypatch):
    """Batch de-duplication must not turn a transient probe failure into a miss."""
    from app.services.report_engine import ReportEngine

    eng = ReportEngine.from_repository(repo, repo.settings)
    attempts = []

    def _flaky(_notebook_id, query):
        attempts.append(query)
        if len(attempts) == 1:
            raise RuntimeError("transient")
        return []

    monkeypatch.setattr(repo.retrieval, "federated_retrieve", _flaky)
    monkeypatch.setattr(
        repo.retrieval, "retrieve_elements", lambda *_args, **_kwargs: [],
    )

    eng._probe_sufficiency("nb", [
        {"title": "first", "sub_queries": ["shared"]},
        {"title": "second", "sub_queries": ["shared"]},
    ])

    assert attempts == ["shared", "shared"]


def test_probe_batch_respects_report_wide_fanout_limit(repo, monkeypatch):
    """Batching topics cannot turn one report into unbounded leaf I/O."""
    import threading
    import time

    from app.services.report_engine import ReportEngine

    settings = repo.settings.model_copy(
        update={
            "report_retrieval_fanout": 2,
            "report_probe_channel_concurrency": 2,
        }
    )
    eng = ReportEngine.from_repository(repo, settings)
    active = 0
    maximum = 0
    lock = threading.Lock()

    def _leaf(*_args, **_kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return []

    monkeypatch.setattr(repo.retrieval, "federated_retrieve", _leaf)
    monkeypatch.setattr(repo.retrieval, "retrieve_elements", _leaf)

    eng._probe_sufficiency("nb", [{
        "title": "A", "sub_queries": ["q1", "q2", "q3"],
    }])

    assert maximum <= settings.report_retrieval_fanout


def test_probe_batch_uses_full_configured_fanout_for_independent_queries(
    repo, monkeypatch,
):
    """Two query pairs use all four configured leaf slots, not the old fixed two."""
    import threading

    from app.services.report_engine import ReportEngine

    settings = repo.settings.model_copy(
        update={
            "report_retrieval_fanout": 4,
            "report_probe_channel_concurrency": 2,
        }
    )
    eng = ReportEngine.from_repository(repo, settings)
    active = 0
    maximum = 0
    lock = threading.Lock()
    all_leaf_slots = threading.Barrier(settings.report_retrieval_fanout)

    def _leaf(*_args, **_kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            all_leaf_slots.wait(timeout=1)
        finally:
            with lock:
                active -= 1
        return []

    monkeypatch.setattr(repo.retrieval, "federated_retrieve", _leaf)
    monkeypatch.setattr(repo.retrieval, "retrieve_elements", _leaf)

    eng._probe_sufficiency("nb", [{
        "title": "A", "sub_queries": ["q1", "q2"],
    }])

    assert maximum == settings.report_retrieval_fanout


def test_deep_dive_seeds_confirmed_directions_and_skips_rerun(repo, monkeypatch):
    """种子化两半都要钉:①大纲方向经 `intent_queries` 进 run(run 内因此跳过
    plan LLM 调用——那是 reasoning_retrieval 已测的既有语义);②run 内已执行
    (attempted 记账)的方向,run 后合并不再重复同一条 federated 检索,但元素
    补取照旧(run 不按方向逐条取元素,那半不是重复)。run 内没执行到的方向
    (低档步数装不下)KG 侧照常兜底执行。
    """
    from app.services.reasoning_retrieval import ReasoningResult, ReasoningRetriever

    captured = {}

    def _run(self, notebook_id, question, **kwargs):
        captured.update(kwargs)
        captured["question"] = question
        result = ReasoningResult()
        # run 声称自己执行了 q1(种子路径的正常账目);q2 没执行到;
        # q3 检索本身炸了(failed 标)——兜底必须把它当没执行过。
        result.attempted.append({"query": "q1", "new": 3, "tries": 1})
        result.attempted.append(
            {"query": "q3", "new": 0, "tries": 1, "failed": True}
        )
        return result

    monkeypatch.setattr(ReasoningRetriever, "run", _run)
    federated, elements = [], []
    monkeypatch.setattr(
        repo.retrieval, "federated_retrieve",
        lambda nb, q: federated.append(q) or [],
    )
    monkeypatch.setattr(
        repo.retrieval, "retrieve_elements",
        lambda nb, q, limit=8: elements.append(q) or [],
    )

    engine = _mk_engine(repo, _OutlineLLM())
    # q1 刻意带两端空白:种子在 trim 后进 run,run 记账的是 trim 形;合并循环
    # 的比对必须同样 strip,否则跳过永假、每方向白付一次检索(规格评审 P3)。
    engine._deep_dive(
        "nb", {"title": "t", "scope": "s", "sub_queries": [" q1 ", "q2", "q3"]},
        "Q", depth=3,
    )

    seeds = captured.get("intent_queries") or []
    # 首条种子恒为节复合问题(镜像 Ask:完整权威问题恒为第一条)——只传裸方向
    # 会让 sec_question 从此不进任何 KG 检索,方向缺主语时整条主语召回消失
    # (质量评审 P2-5)。
    assert seeds and seeds[0] == captured.get("question"), (
        "种子首条必须是节复合问题本身"
    )
    assert seeds[1:] == ["q1", "q2", "q3"], (
        "大纲方向必须作为已确认种子进 run——否则每节多付一次 plan LLM 调用"
    )
    assert federated == ["q2", "q3"], (
        "run 内成功执行的 q1 不得重复 federated;q2 未执行、q3 检索失败"
        "(failed 标)都必须兜底"
    )
    assert [q.strip() for q in elements] == ["q1", "q2", "q3"], (
        "元素补取不是重复,全部方向都要"
    )


@pytest.mark.parametrize("cancelled_channel", ["knowledge", "element"])
def test_deep_dive_direction_retrieval_propagates_cancellation(
    repo, monkeypatch, cancelled_channel,
):
    from app.services.cancellation import AskCancelled
    from app.services.reasoning_retrieval import ReasoningResult, ReasoningRetriever

    def _run(*_args, **_kwargs):
        result = ReasoningResult()
        if cancelled_channel == "element":
            result.attempted.append({"query": "q", "new": 0, "tries": 1})
        return result

    monkeypatch.setattr(ReasoningRetriever, "run", _run)
    monkeypatch.setattr(repo.retrieval, "federated_retrieve", lambda *_args: [])
    monkeypatch.setattr(repo.retrieval, "retrieve_elements", lambda *_args, **_kwargs: [])
    target = (
        "federated_retrieve" if cancelled_channel == "knowledge"
        else "retrieve_elements"
    )
    monkeypatch.setattr(
        repo.retrieval, target,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AskCancelled()),
    )

    with pytest.raises(AskCancelled):
        _mk_engine(repo, _OutlineLLM())._deep_dive(
            "nb", {"title": "t", "scope": "s", "sub_queries": ["q"]},
            "Q", depth=3,
        )


def test_deep_dive_without_directions_leaves_planner_path(repo, monkeypatch):
    """方向为空回落 planner 路径(intent_queries=None),与接入前行为一致。"""
    from app.services.reasoning_retrieval import ReasoningResult, ReasoningRetriever

    captured = {}

    def _run(self, notebook_id, question, **kwargs):
        captured.update(kwargs)
        return ReasoningResult()

    monkeypatch.setattr(ReasoningRetriever, "run", _run)
    engine = _mk_engine(repo, _OutlineLLM())
    engine._deep_dive("nb", {"title": "t", "scope": "s"}, "Q", depth=3)
    assert captured.get("intent_queries") is None


def test_plan_outline_reports_sufficiency_progress(repo, monkeypatch):
    """「多视角规划大纲中」与「大纲就绪」之间必须有充分性进度写。

    这一段(STORM LLM + 每节探针)此前零进度写,大库上是好几分钟的表观卡死
    (生产实测:updated_at 停在 STORM 开始那刻)。
    """
    from app.services.report_engine import ReportEngine

    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "问题")
    eng = ReportEngine.from_repository(repo, repo.settings)
    contract = {
        "objective": "问题",
        "mandatory_topics": [{
            "id": "intent-1", "title": "T", "question": "问题",
            "retrieval_queries": ["q"],
        }],
    }
    monkeypatch.setattr(
        eng, "_plan_intent_contract", lambda question, history: contract,
    )
    monkeypatch.setattr(
        eng, "_probe_intent_coverage",
        lambda notebook_id, value, max_queries=4: [],
    )
    monkeypatch.setattr(eng, "_build_corpus_map", lambda nb_id, q: "CORPUS")
    monkeypatch.setattr(
        eng, "_storm_outline",
        lambda *args, **kwargs: [{
            "title": "节", "scope": "s", "sub_queries": ["q"], "intent_ids": [],
        }],
    )
    progress_seq = []
    # 探针 stub 往同一条时间线里记 sentinel:进度写必须发生在探针**之前**,
    # 否则它毫无意义(用户仍盯着上一条文案看完整个探针段)。只断言"字符串
    # 出现过"扛不住移动变异——把写挪到探针之后照样绿(规格评审 P2-b 实测)。
    monkeypatch.setattr(
        eng, "_probe_sufficiency",
        lambda *args, **kwargs: progress_seq.append("<sufficiency-probe-ran>") or [],
    )
    monkeypatch.setattr(
        eng, "_judge_sufficiency", lambda question, sections, probe, use_llm=True: sections,
    )
    real_update = eng.dependencies.reports.update_report

    def recording_update(notebook_id, report_id, **kwargs):
        if kwargs.get("progress"):
            progress_seq.append(kwargs["progress"])
        return real_update(notebook_id, report_id, **kwargs)

    monkeypatch.setattr(eng.dependencies.reports, "update_report", recording_update)
    eng.plan_outline(nb.id, rid, "问题")

    assert "检查各节证据充分性" in progress_seq
    assert progress_seq.index("多视角规划大纲中") < progress_seq.index(
        "检查各节证据充分性"
    )
    assert progress_seq.index("检查各节证据充分性") < progress_seq.index(
        "<sufficiency-probe-ran>"
    ), "进度写必须先于充分性探针,挪到探针之后等于没写"
    assert progress_seq[-1].startswith("大纲就绪")


def test_sufficiency_llm_skipped_at_low_tiers(repo, monkeypatch):
    """overview/standard 只跑确定性半——LLM 客户端一次都不能被触碰。

    Judge 的 LLM 半本就是 fail-open 的只降不升精修;低档跳过它 = 既有失败
    路径语义 + 人工大纲确认门兜底。deep 及以上照跑(由
    test_plan_outline_produces_enriched_outline_ready 用 depth=4 钉住)。
    """
    from app.services.report_engine import (
        ReportEngine, report_sufficiency_llm_enabled,
    )

    # 映射本身:overview/standard 关,deep/thorough/exhaustive 开,None 保持历史。
    assert report_sufficiency_llm_enabled(1) is False
    assert report_sufficiency_llm_enabled(2) is False
    assert report_sufficiency_llm_enabled(4) is True
    assert report_sufficiency_llm_enabled(8) is True
    assert report_sufficiency_llm_enabled(16) is True
    assert report_sufficiency_llm_enabled(None) is True

    eng = ReportEngine.from_repository(repo, repo.settings)

    # 计数而不是抛错:Judge 的 LLM 半自带 fail-open `except Exception`,爆炸桩
    # 的 AssertionError 会被它吞掉——「删掉 use_llm 早退」的变异下断言照样全绿
    # (实测)。观察「调用发生了没有」才穿透 fail-open。
    llm_calls: list = []

    class _Counting:
        def chat_json(self, *args, **kwargs):
            llm_calls.append("chat_json")
            return "{}"

    monkeypatch.setattr(
        eng.dependencies.model_clients, "chat",
        lambda workload: llm_calls.append(workload) or _Counting(),
    )
    sections = [{"title": "T", "scope": "s", "sub_queries": ["q"]}]
    probe = [{"title": "T", "hits": 5, "base_hits": 0, "element_hits": 4,
              "source_hits": 3, "relevant_items": 4, "relevant_supports": 4,
              "relevant_family_count": 3, "unknown_supports": 0,
              "top_family_share": 0.5, "source_identity_uncertain": 0}]
    out = eng._judge_sufficiency("Q", sections, probe, use_llm=False)
    assert llm_calls == [], "use_llm=False 时不得触碰充分性模型"
    # 确定性半照常:coverage 与充足/薄弱/缺失结论都在(界面消费的就是它们)。
    assert out[0]["sufficiency"] == "充足"
    assert out[0]["coverage"]["hits"] == 5
    assert out[0]["action"] == "keep"


def test_sufficiency_runtime_uses_centralized_policy(repo, monkeypatch):
    from app.services.report_engine import ReportEngine

    eng = ReportEngine.from_repository(repo, repo.settings)
    monkeypatch.setattr(
        repo.settings, "report_sufficiency_min_relevant_items", 5
    )
    sections = [{"title": "T", "scope": "s", "sub_queries": ["q"]}]
    probe = [{
        "title": "T", "hits": 5, "base_hits": 0, "element_hits": 4,
        "source_hits": 3, "relevant_items": 4, "relevant_supports": 4,
        "relevant_family_count": 3, "unknown_supports": 0,
        "top_family_share": 0.5, "source_identity_uncertain": 0,
    }]

    out = eng._judge_sufficiency("Q", sections, probe, use_llm=False)

    assert out[0]["sufficiency"] == "薄弱"
    assert out[0]["action"] == "supplement"


def test_plan_outline_wires_probe_width_from_report_depth(repo, monkeypatch):
    """接线守卫:`plan_outline` 必须把报告行的 depth 真的传成探针宽度。

    映射函数与 `max_queries` 参数各有半边测试,但把两处
    `max_queries=probe_width` 同时删掉,全部用例照绿——特性静默消失
    (规格评审 P2-a 实测)。这里用真 `plan_outline` 数被探测的查询数:
    depth=1(宽度 2)与 depth=16(宽度 4)必须探出不同的量。
    """
    from app.services.report_engine import ReportEngine

    # 两个主题:确认问题按设计排在每个主题查询列表之首——plan_outline 必须
    # 装配 memo,否则同一条确认问题被逐主题重复检索(质量评审 M1:删掉
    # plan_outline 里的 memo 装配行,全部用例曾照绿)。
    contract = {
        "objective": "问题",
        "mandatory_topics": [
            {"id": "i1", "title": "T", "question": "topic-q",
             "retrieval_queries": ["rq1", "rq2", "rq3"]},
            {"id": "i2", "title": "U", "question": "topic-u",
             "retrieval_queries": ["ru1", "ru2", "ru3"]},
        ],
    }
    # 节绑定两个主题:未绑定的必答主题会被 _bind_outline_to_intent 强制补节
    # (红线行为),补出的节又带主题检索词进充分性探针,污染本测试的计数。
    storm_sections = [{
        "title": "节", "scope": "s",
        "sub_queries": ["s1", "s2", "s3", "s4"], "intent_ids": ["i1", "i2"],
    }]

    def probed_queries(depth):
        nb = _mk_nb(repo)
        rid = repo.create_report(nb.id, "问题", depth=depth)
        eng = ReportEngine.from_repository(repo, repo.settings)
        probed: list = []
        monkeypatch.setattr(
            repo.retrieval, "federated_retrieve",
            lambda nb_id, q: probed.append(q) or [],
        )
        monkeypatch.setattr(
            repo.retrieval, "retrieve_elements", lambda nb_id, q, limit=8: [],
        )
        monkeypatch.setattr(
            eng, "_plan_intent_contract", lambda question, history: dict(contract),
        )
        monkeypatch.setattr(eng, "_build_corpus_map", lambda nb_id, q: "C")
        monkeypatch.setattr(
            eng, "_storm_outline",
            lambda *args, **kwargs: [dict(s) for s in storm_sections],
        )
        monkeypatch.setattr(
            eng, "_judge_sufficiency",
            lambda question, sections, probe, use_llm=True: sections,
        )
        eng.plan_outline(nb.id, rid, "问题")
        assert repo.get_report(nb.id, rid)["status"] == "outline_ready"
        return probed

    # 宽度 2:覆盖探针取每主题前 2 条(确认问题 + rq1),充分性取每节前 2 条。
    narrow = probed_queries(1)
    # 宽度 4:覆盖 4 条 + 充分性 4 条。
    wide = probed_queries(16)
    assert len(narrow) < len(wide), (
        f"depth=1 探了 {len(narrow)} 条、depth=16 探了 {len(wide)} 条——"
        "宽度没有随报告 depth 接线"
    )
    assert "rq2" not in narrow and "rq2" in wide
    assert "s3" not in narrow and "s3" in wide
    # memo 经 plan_outline 真实装配:确认问题在两个主题的查询头部各出现一次,
    # 但只允许被真正检索一次。
    confirmed = [q for q in wide if "问题" in q and q not in
                 {"rq1", "rq2", "rq3", "ru1", "ru2", "ru3", "s1", "s2", "s3", "s4"}]
    assert len(confirmed) == 1, (
        f"确认问题被检索 {len(confirmed)} 次——plan_outline 没有装配探针 memo"
    )


# ---------------------------------------------------------------------------
# auto_generate 直通:清晰问题自动确认意图 + 自动接受默认大纲
# ---------------------------------------------------------------------------

class _AutoRunLLM(_OutlineLLM):
    """在 _OutlineLLM 之上补一个可控的问题理解回复。"""

    def __init__(self, ambiguities=None):
        super().__init__()
        self.intent_calls = 0
        self.outline_calls = 0
        self._ambiguities = list(ambiguities or [])

    def chat_json(self, messages, schema_hint, **kw):
        content = messages[-1]["content"]
        if "before seeing any corpus" in content:
            self.intent_calls += 1
            return json.dumps({
                "normalized_question": "PLL 环路稳定性的机理与设计约束是什么？",
                "intent_type": "explain",
                "entities": ["PLL"],
                "mandatory_topics": [{
                    "title": "环路稳定性",
                    "question": "PLL 环路稳定性的机理与设计约束是什么？",
                    "retrieval_queries": ["PLL loop stability"],
                }],
                "confidence": 0.9,
                "needs_clarification": bool(self._ambiguities),
                "ambiguities": self._ambiguities,
            })
        if "OUTLINE" in content:
            self.outline_calls += 1
        return super().chat_json(messages, schema_hint, **kw)


def _stub_auto_run_corpus(eng, repo, monkeypatch):
    from app.services.report_engine import ReportEngine
    from app.services.reasoning_retrieval import ReasoningResult

    monkeypatch.setattr(ReportEngine, "_build_corpus_map", lambda self, n, q: "MAP")
    monkeypatch.setattr(repo.retrieval, "federated_retrieve", lambda a, q: [])
    monkeypatch.setattr(
        eng, "_deep_dive",
        lambda nb_id, section, question, depth=None, on_step=None: ReasoningResult(),
    )


def test_auto_generate_confirms_a_clear_intent_and_runs_straight_through(
    repo, monkeypatch,
):
    """清晰问题 + auto_generate:无任何 confirm/generate 调用即跑到 done。"""
    nb = _mk_nb(repo)
    llm = _AutoRunLLM()
    eng = _mk_engine(repo, llm)
    _stub_auto_run_corpus(eng, repo, monkeypatch)
    rid = repo.create_report(nb.id, "分析 PLL 稳定性")

    eng.run(nb.id, rid, "分析 PLL 稳定性", "", auto_generate=True,
            require_intent_review=True)

    detail = repo.get_report(nb.id, rid)
    assert detail["status"] == "done", detail.get("error")
    understanding = detail["understanding"]
    assert understanding["confirmed"] is True
    assert understanding["needs_clarification"] is False
    # 确认是提交而不是第二次解释:问题理解模型只被调用一次。
    assert llm.intent_calls == 1
    # 自动确认沿用模型已产出、用户本会看到的那个最终问题。
    assert understanding["resolved_question"].startswith("PLL")
    assert detail["content_md"].startswith("#")


def test_auto_confirm_revalidates_a_scoped_report_before_claiming(
    repo, monkeypatch,
):
    """带范围的直通报告在自动确认前必须过与人工端点同一道范围重验(codex R2 P2)：
    重验回调判否 → 留在 intent_ready 并发 scope_invalid 事件;判可 → 照常直通。"""
    scope = {"mode": "include", "source_ids": ["src-alive"], "narrowed": True}

    # 重验不过:留在人工门。
    nb = _mk_nb(repo)
    llm = _AutoRunLLM()
    eng = _mk_engine(repo, llm)
    _stub_auto_run_corpus(eng, repo, monkeypatch)
    rid = repo.create_report(nb.id, "分析 PLL 稳定性")
    seen: list[dict] = []
    events: list[dict] = []
    monkeypatch.setattr(eng.dependencies.event_log, "emit", events.append)

    def deny(understanding):
        seen.append(understanding)
        return None

    eng.run(nb.id, rid, "分析 PLL 稳定性", "", auto_generate=True,
            require_intent_review=True, source_scope=scope,
            scope_reconfirm=deny)
    detail = repo.get_report(nb.id, rid)
    assert detail["status"] == "intent_ready"
    assert len(seen) == 1 and seen[0].get("source_scope") == scope
    skips = [e for e in events
             if e.get("kind") == "report_intent_auto_confirm_skipped"]
    assert skips and skips[-1]["reason"] == "scope_invalid"

    # 重验通过:照常直通到 done,且**刷新后的冻结被采用**——重冻结翻出的
    # narrowed=true 必须落进认领的 understanding(codex R4 P2),后续阶段也要在
    # 刷新后的范围上下文里跑(监听 source_scope_context 的实参)。
    import app.services.source_scope as source_scope_module
    from contextlib import contextmanager

    entered: list[tuple] = []
    real_ctx = source_scope_module.source_scope_context

    @contextmanager
    def recording_ctx(nb_id, scope_arg, base_arg):
        entered.append((nb_id, scope_arg, base_arg))
        with real_ctx(nb_id, scope_arg, base_arg):
            yield

    monkeypatch.setattr(
        source_scope_module, "source_scope_context", recording_ctx
    )
    # 用可被 scope 上下文正常消费的真实形状,同时保留对象同一性可断言。
    refreshed_scope_obj = {**scope, "narrowed": True}

    def allow(understanding):
        return {
            "understanding": {
                **understanding,
                "source_scope": {**scope, "narrowed": True},
            },
            "source_scope": refreshed_scope_obj,
            "base_scope": None,
        }

    nb2 = _mk_nb(repo)
    llm2 = _AutoRunLLM()
    eng2 = _mk_engine(repo, llm2)
    _stub_auto_run_corpus(eng2, repo, monkeypatch)
    rid2 = repo.create_report(nb2.id, "分析 PLL 稳定性")
    eng2.run(nb2.id, rid2, "分析 PLL 稳定性", "", auto_generate=True,
             require_intent_review=True, source_scope=scope,
             scope_reconfirm=allow)
    detail2 = repo.get_report(nb2.id, rid2)
    assert detail2["status"] == "done", detail2.get("error")
    assert detail2["understanding"]["source_scope"]["narrowed"] is True
    assert any(
        entry[0] == nb2.id and entry[1] is refreshed_scope_obj
        for entry in entered
    ), "规划/生成必须在重验刷新后的范围上下文里跑"


def test_auto_confirm_scoped_without_reconfirm_callable_stays_at_the_gate(
    repo, monkeypatch,
):
    """带范围但没有重验回调(旧接线/直接调用):保守留在人工门,不得带着一份
    无法复核的冻结范围继续规划;无范围的合同则完全不需要重验、照常直通。"""
    nb = _mk_nb(repo)
    llm = _AutoRunLLM()
    eng = _mk_engine(repo, llm)
    _stub_auto_run_corpus(eng, repo, monkeypatch)
    rid = repo.create_report(nb.id, "分析 PLL 稳定性")
    eng.run(nb.id, rid, "分析 PLL 稳定性", "", auto_generate=True,
            require_intent_review=True,
            source_scope={"mode": "include", "source_ids": ["s1"], "narrowed": True})
    assert repo.get_report(nb.id, rid)["status"] == "intent_ready"


def test_auto_generate_waits_for_blocking_ambiguity_then_flows_after_confirm(
    repo, monkeypatch,
):
    """有必答歧义:停在 intent_ready;人工确认后大纲阶段仍自动直通到生成。"""
    from app.services.reports.intent_confirmation import confirmed_understanding

    nb = _mk_nb(repo)
    llm = _AutoRunLLM(ambiguities=[{
        "question": "具体研究对象是什么？",
        "reason": "缺少对象",
        "required": True,
        "options": [],
    }])
    eng = _mk_engine(repo, llm)
    _stub_auto_run_corpus(eng, repo, monkeypatch)
    rid = repo.create_report(nb.id, "分析 PLL 稳定性")

    eng.run(nb.id, rid, "分析 PLL 稳定性", "", auto_generate=True,
            require_intent_review=True)

    detail = repo.get_report(nb.id, rid)
    assert detail["status"] == "intent_ready"
    assert detail["understanding"]["needs_clarification"] is True
    assert detail["understanding"]["confirmed"] is False
    assert llm.outline_calls == 0, "阻断性歧义下不得进入规划"

    # 人工确认(镜像 confirm_report_intent 端点):flag 仍在 understanding 里。
    understanding = confirmed_understanding(
        detail["understanding"],
        resolved_question="分析 PLL 环路稳定性",
        answers=[{"id": "ambiguity-1", "answer": "电荷泵 PLL"}],
    )
    assert understanding["auto_generate_requested"] is True
    assert repo.claim_report_intent(nb.id, rid, understanding)
    eng.run(nb.id, rid, "分析 PLL 稳定性", "",
            auto_generate=bool(understanding.get("auto_generate_requested")),
            intent_contract=understanding)

    after = repo.get_report(nb.id, rid)
    assert after["status"] == "done", after.get("error")


def test_auto_intent_confirmation_yields_to_a_racing_manual_claim(
    repo, monkeypatch,
):
    """用户在自动认领前手点确认 → rowcount 0:不抛错、不破坏状态、不重复规划。"""
    import app.services.report_engine as engine_mod

    nb = _mk_nb(repo)
    llm = _AutoRunLLM()
    eng = _mk_engine(repo, llm)
    _stub_auto_run_corpus(eng, repo, monkeypatch)
    rid = repo.create_report(nb.id, "分析 PLL 稳定性")

    real_freeze = engine_mod.confirmed_understanding
    raced: list = []

    def _racing_freeze(understanding, **kwargs):
        frozen = real_freeze(understanding, **kwargs)
        if not raced:                       # 只抢一次:读之后、自动认领之前
            raced.append(True)
            manual = dict(frozen)
            manual["confirmed_input"] = {
                "resolved_question": "用户自己确认的问题", "answers": [],
            }
            assert repo.claim_report_intent(nb.id, rid, manual)
        return frozen

    monkeypatch.setattr(engine_mod, "confirmed_understanding", _racing_freeze)

    eng.run(nb.id, rid, "分析 PLL 稳定性", "", auto_generate=True,
            require_intent_review=True)

    detail = repo.get_report(nb.id, rid)
    assert raced, "竞态桩未生效"
    # 手动认领的结果原样保留,自动侧既不覆盖也不推进。
    assert detail["status"] == "planning"
    assert detail["understanding"]["confirmed_input"]["resolved_question"] == (
        "用户自己确认的问题"
    )
    assert llm.outline_calls == 0 and not llm.section_calls


def test_auto_intent_confirmation_skip_event_reports_claim_lost_when_a_human_wins(
    repo, monkeypatch,
):
    """人工在自动认领前抢先确认时,skip 事件必须记 reason='claim_lost'。"""
    import app.services.report_engine as engine_mod

    nb = _mk_nb(repo)
    llm = _AutoRunLLM()
    eng = _mk_engine(repo, llm)
    _stub_auto_run_corpus(eng, repo, monkeypatch)
    rid = repo.create_report(nb.id, "分析 PLL 稳定性")

    events: list = []
    monkeypatch.setattr(
        eng.dependencies.event_log, "emit", lambda event: events.append(event)
    )

    real_freeze = engine_mod.confirmed_understanding
    raced: list = []

    def _racing_freeze(understanding, **kwargs):
        frozen = real_freeze(understanding, **kwargs)
        if not raced:
            raced.append(True)
            manual = dict(frozen)
            manual["confirmed_input"] = {
                "resolved_question": "用户自己确认的问题", "answers": [],
            }
            assert repo.claim_report_intent(nb.id, rid, manual)
        return frozen

    monkeypatch.setattr(engine_mod, "confirmed_understanding", _racing_freeze)

    eng.run(nb.id, rid, "分析 PLL 稳定性", "", auto_generate=True,
            require_intent_review=True)

    skipped = [e for e in events if e.get("kind") == "report_intent_auto_confirm_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "claim_lost"
    assert repo.get_report(nb.id, rid)["status"] == "planning"


def test_auto_intent_confirmation_skip_event_reports_cancelled_when_report_is_cancelled_mid_race(
    repo, monkeypatch,
):
    """认领前报告被(用户)取消时,罕见竞态读必须把 skip 事件的 reason 改判成
    'cancelled',而不是笼统的 'claim_lost' —— 两者对运维排查的含义不同:前者是
    正常的人工抢跑,后者说明报告已经不在了,重试/告警的意义也不同。"""
    import app.services.report_engine as engine_mod

    nb = _mk_nb(repo)
    llm = _AutoRunLLM()
    eng = _mk_engine(repo, llm)
    _stub_auto_run_corpus(eng, repo, monkeypatch)
    rid = repo.create_report(nb.id, "分析 PLL 稳定性")

    events: list = []
    monkeypatch.setattr(
        eng.dependencies.event_log, "emit", lambda event: events.append(event)
    )

    real_freeze = engine_mod.confirmed_understanding
    raced: list = []

    def _racing_freeze(understanding, **kwargs):
        frozen = real_freeze(understanding, **kwargs)
        if not raced:
            raced.append(True)
            repo.update_report(nb.id, rid, status="cancelled", progress="已取消")
        return frozen

    monkeypatch.setattr(engine_mod, "confirmed_understanding", _racing_freeze)

    eng.run(nb.id, rid, "分析 PLL 稳定性", "", auto_generate=True,
            require_intent_review=True)

    skipped = [e for e in events if e.get("kind") == "report_intent_auto_confirm_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "cancelled"
    # 取消状态原样保留 —— 自动侧发现赢不了这场竞态后不得覆盖它。
    assert repo.get_report(nb.id, rid)["status"] == "cancelled"


def test_auto_intent_confirmation_failure_leaves_the_manual_gate_usable(
    repo, monkeypatch,
):
    """自动推进中途异常 → 留在 intent_ready,用户仍可手动走完。"""
    import app.services.report_engine as engine_mod
    from app.services.reports.intent_confirmation import confirmed_understanding

    nb = _mk_nb(repo)
    llm = _AutoRunLLM()
    eng = _mk_engine(repo, llm)
    _stub_auto_run_corpus(eng, repo, monkeypatch)
    rid = repo.create_report(nb.id, "分析 PLL 稳定性")

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(engine_mod, "confirmed_understanding", _boom)

    eng.run(nb.id, rid, "分析 PLL 稳定性", "", auto_generate=True,
            require_intent_review=True)

    detail = repo.get_report(nb.id, rid)
    assert detail["status"] == "intent_ready"      # 不得打成 failed
    assert detail["understanding"]["confirmed"] is False
    assert llm.outline_calls == 0

    monkeypatch.undo()
    understanding = confirmed_understanding(
        detail["understanding"], resolved_question="分析 PLL 环路稳定性",
    )
    assert repo.claim_report_intent(nb.id, rid, understanding)
    assert repo.get_report(nb.id, rid)["status"] == "planning"


def test_auto_generate_off_still_stops_at_intent_ready(repo, monkeypatch):
    """高级模式(默认)逐位不变:停在 intent_ready,且不记录直通意图。"""
    nb = _mk_nb(repo)
    llm = _AutoRunLLM()
    eng = _mk_engine(repo, llm)
    _stub_auto_run_corpus(eng, repo, monkeypatch)
    rid = repo.create_report(nb.id, "分析 PLL 稳定性")

    eng.run(nb.id, rid, "分析 PLL 稳定性", "", require_intent_review=True)

    detail = repo.get_report(nb.id, rid)
    assert detail["status"] == "intent_ready"
    assert detail["understanding"]["auto_generate_requested"] is False
    assert detail["understanding"]["confirmed"] is False
    assert llm.outline_calls == 0

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
    assert s.report_section_max_tokens == 8192
    assert s.report_allow_parametric is True


def test_report_settings_env(monkeypatch):
    from app.core.config import Settings
    monkeypatch.setenv("REPORT_MAX_SECTIONS", "4")
    monkeypatch.setenv("REPORT_ALLOW_PARAMETRIC", "false")
    s = Settings(_env_file=None)
    assert s.report_max_sections == 4
    assert s.report_allow_parametric is False


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
    lst = repo.list_reports(nb.id)
    assert len(lst) == 1 and lst[0]["id"] == rid and "content_md" not in lst[0]
    repo.delete_report(nb.id, rid)
    assert repo.list_reports(nb.id) == []
    with pytest.raises(KeyError):
        repo.get_report(nb.id, rid)


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
        source_refs_enabled=False,
    )
    assert "before seeing any corpus" in intent and "原始问题" in intent
    assert "mandatory_topics" in REPORT_INTENT_SCHEMA_HINT
    assert "ambiguities" in REPORT_INTENT_SCHEMA_HINT and "needs_clarification" in intent
    assert "source_refs" not in REPORT_INTENT_SCHEMA_HINT
    assert "source_refs" not in intent
    confirmed = query_intent_prompt(
        "已确认的问题", history_block="对象：PLL", purpose="deep report",
        confirmation_mode=True, source_refs_enabled=False,
    )
    assert "authoritative" in confirmed and "needs_clarification=false" in confirmed


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

def test_run_sections_concurrency_uses_report_section_parallelism(repo, monkeypatch):
    """并发 = min(节数, report_section 所属模型并行度)。"""
    original_parallelism = repo._runtime.models.parallelism
    monkeypatch.setattr(
        repo._runtime.models, "parallelism",
        lambda workload_id: 5 if workload_id == "report_section"
        else original_parallelism(workload_id),
    )
    eng = _mk_engine(repo, _OutlineLLM())
    seen = {"max": 0, "cur": 0}
    import threading as _t
    lk = _t.Lock()
    # 4 节全部到齐才放行 —— 确定性地观测真并行度(否则极快 stub 会逐个跑完不重叠)。
    barrier = _t.Barrier(4, timeout=5)
    from app.services.reasoning_retrieval import ReasoningResult
    def _dd(nb_id, section, question, depth=None, on_step=None):
        with lk:
            seen["cur"] += 1; seen["max"] = max(seen["max"], seen["cur"])
        try:
            barrier.wait()
            return ReasoningResult()
        finally:
            with lk: seen["cur"] -= 1
    monkeypatch.setattr(eng, "_deep_dive", _dd)
    nb = _mk_nb(repo); rid = repo.create_report(nb.id, "q")
    outline = [{"title": f"S{i}", "scope": "s", "sub_queries": ["q"]} for i in range(4)]
    eng._run_sections(nb.id, rid, outline, "q", depth=2)
    assert seen["max"] == 4          # 4 节 ≤ 上限5 → 全并行


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


def test_standard_report_drafts_ready_section_before_slowest_retrieval(repo, monkeypatch):
    from types import SimpleNamespace
    import threading

    eng = _mk_engine(repo, _OutlineLLM())
    allow_slow_retrieval = threading.Event()
    events = []

    def _deep(_nb, section, *_args, **_kwargs):
        if section["title"] == "B":
            assert allow_slow_retrieval.wait(timeout=3)
        events.append(f"retrieve:{section['title']}")
        return SimpleNamespace(top_hits=[], elements=[], chunks=[])

    def _draft(_nb, section, *_args, **_kwargs):
        events.append(f"draft:{section['title']}")
        if section["title"] == "A":
            allow_slow_retrieval.set()
        return {"title": section["title"], "scope": "s", "markdown": "## x",
                "grounded": False, "id_map": {}, "claims": [],
                "claim_ledger_status": "missing"}

    monkeypatch.setattr(eng, "_deep_dive", _deep)
    monkeypatch.setattr(eng, "_draft_section", _draft)
    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "q")
    sections = eng._run_sections(
        nb.id, rid,
        [{"title": "A", "scope": "s"}, {"title": "B", "scope": "s"}],
        "q", depth=2,
    )
    assert [section["title"] for section in sections] == ["A", "B"]
    assert events.index("draft:A") < events.index("retrieve:B")


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
        nb.id, rid, [{"title": "A", "scope": "s", "sub_queries": ["A"]}],
        "q", depth=8,
    )
    assert llm.synthesis_calls == 1
    assert "synthesis" not in draft_options[0]
    assert "_synthesis_blueprint" not in sections[0]
    assert sections[0]["_synthesis_status"] == "failed_validation"
    assert ("report_synthesis", "report_summary") in notes
    assert events[-1]["kind"] == "report_synthesis_failed"


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
        nb.id, rid, [{"title": "A", "scope": "s"}], "q", depth=8
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
        "_probe_queries",
        lambda notebook_id, queries: observed.append(list(queries)) or {
            "hits": 0, "base_hits": 0, "element_hits": 0, "source_hits": 0,
        },
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
    rid = repo.create_report(nb.id, "why bandgap 1.2V")
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
        lambda notebook_id, value: calls.append("intent_probe") or [{
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
    monkeypatch.setattr(eng, "_probe_sufficiency", lambda *args: [])
    monkeypatch.setattr(eng, "_judge_sufficiency", lambda question, sections, probe: sections)
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
    monkeypatch.setattr(eng, "_probe_intent_coverage", lambda *_args: [])
    monkeypatch.setattr(eng, "_build_corpus_map", lambda *_args: "MAP")
    monkeypatch.setattr(
        eng, "_storm_outline",
        lambda *_args, **_kwargs: [{
            "title": "原问题", "scope": "s", "sub_queries": ["用户原问题"],
            "intent_ids": ["intent-1"],
        }],
    )
    monkeypatch.setattr(eng, "_probe_sufficiency", lambda *_args: [])
    monkeypatch.setattr(eng, "_judge_sufficiency", lambda _q, sections, _p: sections)

    eng.plan_outline(nb.id, rid, "用户原问题")

    assert events == [{
        "kind": "report_corpus_profile_failed",
        "notebook_id": nb.id,
        "report_id": rid,
    }]
    detail = repo.get_report(nb.id, rid)
    assert detail["understanding"]["corpus_profile"] == {}


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
        def chat_json(self,*a,**k): return json.dumps({"summary":"总"})
    _bind_report_llm(repo, _S())
    eng.generate(nb.id, rid, "q", depth=2)
    d=repo.get_report(nb.id, rid)
    assert d["status"]=="done" and d["content_md"].startswith("#")


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


def test_draft_section_empty_content_marks_failed_and_observable(repo):
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
    # spy 可观测(Task 25:引擎经 ModelErrorSink 端口 = runtime 的模型 provider)
    repo._runtime.models.note_model_error = (
        lambda stage, error, *, workload_id: notes.append((stage, workload_id))
    )
    out = eng._draft_section(nb.id, {"title": "T", "scope": "S"}, "q", ReasoningResult())
    assert stub.calls == 2                                   # 空 markdown 触发重试
    assert out["markdown"] == ""
    assert out.get("failed") is True and out.get("error")   # 不再静默:标 failed→渲染 note
    assert ("report_section", "report_section") in notes   # 精确工作负载可观测


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


def test_final_editor_reports_uncovered_intent_without_rewriting_sections(repo):
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

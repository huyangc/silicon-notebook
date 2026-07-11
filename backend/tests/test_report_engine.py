"""深度报告(report_engine)测试:配置项 / reports CRUD / 三 prompt / 引擎编排。"""
import json

import pytest


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
    return SQLiteRepository(Settings(_env_file=None))


def _mk_nb(repo):
    from app.models.schemas import NotebookCreate
    return repo.create_notebook(NotebookCreate(name="t", purpose="p", primary_domain="d"))


def test_report_crud_roundtrip(repo):
    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "为什么 bandgap 是 1.2V?")
    assert rid.startswith("rep-")
    detail = repo.get_report(nb.id, rid)
    assert detail["status"] == "pending" and detail["question"].startswith("为什么")
    repo.update_report(nb.id, rid, status="running", progress="大纲规划中")
    repo.update_report(nb.id, rid, outline=[{"title": "机理", "scope": "s", "sub_queries": ["q1"]}],
                       sections=[{"title": "机理", "markdown": "md", "grounded": True}],
                       gaps=["缺 X"], content_md="# 报告", status="done", progress="完成")
    detail = repo.get_report(nb.id, rid)
    assert detail["status"] == "done" and detail["content_md"] == "# 报告"
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
        report_outline_prompt, report_section_prompt, report_summary_prompt,
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


# ---------------------------------------------------------------------------
# Task 5: report_engine——大纲 + 逐节并行深挖 + 撰写
# ---------------------------------------------------------------------------

def _mk_engine(repo, llm):
    # Task 25:引擎端口化——测试经 from_repository 冻结适配器构造(提取窄端口,
    # 不再持 facade);检索桩改打在 repo.retrieval / repo._runtime.* 的所有者上。
    from app.services.report_engine import ReportEngine
    repo.llm_client = llm
    return ReportEngine.from_repository(repo, repo.settings)


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
    repo.llm_client = llm
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
    assert "## A" in md and "## B" in md
    assert "## 参考文献" in md and "Razavi" in md
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
    """跨节 [k] 全局按来源去重重编号:同一来源在不同节共享同一全局 [k];未知
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

    # 全局去重:Razavi=k1(A、B 共享)、Gray=k2 → 2 条 references
    assert [r["key"] for r in references] == ["k1", "k2"]
    assert references[0]["label"] == "Razavi Analog CMOS"
    assert references[1]["label"] == "Gray & Meyer"
    # A 段:k1/k2 保留、幻觉 k9 被剥除
    assert "[k1]" in md and "[k2]" in md and "[k9]" not in md and "幻觉 。" in md
    # B 段:节内 k1(Razavi)→ 全局仍 k1(与 A 的 Razavi 同号)。仅取 B 正文
    # (到下一个 ## 标题止,避免命中「参考文献」段里罗列的 [k2]）。
    b_seg = md.split("## B")[1].split("\n## ")[0]
    assert "[k1]" in b_seg and "[k2]" not in b_seg
    # 参考文献段列出 [k1]/[k2] + 标题
    assert "## 参考文献" in md
    assert "[k1]" in md.split("## 参考文献")[1] and "Razavi Analog CMOS" in md.split("## 参考文献")[1]


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
# Task 2(perf): depth 穿透 + 并发复用 kg_job_concurrency + 节内实时进度
# ---------------------------------------------------------------------------

def test_run_sections_concurrency_uses_kg_job_concurrency(repo, monkeypatch):
    """并发 = min(节数, kg_job_concurrency);节数≤上限时全并行。"""
    monkeypatch.setattr(repo.settings, "kg_job_concurrency", 5)
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


def test_deep_dive_passes_depth_as_max_steps(repo, monkeypatch):
    eng = _mk_engine(repo, _OutlineLLM())
    captured = {}
    from app.services.reasoning_retrieval import ReasoningResult
    class _R:
        def __init__(self, *a, **k): pass          # 端口化:仅关键字注入
        def run(self, nb_id, q, **kw):
            captured.update(kw); return ReasoningResult()
    monkeypatch.setattr("app.services.reasoning_retrieval.ReasoningRetriever", _R)
    eng._deep_dive("nb", {"title": "t", "scope": "s", "sub_queries": ["q"]}, "Q", depth=3, on_step=None)
    assert captured.get("max_steps") == 3
    assert captured.get("top_n") is None   # 不传 top_n → 与 ask 同一套自适应证据预算


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
    p = report_storm_outline_prompt("Q问题", "CORPUSMAP内容", max_sections=5, history_block="H历史")
    for kw in ("expert perspectives", "raise", "cluster", "tension", "MECE",
               "vocabulary", "CORPUSMAP内容", "Q问题", "H历史", "3-5"):
        assert kw in p
    assert "perspectives" in REPORT_STORM_SCHEMA_HINT and "tensions" in REPORT_STORM_SCHEMA_HINT


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
    out = eng._probe_sufficiency("nb", [{"title":"A","sub_queries":["base-x","y"]},
                                        {"title":"B","sub_queries":[]}])
    assert out[0]["title"]=="A" and out[0]["hits"]==2 and out[0]["base_hits"]==1
    assert out[1]["hits"]==0

def test_sufficiency_prompt_contract():
    from app.services.prompts import report_sufficiency_prompt, REPORT_SUFFICIENCY_SCHEMA_HINT
    p = report_sufficiency_prompt("Q", "PROBEBLOCK")
    assert "sufficiency" in p and "PROBEBLOCK" in p and "Q" in p
    assert "gap_note" in REPORT_SUFFICIENCY_SCHEMA_HINT and "action" in REPORT_SUFFICIENCY_SCHEMA_HINT


# ---------------------------------------------------------------------------
# Task 4(STORM): plan_outline 编排(map→STORM→探针→Judge→富大纲→outline_ready)
# ---------------------------------------------------------------------------

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
    repo.llm_client = _LLM()          # reasoning/rewrite 都回退到它(测试桩)
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

def test_plan_outline_falls_back_on_bad_storm_json(repo, monkeypatch):
    from app.services.report_engine import ReportEngine
    nb = _mk_nb(repo)
    class _Bad:
        configured=True
        def chat_json(self, *a, **k): return "not json"
    repo.llm_client=_Bad()
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
    repo.llm_client=_S()
    eng.generate(nb.id, rid, "q", depth=2)
    d=repo.get_report(nb.id, rid)
    assert d["status"]=="done" and d["content_md"].startswith("#")

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
    repo._runtime.models.note_model_error = lambda stage, model, exc: notes.append(stage)
    out = eng._draft_section(nb.id, {"title": "T", "scope": "S"}, "q", ReasoningResult())
    assert stub.calls == 2                                   # 空 markdown 触发重试
    assert out["markdown"] == ""
    assert out.get("failed") is True and out.get("error")   # 不再静默:标 failed→渲染 note
    assert "report_section" in notes                        # report_engine 首次有 model_error 可观测


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

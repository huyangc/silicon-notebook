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
    assert s.report_section_top_n == 12
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
    from app.services.report_engine import ReportEngine
    repo.llm_client = llm
    return ReportEngine(repo, repo.settings)


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
            if self._fail_left > 0:
                self._fail_left -= 1
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
    nb = _mk_nb(repo)
    llm = _OutlineLLM(n_fail_sections=1)
    eng = _mk_engine(repo, llm)
    # stub 每节深挖:不跑真检索,返回空 ReasoningResult(编排测试与检索解耦)
    from app.services.reasoning_retrieval import ReasoningResult
    monkeypatch.setattr(eng, "_deep_dive",
                        lambda nb_id, section, question, depth=None, on_step=None: ReasoningResult())
    rid = repo.create_report(nb.id, "q")
    eng.run(nb.id, rid, "q", "")
    detail = repo.get_report(nb.id, rid)
    assert detail["status"] == "done"
    secs = detail["sections"]
    assert len(secs) == 2
    ok = [s for s in secs if not s.get("failed")]
    bad = [s for s in secs if s.get("failed")]
    assert len(ok) == 1 and len(bad) == 1          # 单节失败不拖垮整报告
    assert detail["content_md"].startswith("#")     # 汇总仍生成


def test_engine_cancel_marks_cancelled(repo, monkeypatch):
    import threading
    nb = _mk_nb(repo)
    llm = _OutlineLLM()
    cancel = threading.Event()
    from app.services.report_engine import ReportEngine
    repo.llm_client = llm
    eng = ReportEngine(repo, repo.settings, cancel_event=cancel)
    from app.services.reasoning_retrieval import ReasoningResult
    def _dd(nb_id, section, question, depth=None, on_step=None):
        cancel.set()                                # 深挖中途被取消
        from app.services.cancellation import raise_if_cancelled
        raise_if_cancelled(cancel)
    monkeypatch.setattr(eng, "_deep_dive", _dd)
    rid = repo.create_report(nb.id, "q")
    eng.run(nb.id, rid, "q", "")
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
    monkeypatch.setattr(eng.repo, "_retrieve_neighbors", lambda *a, **k: [])
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
        def __init__(self, *a): pass
        def run(self, nb_id, q, **kw):
            captured.update(kw); return ReasoningResult()
    monkeypatch.setattr("app.services.reasoning_retrieval.ReasoningRetriever", _R)
    eng._deep_dive("nb", {"title": "t", "scope": "s", "sub_queries": ["q"]}, "Q", depth=3, on_step=None)
    assert captured.get("max_steps") == 3


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
    eng = ReportEngine(repo, repo.settings)
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
    monkeypatch.setattr(repo, "federated_retrieve", _fed)
    monkeypatch.setattr(repo, "_ppr_retrieve", _ppr)
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

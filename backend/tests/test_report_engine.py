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
    assert s.report_section_concurrency == 3


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
                        lambda nb_id, section, question: ReasoningResult())
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
    def _dd(nb_id, section, question):
        cancel.set()                                # 深挖中途被取消
        from app.services.cancellation import raise_if_cancelled
        raise_if_cancelled(cancel)
    monkeypatch.setattr(eng, "_deep_dive", _dd)
    rid = repo.create_report(nb.id, "q")
    eng.run(nb.id, rid, "q", "")
    assert repo.get_report(nb.id, rid)["status"] == "cancelled"

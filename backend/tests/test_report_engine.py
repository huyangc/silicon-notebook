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

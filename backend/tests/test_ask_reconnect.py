"""WS2b: 轨迹持久化 + ask_job_detail + 会话 active_job 暴露在途 turn。"""
import threading
import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest, AskResponse


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_PROVIDER": "dashscope", "EMBED_BASE_URL": "https://e.test",
                 "EMBED_API_KEY": "k", "EMBED_MODEL": "m", "EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _begin(repo, mode="reasoning", conv=None):
    nb = repo.create_notebook(NotebookCreate(name="t"))
    p = AskRequest(question="Q?", mode=mode, conversation_id=conv)
    job_id, conv_id = repo.begin_ask_job(nb.id, p, mode, threading.Event())
    return nb, job_id, conv_id


def test_append_ask_trace_accumulates(repo):
    _, job_id, _ = _begin(repo)
    repo.append_ask_trace(job_id, {"step_type": "plan", "summary": "s1", "detail": {}})
    repo.append_ask_trace(job_id, {"step_type": "retrieve", "summary": "s2", "detail": {}})
    d = repo.ask_job_detail(job_id)
    assert [s["step_type"] for s in d["trace"]] == ["plan", "retrieve"]
    assert d["status"] == "running" and d["question"] == "Q?"


def test_append_ask_trace_fail_open_on_unknown_job(repo):
    repo.append_ask_trace("askjob-missing", {"step_type": "x", "summary": "", "detail": {}})  # 不抛


def test_ask_job_detail_missing_raises(repo):
    with pytest.raises(KeyError):
        repo.ask_job_detail("askjob-nope")


def test_get_conversation_exposes_running_active_job(repo):
    _, job_id, conv_id = _begin(repo)
    repo.append_ask_trace(job_id, {"step_type": "plan", "summary": "s", "detail": {}})
    detail = repo.get_conversation(conv_id)
    assert detail.active_job is not None
    assert detail.active_job.job_id == job_id
    assert detail.active_job.question == "Q?"
    assert len(detail.active_job.trace) == 1


def test_active_job_gone_after_done(repo):
    _, job_id, conv_id = _begin(repo)
    repo.finish_ask_job(job_id, "done", answer_id="ans-x")
    assert repo.get_conversation(conv_id).active_job is None

"""WS2b: 轨迹持久化 + ask_job_detail + 会话 active_job 暴露在途 turn。"""
import json
import threading
import pytest
from fastapi.testclient import TestClient

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


def test_active_job_isolated_per_conversation(repo):
    """两个会话各起一个 ask job：A 会话的 running job 不应外溢到 B 会话的
    active_job 上——get_conversation 按 conversation_id 过滤，跨会话不应串态。"""
    nb, job_a, conv_a = _begin(repo, conv=None)
    # 会话 B：同一 notebook 下新起一轮问答 → 新 conversation_id
    payload_b = AskRequest(question="Q-B?", mode="chunk")
    job_b, conv_b = repo.begin_ask_job(nb.id, payload_b, "chunk", threading.Event())
    assert conv_a != conv_b

    detail_a = repo.get_conversation(conv_a)
    detail_b = repo.get_conversation(conv_b)
    assert detail_a.active_job is not None
    assert detail_a.active_job.job_id == job_a
    assert detail_b.active_job is not None
    assert detail_b.active_job.job_id == job_b
    # 关键断言：B 会话看到的是自己的 job，不是 A 的
    assert detail_b.active_job.job_id != detail_a.active_job.job_id

    # A 完成后，B 的 running job 依旧独立可见（不受 A 收尾影响）
    repo.finish_ask_job(job_a, "done", answer_id="ans-a")
    assert repo.get_conversation(conv_a).active_job is None
    assert repo.get_conversation(conv_b).active_job is not None
    assert repo.get_conversation(conv_b).active_job.job_id == job_b


# ---- 路由级测试:GET /notebooks/{id}/ask/jobs/{job_id} ----
# 风格参照 test_ask_jobs.py 的 _api_client() —— TestClient + repository().cache_clear()。

def _api_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    from app.core.config import get_settings
    from app.api import routes
    from app.main import create_app
    get_settings.cache_clear()
    routes.repository.cache_clear()
    return TestClient(create_app())


def test_get_ask_job_endpoint_unknown_job_id_returns_404(tmp_path, monkeypatch):
    client = _api_client(tmp_path, monkeypatch)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    r = client.get(f"/api/notebooks/{nb}/ask/jobs/askjob-doesnotexist")
    assert r.status_code == 404


def test_get_ask_job_endpoint_existing_job_returns_200_with_status(tmp_path, monkeypatch):
    client = _api_client(tmp_path, monkeypatch)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    # chunk 模式走 /ask/stream 同步跑完;首个 NDJSON 事件是 started(带 job_id)——
    # 与 test_ask_jobs.py 的 test_cancel_endpoint_existing_job_returns_200_with_status
    # 同一手法拿到真实、属主的 job_id。
    stream = client.post(f"/api/notebooks/{nb}/ask/stream",
                         json={"question": "q", "mode": "chunk"})
    assert stream.status_code == 200
    events = [json.loads(l) for l in stream.text.splitlines() if l.strip()]
    job_id = events[0]["job_id"]
    assert events[0]["event"] == "started" and job_id

    r = client.get(f"/api/notebooks/{nb}/ask/jobs/{job_id}")
    assert r.status_code == 200
    body = r.json()
    assert "status" in body and "trace" in body and "answer_id" in body

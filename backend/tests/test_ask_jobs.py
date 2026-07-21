"""WS2a: ask_jobs 生命周期 + 显式取消 + 空会话清理 + 重启兜底。"""
import json
import threading
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest


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


def _nb(repo):
    return repo.create_notebook(NotebookCreate(name="t"))


def test_begin_creates_running_job_and_conversation(repo):
    nb = _nb(repo)
    ev = threading.Event()
    payload = AskRequest(question="Q1?", mode="chunk")
    job_id, conv_id = repo.begin_ask_job(nb.id, payload, "chunk", ev)
    assert job_id and conv_id
    assert payload.conversation_id == conv_id          # 就地写回,handler 接续同一会话
    st = repo.ask_job_status(job_id)
    assert st["status"] == "running" and st["conversation_id"] == conv_id
    assert st["created_by"] == repo.current_user().id
    assert repo._ask_cancel_events.get(job_id) is ev   # 注册表登记


def test_finish_done_records_answer_and_deregisters(repo):
    nb = _nb(repo)
    ev = threading.Event()
    payload = AskRequest(question="Q?", mode="chunk")
    job_id, _ = repo.begin_ask_job(nb.id, payload, "chunk", ev)
    repo.finish_ask_job(job_id, "done", answer_id="ans-x")
    st = repo.ask_job_status(job_id)
    assert st["status"] == "done" and st["answer_id"] == "ans-x"
    assert job_id not in repo._ask_cancel_events        # 注销


def test_cancel_sets_event_owner_scoped(repo):
    nb = _nb(repo)
    ev = threading.Event()
    payload = AskRequest(question="Q?", mode="chunk")
    job_id, _ = repo.begin_ask_job(nb.id, payload, "chunk", ev)
    # 非属主取消 → 不触发
    with pytest.raises(KeyError):
        repo.cancel_ask_job(job_id, "user-other")
    assert not ev.is_set()
    repo.cancel_ask_job(job_id, repo.current_user().id)   # 属主
    assert ev.is_set()


def test_finish_cancelled_cleans_empty_new_conversation(repo):
    nb = _nb(repo)
    ev = threading.Event()
    payload = AskRequest(question="Q?", mode="chunk")   # 新会话
    job_id, conv_id = repo.begin_ask_job(nb.id, payload, "chunk", ev)
    repo.finish_ask_job(job_id, "cancelled")            # 无 answer 落库
    with pytest.raises(KeyError):
        repo.get_conversation(conv_id)                  # 空壳被清理


def test_finish_cancelled_keeps_conversation_with_prior_answers(repo):
    nb = _nb(repo)
    ev = threading.Event()
    p1 = AskRequest(question="Q1?", mode="chunk")
    job1, conv_id = repo.begin_ask_job(nb.id, p1, "chunk", ev)
    repo._save_answer(nb.id, "Q1?", _fake_answer(conv_id), conv_id)  # 该会话已有答案
    repo.finish_ask_job(job1, "done", answer_id="ans-1")
    # 第二轮取消,会话仍有前一轮答案 → 不删
    p2 = AskRequest(question="Q2?", mode="chunk", conversation_id=conv_id)
    job2, _ = repo.begin_ask_job(nb.id, p2, "chunk", threading.Event())
    repo.finish_ask_job(job2, "cancelled")
    assert repo.get_conversation(conv_id).turn_count == 1


def test_recover_interrupted_marks_running_ask_jobs(repo):
    nb = _nb(repo)
    ev = threading.Event()
    job_id, _ = repo.begin_ask_job(nb.id, AskRequest(question="Q?", mode="chunk"), "chunk", ev)
    repo._recover_interrupted_jobs()
    assert repo.ask_job_status(job_id)["status"] == "interrupted"


def _fake_answer(conv_id):
    from app.models.schemas import AskResponse
    return AskResponse(answer_id="", conversation_id=conv_id, conclusion="", answer="a",
                       grounded=True, anchors=[], related_knowledge=[], citations=[], llm_mode="x")


# ---- 路由级测试:POST /notebooks/{id}/ask/jobs/{job_id}/cancel ----
# 风格参照 test_ask_modes_api.py 的 _client() —— TestClient + repository().cache_clear()。

def _api_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    from app.core.config import get_settings
    from app.api import ask_routes
    from app.main import create_app
    get_settings.cache_clear()
    ask_routes.repository.cache_clear()
    return TestClient(create_app())


def test_cancel_endpoint_unknown_job_id_returns_404(tmp_path, monkeypatch):
    client = _api_client(tmp_path, monkeypatch)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    r = client.post(f"/api/notebooks/{nb}/ask/jobs/askjob-doesnotexist/cancel")
    assert r.status_code == 404


def test_cancel_endpoint_existing_job_returns_200_with_status(tmp_path, monkeypatch):
    client = _api_client(tmp_path, monkeypatch)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    # chunk 模式走 /ask/stream 同步跑完;首个 NDJSON 事件是 started(带 job_id),
    # 供前端「停止」按钮打 cancel 端点(与 test_ask_modes_api.py 的
    # test_chunk_mode_streams_start_then_final 同一手法拿到真实 job_id)。
    stream = client.post(f"/api/notebooks/{nb}/ask/stream",
                         json={"question": "q", "mode": "chunk"})
    assert stream.status_code == 200
    events = [json.loads(l) for l in stream.text.splitlines() if l.strip()]
    job_id = events[0]["job_id"]
    assert events[0]["event"] == "started" and job_id

    r = client.post(f"/api/notebooks/{nb}/ask/jobs/{job_id}/cancel")
    assert r.status_code == 200
    body = r.json()
    assert "status" in body


# ---- Task 22: 持久化在 runtime.ask_state 组件;facade 保留取消注册编排 ----

def test_begin_and_finish_delegate_persistence_to_runtime_ask_state(repo, monkeypatch):
    """begin/finish 的持久化走 AskStateStore(显式 user_id);facade 只保
    cancel-event 注册/注销与「cancelled/failed 清空会话」编排,行为不变。"""
    nb = _nb(repo)
    seen = []
    store = repo._runtime.ask_state
    real_begin, real_finish = store.begin_durable_job, store.finish_job

    def spy_begin(notebook_id, payload, mode, user_id):
        seen.append(("begin", user_id))
        return real_begin(notebook_id, payload, mode, user_id)

    def spy_finish(job_id, status, *, answer_id="", error=""):
        seen.append(("finish", status))
        return real_finish(job_id, status, answer_id=answer_id, error=error)

    monkeypatch.setattr(store, "begin_durable_job", spy_begin)
    monkeypatch.setattr(store, "finish_job", spy_finish)
    ev = threading.Event()
    payload = AskRequest(question="Q?", mode="chunk")
    job_id, conv_id = repo.begin_ask_job(nb.id, payload, "chunk", ev)
    assert repo._ask_cancel_events.get(job_id) is ev      # facade 注册表仍在
    repo.finish_ask_job(job_id, "cancelled")
    assert job_id not in repo._ask_cancel_events          # 注销
    assert seen == [("begin", repo.current_user().id), ("finish", "cancelled")]
    with pytest.raises(KeyError):
        repo.get_conversation(conv_id)                    # 空壳清理仍生效

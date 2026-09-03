"""WS2a: ask_jobs 生命周期 + 显式取消 + 空会话清理 + 重启兜底。"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest
from tests.model_testkit import bind_all_embedding_clients
from tests.ask_testkit import seed_ask_evidence


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
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


def test_begin_preserves_full_valid_question(repo):
    nb = _nb(repo)
    question = "完整提问：" + "电路噪声分析" * 60
    payload = AskRequest(question=question, mode="chunk")
    job_id, _ = repo.begin_ask_job(nb.id, payload, "chunk", threading.Event())

    assert repo.ask_job_detail(job_id)["question"] == question


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
    from app.api.ask_routes import repository
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    seed_ask_evidence(repository(), nb)  # PR#334:空库 ask 会 409,先塞一条证据
    # chunk 模式走 /ask/stream 同步跑完;首个 NDJSON 事件是 started(带 job_id),
    # 供前端「停止」按钮打 cancel 端点(与 test_ask_modes_api.py 的
    # test_chunk_mode_streams_start_then_final 同一手法拿到真实 job_id)。
    stream = client.post(f"/api/notebooks/{nb}/ask/stream",
                         json={"question": "q", "mode": "chunk"})
    assert stream.status_code == 200
    events = [json.loads(l) for l in stream.text.splitlines() if l.strip()]
    job_id = events[0]["job_id"]
    assert events[0]["event"] == "started" and job_id

    other_nb = client.post("/api/notebooks", json={"name": "other"}).json()["id"]
    cross = client.post(f"/api/notebooks/{other_nb}/ask/jobs/{job_id}/cancel")
    assert cross.status_code == 404

    r = client.post(f"/api/notebooks/{nb}/ask/jobs/{job_id}/cancel")
    assert r.status_code == 200
    body = r.json()
    assert "status" in body


def test_cancel_endpoint_wins_before_final_answer_save_atomically(tmp_path, monkeypatch):
    """A durable explicit cancel must close the final-save race window.

    The worker is stopped immediately before the production answer-store call.
    The real HTTP cancel endpoint then wins, after which releasing the worker
    must produce a cancelled stream and no durable answer row.
    """
    client = _api_client(tmp_path, monkeypatch)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    from app.api import ask_routes

    repo = ask_routes.repository()
    seed_ask_evidence(repo, nb)  # PR#334:空库 ask 会 409,先塞一条证据
    store = repo._runtime.ask_state
    job_started = threading.Event()
    save_entered = threading.Event()
    release_save = threading.Event()
    captured: dict[str, str] = {}
    real_begin = store.begin_durable_job
    real_save = store.save_answer_for_job

    def capture_begin(notebook_id, payload, mode, user_id):
        result = real_begin(notebook_id, payload, mode, user_id)
        captured["job_id"], captured["conversation_id"] = result
        job_started.set()
        return result

    def blocked_save(
        job_id, notebook_id, conversation_id, question, response, user_id
    ):
        save_entered.set()
        assert release_save.wait(timeout=5)
        return real_save(
            job_id, notebook_id, conversation_id, question, response, user_id
        )

    monkeypatch.setattr(store, "begin_durable_job", capture_begin)
    monkeypatch.setattr(store, "save_answer_for_job", blocked_save)

    with ThreadPoolExecutor(max_workers=1) as executor:
        stream_future = executor.submit(
            client.post,
            f"/api/notebooks/{nb}/ask/stream",
            json={"question": "cancel before save", "mode": "chunk"},
        )
        try:
            assert job_started.wait(timeout=5)
            assert save_entered.wait(timeout=5)
            cancelled = client.post(
                f"/api/notebooks/{nb}/ask/jobs/{captured['job_id']}/cancel"
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
        finally:
            release_save.set()
        stream = stream_future.result(timeout=5)

    events = [json.loads(line) for line in stream.text.splitlines() if line.strip()]
    assert any(event["event"] == "cancelled" for event in events)
    assert not any(event["event"] == "final" for event in events)
    assert repo.ask_job_status(captured["job_id"])["status"] == "cancelled"
    with repo._connect() as db:
        count = db.execute(
            "SELECT COUNT(*) AS n FROM answers WHERE notebook_id=?", (nb,)
        ).fetchone()["n"]
    assert count == 0


def test_sync_ask_cancel_endpoint_returns_no_final_answer_or_empty_conversation(
    tmp_path, monkeypatch
):
    client = _api_client(tmp_path, monkeypatch)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    from app.api import ask_routes

    repo = ask_routes.repository()
    seed_ask_evidence(repo, nb)  # PR#334:空库 ask 会 409,先塞一条证据
    store = repo._runtime.ask_state
    job_started = threading.Event()
    save_entered = threading.Event()
    release_save = threading.Event()
    captured: dict[str, str] = {}
    real_begin = store.begin_durable_job
    real_save = store.save_answer_for_job

    def capture_begin(notebook_id, payload, mode, user_id):
        result = real_begin(notebook_id, payload, mode, user_id)
        captured["job_id"], captured["conversation_id"] = result
        job_started.set()
        return result

    def blocked_save(
        job_id, notebook_id, conversation_id, question, response, user_id
    ):
        save_entered.set()
        assert release_save.wait(timeout=5)
        return real_save(
            job_id, notebook_id, conversation_id, question, response, user_id
        )

    monkeypatch.setattr(store, "begin_durable_job", capture_begin)
    monkeypatch.setattr(store, "save_answer_for_job", blocked_save)

    with ThreadPoolExecutor(max_workers=1) as executor:
        ask_future = executor.submit(
            client.post,
            f"/api/notebooks/{nb}/ask",
            json={"question": "cancel synchronous answer", "mode": "chunk"},
        )
        try:
            assert job_started.wait(timeout=5)
            assert save_entered.wait(timeout=5)
            cancelled = client.post(
                f"/api/notebooks/{nb}/ask/jobs/{captured['job_id']}/cancel"
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
        finally:
            release_save.set()
        response = ask_future.result(timeout=5)

    assert response.status_code == 409
    assert response.json()["detail"] == "Ask cancelled"
    assert repo.ask_job_status(captured["job_id"])["status"] == "cancelled"
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS n FROM answers WHERE notebook_id=?", (nb,)
        ).fetchone()["n"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS n FROM conversations WHERE id=?",
            (captured["conversation_id"],),
        ).fetchone()["n"] == 0


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


# ---- 提交幂等键(client_request_id):同键重发接回既有 job,不建第二个 ----

def test_begin_or_attach_reuses_the_job_for_a_repeated_client_request_id(repo):
    nb = _nb(repo)
    store = repo._runtime.ask_state
    uid = repo.current_user().id
    first = AskRequest(question="Q?", mode="reasoning", client_request_id="key-1")
    job_id, conv_id, attached = store.begin_or_attach_durable_job(nb.id, first, "reasoning", uid)
    assert not attached and first.conversation_id == conv_id

    again = AskRequest(question="Q?", mode="reasoning", client_request_id="key-1")
    assert store.begin_or_attach_durable_job(nb.id, again, "reasoning", uid) == (
        job_id, conv_id, True)
    assert again.conversation_id == conv_id          # 与新建同样就地写回
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id, client_request_id FROM ask_jobs WHERE notebook_id=?", (nb.id,)
        ).fetchall()
    assert [(r["id"], r["client_request_id"]) for r in rows] == [(job_id, "key-1")]
    assert len(repo.list_conversations(nb.id)) == 1


def test_begin_or_attach_without_a_key_always_creates(repo):
    nb = _nb(repo)
    store = repo._runtime.ask_state
    uid = repo.current_user().id
    a = store.begin_or_attach_durable_job(
        nb.id, AskRequest(question="Q?", mode="chunk"), "chunk", uid)
    b = store.begin_or_attach_durable_job(
        nb.id, AskRequest(question="Q?", mode="chunk"), "chunk", uid)
    assert a[2] is False and b[2] is False and a[0] != b[0]
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS n FROM ask_jobs WHERE client_request_id IS NULL"
        ).fetchone()["n"] == 2


def test_begin_or_attach_rejects_a_key_spent_in_another_notebook(repo):
    from app.repositories.ports import AskRequestKeyConflict

    nb, other = _nb(repo), _nb(repo)
    store = repo._runtime.ask_state
    uid = repo.current_user().id
    store.begin_or_attach_durable_job(
        nb.id, AskRequest(question="Q?", mode="chunk", client_request_id="key-x"), "chunk", uid)
    with pytest.raises(AskRequestKeyConflict):
        store.begin_or_attach_durable_job(
            other.id, AskRequest(question="Q?", mode="chunk", client_request_id="key-x"),
            "chunk", uid)
    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM ask_jobs").fetchone()["n"] == 1
    assert repo.list_conversations(other.id) == []


def test_ask_request_validates_client_request_id():
    from pydantic import ValidationError

    assert AskRequest(question="q", client_request_id="  ").client_request_id is None
    assert AskRequest(question="q").client_request_id is None
    ok = AskRequest(question="q", client_request_id=" 0f1a-2b3c_4d:5e.6f ")
    assert ok.client_request_id == "0f1a-2b3c_4d:5e.6f"
    with pytest.raises(ValidationError):
        AskRequest(question="q", client_request_id="bad key!")
    with pytest.raises(ValidationError):
        AskRequest(question="q", client_request_id="x" * 129)


def test_stream_resubmission_with_the_same_client_request_id_attaches_to_the_job(
    tmp_path, monkeypatch,
):
    """浏览器在交接后、`started` 之前刷新,带同一个键重发:服务端不建第二个 job,
    而是以同一 job/会话 id 发 `started`,再把已存的结果作为 `final` 回放。"""
    client = _api_client(tmp_path, monkeypatch)
    from app.api.ask_routes import repository
    repo = repository()
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    seed_ask_evidence(repo, nb)
    body = {"question": "same submission", "mode": "chunk", "client_request_id": "sub-1"}

    first = client.post(f"/api/notebooks/{nb}/ask/stream", json=body)
    assert first.status_code == 200
    first_events = [json.loads(l) for l in first.text.splitlines() if l.strip()]
    assert first_events[0]["event"] == "started"
    assert first_events[-1]["event"] == "final"

    again = client.post(f"/api/notebooks/{nb}/ask/stream", json=body)
    assert again.status_code == 200
    again_events = [json.loads(l) for l in again.text.splitlines() if l.strip()]
    assert again_events[0] == first_events[0]
    assert again_events[-1]["event"] == "final"
    assert again_events[-1]["response"]["answer_id"] == first_events[-1]["response"]["answer_id"]
    assert again_events[-1]["response"]["answer"] == first_events[-1]["response"]["answer"]
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS n FROM ask_jobs WHERE notebook_id=?", (nb,)
        ).fetchone()["n"] == 1
        assert db.execute(
            "SELECT COUNT(*) AS n FROM answers WHERE notebook_id=?", (nb,)
        ).fetchone()["n"] == 1
    assert len(repo.list_conversations(nb)) == 1

    # The same key under another notebook is a client defect: a well-formed
    # stream that carries only an error, and no job in that notebook.
    other = client.post("/api/notebooks", json={"name": "other"}).json()["id"]
    seed_ask_evidence(repo, other)
    cross = client.post(f"/api/notebooks/{other}/ask/stream", json=body)
    assert cross.status_code == 200
    cross_events = [json.loads(l) for l in cross.text.splitlines() if l.strip()]
    from app.services.ask_execution import KEY_CONFLICT_MESSAGE
    assert cross_events == [{"event": "error", "error": KEY_CONFLICT_MESSAGE}]
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS n FROM ask_jobs WHERE notebook_id=?", (other,)
        ).fetchone()["n"] == 0

    malformed = client.post(f"/api/notebooks/{nb}/ask/stream",
                            json={**body, "client_request_id": "no spaces allowed"})
    assert malformed.status_code == 422


def test_sync_begin_never_stores_the_key_and_always_creates(repo):
    """codex #665 r1 P2: the synchronous path keeps its always-create
    semantics — a repeated keyed call must not trip the unique index."""
    nb = _nb(repo)
    store = repo._runtime.ask_state
    uid = repo.current_user().id
    first = AskRequest(question="Q?", mode="chunk", client_request_id="sync-key")
    second = AskRequest(question="Q?", mode="chunk", client_request_id="sync-key")
    a = store.begin_durable_job(nb.id, first, "chunk", uid)
    b = store.begin_durable_job(nb.id, second, "chunk", uid)
    assert a[0] != b[0]
    with repo._connect() as db:
        rows = db.execute(
            "SELECT client_request_id FROM ask_jobs WHERE notebook_id=?", (nb.id,)
        ).fetchall()
    assert [r["client_request_id"] for r in rows] == [None, None]
    assert store.find_job_for_client_request(uid, "sync-key") is None


def test_sync_ask_endpoint_accepts_a_repeated_key_and_always_answers(tmp_path, monkeypatch):
    client = _api_client(tmp_path, monkeypatch)
    from app.api.ask_routes import repository
    repo = repository()
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    seed_ask_evidence(repo, nb)
    body = {"question": "sync twice", "mode": "chunk", "client_request_id": "sync-1"}
    first = client.post(f"/api/notebooks/{nb}/ask", json=body)
    second = client.post(f"/api/notebooks/{nb}/ask", json=body)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["answer_id"] != second.json()["answer_id"]
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS n FROM ask_jobs WHERE notebook_id=?", (nb,)
        ).fetchone()["n"] == 2


def test_keyed_retry_attaches_even_when_the_preflight_would_now_reject(tmp_path, monkeypatch):
    """codex #665 r1 P2: the original submission passed the route's preflight;
    a retry under the same key replays its job even after the notebook lost
    its retrievable evidence (the preflight would answer 409 to a NEW ask)."""
    client = _api_client(tmp_path, monkeypatch)
    from app.api.ask_routes import repository
    repo = repository()
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    seed_ask_evidence(repo, nb)
    body = {"question": "retry after change", "mode": "chunk", "client_request_id": "pre-1"}
    first = client.post(f"/api/notebooks/{nb}/ask/stream", json=body)
    first_events = [json.loads(l) for l in first.text.splitlines() if l.strip()]
    assert first_events[-1]["event"] == "final"

    # The notebook is now empty: a fresh ask is refused by the preflight...
    with repo._write() as db:
        db.execute("DELETE FROM chunks WHERE notebook_id=?", (nb,))
        db.execute("DELETE FROM sources WHERE notebook_id=?", (nb,))
    fresh = client.post(f"/api/notebooks/{nb}/ask/stream",
                        json={**body, "client_request_id": "pre-2"})
    assert fresh.status_code == 409
    # ...but the keyed retry attaches to the job the original created.
    retry = client.post(f"/api/notebooks/{nb}/ask/stream", json=body)
    assert retry.status_code == 200
    retry_events = [json.loads(l) for l in retry.text.splitlines() if l.strip()]
    assert retry_events[0] == first_events[0]
    assert retry_events[-1]["response"]["answer_id"] == first_events[-1]["response"]["answer_id"]

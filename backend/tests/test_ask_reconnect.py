"""WS2b: 轨迹持久化 + ask_job_detail + 会话 active_job 暴露在途 turn。"""
import json
import threading
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest, AskResponse
from tests.model_testkit import bind_all_embedding_clients


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


def test_append_ask_trace_writes_to_subtable_not_trace_json_column(repo):
    """perf fast-follow: append_ask_trace 现追加进 ask_trace_steps 子表(O(1) 单行
    INSERT),不再对 ask_jobs.trace_json 做「读整个 JSON 数组→append→写回」的
    O(N^2) read-modify-write。该列继续保留(兼容旧行)但停止写入——留空。"""
    _, job_id, _ = _begin(repo)
    repo.append_ask_trace(job_id, {"step_type": "plan", "summary": "s1", "detail": {}})
    repo.append_ask_trace(job_id, {"step_type": "retrieve", "summary": "s2", "detail": {}})
    with repo._connect() as db:
        rows = db.execute(
            "SELECT seq, step_json FROM ask_trace_steps WHERE job_id=? ORDER BY seq ASC",
            (job_id,),
        ).fetchall()
        trace_json_col = db.execute(
            "SELECT trace_json FROM ask_jobs WHERE id=?", (job_id,)
        ).fetchone()["trace_json"]
    assert [r["seq"] for r in rows] == [0, 1]
    assert [json.loads(r["step_json"])["step_type"] for r in rows] == ["plan", "retrieve"]
    # 新写不再落 trace_json 列——保持默认空字符串
    assert trace_json_col == ""


def test_append_ask_trace_fail_open_on_unknown_job(repo):
    repo.append_ask_trace("askjob-missing", {"step_type": "x", "summary": "", "detail": {}})  # 不抛


def test_append_ask_trace_concurrent_writers_no_duplicate_seq(repo):
    """并发向同一 job 追加轨迹(如 reflect 循环的多个子步骤并发写入)不应产生
    重复/跳号的 seq——取号(MAX(seq)+1)与插入须在同一个 _write() 事务里原子完成。
    仓库单写者假设下本无写写竞态,但该断言验证实现没有退化成「先读号、放开锁、
    再插入」的非原子两段式(那样并发下会取到同一个 next_seq、UNION PK 冲突或
    悄悄覆盖)。"""
    _, job_id, _ = _begin(repo)
    N = 30
    errors = []

    def worker(i):
        try:
            repo.append_ask_trace(job_id, {"step_type": f"t{i}", "summary": "", "detail": {}})
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert not errors, errors
    with repo._connect() as db:
        seqs = [r["seq"] for r in db.execute(
            "SELECT seq FROM ask_trace_steps WHERE job_id=? ORDER BY seq ASC", (job_id,)
        ).fetchall()]
    assert seqs == list(range(N))          # 连续无重复无跳号
    assert len(set(seqs)) == N              # 每个 seq 恰好一行(PK 不冲突)


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
    from app.api import ask_routes
    from app.main import create_app
    get_settings.cache_clear()
    ask_routes.repository.cache_clear()
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


# ---- Task 23: 经 AskExecutionCoordinator 的脱离连接执行,WS2b 接回语义不变 ----

def test_coordinator_run_replays_trace_via_conversation_active_job(repo):
    """轨迹经 coordinator 落 ask_trace_steps:运行中重开会话(get_conversation)
    按 active_job 回放已持久化步骤(合成 start 不在内);跑完后 job 终态 done、
    active_job 消失——重开实时接回不因执行编排搬迁而变。"""
    from app.models.schemas import NotebookCreate, TraceStep
    from app.services.ask_modes import ASK_MODES

    nb = repo.create_notebook(NotebookCreate(name="t23"))
    payload = AskRequest(question="Q-t23?", mode="reasoning")
    release = threading.Event()

    # Task 24: 协调器执行 runtime-owned AskService —— 在服务座上替换 ask,
    # 门控轨迹与完成时点(与旧 runner 回调同一观察面)。
    service = repo._runtime.ask_service()

    def fake_ask(notebook_id, p, *, user_id, on_trace=None, cancel_event=None):
        on_trace(TraceStep(step_type="plan", summary="s1", detail={}))
        assert release.wait(2)
        return AskResponse(answer_id="ans-t23", conversation_id=p.conversation_id,
                           conclusion="", answer="a", grounded=True, anchors=[],
                           related_knowledge=[], citations=[], llm_mode="x")

    service.ask = fake_ask
    events = repo._runtime.ask_execution.start(
        nb.id, payload, ASK_MODES["reasoning"],
        user_id=repo.current_user().id)
    started = events.get(timeout=2)
    job_id = started["job_id"]
    assert started["event"] == "started" and job_id
    for _ in range(2):                     # 合成 start + 真实 plan(持久化先于交付)
        assert events.get(timeout=2)["event"] == "progress"
    detail = repo.get_conversation(payload.conversation_id)
    assert detail.active_job is not None and detail.active_job.job_id == job_id
    assert [s["step_type"] for s in detail.active_job.trace] == ["plan"]
    release.set()
    while events.get(timeout=2) is not None:
        pass
    assert repo.ask_job_status(job_id)["status"] == "done"
    assert repo.get_conversation(payload.conversation_id).active_job is None

import datetime as dt
import json, threading, pytest
from app.core.config import Settings
from app.services import repository_facade
from app.services.sqlite_repository import SQLiteRepository, _now
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest
from tests.model_testkit import bind_all_embedding_clients
from tests.model_testkit import bind_chat_client
from tests.ask_testkit import seed_ask_evidence

class FakeLLM:
    configured = True
    def chat_json(self, messages, schema_hint, **kwargs):
        return json.dumps({"answer": "ok.", "grounded": False})


def test_repository_clock_preserves_subsecond_activity_order(monkeypatch):
    class FixedDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 24, 10, 5, 0, 123456, tzinfo=dt.timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(repository_facade, "datetime", FixedDatetime)
    assert dt.datetime.fromisoformat(_now()).microsecond == 123456

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings()); bind_all_embedding_clients(r, FakeEmbedder(dim=16)); bind_chat_client(r, "ask_answer", FakeLLM())
    return r

def _seed(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [{"local_id": "C1", "object_type": "concept",
        "payload": {"name": "Engram", "section_path": "1"}, "evidence": []}], [])
    return nb

def test_ask_creates_then_appends_conversation(repo):
    nb = _seed(repo)
    r1 = repo.ask(nb.id, AskRequest(question="what is engram"))
    assert r1.conversation_id                      # new conversation created
    r2 = repo.ask(nb.id, AskRequest(question="and its drawbacks?", conversation_id=r1.conversation_id))
    assert r2.conversation_id == r1.conversation_id  # appended, not new
    detail = repo.get_conversation(r1.conversation_id)
    assert detail.turn_count == 2 and len(detail.turns) == 2


def test_answer_completion_time_matches_persisted_answer_row(repo):
    """The live response and reopened turn expose the same write-clock instant."""
    nb = _seed(repo)
    response = repo.ask(nb.id, AskRequest(question="when was this answered"))

    with repo._connect() as db:
        row = db.execute(
            "SELECT payload, created_at FROM answers WHERE id=?",
            (response.answer_id,),
        ).fetchone()
    payload = json.loads(row["payload"])

    assert response.answered_at == row["created_at"]
    assert payload["answered_at"] == row["created_at"]
    turn = repo.get_conversation(response.conversation_id).turns[0]
    assert turn.response.answered_at == row["created_at"]


def test_reopened_legacy_answer_backfills_completion_time_from_row(repo):
    """Answers saved before answered_at existed remain timestamped on reopen."""
    nb = _seed(repo)
    completion = "2026-07-29T09:05:00+08:00"
    conversation_id = "conv-legacy-answer-time"
    with repo._connect() as db:
        db.execute(
            "INSERT INTO conversations "
            "(id,notebook_id,title,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (conversation_id, nb.id, "legacy", repo.current_user().id,
             completion, completion),
        )
        db.execute(
            "INSERT INTO answers "
            "(id,notebook_id,question,payload,created_at,conversation_id) "
            "VALUES (?,?,?,?,?,?)",
            ("ans-legacy-answer-time", nb.id, "q",
             json.dumps({"conclusion": "old answer"}), completion, conversation_id),
        )

    turn = repo.get_conversation(conversation_id).turns[0]
    assert turn.response.answered_at == completion
    assert turn.created_at == completion

def test_ask_feeds_prior_turns_into_prompt(repo, monkeypatch):
    nb = _seed(repo)
    captured = {}
    def cap(messages, schema_hint, **kwargs):
        captured["p"] = messages[0]["content"]
        return json.dumps({"answer": "ok.", "grounded": False})
    capture_client = FakeLLM()
    capture_client.chat_json = cap
    bind_chat_client(repo, "ask_answer", capture_client)
    r1 = repo.ask(nb.id, AskRequest(question="ZZTOPIC question", mode="reasoning"))
    repo.ask(
        nb.id,
        AskRequest(
            question="follow up",
            conversation_id=r1.conversation_id,
            mode="reasoning",
        ),
    )
    assert "ZZTOPIC question" in captured["p"]      # prior turn present in 2nd prompt


def test_list_conversations(repo):
    nb = _seed(repo)
    r = repo.ask(nb.id, AskRequest(question="q1"))
    convs = repo.list_conversations(nb.id)
    assert len(convs) == 1 and convs[0].id == r.conversation_id and convs[0].turn_count == 1


def test_starting_follow_up_refreshes_last_activity_and_history_order(repo, monkeypatch):
    """A submitted user turn is history activity before its answer exists."""
    nb = _seed(repo)
    user_id = repo.current_user().id
    with repo._connect() as db:
        db.execute(
            "INSERT INTO conversations "
            "(id,notebook_id,title,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("conv-follow-up", nb.id, "older conversation", user_id,
             "2020-01-01T00:00:00", "2026-07-01T00:00:00"),
        )
        db.execute(
            "INSERT INTO conversations "
            "(id,notebook_id,title,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("conv-was-latest", nb.id, "newer conversation", user_id,
             "2026-07-20T00:00:00", "2026-07-20T00:00:00"),
        )

    latest_activity = "2026-07-24T10:05:00"
    monkeypatch.setattr("app.services.sqlite_repository._now", lambda: latest_activity)
    job_id, conversation_id = repo.begin_ask_job(
        nb.id,
        AskRequest(
            question="new user turn",
            conversation_id="conv-follow-up",
            mode="chunk",
        ),
        "chunk",
        threading.Event(),
    )

    sessions = repo.list_conversations(nb.id)
    assert conversation_id == "conv-follow-up"
    assert [session.id for session in sessions[:2]] == [
        "conv-follow-up",
        "conv-was-latest",
    ]
    assert sessions[0].updated_at == latest_activity
    assert sessions[0].turn_count == 0
    repo.finish_ask_job(job_id, "cancelled")


def test_conversations_scoped_by_current_user(repo):
    nb = _seed(repo)  # 复用本文件已有的 _seed
    r = repo.ask(nb.id, AskRequest(question="q1"))
    # 当前用户能看到自己的会话
    convs = repo.list_conversations(nb.id)
    assert [c.id for c in convs] == [r.conversation_id]
    # 归属字段已写入
    with repo._connect() as db:
        owner = db.execute("SELECT created_by FROM conversations WHERE id=?", (r.conversation_id,)).fetchone()[0]
    assert owner == repo.current_user().id
    # 另一个用户的会话不出现在列表里
    with repo._connect() as db:
        db.execute("INSERT INTO conversations (id, notebook_id, title, created_by, created_at, updated_at) "
                   "VALUES ('conv-other','%s','x','someone-else','t','t')" % nb.id)
    assert all(c.id != "conv-other" for c in repo.list_conversations(nb.id))


def test_delete_and_rename_conversation(repo):
    nb = _seed(repo)
    r = repo.ask(nb.id, AskRequest(question="q1"))
    repo.rename_conversation(r.conversation_id, "新标题")
    assert repo.get_conversation(r.conversation_id).title == "新标题"
    with repo._connect() as db:
        job_id = db.execute(
            "SELECT id FROM ask_jobs WHERE conversation_id=?", (r.conversation_id,)
        ).fetchone()[0]
    repo.append_ask_trace(job_id, {"step_type": "answer", "summary": "private"})
    repo.delete_conversation(r.conversation_id)
    with pytest.raises(KeyError):
        repo.get_conversation(r.conversation_id)         # 会话已删
    with repo._connect() as db:
        n = db.execute("SELECT count(*) FROM answers WHERE conversation_id=?", (r.conversation_id,)).fetchone()[0]
        jobs = db.execute(
            "SELECT count(*) FROM ask_jobs WHERE conversation_id=?", (r.conversation_id,)
        ).fetchone()[0]
        traces = db.execute(
            "SELECT count(*) FROM ask_trace_steps WHERE job_id=?", (job_id,)
        ).fetchone()[0]
    assert n == 0                                          # 其下 answers 一并删除
    assert jobs == 0 and traces == 0                       # 隐藏 job/trace 同步清除


def test_conversation_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app
    client = TestClient(app)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    from app.api.ask_routes import repository
    seed_ask_evidence(repository(), nb)  # PR#334:空库 ask 会 409,先塞一条证据
    r = client.post(f"/api/notebooks/{nb}/ask", json={"question": "q1"})
    assert r.status_code == 200
    cid = r.json()["conversation_id"]
    assert cid
    lst = client.get(f"/api/notebooks/{nb}/conversations")
    assert lst.status_code == 200 and len(lst.json()) == 1 and lst.json()[0]["id"] == cid
    detail = client.get(f"/api/conversations/{cid}")
    assert detail.status_code == 200 and detail.json()["turn_count"] == 1
    assert client.get("/api/conversations/bogus").status_code == 404


def test_conversation_mutation_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app
    from app.api.ask_routes import repository
    client = TestClient(app)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    from app.api.ask_routes import repository
    seed_ask_evidence(repository(), nb)  # PR#334:空库 ask 会 409,先塞一条证据
    cid = client.post(f"/api/notebooks/{nb}/ask", json={"question": "q"}).json()["conversation_id"]
    with repository()._connect() as db:
        job_id = db.execute(
            "SELECT id FROM ask_jobs WHERE conversation_id=?", (cid,)
        ).fetchone()[0]
    assert client.patch(f"/api/conversations/{cid}", json={"title": "T"}).status_code == 200
    assert client.get(f"/api/conversations/{cid}").json()["title"] == "T"
    assert client.delete(f"/api/conversations/{cid}").status_code == 200
    assert client.get(f"/api/conversations/{cid}").status_code == 404
    assert client.get(f"/api/notebooks/{nb}/ask/jobs/{job_id}").status_code == 404
    assert client.patch("/api/conversations/bogus", json={"title": "x"}).status_code == 404
    assert client.delete("/api/conversations/bogus").status_code == 404

    running_job, running_conversation = repository().begin_ask_job(
        nb,
        AskRequest(question="still running", mode="chunk"),
        "chunk",
        threading.Event(),
    )
    conflict = client.delete(f"/api/conversations/{running_conversation}")
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "conversation has a running Ask job"
    assert client.get(f"/api/conversations/{running_conversation}").status_code == 200
    assert client.get(
        f"/api/notebooks/{nb}/ask/jobs/{running_job}"
    ).status_code == 200


def test_rename_refuses_an_over_length_title_and_stores_nothing_clipped(tmp_path, monkeypatch):
    """会话标题必须在**重命名**这一刻就有界。

    公开分享页把标题 `_title_text` **原样**发给匿名访客(旧的 400 字公开截断在
    codex #522 R2 被拿掉,因为静默截断用户自撰的标题正是「用户编辑的数据不得静默
    截断」要防的)——所以「原样返回」只有在写入侧拒收超长标题时才是**有界**的。
    这是 codex #525 R1 P2 对报告投影提的同一条,#526 已为提问那半闭合,这里闭合标题
    这半。重命名是标题唯一能超过服务端自动取的前 60 字的途径,所以这一个端点就是
    它的全部写入侧。

    与前端 `ASK_INPUT_LIMITS.conversationTitleMaxChars` 是同一条护栏的两半。
    """
    from fastapi.testclient import TestClient

    from app.models.ask import CONVERSATION_TITLE_MAX_CHARS

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app
    from app.api.ask_routes import repository
    client = TestClient(app)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    seed_ask_evidence(repository(), nb)
    cid = client.post(f"/api/notebooks/{nb}/ask", json={"question": "q"}).json()["conversation_id"]

    before = client.get(f"/api/conversations/{cid}").json()["title"]
    over = "标" * (CONVERSATION_TITLE_MAX_CHARS + 1)
    assert client.patch(f"/api/conversations/{cid}", json={"title": over}).status_code == 422
    # 拒绝,不是裁短了存:库里绝不能留下一份被悄悄截过的标题——那正是公开投影不再
    # 截断之后、唯一还能让匿名响应重新变成"被静默改写过的用户文本"的路径。
    assert client.get(f"/api/conversations/{cid}").json()["title"] == before

    # 恰好等于上限**不是** 422,而且原样存下——空转保护:一个恒 422、或者悄悄裁到
    # 199 字的实现都过不了这一段。
    at_cap = "标" * CONVERSATION_TITLE_MAX_CHARS
    assert client.patch(f"/api/conversations/{cid}", json={"title": at_cap}).status_code == 200
    assert client.get(f"/api/conversations/{cid}").json()["title"] == at_cap


def test_list_conversations_used_reasoning_last_turn(repo):
    """used_reasoning 反映会话最后一轮是否走了推理（reasoning_trace 非空）。"""
    from app.models.schemas import AskResponse, TraceStep
    nb = _seed(repo)

    def used_reasoning(conv_id):
        return next(c.used_reasoning for c in repo.list_conversations(nb.id) if c.id == conv_id)

    # 1) 单条快速轮（repo.ask 不写 reasoning_trace）→ 最后一轮快速 → False
    r = repo.ask(nb.id, AskRequest(question="q1"))
    cid = r.conversation_id
    assert used_reasoning(cid) is False

    # 2) 追加一条推理轮（带非空 reasoning_trace）→ 最后一轮推理 → True
    repo._save_answer(
        nb.id, "q2",
        AskResponse(conclusion="c", conversation_id=cid,
                    reasoning_trace=[TraceStep(step_type="answer", summary="s")]),
        conversation_id=cid,
    )
    assert used_reasoning(cid) is True

    # 3) 再追加一条快速轮 → 最后一轮又变快速 → False（证明看的是"最后一轮"而非"任意一轮"）
    repo._save_answer(
        nb.id, "q3",
        AskResponse(conclusion="c", conversation_id=cid),
        conversation_id=cid,
    )
    assert used_reasoning(cid) is False

    # 4) 单条推理会话 → True（直接建会话行 + 一条推理 answer，沿用本文件直插风格）
    with repo._connect() as db:
        db.execute(
            "INSERT INTO conversations (id, notebook_id, title, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("conv-r", nb.id, "r", repo.current_user().id, "t", "t"),
        )
    repo._save_answer(
        nb.id, "qr",
        AskResponse(conclusion="c", conversation_id="conv-r",
                    reasoning_trace=[TraceStep(step_type="answer", summary="s")]),
        conversation_id="conv-r",
    )
    assert used_reasoning("conv-r") is True


def test_used_reasoning_empty_trace_counts_as_fast(repo):
    """空 reasoning_trace([]) 在 list 与 detail 端一致地算作快速(False)。"""
    nb = _seed(repo)
    # 直接落一条 reasoning_trace 为空数组的 answer，绕过 ask 写入路径(它会把空 trace 存成 null)
    with repo._connect() as db:
        db.execute(
            "INSERT INTO conversations (id, notebook_id, title, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("conv-empty", nb.id, "e", repo.current_user().id, "t", "t"),
        )
        db.execute(
            "INSERT INTO answers (id, notebook_id, question, payload, created_at, conversation_id) "
            "VALUES (?,?,?,?,?,?)",
            ("ans-empty", nb.id, "q", json.dumps({"conclusion": "c", "reasoning_trace": []}), "t", "conv-empty"),
        )
    ur = next(c.used_reasoning for c in repo.list_conversations(nb.id) if c.id == "conv-empty")
    assert ur is False                                              # list 端:空数组算快速
    assert repo.get_conversation("conv-empty").used_reasoning is False  # detail 端一致


def test_get_conversation_used_reasoning_reflects_last_turn(repo):
    """ConversationDetail.used_reasoning 与最后一轮一致(不再恒为 False)。"""
    from app.models.schemas import AskResponse, TraceStep
    nb = _seed(repo)
    r = repo.ask(nb.id, AskRequest(question="q1"))
    cid = r.conversation_id
    assert repo.get_conversation(cid).used_reasoning is False   # 末轮快速
    repo._save_answer(
        nb.id, "q2",
        AskResponse(conclusion="c", conversation_id=cid,
                    reasoning_trace=[TraceStep(step_type="answer", summary="s")]),
        conversation_id=cid,
    )
    assert repo.get_conversation(cid).used_reasoning is True    # 末轮推理


def test_bulk_delete_conversations_by_last_activity(repo):
    nb = _seed(repo)
    other = repo.create_notebook(NotebookCreate(name="other"))
    me = repo.current_user().id

    def add_conv(cid, notebook_id, updated_at, *, created_by=None, created_at="2000-01-01T00:00:00"):
        with repo._connect() as db:
            db.execute(
                "INSERT INTO conversations (id, notebook_id, title, created_by, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (cid, notebook_id, cid, created_by or me, created_at, updated_at),
            )
            db.execute(
                "INSERT INTO answers (id, notebook_id, question, payload, created_at, conversation_id) "
                "VALUES (?,?,?,?,?,?)",
                (f"ans-{cid}", notebook_id, "q", "{}", updated_at, cid),
            )

    add_conv("conv-old", nb.id, "2000-01-01T00:00:00")                          # stale -> delete
    add_conv("conv-new", nb.id, _now())                                         # active today -> keep
    add_conv("conv-revived", nb.id, _now(), created_at="2000-01-01T00:00:00")   # old created, recent activity -> keep
    add_conv("conv-otnb", other.id, "2000-01-01T00:00:00")                      # other notebook -> untouched
    add_conv("conv-other-user", nb.id, "2000-01-01T00:00:00", created_by="someone-else")  # other user -> untouched

    result = repo.bulk_delete_conversations(nb.id, older_than_days=3)
    assert result.deleted == 1                                                  # only conv-old matched
    assert result.deleted_ids == ["conv-old"]

    survivors = {c.id for c in repo.list_conversations(nb.id)}
    assert survivors == {"conv-new", "conv-revived"}                           # keyed on updated_at, not created_at

    with repo._connect() as db:
        assert db.execute("SELECT count(*) FROM answers WHERE conversation_id='conv-old'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM answers WHERE conversation_id='conv-new'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM conversations WHERE id='conv-otnb'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM conversations WHERE id='conv-other-user'").fetchone()[0] == 1


def test_bulk_delete_conversations_missing_notebook(repo):
    with pytest.raises(KeyError):
        repo.bulk_delete_conversations("nb-bogus", older_than_days=3)


def test_bulk_delete_conversations_rejects_nonpositive_days(repo):
    nb = _seed(repo)
    for bad in (0, -1):
        with pytest.raises(ValueError):
            repo.bulk_delete_conversations(nb.id, older_than_days=bad)


def test_bulk_delete_conversations_route(tmp_path, monkeypatch):
    import sqlite3
    from fastapi.testclient import TestClient
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app
    from app.api.ask_routes import repository
    client = TestClient(app)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    seed_ask_evidence(repository(), nb)  # PR#334:空库 ask 会 409,先塞一条证据
    cid = client.post(f"/api/notebooks/{nb}/ask", json={"question": "q"}).json()["conversation_id"]

    # age the conversation's last activity to long ago (external connection, no concurrent write)
    # use repository().db_path so we connect to whichever DB the cached repo actually uses
    con = sqlite3.connect(repository().db_path)
    con.execute("UPDATE conversations SET updated_at='2000-01-01T00:00:00' WHERE id=?", (cid,))
    con.commit(); con.close()

    # invalid threshold -> 422 (Query ge=1)
    assert client.delete(f"/api/notebooks/{nb}/conversations", params={"older_than_days": 0}).status_code == 422
    # missing notebook -> 404
    assert client.delete("/api/notebooks/bogus/conversations", params={"older_than_days": 3}).status_code == 404
    # happy path -> deletes the aged conversation
    resp = client.delete(f"/api/notebooks/{nb}/conversations", params={"older_than_days": 3})
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 1, "deleted_ids": [cid]}
    assert client.get(f"/api/notebooks/{nb}/conversations").json() == []


# ---- Task 22: 会话持久化在 runtime.ask_state 组件,按显式 user_id 作用域 ----

def test_conversation_store_scopes_by_explicit_user_id(repo):
    """AskStateStore 不读请求 ContextVar:ensure/list 都以调用方传入的 user_id
    为准;传入他人 conversation_id 不接续、新建归自己的(created_by 语义保持)。"""
    nb = _seed(repo)
    store = repo._runtime.ask_state
    with repo._write() as db:
        cid = store.ensure_conversation(db, nb.id, None, "q-a", "user-a")
    assert [c.id for c in store.list_conversations(nb.id, "user-a")] == [cid]
    assert store.list_conversations(nb.id, repo.current_user().id) == []
    with repo._write() as db:
        other = store.ensure_conversation(db, nb.id, cid, "q-b", "user-b")
    assert other != cid                       # 未注入 user-a 的对话
    with repo._connect() as db:
        assert db.execute("SELECT created_by FROM conversations WHERE id=?",
                          (other,)).fetchone()[0] == "user-b"

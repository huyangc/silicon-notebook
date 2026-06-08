import json, pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest

class FakeLLM:
    configured = True
    def chat_json(self, messages, schema_hint):
        return json.dumps({"answer": "ok.", "grounded": False})

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings()); r.embedder = FakeEmbedder(dim=16); r.llm_client = FakeLLM()
    return r

def test_schema_has_conversations_and_fk(repo):
    with repo._connect() as db:
        cols = {row[1] for row in db.execute("PRAGMA table_info(answers)").fetchall()}
        assert "conversation_id" in cols
        tbls = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "conversations" in tbls


def test_conversation_schemas_exist():
    from app.models.schemas import AskRequest, AskResponse, ConversationSummary, ConversationDetail
    req = AskRequest(question="q", conversation_id="c1")
    assert req.conversation_id == "c1"
    resp = AskResponse(conclusion="x", conversation_id="c1")
    assert resp.conversation_id == "c1"
    s = ConversationSummary(id="c1", notebook_id="n", title="t", updated_at="now", turn_count=2)
    assert s.turn_count == 2


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

def test_ask_feeds_prior_turns_into_prompt(repo, monkeypatch):
    nb = _seed(repo)
    captured = {}
    def cap(messages, schema_hint):
        captured["p"] = messages[0]["content"]
        return json.dumps({"answer": "ok.", "grounded": False})
    repo.llm_client.chat_json = cap
    r1 = repo.ask(nb.id, AskRequest(question="ZZTOPIC question"))
    repo.ask(nb.id, AskRequest(question="follow up", conversation_id=r1.conversation_id))
    assert "ZZTOPIC question" in captured["p"]      # prior turn present in 2nd prompt


def test_list_conversations(repo):
    nb = _seed(repo)
    r = repo.ask(nb.id, AskRequest(question="q1"))
    convs = repo.list_conversations(nb.id)
    assert len(convs) == 1 and convs[0].id == r.conversation_id and convs[0].turn_count == 1


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
    repo.delete_conversation(r.conversation_id)
    with pytest.raises(KeyError):
        repo.get_conversation(r.conversation_id)         # 会话已删
    with repo._connect() as db:
        n = db.execute("SELECT count(*) FROM answers WHERE conversation_id=?", (r.conversation_id,)).fetchone()[0]
    assert n == 0                                          # 其下 answers 一并删除


def test_conversation_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app
    client = TestClient(app)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
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
    client = TestClient(app)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    cid = client.post(f"/api/notebooks/{nb}/ask", json={"question": "q"}).json()["conversation_id"]
    assert client.patch(f"/api/conversations/{cid}", json={"title": "T"}).status_code == 200
    assert client.get(f"/api/conversations/{cid}").json()["title"] == "T"
    assert client.delete(f"/api/conversations/{cid}").status_code == 200
    assert client.get(f"/api/conversations/{cid}").status_code == 404
    assert client.patch("/api/conversations/bogus", json={"title": "x"}).status_code == 404
    assert client.delete("/api/conversations/bogus").status_code == 404


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

# tests/test_graph_src_chunks.py
import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest
from tests.model_testkit import bind_embedding_client
from tests.model_testkit import bind_chat_client


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None))
    bind_embedding_client(r, FakeEmbedder(dim=16))
    return r


class _GraphLLM:
    """verify(返回 valid) + _answer_mix(引用首个 chunk k1)两用 stub。"""
    configured = True
    def chat_json(self, messages, schema_hint, **kw):
        text = messages[0]["content"]
        if "valid" in (schema_hint or ""):          # verify_chain_edges 的 schema
            return '{"valid": true, "reason": "ok"}'
        return '{"answer": "Mamba 是选择性状态空间模型 [k1].", "grounded": true}'


def _seed_one_node_with_chunk(repo):
    """一个 concept(名含 query 关键词)+ 它 evidence 指向的 chunk。"""
    nb = repo.create_notebook(NotebookCreate(name="g"))
    with repo._write() as db:
        now = "2026-06-24T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("src-M", nb.id, "Mamba paper", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("cM", nb.id, "src-M", "[2.1 MAMBA] Mamba uses a selective state space mechanism.",
                    "Mamba", json.dumps(["elM"]), now))
        ev = json.dumps([{"source_id": "src-M", "source_title": "", "element_id": "elM",
                          "element_type": "paragraph", "location_label": "p",
                          "quoted_span": "selective state space", "confidence": 1.0}])
        db.execute("INSERT INTO knowledge_objects "
                   "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("eM", nb.id, "concept", "approved", "", json.dumps({"name": "Mamba"}), ev, "src-M", now, now))
    return nb


def test_bfs_brings_source_chunks(repo, monkeypatch):
    nb = _seed_one_node_with_chunk(repo)
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)   # 强制走 BFS
    llm = _GraphLLM()
    bind_chat_client(repo, "ask_answer", llm)
    bind_chat_client(repo, "graph_chain_verify", llm)
    resp = repo.ask_graph(nb.id, AskRequest(question="Mamba 的原理", mode="graph"))
    assert resp.mode == "graph"
    src_ids = {c.source_id for c in resp.citations}
    assert "src-M" in src_ids                       # BFS 答案带上了 chunk 原文引用


def test_bfs_falls_back_when_no_source_chunk(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="g2"))
    with repo._write() as db:
        now = "2026-06-24T00:00:00"
        # 节点 evidence 指向不存在的 element(无 chunk 命中)→ _kg_source_chunks 空
        ev = json.dumps([{"source_id": "src-X", "source_title": "", "element_id": "ghost",
                          "element_type": "paragraph", "location_label": "p",
                          "quoted_span": "x", "confidence": 1.0}])
        db.execute("INSERT INTO knowledge_objects "
                   "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("eX", nb.id, "concept", "approved", "", json.dumps({"name": "Mamba"}), ev, "src-X", now, now))
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)
    llm = _GraphLLM()
    bind_chat_client(repo, "ask_answer", llm)
    bind_chat_client(repo, "graph_chain_verify", llm)
    resp = repo.ask_graph(nb.id, AskRequest(question="Mamba", mode="graph"))
    assert resp.mode == "graph"
    assert not any(c.source_id for c in resp.citations)   # 回退 KG-only,无 chunk 引用

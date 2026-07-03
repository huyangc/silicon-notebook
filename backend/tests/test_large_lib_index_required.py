"""大库检索统一 copyable + 无索引提示建索引。"""
import json
import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_PROVIDER": "dashscope", "EMBED_BASE_URL": "https://e.test",
                 "EMBED_API_KEY": "k", "EMBED_MODEL": "m", "EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _add_chunk(repo, nb_id, sid, cid, text):
    now = "2026-07-03T00:00:00"
    with repo._write() as db:
        db.execute("INSERT OR IGNORE INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   (sid, nb_id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                   (cid, nb_id, sid, text, "", "[]", now))
        v = repo.embedder.embed_query(text)
        db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                   (cid, nb_id, json.dumps(v), now))


def test_large_lib_few_chunks_degrades_to_fts(repo, monkeypatch):
    """大库(copyable=False)即使 chunk 数远低于阈值,也走 FTS 降级、不全表暴力。"""
    nb = repo.create_notebook(NotebookCreate(name="big"))
    _add_chunk(repo, nb.id, "s1", "c1", "alpha")     # 仅 1 chunk,远低于 20000
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)  # 一切皆大
    events = []
    monkeypatch.setattr(repo.event_log, "emit", lambda e: events.append(e))

    def _boom(*a, **k):
        raise AssertionError("大库不得走 _gather_chunks 全表暴力")
    monkeypatch.setattr(repo, "_gather_chunks", _boom)

    scored, ids, mat = repo._retrieve_chunks(nb.id, "alpha")
    assert any(e.get("kind") == "chunk_bruteforce_skipped" for e in events)


def test_small_lib_few_chunks_bruteforces(repo):
    """小库 chunk 少 → 全量暴力路径不变(能拿到打分结果)。"""
    nb = repo.create_notebook(NotebookCreate(name="small"))
    _add_chunk(repo, nb.id, "s1", "c1", "alpha beta")
    scored, ids, mat = repo._retrieve_chunks(nb.id, "alpha")
    assert ids is not None   # 走了全量矩阵路径

"""IN 分批:所有 delta id 列表内联 SQL 的位点在超过 _IN_CHUNK 时结果不变、不抛
"too many SQL variables"(生产 48,739 delta source 实测打爆 SQLite 32,766 上限)。"""
import json
import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _insert_source_with_object(repo, nb_id, i):
    """一个 source + 一个 chunk + 一个 KG 对象(带 embedding)+ 一条自环外关系。"""
    sid, cid, oid = f"s{i}", f"c{i}", f"o{i}"
    now = "2026-07-01T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   (sid, nb_id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                   (cid, nb_id, sid, f"text {i}", "", "[]", now))
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,source_id,object_type,payload,evidence,status,owner,last_reviewed,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (oid, nb_id, sid, "claim", json.dumps({"name": f"obj {i}"}), "[]",
                    "approved", "", "", now, now))
        v = repo._runtime.models.embedding("retrieval_query_embedding").embed_query(f"obj {i}")
        db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                   (oid, nb_id, json.dumps(v), now))
    return sid, cid, oid


def test_in_batches_dedup_and_order(repo):
    batches = list(repo.retrieval.candidates._in_batches(["a", "b", "a", "c"]))
    assert [x for b in batches for x in b] == ["a", "b", "c"]


def test_delta_sites_equivalent_when_batched(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    sids = []
    for i in range(5):
        sid, _, _ = _insert_source_with_object(repo, nb.id, i)
        sids.append(sid)

    big = {
        "index_delta": repo._index_delta(nb.id),
        "gather": repo._gather_kg_graph(nb.id, source_ids=sids),
    }
    monkeypatch.setattr(SQLiteRepository, "_IN_CHUNK", 2)
    small = {
        "index_delta": repo._index_delta(nb.id),
        "gather": repo._gather_kg_graph(nb.id, source_ids=sids),
    }
    assert small["index_delta"] == big["index_delta"]
    assert sorted(small["gather"][0]) == sorted(big["gather"][0])   # node_ids
    assert sorted(small["gather"][2]) == sorted(big["gather"][2])   # chunk_ids
    assert small["index_delta"]["delta_chunks"] == 5


def test_knowledge_store_compat_reads_late_bound_facade_chunk_size(
    repo, monkeypatch
):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    object_ids = [
        _insert_source_with_object(repo, nb.id, i)[2]
        for i in range(5)
    ]
    requested = list(reversed(object_ids)) + [object_ids[-1]]
    with repo._connect() as db:
        expected = repo._knowledge_objects(
            db, nb.id, "claim", id_filter=requested
        )

    monkeypatch.setattr(SQLiteRepository, "_IN_CHUNK", 2)
    statements = []
    with repo._connect() as db:
        db.set_trace_callback(statements.append)
        actual = repo._knowledge_objects(
            db, nb.id, "claim", id_filter=requested
        )

    bounded_queries = [
        statement
        for statement in statements
        if "FROM knowledge_objects" in statement and " id IN (" in statement
    ]
    assert len(bounded_queries) == 3
    assert actual == expected

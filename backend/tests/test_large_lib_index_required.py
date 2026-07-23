"""大库检索统一 copyable + 无索引提示建索引。"""
import json

import numpy as np
import pytest
import scipy.sparse as sp

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


def _add_chunk(repo, nb_id, sid, cid, text):
    now = "2026-07-03T00:00:00"
    with repo._write() as db:
        db.execute("INSERT OR IGNORE INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   (sid, nb_id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                   (cid, nb_id, sid, text, "", "[]", now))
        v = repo._runtime.models.embedding("retrieval_query_embedding").embed_query(text)
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
    monkeypatch.setattr(repo.retrieval.candidates, "_gather_chunks", _boom)

    scored, ids, mat = repo.retrieval.candidates._retrieve_chunks(nb.id, "alpha")
    assert any(e.get("kind") == "chunk_bruteforce_skipped" for e in events)


def test_small_lib_few_chunks_bruteforces(repo):
    """小库 chunk 少 → 全量暴力路径不变(能拿到打分结果)。"""
    nb = repo.create_notebook(NotebookCreate(name="small"))
    _add_chunk(repo, nb.id, "s1", "c1", "alpha beta")
    scored, ids, mat = repo.retrieval.candidates._retrieve_chunks(nb.id, "alpha")
    assert ids is not None   # 走了全量矩阵路径


from app.models.schemas import AskResponse


def _index_nb(repo, name="big"):
    nb = repo.create_notebook(NotebookCreate(name=name))
    _add_chunk(repo, nb.id, "s1", "c1", "alpha")
    _write_minimum_valid_scale_artifact(repo, nb.id)
    return nb


def _write_minimum_valid_scale_artifact(repo, notebook_id):
    """Persist the smallest loadable scale artifact through the real store.

    ``_needs_index`` only probes whether an on-disk artifact can be loaded.
    Building a complete KG and ANN here tests unrelated construction work and
    used to dominate the suite.  The one-node artifact retains the production
    file format and catalog/load path without manufacturing a whole graph.
    """
    runtime = repo._runtime.scale_artifacts
    runtime.artifacts.save_full(
        notebook_id,
        {
            "node_ids": ["c1"],
            "transition": sp.csr_matrix((1, 1), dtype=np.float32),
            "idf": np.ones(1, dtype=np.float32),
            "chunk_index": np.array([0], dtype=np.int32),
            "ann_vectors": np.empty((0, 16), dtype=np.float32),
            "ann_labels": [],
            "manifest": {
                "version": runtime.version(notebook_id),
                "dim": 16,
                "n_nodes": 1,
                "n_chunks": 1,
                "n_ann": 0,
            },
        },
    )


def test_indexed_fixture_is_detected_without_rebuild(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="indexed"))
    _add_chunk(repo, notebook.id, "s1", "c1", "alpha")
    _write_minimum_valid_scale_artifact(repo, notebook.id)
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)

    assert repo._needs_index(notebook.id) is False


def test_needs_index_truth_table(repo, monkeypatch):
    # 大库无索引 → True
    big = repo.create_notebook(__import__("app.models.schemas", fromlist=["NotebookCreate"]).NotebookCreate(name="bignoidx"))
    _add_chunk(repo, big.id, "s1", "c1", "alpha")
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)
    assert repo._needs_index(big.id) is True
    # 小库无索引 → False(小库允许暴力,不要求索引)
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 5000)
    assert repo._needs_index(big.id) is False


def test_needs_index_false_when_indexed(repo, monkeypatch):
    nb = _index_nb(repo)
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)  # 大库
    assert repo._needs_index(nb.id) is False   # 有磁盘索引 → 不提示


def test_save_answer_sets_index_required(repo, monkeypatch):
    """_save_answer 是所有 handler 的收口:大库无索引时给 response 打 index_required。"""
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="bignoidx2"))
    _add_chunk(repo, nb.id, "s1", "c1", "alpha")
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)
    resp = AskResponse(conclusion="x")
    repo._save_answer(nb.id, "q", resp)
    assert resp.index_required is True


def test_save_answer_index_required_false_small(repo):
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="small2"))
    _add_chunk(repo, nb.id, "s1", "c1", "alpha")
    resp = AskResponse(conclusion="x")
    repo._save_answer(nb.id, "q", resp)
    assert resp.index_required is False

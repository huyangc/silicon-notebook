"""KG 对象侧/关系侧 delta 门控(SCALE_SEARCH_INCLUDE_DELTA):已索引库检索默认
只搜已索引部分,水位后新增的 KG 对象/关系(delta)默认不被语义暴力检回 —— 与
chunk 侧(_retrieve_chunks_ann)同一原则。flag 开时保持强一致的 delta 暴力
(今日行为)。
"""
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
        v = repo.embedder.embed_query(f"obj {i}")
        db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                   (oid, nb_id, json.dumps(v), now))
    return sid, cid, oid


def _build_indexed_nb_with_delta_object(repo):
    """source A 进水位;source B 在 build 之后插入(其 KG 对象 embedding 与
    查询词 'bravo' 最匹配,payload 名字与查询无词法重叠)→ B 的对象只可能经
    delta 语义暴力被检回,FTS/关键词都救不了它。"""
    nb = repo.create_notebook(NotebookCreate(name="base"))
    _insert_source_with_object(repo, nb.id, 0)          # sA: 'obj 0'
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)                        # watermark = {s0}
    sid, cid, oid = "sB", "cB", "oB"
    now = "2026-07-02T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   (sid, nb.id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,source_id,object_type,payload,evidence,status,owner,last_reviewed,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (oid, nb.id, sid, "claim", json.dumps({"name": "zzz"}), "[]",
                    "approved", "", "", now, now))
        v = repo.embedder.embed_query("bravo")
        db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                   (oid, nb.id, json.dumps(v), now))
    return nb, oid


def test_object_delta_excluded_by_default(repo):
    nb, oid = _build_indexed_nb_with_delta_object(repo)
    assert repo.settings.scale_search_include_delta is False
    hits = repo._retrieve_scored(nb.id, "bravo")
    assert oid not in {h.object_id for h in hits}


def test_object_delta_included_when_opted_in(repo, monkeypatch):
    nb, oid = _build_indexed_nb_with_delta_object(repo)
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    hits = repo._retrieve_scored(nb.id, "bravo")
    assert oid in {h.object_id for h in hits}


def _build_indexed_nb_with_delta_relation(repo):
    """source A(两个 KG 对象 + 一条带 embedding 的关系)进水位 build_scale_index
    (使 manifest has_relation_ann=True——插行 SQL 抄 tests/test_relation_ann.py
    现成 fixture 的列名);source B 在 build 之后插入一条新关系,其 embedding 与
    查询词 'bravo' 最匹配 → 只可能经 delta 语义暴力被检回,ANN 核救不了它。
    返回 (nb, delta_relation_id)。"""
    nb = repo.create_notebook(NotebookCreate(name="base"))
    now = "2026-07-01T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   ("sA", nb.id, "t", "md", "ready", now, now))
        for oid, name in [("o1", "MOSFET"), ("o2", "current mirror")]:
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
                "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (oid, nb.id, "concept", "approved", "", json.dumps({"name": name}), "[]", "sA", now, now))
        db.execute(
            "INSERT INTO knowledge_relations (id,notebook_id,source_object_id,target_object_id,"
            "edge_type,evidence,source_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("rel0", nb.id, "o1", "o2", "depends_on", "[]", "sA", now))
        v = repo.embedder.embed_texts(["depends_on"])[0]
        db.execute(
            "INSERT INTO relation_embeddings (relation_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
            ("rel0", nb.id, json.dumps(v), now))
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)                         # watermark = {sA}

    rid = "rel-delta"
    now2 = "2026-07-02T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   ("sB", nb.id, "t2", "md", "ready", now2, now2))
        for oid, name in [("o3", "bandgap"), ("o4", "reference voltage")]:
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
                "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (oid, nb.id, "concept", "approved", "", json.dumps({"name": name}), "[]", "sB", now2, now2))
        db.execute(
            "INSERT INTO knowledge_relations (id,notebook_id,source_object_id,target_object_id,"
            "edge_type,evidence,source_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (rid, nb.id, "o3", "o4", "zzz_unrelated_edge", "[]", "sB", now2))
        v = repo.embedder.embed_query("bravo")
        db.execute(
            "INSERT INTO relation_embeddings (relation_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
            (rid, nb.id, json.dumps(v), now2))
    return nb, rid


def test_relation_delta_excluded_by_default(repo):
    nb, rid = _build_indexed_nb_with_delta_relation(repo)
    assert repo.settings.scale_search_include_delta is False
    sims = repo._relation_ann_candidates(
        nb.id, repo.embedder.embed_query("bravo"),
        repo._scale_index(nb.id, allow_stale=True), 10)
    assert rid not in sims


def test_relation_delta_included_when_opted_in(repo, monkeypatch):
    nb, rid = _build_indexed_nb_with_delta_relation(repo)
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    sims = repo._relation_ann_candidates(
        nb.id, repo.embedder.embed_query("bravo"),
        repo._scale_index(nb.id, allow_stale=True), 10)
    assert rid in sims

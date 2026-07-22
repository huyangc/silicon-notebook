import pytest
from app.services.retrieval import keyword_score
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    from app.services.embedding import FakeEmbedder
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def test_kg_object_candidates_core_and_delta(repo, monkeypatch):
    """opt-in delta brute-force: with scale_search_include_delta=True, a KG
    object added AFTER the watermark is still recalled via the delta matmul
    path. (Default is now OFF — see test_indexed_only_principle.py — so this
    test explicitly enables the opt-in to exercise the brute-force branch.)"""
    import json
    from app.models.schemas import NotebookCreate
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    nb = repo.create_notebook(NotebookCreate(name="base"))
    def add(sid, oid, name, day):
        with repo._write() as db:
            now = f"2026-07-{day:02d}T00:00:00"
            db.execute("INSERT OR IGNORE INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (sid, nb.id, "t", "md", "ready", now, now))
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
                       "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "", json.dumps({"name": name}), "[]", sid, now, now))
            v = repo._runtime.models.embedding("retrieval_query_embedding").embed_texts([name])[0]
            db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
                       "VALUES (?,?,?,?)", (oid, nb.id, json.dumps(v), now))
    add("s1", "o1", "current mirror", 1)
    add("s1", "o2", "bandgap reference", 1)
    repo.rebuild_unified_kg(nb.id); repo.build_scale_index(nb.id)
    add("s2", "o3", "MOSFET amplifier", 2)   # build 后新增 = delta
    # id_filter
    with repo._connect() as db:
        objs = repo._knowledge_objects(db, nb.id, "concept", id_filter={"o1"})
    assert {o["id"] for o in objs} == {"o1"}
    # 候选:ANN 核(o1/o2)⊕ delta(o3)
    idx = repo._scale_index(nb.id, allow_stale=True)
    cand = repo._kg_object_candidates(nb.id, repo._embed_query("MOSFET amplifier"), idx, recall=10)
    assert "o3" in cand                        # delta 对象在候选
    assert set(cand.keys()) & {"o1", "o2"}     # ANN 核也在候选
    assert all(0.0 <= s <= 1.0 for s in cand.values())


def test_retrieve_scored_bounded_when_indexed(repo, monkeypatch):
    """opt-in delta brute-force: with scale_search_include_delta=True, a
    delta KG object is still recalled and the candidate set stays bounded.
    (Default is now OFF — see test_indexed_only_principle.py.)"""
    import json
    from app.models.schemas import NotebookCreate
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    nb = repo.create_notebook(NotebookCreate(name="base"))
    def add(sid, oid, name, day):
        with repo._write() as db:
            now = f"2026-07-{day:02d}T00:00:00"
            db.execute("INSERT OR IGNORE INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (sid, nb.id, "t", "md", "ready", now, now))
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
                       "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "", json.dumps({"name": name}), "[]", sid, now, now))
            v = repo._runtime.models.embedding("retrieval_query_embedding").embed_texts([name])[0]
            db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
                       "VALUES (?,?,?,?)", (oid, nb.id, json.dumps(v), now))
    for i in range(6):
        add("s1", f"o{i}", f"concept {i}", 1)
    repo.rebuild_unified_kg(nb.id); repo.build_scale_index(nb.id)
    add("s2", "odelta", "delta concept special", 2)
    monkeypatch.setattr(repo.settings, "chunk_recall", 3)
    out = repo._retrieve_scored(nb.id, "delta concept special")
    ids = {o.object_id for o in out}
    assert "odelta" in ids                # delta 对象被召回
    assert len(ids) <= 3 + 1              # 有界:≤ recall(3)核 + delta(1),远小于 7 全量


def test_keyword_score_ignores_stopwords():
    # Verbose phrasing must not dilute the score: only content tokens count.
    # Basis after dropping stopwords (what/is/and/are/its) -> {engram, problems};
    # "problems" is a genuine content word absent from the short KG name, so it
    # remains in the denominator. The point is the score is no longer crushed by
    # the function words (raw token basis would be 8 -> 0.125).
    concise = keyword_score("engram", "Engram is a memory module")
    verbose = keyword_score("what is engram and what are its problems", "Engram is a memory module")
    assert concise == 1.0
    # Without stopword filtering this would be 1/8 = 0.125; with filtering the
    # basis is the 2 content tokens (engram hits) -> 0.5.
    assert verbose == 0.5


def test_fuse_custom_weights_shift_balance():
    from app.services.retrieval import _fuse
    # 默认 0.4/0.6: 语义为 0 时融合分 = keyword * 0.4/(0.4+0.6) = 0.4
    assert abs(_fuse(1.0, 0.0, True) - 0.4) < 1e-9
    # keyword-heavy 0.7/0.3: 同输入下关键词权重更高
    assert abs(_fuse(1.0, 0.0, True, w_keyword=0.7, w_semantic=0.3) - 0.7) < 1e-9


def test_score_knowledge_passes_weights_through():
    from app.services.retrieval import score_knowledge
    objs = [{"id": "o1", "payload": {"name": "RTL synthesis flow"}, "evidence": []}]
    # 纯关键词(无向量)下,提高 w_keyword 不应改变 keyword-only 融合分(归一化抵消),
    # 但调用必须接受参数且不报错,返回命中。
    hits = score_knowledge("RTL synthesis", objs, "claim", w_keyword=0.7, w_semantic=0.3)
    assert hits and hits[0].object_id == "o1"

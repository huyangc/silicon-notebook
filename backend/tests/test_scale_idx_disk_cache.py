"""大库检索按磁盘索引身份缓存:stale 实例按磁盘 manifest 版本复用,脱离 kg_mutation_seq
churn,使摄取期严格推理恒定 O(1)。"""
import json
import os
import threading

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder


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


def test_read_manifest_version(repo, tmp_path):
    out_dir = tmp_path / "idxdir"
    out_dir.mkdir()
    # 无 manifest → None
    assert repo._read_manifest_version(str(out_dir)) is None
    # 有 manifest → 返回 version list
    (out_dir / "manifest.json").write_text(json.dumps({"version": ["a", 3, "t"]}))
    assert repo._read_manifest_version(str(out_dir)) == ["a", 3, "t"]
    # 损坏 JSON → None(不抛)
    (out_dir / "manifest.json").write_text("{not json")
    assert repo._read_manifest_version(str(out_dir)) is None
    # 无 version 字段 → None
    (out_dir / "manifest.json").write_text(json.dumps({"n_nodes": 5}))
    assert repo._read_manifest_version(str(out_dir)) is None


def test_load_lock_table_present(repo):
    assert isinstance(repo._scale_idx_load_lock, threading.Lock().__class__)
    assert repo._scale_idx_load_locks == {}


def _insert_source_chunk(repo, nb_id, sid, cid, text, day):
    now = f"2026-07-{day:02d}T00:00:00"
    with repo._write() as db:
        db.execute("INSERT OR IGNORE INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   (sid, nb_id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                   (cid, nb_id, sid, text, "", "[]", now))
        v = repo.embedder.embed_query(text)
        db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                   (cid, nb_id, json.dumps(v), now))
    # Raw SQL above bypasses the real chunk-ingest path, which unconditionally
    # bumps kg_mutation_seq (sqlite_repository.py ~:3487) precisely so
    # _scale_index_version drifts on any chunk write. Without this explicit
    # bump, this helper's inserts are invisible to _scale_index_version's
    # seq-memoized fast path and `cur` would never diverge from the watermark
    # build's manifest version — defeating the whole point of "insert a delta
    # source after build_scale_index".
    repo._mark_unified_kg_dirty(nb_id)


def _indexed_nb_with_delta(repo):
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="big"))
    _insert_source_chunk(repo, nb.id, "sA", "cA", "alpha", 1)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)                 # watermark={sA}; manifest.version=V0
    _insert_source_chunk(repo, nb.id, "sB", "cB", "bravo", 2)  # delta → cur != V0
    return nb


def test_stale_index_reused_across_queries(repo, monkeypatch):
    """摄取造成 cur != manifest.version 时,多次 allow_stale 调用只 load 一次磁盘。"""
    nb = _indexed_nb_with_delta(repo)
    import app.services.kg.scale_index as si
    calls = {"n": 0}
    real = si.load_scale_index
    monkeypatch.setattr(si, "load_scale_index", lambda d: (calls.__setitem__("n", calls["n"] + 1), real(d))[1])
    a = repo._scale_index(nb.id, allow_stale=True)
    b = repo._scale_index(nb.id, allow_stale=True)
    c = repo._scale_index(nb.id, allow_stale=True)
    assert a is not None and a is b is c          # 同一实例复用
    assert calls["n"] == 1                         # 只从磁盘 load 一次


def test_exact_caller_unchanged_on_delta(repo):
    """version-exact 调用方(无 allow_stale)在有 delta 时仍返 None,行为不变。"""
    nb = _indexed_nb_with_delta(repo)
    assert repo._scale_index(nb.id) is None


def test_stale_reload_after_disk_rebuild(repo):
    """磁盘 manifest 版本变(rebuild/fold)后,下次 stale 调用返回新实例(自愈)。"""
    nb = _indexed_nb_with_delta(repo)
    a = repo._scale_index(nb.id, allow_stale=True)
    repo.build_scale_index(nb.id)                  # 重建 → 新 manifest.version,收进 sB
    b = repo._scale_index(nb.id, allow_stale=True)
    assert b is not None and b is not a            # 新磁盘身份 → 新实例


def test_no_manifest_returns_none(repo):
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="empty"))
    assert repo._scale_index(nb.id, allow_stale=True) is None


def test_concurrent_cold_stale_single_flight(repo, monkeypatch):
    """并发 cold stale 调用只 load 一次(单飞)。"""
    import app.services.kg.scale_index as si
    nb = _indexed_nb_with_delta(repo)
    repo._scale_idx_cache.pop(nb.id, None)         # 清缓存造 cold
    calls = {"n": 0}
    real = si.load_scale_index
    import time

    def slow_load(d):
        calls["n"] += 1
        time.sleep(0.05)
        return real(d)
    monkeypatch.setattr(si, "load_scale_index", slow_load)
    import threading
    results = []
    threads = [threading.Thread(target=lambda: results.append(repo._scale_index(nb.id, allow_stale=True))) for _ in range(6)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert calls["n"] == 1                          # 单飞:只加载一次
    assert all(r is results[0] for r in results)    # 都拿到同一实例


def test_combined_graph_cache_hits_under_ingestion(repo, monkeypatch):
    """flag 关:摄取(kg_mutation_seq 变)期间组合图缓存命中,不每查询 _load 重建。"""
    nb = _indexed_nb_with_delta(repo)
    base_indexes = [(nb.id, repo._scale_index(nb.id, allow_stale=True))]
    loads = {"n": 0}
    orig = repo._vector_cache.get

    def counting_get(key, version, loader):
        # 只计数 scale_combined 的加载,忽略 entchunk/elemchunk 等其他 cache 键
        if key.endswith(":scale_combined"):
            def wrapped():
                loads["n"] += 1
                return loader()
        else:
            wrapped = loader
        return orig(key, version, wrapped)
    monkeypatch.setattr(repo._vector_cache, "get", counting_get)

    repo._scale_combined_graph(nb.id, base_indexes)
    _insert_source_chunk(repo, nb.id, "sC", "cC", "carol", 3)  # bump kg_mutation_seq
    repo._scale_combined_graph(nb.id, base_indexes)
    assert loads["n"] == 1     # flag 关:active churn 不进 key → 第二次命中缓存


def test_combined_graph_rebuilds_when_flag_on_and_delta_changes(repo, monkeypatch):
    """flag 开:delta 变仍触发组合图重建(active_ver 保留在 key 里)。"""
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    nb = _indexed_nb_with_delta(repo)
    base_indexes = [(nb.id, repo._scale_index(nb.id, allow_stale=True))]
    loads = {"n": 0}
    orig = repo._vector_cache.get

    def counting_get(key, version, loader):
        # 只计数 scale_combined 的加载,忽略 entchunk/elemchunk 等其他 cache 键
        if key.endswith(":scale_combined"):
            def wrapped():
                loads["n"] += 1
                return loader()
        else:
            wrapped = loader
        return orig(key, version, wrapped)
    monkeypatch.setattr(repo._vector_cache, "get", counting_get)

    repo._scale_combined_graph(nb.id, base_indexes)
    _insert_source_chunk(repo, nb.id, "sC", "cC", "carol", 3)
    repo._scale_combined_graph(nb.id, base_indexes)
    assert loads["n"] == 2     # flag 开:delta 变 → 版本键变 → 重建


def test_active_kg_delta_skips_count_when_gated(repo, monkeypatch):
    """indexed + flag 关:_active_kg_delta 返 ([],[],[]) 且不调 _index_delta 的完整 COUNT。"""
    nb = _indexed_nb_with_delta(repo)
    calls = {"index_delta": 0}
    real = repo.retrieval.graph._index_delta
    monkeypatch.setattr(repo.retrieval.graph, "_index_delta",
                        lambda n: (calls.__setitem__("index_delta", calls["index_delta"] + 1), real(n))[1])
    out = repo._active_kg_delta(nb.id)
    assert out == ([], [], [])
    assert calls["index_delta"] == 0   # 门控早退:不触碰 _index_delta


def test_active_kg_delta_gathers_when_flag_on(repo, monkeypatch):
    """flag 开:仍 gather delta(不早退),保持既有行为。"""
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    nb = _indexed_nb_with_delta(repo)
    node_ids, edges, chunk_ids = repo._active_kg_delta(nb.id)
    assert "cB" in chunk_ids            # delta chunk 被 gather


def _build_indexed_base(repo, name="base"):
    """tier='base' notebook WITH a built scale index (mirrors
    test_scale_xlayer_bridge_delta.py's _build_indexed_base)."""
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name=name))
    repo.mark_notebook_base(nb.id)
    _insert_source_chunk(repo, nb.id, "sBase", "cBase", "base-alpha", 1)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    return nb


def test_combined_graph_key_keeps_active_ver_for_unindexed_active_over_base(repo, monkeypatch):
    """联邦回归:active 自身未索引(无 manifest),但有独立的 tier='base' 索引参与者
    时,flag 关也必须保留 active_ver——_active_kg_delta 对未索引的 active 不早退,
    会把整个 active KG gather+splice 进组合图,故 active 的写入必须使缓存失效,
    否则会静默服务陈旧组合图(见 finding)。"""
    base = _build_indexed_base(repo)
    from app.models.schemas import NotebookCreate
    active = repo.create_notebook(NotebookCreate(name="active"))
    # active 无 build_scale_index → 无 manifest.json → active_indexed=False
    assert not os.path.exists(os.path.join(
        repo.settings.storage_dir, "kg_index", active.id, "manifest.json"))

    base_indexes = [(base.id, repo._scale_index(base.id, allow_stale=True))]
    loads = {"n": 0}
    orig = repo._vector_cache.get

    def counting_get(key, version, loader):
        if key.endswith(":scale_combined"):
            def wrapped():
                loads["n"] += 1
                return loader()
        else:
            wrapped = loader
        return orig(key, version, wrapped)
    monkeypatch.setattr(repo._vector_cache, "get", counting_get)

    repo._scale_combined_graph(active.id, base_indexes)
    _insert_source_chunk(repo, active.id, "sC", "cC", "carol", 3)  # active KG 变更
    repo._scale_combined_graph(active.id, base_indexes)
    assert loads["n"] == 2      # active_ver 保留在 key 里 → 变更触发重建,不陈旧命中

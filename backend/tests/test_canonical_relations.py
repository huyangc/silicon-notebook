import sqlite3

import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository, SCHEMA_VERSION


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _table_cols(repo, table):
    with repo._connect() as db:
        return {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def test_fresh_db_has_canonical_relations_table(repo):
    assert {"notebook_id", "canonical_src", "edge_type", "canonical_tgt",
            "support_count", "source_count", "sample_relation_ids",
            "updated_at"} <= _table_cols(repo, "canonical_relations")
    assert "canonical_rel_seq" in _table_cols(repo, "unified_kg_state")


def test_deployed_v7_db_gets_backfilled(tmp_path, monkeypatch):
    # 模拟已部署 user_version=7 的库:全新建库后删掉新表/新列、回拨版本号,
    # 再次实例化必须经 _migration_8 补齐(schema-migration-convention 教训用例)。
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'m.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    SQLiteRepository(Settings())  # 建全新库(version=SCHEMA_VERSION)
    raw = sqlite3.connect(tmp_path / "m.db")
    raw.execute("DROP TABLE canonical_relations")
    raw.execute("ALTER TABLE unified_kg_state DROP COLUMN canonical_rel_seq")
    raw.execute("PRAGMA user_version = 7")
    raw.commit(); raw.close()
    r2 = SQLiteRepository(Settings())  # 重新迁移:必须跑 _migration_8
    assert "canonical_src" in _table_cols(r2, "canonical_relations")
    assert "canonical_rel_seq" in _table_cols(r2, "unified_kg_state")


def test_schema_version_bumped():
    assert SCHEMA_VERSION == 8


def _mk_src(repo, nb_id, sid):
    """插一行最小 sources 满足 knowledge_relations.source_id 的 FK(_connect 常开
    PRAGMA foreign_keys=ON,brief helper 直接传 's1'/'s2' 作 source_id + 关系会 FK
    失败;补建真实源行 — 适配已在报告说明)。"""
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id, notebook_id, title, source_type, created_at, updated_at) "
            "VALUES (?, ?, ?, 'markdown', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (sid, nb_id, sid))


def _mk_nb_with_relations(repo):
    """两源:s1/s2 各有 A--supports-->B 的关系(A/B 同名跨源,会折叠到同 canonical);
    另有 s1 内 rejected 边与自环候选。返回 (nb_id, ids)。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    for src, (a, b) in {"s1": ("A1", "B1"), "s2": ("A2", "B2")}.items():
        _mk_src(repo, nb.id, src)
        repo.store_kg(nb.id, src, [
            {"local_id": a, "object_type": "concept",
             "payload": {"name": "cascode", "section_path": "1"}, "evidence": []},
            {"local_id": b, "object_type": "concept",
             "payload": {"name": "gain", "section_path": "1"}, "evidence": []},
        ], [
            {"source_local_id": a, "target_local_id": b, "edge_type": "supports", "evidence": []},
        ])
    repo.rebuild_unified_kg(nb.id)
    return nb


def _canon_rows(repo, nb_id):
    with repo._connect() as db:
        return db.execute(
            "SELECT * FROM canonical_relations WHERE notebook_id=?", (nb_id,)).fetchall()


def test_rebuild_aggregates_cross_source_support(repo):
    nb = _mk_nb_with_relations(repo)
    rows = _canon_rows(repo, nb.id)
    assert len(rows) == 1                      # 两源同一逻辑边折叠成一行
    r = rows[0]
    assert r["canonical_src"] == "K-cascode" and r["canonical_tgt"] == "K-gain"
    assert r["edge_type"] == "supports"
    assert r["support_count"] == 2 and r["source_count"] == 2
    import json as _j
    assert 1 <= len(_j.loads(r["sample_relation_ids"])) <= 5


def test_rejected_edges_excluded(repo):
    nb = _mk_nb_with_relations(repo)
    with repo._write() as db:
        db.execute("UPDATE knowledge_relations SET review_status='rejected' "
                   "WHERE notebook_id=? AND source_id='s2'", (nb.id,))
    repo.rebuild_canonical_relations(nb.id, force=True)
    r = _canon_rows(repo, nb.id)[0]
    assert r["support_count"] == 1 and r["source_count"] == 1


def test_direction_preserved(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _mk_src(repo, nb.id, "s1")
    repo.store_kg(nb.id, "s1", [
        {"local_id": "X", "object_type": "concept",
         "payload": {"name": "a", "section_path": "1"}, "evidence": []},
        {"local_id": "Y", "object_type": "concept",
         "payload": {"name": "b", "section_path": "1"}, "evidence": []},
    ], [
        {"source_local_id": "X", "target_local_id": "Y", "edge_type": "supports", "evidence": []},
        {"source_local_id": "Y", "target_local_id": "X", "edge_type": "supports", "evidence": []},
    ])
    repo.rebuild_unified_kg(nb.id)
    assert len(_canon_rows(repo, nb.id)) == 2   # A→B 与 B→A 不合并


def test_seq_gate_skips_then_force_recomputes(repo):
    nb = _mk_nb_with_relations(repo)
    with repo._connect() as db:
        seq0 = db.execute("SELECT canonical_rel_seq FROM unified_kg_state WHERE notebook_id=?",
                          (nb.id,)).fetchone()["canonical_rel_seq"]
    assert seq0 >= 0                           # rebuild 后闸已写
    assert repo.rebuild_canonical_relations(nb.id) >= 0   # 未变 → 跳过不炸
    assert repo.rebuild_canonical_relations(nb.id, force=True) == 1


def test_unified_graph_edges_carry_support(repo):
    nb = _mk_nb_with_relations(repo)
    g = repo.unified_graph(nb.id, level="object")
    sup = [e for e in g["edges"] if e.get("source_count")]
    assert sup and sup[0]["support_count"] == 2 and sup[0]["source_count"] == 2


def test_neighbors_edges_carry_support(repo):
    nb = _mk_nb_with_relations(repo)
    g = repo.unified_graph(nb.id, level="object")
    nid = next(n["id"] for n in g["nodes"])
    nbres = repo.kg_neighbors(nb.id, nid)
    assert any(e.get("source_count") == 2 for e in nbres["edges"])


def test_neighbors_incoming_edge_carries_support(repo):
    # 表里存 K-cascode --supports--> K-gain;kg_neighbors 把边统一画成
    # 「查询节点→邻居」,查 TARGET 侧(K-gain)时展示方向与存储方向相反,
    # 注解必须经对称回退仍命中(否则入边永远丢 support/source_count)。
    nb = _mk_nb_with_relations(repo)
    res = repo.kg_neighbors(nb.id, "K-gain")
    assert any(e.get("source_count") == 2 for e in res["edges"])


def test_empty_table_leaves_edges_bare(repo):
    nb = _mk_nb_with_relations(repo)
    with repo._write() as db:
        db.execute("DELETE FROM canonical_relations WHERE notebook_id=?", (nb.id,))
        db.execute("UPDATE unified_kg_state SET canonical_rel_seq=-1 WHERE notebook_id=?", (nb.id,))
    repo._unified_cache.clear()
    g = repo.unified_graph(nb.id, level="object")
    assert all("support_count" not in e for e in g["edges"])

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


def _cols(repo, table):
    with repo._connect() as db:
        return {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def test_fresh_db_has_mention_bridge_tables(repo):
    assert {"notebook_id", "claim_object_id", "concept_canonical_id", "matched_alias"} <= _cols(repo, "mention_edges")
    assert {"notebook_id", "canonical_a", "canonical_b", "bridge_claims"} <= _cols(repo, "concept_comentions")
    assert "mention_seq" in _cols(repo, "unified_kg_state")


def test_deployed_v8_db_gets_backfilled(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'m.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    SQLiteRepository(Settings())
    raw = sqlite3.connect(tmp_path / "m.db")
    raw.execute("DROP TABLE mention_edges")
    raw.execute("DROP TABLE concept_comentions")
    raw.execute("ALTER TABLE unified_kg_state DROP COLUMN mention_seq")
    raw.execute("PRAGMA user_version = 8")
    raw.commit(); raw.close()
    r2 = SQLiteRepository(Settings())
    assert "claim_object_id" in _cols(r2, "mention_edges")
    assert "bridge_claims" in _cols(r2, "concept_comentions")
    assert "mention_seq" in _cols(r2, "unified_kg_state")


def test_schema_version_is_9():
    assert SCHEMA_VERSION == 9


def _mk_src(repo, nb_id, sid):
    """插一行最小 sources 满足 knowledge_objects.source_id 的 FK(与
    test_canonical_relations._mk_src 同款；brief helper 直接传 's1' 作 source_id）。"""
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id, notebook_id, title, source_type, created_at, updated_at) "
            "VALUES (?, ?, ?, 'markdown', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (sid, nb_id, sid))


def _seed_bridge_nb(repo):
    """3 源:GQA/MQA 各跨 2 源(跨源簇);2 条对比 claim 同提两者;1 条只提 GQA。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    for i in (1, 2, 3):
        _mk_src(repo, nb.id, f"s{i}")
    objs = lambda src, names, t="concept": [
        {"local_id": f"{src}-{j}", "object_type": t,
         "payload": {"name": n, "section_path": "1"}, "evidence": []}
        for j, n in enumerate(names)]
    repo.store_kg(nb.id, "s1", objs("s1", ["Grouped-query attention (GQA)", "Multi-Query Attention (MQA)"]), [])
    repo.store_kg(nb.id, "s2", objs("s2", ["Grouped-query attention (GQA)", "Multi-Query Attention (MQA)"]), [])
    repo.store_kg(nb.id, "s3", objs("s3", [
        "GQA uses fewer KV heads than MQA while keeping quality.",
        "GQA halves KV cache compared with MQA in practice.",
        "GQA is adopted by many recent models."], t="claim"), [])
    repo.rebuild_unified_kg(nb.id)
    return nb


def test_rebuild_extracts_mention_edges_and_comentions(repo):
    nb = _seed_bridge_nb(repo)
    with repo._connect() as db:
        me = db.execute("SELECT COUNT(*) c FROM mention_edges WHERE notebook_id=?", (nb.id,)).fetchone()["c"]
        cm = db.execute("SELECT * FROM concept_comentions WHERE notebook_id=?", (nb.id,)).fetchall()
        # canonical_id 由 _norm 决定(连字符→空格,如 "K-grouped query attention"),
        # 从簇表取真实值而非硬编码(brief 原文写成连字符版是笔误——见报告)。
        gqa = db.execute("SELECT DISTINCT canonical_id FROM concept_clusters "
                         "WHERE notebook_id=? AND canonical_name LIKE 'Grouped%'",
                         (nb.id,)).fetchone()["canonical_id"]
        mqa = db.execute("SELECT DISTINCT canonical_id FROM concept_clusters "
                         "WHERE notebook_id=? AND canonical_name LIKE 'Multi%'",
                         (nb.id,)).fetchone()["canonical_id"]
    assert me >= 5                       # 3条claim×命中(2+2+1)
    assert len(cm) == 1                  # GQA↔MQA 一对
    assert cm[0]["bridge_claims"] == 2   # 两条对比claim
    a, b = sorted((gqa, mqa))
    assert (cm[0]["canonical_a"], cm[0]["canonical_b"]) == (a, b)
    # 扫描 FTS 是连接私有 TEMP 表(纯内存,零 WAL):绝不能落进持久 schema。
    with repo._connect() as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='mention_scan_fts'").fetchone() is None


def test_df_cap_drops_generic_alias(repo):
    nb = _seed_bridge_nb(repo)
    repo.settings.mention_alias_df_floor = 0      # 关掉绝对下限,让比例门生效
    repo.settings.mention_alias_df_cap = 0.0001   # 人为压到全部超限
    assert repo.rebuild_mention_bridge(nb.id, force=True) == 0


def test_seq_gate_and_flag(repo):
    nb = _seed_bridge_nb(repo)
    n1 = repo.rebuild_mention_bridge(nb.id)       # 未变 → 跳过,返回现有行数
    assert n1 >= 5
    repo.settings.mention_bridge_enabled = False
    assert repo.rebuild_mention_bridge(nb.id, force=True) == 0   # flag 关 → 清空/不建

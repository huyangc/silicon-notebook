"""P0-A:cluster_mutation_seq 列 + 探针 O(1) 化的行为契约。
核心不变量:任何 concept_clusters 写路径之后,_scale_index_version 必须反映变化
(经 cseq bump→memo miss→冷路径重算);热路径不再每次跑 concept_clusters COUNT/MAX。"""
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository


import pytest
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo_factory(tmp_path, monkeypatch):
    def _make():
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
        monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
        monkeypatch.setenv("LLM_LOG_ENABLED", "false")
        r = SQLiteRepository(Settings())
        bind_all_embedding_clients(r, FakeEmbedder(dim=16))
        nb = r.create_notebook(NotebookCreate(name="nb"))
        return r, nb.id
    return _make


def _cseq(repo, nb):
    with repo._connect() as db:
        row = db.execute(
            "SELECT cluster_mutation_seq FROM unified_kg_state WHERE notebook_id=?",
            (nb,)).fetchone()
    return int(row["cluster_mutation_seq"]) if row else 0


def test_migration_adds_cluster_seq_column(repo_factory):
    repo, nb = repo_factory()
    with repo._connect() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(unified_kg_state)")}
    assert "cluster_mutation_seq" in cols


def test_write_clusters_bumps_cseq(repo_factory):
    repo, nb = repo_factory()
    before = _cseq(repo, nb)
    repo.write_clusters(nb, [{"canonical_id": "c1", "member_object_id": "o1",
                              "canonical_name": "N"}])
    assert _cseq(repo, nb) > before


def test_append_clusters_bumps_cseq(repo_factory):
    repo, nb = repo_factory()
    before = _cseq(repo, nb)
    repo.append_clusters(nb, [{"canonical_id": "c2", "member_object_id": "o2",
                               "canonical_name": "M"}])
    assert _cseq(repo, nb) > before


def test_version_changes_after_cluster_write(repo_factory):
    """写 clusters 后 version key 必须变化(memo 失效→冷路径重算 COUNT/MAX)。"""
    repo, nb = repo_factory()
    v1 = repo._scale_index_version(nb)
    repo.write_clusters(nb, [{"canonical_id": "c1", "member_object_id": "o1",
                              "canonical_name": "N"}])
    v2 = repo._scale_index_version(nb)
    assert v1 != v2


def test_kwtok_bounded_skips_count_and_matches_live(repo_factory):
    """bounded=True:不跑 knowledge_objects COUNT,token set 与非缓存构建逐字节等价。

    sqlite3.Connection.execute 是内建 C 类型属性,不可直接赋值 monkeypatch(与
    test_incremental_fuse_perf.py 的 _loader_spy 同一限制)——用包装类记录 SQL。"""
    from app.services.retrieval import _tokens, _payload_text
    repo, nb = repo_factory()
    objs = [{"id": "o1", "payload": {"name": "带隙基准", "statement": "PTAT 电流"},
             "evidence": []}]
    seen_sql = []

    class _SpyConn:
        def __init__(self, inner):
            self._inner = inner
        def execute(self, sql, *a):
            seen_sql.append(sql)
            return self._inner.execute(sql, *a)
        def __getattr__(self, name):
            return getattr(self._inner, name)

    with repo._connect() as real_db:
        db = _SpyConn(real_db)
        ts = repo._keyword_token_sets(db, nb, objs, bounded=True)
    assert not any("COUNT" in s for s in seen_sql)
    expected = frozenset(_tokens(f"{_payload_text(objs[0]['payload'])} "))
    assert ts["o1"] == expected

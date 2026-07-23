import sqlite3

import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository, SCHEMA_VERSION
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
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
    # >= 而非 ==:后续迁移(如 _migration_10)会继续推高 SCHEMA_VERSION,这里只断言
    # mention_bridge 那一步(_migration_9)已落地,不锁死为全局当前版本号。
    assert SCHEMA_VERSION >= 9


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


# --------------------------------------------------------------------------- #
# Task 4: sibling_peers(共提兄弟)+ resolve_comparison_peers(共提优先/社区回退)
# --------------------------------------------------------------------------- #
def test_ppr_graph_contains_mention_edges(repo):
    nb = _seed_bridge_nb(repo)
    G, key_to_idx, _ = repo._ppr_graph(nb.id)
    # 桥 claim 节点应连到 GQA/MQA 的 cluster router
    with repo._connect() as db:
        row = db.execute("SELECT claim_object_id, concept_canonical_id FROM mention_edges "
                         "WHERE notebook_id=? LIMIT 1", (nb.id,)).fetchone()
    a = key_to_idx.get(row["claim_object_id"])
    b = key_to_idx.get(f"cluster:{row['concept_canonical_id']}")
    assert a is not None and b is not None
    assert G.has_edge(a, b)


def test_sibling_peers_returns_comention_partner(repo):
    from app.services.communities import sibling_peers
    nb = _seed_bridge_nb(repo)
    peers = sibling_peers(_community_queries(repo), nb.id, "Grouped-query attention (GQA)", top_k=5)
    assert peers and "Multi-Query Attention" in peers[0][0]
    assert peers[0][1] == 2


def test_sibling_peers_respects_min_bridge(repo):
    from app.services.communities import sibling_peers
    nb = _seed_bridge_nb(repo)
    repo.settings.sibling_min_bridge = 3          # 唯一共提对 bridge_claims=2 < 3 → 全过滤
    assert sibling_peers(_community_queries(repo), nb.id, "Grouped-query attention (GQA)") == []


def test_sibling_peers_unresolved_focal_silent_empty(repo):
    """焦点解析不到 → [](不 emit,由调用方回退社区路径兜底文案)。"""
    from app.services.communities import sibling_peers
    nb = _seed_bridge_nb(repo)
    assert sibling_peers(_community_queries(repo), nb.id, "No Such Concept XYZ") == []


def test_resolve_comparison_peers_prefers_comention_then_community(repo, monkeypatch):
    """共提优先→回退社区:直接单测抽出的共享 helper(免 mock LLM)。"""
    from app.services import communities as C
    nb = _seed_bridge_nb(repo)
    # 有 concept_comentions → source=comention,名单来自共提兄弟(本 fixture 未建社区数据)
    names, source = C.resolve_comparison_peers(
        _community_queries(repo), nb.id, "Grouped-query attention (GQA)", "compare gqa and mqa",
        top_k=5, candidates=50)
    assert source == "comention"
    assert any("Multi-Query Attention" in n for n in names)
    # 抬高门槛让 sibling_peers 空 → 回退 community_peers(打桩验证被调用 + 结果透传 + source 标记)
    monkeypatch.setattr(C, "community_peers", lambda *a, **k: ["STUB-COMMUNITY-PEER"])
    repo.settings.sibling_min_bridge = 99
    names2, source2 = C.resolve_comparison_peers(
        _community_queries(repo), nb.id, "Grouped-query attention (GQA)", "q", top_k=5, candidates=50)
    assert source2 == "community"
    assert names2 == ["STUB-COMMUNITY-PEER"]


def test_mention_temp_fts_delegates_on_one_private_connection(repo, monkeypatch):
    from contextlib import contextmanager
    nb = _seed_bridge_nb(repo)
    runtime = object.__getattribute__(repo, "_runtime")
    store = runtime.unified_kg
    events = []
    write_ids = set()
    close_calls = 0
    original_write = runtime.database.write
    original_close_local = runtime.database.close_local

    @contextmanager
    def traced_write():
        with original_write() as db:
            write_ids.add(id(db))
            yield db

    monkeypatch.setattr(runtime.database, "write", traced_write)

    def traced_close_local():
        nonlocal close_calls
        close_calls += 1
        return original_close_local()

    monkeypatch.setattr(runtime.database, "close_local", traced_close_local)

    def spy(name, *, cursor=False):
        original = getattr(store, name)

        def wrapped(db, *args, **kwargs):
            assert isinstance(db, sqlite3.Connection)
            result = original(db, *args, **kwargs)
            if cursor:
                assert isinstance(result, sqlite3.Cursor)
            events.append((name, id(db), args))
            return result

        monkeypatch.setattr(store, name, wrapped)

    spy("mention_seed_rows")
    spy("replace_mention_bridge")
    original_batches = store.mention_alias_candidate_batches

    def traced_batches(claims, aliases):
        result = original_batches(claims, aliases)
        events.append(("mention_alias_candidate_batches", None, (claims, aliases)))
        return result

    monkeypatch.setattr(store, "mention_alias_candidate_batches", traced_batches)

    count = getattr(repo, "rebuild_mention_bridge")(nb.id, force=True)

    assert count >= 5
    names = [name for name, _db, _args in events]
    assert names[0] == "mention_seed_rows"
    assert names.count("mention_alias_candidate_batches") == 1
    assert close_calls == 1
    replace = next(event for event in events if event[0] == "replace_mention_bridge")
    assert replace[1] in write_ids


def test_mention_alias_batches_query_lazily_one_alias_at_a_time(repo, monkeypatch):
    runtime = object.__getattribute__(repo, "_runtime")
    store = runtime.unified_kg
    queries = []
    close_calls = 0
    original_close_local = runtime.database.close_local

    monkeypatch.setattr(store, "claim_name_rows", lambda _db, _rows: None)

    def scan(_db, match_expr):
        queries.append(match_expr)
        return iter(({"rowid": 1}, {"rowid": 2}))

    monkeypatch.setattr(store, "mention_scan_matches", scan)

    def close_local():
        nonlocal close_calls
        close_calls += 1
        return original_close_local()

    monkeypatch.setattr(runtime.database, "close_local", close_local)
    claims = (("claim-1", "first claim"), ("claim-2", "second claim"))

    with store.mention_alias_candidate_batches(claims, ("first", "later")) as batches:
        batches = iter(batches)
        alias, rows = next(batches)
        assert alias == "first"
        assert queries == ['"first"']
        assert list(rows) == list(claims)
        assert queries == ['"first"']
        alias, rows = next(batches)
        assert alias == "later"
        assert queries == ['"first"', '"later"']
        assert list(rows) == list(claims)

    assert close_calls == 1


def test_mention_alias_batches_cleanup_on_exceptional_exit(repo, monkeypatch):
    runtime = object.__getattribute__(repo, "_runtime")
    store = runtime.unified_kg
    close_calls = 0
    original_close_local = runtime.database.close_local
    monkeypatch.setattr(store, "claim_name_rows", lambda _db, _rows: None)
    monkeypatch.setattr(
        store, "mention_scan_matches", lambda _db, _expr: iter(({"rowid": 1},))
    )

    def close_local():
        nonlocal close_calls
        close_calls += 1
        return original_close_local()

    monkeypatch.setattr(runtime.database, "close_local", close_local)

    with pytest.raises(RuntimeError, match="stop current alias"):
        with store.mention_alias_candidate_batches(
            (("claim-1", "claim text"),), ("alias", "later")
        ) as batches:
            alias, rows = next(iter(batches))
            assert alias == "alias"
            assert next(iter(rows)) == ("claim-1", "claim text")
            raise RuntimeError("stop current alias")

    assert close_calls == 1


def _community_queries(repo):
    from app.services.communities import CommunityQueryService

    runtime = object.__getattribute__(repo, "_runtime")
    settings = object.__getattribute__(repo, "settings")
    return CommunityQueryService(
        notebooks=runtime.notebook_store,
        unified_kg=runtime.unified_kg,
        event_log=runtime.event_log,
        sibling_min_bridge=settings.sibling_min_bridge,
    )

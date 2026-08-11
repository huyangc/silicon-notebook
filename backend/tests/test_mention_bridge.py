import sqlite3

import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
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


# ── 单 claim 组合上限(P1 轮批 D:O(hits²) 组合爆炸加界) ─────────────────────

def _seed_wide_claim_nb(repo, concept_count):
    """一条 claim 同时提到 ``concept_count`` 个跨源概念。

    别名 DF 双门约束的是「单**别名**命中多少 claim」,这里每个别名只命中 1 条,
    双门全程不触发 —— 正是它管不到的那一维:「单条 **claim** 命中多少
    canonical」。名字取等长且互不为子串,`sorted()` 的顺序 == 生成顺序,
    截断集合可以直接算出来。"""
    nb = repo.create_notebook(NotebookCreate(name="wide"))
    for i in (1, 2, 3):
        _mk_src(repo, nb.id, f"s{i}")
    names = [f"conceptw{chr(97 + i // 26)}{chr(97 + i % 26)}"
             for i in range(concept_count)]
    concepts = lambda src: [
        {"local_id": f"{src}-{j}", "object_type": "concept",
         "payload": {"name": n, "section_path": "1"}, "evidence": []}
        for j, n in enumerate(names)]
    # 每个概念都出现在 s1/s2 两个源里 → 跨源簇(mention bridge 的准入条件)。
    repo.store_kg(nb.id, "s1", concepts("s1"), [])
    repo.store_kg(nb.id, "s2", concepts("s2"), [])
    repo.store_kg(nb.id, "s3", [
        {"local_id": "s3-0", "object_type": "claim",
         "payload": {"name": "These are jointly evaluated: "
                             + ", ".join(names) + ".",
                     "section_path": "1"},
         "evidence": []}], [])
    repo.rebuild_unified_kg(nb.id)
    return nb, names


def _bridge_rows(repo, notebook_id):
    with repo._connect() as db:
        edges = [r["concept_canonical_id"] for r in db.execute(
            "SELECT concept_canonical_id FROM mention_edges WHERE notebook_id=?",
            (notebook_id,))]
        pairs = {(r["canonical_a"], r["canonical_b"]) for r in db.execute(
            "SELECT canonical_a, canonical_b FROM concept_comentions "
            "WHERE notebook_id=?", (notebook_id,))}
    return edges, pairs


def test_comention_cap_constant_is_the_registered_literal():
    """常量语义钉死用**字面量**:夹具从常量派生,只断言派生量的话「把上限调大」
    这种退化变异不会变红(评审的退化变异发现)。16 → C(16,2)=120 是登记在
    `_MENTION_MAX_CANONS_PER_CLAIM` docstring 与设计文档批 D 段里的那两个数。"""
    from app.services.knowledge_lifecycle import _MENTION_MAX_CANONS_PER_CLAIM

    assert _MENTION_MAX_CANONS_PER_CLAIM == 16
    assert 16 * (16 - 1) // 2 == 120


def test_comentions_below_cap_are_the_full_combination_oracle(repo):
    """≤ 上限时逐位等于「全部两两组合」—— 加界不得改变既有输出。"""
    from itertools import combinations

    nb, _ = _seed_wide_claim_nb(repo, 12)          # < 16
    edges, pairs = _bridge_rows(repo, nb.id)
    assert len(edges) == 12
    assert pairs == set(combinations(sorted(set(edges)), 2))
    assert len(pairs) == 12 * 11 // 2


def test_comentions_skip_the_whole_claim_past_the_cap(repo):
    """超限:该 claim **整条**退出组合(不产出任何共提对),而不是按字典序截断 ——
    截断会系统性偏向 ASCII 命名的概念。mention_edges(线性,检索侧真正消费的
    产物)一条不少。"""
    nb, _ = _seed_wide_claim_nb(repo, 22)          # > 16
    edges, pairs = _bridge_rows(repo, nb.id)
    assert len(edges) == 22                        # edges 不受影响
    assert len(set(edges)) == 22
    assert pairs == set()                          # 整条跳过 → 零共提对
    # 反面钉:若改成「取前 16」,这里会是 120 对。
    assert len(pairs) != 120


def test_comention_claim_skip_is_reported_as_a_countonly_event(repo):
    """跳过必须可观测,且事件里只有计数 —— 无 claim 正文、无 canonical id
    (对齐隔壁 `mention_alias_df_dropped`)。"""
    nb, _ = _seed_wide_claim_nb(repo, 22)
    events = []
    runtime = object.__getattribute__(repo, "_runtime")
    original = runtime.knowledge_lifecycle.event_log.emit
    try:
        runtime.knowledge_lifecycle.event_log.emit = (
            lambda payload: events.append(dict(payload)) or original(payload))
        repo.rebuild_mention_bridge(nb.id, force=True)
    finally:
        runtime.knowledge_lifecycle.event_log.emit = original
    skipped = [e for e in events if e["kind"] == "mention_comention_claim_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["skipped"] == 1
    assert skipped[0]["max_canonicals"] == 16
    assert set(skipped[0]) == {"kind", "notebook_id", "skipped", "max_canonicals"}


def test_comention_claim_skip_has_no_ordering_bias(repo):
    """无偏置的直接判据:把概念名换成「一半 ASCII、一半中文」,输出仍是**空**。

    截断实现在同一夹具上会只保留 ASCII 那一半的组合(canonical id 由 `_norm`
    派生,中文码位排在 ASCII 之后),这条用例正是那个偏置的守卫。"""
    nb = repo.create_notebook(NotebookCreate(name="mixed"))
    for i in (1, 2, 3):
        _mk_src(repo, nb.id, f"s{i}")
    names = ([f"latinconcept{chr(97 + i)}" for i in range(11)]
             + [f"中文概念{chr(97 + i)}" for i in range(11)])
    concepts = lambda src: [
        {"local_id": f"{src}-{j}", "object_type": "concept",
         "payload": {"name": n, "section_path": "1"}, "evidence": []}
        for j, n in enumerate(names)]
    repo.store_kg(nb.id, "s1", concepts("s1"), [])
    repo.store_kg(nb.id, "s2", concepts("s2"), [])
    repo.store_kg(nb.id, "s3", [
        {"local_id": "s3-0", "object_type": "claim",
         "payload": {"name": "These are jointly evaluated: " + "、".join(names) + "。",
                     "section_path": "1"},
         "evidence": []}], [])
    repo.rebuild_unified_kg(nb.id)
    edges, pairs = _bridge_rows(repo, nb.id)
    assert len(set(edges)) == 22          # 中英两侧都真的命中了
    assert any(cid.startswith("K-中文") for cid in edges)
    assert pairs == set()                 # 无偏置:不是「只留 latin 那一半」


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

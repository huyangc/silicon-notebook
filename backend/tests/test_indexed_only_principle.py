"""KG 对象侧/关系侧/PPR 图基底 delta 门控(SCALE_SEARCH_INCLUDE_DELTA):已索引库
检索默认只搜已索引部分,水位后新增的 KG 对象/关系/self-delta(PPR 组合图 splice)
默认不被语义暴力检回 —— 与 chunk 侧(_retrieve_chunks_ann)同一原则。flag 开时
保持强一致的 delta 暴力(今日行为)。
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
    hits = repo.retrieval.candidates._retrieve_scored(nb.id, "bravo")
    assert oid not in {h.object_id for h in hits}


def test_object_delta_included_when_opted_in(repo, monkeypatch):
    nb, oid = _build_indexed_nb_with_delta_object(repo)
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    hits = repo.retrieval.candidates._retrieve_scored(nb.id, "bravo")
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
    sims = repo.retrieval.candidates._relation_ann_candidates(
        nb.id, repo.embedder.embed_query("bravo"),
        repo._scale_index(nb.id, allow_stale=True), 10)
    assert rid not in sims


def test_relation_delta_included_when_opted_in(repo, monkeypatch):
    nb, rid = _build_indexed_nb_with_delta_relation(repo)
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    sims = repo.retrieval.candidates._relation_ann_candidates(
        nb.id, repo.embedder.embed_query("bravo"),
        repo._scale_index(nb.id, allow_stale=True), 10)
    assert rid in sims


# ── PPR 图基底 self-delta splice(第四处门控)─────────────────────────────────


def _insert_source_chunk(repo, nb_id, sid, cid, text, embed_text, day):
    """同 tests/test_scale_delta_policy.py 的同名 helper:插入一个 source + 一个
    chunk + 该 chunk 的 embedding(embedding 取自 embed_text,可与 chunk 词法文本
    独立摆动,用于让 chunk 只能经语义路径被检回,不能经 FTS)。"""
    with repo._write() as db:
        now = f"2026-07-{day:02d}T00:00:00"
        db.execute(
            "INSERT OR IGNORE INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)", (sid, nb_id, "t", "md", "ready", now, now))
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
            "VALUES (?,?,?,?,?,?,?)", (cid, nb_id, sid, text, "", "[]", now))
        v = repo.embedder.embed_query(embed_text)
        db.execute(
            "INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
            (cid, nb_id, json.dumps(v), now))


def _build_indexed_nb_with_delta_chunk(repo):
    """source A('alpha')进水位 build_scale_index;source B('bravo')之后插入,
    其 chunk embedding 与查询词 'bravo' 最匹配、chunk 文本与查询词无词法重叠 →
    只可能经 self-delta splice 被 scale_ppr 排名收录。返回 (nb, delta_chunk_id)。"""
    nb = repo.create_notebook(NotebookCreate(name="base"))
    _insert_source_chunk(repo, nb.id, "sA", "cA", "alpha content here", "alpha", 1)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)  # watermark = {sA}
    _insert_source_chunk(repo, nb.id, "sB", "cB", "unrelated words xxyyzz", "bravo", 2)
    return nb, "cB"


def test_ppr_splice_excludes_self_delta_by_default(repo):
    """已索引库的 PPR 图基底默认不 splice 水位后 delta:delta chunk 不出现在
    scale_ppr 排名里;开 flag 后出现。用 test_scale_delta_policy 同款构造:
    delta source 的 chunk embedding 与查询最匹配。"""
    nb, d_cid = _build_indexed_nb_with_delta_chunk(repo)
    ranked = dict(repo.retrieval.graph.scale_ppr(nb.id, "bravo"))
    assert d_cid not in ranked


def test_ppr_splice_includes_self_delta_when_opted_in(repo, monkeypatch):
    nb, d_cid = _build_indexed_nb_with_delta_chunk(repo)
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    ranked = dict(repo.retrieval.graph.scale_ppr(nb.id, "bravo"))
    assert d_cid in ranked


# ── fold 自动升级 full(delta 超阈值)────────────────────────────────────────


def test_resolve_scale_mode_upgrades_big_delta_fold_to_full(repo, monkeypatch):
    nb, _oid = _build_indexed_nb_with_delta_object(repo)   # 1 个 delta source
    monkeypatch.setattr(repo.settings, "scale_fold_max_delta_sources", 0)
    assert repo._resolve_scale_mode(nb.id, "fold") == "full"
    assert repo._resolve_scale_mode(nb.id, "auto") == "full"
    monkeypatch.setattr(repo.settings, "scale_fold_max_delta_sources", 500)
    assert repo._resolve_scale_mode(nb.id, "fold") == "fold"


def test_fold_threshold_env_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("SCALE_FOLD_MAX_DELTA_SOURCES", "7")
    from app.core.config import Settings
    assert Settings().scale_fold_max_delta_sources == 7


# ── 大库无 ANN 候选拒绝全量暴力(FTS 词法有界兜底)───────────────────────────


def test_big_unindexed_lib_refuses_bruteforce(repo, monkeypatch):
    """大库 + 无索引:不做全量矩阵加载,FTS 词法兜底 + kg_bruteforce_refused 事件。"""
    nb = repo.create_notebook(NotebookCreate(name="big"))
    _sid, _cid, oid = _insert_source_with_object(repo, nb.id, 1)   # payload 名 'obj 1'
    with repo._write() as db:
        db.execute("INSERT INTO kg_objects_fts (notebook_id, object_id, name) VALUES (?,?,?)",
                   (nb.id, oid, "obj 1"))
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)   # 一切皆「大」
    events = []
    monkeypatch.setattr(repo.event_log, "emit", lambda e: events.append(e))

    def _boom(*a, **k):
        raise AssertionError("大库不得触发全量向量矩阵加载")
    monkeypatch.setattr(repo.retrieval.candidates, "_vector_matrix", _boom)

    hits = repo.retrieval.candidates._retrieve_scored(nb.id, "obj 1")
    assert oid in {h.object_id for h in hits}          # FTS 词法兜底仍可命中
    assert any(e.get("kind") == "kg_bruteforce_refused" for e in events)


def test_small_lib_bruteforce_unchanged(repo):
    nb = repo.create_notebook(NotebookCreate(name="small"))
    _sid, _cid, oid = _insert_source_with_object(repo, nb.id, 2)
    hits = repo.retrieval.candidates._retrieve_scored(nb.id, "obj 2")       # 小库全量路径不受影响
    assert oid in {h.object_id for h in hits}


# ── element 侧(fallback layer 2)大库守卫 ───────────────────────────────────


def _insert_source_element(repo, nb_id, sid, eid, text):
    """一个 source + 一个 source_element(带 embedding)。"""
    now = "2026-07-01T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   (sid, nb_id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO source_elements (id,source_id,element_type,location_label,text,metadata,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   (eid, sid, "paragraph", "p1", text, "{}", now))
        v = repo.embedder.embed_query(text)
        db.execute("INSERT INTO element_embeddings (element_id,source_id,notebook_id,vector,created_at) VALUES (?,?,?,?,?)",
                   (eid, sid, nb_id, json.dumps(v), now))


def test_big_lib_element_search_skipped(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="big"))
    _insert_source_with_object(repo, nb.id, 3)
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)
    events = []
    monkeypatch.setattr(repo.event_log, "emit", lambda e: events.append(e))
    assert repo._retrieve_elements(nb.id, "anything") == []
    assert any(e.get("kind") == "element_scoring_skipped" for e in events)


def test_small_lib_element_search_unchanged(repo):
    """小库路径不受影响:_retrieve_elements 仍全表扫并返回打分元素。"""
    nb = repo.create_notebook(NotebookCreate(name="small-elem"))
    _insert_source_element(repo, nb.id, "sE", "eE", "obj 4")
    hits = repo._retrieve_elements(nb.id, "obj 4")
    assert "eE" in {h.element_id for h in hits}


# ── 候选集下游 IN 分批(终审发现:_knowledge_objects id_filter / 边查询 /
# element_embeddings 查询 / _retrieve_chunks_ann hydrate 四处仍单条 IN 内联)──


def _build_indexed_nb_with_multi_delta_objects(repo, n=4):
    """source A('obj 0')进水位;source B 在 build 之后一次插入 n 个 KG 对象
    (oB1..oBn),每个 embedding 都与查询词 'bravo' 最匹配、payload 名字与查询词
    无词法重叠 → 全部只可能经 delta 语义暴力被检回。单一 post-watermark source
    (delta_sources 只有 1 项,批量压力来自候选对象 id 数,不是 delta source 数)。
    返回 (nb, [oB1..oBn])。"""
    nb = repo.create_notebook(NotebookCreate(name="base"))
    _insert_source_with_object(repo, nb.id, 0)          # sA: 'obj 0'
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)                        # watermark = {s0}
    sid = "sB"
    now = "2026-07-02T00:00:00"
    oids = [f"oB{i}" for i in range(1, n + 1)]
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   (sid, nb.id, "t", "md", "ready", now, now))
        for oid in oids:
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,source_id,object_type,payload,evidence,status,owner,last_reviewed,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, sid, "claim", json.dumps({"name": f"zzz {oid}"}), "[]",
                        "approved", "", "", now, now))
            v = repo.embedder.embed_query("bravo")
            db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                       (oid, nb.id, json.dumps(v), now))
    return nb, oids


def test_flag_on_big_delta_multibatch_no_sql_variable_blowup(repo, monkeypatch):
    """flag 开 + _IN_CHUNK 压到 2(4 个 delta 对象 + 1 核对象 → candidate_ids/
    id_filter/elem_id_set 都 >_IN_CHUNK)强制 _knowledge_objects id_filter 分支、
    _retrieve_scored 边查询、element_embeddings 查询三处走多批。批后不抛异常,
    且全部 4 个 delta 对象 id 都在结果里(与单批语义等价 —— 见 PR 报告里记录的
    分批前/后两次结果对照,充当 RED/GREEN 替代证据:_IN_CHUNK=2 时现有单条 IN
    在 5 个变量下本就不会撞真实 SQLite 上限,这条测试真正验证的是"批与不批
    结果一致",不是"不批会抛异常")。"""
    nb, oids = _build_indexed_nb_with_multi_delta_objects(repo, n=4)
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    monkeypatch.setattr(repo.retrieval.candidates, "_IN_CHUNK", 2)
    observed_batch_sizes = []
    original = repo._runtime.knowledge.retrieval_objects

    def spy_retrieval_objects(*args, **kwargs):
        observed_batch_sizes.append(kwargs.get("batch_size"))
        return original(*args, **kwargs)

    monkeypatch.setattr(
        repo._runtime.knowledge, "retrieval_objects", spy_retrieval_objects,
    )
    hits = repo.retrieval.candidates._retrieve_scored(nb.id, "bravo")
    hit_ids = {h.object_id for h in hits}
    assert set(oids) <= hit_ids
    assert 2 in observed_batch_sizes


# Fast inner-loop opt-out: these tests build real HNSW/ANN scale indexes.
# Skip them with `pytest -m "not slow"`; full runs (default) still include them.
import pytest as _pytest_slow  # noqa: E402
pytestmark = _pytest_slow.mark.slow

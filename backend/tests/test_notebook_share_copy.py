# backend/tests/test_notebook_share_copy.py
import json
import uuid
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    return SQLiteRepository(Settings())


@pytest.fixture
def client(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    from app.main import app
    return TestClient(app)


def _mk_nb(repo, name="NB", owner="user-local"):
    """直接建一个空 notebook(不依赖当前用户 ContextVar),返回 nb_id。"""
    nb_id = f"nb-{uuid.uuid4().hex[:10]}"; now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks (id,name,purpose,primary_domain,status,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)", (nb_id, name, "", "Semiconductor", "draft", owner, now, now))
    return nb_id


def _rows(repo, table, nb):
    with repo._connect() as db:
        return db.execute(f"SELECT * FROM {table} WHERE notebook_id=?", (nb,)).fetchall()


def test_copy_notebook_service_refuses_non_copyable_defense_in_depth(tmp_path, monkeypatch):
    """OOM audit P2-7: the deep copy materialises every table into ONE in-memory
    snapshot (300GB+ at 8M objects). The API route already 409s a non-copyable
    notebook, but the service-layer copy_notebook must ALSO refuse it — a
    non-router caller must never be able to trigger that OOM. Removing the guard
    lets the copy proceed (no raise) and fails here."""
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("NOTEBOOK_COPY_MAX_ROWS", "0")   # any node/chunk → not copyable
    repo = SQLiteRepository(Settings())
    nb = _mk_nb(repo)
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("ko-x", nb, "concept", "approved", "", "{}", "[]", "", _now(), _now()))
    assert repo.notebook_copy_stats(nb)["copyable"] is False
    with pytest.raises(ValueError, match="too large to deep-copy"):
        repo.copy_notebook(nb, new_owner_id="user-local")


def _seed_nodes(repo, nb, n):
    with repo._write() as db:
        for i in range(n):
            db.execute(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"ko-{nb}-{i}", nb, "concept", "approved", "", "{}", "[]", "", _now(), _now()))


def test_snapshot_copy_rows_enforces_row_bound_in_its_own_transaction(tmp_path, monkeypatch):
    """codex PR#353 r3: the copyable-row bound is counted INSIDE
    snapshot_copy_rows' own stable-snapshot read transaction (atomic with the
    fetchall), raising BEFORE materialising the oversized rows. Under the limit →
    returns the snapshot; over → raises."""
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("NOTEBOOK_COPY_MAX_ROWS", "1")
    repo = SQLiteRepository(Settings())
    nb = _mk_nb(repo)
    store = repo._runtime.sharing_store
    assert store.snapshot_copy_rows(nb)["notebooks"]        # 0 rows <= 1 → snapshot returned
    _seed_nodes(repo, nb, 2)                                 # 2 nodes > 1
    with pytest.raises(ValueError, match="crossed the copy-size limit"):
        store.snapshot_copy_rows(nb)


def test_copy_notebook_atomic_bound_refuses_grown_source(tmp_path, monkeypatch):
    """The atomic snapshot bound refuses an over-limit notebook even when the
    cached copy_stats pre-check is made to pass (the race it can't close), and
    the raise propagates through copy_notebook. Removing the bound from
    snapshot_copy_rows lets the (grown) snapshot materialize (no raise)."""
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("NOTEBOOK_COPY_MAX_ROWS", "1")
    repo = SQLiteRepository(Settings())
    nb = _mk_nb(repo)
    _seed_nodes(repo, nb, 2)                                 # 2 nodes > 1
    # force the cached pre-check to pass so the atomic snapshot bound is the
    # thing that must refuse the copy
    monkeypatch.setattr(repo._runtime.sharing, "notebook_copy_stats",
                        lambda n: {"copyable": True})
    with pytest.raises(ValueError, match="crossed the copy-size limit"):
        repo.copy_notebook(nb, new_owner_id="user-local")


def _seed_relations(repo, nb, n):
    with repo._write() as db:
        for i in range(n):
            db.execute(
                "INSERT INTO knowledge_relations "
                "(id,notebook_id,source_id,source_object_id,target_object_id,edge_type,evidence,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"rel-{nb}-{i}", nb, None, "ko-a", "ko-b", "rel", "[]", _now()))


def test_snapshot_copy_rows_bounds_every_table_not_just_chunks_nodes(tmp_path, monkeypatch):
    """codex PR#353 r5 P2: the chunks+nodes gate does NOT bound relations /
    embeddings / elements / knowhow, whose combined rows are what the copy holds
    in memory. A notebook with few nodes but many relations passes the chunks+nodes
    gate yet must be refused by the second, total-materialisation bound — so peak
    copy memory is decoupled from a pathological graph/embedding fan-out. Removing
    that second bound lets the (unbounded) snapshot materialise (no raise) and
    fails here."""
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("NOTEBOOK_COPY_MAX_ROWS", "1000")         # chunks+nodes gate: generous
    monkeypatch.setenv("NOTEBOOK_COPY_MAX_SNAPSHOT_ROWS", "10")  # total-materialisation gate: tight
    repo = SQLiteRepository(Settings())
    nb = _mk_nb(repo)
    store = repo._runtime.sharing_store
    _seed_nodes(repo, nb, 2)                                     # 2 nodes ≤ 1000, few total rows
    assert store.snapshot_copy_rows(nb)["notebooks"]            # under both bounds → snapshot returned
    _seed_relations(repo, nb, 20)                               # relations aren't counted by chunks+nodes…
    # chunks+nodes is still 2 ≤ 1000 (passes the FIRST gate), but total materialised
    # rows now exceed 10 → the SECOND bound must refuse it before any fetchall.
    with pytest.raises(ValueError, match="crossed the copy-materialisation limit"):
        store.snapshot_copy_rows(nb)


# ---------------------------------------------------------------------------
# codex #659 R14 P1: snapshot_copy_rows' root ``notebooks`` read used to carry
# no liveness predicate — a tombstone landing between the share route's token
# resolution and this snapshot could either (a) copy a deleting/half-cleared
# notebook whole, or (b) once finalize's DELETE has actually run, leave
# ``snapshot["notebooks"]`` empty, which the caller indexed with a bare
# ``[0]`` — an uncaught ``IndexError`` (500), not the documented 404. Both are
# now the SAME outcome: the root read is liveness-filtered, and an empty
# result raises ``KeyError`` inside the store method itself.
# ---------------------------------------------------------------------------


def test_snapshot_copy_rows_rejects_a_non_live_notebook(tmp_path, monkeypatch):
    """Status flipped to 'deleting' (tombstone already landed) but the row
    still physically exists — must be refused, not silently copied whole."""
    _env(monkeypatch, tmp_path)
    repo = SQLiteRepository(Settings())
    nb = _mk_nb(repo)
    store = repo._runtime.sharing_store
    assert store.snapshot_copy_rows(nb)["notebooks"]  # live → snapshot returned
    with repo._write() as db:
        db.execute("UPDATE notebooks SET status='deleting' WHERE id=?", (nb,))
    with pytest.raises(KeyError):
        store.snapshot_copy_rows(nb)


def test_snapshot_copy_rows_raises_key_error_not_index_error_when_the_row_is_gone(
    tmp_path, monkeypatch,
):
    """finalize's own DELETE already ran (the row is physically gone, not
    merely non-live) — the caller's ``snapshot["notebooks"][0]`` used to
    bare-index an empty list here. Must surface as KeyError (the route's
    existing ``except KeyError: 404``), never an IndexError (uncaught → 500)."""
    _env(monkeypatch, tmp_path)
    repo = SQLiteRepository(Settings())
    nb = _mk_nb(repo)
    store = repo._runtime.sharing_store
    with repo._write() as db:
        db.execute("DELETE FROM notebooks WHERE id=?", (nb,))
    with pytest.raises(KeyError):
        store.snapshot_copy_rows(nb)


def test_copy_notebook_propagates_key_error_for_a_tombstoned_source(tmp_path, monkeypatch):
    """The KeyError raised inside snapshot_copy_rows must survive
    copy_notebook's own except/compensate/raise wrapping unchanged — this is
    what gives the share route's existing ``except KeyError: 404`` something
    to catch.

    ``NotebookCopyService.copy_notebook`` ALREADY calls ``self._catalog.
    get_notebook(source_notebook_id)`` (liveness-filtered since well before
    R14 — the "目录寻址已经把 deleting/copying 挡在入口" gate R11's own
    docstring names) before it ever reaches ``snapshot_copy_rows`` — so
    simply flipping the DB status and calling copy_notebook would only
    re-prove THAT pre-existing gate, not the new one. ``get_notebook`` is
    monkeypatched to let a stale/faked summary through regardless of real
    DB status, isolating the race R14 actually targets: the tombstone
    landing strictly AFTER that check passed but before snapshot_copy_rows
    reads the root row for itself."""
    from types import SimpleNamespace
    from app.services.notebook_catalog import NotebookCatalogService

    _env(monkeypatch, tmp_path)
    repo = SQLiteRepository(Settings())
    src = _seed_full_notebook(repo, owner="user-local")
    monkeypatch.setattr(
        NotebookCatalogService, "get_notebook",
        lambda self, nb_id: SimpleNamespace(name="stale-summary", status="ready"),
    )
    with repo._write() as db:
        db.execute("UPDATE notebooks SET status='deleting' WHERE id=?", (src,))
    with pytest.raises(KeyError):
        repo._runtime.sharing.copy_notebook(src, new_owner_id="user-local")


def test_copy_stats_reports_snapshot_exceeding_notebook_non_copyable(tmp_path, monkeypatch):
    """codex PR#354 r1 P2: a notebook under the byte + chunk+node limits but over
    the total-materialisation limit must read NON-copyable (share-routing), so the
    UI routes it to read-only join instead of a copy that dead-ends at the guard's
    409 (recipient could then neither copy nor join-as-large — stuck). The share
    service's notebook_copy_stats (what notebook_sharing_repository() returns, and
    the copy/join routes call) applies the SAME snapshot-row criterion the guard
    does. Dropping its recheck lets B read copyable (the stuck-recipient bug)."""
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("NOTEBOOK_COPY_MAX_ROWS", "1000")        # chunks+nodes gate: generous
    monkeypatch.setenv("NOTEBOOK_COPY_MAX_SNAPSHOT_ROWS", "10")  # total gate: tight
    repo = SQLiteRepository(Settings())
    # A: 2 nodes + 3 relations → 1 nb + 2 + 3 = 6 rows ≤ 10 → copyable
    a = _mk_nb(repo); _seed_nodes(repo, a, 2); _seed_relations(repo, a, 3)
    assert repo._runtime.sharing.notebook_copy_stats(a)["copyable"] is True
    # B: same 2 nodes (passes chunks+nodes gate) but 20 relations → total 23 > 10.
    # Relations aren't counted by chunks+nodes, so ONLY the snapshot term rejects it.
    b = _mk_nb(repo); _seed_nodes(repo, b, 2); _seed_relations(repo, b, 20)
    assert repo._runtime.sharing.notebook_copy_stats(b)["copyable"] is False


def _seed_assets(repo, nb, n):
    with repo._write() as db:
        for i in range(n):
            db.execute(
                "INSERT INTO notebook_assets "
                "(id, notebook_id, filename, mime, size, created_by, created_at, source_id) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"asset-{nb}-{i}", nb, f"f{i}.png", "image/png", 1, "user-local", _now(), None))


def test_notebook_copy_stats_rechecks_snapshot_bound_fresh_not_stale(tmp_path, monkeypatch):
    """codex PR#354 r2 P2: notebook_assets (and sources/paper_meta) grow WITHOUT
    bumping the KG-version key that scale_artifacts' copy_stats caches on. The
    share-routing copyability must re-check the total-materialisation bound FRESH
    (at the facade), so a notebook that crosses the bound via such an untracked
    table stops reading copyable even with NO intervening KG mutation — otherwise a
    stale cached 'copyable' offers a copy that 409s while join rejects it as small
    (the dead end). Dropping the fresh facade recheck keeps the stale True here."""
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("NOTEBOOK_COPY_MAX_ROWS", "1000")
    monkeypatch.setenv("NOTEBOOK_COPY_MAX_SNAPSHOT_ROWS", "5")
    repo = SQLiteRepository(Settings())
    nb = _mk_nb(repo)
    _seed_nodes(repo, nb, 2)                                    # 1 nb + 2 nodes = 3 ≤ 5 → copyable
    assert repo._runtime.sharing.notebook_copy_stats(nb)["copyable"] is True   # warms the cached copy_stats
    _seed_assets(repo, nb, 10)                                  # assets DON'T bump kg_mutation_seq…
    # …so the cached verdict is unchanged, but the FRESH facade recheck counts
    # 1 nb + 2 nodes + 10 assets = 13 > 5 → non-copyable.
    assert repo._runtime.sharing.notebook_copy_stats(nb)["copyable"] is False


def test_share_sets_token_idempotent_then_unshare_clears(repo):
    nb = _mk_nb(repo, "L")
    out = repo.share_notebook(nb)
    assert out["share_token"].startswith("shr-")
    assert repo.find_notebook_by_share_token(out["share_token"]) == nb
    # 幂等:再分享返回同一个 token
    assert repo.share_notebook(nb)["share_token"] == out["share_token"]
    # 取消 → token 失效
    repo.unshare_notebook(nb)
    assert repo.find_notebook_by_share_token(out["share_token"]) is None


def test_remap_json_ids_scalars_and_arrays():
    from app.services.sqlite_repository import _remap_json_ids
    # 生产里 copy_notebook 对 element_id / element_ids 传的是同一个 emap,故这里
    # 两个键共用同一份 element 映射(el-1→el-A, el-2→el-B),与真实调用一致。
    el_map = {"el-1": "el-A", "el-2": "el-B"}
    maps = {"element_id": el_map, "element_ids": el_map,
            "source_id": {"src-1": "src-A"}, "object_id": {"ko-1": "ko-A"}}
    payload = {
        "source_id": "src-1",
        "steps": [{"element_id": "el-1", "quote": "keep me"}],
        "evidence": [{"element_id": "el-2", "source_id": "src-1", "quoted_span": "keep"}],
        "element_ids": ["el-1", "el-2", "el-unknown"],
        "note": "untouched",
    }
    out = _remap_json_ids(payload, maps)
    assert out["source_id"] == "src-A"
    assert out["steps"][0]["element_id"] == "el-A"
    assert out["steps"][0]["quote"] == "keep me"
    assert out["evidence"][0]["element_id"] == "el-B"
    assert out["evidence"][0]["source_id"] == "src-A"
    assert out["element_ids"] == ["el-A", "el-B", "el-unknown"]  # 未命中的原样
    assert out["note"] == "untouched"


def _seed_full_notebook(repo, owner="user-local"):
    """种一个各表都有数据、且含交叉引用的小 notebook,返回 nb_id。"""
    import json, uuid
    from app.services.sqlite_repository import _now
    now = _now()
    nb = f"nb-{uuid.uuid4().hex[:10]}"; s = f"src-{uuid.uuid4().hex[:6]}"
    e1 = f"el-{uuid.uuid4().hex[:6]}"; c1 = f"ck-{uuid.uuid4().hex[:6]}"
    o1 = f"ko-{uuid.uuid4().hex[:6]}"; o2 = f"ko-{uuid.uuid4().hex[:6]}"
    r1 = f"rel-{uuid.uuid4().hex[:6]}"
    with repo._write() as db:
        db.execute("INSERT INTO notebooks (id,name,purpose,primary_domain,status,created_by,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?)", (nb,"Orig","","Semiconductor","draft",owner,now,now))
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,file_size,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", (s,nb,"S","document","s.md","",10,now,now))
        db.execute("INSERT INTO source_elements (id,source_id,element_type,location_label,text,created_at) "
                   "VALUES (?,?,?,?,?,?)", (e1,s,"para","p1","hello",now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?)", (c1,nb,s,"chunk txt",json.dumps([e1]),now))
        db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                   (c1,nb,json.dumps([0.1,0.2]),now))
        for o in (o1,o2):
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,source_id,payload,evidence,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (o,nb,"concept",s,json.dumps({"name":"x"}),json.dumps([{"element_id":e1,"source_id":s}]),now,now))
            db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                       (o,nb,json.dumps([0.3]),now))
        db.execute("INSERT INTO knowledge_relations (id,notebook_id,source_id,source_object_id,target_object_id,edge_type,evidence,created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)", (r1,nb,s,o1,o2,"rel",json.dumps([{"element_id":e1}]),now))
        db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,created_at) "
                   "VALUES (?,?,?,?,?,?)", (f"cl-{uuid.uuid4().hex[:6]}",nb,o1,o1,"x",now))
        # 一个不该被拷贝的对话
        db.execute("INSERT INTO conversations (id,notebook_id,title,created_by,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?)", (f"cv-{uuid.uuid4().hex[:6]}",nb,"chat",owner,now,now))
    return nb


def _mk_user(repo, uid, email=None):
    """建一个真实 users 行(notebooks.created_by 有 FK→users.id,拷贝目标用户须存在)。"""
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO users (id,email,display_name,role,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)", (uid, email or f"{uid}@e.test", uid, "user", now, now))
    return uid


def test_copy_notebook_deep_copies_and_remaps(repo):
    src = _seed_full_notebook(repo)
    with repo._write() as db:
        db.execute(
            "UPDATE sources SET uploaded_by='user-local' WHERE notebook_id=?",
            (src,),
        )
    _mk_user(repo, "user-bob")  # created_by FK→users.id,目标用户须先存在(生产里恒成立)
    new = repo.copy_notebook(src, new_owner_id="user-bob")
    assert new.id != src and new.tier == "personal"
    with repo._connect() as db:
        assert db.execute("SELECT created_by FROM notebooks WHERE id=?", (new.id,)).fetchone()[0] == "user-bob"
        assert db.execute("SELECT is_shared,share_token FROM notebooks WHERE id=?", (new.id,)).fetchone()[0] == 0
    # 行数一致
    for t in ("sources","chunks","knowledge_objects","knowledge_relations","concept_clusters"):
        assert len(_rows(repo, t, new.id)) == len(_rows(repo, t, src)), t
    # 关系指向副本内 objects(无悬空)
    with repo._connect() as db:
        obj_ids = {r["id"] for r in _rows(repo, "knowledge_objects", new.id)}
        rel = _rows(repo, "knowledge_relations", new.id)[0]
        assert rel["source_object_id"] in obj_ids and rel["target_object_id"] in obj_ids
        # chunk.element_ids 已重写到副本 element
        import json
        new_elem_ids = {r["id"] for r in db.execute(
            "SELECT se.id FROM source_elements se JOIN sources s ON s.id=se.source_id WHERE s.notebook_id=?", (new.id,))}
        ck = _rows(repo, "chunks", new.id)[0]
        assert json.loads(ck["element_ids"])[0] in new_elem_ids
        # evidence.element_id 已重写
        ev = json.loads(_rows(repo, "knowledge_objects", new.id)[0]["evidence"])
        assert ev[0]["element_id"] in new_elem_ids
        assert db.execute(
            "SELECT uploaded_by FROM sources WHERE notebook_id=?", (new.id,)
        ).fetchone()[0] is None
    # conversations 不被拷贝
    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM conversations WHERE notebook_id=?", (new.id,)).fetchone()[0] == 0
    # 原库不受影响
    assert len(_rows(repo, "knowledge_objects", src)) == 2


def test_copy_notebook_remaps_source_local_facts_and_element_bindings(repo):
    src = _seed_full_notebook(repo)
    _mk_user(repo, "user-fact-copy")
    now = _now()
    with repo._write() as db:
        source_id = db.execute(
            "SELECT id FROM sources WHERE notebook_id=?", (src,)
        ).fetchone()[0]
        element_id = db.execute(
            "SELECT id FROM source_elements WHERE source_id=?", (source_id,)
        ).fetchone()[0]
        object_id = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=? ORDER BY id LIMIT 1",
            (src,),
        ).fetchone()[0]
        fact_id = "ksf-copy-source"
        evidence = json.dumps([{"source_id": source_id, "element_id": element_id}])
        db.execute(
            "INSERT INTO knowledge_source_facts "
            "(id,notebook_id,source_id,source_generation,local_object_id,"
            "global_object_id,object_type,payload,evidence,projection_version,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fact_id, src, source_id, "run-copy", "local-copy", object_id,
                "concept", json.dumps({"name": "copy fact"}), evidence, 1, now, now,
            ),
        )
        db.execute(
            "INSERT INTO knowledge_source_fact_elements "
            "(fact_id,notebook_id,source_id,source_generation,element_id,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (fact_id, src, source_id, "run-copy", element_id, now),
        )
        db.execute(
            "INSERT INTO knowledge_source_fact_backfills "
            "(source_id,notebook_id,source_generation,projection_version,status,"
            "after_object_id,objects_scanned,facts_written,incomplete_objects,"
            "failure_code,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, src, "run-copy", 1, "complete", object_id,
             1, 1, 0, "", now, now),
        )

    copied = repo.copy_notebook(src, new_owner_id="user-fact-copy")
    with repo._connect() as db:
        fact = db.execute(
            "SELECT * FROM knowledge_source_facts WHERE notebook_id=?", (copied.id,)
        ).fetchone()
        binding = db.execute(
            "SELECT * FROM knowledge_source_fact_elements WHERE notebook_id=?",
            (copied.id,),
        ).fetchone()
        backfill = db.execute(
            "SELECT * FROM knowledge_source_fact_backfills WHERE notebook_id=?",
            (copied.id,),
        ).fetchone()
        copied_object_ids = {
            row[0] for row in db.execute(
                "SELECT id FROM knowledge_objects WHERE notebook_id=?", (copied.id,)
            ).fetchall()
        }
        copied_element_ids = {
            row[0] for row in db.execute(
                "SELECT se.id FROM source_elements se JOIN sources s ON s.id=se.source_id "
                "WHERE s.notebook_id=?", (copied.id,)
            ).fetchall()
        }
        copied_run = db.execute(
            "SELECT * FROM extraction_runs WHERE notebook_id=? AND source_id=? "
            "AND run_type='kg' ORDER BY created_at DESC,id DESC LIMIT 1",
            (copied.id, fact["source_id"]),
        ).fetchone()
    assert fact["id"] != fact_id
    assert fact["projection_origin"] == "live"
    assert fact["global_object_id"] in copied_object_ids
    assert binding["fact_id"] == fact["id"]
    assert binding["element_id"] in copied_element_ids
    assert copied_run is not None
    assert copied_run["id"] == fact["source_generation"]
    assert copied_run["id"] == binding["source_generation"]
    assert copied_run["status"] == "completed"
    assert copied_run["error_message"].startswith("kg objects=")
    assert backfill["incomplete_reason"] == ""
    assert backfill["source_id"] == fact["source_id"]
    assert backfill["source_generation"] == copied_run["id"]
    assert backfill["after_object_id"] == fact["global_object_id"]

    repair = repo.maintenance.backfill_source_fact_batch(
        copied.id, fact["source_id"], force=True
    )
    assert repair["status"] != "no_generation"


def test_copy_notebook_clears_memory_id(repo):
    """Task 6: Memory 记录本身 owner 私有,不随 notebook 深拷贝走——副本源退化为
    普通内容源。若原样搬 memory_id 会悬空引用接收方看不到的 Memory,原 owner 再拷
    一次时还会撞 idx_sources_memory_id 分区唯一索引(两行同一 memory_id)。"""
    nb = _mk_nb(repo, "MemSrc")
    store = repo._runtime.source_store
    store.insert_source(
        source_id="src-mem-1",
        notebook_id=nb,
        title="Memory: foo",
        source_type="memory",
        status="ready",
        parse_status="ready",
        file_name="",
        file_path="",
        file_size=0,
        file_hash="",
        summary="",
        doc_type="memory",
        memory_id="mem-x",
    )
    _mk_user(repo, "user-memcopy")
    new = repo.copy_notebook(nb, new_owner_id="user-memcopy")
    copied = _rows(repo, "sources", new.id)
    assert len(copied) == 1
    row = copied[0]
    assert not row["memory_id"]  # 清空,而非原样带走 'mem-x'
    assert row["title"] == "Memory: foo"  # 其余字段完整
    assert row["source_type"] == "memory"
    assert row["doc_type"] == "memory"
    # 原库不受影响
    orig = _rows(repo, "sources", nb)[0]
    assert orig["memory_id"] == "mem-x"


def test_copy_notebook_twice_no_memory_id_unique_index_collision(repo):
    """同一 owner 对同一源库连拷两次,历史上不会撞 idx_sources_memory_id——两份
    副本的 memory_id 都已清空为空串,分区唯一索引豁免空值(WHERE memory_id IS NOT
    NULL AND memory_id != '')。"""
    nb = _mk_nb(repo, "MemSrc2")
    store = repo._runtime.source_store
    store.insert_source(
        source_id="src-mem-2",
        notebook_id=nb,
        title="Memory: bar",
        source_type="memory",
        status="ready",
        parse_status="ready",
        file_name="",
        file_path="",
        file_size=0,
        file_hash="",
        summary="",
        doc_type="memory",
        memory_id="mem-y",
    )
    _mk_user(repo, "user-memcopy2")
    new1 = repo.copy_notebook(nb, new_owner_id="user-memcopy2")
    new2 = repo.copy_notebook(nb, new_owner_id="user-memcopy2")
    assert new1.id != new2.id
    for nid in (new1.id, new2.id):
        assert not _rows(repo, "sources", nid)[0]["memory_id"]


def test_share_preview_copy_end_to_end(repo, client):
    src = _seed_full_notebook(repo, owner="user-local")  # 归 seeded admin(=API 调用者)
    # 分享
    r = client.post(f"/api/notebooks/{src}/share"); assert r.status_code == 200
    token = r.json()["share_token"]; assert r.json()["copyable"] is True
    # 预览
    p = client.get(f"/api/shared/{token}"); assert p.status_code == 200
    assert p.json()["mode"] == "copy" and p.json()["source_count"] == 1
    # 拷贝
    c = client.post(f"/api/shared/{token}/copy"); assert c.status_code == 200
    new_id = c.json()["id"]; assert new_id != src
    assert len(_rows(repo, "knowledge_objects", new_id)) == 2
    # 取消分享 → 预览/拷贝 404
    assert client.delete(f"/api/notebooks/{src}/share").status_code == 204
    assert client.get(f"/api/shared/{token}").status_code == 404
    assert client.post(f"/api/shared/{token}/copy").status_code == 404


def test_copy_route_maps_race_size_guard_to_409_not_500(repo, client, monkeypatch):
    """OOM audit P2-7 (codex PR#353): if a notebook crosses the copy-size
    threshold between the route's pre-check and copy_notebook's defense-in-depth
    recheck (concurrent ingestion), the route must keep its documented 409 — not
    surface the guard's exception as a 500. Reverting the route's catch → 500."""
    from app.services import notebook_sharing as ns
    src = _seed_full_notebook(repo, owner="user-local")
    token = client.post(f"/api/notebooks/{src}/share").json()["share_token"]
    # simulate the race: the route's pre-check sees copyable, the deep-copy
    # defense-in-depth recheck rejects.
    monkeypatch.setattr(
        ns.NotebookSharingService, "notebook_copy_stats",
        lambda self, nb: {"copyable": True},
    )

    def _raise(
        self, source_notebook_id, *, new_owner_id, actor_label=None, new_name=None
    ):
        raise ns.NotebookTooLargeToCopyError("raced past the threshold")

    monkeypatch.setattr(ns.NotebookSharingService, "copy_notebook", _raise)
    assert client.post(f"/api/shared/{token}/copy").status_code == 409


def test_copy_route_maps_a_tombstone_after_token_resolution_to_404_not_500(
    repo, client, monkeypatch,
):
    """codex #659 R14 P1: find_notebook_by_share_token (R11) already filters
    a non-live notebook, closing the DOMINANT race (tombstone already landed
    when the request starts). This pins the NARROWER window: the tombstone
    lands strictly AFTER token resolution succeeds AND after copy_notebook's
    own pre-existing ``get_notebook`` liveness gate passes, but before
    snapshot_copy_rows reaches the root row itself — both earlier gates are
    monkeypatched through so only the NEW one is under test. Must 404 — the
    route's existing ``except KeyError`` catching snapshot_copy_rows' new
    raise, not an uncaught IndexError surfacing as 500."""
    from types import SimpleNamespace
    from app.services import notebook_sharing as ns
    from app.services.notebook_catalog import NotebookCatalogService

    src = _seed_full_notebook(repo, owner="user-local")
    token = client.post(f"/api/notebooks/{src}/share").json()["share_token"]
    monkeypatch.setattr(
        ns.NotebookSharingService, "find_notebook_by_share_token",
        lambda self, tok: src,
    )
    # Bypass the pre-existing gate ONLY for the source id — a successful
    # copy (possible if the NEW gate is the one under mutation) still needs
    # a REAL get_notebook(new_id) for the route's response body, so a
    # blanket bypass would misreport a different failure (response
    # serialization) as this test's signal instead of "not 404".
    original_get_notebook = NotebookCatalogService.get_notebook

    def _bypass_for_source_only(self, nb_id):
        if nb_id == src:
            return SimpleNamespace(name="stale-summary", status="ready")
        return original_get_notebook(self, nb_id)

    monkeypatch.setattr(NotebookCatalogService, "get_notebook", _bypass_for_source_only)
    with repo._write() as db:
        db.execute("UPDATE notebooks SET status='deleting' WHERE id=?", (src,))
    assert client.post(f"/api/shared/{token}/copy").status_code == 404


def test_share_preview_count_is_visible_while_copy_size_stays_physical(repo):
    nb = _mk_nb(repo, "Mixed")
    now = _now()
    with repo._write() as db:
        for source_id, source_type in (
            ("s-uploaded", "document"),
            ("s-memory", "memory"),
            ("s-knowhow", "knowhow"),
        ):
            db.execute(
                "INSERT INTO sources "
                "(id,notebook_id,title,source_type,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    source_id,
                    nb,
                    source_id,
                    source_type,
                    "ready",
                    now,
                    now,
                ),
            )

    preview = repo.shared_preview(nb)
    assert preview["source_count"] == 1
    assert preview["size"]["sources"] == 3


def test_copy_refuses_too_large(repo, tmp_path, monkeypatch):
    # ⚠ 不能用 `client` fixture:它在测试体执行前就已 resolve(fixture 先于测试体运行),
    # 而 `from app.main import app` 只在本进程"首次"导入 app.main 时才跑 create_app()→
    # get_settings()(lru_cache,模块级单例、只执行一次)。若本测试恰是本进程第一个用到
    # client 的测试(单跑 / -k 子串命中如 "fuse" 恰好只选中本测试时),fixture 阶段就把
    # NOTEBOOK_COPY_MAX_ROWS=5000(默认值)缓存进 get_settings(),
    # 随后测试体里再 setenv 已经来不及——deps.repository() 读到的仍是旧缓存,
    # notebook_copy_stats() 判定「可拷贝」而非期望的「过大」。
    # 全量跑之所以侥幸通过：更早的测试已经导入过 app.main,create_app() 早跑完了,
    # 本测试的 setenv 发生在 repository() 真正首次调用之前,恰好读到新值——顺序耦合。
    # 修法(与 test_notebook_share_readonly.py::test_join_large_then_leave 一致的写法):
    # 在任何 HTTP 请求(即任何 client/app 构建)之前先设好阈值,再在测试体内现建 client。
    monkeypatch.setenv("NOTEBOOK_COPY_MAX_ROWS", "1")
    _env(monkeypatch, tmp_path)
    from app.main import app
    client = TestClient(app)
    src = _seed_full_notebook(repo, owner="user-local")
    token = client.post(f"/api/notebooks/{src}/share").json()["share_token"]
    # Phase 2:大库预览 mode 由 "too_large" 改为 "readonly"(改走只读共享而非拒绝)。
    assert client.get(f"/api/shared/{token}").json()["mode"] == "readonly"
    assert client.post(f"/api/shared/{token}/copy").status_code == 409


def test_non_owner_cannot_share(repo, client):
    # 造一个属于别人的库;当前用户(seeded admin=user-local)不是 owner。
    # created_by 有 FK→users.id,故先建出这个 owner 用户(生产里 owner 恒存在)。
    _mk_user(repo, "user-someone-else")
    other = _seed_full_notebook(repo, owner="user-someone-else")
    assert client.post(f"/api/notebooks/{other}/share").status_code == 404  # 不泄露存在性


def test_copy_appears_in_copier_list_and_original_untouched(repo, client):
    src = _seed_full_notebook(repo, owner="user-local")
    token = client.post(f"/api/notebooks/{src}/share").json()["share_token"]
    new_id = client.post(f"/api/shared/{token}/copy").json()["id"]
    ids = {n["id"] for n in client.get("/api/notebooks").json()}
    assert new_id in ids and src in ids  # copier==admin 两个都在
    # 原库对象数不变
    assert len(_rows(repo, "knowledge_objects", src)) == 2


def _seed_many_chunks_notebook(repo, n, owner="user-local"):
    """种一个有 n 个 chunk(各自一个 source)+ n 个 knowledge_object 的 notebook,
    用于跨块(chunk-boundary)拷贝行为测试。返回 nb_id。"""
    now = _now()
    nb = f"nb-{uuid.uuid4().hex[:10]}"
    with repo._write() as db:
        db.execute("INSERT INTO notebooks (id,name,purpose,primary_domain,status,created_by,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?)", (nb, "Many", "", "Semiconductor", "draft", owner, now, now))
        for i in range(n):
            s = f"src-{uuid.uuid4().hex[:8]}"
            c = f"ck-{uuid.uuid4().hex[:8]}"
            o = f"ko-{uuid.uuid4().hex[:8]}"
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,file_size,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?)", (s, nb, f"S{i}", "document", "s.md", "", 1, now, now))
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?)", (c, nb, s, f"chunk {i}", "[]", now))
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,source_id,payload,evidence,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (o, nb, "concept", s, json.dumps({"name": f"x{i}"}), "[]", now, now))
            db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                       (o, nb, json.dumps([0.1]), now))
    return nb


def test_copy_notebook_chunked_transactions_release_lock_between_chunks(repo, monkeypatch):
    """P1-4: copy_notebook must not hold the global write lock for the entire
    copy. With a small per-table chunk size and a multi-chunk source notebook,
    another thread must be able to acquire repo._write_lock WHILE the copy is
    still in progress (i.e. the lock is released between chunks)."""
    import app.services.sqlite_repository as sr
    import threading

    src = _seed_many_chunks_notebook(repo, 5)
    _mk_user(repo, "user-chunklock")
    monkeypatch.setattr(sr, "_COPY_CHUNK", 1)  # force many chunk boundaries

    acquired = threading.Event()
    release = threading.Event()
    orig_insert_row = repo._insert_row

    calls = {"n": 0}

    def spy_insert_row(db, table, d):
        calls["n"] += 1
        # After a few inserts (i.e. mid-copy, across several chunk
        # transactions), give the other thread a chance to grab the lock.
        if calls["n"] == 3:
            release.set()
        return orig_insert_row(db, table, d)

    monkeypatch.setattr(repo, "_insert_row", spy_insert_row)

    def contender():
        release.wait(timeout=5)
        got = repo._write_lock.acquire(timeout=3)
        if got:
            acquired.set()
            repo._write_lock.release()

    t = threading.Thread(target=contender)
    t.start()
    repo.copy_notebook(src, new_owner_id="user-chunklock")
    t.join(timeout=5)
    assert acquired.is_set(), "contending thread never acquired _write_lock during copy -> lock held too long"


def test_copy_notebook_fts_contains_only_copied_ids(repo):
    """FTS rows for the copy must be inserted incrementally for the copied ids
    only (not a whole-table backfill that happens to look right)."""
    src = _seed_full_notebook(repo, owner="user-local")
    new = repo.copy_notebook(src, new_owner_id="user-local")
    with repo._connect() as db:
        src_obj_ids = {r["id"] for r in db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=?", (src,)).fetchall()}
        new_obj_ids = {r["id"] for r in db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=?", (new.id,)).fetchall()}
        fts_ids = {r["object_id"] for r in db.execute(
            "SELECT object_id FROM kg_objects_fts WHERE notebook_id=?", (new.id,)).fetchall()}
        chunk_fts_ids = {r["chunk_id"] for r in db.execute(
            "SELECT chunk_id FROM chunks_fts WHERE notebook_id=?", (new.id,)).fetchall()}
        new_chunk_ids = {r["id"] for r in db.execute(
            "SELECT id FROM chunks WHERE notebook_id=?", (new.id,)).fetchall()}
    assert fts_ids == new_obj_ids
    assert fts_ids.isdisjoint(src_obj_ids)
    assert chunk_fts_ids == new_chunk_ids


def test_copy_notebook_crash_midway_leaves_no_visible_notebook_and_self_heals(repo, monkeypatch):
    """A crash mid-copy must not leave a half-copied notebook visible to
    readers (list_notebooks / get_notebook). A subsequent copy_notebook call
    must self-heal (sweep the orphaned/stuck row) and succeed."""
    import app.services.sqlite_repository as sr

    src = _seed_many_chunks_notebook(repo, 3)
    _mk_user(repo, "user-crashtest")
    monkeypatch.setattr(sr, "_COPY_CHUNK", 1)

    calls = {"n": 0}
    orig_insert_row = repo._insert_row

    def boom_after_a_few(db, table, d):
        calls["n"] += 1
        if calls["n"] == 4:
            raise RuntimeError("simulated crash mid-copy")
        return orig_insert_row(db, table, d)

    monkeypatch.setattr(repo, "_insert_row", boom_after_a_few)
    with pytest.raises(RuntimeError):
        repo.copy_notebook(src, new_owner_id="user-crashtest")

    # The half-copied notebook must not be visible via get_notebook/list.
    with repo._connect() as db:
        stuck = db.execute(
            "SELECT id FROM notebooks WHERE created_by=? AND status='copying'",
            ("user-crashtest",)).fetchall()
        # Either the crash-sim rolled back its own partial insert cleanly
        # (no stuck row) or the row is left in the non-visible 'copying'
        # sentinel state — both satisfy "no visible half-copy". If a stuck
        # row exists, orphan child rows for it must exist (proving the FK
        # chain), i.e. this is genuinely the self-heal scenario.
        assert len(stuck) <= 1

    # Self-heal: a fresh copy attempt must succeed and produce a complete,
    # correct copy (proving the sweep did not corrupt state for a retry).
    new = repo.copy_notebook(src, new_owner_id="user-crashtest")
    assert len(_rows(repo, "knowledge_objects", new.id)) == 3
    with repo._connect() as db:
        # No leftover stuck 'copying' rows after the sweep + successful retry.
        leftover = db.execute(
            "SELECT COUNT(*) FROM notebooks WHERE created_by=? AND status='copying'",
            ("user-crashtest",)).fetchone()[0]
        assert leftover == 0
        # No orphaned knowledge_embeddings (the one table without a direct FK
        # to notebooks) pointing at a notebook_id that no longer exists.
        dangling_emb = db.execute(
            "SELECT COUNT(*) FROM knowledge_embeddings WHERE notebook_id NOT IN "
            "(SELECT id FROM notebooks)").fetchone()[0]
        assert dangling_emb == 0


def test_copy_sweeper_only_removes_expired_copy_jobs(repo):
    """A second copy must never delete another copy that is still active."""
    owner = "user-local"
    current_id = _mk_nb(repo, "active-copy", owner)
    expired_id = _mk_nb(repo, "expired-copy", owner)
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET status='copying', created_at=? WHERE id=?",
            (_now(), current_id),
        )
        db.execute(
            "UPDATE notebooks SET status='copying', created_at='2000-01-01T00:00:00' WHERE id=?",
            (expired_id,),
        )

    assert repo._sweep_stuck_copies(created_by=owner) == 1
    with repo._connect() as db:
        assert db.execute("SELECT 1 FROM notebooks WHERE id=?", (current_id,)).fetchone()
        assert db.execute("SELECT 1 FROM notebooks WHERE id=?", (expired_id,)).fetchone() is None


def test_public_notebook_update_rejects_internal_status():
    """The copy sentinel is an internal lifecycle state, never API input."""
    from pydantic import ValidationError
    from app.models.schemas import NotebookUpdate

    with pytest.raises(ValidationError):
        NotebookUpdate(status="copying")


def test_copy_skips_object_schemas_and_backfills_fts(repo):
    """B1 回归:源库有自定义 object_schema(object_type 全局唯一)时,拷贝不撞主键、不拷该表;
    I1:拷完 kg_objects_fts 已按副本重建(拷完即搜)。"""
    src = _seed_full_notebook(repo, owner="user-local")
    with repo._write() as db:
        db.execute(
            "INSERT INTO object_schemas (object_type,notebook_id,created_at,updated_at) VALUES (?,?,?,?)",
            ("customtype", src, _now(), _now()))
    new = repo.copy_notebook(src, new_owner_id="user-local")  # user-local 已 seed,满足 created_by FK
    with repo._connect() as db:
        # object_schemas 不随库拷(全局表;副本名下 0 行,不撞 UNIQUE)
        assert db.execute(
            "SELECT COUNT(*) FROM object_schemas WHERE notebook_id=?", (new.id,)).fetchone()[0] == 0
        # 词法搜索索引已按副本 backfill(源 2 个 name 非空对象 → 副本 2 行)
        assert db.execute(
            "SELECT COUNT(*) FROM kg_objects_fts WHERE notebook_id=?", (new.id,)).fetchone()[0] == 2


def test_copy_carries_notebook_schema_rows_and_reassigns_creator(repo):
    from app.models.knowledge import ObjectSchemaCreate
    from app.models.schemas import NotebookCreate

    _mk_user(repo, "copy-owner")
    source = repo.create_notebook(NotebookCreate(name="source"))
    repo.create_notebook_object_schema(
        source.id,
        ObjectSchemaCreate(
            object_type="lab_recipe",
            fields=["name", "steps"],
            primary="name",
            list_fields=["steps"],
            label="Recipe",
        ),
        created_by="user-local",
    )

    copied = repo.copy_notebook(
        source.id, new_owner_id="copy-owner", new_name="copied"
    )
    with repo._connect() as db:
        source_row = db.execute(
            "SELECT * FROM notebook_object_schemas "
            "WHERE notebook_id=? AND object_type='lab_recipe'",
            (source.id,),
        ).fetchone()
        copied_row = db.execute(
            "SELECT * FROM notebook_object_schemas "
            "WHERE notebook_id=? AND object_type='lab_recipe'",
            (copied.id,),
        ).fetchone()
    assert source_row is not None
    assert copied_row is not None
    assert copied_row["fields"] == source_row["fields"]
    assert copied_row["created_by"] == "copy-owner"

    repo.delete_notebook(copied.id)
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM notebook_object_schemas WHERE notebook_id=?",
            (copied.id,),
        ).fetchone()[0] == 0


def test_share_returns_transaction_token_if_concurrent_unshare_wins(repo, monkeypatch):
    nb = _mk_nb(repo, "race")
    store = repo._runtime.sharing_store
    original = store.set_share_token

    def set_then_unshare(notebook_id, candidate):
        selected = original(notebook_id, candidate)
        store.clear_share(notebook_id)
        return selected

    monkeypatch.setattr(store, "set_share_token", set_then_unshare)

    out = repo.share_notebook(nb)

    assert isinstance(out["share_token"], str)
    assert out["share_token"].startswith("shr-")
    assert repo.find_notebook_by_share_token(out["share_token"]) is None


# NOTE: appended at EOF on purpose — inserting mid-file shifts the by-line
# patch/consumer sites the repository_surface_manifest guard pins for the
# _COPY_CHUNK / _insert_row seams below this file's earlier tests.
def _seed_knowhow_into_notebook(repo, nb, owner="user-local"):
    """Seed a knowhow table (one real column+row+cell) into an existing
    notebook, plus its projected element/chunk(+embedding) and PR-1-vocabulary
    KOs+edge — mostly raw SQL (mirrors _seed_full_notebook's own style), but
    with REAL knowhow_columns/knowhow_rows rows underneath the element's
    metadata (PR-2+3 Task 13's copy resolves metadata.knowhow.{row_id,
    column_id} through khrow_map/khcol_map, built from the ACTUAL business
    rows — a bare source_type='knowhow' hidden source is no longer enough,
    unlike PR-1's blanket-exclusion era this fixture used to test). Returns
    (hidden_source_id, table_id). The knowledge_objects/knowledge_relations
    rows stay hand-rolled with PR-1's retired case/procedure/tool vocabulary
    on purpose — Task 13 still EXCLUDES them from the copy (rebuilt by
    project_table instead, see test_knowhow_copy.py for that contract), so
    their exact shape is irrelevant here, only their PRESENCE/count is."""
    # Deferred import (keeps this EOF-appended block's own line count from
    # perturbing any earlier test's pinned _COPY_CHUNK/_insert_row usage
    # line — see test_repository_surface_manifest.py's EXPECTED_PATCH_DELTAS;
    # a top-of-file import shifts EVERY later line by one).
    from app.services.knowhow.projection import cell_chunk_id

    now = _now()
    hs = f"src-kh-{uuid.uuid4().hex[:6]}"
    tbl = f"khtbl-{uuid.uuid4().hex[:6]}"
    col = f"khcol-{uuid.uuid4().hex[:6]}"
    row = f"khrow-{uuid.uuid4().hex[:6]}"
    cel = f"khcel-{uuid.uuid4().hex[:6]}"
    el = f"el-kh-{uuid.uuid4().hex[:8]}"
    # Real chunk-id shape (app.services.knowhow.projection.cell_chunk_id):
    # "chunk-kh-{row_hash}-{part}" — the trailing "-{part}" integer is what
    # copy_notebook's chunks_out loop parses back off with rsplit("-", 1), so
    # a fixture id missing it would ValueError there (caught by an earlier,
    # less realistic version of this fixture).
    ck = cell_chunk_id(row, 1)
    case = f"ko-kh-{uuid.uuid4().hex[:8]}"
    proc = f"ko-kh-{uuid.uuid4().hex[:8]}"
    tool = f"ko-kh-{uuid.uuid4().hex[:8]}"
    rel = f"kr-kh-{uuid.uuid4().hex[:8]}"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,parse_status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", (hs, nb, "Knowhow 表：时序修复", "knowhow", "parsed", now, now))
        db.execute("INSERT INTO knowhow_tables (id,notebook_id,title,hidden_source_id,created_by,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", (tbl, nb, "时序修复", hs, owner, now, now))
        db.execute("INSERT INTO knowhow_columns (id,table_id,name,role,position) VALUES (?,?,?,?,?)",
                   (col, tbl, "修复方法", "procedure", 0))
        db.execute("INSERT INTO knowhow_rows (id,table_id,position,created_at,updated_at) VALUES (?,?,?,?,?)",
                   (row, tbl, 0, now, now))
        db.execute("INSERT INTO knowhow_cells (id,row_id,column_id,content_md,updated_at) VALUES (?,?,?,?,?)",
                   (cel, row, col, "增加阻尼电阻", now))
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (el, hs, "knowhow_cell", "过冲问题 › 修复方法", "增加阻尼电阻",
             json.dumps({"knowhow": {
                 "table_id": tbl, "row_id": row, "column_id": col,
                 "role": "procedure", "column_name": "修复方法",
                 "row_title": "过冲问题", "content_hash": "deadbeef",
             }}), now),
        )
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?)", (ck, nb, hs, "增加阻尼电阻", json.dumps([el]), now))
        db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                   (ck, nb, json.dumps([0.9, 0.8]), now))
        for oid, otype, payload in (
            (case, "case", {"title": "过冲问题", "row_id": "r1"}),
            (proc, "procedure", {"name": "过冲问题·修复方法", "row_id": "r1"}),
            (tool, "tool", {"name": "示波器"}),
        ):
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,source_id,payload,evidence,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (oid, nb, otype, hs, json.dumps(payload), json.dumps([{"element_id": el, "source_id": hs}]), now, now))
        db.execute("INSERT INTO knowledge_relations (id,notebook_id,source_id,source_object_id,target_object_id,edge_type,evidence,created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)", (rel, nb, hs, case, proc, "fixed_by", json.dumps([]), now))
    return hs, tbl


def test_copy_notebook_includes_knowhow_content_with_remap(repo):
    """PR-2+3 Task 13 supersedes PR-1's blanket knowhow exclusion this test
    used to assert (see git history for the prior
    test_copy_notebook_excludes_all_knowhow_content): the hidden source, its
    elements/chunks(+vectors), and the five knowhow business tables now
    travel with a copy, ids fully remapped. knowledge_objects/knowledge_
    relations derived from a knowhow hidden source stay excluded either way
    (rebuilt by project_table, never copied directly — see
    test_knowhow_copy.py for the full zero-re-embed/dynamic-type contract);
    this test's job is only telling those two apart, not re-covering
    ordinary-content copy semantics (test_copy_notebook_deep_copies_and_remaps
    above already does that)."""
    src = _seed_full_notebook(repo)  # 1 normal source, 2 concept KOs, 1 chunk(+emb), 1 rel, 1 cluster
    hidden, table_id = _seed_knowhow_into_notebook(repo, src)  # + knowhow: 3 KOs, 1 chunk(+emb), 1 rel, 1 table+col+row+cell
    with repo._connect() as db:  # sanity: the ORIGINAL really has knowhow content
        assert db.execute("SELECT COUNT(*) FROM knowledge_objects WHERE source_id=?", (hidden,)).fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM knowhow_tables WHERE notebook_id=?", (src,)).fetchone()[0] == 1

    _mk_user(repo, "user-copykh")
    new = repo.copy_notebook(src, new_owner_id="user-copykh")  # must NOT raise

    with repo._connect() as db:
        # KOs/relations derived from the knowhow source stay excluded (still
        # rebuilt by project_table, never copied directly).
        assert db.execute("SELECT COUNT(*) FROM knowledge_objects WHERE notebook_id=? AND object_type IN ('case','procedure','tool')", (new.id,)).fetchone()[0] == 0
        # Everything else DOES travel now, remapped.
        assert db.execute("SELECT COUNT(*) FROM sources WHERE notebook_id=? AND source_type='knowhow'", (new.id,)).fetchone()[0] == 1
        new_table = db.execute(
            "SELECT id, hidden_source_id FROM knowhow_tables WHERE notebook_id=?", (new.id,)
        ).fetchone()
        assert new_table is not None
        assert new_table["id"] != table_id
        assert new_table["hidden_source_id"] not in (None, hidden)
        new_hidden = new_table["hidden_source_id"]
        new_chunks = db.execute(
            "SELECT id, text FROM chunks WHERE source_id=?", (new_hidden,)
        ).fetchall()
        assert len(new_chunks) == 1
        assert new_chunks[0]["text"] == "增加阻尼电阻"
        assert db.execute(
            "SELECT COUNT(*) FROM chunk_embeddings WHERE chunk_id=?", (new_chunks[0]["id"],)
        ).fetchone()[0] == 1
        # The ordinary (non-knowhow) content copied intact.
        assert db.execute("SELECT COUNT(*) FROM sources WHERE notebook_id=? AND source_type!='knowhow'", (new.id,)).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM knowledge_objects WHERE notebook_id=?", (new.id,)).fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM chunks WHERE notebook_id=?", (new.id,)).fetchone()[0] == 2  # 1 ordinary + 1 knowhow
    # Original untouched: knowhow table + hidden source + its 3 KOs still present.
    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM sources WHERE notebook_id=? AND source_type='knowhow'", (src,)).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM knowledge_objects WHERE source_id=?", (hidden,)).fetchone()[0] == 3


# NOTE: appended at EOF for the same line-pin reason as the block above.
def test_copy_notebook_carries_paper_metadata(repo):
    """Final-review Fix 1 (merge blocker): notebook deep-copy must carry
    source_paper_meta + source_authors alongside the source they describe —
    without this a copied notebook silently loses author search + paper
    display, and re-extraction would re-spend LLM calls (efficiency
    mandate). Mirrors the established per-table copy pattern: source_id
    remapped via the existing source map, notebook_id -> new notebook,
    source_authors.id regenerated with the same deterministic
    f"{source_id}:auth:{position:03d}" scheme upsert_paper_meta uses at
    write time, every other column carried verbatim."""
    src = _seed_full_notebook(repo)
    store = repo._runtime.source_store
    orig_source_id = _rows(repo, "sources", src)[0]["id"]
    store.upsert_paper_meta(orig_source_id, src, {
        "is_paper": True,
        "paper_title": "Gate Sizing Under Variability",
        "venue": "DAC",
        "pub_year": 2025,
        "doi": "10.1234/abcd.5678",
        "keywords": ["timing", "variability"],
        "authors": [
            {"position": 0, "name": "Chen Hao", "affiliation": "Fudan University"},
            {"position": 1, "name": "Li Wei", "affiliation": "Tsinghua University; SJTU"},
        ],
        "model": "fake-kg",
        "raw_json": json.dumps({"llm": {"is_paper": True}, "dropped": {}}),
    })

    _mk_user(repo, "user-papercopy")
    new = repo.copy_notebook(src, new_owner_id="user-papercopy")

    new_source_id = _rows(repo, "sources", new.id)[0]["id"]
    assert new_source_id != orig_source_id
    copied_meta = repo.get_paper_meta(new_source_id)
    assert copied_meta is not None
    assert copied_meta["paper_title"] == "Gate Sizing Under Variability"
    assert copied_meta["venue"] == "DAC"
    assert copied_meta["pub_year"] == 2025
    assert copied_meta["doi"] == "10.1234/abcd.5678"
    assert copied_meta["keywords"] == ["timing", "variability"]
    assert copied_meta["model"] == "fake-kg"
    assert [a["name"] for a in copied_meta["authors"]] == ["Chen Hao", "Li Wei"]
    assert copied_meta["authors"][0]["affiliation"] == "Fudan University"
    assert copied_meta["authors"][1]["affiliation"] == "Tsinghua University; SJTU"

    # author row ids follow the new source id's deterministic scheme (not the
    # original source id's), and raw_json/model/created_at carry verbatim.
    with repo._connect() as db:
        author_ids = [r["id"] for r in db.execute(
            "SELECT id FROM source_authors WHERE source_id=? ORDER BY position",
            (new_source_id,),
        ).fetchall()]
        orig_row = db.execute(
            "SELECT raw_json, model, created_at FROM source_paper_meta WHERE source_id=?",
            (orig_source_id,),
        ).fetchone()
        new_row = db.execute(
            "SELECT raw_json, model, created_at FROM source_paper_meta WHERE source_id=?",
            (new_source_id,),
        ).fetchone()
    assert author_ids == [f"{new_source_id}:auth:000", f"{new_source_id}:auth:001"]
    assert new_row["raw_json"] == orig_row["raw_json"]
    assert new_row["model"] == orig_row["model"]
    assert new_row["created_at"] == orig_row["created_at"]

    # author-name search hits the copy via list_sources_page(q=...)
    page = repo.list_sources_page(new.id, q="Chen Hao")
    assert any(s.id == new_source_id for s in page.items)

    # original untouched
    orig_meta = repo.get_paper_meta(orig_source_id)
    assert orig_meta is not None
    assert orig_meta["paper_title"] == "Gate Sizing Under Variability"
    assert [a["name"] for a in orig_meta["authors"]] == ["Chen Hao", "Li Wei"]
    with repo._connect() as db:
        orig_author_ids = [r["id"] for r in db.execute(
            "SELECT id FROM source_authors WHERE source_id=? ORDER BY position",
            (orig_source_id,),
        ).fetchall()]
    assert orig_author_ids == [f"{orig_source_id}:auth:000", f"{orig_source_id}:auth:001"]


def test_copy_notebook_remaps_generated_questions_to_original_chunk(repo):
    src = _seed_full_notebook(repo)
    original_chunk = _rows(repo, "chunks", src)[0]
    original_source = _rows(repo, "sources", src)[0]
    repo._runtime.chunk_store.replace_chunk_questions(
        original_chunk["id"],
        src,
        original_source["id"],
        (("cq-original", "Which timing constraint applies?", [0.25] * 8),),
        created_at=_now(),
    )

    _mk_user(repo, "user-question-copy")
    copied = repo.copy_notebook(src, new_owner_id="user-question-copy")

    copied_question = _rows(repo, "chunk_questions", copied.id)[0]
    copied_chunk = _rows(repo, "chunks", copied.id)[0]
    copied_source = _rows(repo, "sources", copied.id)[0]
    assert copied_question["id"] != "cq-original"
    assert copied_question["question"] == "Which timing constraint applies?"
    assert copied_question["chunk_id"] == copied_chunk["id"]
    assert copied_question["source_id"] == copied_source["id"]
    assert copied_chunk["question_indexed_at"]

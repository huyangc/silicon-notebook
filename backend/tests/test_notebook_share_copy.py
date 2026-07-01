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


def test_notebooks_has_share_columns(repo):
    with repo._connect() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(notebooks)")}
    assert "is_shared" in cols
    assert "share_token" in cols


def test_copy_thresholds_defaults():
    from app.core.config import Settings
    s = Settings()
    assert s.notebook_copy_max_bytes == 50 * 1024 * 1024
    assert s.notebook_copy_max_rows == 5000


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


def test_copy_stats_reports_size_and_copyable(repo):
    nb = _mk_nb(repo, "L")
    stats = repo.notebook_copy_stats(nb)
    assert stats["copyable"] is True          # 空库当然可拷贝
    assert set(stats["size"]) == {"bytes", "sources", "chunks", "nodes", "edges"}


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
    # conversations 不被拷贝
    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM conversations WHERE notebook_id=?", (new.id,)).fetchone()[0] == 0
    # 原库不受影响
    assert len(_rows(repo, "knowledge_objects", src)) == 2


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


def test_copy_refuses_too_large(repo, client, monkeypatch):
    # 首个 client 请求才触发 repository() 重建(conftest 已清缓存),此时读到 =1
    monkeypatch.setenv("NOTEBOOK_COPY_MAX_ROWS", "1")
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

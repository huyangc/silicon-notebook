import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    return SQLiteRepository(Settings())


def _seed(repo):
    now = "2026-07-07T00:00:00"
    with repo._write() as db:
        # 两个用户(notebooks.created_by 是 FK→users(id),必须先建用户)
        for uid, uname in (("u1", "a00000001"), ("u2", "b00000002")):
            db.execute(
                "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (uid, f"{uid}@x", uid.upper(), "user", "active", uname, now, now),
            )
        # u1: 2 个正常 notebook + 1 个 copying(应被排除);u2: 0
        for nid, status in (("n1", "ready"), ("n2", "ready"), ("n3", "copying")):
            db.execute(
                "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)", (nid, nid, "u1", status, now, now),
            )
        # u1 在 n1 下:2 个 source、1 个 report、1 个 conversation，提交 2 次提问；
        # u2 作为共享成员在同一 notebook 另有 1 个 conversation/提问。
        # 提问次数必须数 ask_jobs，失败/取消也属于一次已提交问题。
        for sid in ("s1", "s2"):
            db.execute(
                "INSERT INTO sources "
                "(id,notebook_id,title,source_type,created_at,updated_at,uploaded_by) "
                "VALUES (?,?,?,?,?,?,?)", (sid, "n1", sid, "md", now, now, "u1"),
            )
        # 两份报告都建在 u1 的 notebook n1 里,但创建者不同:r1 是 owner 自己建的,
        # r2 是共享成员 u2 在同一本库里建的**他自己的**报告(群组知识共享 P1)。
        # 用量必须按 created_by 归集——按 notebook owner 归集会把 r2 记到 u1 头上。
        for report_id, creator in (("r1", "u1"), ("r2", "u2")):
            db.execute(
                "INSERT INTO reports (id,notebook_id,question,created_by,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)", (report_id, "n1", "q?", creator, now, now),
            )
        db.execute(
            "INSERT INTO conversations (id,notebook_id,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?)", ("c1", "n1", "u1", "2026-07-06T10:00:00", "2026-07-06T12:00:00"),
        )
        db.execute(
            "INSERT INTO conversations (id,notebook_id,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?)", ("c2", "n1", "u2", "2026-07-05T10:00:00", "2026-07-05T12:00:00"),
        )
        for job_id, conversation_id, creator, question, status in (
            ("j1", "c1", "u1", "first?", "completed"),
            ("j2", "c1", "u1", "second?", "failed"),
            ("j3", "c2", "u2", "shared?", "cancelled"),
        ):
            db.execute(
                "INSERT INTO ask_jobs "
                "(id,notebook_id,conversation_id,created_by,mode,question,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (job_id, "n1", conversation_id, creator, "chunk", question, status, now, now),
            )


def test_list_user_usage_counts(repo):
    _seed(repo)
    rows = {r["username"]: r for r in repo.list_user_usage()}
    # Should include u1, u2 and the auto-created admin user; check only the two we seeded
    assert "a00000001" in rows and "b00000002" in rows
    a = rows["a00000001"]
    assert a["id"] == "u1"
    assert a["role"] == "user"
    assert a["notebooks"] == 2          # copying 被排除
    assert a["sources"] == 2
    assert a["conversations"] == 1      # 兼容旧 API 字段
    assert a["questions"] == 2
    # 只数 u1 自己建的那一份;u2 在同一本库里建的 r2 不算 u1 的用量。
    assert a["reports"] == 1
    assert a["last_active"] == "2026-07-07T00:00:00"
    b = rows["b00000002"]
    assert b["notebooks"] == 0 and b["sources"] == 0
    assert b["conversations"] == 1 and b["questions"] == 1
    # u2 一本自己的库都没有,但他在别人的共享库里建了一份报告——按创建者归集,
    # 这一份必须记在他头上(与 `questions` 含共享库提交是同一条口径)。
    assert b["reports"] == 1
    assert b["last_active"] == "2026-07-07T00:00:00"


def test_last_active_tracks_user_actions_not_conversation_updates(repo):
    _seed(repo)

    def last_active(user_id="u1"):
        return next(
            row["last_active"]
            for row in repo.list_user_usage()
            if row["id"] == user_id
        )

    # 上传可见来源、提交提问、发起深度报告都会立即刷新。
    with repo._write() as db:
        db.execute(
            "UPDATE sources SET created_at=? WHERE id='s1'",
            ("2026-07-08T01:00:00+00:00",),
        )
    assert last_active() == "2026-07-08T01:00:00+00:00"

    with repo._write() as db:
        db.execute(
            "UPDATE ask_jobs SET created_at=? WHERE id='j1'",
            ("2026-07-08T02:00:00+00:00",),
        )
    assert last_active() == "2026-07-08T02:00:00+00:00"

    with repo._write() as db:
        db.execute(
            "UPDATE reports SET created_at=? WHERE id='r1'",
            ("2026-07-08T03:00:00+00:00",),
        )
    assert last_active() == "2026-07-08T03:00:00+00:00"

    # 后台回答落库会推进 conversation.updated_at，但那不是新的用户动作。
    with repo._write() as db:
        db.execute(
            "UPDATE conversations SET updated_at=? WHERE id='c1'",
            ("2026-07-08T04:00:00+00:00",),
        )
        db.execute(
            "UPDATE sources SET source_type='memory',created_at=? WHERE id='s2'",
            ("2026-07-08T05:00:00+00:00",),
        )
    assert last_active() == "2026-07-08T03:00:00+00:00"


def test_last_active_attributes_shared_upload_to_the_actual_uploader(repo):
    _seed(repo)
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,created_at,updated_at,uploaded_by) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                "shared-upload", "n1", "Shared", "pdf",
                "2026-07-09T00:00:00+00:00", "2026-07-09T00:00:00+00:00", "u2",
            ),
        )
    rows = {row["id"]: row for row in repo.list_user_usage()}
    assert rows["u2"]["last_active"] == "2026-07-09T00:00:00+00:00"
    assert rows["u1"]["last_active"] == "2026-07-07T00:00:00"


def test_last_active_source_group_uses_absolute_time_not_text_order(repo):
    _seed(repo)
    with repo._write() as db:
        db.execute(
            "UPDATE sources SET created_at=? WHERE id='s1'",
            ("2026-07-08T02:00:00+00:00",),
        )
        # 文本更大但绝对时刻是 01:30Z，不能盖过 s1 的 02:00Z。
        db.execute(
            "UPDATE sources SET created_at=? WHERE id='s2'",
            ("2026-07-08T10:30:00+09:00",),
        )
    usage = next(row for row in repo.list_user_usage() if row["id"] == "u1")
    assert usage["last_active"] == "2026-07-08T02:00:00+00:00"


def test_last_active_does_not_expose_unresolved_time_sentinel(repo):
    with repo._write() as db:
        db.execute(
            "INSERT INTO users "
            "(id,email,display_name,role,status,username,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "legacy", "legacy@x", "Legacy", "user", "active",
                "l00000001", "", "",
            ),
        )
        db.execute(
            "INSERT INTO notebooks "
            "(id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("legacy-nb", "Legacy", "legacy", "ready", "", ""),
        )
        db.execute(
            "INSERT INTO ask_jobs "
            "(id,notebook_id,created_by,mode,question,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "legacy-ask", "legacy-nb", "legacy", "chunk", "q", "done", "", "",
            ),
        )
    usage = next(row for row in repo.list_user_usage() if row["id"] == "legacy")
    assert usage["last_active"] is None


def test_source_store_stamps_visible_upload_actor_but_not_hidden_projection(repo):
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("actor-nb", "Actor", "user-local", "ready", "t0", "t0"),
        )
    store = repo._runtime.source_store
    common = {
        "notebook_id": "actor-nb", "status": "ready", "parse_status": "ready",
        "file_name": "", "file_path": "", "file_size": 0, "file_hash": "",
        "summary": "", "doc_type": "",
    }
    store.insert_source(
        source_id="visible-actor", title="Visible", source_type="pdf", **common
    )
    store.insert_source(
        source_id="hidden-actor", title="Hidden", source_type="memory", **common
    )
    with repo._connect() as db:
        rows = {
            row["id"]: row["uploaded_by"]
            for row in db.execute(
                "SELECT id,uploaded_by FROM sources WHERE notebook_id='actor-nb'"
            )
        }
    assert rows == {"visible-actor": "user-local", "hidden-actor": None}

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.main import create_app
    return TestClient(create_app())


def _auth(client, username):
    client.post("/api/auth/register", json={"username": username, "password": "pw"})
    token = client.post(
        "/api/auth/login", json={"username": username, "password": "pw"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _auth_admin(client):
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_admin_users_forbidden_for_regular_user(client):
    b = _auth(client, "z00123456")
    assert client.get("/api/admin/users", headers=b).status_code == 403


def test_admin_users_lists_username_and_counts(client):
    admin = _auth_admin(client)
    a = _auth(client, "z00123456")
    client.post("/api/notebooks", json={"name": "A1"}, headers=a)
    client.post("/api/notebooks", json={"name": "A2"}, headers=a)
    resp = client.get("/api/admin/users", headers=admin)
    assert resp.status_code == 200
    rows = {r["username"]: r for r in resp.json()}
    assert "admin" in rows and "z00123456" in rows
    assert rows["z00123456"]["notebooks"] == 2
    assert rows["z00123456"]["role"] == "user"
    assert rows["z00123456"]["role_mutable"] is True
    assert rows["admin"]["role_mutable"] is False
    # 展示用户名,但内部 id 仍是 user-<hex>(未统一)
    assert rows["z00123456"]["id"].startswith("user-")


def test_admin_users_is_online_reflects_pending_bus(client):
    from app.services.pending_bus import pending_bus
    admin = _auth_admin(client)
    _auth(client, "z00123456")
    rows = {r["username"]: r for r in client.get("/api/admin/users", headers=admin).json()}
    uid = rows["z00123456"]["id"]
    assert rows["z00123456"]["is_online"] is False        # 未连接 → 离线
    q = pending_bus.register(uid)
    try:
        rows2 = {r["username"]: r for r in client.get("/api/admin/users", headers=admin).json()}
        assert rows2["z00123456"]["is_online"] is True     # 有连接 → 在线
    finally:
        pending_bus.unregister(uid, q)
    rows3 = {r["username"]: r for r in client.get("/api/admin/users", headers=admin).json()}
    assert rows3["z00123456"]["is_online"] is False        # 断开 → 离线


def test_admin_online_endpoint_lists_connected(client):
    from app.services.pending_bus import pending_bus
    admin = _auth_admin(client)
    _auth(client, "z00123456")
    uid = {r["username"]: r for r in client.get("/api/admin/users", headers=admin).json()}["z00123456"]["id"]
    q = pending_bus.register(uid)
    try:
        data = client.get("/api/admin/online", headers=admin).json()
        assert uid in data["online_ids"]
        assert data["online_ids"] == sorted(data["online_ids"])  # 端点保证已排序
    finally:
        pending_bus.unregister(uid, q)


def test_admin_online_forbidden_for_regular_user(client):
    b = _auth(client, "z00123456")
    assert client.get("/api/admin/online", headers=b).status_code == 403


def test_admin_can_grant_and_revoke_role_for_existing_session(client):
    admin = _auth_admin(client)
    user_headers = _auth(client, "z00123456")
    user_id = {
        row["username"]: row
        for row in client.get("/api/admin/users", headers=admin).json()
    }["z00123456"]["id"]

    granted = client.patch(
        f"/api/admin/users/{user_id}/role",
        headers=admin,
        json={"role": "admin"},
    )
    assert granted.status_code == 200
    assert granted.json() == {
        "id": user_id,
        "username": "z00123456",
        "role": "admin",
    }
    # resolve_session 每次重新读取 users.role：已有 token 无需重新登录。
    assert client.get("/api/admin/users", headers=user_headers).status_code == 200

    revoked = client.patch(
        f"/api/admin/users/{user_id}/role",
        headers=admin,
        json={"role": "user"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["role"] == "user"
    assert client.get("/api/admin/users", headers=user_headers).status_code == 403


def test_regular_user_cannot_assign_admin_role(client):
    admin = _auth_admin(client)
    user_headers = _auth(client, "z00123456")
    user_id = {
        row["username"]: row
        for row in client.get("/api/admin/users", headers=admin).json()
    }["z00123456"]["id"]
    response = client.patch(
        f"/api/admin/users/{user_id}/role",
        headers=user_headers,
        json={"role": "admin"},
    )
    assert response.status_code == 403
    assert response.headers["X-User-Message"] == "1"


def test_builtin_admin_and_active_admin_cannot_demote_themselves(client):
    admin = _auth_admin(client)
    builtin = client.patch(
        "/api/admin/users/user-local/role",
        headers=admin,
        json={"role": "user"},
    )
    assert builtin.status_code == 409
    assert builtin.json()["detail"] == "内置管理员权限不可撤销"

    promoted_headers = _auth(client, "z00123456")
    user_id = {
        row["username"]: row
        for row in client.get("/api/admin/users", headers=admin).json()
    }["z00123456"]["id"]
    assert client.patch(
        f"/api/admin/users/{user_id}/role",
        headers=admin,
        json={"role": "admin"},
    ).status_code == 200
    self_demote = client.patch(
        f"/api/admin/users/{user_id}/role",
        headers=promoted_headers,
        json={"role": "user"},
    )
    assert self_demote.status_code == 409
    assert self_demote.json()["detail"] == "不能撤销当前账户的管理员权限"


def test_role_update_validates_target_and_role(client):
    admin = _auth_admin(client)
    assert client.patch(
        "/api/admin/users/missing/role",
        headers=admin,
        json={"role": "admin"},
    ).status_code == 404
    assert client.patch(
        "/api/admin/users/user-local/role",
        headers=admin,
        json={"role": "owner"},
    ).status_code == 422

# backend/tests/test_group_routes.py
"""群组与授权边 API 的行为契约(群组知识共享 P1-T3)。

T2 已经让 `notebook_grants` 一有行、读权与挂载有效性就自动生效;本轮做的是让那些行
**能被建出来、也能被撤掉**。所以这份矩阵钉的是策略层,而不是谓词层(谓词由
`test_access_sql_contract.py` 负责):

* 谁能建哪一类组(项目人人可建;部门/领域仅系统管理员);
* 建组即建组管理员——中间不存在一个「有组无管理员」的窗口;
* 群组的可见性口径是 **404**(非成员与不存在同一形态,不泄露存在性);
* 唯一 owner 不可被降级/移除/退出，转让后原 owner 保留管理员;
* 授权边创建的**双重条件**——对库有管理权 **且** 是目标组的组管理员,缺哪一半各有
  各自的响应;
* 撤销的**不对称**——笔记本维度与群组维度两个入口各只要一半权限;
* 删组把指向本组的授权边在同一事务里清掉(否则共享清单会挂着孤儿边);
* 列表投影:经群组授权可读的库进「群组」分区、带 `granted_via`,与成员行去重。
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app

    return TestClient(app)


_PASSWORD = "pw12345678"
_USERNAMES = iter(f"u{index:08d}" for index in range(1, 999))


def _login(client: TestClient, username: str) -> dict:
    client.post(
        "/api/auth/register", json={"username": username, "password": _PASSWORD}
    )
    token = client.post(
        "/api/auth/login", json={"username": username, "password": _PASSWORD}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _new_user(client: TestClient) -> tuple[dict, str, str]:
    """注册并登录一个新用户 → (headers, user_id, username)。"""
    username = next(_USERNAMES)
    headers = _login(client, username)
    user_id = client.get("/api/me", headers=headers).json()["id"]
    return headers, user_id, username


def _app_group_store():
    """**应用**(TestClient)手上那个 GroupStore 实例。

    `repo` fixture 另建了一个 `SQLiteRepository`——同一个 tmp 数据库文件,但不是同一个
    Python 对象。要在 HTTP 请求的执行路径中间插桩(模拟「守卫过了、写事务还没开」这个
    并发窗口),必须打应用自己那个实例的桩;打 `repo._runtime.groups` 的桩路由压根看
    不见,请求会一路成功,而测试看起来只是「断言写错了」。
    """
    from app.api import deps

    return deps.repository()._runtime.groups


def _promote_to_admin(repo, user_id: str) -> None:
    with repo._write() as db:
        db.execute("UPDATE users SET role='admin' WHERE id=?", (user_id,))


def _make_group(client: TestClient, headers: dict, name: str = "组") -> str:
    response = client.post("/api/groups", json={"name": name}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _make_notebook(client: TestClient, headers: dict, name: str = "库") -> str:
    return client.post("/api/notebooks", json={"name": name}, headers=headers).json()["id"]


def _share_to_group(
    client: TestClient, headers: dict, notebook_id: str, group_id: str, role: str = "viewer"
):
    return client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={"principal_type": "group", "principal_id": group_id, "role": role},
        headers=headers,
    )


# --------------------------------------------------------------------- 建组


def test_project_groups_are_open_but_department_and_domain_need_a_system_admin(
    tmp_path, monkeypatch, repo
):
    client = _client(tmp_path, monkeypatch)
    plain, _plain_id, _ = _new_user(client)

    assert client.post("/api/groups", json={"name": "项目组"}, headers=plain).status_code == 200
    for kind in ("department", "domain"):
        denied = client.post(
            "/api/groups", json={"name": "受限", "kind": kind}, headers=plain
        )
        assert denied.status_code == 403, kind
        assert denied.headers.get("X-User-Message") == "1"

    boss, boss_id, _ = _new_user(client)
    _promote_to_admin(repo, boss_id)
    for kind in ("department", "domain"):
        allowed = client.post(
            "/api/groups", json={"name": f"{kind}组", "kind": kind}, headers=boss
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["kind"] == kind


def test_unknown_kind_is_rejected_before_the_admin_gate(tmp_path, monkeypatch):
    """非法 kind 是 422,而不是「不是管理员」的 403 —— 两种错因不能混为一谈。"""
    client = _client(tmp_path, monkeypatch)
    plain, _uid, _ = _new_user(client)
    bad = client.post("/api/groups", json={"name": "X", "kind": "team"}, headers=plain)
    assert bad.status_code == 422


def test_creator_becomes_group_admin_in_the_same_write(tmp_path, monkeypatch):
    """建组即建组管理员。分成两个事务会留下一个谁也管不了的组。"""
    client = _client(tmp_path, monkeypatch)
    owner, owner_id, owner_name = _new_user(client)
    created = client.post("/api/groups", json={"name": "项目"}, headers=owner).json()

    assert created["my_role"] == "admin"
    assert created["owner_id"] == owner_id
    assert created["member_count"] == 1
    assert [(m["id"], m["role"]) for m in created["members"]] == [(owner_id, "admin")]
    assert created["members"][0]["username"] == owner_name


def test_blank_and_overlong_group_names_are_refused_not_truncated(tmp_path, monkeypatch):
    """用户编辑的数据不得静默截断:超限**明确拒绝**。"""
    client = _client(tmp_path, monkeypatch)
    headers, _uid, _ = _new_user(client)
    assert client.post("/api/groups", json={"name": "   "}, headers=headers).status_code == 400
    too_long = client.post("/api/groups", json={"name": "名" * 121}, headers=headers)
    assert too_long.status_code == 400
    assert client.get("/api/groups", headers=headers).json() == []


# ----------------------------------------------------------------- 可见性


def test_a_non_member_cannot_tell_a_group_apart_from_a_missing_one(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    stranger, _stranger_id, _ = _new_user(client)

    real = client.get(f"/api/groups/{group_id}", headers=stranger)
    missing = client.get("/api/groups/grp-does-not-exist", headers=stranger)
    assert real.status_code == missing.status_code == 404
    assert real.json()["detail"] == missing.json()["detail"]
    # 写路径同样 404(而不是 403):存在性不能从状态码上被区分出来。
    assert client.patch(
        f"/api/groups/{group_id}", json={"name": "改名"}, headers=stranger
    ).status_code == 404
    assert client.delete(f"/api/groups/{group_id}", headers=stranger).status_code == 404


def test_a_plain_member_sees_the_group_but_cannot_administer_it(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    member, member_id, _ = _new_user(client)
    assert client.put(
        f"/api/groups/{group_id}/members/{member_id}",
        json={"role": "member"},
        headers=owner,
    ).status_code == 200

    detail = client.get(f"/api/groups/{group_id}", headers=member)
    assert detail.status_code == 200
    assert detail.json()["my_role"] == "member"
    assert detail.json()["member_count"] == 2
    assert client.patch(
        f"/api/groups/{group_id}", json={"name": "改名"}, headers=member
    ).status_code == 404


def test_scope_all_is_admin_only(tmp_path, monkeypatch, repo):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    _make_group(client, owner)
    outsider, outsider_id, _ = _new_user(client)

    assert client.get("/api/groups", headers=outsider).json() == []
    assert client.get("/api/groups?scope=all", headers=outsider).status_code == 403
    _promote_to_admin(repo, outsider_id)
    everything = client.get("/api/groups?scope=all", headers=outsider)
    assert everything.status_code == 200
    assert len(everything.json()) == 1
    # 全局管理面里,他自己不是成员 —— my_role 必须如实为空,不能借视图冒充成员。
    assert everything.json()[0]["my_role"] == ""


# ----------------------------------------------------------------- 成员管理


def test_member_add_promote_demote_and_remove(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    member, member_id, _ = _new_user(client)

    added = client.put(
        f"/api/groups/{group_id}/members/{member_id}",
        json={"role": "member"},
        headers=owner,
    )
    assert added.status_code == 200
    assert {m["id"]: m["role"] for m in added.json()["members"]}[member_id] == "member"

    promoted = client.put(
        f"/api/groups/{group_id}/members/{member_id}",
        json={"role": "admin"},
        headers=owner,
    )
    assert {m["id"]: m["role"] for m in promoted.json()["members"]}[member_id] == "admin"
    # 升成管理员之后,他自己也能管人了。
    assert client.put(
        f"/api/groups/{group_id}/members/{member_id}",
        json={"role": "member"},
        headers=member,
    ).status_code == 200

    assert client.delete(
        f"/api/groups/{group_id}/members/{member_id}", headers=owner
    ).status_code == 204
    assert client.delete(
        f"/api/groups/{group_id}/members/{member_id}", headers=owner
    ).status_code == 404  # 已经不是成员了


def test_adding_an_unknown_user_is_a_clean_404_not_a_foreign_key_crash(
    tmp_path, monkeypatch
):
    """`group_members.user_id` 有 FK;不先查存在性就插,用户看到的是 500。"""
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    missing = client.put(
        f"/api/groups/{group_id}/members/user-does-not-exist",
        json={"role": "member"},
        headers=owner,
    )
    assert missing.status_code == 404
    assert missing.headers.get("X-User-Message") == "1"


def test_unknown_group_role_is_rejected(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    _member, member_id, _ = _new_user(client)
    assert client.put(
        f"/api/groups/{group_id}/members/{member_id}",
        json={"role": "owner"},
        headers=owner,
    ).status_code == 422


def test_group_invite_link_is_admin_managed_revocable_and_idempotent(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner, "邀请测试组")
    visitor, visitor_id, _ = _new_user(client)
    second, _second_id, _ = _new_user(client)

    # A read-only state check has no side effect; non-admins cannot inspect or
    # mint the bearer capability even when they know the group id.
    empty = client.get(f"/api/groups/{group_id}/invite-link", headers=owner)
    assert empty.status_code == 200
    assert empty.json() == {"active": False, "token": "", "created_at": None}
    assert client.post(
        f"/api/groups/{group_id}/invite-link", headers=visitor
    ).status_code == 404

    issued = client.post(f"/api/groups/{group_id}/invite-link", headers=owner)
    assert issued.status_code == 200
    token = issued.json()["token"]
    assert token.startswith("gri-")
    # Ordinary POST is idempotent: reopening the workspace can copy the same
    # link instead of silently invalidating one already sent to people.
    assert client.post(
        f"/api/groups/{group_id}/invite-link", headers=owner
    ).json()["token"] == token

    joined = client.post(f"/api/group-invites/{token}/join", headers=visitor)
    assert joined.status_code == 200
    assert joined.json()["id"] == group_id
    assert joined.json()["my_role"] == "member"
    assert {m["id"]: m["role"] for m in joined.json()["members"]}[visitor_id] == "member"
    # Reusing the link preserves existing role and membership rather than
    # inserting twice or rewriting the row.
    again = client.post(f"/api/group-invites/{token}/join", headers=visitor)
    assert again.status_code == 200
    assert again.json()["member_count"] == 2

    rotated = client.post(
        f"/api/groups/{group_id}/invite-link/rotate", headers=owner
    ).json()["token"]
    assert rotated != token
    assert client.post(f"/api/group-invites/{token}/join", headers=second).status_code == 404
    assert client.post(f"/api/group-invites/{rotated}/join", headers=second).status_code == 200
    assert client.get(f"/api/groups/{group_id}", headers=second).json()["my_role"] == "member"

    assert client.delete(
        f"/api/groups/{group_id}/invite-link", headers=owner
    ).status_code == 204
    third, _third_id, _ = _new_user(client)
    assert client.post(f"/api/group-invites/{rotated}/join", headers=third).status_code == 404
    assert client.get("/api/groups", headers=third).json() == []


def test_group_invite_never_demotes_an_existing_admin(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    admin, admin_id, _ = _new_user(client)
    client.put(
        f"/api/groups/{group_id}/members/{admin_id}",
        json={"role": "admin"},
        headers=owner,
    )
    token = client.post(
        f"/api/groups/{group_id}/invite-link", headers=owner
    ).json()["token"]
    joined = client.post(f"/api/group-invites/{token}/join", headers=admin)
    assert joined.status_code == 200
    assert joined.json()["my_role"] == "admin"
    assert {m["id"]: m["role"] for m in joined.json()["members"]}[admin_id] == "admin"


def test_group_owner_must_transfer_before_leaving_and_old_owner_stays_admin(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    owner, owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    _member, member_id, _ = _new_user(client)
    client.put(
        f"/api/groups/{group_id}/members/{member_id}",
        json={"role": "member"},
        headers=owner,
    )

    for response in (
        client.put(
            f"/api/groups/{group_id}/members/{owner_id}",
            json={"role": "member"},
            headers=owner,
        ),
        client.delete(f"/api/groups/{group_id}/members/{owner_id}", headers=owner),
        client.delete(f"/api/groups/{group_id}/membership", headers=owner),
    ):
        assert response.status_code == 409, response.text
        assert response.headers.get("X-User-Message") == "1"

    transferred = client.post(
        f"/api/groups/{group_id}/transfer",
        json={"new_owner_id": member_id},
        headers=owner,
    )
    assert transferred.status_code == 200, transferred.text
    assert transferred.json()["owner_id"] == member_id
    roles = {item["id"]: item["role"] for item in transferred.json()["members"]}
    assert roles == {owner_id: "admin", member_id: "admin"}

    # 转让后原 owner 已是普通管理员，可以退出。
    assert (
        client.delete(f"/api/groups/{group_id}/membership", headers=owner).status_code
        == 204
    )
    assert client.get(f"/api/groups/{group_id}", headers=owner).status_code == 404


def test_only_owner_can_transfer_or_delete_group(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    deputy, deputy_id, _ = _new_user(client)
    client.put(
        f"/api/groups/{group_id}/members/{deputy_id}",
        json={"role": "admin"},
        headers=owner,
    )

    denied = client.post(
        f"/api/groups/{group_id}/transfer",
        json={"new_owner_id": deputy_id},
        headers=deputy,
    )
    assert denied.status_code == 403
    assert client.delete(f"/api/groups/{group_id}", headers=deputy).status_code == 403
    assert client.get(f"/api/groups/{group_id}", headers=owner).json()["owner_id"] == owner_id


def test_a_plain_member_can_always_leave(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    member, member_id, _ = _new_user(client)
    client.put(
        f"/api/groups/{group_id}/members/{member_id}",
        json={"role": "member"},
        headers=owner,
    )
    assert client.delete(f"/api/groups/{group_id}/membership", headers=member).status_code == 204
    assert client.get(f"/api/groups/{group_id}", headers=member).status_code == 404
    assert client.get(f"/api/groups/{group_id}", headers=owner).json()["member_count"] == 1


def test_store_owner_protection_is_inside_the_member_write_transaction(repo):
    """直接调用 store 也不能把 owner 降级或移出。

    走 store 而不是 HTTP:要观察的是事务边界,不是路由。SQLite 侧由进程级写锁串行,
    所以这条在真实并发下也成立(PG 侧另在同一事务里锁 `groups` 行,由 G3 conformance
    覆盖)。
    """
    from app.repositories.ports import GroupOwnerProtectedError, GroupOwnerRequiredError

    store = repo._runtime.groups
    with repo._write() as db:
        for uid in ("g-admin-a", "g-admin-b"):
            db.execute(
                "INSERT INTO users "
                "(id,email,display_name,username,password_hash,role,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (uid, f"{uid}@t", uid, uid, "x", "user", _now(), _now()),
            )
    group = store.create_group(
        name="并发", kind="project", description="", created_by="g-admin-a"
    )
    store.upsert_member(group["id"], "g-admin-b", role="admin", added_by="g-admin-a")

    # 非 owner 管理员可降级；owner 即使还有另一名管理员也必须先转让。
    assert store.upsert_member(
        group["id"], "g-admin-b", role="member", added_by="g-admin-a"
    ) == "updated"
    with pytest.raises(GroupOwnerProtectedError):
        store.upsert_member(
            group["id"], "g-admin-a", role="member", added_by="g-admin-a"
        )
    with pytest.raises(GroupOwnerProtectedError):
        store.remove_member(group["id"], "g-admin-a")

    store.transfer_group_owner(
        group["id"], new_owner_id="g-admin-b", actor_id="g-admin-a"
    )
    # 路由鉴权之后若恰好完成一次转让，旧 owner 也不能穿过事务删除群组。
    with pytest.raises(GroupOwnerRequiredError):
        store.delete_group(group["id"], actor_id="g-admin-a")
    assert store.get_group(group["id"])["owner_id"] == "g-admin-b"


# ------------------------------------------------------------- 用户名解析


def test_resolve_user_is_exact_and_reveals_nothing_extra(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    headers, _uid, _ = _new_user(client)
    _other, other_id, other_name = _new_user(client)

    found = client.get(f"/api/users/resolve?username={other_name}", headers=headers)
    assert found.status_code == 200
    assert found.json() == {
        "id": other_id,
        "username": other_name,
        "display_name": other_name,
    }
    # 精确匹配:前缀不算命中。
    assert client.get(
        f"/api/users/resolve?username={other_name[:-1]}", headers=headers
    ).status_code == 404


# ------------------------------------------------------------------- 授权边


def test_granting_needs_both_notebook_manage_and_group_admin(tmp_path, monkeypatch):
    """双重条件:两半都要,而且缺哪一半的响应形态不同。"""
    client = _client(tmp_path, monkeypatch)
    boss, _boss_id, _ = _new_user(client)
    group_id = _make_group(client, boss)

    librarian, librarian_id, _ = _new_user(client)
    notebook_id = _make_notebook(client, librarian)

    # ① 有库的管理权,但不是组管理员 → 403(库的存在性对他本来就不是秘密)。
    lacking_group = _share_to_group(client, librarian, notebook_id, group_id)
    assert lacking_group.status_code == 403
    assert lacking_group.headers.get("X-User-Message") == "1"

    # ② 是组管理员,但对这本库没有管理权 → 404(不泄露库的存在性)。
    lacking_notebook = _share_to_group(client, boss, notebook_id, group_id)
    assert lacking_notebook.status_code == 404

    # ③ 两半都有 → 200。
    client.put(
        f"/api/groups/{group_id}/members/{librarian_id}",
        json={"role": "admin"},
        headers=boss,
    )
    granted = _share_to_group(client, librarian, notebook_id, group_id)
    assert granted.status_code == 200, granted.text
    body = granted.json()
    assert body["principal_type"] == "group"
    assert body["principal_id"] == group_id
    assert body["role"] == "viewer"
    assert body["principal_name"] == "组"
    assert body["principal_kind"] == "project"


def test_granting_to_a_missing_group_is_a_group_shaped_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    notebook_id = _make_notebook(client, owner)
    missing = _share_to_group(client, owner, notebook_id, "grp-nope")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "群组不存在"


def test_only_group_principals_can_be_granted_here(tmp_path, monkeypatch):
    """`user` 走只读共享链接、`everyone` 走公共知识库开关 —— 都不从这里进。"""
    client = _client(tmp_path, monkeypatch)
    owner, owner_id, _ = _new_user(client)
    notebook_id = _make_notebook(client, owner)
    for principal_type, principal_id in (("user", owner_id), ("everyone", "")):
        rejected = client.post(
            f"/api/notebooks/{notebook_id}/grants",
            json={
                "principal_type": principal_type,
                "principal_id": principal_id,
                "role": "viewer",
            },
            headers=owner,
        )
        assert rejected.status_code == 422, principal_type
    bad_role = client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={"principal_type": "group", "principal_id": "g", "role": "editor"},
        headers=owner,
    )
    assert bad_role.status_code == 422


def test_duplicate_grants_are_refused_not_silently_reused(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    notebook_id = _make_notebook(client, owner)

    assert _share_to_group(client, owner, notebook_id, group_id).status_code == 200
    duplicate = _share_to_group(client, owner, notebook_id, group_id, role="admin")
    assert duplicate.status_code == 409
    assert duplicate.headers.get("X-User-Message") == "1"
    # 冲突之后库里仍然只有原来那一条(role 没被偷偷改成 admin)。
    grants = client.get(f"/api/notebooks/{notebook_id}/grants", headers=owner).json()
    assert [(g["principal_id"], g["role"]) for g in grants] == [(group_id, "viewer")]
    # 同组的另一个主体类型是**另一条**边,不受 UNIQUE 影响。
    admins_edge = client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={
            "principal_type": "group_admins",
            "principal_id": group_id,
            "role": "admin",
        },
        headers=owner,
    )
    assert admins_edge.status_code == 200


def test_grant_list_is_owner_only_and_reports_every_principal_kind(
    tmp_path, monkeypatch, repo
):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    notebook_id = _make_notebook(client, owner)
    _share_to_group(client, owner, notebook_id, group_id)
    # 本端点建不出来、但库里可能存在的两类边:清单必须如实带上它们。
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebook_grants "
            "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("gnt-everyone", notebook_id, "everyone", "", "viewer", None, _now()),
        )

    listed = client.get(f"/api/notebooks/{notebook_id}/grants", headers=owner).json()
    assert {g["principal_type"] for g in listed} == {"group", "everyone"}
    everyone = next(g for g in listed if g["principal_type"] == "everyone")
    # everyone 行不该被 LEFT JOIN 误配上某个组的名字(principal_id 是多态列)。
    assert everyone["principal_name"] == "" and everyone["principal_kind"] == ""

    stranger, _stranger_id, _ = _new_user(client)
    assert client.get(
        f"/api/notebooks/{notebook_id}/grants", headers=stranger
    ).status_code == 404


# --------------------------------------------------------------------- 撤销


def test_revocation_is_asymmetric_from_both_sides(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    boss, boss_id, _ = _new_user(client)
    group_id = _make_group(client, boss)
    librarian, librarian_id, _ = _new_user(client)
    client.put(
        f"/api/groups/{group_id}/members/{librarian_id}",
        json={"role": "admin"},
        headers=boss,
    )
    notebook_id = _make_notebook(client, librarian)
    grant_id = _share_to_group(client, librarian, notebook_id, group_id).json()["id"]
    member, member_id, _ = _new_user(client)
    client.put(
        f"/api/groups/{group_id}/members/{member_id}",
        json={"role": "member"},
        headers=boss,
    )
    assert client.get(f"/api/notebooks/{notebook_id}", headers=member).status_code == 200

    # ① 笔记本维度:库的管理者删边,不要求他也是那个组的管理员。
    assert client.delete(
        f"/api/notebooks/{notebook_id}/grants/{grant_id}", headers=librarian
    ).status_code == 204
    assert client.get(f"/api/notebooks/{notebook_id}", headers=member).status_code == 404
    assert client.delete(
        f"/api/notebooks/{notebook_id}/grants/{grant_id}", headers=librarian
    ).status_code == 404

    # ② 群组维度:组管理员(这里用 boss,他对这本库没有任何权限)撤掉指向本组的边。
    _share_to_group(client, librarian, notebook_id, group_id)
    client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={
            "principal_type": "group_admins",
            "principal_id": group_id,
            "role": "admin",
        },
        headers=librarian,
    )
    assert client.get(f"/api/notebooks/{notebook_id}", headers=member).status_code == 200
    assert client.get(f"/api/notebooks/{notebook_id}", headers=boss).status_code == 200

    shared = client.get(f"/api/groups/{group_id}/shared-notebooks", headers=boss).json()
    assert len(shared) == 1
    assert shared[0]["notebook_id"] == notebook_id
    assert sorted(shared[0]["roles"]) == ["admin", "viewer"]

    assert client.delete(
        f"/api/groups/{group_id}/shared-notebooks/{notebook_id}", headers=boss
    ).status_code == 204
    assert client.get(f"/api/notebooks/{notebook_id}", headers=member).status_code == 404
    assert client.get(f"/api/groups/{group_id}/shared-notebooks", headers=boss).json() == []
    assert client.delete(
        f"/api/groups/{group_id}/shared-notebooks/{notebook_id}", headers=boss
    ).status_code == 404
    # 撤销入口只对组管理员开放。
    assert client.delete(
        f"/api/groups/{group_id}/shared-notebooks/{notebook_id}", headers=member
    ).status_code == 404


def test_a_grant_id_from_another_notebook_cannot_be_deleted(tmp_path, monkeypatch):
    """撤销端点的权限挂在 notebook 上,所以 id 必须与 notebook 一起验。"""
    client = _client(tmp_path, monkeypatch)
    victim, _victim_id, _ = _new_user(client)
    victim_group = _make_group(client, victim)
    victim_notebook = _make_notebook(client, victim)
    victim_grant = _share_to_group(
        client, victim, victim_notebook, victim_group
    ).json()["id"]

    attacker, _attacker_id, _ = _new_user(client)
    own_notebook = _make_notebook(client, attacker)
    assert client.delete(
        f"/api/notebooks/{own_notebook}/grants/{victim_grant}", headers=attacker
    ).status_code == 404
    assert len(
        client.get(f"/api/notebooks/{victim_notebook}/grants", headers=victim).json()
    ) == 1


def test_plain_member_inventory_excludes_group_admin_only_notebooks(
    tmp_path, monkeypatch
):
    """成员清单只能披露调用者实际有读权的 notebook。"""
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    notebook_id = _make_notebook(client, owner)
    admin, admin_id, _ = _new_user(client)
    member, member_id, _ = _new_user(client)
    for user_id, role in ((admin_id, "admin"), (member_id, "member")):
        client.put(
            f"/api/groups/{group_id}/members/{user_id}",
            json={"role": role},
            headers=owner,
        )
    _grant(client, owner, notebook_id, group_id, "group_admins", "admin")

    admin_inventory = client.get(
        f"/api/groups/{group_id}/shared-notebooks", headers=admin
    )
    assert [item["notebook_id"] for item in admin_inventory.json()] == [notebook_id]
    assert client.get(
        f"/api/groups/{group_id}/shared-notebooks", headers=member
    ).json() == []
    assert client.get(f"/api/notebooks/{notebook_id}", headers=member).status_code == 404


def test_deleting_a_group_clears_its_grants_in_the_same_write(tmp_path, monkeypatch, repo):
    """已定裁决 3:`principal_id` 无 FK,级联够不着它,必须显式清。"""
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    notebook_id = _make_notebook(client, owner)
    _share_to_group(client, owner, notebook_id, group_id)
    member, member_id, _ = _new_user(client)
    client.put(
        f"/api/groups/{group_id}/members/{member_id}",
        json={"role": "member"},
        headers=owner,
    )
    assert client.get(f"/api/notebooks/{notebook_id}", headers=member).status_code == 200

    assert client.delete(f"/api/groups/{group_id}", headers=owner).status_code == 204

    # 谓词即刻失效……
    assert client.get(f"/api/notebooks/{notebook_id}", headers=member).status_code == 404
    assert repo.user_can_read_notebook(notebook_id, member_id) is False
    # ……而且没有留下一条谁也解释不了的孤儿边。
    assert client.get(f"/api/notebooks/{notebook_id}/grants", headers=owner).json() == []
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM notebook_grants WHERE principal_id=?",
            (group_id,),
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM group_members WHERE group_id=?", (group_id,)
        ).fetchone()["c"] == 0


# --------------------------------------------------------------- 列表投影


def test_group_granted_notebooks_join_the_list_as_readers_with_their_origin(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, owner_name = _new_user(client)
    group_id = _make_group(client, owner, name="芯片项目")
    notebook_id = _make_notebook(client, owner, name="共享库")
    member, member_id, _ = _new_user(client)
    client.put(
        f"/api/groups/{group_id}/members/{member_id}",
        json={"role": "member"},
        headers=owner,
    )
    own_notebook = _make_notebook(client, member, name="我自己的")

    assert client.get("/api/notebooks", headers=member).json() != []
    _share_to_group(client, owner, notebook_id, group_id)
    listed = {n["id"]: n for n in client.get("/api/notebooks", headers=member).json()}

    assert set(listed) == {own_notebook, notebook_id}
    assert listed[own_notebook]["access"] == "owner"
    assert listed[own_notebook]["granted_via"] == []
    shared = listed[notebook_id]
    assert shared["access"] == "reader"
    assert shared["shared_from"] == owner_name
    assert shared["granted_via"] == [
        {"group_id": group_id, "group_name": "芯片项目", "kind": "project"}
    ]

    # 撤掉授权边,库立刻从列表里消失。
    grants = client.get(f"/api/notebooks/{notebook_id}/grants", headers=owner).json()
    client.delete(
        f"/api/notebooks/{notebook_id}/grants/{grants[0]['id']}", headers=owner
    )
    assert {n["id"] for n in client.get("/api/notebooks", headers=member).json()} == {
        own_notebook
    }


def test_group_admins_grants_only_reach_group_admins(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    notebook_id = _make_notebook(client, owner)
    plain, plain_id, _ = _new_user(client)
    deputy, deputy_id, _ = _new_user(client)
    client.put(
        f"/api/groups/{group_id}/members/{plain_id}",
        json={"role": "member"},
        headers=owner,
    )
    client.put(
        f"/api/groups/{group_id}/members/{deputy_id}",
        json={"role": "admin"},
        headers=owner,
    )
    client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={
            "principal_type": "group_admins",
            "principal_id": group_id,
            "role": "admin",
        },
        headers=owner,
    )

    assert {n["id"] for n in client.get("/api/notebooks", headers=plain).json()} == set()
    assert notebook_id in {
        n["id"] for n in client.get("/api/notebooks", headers=deputy).json()
    }
    # 降级即失效 —— 列表投影与读权谓词同步。
    client.put(
        f"/api/groups/{group_id}/members/{deputy_id}",
        json={"role": "member"},
        headers=owner,
    )
    assert {n["id"] for n in client.get("/api/notebooks", headers=deputy).json()} == set()


def test_a_notebook_reachable_twice_appears_once_with_the_member_row_winning(
    tmp_path, monkeypatch, repo
):
    """既是只读成员、又被共享给我所在的组 —— 列表里只出现一次,成员行优先。"""
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    notebook_id = _make_notebook(client, owner)
    member, member_id, _ = _new_user(client)
    client.put(
        f"/api/groups/{group_id}/members/{member_id}",
        json={"role": "member"},
        headers=owner,
    )
    _share_to_group(client, owner, notebook_id, group_id)
    repo.add_member(notebook_id, member_id)

    listed = [n for n in client.get("/api/notebooks", headers=member).json()]
    assert [n["id"] for n in listed] == [notebook_id]
    assert listed[0]["access"] == "reader"
    assert listed[0]["granted_via"] == []  # 成员行优先,不带群组来源


def test_owning_a_notebook_shared_to_my_own_group_does_not_duplicate_it(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    notebook_id = _make_notebook(client, owner)
    _share_to_group(client, owner, notebook_id, group_id)

    listed = client.get("/api/notebooks", headers=owner).json()
    assert [n["id"] for n in listed] == [notebook_id]
    assert listed[0]["access"] == "owner"


def test_two_groups_sharing_the_same_notebook_fold_into_one_card(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    first = _make_group(client, owner, name="甲组")
    second = _make_group(client, owner, name="乙组")
    notebook_id = _make_notebook(client, owner)
    member, member_id, _ = _new_user(client)
    for group_id in (first, second):
        client.put(
            f"/api/groups/{group_id}/members/{member_id}",
            json={"role": "member"},
            headers=owner,
        )
        _share_to_group(client, owner, notebook_id, group_id)

    listed = client.get("/api/notebooks", headers=member).json()
    assert [n["id"] for n in listed] == [notebook_id]
    assert sorted(g["group_id"] for g in listed[0]["granted_via"]) == sorted(
        [first, second]
    )


def test_a_copying_notebook_never_leaks_through_the_group_partition(
    tmp_path, monkeypatch, repo
):
    """半拷贝的哨兵状态必须与另外两段一样被排除。"""
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    notebook_id = _make_notebook(client, owner)
    _share_to_group(client, owner, notebook_id, group_id)
    member, member_id, _ = _new_user(client)
    client.put(
        f"/api/groups/{group_id}/members/{member_id}",
        json={"role": "member"},
        headers=owner,
    )
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET status='copying' WHERE id=?", (notebook_id,)
        )
    assert client.get("/api/notebooks", headers=member).json() == []


def test_shared_notebook_list_hides_copies_in_flight(tmp_path, monkeypatch, repo):
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, owner_name = _new_user(client)
    group_id = _make_group(client, owner)
    notebook_id = _make_notebook(client, owner, name="半拷贝")
    _share_to_group(client, owner, notebook_id, group_id)
    assert client.get(f"/api/groups/{group_id}/shared-notebooks", headers=owner).json() == [
        {
            "notebook_id": notebook_id,
            "name": "半拷贝",
            "owner_username": owner_name,
            "roles": ["viewer"],
        }
    ]
    with repo._write() as db:
        db.execute("UPDATE notebooks SET status='copying' WHERE id=?", (notebook_id,))
    assert client.get(f"/api/groups/{group_id}/shared-notebooks", headers=owner).json() == []


def test_a_viewer_grant_still_does_not_widen_write_access(tmp_path, monkeypatch):
    """`role='viewer'` 的授权边**只**扩读权——P1 的原始断言,P2 之后逐字仍成立。

    P2 翻的是 `role='admin'` 那一档(见下一条),这一条钉的是「翻的只有那一档」:
    viewer 边不因为主体是 `group_admins` 就顺带获得任何管理能力。此前这条用的是
    admin 边(P1 时两者行为相同),P2 之后必须换成 viewer 边才还在测这件事。
    """
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    notebook_id = _make_notebook(client, owner)
    member, member_id, _ = _new_user(client)
    client.put(
        f"/api/groups/{group_id}/members/{member_id}",
        json={"role": "admin"},
        headers=owner,
    )
    client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={
            "principal_type": "group_admins",
            "principal_id": group_id,
            "role": "viewer",
        },
        headers=owner,
    )

    assert client.get(f"/api/notebooks/{notebook_id}", headers=member).status_code == 200
    for method, suffix in (
        ("patch", ""),
        ("delete", ""),
        ("post", "/tier"),
        ("post", "/share"),
        ("get", "/grants"),
    ):
        response = client.request(
            method.upper(),
            f"/api/notebooks/{notebook_id}{suffix}",
            headers=member,
            json={} if method in ("post", "patch") else None,
        )
        assert response.status_code == 404, (method, suffix, response.status_code)


def test_an_admin_grant_widens_management_but_never_deletion(tmp_path, monkeypatch):
    """P2-T2 的能力翻转在这条路由矩阵上的样子——**逐格**说清哪几格翻了。

    这条取代了 P1 的「授权边的 role 写成 admin 也一个字的写权都不给」:那句话在 P2
    之后**不再为真**,是刻意的边界放宽(裁决 P2-1),不是回归。翻的是 `notebook:manage`
    那几格(改库信息 / 设 tier / 看授权边清单);`DELETE /notebooks/{id}` 挂
    `notebook:delete`,`/share` 与 `/bases`、`/mountable` 挂 `notebook:configure`
    (P2-T2 评审 P0),这三类恒 owner,组管理员照旧 404。

    ⚠ 「组管理员能删掉库主的整本笔记本」是本次改动最贵的失手形态,所以删库那一格
    **单独断言**而不是混在循环里——它失败时的信息必须直接说出这件事。
    """
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    notebook_id = _make_notebook(client, owner)
    member, member_id, _ = _new_user(client)
    client.put(
        f"/api/groups/{group_id}/members/{member_id}",
        json={"role": "admin"},
        headers=owner,
    )
    client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={
            "principal_type": "group_admins",
            "principal_id": group_id,
            "role": "admin",
        },
        headers=owner,
    )

    assert client.get(f"/api/notebooks/{notebook_id}", headers=member).status_code == 200
    # notebook:manage 那几格 —— 组管理员放行(不是 404)。/share **不在这里**:它已归
    # notebook:configure(恒 owner),下面单独断言 404。
    for method, suffix, payload in (
        ("patch", "", {"name": "组管理员改的名"}),
        ("post", "/tier", {"tier": "personal"}),
        ("get", "/grants", None),
    ):
        response = client.request(
            method.upper(),
            f"/api/notebooks/{notebook_id}{suffix}",
            headers=member,
            json=payload,
        )
        assert response.status_code != 404, (method, suffix, response.status_code, response.text)

    # notebook:configure 那几格(挂载配置 + 链接分享)—— 组管理员一律 404(P2-T2 评审 P0)。
    for method, suffix, payload in (
        ("get", "/bases", None),
        ("get", "/mountable", None),
        ("put", "/bases", {"base_notebook_ids": []}),
        ("get", "/share", None),
        ("post", "/share", None),
        ("delete", "/share", None),
    ):
        response = client.request(
            method.upper(),
            f"/api/notebooks/{notebook_id}{suffix}",
            headers=member,
            json=payload,
        )
        assert response.status_code == 404, (
            "configure 端点漏给了组管理员(P0):", method, suffix, response.status_code,
        )

    deleted = client.delete(f"/api/notebooks/{notebook_id}", headers=member)
    assert deleted.status_code == 404, (
        "组管理员删掉了库主的整本笔记本 —— notebook:delete 恒 owner(裁决 P2-1)"
    )
    assert client.get(f"/api/notebooks/{notebook_id}", headers=owner).status_code == 200


def test_group_partition_query_is_bounded_by_my_memberships(repo):
    """「群组」分区的列表投影挂在**每次打开笔记本列表**的路径上,代价必须只随
    「我加入了几个组」增长,不随「整个部署共享了多少本库」增长。

    判据是查询规划:必须从 `idx_group_members_user (user_id=?)` 起步,再用
    `idx_notebook_grants_principal` 的**双列**(principal_type + principal_id)点查。
    去掉 SQLite 那条 `CROSS JOIN` 驱动顺序提示时,planner 会改从 `notebook_grants`
    起步、只用得上 `principal_type` 单列——那是把全库群组授权边扫一遍。两者都不报错,
    也都返回同一批行,所以只有钉住计划才拦得住这次退化。

    ⚠ 曾经靠 `inspect.getsource` + 正则把 `granted_notebook_rows` 的源码字符串字面量
    拼回一条 SQL(批 3·W1 T-1 之前)。谓词单点化(`access_sql.NOTEBOOK_LIVE_SQL`)把
    末尾那个 `AND nb.status != 'copying'` 换成了一次变量拼接——源码里不再是一段连续
    的引号字面量,正则会把它整段吃掉,拼出语法错误的 SQL。改成**真的调用**
    `granted_notebook_rows` 并用一个记录型假 `db` 拦下它实际执行的 SQL/参数:这样
    拿到的是解释器真正会跑的语句,而不是源码层面的近似,顺带甩掉了「先剥注释再切」
    这条只为绕开源码巧合而存在的脆弱前处理。
    """
    from app.repositories.sqlite.query_store import QueryStore

    class _SqlCapture:
        """记录 `granted_notebook_rows` 会执行的 SQL 与参数,不碰真连接。"""

        sql: str = ""
        params: tuple = ()

        def execute(self, sql: str, params: tuple = ()) -> "_SqlCapture":
            self.sql = sql
            self.params = params
            return self

        @staticmethod
        def fetchall() -> list:
            return []

    capture = _SqlCapture()
    QueryStore.granted_notebook_rows(capture, "u")

    with repo._connect() as db:
        plan = [
            row["detail"]
            for row in db.execute("EXPLAIN QUERY PLAN " + capture.sql, capture.params)
        ]

    assert plan[0] == "SEARCH gm USING INDEX idx_group_members_user (user_id=?)", plan
    assert any(
        "idx_notebook_grants_principal (principal_type=? AND principal_id=?)" in step
        for step in plan
    ), plan
    assert not any("SCAN" in step for step in plan), plan


# ------------------------------------------------- 系统管理员运维旁路(裁决 A)


def test_system_admin_can_administer_any_group(tmp_path, monkeypatch, repo):
    """系统管理员绕过群组维度的成员/管理员判定(运维旁路)。

    与「系统管理员可转移 `notebooks.created_by`」同性质。普通用户的行为一个字不变,
    由本文件其余用例(非成员 404、普通成员不能改组)逐条钉住。
    """
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner, name="别人的组")
    boss, boss_id, _ = _new_user(client)

    # 提权之前:与任何陌生人一样,一律 404。
    assert client.get(f"/api/groups/{group_id}", headers=boss).status_code == 404
    assert client.patch(
        f"/api/groups/{group_id}", json={"name": "X"}, headers=boss
    ).status_code == 404

    _promote_to_admin(repo, boss_id)

    detail = client.get(f"/api/groups/{group_id}", headers=boss)
    assert detail.status_code == 200
    assert detail.json()["my_role"] == ""  # 旁路不伪造成员身份
    assert client.patch(
        f"/api/groups/{group_id}", json={"name": "接管改名"}, headers=boss
    ).status_code == 200
    assert client.get(
        f"/api/groups/{group_id}/shared-notebooks", headers=boss
    ).status_code == 200
    # 旁路仍要求组真的存在。
    assert client.get("/api/groups/grp-nope", headers=boss).status_code == 404
    assert client.patch(
        "/api/groups/grp-nope", json={"name": "X"}, headers=boss
    ).status_code == 404
    assert client.delete(f"/api/groups/{group_id}", headers=boss).status_code == 204


def test_system_admin_can_repair_a_group_left_without_admins(
    tmp_path, monkeypatch, repo
):
    """零管理员的组必须在 API 里可恢复 —— 那正是这条旁路存在的理由。

    「最后一名组管理员保护」在理论并发窗口下仍可能留下这种组;没有旁路,它的每个
    管理端点对**所有人**(包括系统管理员)都 404,只能改数据库才救得回来。
    """
    client = _client(tmp_path, monkeypatch)
    owner, owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    orphan, orphan_id, _ = _new_user(client)
    boss, boss_id, _ = _new_user(client)
    _promote_to_admin(repo, boss_id)
    # 直接造出零管理员的终态(并发窗口的产物,正常路径造不出来)。
    with repo._write() as db:
        db.execute(
            "UPDATE group_members SET role='member' WHERE group_id=?", (group_id,)
        )

    assert client.patch(
        f"/api/groups/{group_id}", json={"name": "X"}, headers=owner
    ).status_code == 404, "组管理员没了,原创建者自己也管不了 —— 这正是要救的形态"

    repaired = client.put(
        f"/api/groups/{group_id}/members/{orphan_id}",
        json={"role": "admin"},
        headers=boss,
    )
    assert repaired.status_code == 200
    assert {m["id"]: m["role"] for m in repaired.json()["members"]}[orphan_id] == "admin"
    # 补回管理员之后,组重新自治。
    assert client.patch(
        f"/api/groups/{group_id}", json={"name": "恢复"}, headers=orphan
    ).status_code == 200
    del owner_id


def test_system_admin_bypass_does_not_apply_to_leaving_a_group(
    tmp_path, monkeypatch, repo
):
    """自助退出**不**走旁路:「退出」的前提是本人真的在组里。"""
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    boss, boss_id, _ = _new_user(client)
    _promote_to_admin(repo, boss_id)
    assert client.delete(
        f"/api/groups/{group_id}/membership", headers=boss
    ).status_code == 404
    assert client.get(f"/api/groups/{group_id}", headers=owner).json()["member_count"] == 1


def test_scope_all_rows_are_openable_by_the_system_admin(tmp_path, monkeypatch, repo):
    """`?scope=all` 的每一行都必须点得开 —— 否则它是一张没用的表。"""
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    first = _make_group(client, owner, name="甲")
    second = _make_group(client, owner, name="乙")
    boss, boss_id, _ = _new_user(client)
    _promote_to_admin(repo, boss_id)
    listed = client.get("/api/groups?scope=all", headers=boss).json()
    assert sorted(g["id"] for g in listed) == sorted([first, second])
    for row in listed:
        assert client.get(f"/api/groups/{row['id']}", headers=boss).status_code == 200


def test_scope_parameter_is_strict(tmp_path, monkeypatch, repo):
    """非法 scope 明确 422,不静默当成 mine。"""
    client = _client(tmp_path, monkeypatch)
    boss, boss_id, _ = _new_user(client)
    _promote_to_admin(repo, boss_id)
    for bad in ("al", "ALL", "everything", ""):
        response = client.get(f"/api/groups?scope={bad}", headers=boss)
        assert response.status_code == 422, bad
    assert client.get("/api/groups?scope=mine", headers=boss).status_code == 200
    assert client.get("/api/groups?scope=all", headers=boss).status_code == 200


# ------------------------------------------------------ 并发窗口的失败关闭


def test_adding_a_member_to_a_concurrently_deleted_group_is_404_not_500(
    tmp_path, monkeypatch, repo
):
    """守卫通过之后、写事务落库之前组被删掉 → 404。

    少了写事务内那次存在性复核,`INSERT INTO group_members` 会撞外键抛
    `IntegrityError`,用户拿到的是 500。这里直接在守卫与写入之间把组删掉来复现
    (等价于并发删组恰好落在那个窗口里)。
    """
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    _member, member_id, _ = _new_user(client)

    store = _app_group_store()
    original = store.user_group_role

    def delete_then_answer(gid, uid):
        role = original(gid, uid)
        if gid == group_id:
            store.delete_group(group_id)  # 守卫刚过,写事务还没开
        return role

    monkeypatch.setattr(store, "user_group_role", delete_then_answer)
    response = client.put(
        f"/api/groups/{group_id}/members/{member_id}",
        json={"role": "member"},
        headers=owner,
    )
    monkeypatch.undo()
    assert response.status_code == 404, response.text
    assert response.headers.get("X-User-Message") == "1"


def test_removing_a_member_from_a_concurrently_deleted_group_is_404_not_500(repo):
    """`remove_member` 走同一条复核。直接打 store:路由已由上一条覆盖。"""
    from app.repositories.ports import GroupNotFoundError

    store = repo._runtime.groups
    with repo._write() as db:
        db.execute(
            "INSERT INTO users "
            "(id,email,display_name,username,password_hash,role,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("gone-a", "gone-a@t", "a", "gone-a", "x", "user", _now(), _now()),
        )
    group = store.create_group(
        name="将被删除", kind="project", description="", created_by="gone-a"
    )
    store.delete_group(group["id"])
    with pytest.raises(GroupNotFoundError):
        store.remove_member(group["id"], "gone-a")
    with pytest.raises(GroupNotFoundError):
        store.upsert_member(group["id"], "gone-a", role="member", added_by="gone-a")


def test_grant_creation_rechecks_group_admin_inside_the_write_transaction(
    tmp_path, monkeypatch, repo
):
    """授权边的双重条件在**写事务里**复核,不靠路由层那次查询。

    这条边一落库就立刻给整组人读权,所以「组还在、我还是它的管理员」必须与插入同
    事务。这里在请求进入 store 之前把发起者降级(等价于并发降级恰好落在窗口里),
    响应必须是 403 而**不是**一条已经发出去的边。
    """
    client = _client(tmp_path, monkeypatch)
    boss, boss_id, _ = _new_user(client)
    group_id = _make_group(client, boss)
    notebook_id = _make_notebook(client, boss)

    store = _app_group_store()
    original = store.create_grant

    def demote_then_create(*args, **kwargs):
        with repo._write() as db:
            db.execute(
                "UPDATE group_members SET role='member' WHERE group_id=? AND user_id=?",
                (group_id, boss_id),
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "create_grant", demote_then_create)
    response = _share_to_group(client, boss, notebook_id, group_id)
    monkeypatch.undo()
    assert response.status_code == 403, response.text
    assert client.get(f"/api/notebooks/{notebook_id}/grants", headers=boss).json() == []


def test_grant_creation_rechecks_group_existence_inside_the_write_transaction(
    tmp_path, monkeypatch, repo
):
    client = _client(tmp_path, monkeypatch)
    boss, _boss_id, _ = _new_user(client)
    group_id = _make_group(client, boss)
    notebook_id = _make_notebook(client, boss)

    store = _app_group_store()
    original = store.create_grant

    def delete_then_create(*args, **kwargs):
        store.delete_group(group_id)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "create_grant", delete_then_create)
    response = _share_to_group(client, boss, notebook_id, group_id)
    monkeypatch.undo()
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "群组不存在"


def test_orphan_grants_are_labelled_so_the_owner_can_understand_them(
    tmp_path, monkeypatch, repo
):
    """指向已不存在群组的孤儿边带 `principal_kind="missing"`,并且删得掉。

    删组事务会清掉这类边,但 `principal_id` 没有外键,合库(merge_dbs)的并集仍可能
    复活它们。不标注的话,库主看到的是一条没有名字的共享记录 —— 既不知道那是什么,
    也不知道能不能删。
    """
    client = _client(tmp_path, monkeypatch)
    owner, owner_id, _ = _new_user(client)
    notebook_id = _make_notebook(client, owner)
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebook_grants "
            "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("gnt-orphan", notebook_id, "group", "grp-vanished", "viewer", owner_id, _now()),
        )
        db.execute(
            "INSERT INTO notebook_grants "
            "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("gnt-all", notebook_id, "everyone", "", "viewer", owner_id, _now()),
        )
    listed = {g["id"]: g for g in client.get(
        f"/api/notebooks/{notebook_id}/grants", headers=owner
    ).json()}
    assert listed["gnt-orphan"]["principal_kind"] == "missing"
    assert listed["gnt-orphan"]["principal_name"] == ""
    # everyone / user 主体本来就没有组可解析,**不得**被误标成失效。
    assert listed["gnt-all"]["principal_kind"] == ""
    assert client.delete(
        f"/api/notebooks/{notebook_id}/grants/gnt-orphan", headers=owner
    ).status_code == 204


def test_duplicate_grant_classification_is_narrowed_to_its_own_constraint(repo):
    """UNIQUE 分类必须钉到**这条**约束上,别的完整性错误照常上抛。

    裸 `"UNIQUE" in message` 会把主键冲突之类的别的约束一起吞成一句「已经共享过了」
    —— 那是把一个真 bug 说成用户操作重复。
    """
    import sqlite3

    from app.repositories.ports import GroupGrantAlreadyExists

    store = repo._runtime.groups
    with repo._write() as db:
        db.execute(
            "INSERT INTO users "
            "(id,email,display_name,username,password_hash,role,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("dup-a", "dup-a@t", "a", "dup-a", "x", "user", _now(), _now()),
        )
        db.execute(
            "INSERT INTO notebooks "
            "(id,name,purpose,primary_domain,status,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("nb-dup", "NB", "", "Semiconductor", "draft", "dup-a", _now(), _now()),
        )
    group = store.create_group(
        name="重复", kind="project", description="", created_by="dup-a"
    )
    kwargs = dict(
        principal_type="group",
        principal_id=group["id"],
        role="viewer",
        created_by="dup-a",
        admin_user_id="dup-a",
    )
    store.create_grant("nb-dup", **kwargs)
    with pytest.raises(GroupGrantAlreadyExists):
        store.create_grant("nb-dup", **kwargs)

    # 别的完整性错误必须**原样上抛**,不被当成「重复共享」。这里造的是主键冲突:
    # 两条边的 (notebook, principal_type, principal_id) 三元组**不同**(第二个组),
    # 所以那条 UNIQUE 不成立,只有 `notebook_grants.id` 的主键会撞。两种冲突的
    # SQLite 报文都以「UNIQUE constraint failed」开头 —— 裸 `"UNIQUE" in message`
    # 会把这条真 bug 说成「你已经共享过了」。
    other = store.create_group(
        name="另一个", kind="project", description="", created_by="dup-a"
    )
    store.new_id = lambda prefix: "gnt-fixed-id"
    store.create_grant("nb-dup", **{**kwargs, "principal_id": other["id"]})
    with pytest.raises(sqlite3.IntegrityError) as raised:
        store.create_grant(
            "nb-dup", **{**kwargs, "principal_id": other["id"], "principal_type": "group_admins"}
        )
    assert "notebook_grants.id" in str(raised.value)
    assert not isinstance(raised.value, GroupGrantAlreadyExists)


# ------------------------------------------------------------- 成员侧行为


def test_group_granted_reader_can_ask_in_the_shared_notebook(tmp_path, monkeypatch):
    """成员经组授权 reader 走的是同一道读守卫 —— Ask/检索自动生效,零代码。

    计划里这一行的交付物就是这条验证用例。用 `test_notebook_share_readonly.py` 的
    同一批读路由,证明组授权 reader 与只读成员逐条同权。
    """
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    notebook_id = _make_notebook(client, owner)
    member, member_id, _ = _new_user(client)
    client.put(
        f"/api/groups/{group_id}/members/{member_id}",
        json={"role": "member"},
        headers=owner,
    )

    for suffix in ("", "/analytics", "/sources", "/graph", "/search?q=x", "/conversations"):
        assert client.get(
            f"/api/notebooks/{notebook_id}{suffix}", headers=member
        ).status_code == 404, suffix  # 授权之前一律不可见

    _share_to_group(client, owner, notebook_id, group_id)

    for suffix in ("", "/analytics", "/sources", "/graph", "/search?q=x", "/conversations"):
        assert client.get(
            f"/api/notebooks/{notebook_id}{suffix}", headers=member
        ).status_code == 200, suffix
    # 问题理解预检(Ask 管线的第一步)同样只过读守卫。
    intent = client.post(
        f"/api/notebooks/{notebook_id}/ask/intent",
        json={"question": "这本库讲了什么?"},
        headers=member,
    )
    assert intent.status_code != 404, intent.status_code


def test_group_kind_cannot_be_changed_after_creation(tmp_path, monkeypatch):
    """`kind` 不可改:传了直接 422(`extra="forbid"`),不是安静地忽略。

    它只是分类标签、不影响权限机制;放开它的后果是**标签失真**——普通用户能把自己
    建的项目组改标成「部门」,而建组时那道「部门/领域仅系统管理员」的闸正是为了让
    这个标签可信。
    """
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    rejected = client.patch(
        f"/api/groups/{group_id}", json={"kind": "department"}, headers=owner
    )
    assert rejected.status_code == 422
    assert client.get(f"/api/groups/{group_id}", headers=owner).json()["kind"] == "project"
    # 合法字段照常可改(证明 422 来自 kind 本身,不是整个端点坏了)。
    assert client.patch(
        f"/api/groups/{group_id}", json={"name": "新名"}, headers=owner
    ).status_code == 200


def test_group_ids_are_random_per_creation(tmp_path, monkeypatch):
    """已定裁决 1c:合库的 GLOBAL_UNION 语义依赖跨部署 id 不撞车。"""
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    ids = {_make_group(client, owner, name=f"组{index}") for index in range(3)}
    assert len(ids) == 3
    for group_id in ids:
        suffix = group_id.split("-", 1)[1]
        assert len(suffix) == len(uuid.uuid4().hex)
        int(suffix, 16)  # 纯十六进制:确认是随机 uuid 而不是自增序号


# ------------------------------------------------- P1-T4:owner 侧的可见性对账


def test_the_shared_badge_and_the_shared_by_me_overview_both_count_group_sharing(
    tmp_path, monkeypatch
):
    """「已分享」徽标与「已分享」总览的范围 = 只读共享 ∨ 共享给群组(P1-T4)。

    此前两者都只看 `notebooks.is_shared`(分享链接那一列),于是 owner 会看到一本
    「没有分享过」的库其实整组人可读——徽标不亮、总览也不列。两处必须同时改口径,
    否则会变成「徽标亮着而总览说尚未分享任何笔记本」这种更难解释的形态。
    """
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    notebook_id = _make_notebook(client, owner)

    # 共享之前:徽标灭、总览空。
    def summary() -> dict:
        listed = client.get("/api/notebooks", headers=owner).json()
        return next(item for item in listed if item["id"] == notebook_id)

    assert summary()["is_shared"] is False
    assert client.get("/api/notebooks/shared-by-me", headers=owner).json() == []

    assert _share_to_group(client, owner, notebook_id, group_id).status_code == 200

    assert summary()["is_shared"] is True
    # 单库详情走的是另一条行查询,同样要带上这一列。
    assert client.get(f"/api/notebooks/{notebook_id}", headers=owner).json()["is_shared"] is True

    overview = client.get("/api/notebooks/shared-by-me", headers=owner).json()
    assert [item["id"] for item in overview] == [notebook_id]
    # 只因群组共享而出现的行没有分享链接——前端据此不渲染链接框,也不给「取消分享」
    # (那个动作只撤链接与只读成员,对授权边是空操作)。
    assert overview[0]["share_token"] == ""
    assert overview[0]["group_count"] == 1


def test_group_count_counts_groups_not_grant_rows(tmp_path, monkeypatch):
    """同一个组的两条边(成员只读 / 组管理员可管)是一个组,不是两个。"""
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    other_group = _make_group(client, owner, name="另一个组")
    notebook_id = _make_notebook(client, owner)

    assert _share_to_group(client, owner, notebook_id, group_id).status_code == 200
    assert client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={"principal_type": "group_admins", "principal_id": group_id, "role": "admin"},
        headers=owner,
    ).status_code == 200
    assert _share_to_group(client, owner, notebook_id, other_group).status_code == 200

    overview = client.get("/api/notebooks/shared-by-me", headers=owner).json()
    assert overview[0]["group_count"] == 2


def test_revoking_the_last_group_grant_puts_the_badge_back_out(tmp_path, monkeypatch):
    """撤销之后徽标必须灭、总览必须空——徽标只增不减就是另一种撒谎。"""
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    notebook_id = _make_notebook(client, owner)
    grant_id = _share_to_group(client, owner, notebook_id, group_id).json()["id"]

    assert client.delete(
        f"/api/notebooks/{notebook_id}/grants/{grant_id}", headers=owner
    ).status_code == 204

    listed = client.get("/api/notebooks", headers=owner).json()
    assert next(item for item in listed if item["id"] == notebook_id)["is_shared"] is False
    assert client.get("/api/notebooks/shared-by-me", headers=owner).json() == []


def test_a_link_shared_notebook_keeps_its_overview_row_shape(tmp_path, monkeypatch):
    """只读共享那条路逐字不变:仍带 token、仍可取消,只是多一个恒为 0 的群组计数。"""
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    notebook_id = _make_notebook(client, owner)
    client.post(f"/api/notebooks/{notebook_id}/share", headers=owner)

    overview = client.get("/api/notebooks/shared-by-me", headers=owner).json()
    assert [item["id"] for item in overview] == [notebook_id]
    assert overview[0]["share_token"]
    assert overview[0]["group_count"] == 0


def test_a_group_reader_does_not_see_the_owners_sharing_state(tmp_path, monkeypatch):
    """`is_shared` 是 owner 视角的字段。成员那份投影不带它——群组共享进来的库在成员
    列表里仍是 `is_shared=False`,与只读共享成员看到的形状一致。"""
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    member, member_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    client.put(
        f"/api/groups/{group_id}/members/{member_id}", json={"role": "member"}, headers=owner
    )
    notebook_id = _make_notebook(client, owner)
    _share_to_group(client, owner, notebook_id, group_id)

    listed = client.get("/api/notebooks", headers=member).json()
    shared = next(item for item in listed if item["id"] == notebook_id)
    assert shared["access"] == "reader"
    assert shared["is_shared"] is False
    assert [g["group_id"] for g in shared["granted_via"]] == [group_id]


def test_mountable_candidates_carry_where_they_come_from(tmp_path, monkeypatch, repo):
    """挂载候选带 `origin`,让选择器分得出「我的」与「共享给我的」(P1-T4)。

    「读权 ⇒ 可挂载」放开之后,别人 owner 的库也会进候选;前端只有 `tier` 可分时会
    把它们标成「我的笔记本」——一句事实错误的标签。
    """
    client = _client(tmp_path, monkeypatch)
    owner, owner_id, _ = _new_user(client)
    other, _other_id, _ = _new_user(client)
    _promote_to_admin(repo, owner_id)

    mine = _make_notebook(client, owner, name="我的库")
    public = _make_notebook(client, owner, name="公共库")
    client.post(f"/api/notebooks/{public}/tier", json={"tier": "base"}, headers=owner)

    # 别人的库经群组授权共享给我。
    group_id = _make_group(client, other, name="他的组")
    client.put(
        f"/api/groups/{group_id}/members/{owner_id}", json={"role": "member"}, headers=other
    )
    borrowed = _make_notebook(client, other, name="他的库")
    assert _share_to_group(client, other, borrowed, group_id).status_code == 200

    target = _make_notebook(client, owner, name="挂载方")
    candidates = client.get(f"/api/notebooks/{target}/mountable", headers=owner).json()
    origins = {item["id"]: item["origin"] for item in candidates}
    assert origins[public] == "base"
    assert origins[mine] == "mine"
    assert origins[borrowed] == "shared"


def test_my_own_public_notebook_still_reads_as_a_public_one(tmp_path, monkeypatch, repo):
    """优先级 base → mine → shared:自己 owner 的公共知识库仍判 `base`,这样本字段
    出现之前按 `tier` 分组的结果逐字不变。"""
    client = _client(tmp_path, monkeypatch)
    owner, owner_id, _ = _new_user(client)
    _promote_to_admin(repo, owner_id)
    public = _make_notebook(client, owner, name="我建的公共库")
    client.post(f"/api/notebooks/{public}/tier", json={"tier": "base"}, headers=owner)
    target = _make_notebook(client, owner, name="挂载方")

    candidates = client.get(f"/api/notebooks/{target}/mountable", headers=owner).json()
    assert {item["id"]: item["origin"] for item in candidates}[public] == "base"


# ------------------------------------------ P1-T4:单库详情投影(修长期缺口)


def test_notebook_detail_reports_the_viewers_own_relation(tmp_path, monkeypatch):
    """`GET /notebooks/{id}` 必须如实回填 `access` / `shared_from` / `granted_via`。

    这是一个**长期缺口**:详情投影从来没算过 `access`,于是它永远是模型默认的
    `"owner"`,工作区顶栏那整段 reader 分支(只读徽章、退出共享、群组来源标注)对着
    详情响应从来没有为真过。列表投影一直是对的,所以缺口只在「打开一本别人共享给我
    的库」时暴露 —— 而那正是本特性的主场景。
    """
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, owner_name = _new_user(client)
    member, member_id, _ = _new_user(client)
    group_id = _make_group(client, owner, name="封装项目")
    client.put(
        f"/api/groups/{group_id}/members/{member_id}", json={"role": "member"}, headers=owner
    )
    notebook_id = _make_notebook(client, owner, name="封装工艺库")
    assert _share_to_group(client, owner, notebook_id, group_id).status_code == 200

    seen = client.get(f"/api/notebooks/{notebook_id}", headers=member)
    assert seen.status_code == 200, seen.text
    body = seen.json()
    assert body["access"] == "reader"
    assert body["shared_from"] == owner_name
    assert [
        (g["group_id"], g["group_name"], g["kind"]) for g in body["granted_via"]
    ] == [(group_id, "封装项目", "project")]

    # owner 自己打开同一本库:仍是 owner,不带来源标注(他的库不可能经群组共享给他)。
    mine = client.get(f"/api/notebooks/{notebook_id}", headers=owner).json()
    assert mine["access"] == "owner"
    assert mine["shared_from"] == ""
    assert mine["granted_via"] == []


def test_plain_readonly_member_detail_has_no_group_origin(tmp_path, monkeypatch):
    """只读共享(分享链接)进来的成员同样是 reader,但 `granted_via` 为空。

    前端两处入口靠这一条分流:只读共享有「退出共享」这个用户自己能按的出口,群组
    共享没有(那个按钮打的是成员表,对授权边是空操作)。
    """
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, owner_name = _new_user(client)
    member, member_id, _ = _new_user(client)
    notebook_id = _make_notebook(client, owner)
    _app_group_store()  # 触发应用实例构造,与本文件其它用例同款
    from app.api import deps

    deps.repository().add_member(notebook_id, member_id)

    body = client.get(f"/api/notebooks/{notebook_id}", headers=member).json()
    assert body["access"] == "reader"
    assert body["shared_from"] == owner_name
    assert body["granted_via"] == []


def test_a_member_who_is_also_in_a_granted_group_keeps_the_member_row(
    tmp_path, monkeypatch
):
    """交叉态(P3-3):既经分享链接加入、又在被授权的群组里。

    列表投影按 **id 去重、成员行优先**,所以这本库出现一次、`granted_via` 为空 ——
    他仍然拿得到「退出共享」,而那个动作对他确实有效(删的正是那条成员行)。删掉成员
    行之后,群组授权接管,同一本库改从「群组」那一段来、带上来源标注。详情投影必须
    与列表同口径,否则同一本库在卡片上和打开之后是两副面孔。
    """
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    member, member_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    client.put(
        f"/api/groups/{group_id}/members/{member_id}", json={"role": "member"}, headers=owner
    )
    notebook_id = _make_notebook(client, owner)
    _share_to_group(client, owner, notebook_id, group_id)
    from app.api import deps

    deps.repository().add_member(notebook_id, member_id)

    listed = [
        item for item in client.get("/api/notebooks", headers=member).json()
        if item["id"] == notebook_id
    ]
    assert len(listed) == 1, "同一本库不得出现两次"
    assert listed[0]["granted_via"] == []            # 成员行优先
    assert client.get(f"/api/notebooks/{notebook_id}", headers=member).json()["granted_via"] == []

    # 退出只读共享之后,群组授权接管 —— 库还在,只是这次带上来源标注。
    assert client.delete(
        f"/api/notebooks/{notebook_id}/membership", headers=member
    ).status_code == 204
    after = [
        item for item in client.get("/api/notebooks", headers=member).json()
        if item["id"] == notebook_id
    ]
    assert len(after) == 1
    assert [g["group_id"] for g in after[0]["granted_via"]] == [group_id]
    detail = client.get(f"/api/notebooks/{notebook_id}", headers=member).json()
    assert [g["group_id"] for g in detail["granted_via"]] == [group_id]


def test_detail_still_reports_is_shared_to_a_reader(tmp_path, monkeypatch):
    """`is_shared` 是 owner 视角的字段,但**详情**路径不分角色地带上它。

    这不是本轮改出来的:`NotebookSummary.is_shared` 的字段注释一直写着「reader 看到
    的原库 is_shared 也为 True」。钉住现状,免得日后有人把它读成泄露。
    """
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    member, member_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    client.put(
        f"/api/groups/{group_id}/members/{member_id}", json={"role": "member"}, headers=owner
    )
    notebook_id = _make_notebook(client, owner)
    _share_to_group(client, owner, notebook_id, group_id)

    assert client.get(f"/api/notebooks/{notebook_id}", headers=member).json()["is_shared"] is True


# ------------------------------------- P1-T4:打开分享弹窗不该铸出一条链接


def test_reading_share_state_never_mints_a_token(tmp_path, monkeypatch):
    """`GET /notebooks/{id}/share` 是只读的。

    此前打开分享弹窗会 `POST .../share`,于是「只想共享给群组」的用户会顺带被发一条
    分享链接——一次纯查看的动作产生了持久副作用。
    """
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    notebook_id = _make_notebook(client, owner)
    _share_to_group(client, owner, notebook_id, group_id)

    state = client.get(f"/api/notebooks/{notebook_id}/share", headers=owner)
    assert state.status_code == 200, state.text
    assert state.json() == {"share_token": "", "copyable": False, "size": {}}

    # 真的没铸:读第二次仍然没有 token,总览里也没有链接。
    assert client.get(f"/api/notebooks/{notebook_id}/share", headers=owner).json()["share_token"] == ""
    overview = client.get("/api/notebooks/shared-by-me", headers=owner).json()
    assert [item["share_token"] for item in overview] == [""]
    assert overview[0]["group_count"] == 1


def test_share_state_reports_an_existing_link(tmp_path, monkeypatch):
    """已经开过链接的库:GET 如实返回同一个 token 与规模,POST 仍然幂等。"""
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    notebook_id = _make_notebook(client, owner)
    minted = client.post(f"/api/notebooks/{notebook_id}/share", headers=owner).json()

    state = client.get(f"/api/notebooks/{notebook_id}/share", headers=owner).json()
    assert state["share_token"] == minted["share_token"]
    assert state["copyable"] == minted["copyable"]
    assert state["size"] == minted["size"]
    # 撤销之后回到空态。
    assert client.delete(f"/api/notebooks/{notebook_id}/share", headers=owner).status_code == 204
    assert client.get(f"/api/notebooks/{notebook_id}/share", headers=owner).json()["share_token"] == ""


def test_share_state_is_owner_only(tmp_path, monkeypatch):
    """与 POST 同一道 `notebook:manage` 守卫:组成员读不到别人库的分享状态。"""
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    member, member_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    client.put(
        f"/api/groups/{group_id}/members/{member_id}", json={"role": "member"}, headers=owner
    )
    notebook_id = _make_notebook(client, owner)
    _share_to_group(client, owner, notebook_id, group_id)

    assert client.get(f"/api/notebooks/{notebook_id}", headers=member).status_code == 200
    assert client.get(f"/api/notebooks/{notebook_id}/share", headers=member).status_code == 404


def test_group_only_rows_skip_the_copy_stats_and_member_lookups(tmp_path, monkeypatch):
    """「已分享」总览对**没有链接**的行不算规模、不查成员。

    `mode`/`size`/`members` 说的全是「这条链接是怎么回事」,纯群组共享的行没有链接、
    前端也不渲染它们;而 `notebook_copy_stats` 每次要新鲜复核一次深拷贝上限、
    `list_members` 又是一次查询——50 本只共享给群组的库会白付 100 次。
    """
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    linked = _make_notebook(client, owner, name="发了链接的")
    group_only = _make_notebook(client, owner, name="只共享给组的")
    client.post(f"/api/notebooks/{linked}/share", headers=owner)
    _share_to_group(client, owner, group_only, group_id)

    from app.api import deps

    sharing = deps.notebook_sharing_repository()
    calls: list[str] = []
    original = sharing.notebook_copy_stats
    member_calls: list[str] = []
    original_members = sharing.list_members

    def spy_stats(notebook_id: str):
        calls.append(notebook_id)
        return original(notebook_id)

    def spy_members(notebook_id: str):
        member_calls.append(notebook_id)
        return original_members(notebook_id)

    monkeypatch.setattr(sharing, "notebook_copy_stats", spy_stats)
    monkeypatch.setattr(sharing, "list_members", spy_members)

    overview = {item["id"]: item for item in sharing.shared_by_me(_owner_id)}
    assert set(overview) == {linked, group_only}
    assert calls == [linked], "纯群组共享的行不该触发规模统计"
    assert group_only not in member_calls
    assert overview[group_only]["size"] == {}
    assert overview[group_only]["members"] == []
    assert overview[linked]["size"]["sources"] == 0


# ------------------------------------------- P2-T2:can_manage_content 投影


def _grant(client, headers, notebook_id, group_id, principal_type, role):
    return client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={
            "principal_type": principal_type,
            "principal_id": group_id,
            "role": role,
        },
        headers=headers,
    )


def test_can_manage_content_tracks_the_grant_role_in_list_and_detail(
    tmp_path, monkeypatch
):
    """`NotebookSummary.can_manage_content`(裁决 P2-3)在**列表与详情**给同一答案。

    列表侧由 `granted_notebook_rows` 顺带带回的 `_grant_role` 派生(零新增查询),详情侧
    由同一条查询的点查形态派生 —— 两处必须逐格一致,否则同一本库在卡片上可写、点进去
    只读(或反过来),而这种分叉不报错、只是界面自相矛盾。
    """
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    managed = _make_notebook(client, owner, name="可管理的")
    read_only = _make_notebook(client, owner, name="只读的")
    deputy, deputy_id, _ = _new_user(client)
    client.put(
        f"/api/groups/{group_id}/members/{deputy_id}",
        json={"role": "admin"},
        headers=owner,
    )
    assert _grant(client, owner, managed, group_id, "group_admins", "admin").status_code == 200
    assert _grant(client, owner, read_only, group_id, "group", "viewer").status_code == 200
    own = _make_notebook(client, deputy, name="我自己的")

    listed = {n["id"]: n for n in client.get("/api/notebooks", headers=deputy).json()}
    assert set(listed) == {own, managed, read_only}
    assert listed[own]["can_manage_content"] is True       # owner 恒真
    assert listed[managed]["can_manage_content"] is True   # 管理边
    assert listed[read_only]["can_manage_content"] is False  # viewer 边

    for notebook_id in (own, managed, read_only):
        detail = client.get(f"/api/notebooks/{notebook_id}", headers=deputy).json()
        assert detail["can_manage_content"] == listed[notebook_id]["can_manage_content"], (
            notebook_id, detail["can_manage_content"],
        )
        # access 枚举**不扩**(裁决 P2-3):管理权是正交的第二维。
        assert detail["access"] == ("owner" if notebook_id == own else "reader")


def test_can_manage_content_survives_the_member_row_dedup(tmp_path, monkeypatch, repo):
    """交叉态:既是只读成员、又持管理边 —— 权限为真,`granted_via` 仍被成员行压掉。

    ⚠ 这是列表与详情最容易分叉的那一格。`granted_via` 的去重口径是「成员行优先」
    (成员手上的「退出共享」是一个真的有效的动作,不能藏),而 `can_manage_content`
    是**权限**、不该被展示去重带走。任何把两者写在同一个 early-return 里的实现,都会
    让这本库在列表上说「可管理」、点进去说「只读」。
    """
    client = _client(tmp_path, monkeypatch)
    owner, _owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    notebook_id = _make_notebook(client, owner)
    deputy, deputy_id, _ = _new_user(client)
    client.put(
        f"/api/groups/{group_id}/members/{deputy_id}",
        json={"role": "admin"},
        headers=owner,
    )
    _grant(client, owner, notebook_id, group_id, "group_admins", "admin")
    repo.add_member(notebook_id, deputy_id)  # 同时还经分享链接加入

    listed = [n for n in client.get("/api/notebooks", headers=deputy).json()]
    assert [n["id"] for n in listed] == [notebook_id]
    assert listed[0]["granted_via"] == [], "成员行优先的展示口径被改了"
    assert listed[0]["can_manage_content"] is True

    detail = client.get(f"/api/notebooks/{notebook_id}", headers=deputy).json()
    assert detail["granted_via"] == []
    assert detail["can_manage_content"] is True
    # 且它确实能用:内容管理档端点放行(改名 = notebook:manage)。⚠ 不用 mounted-by-count
    # ——它是 notebook:configure(恒 owner),组管理员对它 404,拿它当「管理档」会测反。
    assert client.patch(
        f"/api/notebooks/{notebook_id}", json={"name": "probe"}, headers=deputy
    ).status_code == 200


def test_can_manage_content_matches_the_authoritative_predicate(tmp_path, monkeypatch, repo):
    """投影必须与**守卫用的那条谓词**同答案(而不是各判各的)。

    投影为了零新增查询走的是列表查询顺带带回的 `_grant_role`,守卫走的是
    `access_sql.NOTEBOOK_ADMIN_SQL`。两条路径不同,答案必须相同——这条把「不同实现、
    同一答案」变成可执行的断言,免得投影某天悄悄比谓词宽(那会让界面画出一个必然
    404 的按钮)。
    """
    client = _client(tmp_path, monkeypatch)
    owner, owner_id, _ = _new_user(client)
    group_id = _make_group(client, owner)
    managed = _make_notebook(client, owner)
    read_only = _make_notebook(client, owner)
    deputy, deputy_id, _ = _new_user(client)
    plain, plain_id, _ = _new_user(client)
    for user_id, role in ((deputy_id, "admin"), (plain_id, "member")):
        client.put(
            f"/api/groups/{group_id}/members/{user_id}",
            json={"role": role},
            headers=owner,
        )
    _grant(client, owner, managed, group_id, "group", "admin")  # 整组可管理
    _grant(client, owner, read_only, group_id, "group", "viewer")

    for headers, user_id in ((owner, owner_id), (deputy, deputy_id), (plain, plain_id)):
        listed = {n["id"]: n for n in client.get("/api/notebooks", headers=headers).json()}
        for notebook_id in (managed, read_only):
            if notebook_id not in listed:
                continue
            assert listed[notebook_id]["can_manage_content"] is repo.user_can_admin_notebook(
                notebook_id, user_id
            ), (notebook_id, user_id)


def test_grantable_principal_types_stay_the_two_group_subjects(tmp_path, monkeypatch):
    """`can_manage_content` 的派生前提:授权边只能经**两个群组主体**发放。

    列表投影(`granted_notebook_rows`)只覆盖 `group` / `group_admins`,而后端的管理权
    谓词是四臂全收。两者今天等价**只因为**这份白名单——`user` / `everyone` 的边根本
    没有写入口,更不可能带 `role='admin'`。真要给 `user` 主体开写入口,这条守卫会先红,
    提醒把那份投影一并扩掉(否则新主体的管理边在界面上完全不可见)。
    """
    from app.models.groups import GRANTABLE_PRINCIPAL_TYPES
    from app.repositories.group_rows import GROUP_PRINCIPAL_TYPES

    assert set(GRANTABLE_PRINCIPAL_TYPES) == {"group", "group_admins"}
    assert set(GRANTABLE_PRINCIPAL_TYPES) == set(GROUP_PRINCIPAL_TYPES)

    # 行为面:端点确实拒收另外两个主体。
    client = _client(tmp_path, monkeypatch)
    owner, owner_id, _ = _new_user(client)
    notebook_id = _make_notebook(client, owner)
    for principal_type, principal_id in (("user", owner_id), ("everyone", "")):
        refused = client.post(
            f"/api/notebooks/{notebook_id}/grants",
            json={
                "principal_type": principal_type,
                "principal_id": principal_id,
                "role": "admin",
            },
            headers=owner,
        )
        assert refused.status_code == 422, (principal_type, refused.status_code)

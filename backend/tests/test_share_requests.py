# backend/tests/test_share_requests.py
"""成员贡献审批流的行为契约(群组知识共享 P2-T3)。

覆盖的是**另一半**共享入口:一个对某本库有管理权、但对目标组只是普通成员的人,把
库贡献给那个组要**申请**,由组管理员审批。状态机是 **pending → approved / rejected
单向**;撤回是申请者删整行、不是第四个状态(裁决 P2-2)。这份矩阵钉的红线:

* 创建的双重条件——对库有管理权 **且** 是目标组成员,缺哪一半各有各自的响应;
* 撞 `uq_share_requests_one_pending` 是**幂等**返回既有 pending,不是 409(裁决 P2-5);
* 批准把 `(group, viewer)` 边与状态更新放在**同一写事务**;已共享则幂等;
* `decided_at` 两态:pending 时 SQL NULL、已决定时 ISO 时间戳,绝不是空串;
* `status` 一律**精确匹配**已知取值消费(pending 队列 / 审批前置 / 铃铛计数);
* 撤回只对 pending 生效——已决定的撤回是 409,不存在的是 404;组/库删除 CASCADE 带走申请。
"""
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
_USERNAMES = iter(f"s{index:08d}" for index in range(1, 999))


def _login(client: TestClient, username: str) -> dict:
    client.post("/api/auth/register", json={"username": username, "password": _PASSWORD})
    token = client.post(
        "/api/auth/login", json={"username": username, "password": _PASSWORD}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _new_user(client: TestClient) -> tuple[dict, str, str]:
    username = next(_USERNAMES)
    headers = _login(client, username)
    user_id = client.get("/api/me", headers=headers).json()["id"]
    return headers, user_id, username


def _make_group(client: TestClient, headers: dict, name: str = "组") -> str:
    response = client.post("/api/groups", json={"name": name}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _make_notebook(client: TestClient, headers: dict, name: str = "库") -> str:
    return client.post("/api/notebooks", json={"name": name}, headers=headers).json()["id"]


def _add_member(client, admin_headers, group_id, user_id, role="member"):
    return client.put(
        f"/api/groups/{group_id}/members/{user_id}",
        json={"role": role},
        headers=admin_headers,
    )


def _submit(client, headers, notebook_id, group_id):
    return client.post(
        f"/api/notebooks/{notebook_id}/share-requests",
        json={"group_id": group_id},
        headers=headers,
    )


def _pending_for(client, admin_headers, group_id):
    return client.get(
        f"/api/groups/{group_id}/share-requests", headers=admin_headers
    )


def _make_member_owned_notebook(client):
    """常见舞台:boss 建组管理组;librarian 拥有一本库、是组的**普通成员**。

    返回 (boss_headers, librarian_headers, librarian_id, group_id, notebook_id)。
    """
    boss, _boss_id, _ = _new_user(client)
    group_id = _make_group(client, boss, name="芯片项目")
    librarian, librarian_id, _ = _new_user(client)
    notebook_id = _make_notebook(client, librarian, name="共享库")
    assert _add_member(client, boss, group_id, librarian_id).status_code == 200
    return boss, librarian, librarian_id, group_id, notebook_id


# --------------------------------------------------------------------- 创建


def test_a_member_submits_and_a_group_admin_approves_the_whole_flow(tmp_path, monkeypatch):
    """端到端:普通成员申请 → 组管理员在队列里看到 → 批准 → 授权边落库、整组可读。"""
    client = _client(tmp_path, monkeypatch)
    boss, librarian, librarian_id, group_id, notebook_id = _make_member_owned_notebook(client)

    submitted = _submit(client, librarian, notebook_id, group_id)
    assert submitted.status_code == 200, submitted.text
    body = submitted.json()
    assert body["status"] == "pending"
    assert body["notebook_id"] == notebook_id
    assert body["group_id"] == group_id
    assert body["requested_by"] == librarian_id
    # decided_at 两态之一:pending 时必须是 null(绝不是空串)。
    assert body["decided_at"] is None
    assert body["decided_by"] is None
    request_id = body["id"]

    # 组管理员的审核队列里看得到,含库名与申请者用户名。
    queue = _pending_for(client, boss, group_id)
    assert queue.status_code == 200
    assert [r["id"] for r in queue.json()] == [request_id]
    assert queue.json()[0]["notebook_name"] == "共享库"

    # 库还没共享:另一个组成员看不到它。
    member, member_id, _ = _new_user(client)
    _add_member(client, boss, group_id, member_id)
    assert notebook_id not in {n["id"] for n in client.get("/api/notebooks", headers=member).json()}

    # 批准 → 授权边写入 + 状态置 approved(同一事务)。
    approved = client.post(
        f"/api/groups/{group_id}/share-requests/{request_id}/approve", headers=boss
    )
    assert approved.status_code == 200, approved.text
    decided = approved.json()
    assert decided["status"] == "approved"
    assert decided["decided_by"] == client.get("/api/me", headers=boss).json()["id"]
    # 已决定 → decided_at 是非空 ISO 时间戳(不是空串)。
    assert isinstance(decided["decided_at"], str) and decided["decided_at"]

    # 授权边真的落了:整组成员现在读得到,带 granted_via 来源。
    listed = {n["id"]: n for n in client.get("/api/notebooks", headers=member).json()}
    assert notebook_id in listed
    assert listed[notebook_id]["access"] == "reader"
    assert listed[notebook_id]["granted_via"] == [
        {"group_id": group_id, "group_name": "芯片项目", "kind": "project"}
    ]

    # 队列清空;申请者的自查里这条已是 approved。
    assert _pending_for(client, boss, group_id).json() == []
    mine = client.get(f"/api/notebooks/{notebook_id}/share-requests", headers=librarian).json()
    assert [r["status"] for r in mine] == ["approved"]


def test_create_needs_both_notebook_manage_and_group_membership(tmp_path, monkeypatch):
    """双重条件:对库有管理权 **且** 是目标组成员,缺哪一半各有各自的响应。"""
    client = _client(tmp_path, monkeypatch)
    boss, _boss_id, _ = _new_user(client)
    group_id = _make_group(client, boss)

    # 有库管理权(owner),但不是组成员 → 404(不泄露组的存在性)。
    librarian, librarian_id, _ = _new_user(client)
    notebook_id = _make_notebook(client, librarian)
    lacking_membership = _submit(client, librarian, notebook_id, group_id)
    assert lacking_membership.status_code == 404
    assert lacking_membership.json()["detail"] == "群组不存在"

    # 是组成员,但对别人的库没有管理权 → 404(不泄露库的存在性)。
    _add_member(client, boss, group_id, librarian_id)
    outsider_notebook = _make_notebook(client, boss, name="boss的库")
    lacking_manage = _submit(client, librarian, outsider_notebook, group_id)
    assert lacking_manage.status_code == 404

    # 两半都有 → 200。
    both = _submit(client, librarian, notebook_id, group_id)
    assert both.status_code == 200, both.text


def test_duplicate_pending_request_is_idempotent_not_an_error(tmp_path, monkeypatch):
    """撞 `uq_share_requests_one_pending` 返回既有 pending 行(裁决 P2-5),不是 409。"""
    client = _client(tmp_path, monkeypatch)
    boss, librarian, _lid, group_id, notebook_id = _make_member_owned_notebook(client)

    first = _submit(client, librarian, notebook_id, group_id)
    second = _submit(client, librarian, notebook_id, group_id)
    assert first.status_code == 200 and second.status_code == 200
    # 同一条 pending,不是两条。
    assert first.json()["id"] == second.json()["id"]
    assert len(_pending_for(client, boss, group_id).json()) == 1


def test_submitting_into_a_deleted_group_is_a_group_shaped_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _boss, librarian, _lid, _group_id, notebook_id = _make_member_owned_notebook(client)
    gone = _submit(client, librarian, notebook_id, "grp-nope")
    assert gone.status_code == 404
    assert gone.json()["detail"] == "群组不存在"


# --------------------------------------------------------------------- 批准


def test_approve_is_idempotent_when_the_notebook_is_already_shared(tmp_path, monkeypatch):
    """批准一条 pending 时若那本库已经共享给本组(边已在),不能报错——静默复用边,
    继续把申请标 approved,否则会留下一条永远批不掉的申请。"""
    client = _client(tmp_path, monkeypatch)
    boss, librarian, librarian_id, group_id, notebook_id = _make_member_owned_notebook(client)

    request_id = _submit(client, librarian, notebook_id, group_id).json()["id"]
    # 期间 librarian 被提为组管理员并直接发了边。
    _add_member(client, boss, group_id, librarian_id, role="admin")
    direct = client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={"principal_type": "group", "principal_id": group_id, "role": "viewer"},
        headers=librarian,
    )
    assert direct.status_code == 200, direct.text

    approved = client.post(
        f"/api/groups/{group_id}/share-requests/{request_id}/approve", headers=boss
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    # 仍只有一条 (group, viewer) 边,没有因幂等重复插入。
    grants = client.get(f"/api/notebooks/{notebook_id}/grants", headers=librarian).json()
    viewer_edges = [g for g in grants if g["principal_type"] == "group" and g["role"] == "viewer"]
    assert len(viewer_edges) == 1


def test_approving_or_rejecting_twice_is_a_404_not_a_second_transition(tmp_path, monkeypatch):
    """状态机单向:已决定的申请再批/驳 → 404(store 精确匹配 status='pending' 才动)。

    这也钉住并发双审:第二次动作(串行等价于并发的后到者)看到 status 已非 pending,
    返回 None → 404,绝不第二次改状态或第二次发边。"""
    client = _client(tmp_path, monkeypatch)
    boss, librarian, _lid, group_id, notebook_id = _make_member_owned_notebook(client)
    request_id = _submit(client, librarian, notebook_id, group_id).json()["id"]

    assert client.post(
        f"/api/groups/{group_id}/share-requests/{request_id}/approve", headers=boss
    ).status_code == 200
    again = client.post(
        f"/api/groups/{group_id}/share-requests/{request_id}/approve", headers=boss
    )
    assert again.status_code == 404
    rejected_after = client.post(
        f"/api/groups/{group_id}/share-requests/{request_id}/reject", headers=boss
    )
    assert rejected_after.status_code == 404


def test_approve_reject_and_queue_are_group_admin_only(tmp_path, monkeypatch):
    """审批面对非组管理员一律 404(群组可见性口径)。"""
    client = _client(tmp_path, monkeypatch)
    boss, librarian, librarian_id, group_id, notebook_id = _make_member_owned_notebook(client)
    request_id = _submit(client, librarian, notebook_id, group_id).json()["id"]

    # 普通成员(librarian 自己)读不到审核队列,也审批不了。
    assert _pending_for(client, librarian, group_id).status_code == 404
    assert client.post(
        f"/api/groups/{group_id}/share-requests/{request_id}/approve", headers=librarian
    ).status_code == 404
    assert client.post(
        f"/api/groups/{group_id}/share-requests/{request_id}/reject", headers=librarian
    ).status_code == 404

    # 完全不相干的人同样 404。
    stranger, _sid, _ = _new_user(client)
    assert _pending_for(client, stranger, group_id).status_code == 404


def test_unknown_request_id_is_a_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    boss, _librarian, _lid, group_id, _nb = _make_member_owned_notebook(client)
    assert client.post(
        f"/api/groups/{group_id}/share-requests/shr-nope/approve", headers=boss
    ).status_code == 404
    assert client.post(
        f"/api/groups/{group_id}/share-requests/shr-nope/reject", headers=boss
    ).status_code == 404


# --------------------------------------------------------------------- 驳回


def test_reject_writes_no_grant_and_lets_the_member_re_apply(tmp_path, monkeypatch):
    """驳回置 rejected、不发边;rejected 不占 pending 唯一索引,可重新申请。"""
    client = _client(tmp_path, monkeypatch)
    boss, librarian, _lid, group_id, notebook_id = _make_member_owned_notebook(client)
    request_id = _submit(client, librarian, notebook_id, group_id).json()["id"]

    rejected = client.post(
        f"/api/groups/{group_id}/share-requests/{request_id}/reject", headers=boss
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert isinstance(rejected.json()["decided_at"], str) and rejected.json()["decided_at"]

    # 没有发出任何授权边。
    assert client.get(f"/api/notebooks/{notebook_id}/grants", headers=librarian).json() == []
    # 队列清空;可以重新申请(新的 pending)。
    assert _pending_for(client, boss, group_id).json() == []
    reapply = _submit(client, librarian, notebook_id, group_id)
    assert reapply.status_code == 200
    assert reapply.json()["id"] != request_id
    assert reapply.json()["status"] == "pending"


# --------------------------------------------------------------------- 撤回


def test_withdraw_deletes_a_pending_request(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    boss, librarian, _lid, group_id, notebook_id = _make_member_owned_notebook(client)
    request_id = _submit(client, librarian, notebook_id, group_id).json()["id"]

    withdrawn = client.delete(
        f"/api/notebooks/{notebook_id}/share-requests/{request_id}", headers=librarian
    )
    assert withdrawn.status_code == 204
    assert _pending_for(client, boss, group_id).json() == []
    # 再撤一次 → 404(行已不在)。
    assert client.delete(
        f"/api/notebooks/{notebook_id}/share-requests/{request_id}", headers=librarian
    ).status_code == 404


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_withdrawing_a_decided_request_is_409(tmp_path, monkeypatch, decision):
    """已批准 **或** 已驳回的申请都不能撤回:撤回是申请者的动作,已决定的不可回退
    (裁决 P2-2)。两种终态都测,顺带钉住撤回门是**正向** `== 'pending'` 放行——
    否定式(如 `!= 'approved'`)会漏放 rejected。"""
    client = _client(tmp_path, monkeypatch)
    boss, librarian, _lid, group_id, notebook_id = _make_member_owned_notebook(client)
    request_id = _submit(client, librarian, notebook_id, group_id).json()["id"]
    client.post(
        f"/api/groups/{group_id}/share-requests/{request_id}/{decision}", headers=boss
    )

    conflict = client.delete(
        f"/api/notebooks/{notebook_id}/share-requests/{request_id}", headers=librarian
    )
    assert conflict.status_code == 409, decision
    assert conflict.headers.get("X-User-Message") == "1"


def test_withdraw_needs_manage_on_that_exact_notebook(tmp_path, monkeypatch):
    """撤回要求对**这本库**有管理权,且 request 必须属于它——防止「一本库的管理权」
    变成「能撤任何库上的任何申请」。"""
    client = _client(tmp_path, monkeypatch)
    boss, librarian, _lid, group_id, notebook_id = _make_member_owned_notebook(client)
    request_id = _submit(client, librarian, notebook_id, group_id).json()["id"]

    # 攻击者拿自己的一本库,试图用它的路径撤别人库上的申请 → 按 notebook_id 验,404。
    attacker, _aid, _ = _new_user(client)
    attacker_notebook = _make_notebook(client, attacker)
    assert client.delete(
        f"/api/notebooks/{attacker_notebook}/share-requests/{request_id}", headers=attacker
    ).status_code == 404
    # 真正的 pending 没被动到。
    assert len(_pending_for(client, boss, group_id).json()) == 1


# --------------------------------------------------------------------- 孤儿治理


def test_deleting_the_group_cascades_share_requests(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    boss, librarian, _lid, group_id, notebook_id = _make_member_owned_notebook(client)
    _submit(client, librarian, notebook_id, group_id)

    assert client.delete(f"/api/groups/{group_id}", headers=boss).status_code == 204
    # 组没了,申请也 CASCADE 消失(申请者的自查为空)。
    assert client.get(
        f"/api/notebooks/{notebook_id}/share-requests", headers=librarian
    ).json() == []


# --------------------------------------------------------------------- store 层


def _seed_users(repo, *user_ids):
    with repo._write() as db:
        for uid in user_ids:
            db.execute(
                "INSERT INTO users "
                "(id,email,display_name,username,password_hash,role,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (uid, f"{uid}@t", uid, uid, "x", "user", _now(), _now()),
            )


def _seed_notebook(repo, notebook_id, owner_id):
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (notebook_id, "库", owner_id, "ready", _now(), _now()),
        )


def test_decided_at_is_null_while_pending_and_iso_once_decided(repo):
    """`decided_at` 两态断言(红线):pending → SQL NULL,已决定 → 非空 ISO,绝不是空串。

    走 store:两态的**写路径**由 `assert_share_request_decided_at` 在每次行投影时复核,
    这条测试从外部再钉一遍,免得断言本身被误删也无人察觉。"""
    store = repo._runtime.groups
    _seed_users(repo, "sr-owner", "sr-admin")
    _seed_notebook(repo, "nb-sr", "sr-owner")
    group = store.create_group(name="g", kind="project", description="", created_by="sr-admin")

    created = store.create_share_request("nb-sr", group_id=group["id"], requested_by="sr-owner")
    assert created["status"] == "pending"
    assert created["decided_at"] is None  # 决不是 ""

    decided = store.approve_share_request(group["id"], created["id"], decided_by="sr-admin")
    assert decided["status"] == "approved"
    assert isinstance(decided["decided_at"], str) and decided["decided_at"]
    assert decided["decided_by"] == "sr-admin"


def test_the_partial_unique_index_holds_at_the_store_layer(repo):
    """同库同组的第二次 pending 申请撞唯一索引 → 幂等返回既有行,不抛也不重复。"""
    store = repo._runtime.groups
    _seed_users(repo, "u-o", "u-a")
    _seed_notebook(repo, "nb-u", "u-o")
    group = store.create_group(name="g", kind="project", description="", created_by="u-a")

    first = store.create_share_request("nb-u", group_id=group["id"], requested_by="u-o")
    second = store.create_share_request("nb-u", group_id=group["id"], requested_by="u-o")
    assert first["id"] == second["id"]
    assert len(store.list_pending_share_requests(group["id"])) == 1

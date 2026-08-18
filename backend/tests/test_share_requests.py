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


def _promote_to_system_admin(repo, user_id: str) -> None:
    """把用户提成**系统**管理员(`users.role`,与组内 role 两条轴,别混用)。"""
    with repo._write() as db:
        db.execute("UPDATE users SET role='admin' WHERE id=?", (user_id,))


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


def test_a_group_admin_is_sent_to_the_direct_share_path_not_the_approval_queue(
    tmp_path, monkeypatch
):
    """目标组的**组管理员**不能提交共享申请(codex #519 R8 P2)。

    审批流覆盖的是**另一半**入口:对库有管理权、但对目标组只是普通成员的人。组管理员
    分享进自己管理的组永远走 `POST /notebooks/{id}/grants`、不经这张表(设计 §4 决策 9,
    v49/v50 迁移的 docstring 也逐字写着)。此前判据只是「有没有成员行」,于是组管理员能
    建出一条 pending 申请再**自己批自己**——契约早就写明了,只是实现没兑现。

    响应刻意**不是** 404:他是这个组的管理员,组的存在性对他不是秘密;给一句可操作的
    说明,告诉他直接共享即可。
    """
    client = _client(tmp_path, monkeypatch)
    boss, boss_id, _ = _new_user(client)
    group_id = _make_group(client, boss, name="他管的组")
    notebook_id = _make_notebook(client, boss, name="他自己的库")

    denied = _submit(client, boss, notebook_id, group_id)
    assert denied.status_code == 403, denied.text
    assert denied.headers.get("X-User-Message") == "1"
    assert denied.json()["detail"] == "你是这个群组的组管理员,直接共享给它即可,不必提交申请"
    # 一条申请都不该落库——否则他下一步就能自己批准自己。
    assert _pending_for(client, boss, group_id).json() == []

    # 反向护栏:同一个人走**正确**的那条路(直接发边)照常成功。
    granted = client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={"principal_type": "group", "principal_id": group_id, "role": "viewer"},
        headers=boss,
    )
    assert granted.status_code == 200, granted.text


def test_being_promoted_to_group_admin_in_the_toctou_window_blocks_the_request(
    tmp_path, monkeypatch
):
    """普通成员在路由前置检查之后、写事务之前被**提升为组管理员** → 事务内复检拦下。

    与 R2 P2-1 那条(被移出组)同一条纪律的另一格:承重判定在写事务里,路由那次只给文案。
    只改路由那半,这个窗口里落下的申请仍然是一条「组管理员给自己开的」待审批申请。
    """
    client = _client(tmp_path, monkeypatch)
    boss, librarian, librarian_id, group_id, notebook_id = _make_member_owned_notebook(client)

    store = _app_group_store()
    original = store.user_group_role

    def promote_then_answer(gid, uid):
        role = original(gid, uid)
        if gid == group_id and uid == librarian_id:
            # 守卫刚读到 "member",紧接着他被提升——写事务尚未开始。
            store.upsert_member(gid, librarian_id, role="admin", added_by=librarian_id)
        return role

    monkeypatch.setattr(store, "user_group_role", promote_then_answer)
    denied = _submit(client, librarian, notebook_id, group_id)
    monkeypatch.setattr(store, "user_group_role", original)
    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"] == "你是这个群组的组管理员,直接共享给它即可,不必提交申请"
    assert _pending_for(client, boss, group_id).json() == []


def test_store_level_share_request_requires_plain_membership(repo):
    """直接走 store:非成员 → `GroupMembershipRequiredError`;组管理员 →
    `GroupAdminShouldShareDirectlyError`。两种不合格给**不同**的异常,因为路由要据此
    分出 404(不泄露存在性)与 403(给可操作说明)两种响应。

    放行是**正向精确匹配** `role == 'member'`:未知取值(正向 shadow 停车写进 `role` 的
    哨兵串就是这一类)落进第二支、一律不放行,方向 fail closed。写成 `!= 'admin'` 就反了。
    """
    from app.repositories.ports import (
        GroupAdminShouldShareDirectlyError,
        GroupMembershipRequiredError,
    )

    store = repo._runtime.groups
    _seed_users(repo, "pm-admin", "pm-member", "pm-outsider")
    _seed_notebook(repo, "nb-pm", "pm-member")
    group = store.create_group(
        name="g", kind="project", description="", created_by="pm-admin"
    )
    store.upsert_member(group["id"], "pm-member", role="member", added_by="pm-admin")

    with pytest.raises(GroupMembershipRequiredError):
        store.create_share_request("nb-pm", group_id=group["id"], requested_by="pm-outsider")
    with pytest.raises(GroupAdminShouldShareDirectlyError):
        store.create_share_request("nb-pm", group_id=group["id"], requested_by="pm-admin")
    # 未知 role(哨兵)同样不放行——判据是正向匹配 'member',不是「不等于 admin」。
    with repo._write() as db:
        db.execute(
            "UPDATE group_members SET role=? WHERE group_id=? AND user_id=?",
            ("__parked_sentinel__", group["id"], "pm-member"),
        )
    with pytest.raises(GroupAdminShouldShareDirectlyError):
        store.create_share_request("nb-pm", group_id=group["id"], requested_by="pm-member")
    # 恢复成普通成员 → 照常放行(复检不能变成恒关的闸)。
    with repo._write() as db:
        db.execute(
            "UPDATE group_members SET role='member' WHERE group_id=? AND user_id=?",
            (group["id"], "pm-member"),
        )
    assert store.create_share_request(
        "nb-pm", group_id=group["id"], requested_by="pm-member"
    )["status"] == "pending"


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


def test_another_managers_pending_request_is_a_conflict_not_a_silent_handover(
    tmp_path, monkeypatch
):
    """幂等**只对本人成立**(codex #519 R3):别人已提过 → 409,不把他的行返回给我。

    一本库可以有多个管理权持有者(owner + 组管理员)。把第一个人的申请行原样返回给第二
    个人,会让三件事同时成立而互相矛盾:他的界面报成功、`list_my_share_requests` 里查
    不到、那条申请他也撤不掉,于是那个组对他永远显示「可申请」。
    """
    client = _client(tmp_path, monkeypatch)
    boss, boss_id, _ = _new_user(client)
    group_id = _make_group(client, boss, name="芯片项目")
    # 另一个组、boss 自己的库:两个组管理员都对它有 manage(owner + group_admins 边),
    # 于是「同一 (库,组) 上两个不同申请者」这个形态成立。
    other_group = _make_group(client, boss, name="另一个组")
    notebook_id = _make_notebook(client, boss, name="共享库")
    deputy, deputy_id, _ = _new_user(client)
    _add_member(client, boss, other_group, deputy_id, role="admin")
    shared = client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={"principal_type": "group_admins", "principal_id": other_group, "role": "admin"},
        headers=boss,
    )
    assert shared.status_code == 200, shared.text
    # 两人都只是**目标组** `group_id` 的普通成员 —— 所以都得走申请这条路。
    _add_member(client, boss, group_id, deputy_id, role="member")
    demote = client.put(
        f"/api/groups/{group_id}/members/{boss_id}", json={"role": "member"}, headers=boss
    )
    assert demote.status_code == 409  # 唯一组管理员不能自我降级,补一个再降
    third, third_id, _ = _new_user(client)
    _add_member(client, boss, group_id, third_id, role="admin")
    assert client.put(
        f"/api/groups/{group_id}/members/{boss_id}", json={"role": "member"}, headers=boss
    ).status_code == 200

    first = _submit(client, boss, notebook_id, group_id)
    assert first.status_code == 200, first.text

    conflict = _submit(client, deputy, notebook_id, group_id)
    assert conflict.status_code == 409, conflict.text
    assert conflict.headers.get("X-User-Message") == "1"
    # 文案不点名申请者。
    detail = conflict.json()["detail"]
    assert "申请" in detail
    assert boss_id not in detail
    # 第一个人的申请分毫未动,仍归他所有;第二个人的自查列表是空的(没有凭空多一条)。
    assert [r["id"] for r in client.get(
        f"/api/notebooks/{notebook_id}/share-requests", headers=boss
    ).json()] == [first.json()["id"]]
    assert client.get(
        f"/api/notebooks/{notebook_id}/share-requests", headers=deputy
    ).json() == []
    assert len(_pending_for(client, third, group_id).json()) == 1


def test_a_pending_request_decided_during_conflict_recovery_retries_instead_of_500(
    tmp_path, monkeypatch
):
    """撞唯一违例后、回读之前那条 pending 被决定 → **重试插入**,不让 DB 异常冒成 500。

    注入式:让恢复期的 `_pending_share_request` 回读在第一次被调用时先把那条 pending
    批准掉再回答 —— 回读因此得 None(部分唯一索引的谓词只覆盖 pending)。此时正确行为
    是重试插入并成功,而不是把原始 UniqueViolation/IntegrityError 抛给用户(codex #519 R3)。
    """
    client = _client(tmp_path, monkeypatch)
    boss, librarian, _lid, group_id, notebook_id = _make_member_owned_notebook(client)
    first_id = _submit(client, librarian, notebook_id, group_id).json()["id"]

    boss_id = client.get("/api/me", headers=boss).json()["id"]
    store = _app_group_store()
    original = store._pending_share_request
    calls: list[int] = []

    def decide_then_answer(nb_id, gid):
        if not calls:
            calls.append(1)
            # 恢复窗口里那条 pending 被批准 —— 回读将得 None。
            store.approve_share_request(gid, first_id, decided_by=boss_id)
        return original(nb_id, gid)

    monkeypatch.setattr(store, "_pending_share_request", decide_then_answer)
    retried = _submit(client, librarian, notebook_id, group_id)
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "pending"
    assert retried.json()["id"] != first_id
    assert calls, "注入未生效——本用例没有真的走到冲突恢复路径"


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


def test_another_manager_cannot_withdraw_someone_elses_request(tmp_path, monkeypatch):
    """撤回只属于**申请者本人**——同一本库上的另一位管理权持有者撤不掉别人的申请。

    能力守卫只证明「这个人对这本库有管理权」,证明不了「这条申请是他提的」。P2 之后
    一本库可以同时有多个管理权持有者(owner + 经 `group_admins` 边的组管理员),丢掉
    `requested_by` 谓词就等于让他们互相撤回对方的待审批申请(codex #519 R1 P1)。
    别人的申请与不存在的申请同样落 **404**,不泄露「这本库上有一条别人的待审批申请」。
    """
    client = _client(tmp_path, monkeypatch)
    boss, librarian, _lid, group_id, notebook_id = _make_member_owned_notebook(client)
    request_id = _submit(client, librarian, notebook_id, group_id).json()["id"]

    # deputy:librarian 建一个自己管理的组、把 deputy 提为该组的组管理员,再发一条
    # `(group_admins, admin)` 边 —— 于是 deputy 对这本库有 notebook:manage(P2 六格
    # 之一),但那条申请不是他提的。发边要求发起者同时是目标组的组管理员(P1 双重
    # 条件),所以这条边必须由建组的 librarian 自己发。
    deputy, deputy_id, _ = _new_user(client)
    deputy_group = _make_group(client, librarian, name="受托组")
    assert _add_member(
        client, librarian, deputy_group, deputy_id, role="admin"
    ).status_code == 200
    granted = client.post(
        f"/api/notebooks/{notebook_id}/grants",
        headers=librarian,
        json={
            "principal_type": "group_admins",
            "principal_id": deputy_group,
            "role": "admin",
        },
    )
    assert granted.status_code in (200, 201), granted.text

    # deputy 确实拿到了管理权(否则本用例证明不了它拦的是「不是申请者」而非「没权限」)。
    assert client.patch(
        f"/api/notebooks/{notebook_id}", headers=deputy, json={"name": "受托改名"}
    ).status_code == 200

    stolen = client.delete(
        f"/api/notebooks/{notebook_id}/share-requests/{request_id}", headers=deputy
    )
    assert stolen.status_code == 404, stolen.text
    # 申请仍在,申请者本人仍可撤。
    assert [r["id"] for r in _pending_for(client, boss, group_id).json()] == [request_id]
    assert client.delete(
        f"/api/notebooks/{notebook_id}/share-requests/{request_id}", headers=librarian
    ).status_code == 204


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


def _my_pending(client, headers):
    return client.get("/api/me/share-requests", headers=headers)


def test_a_requester_who_lost_manage_rights_can_still_find_and_withdraw(
    tmp_path, monkeypatch
):
    """裁决 P2-7 的另一半:失权申请人必须**够得着**自己的申请(codex #519 R11 P1)。

    撤回刻意只认「这条申请是你提的」,但按笔记本列申请那条挂 `notebook:manage`——申请人
    一失权就拿不到申请 id,那个口子在**它唯一存在意义的场景**里等于没开。全局入口
    `GET /me/share-requests` 补上这一半:唯一谓词是 `requested_by`,与 DELETE 逐字相同。

    舞台:Bob 经 `group_admins` 边对 Alice 的库有管理权 → 提交申请 → Alice 撤掉那条边。
    此后他对那本库**连读权都没有**。
    """
    client = _client(tmp_path, monkeypatch)
    alice, _alice_id, _ = _new_user(client)
    notebook_id = _make_notebook(client, alice, name="Alice 的库")
    bob, bob_id, _ = _new_user(client)
    conferring = _make_group(client, alice, name="授权组")
    assert _add_member(client, alice, conferring, bob_id, role="admin").status_code == 200
    edge = client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={"principal_type": "group_admins", "principal_id": conferring, "role": "admin"},
        headers=alice,
    )
    assert edge.status_code == 200, edge.text
    carol, _carol_id, _ = _new_user(client)
    target = _make_group(client, carol, name="Carol 的组")
    assert _add_member(client, carol, target, bob_id).status_code == 200
    request_id = _submit(client, bob, notebook_id, target).json()["id"]

    # Alice 收回管理边 —— Bob 从此对这本库一无所有。
    assert client.delete(
        f"/api/notebooks/{notebook_id}/grants/{edge.json()['id']}", headers=alice
    ).status_code == 204
    # 笔记本维度那条(manage 门)现在对他关着 —— 这正是 R11 P1 描述的处境。
    assert client.get(
        f"/api/notebooks/{notebook_id}/share-requests", headers=bob
    ).status_code == 404

    # 全局入口仍然给得出这条申请,且带着撤回所需的两个 id。
    mine = _my_pending(client, bob)
    assert mine.status_code == 200, mine.text
    assert [r["id"] for r in mine.json()] == [request_id]
    row = mine.json()[0]
    assert row["notebook_id"] == notebook_id
    assert row["notebook_name"] == "Alice 的库"   # 有意披露,见路由 docstring
    assert row["status"] == "pending"
    assert row["decided_by"] is None and row["decided_at"] is None

    # 而且撤得掉 —— 这才是整条裁决要兑现的动作。
    assert client.delete(
        f"/api/notebooks/{notebook_id}/share-requests/{request_id}", headers=bob
    ).status_code == 204
    assert _my_pending(client, bob).json() == []
    assert _pending_for(client, carol, target).json() == []


def test_the_global_request_list_is_scoped_to_the_requester_and_to_pending(
    tmp_path, monkeypatch
):
    """全局入口的两条收窄:**只回自己发起的**、**只回待审批的**。

    前者是授权轴(否则它就成了「谁提过申请」的全站清单);后者是披露面——已决定的申请撤不
    回来,列出来既没有可做的动作、又平白多披露一份历史(连同审批者身份)。
    """
    client = _client(tmp_path, monkeypatch)
    boss, librarian, _lid, group_id, notebook_id = _make_member_owned_notebook(client)
    mine_id = _submit(client, librarian, notebook_id, group_id).json()["id"]

    # 另一个人对自己的库、同一个组也提一条 —— 绝不能出现在我的清单里。
    other, other_id, _ = _new_user(client)
    other_nb = _make_notebook(client, other, name="别人的库")
    assert _add_member(client, boss, group_id, other_id).status_code == 200
    other_id_req = _submit(client, other, other_nb, group_id).json()["id"]

    assert [r["id"] for r in _my_pending(client, librarian).json()] == [mine_id]
    assert [r["id"] for r in _my_pending(client, other).json()] == [other_id_req]
    # 组管理员自己没提过申请 —— 哪怕他能看见整个队列,这条清单也是空的。
    assert _my_pending(client, boss).json() == []
    assert len(_pending_for(client, boss, group_id).json()) == 2

    # 被驳回之后从这条清单消失(它撤不回来了)。
    assert client.post(
        f"/api/groups/{group_id}/share-requests/{mine_id}/reject", headers=boss
    ).status_code == 200
    assert _my_pending(client, librarian).json() == []
    # 但笔记本维度那条仍然回显「已驳回」(那是给有管理权的人看的历史)。
    assert [r["status"] for r in client.get(
        f"/api/notebooks/{notebook_id}/share-requests", headers=librarian
    ).json()] == ["rejected"]


def test_a_requester_who_lost_manage_rights_can_still_withdraw(tmp_path, monkeypatch):
    """撤回按**申请归属**授权,不按当前笔记本权限(codex #519 R6 P2)。

    R4 之后批准会拒绝「申请人已失权」的申请;撤回若也要求管理权,这类申请就**既批不了
    也撤不掉**,永远卡在组管理员的队列里。两条裁决互补:一个防陈旧授权被兑现,一个保证
    申请人始终能收回自己的提议。
    """
    client = _client(tmp_path, monkeypatch)
    alice, _alice_id, _ = _new_user(client)
    notebook_id = _make_notebook(client, alice, name="Alice 的库")
    grantor_group = _make_group(client, alice, name="授权组")
    bob, bob_id, _ = _new_user(client)
    _add_member(client, alice, grantor_group, bob_id, role="admin")
    client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={"principal_type": "group_admins", "principal_id": grantor_group, "role": "admin"},
        headers=alice,
    )
    carol, _carol_id, _ = _new_user(client)
    target_group = _make_group(client, carol, name="G1")
    _add_member(client, carol, target_group, bob_id, role="member")
    request_id = _submit(client, bob, notebook_id, target_group).json()["id"]

    # 库主撤掉 Bob 的管理边 —— 他对这本库什么权限都没有了。
    for g in client.get(f"/api/notebooks/{notebook_id}/grants", headers=alice).json():
        client.delete(f"/api/notebooks/{notebook_id}/grants/{g['id']}", headers=alice)
    # 连读都读不到了(所以也不能改挂 require_notebook_read)。
    assert client.get(f"/api/notebooks/{notebook_id}", headers=bob).status_code == 404

    # 但他仍能撤回**自己**那条申请。
    withdrawn = client.delete(
        f"/api/notebooks/{notebook_id}/share-requests/{request_id}", headers=bob
    )
    assert withdrawn.status_code == 204, withdrawn.text
    assert _pending_for(client, carol, target_group).json() == []


def test_withdraw_stays_owner_scoped_without_the_capability_guard(tmp_path, monkeypatch):
    """去掉能力依赖之后,三列谓词必须独自兜住:别人 404、跨库 404。"""
    client = _client(tmp_path, monkeypatch)
    boss, librarian, _lid, group_id, notebook_id = _make_member_owned_notebook(client)
    request_id = _submit(client, librarian, notebook_id, group_id).json()["id"]

    # 别人(哪怕他也对这本库有管理权——这里用库主本人)撤不掉:非本人 → 404。
    assert client.delete(
        f"/api/notebooks/{notebook_id}/share-requests/{request_id}", headers=boss
    ).status_code == 404
    # 完全不相干的登录用户同样 404,不泄露存在性。
    stranger, _sid, _ = _new_user(client)
    assert client.delete(
        f"/api/notebooks/{notebook_id}/share-requests/{request_id}", headers=stranger
    ).status_code == 404
    # 跨库拼 URL:notebook_id 仍在 WHERE 里 → 404。
    other_notebook = _make_notebook(client, librarian, name="另一本")
    assert client.delete(
        f"/api/notebooks/{other_notebook}/share-requests/{request_id}", headers=librarian
    ).status_code == 404
    # 申请分毫未动。
    assert [r["id"] for r in _pending_for(client, boss, group_id).json()] == [request_id]


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
    # 申请人必须真的是组成员——事务内的成员资格复核是无条件的(R2 P2-1)。
    store.upsert_member(group["id"], "sr-owner", role="member", added_by="sr-admin")

    created = store.create_share_request("nb-sr", group_id=group["id"], requested_by="sr-owner")
    assert created["status"] == "pending"
    assert created["decided_at"] is None  # 决不是 ""

    decided = store.approve_share_request(group["id"], created["id"], decided_by="sr-admin")
    assert decided["status"] == "approved"
    assert isinstance(decided["decided_at"], str) and decided["decided_at"]
    assert decided["decided_by"] == "sr-admin"


# ------------------------------------------------- 事务内的授权复检(TOCTOU)


def _app_group_store():
    """**应用**(TestClient)手上那个 GroupStore 实例。

    要在 HTTP 请求的执行路径中间插桩(模拟「守卫过了、写事务还没开」这个并发窗口),
    必须打应用自己那个实例的桩;打 `repo._runtime.groups` 的桩路由压根看不见,请求会
    一路成功,而测试看起来只是「断言写错了」(与 `test_group_routes.py` 同款)。
    """
    from app.api import deps

    return deps.repository()._runtime.groups


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_a_demoted_admin_cannot_decide_in_the_toctou_window(
    tmp_path, monkeypatch, decision
):
    """守卫过了、写事务还没开的窗口里被降级 → 审批/驳回必须被事务内复核拦下(403)。

    这正是 codex #519 R2 P1:批准会把**整组**的读权放出去,而 `_require_group_admin`
    与写事务之间的窗口足够让这个人被降级。窗口用桩模拟——降级动作插在守卫那次
    `user_group_role` 之后、store 写事务之前,与 `test_group_routes.py` 里模拟并发删组
    的手法逐字相同。
    """
    client = _client(tmp_path, monkeypatch)
    boss, librarian, _lid, group_id, notebook_id = _make_member_owned_notebook(client)
    request_id = _submit(client, librarian, notebook_id, group_id).json()["id"]

    # 再加一名组管理员,好让 boss 可以被合法降级(最后一名组管理员保护会拦住降级)。
    deputy, deputy_id, _ = _new_user(client)
    _add_member(client, boss, group_id, deputy_id, role="admin")
    boss_id = client.get("/api/me", headers=boss).json()["id"]

    store = _app_group_store()
    original = store.user_group_role

    def demote_then_answer(gid, uid):
        role = original(gid, uid)
        if gid == group_id and uid == boss_id:
            # 守卫刚读到 "admin",紧接着他被降级——写事务尚未开始。
            store.upsert_member(gid, boss_id, role="member", added_by=deputy_id)
        return role

    monkeypatch.setattr(store, "user_group_role", demote_then_answer)
    denied = client.post(
        f"/api/groups/{group_id}/share-requests/{request_id}/{decision}", headers=boss
    )
    assert denied.status_code == 403, denied.text
    assert denied.headers.get("X-User-Message") == "1"

    # 申请仍是 pending:被拒的决定一点副作用都不能留下。
    monkeypatch.setattr(store, "user_group_role", original)
    assert [r["id"] for r in _pending_for(client, deputy, group_id).json()] == [request_id]
    # 批准那一支还要证明**没有**把整组的读权放出去。
    assert client.get(f"/api/notebooks/{notebook_id}/grants", headers=librarian).json() == []


def test_approving_a_request_whose_author_lost_manage_rights_is_refused(
    tmp_path, monkeypatch
):
    """申请人在提交后失去管理权 → 批准必须被拒(409),且**零副作用**(R4 裁决变更)。

    场景是 codex #519 R4 的原话:Bob 经 `group_admins` 边对库 N 有管理权 → 提交申请 →
    库主 Alice 撤掉那条边 → 组管理员批准 → N 的读权发给整个 G1。Bob 早已失权、库主从未
    同意,而一条**活的**授权边就这么落库了。授权在生效时刻实时判定、绝不缓存。
    """
    client = _client(tmp_path, monkeypatch)
    alice, _alice_id, _ = _new_user(client)          # 库主
    notebook_id = _make_notebook(client, alice, name="Alice 的库")

    # Bob 经「授权组」的 group_admins 边拿到对该库的管理权。
    grantor_group = _make_group(client, alice, name="授权组")
    bob, bob_id, _ = _new_user(client)
    _add_member(client, alice, grantor_group, bob_id, role="admin")
    edge = client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={"principal_type": "group_admins", "principal_id": grantor_group, "role": "admin"},
        headers=alice,
    )
    assert edge.status_code == 200, edge.text

    # 目标组 G1:Bob 只是普通成员,所以只能申请;carol 是它的组管理员。
    carol, carol_id, _ = _new_user(client)
    target_group = _make_group(client, carol, name="G1")
    _add_member(client, carol, target_group, bob_id, role="member")

    request_id = _submit(client, bob, notebook_id, target_group).json()["id"]
    assert [r["id"] for r in _pending_for(client, carol, target_group).json()] == [request_id]

    # 库主撤掉 Bob 的管理边——他从此对这本库什么权限都没有。
    grants = client.get(f"/api/notebooks/{notebook_id}/grants", headers=alice).json()
    for g in grants:
        client.delete(f"/api/notebooks/{notebook_id}/grants/{g['id']}", headers=alice)

    refused = client.post(
        f"/api/groups/{target_group}/share-requests/{request_id}/approve", headers=carol
    )
    assert refused.status_code == 409, refused.text
    assert refused.headers.get("X-User-Message") == "1"
    assert "管理权" in refused.json()["detail"]

    # 零副作用:①没有任何授权边落库(整组读不到这本库);
    assert client.get(f"/api/notebooks/{notebook_id}/grants", headers=alice).json() == []
    # ②申请**保留**在待审批队列里(刻意不自动删,审计价值 > 清理);
    still = _pending_for(client, carol, target_group).json()
    assert [r["id"] for r in still] == [request_id]
    assert still[0]["status"] == "pending"
    assert still[0]["decided_by"] is None and still[0]["decided_at"] is None
    # ③G1 的成员确实读不到这本库。
    member, member_id, _ = _new_user(client)
    _add_member(client, carol, target_group, member_id, role="member")
    assert notebook_id not in {
        n["id"] for n in client.get("/api/notebooks", headers=member).json()
    }

    # 驳回**不做**这条复检:终止不产生授权,失权申请人的申请当然可以被驳回。
    rejected = client.post(
        f"/api/groups/{target_group}/share-requests/{request_id}/reject", headers=carol
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"


def test_losing_the_group_membership_behind_the_edge_also_revokes_manage_rights(
    tmp_path, monkeypatch
):
    """管理权来自 `group_admins` 边时,**撤掉组成员资格**与撤掉边等效(codex #519 R8 P1)。

    `_require_notebook_manage_on` 认的是一条**两环的生效链**:①那条 `notebook_grants`
    边;②让它生效的那行 `group_members`。既有用例(R4/R6)撤的都是第①环,这条撤第②环。
    两个消费点各测一次——`approve_share_request`(申请人)与 `create_grant`(发起人)——
    因为它们共用这一个谓词,而 PG 侧正是在这里只锁了第①环。

    ⚠ 这条是**确定性**用例(先移出组、再动作),证的是「谓词认不认第②环」;PG 侧「那把锁
    拦不拦得住并发移出组」由 `tests/postgres/test_concurrency.py` 的并发用例承担,两者互补。
    """
    client = _client(tmp_path, monkeypatch)
    alice, _alice_id, _ = _new_user(client)
    notebook_id = _make_notebook(client, alice, name="Alice 的库")
    bob, bob_id, _ = _new_user(client)
    # Bob 经 group_admins 边拿到管理权(而不是自己是库主)。
    conferring = _make_group(client, alice, name="授权组")
    assert _add_member(client, alice, conferring, bob_id, role="admin").status_code == 200
    assert client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={"principal_type": "group_admins", "principal_id": conferring, "role": "admin"},
        headers=alice,
    ).status_code == 200

    # Bob 自己管一个组(用来走发起人那条路),另有 Carol 管一个组、Bob 只是普通成员
    # (用来走申请人那条路)。
    bobs_group = _make_group(client, bob, name="Bob 的组")
    carol, _carol_id, _ = _new_user(client)
    carols_group = _make_group(client, carol, name="Carol 的组")
    assert _add_member(client, carol, carols_group, bob_id).status_code == 200
    request_id = _submit(client, bob, notebook_id, carols_group).json()["id"]

    # 前提:此刻两条路都是通的(否则下面的断言全是空转)。
    assert client.get(f"/api/notebooks/{notebook_id}/share-requests", headers=bob).status_code == 200

    # Alice 把 Bob 移出授权组——授权**边一个字没动**,只有第②环没了。
    assert client.delete(
        f"/api/groups/{conferring}/members/{bob_id}", headers=alice
    ).status_code == 204
    assert [g["principal_type"] for g in client.get(
        f"/api/notebooks/{notebook_id}/grants", headers=alice
    ).json()] == ["group_admins"], "边应当原样还在——本用例撤的是成员资格那一环"

    # ① 发起人侧(`create_grant`):他再也发不出新的授权边。
    denied = client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={"principal_type": "group", "principal_id": bobs_group, "role": "viewer"},
        headers=bob,
    )
    assert denied.status_code in (403, 404), denied.text

    # ② 申请人侧(`approve_share_request`):他此前提交的申请也兑现不了。
    refused = client.post(
        f"/api/groups/{carols_group}/share-requests/{request_id}/approve", headers=carol
    )
    assert refused.status_code == 409, refused.text
    assert "管理权" in refused.json()["detail"]
    # 零副作用:那条 (group, viewer) 边没有发出去。
    assert [g["principal_type"] for g in client.get(
        f"/api/notebooks/{notebook_id}/grants", headers=alice
    ).json()] == ["group_admins"]


def test_a_demoted_manager_cannot_still_hand_out_a_new_grant(tmp_path, monkeypatch):
    """`create_grant` 也要在事务内复核**发起人**的笔记本侧权限(codex #519 R6 P1)。

    能力守卫放行之后、写事务开始之前,库主可以撤掉发起人的管理边。少了这一次复核,失权者
    仍能发出一条**新的**授权边(而且可以发给另一个组),把访问权继续散出去——授权边的效力
    超出发起人自身权限的存续,这正是「授予他人访问权的写入必须事务内复检」那条裁决要防的。
    """
    client = _client(tmp_path, monkeypatch)
    alice, _alice_id, _ = _new_user(client)
    notebook_id = _make_notebook(client, alice, name="Alice 的库")
    grantor_group = _make_group(client, alice, name="授权组")
    bob, bob_id, _ = _new_user(client)
    _add_member(client, alice, grantor_group, bob_id, role="admin")
    edge = client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={"principal_type": "group_admins", "principal_id": grantor_group, "role": "admin"},
        headers=alice,
    )
    assert edge.status_code == 200, edge.text
    # Bob 另建一个组,准备把 Alice 的库再散给它。
    bobs_group = _make_group(client, bob, name="Bob 的组")

    # 注入点就是那个窗口本身:能力守卫(依赖层)**已经**放行,store 的写事务**尚未**开始。
    # 在 `create_grant` 进入之前撤掉 Bob 的边,正是「守卫读到有权 → 库主撤权 → 写事务落库」
    # 这条时序。
    store = _app_group_store()
    original_create = store.create_grant

    def revoke_then_create(*args, **kwargs):
        for g in client.get(
            f"/api/notebooks/{notebook_id}/grants", headers=alice
        ).json():
            client.delete(
                f"/api/notebooks/{notebook_id}/grants/{g['id']}", headers=alice
            )
        return original_create(*args, **kwargs)

    monkeypatch.setattr(store, "create_grant", revoke_then_create)
    denied = client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={"principal_type": "group", "principal_id": bobs_group, "role": "viewer"},
        headers=bob,
    )
    monkeypatch.setattr(store, "create_grant", original_create)
    assert denied.status_code == 403, denied.text
    assert denied.headers.get("X-User-Message") == "1"
    assert "管理权" in denied.json()["detail"]
    # 零副作用:一条边都没散出去。
    assert client.get(f"/api/notebooks/{notebook_id}/grants", headers=alice).json() == []


def test_granting_into_a_notebook_deleted_in_the_toctou_window_is_a_404(
    tmp_path, monkeypatch
):
    """`create_grant` 的**外键父行**复核(codex #519 R7 存疑项收口)。

    与上面那条同一个注入窗口(能力守卫已放行、store 写事务未开),但换一个维度:那条撤掉
    发起人的**权限**,这条删掉**笔记本本身**。少了 `_require_notebook_on` /
    `_lock_notebook_on`:

    * PG 侧 —— `_require_notebook_manage_on` 的 owner 半是一条**无锁** SELECT 且当场短路,
      删库若提交在它与 INSERT 之间,`notebook_grants.notebook_id` 外键违例,而
      `create_grant` 只 catch `UniqueViolation` → **500**;
    * SQLite 侧 —— 进程写锁保证删库插不进事务中间,两半都查不到 → 抛的是
      `NotebookManageRequiredError` → **403「你已不再拥有这本笔记本的管理权」**。不是 500,
      但在库已经不存在时是一句误导,而且与 PG 修好之后的 404 分叉。

    所以两个后端都补,这条钉的是**它们答同一句话**。真并发那一半(锁在不在承重)由
    `tests/postgres/test_concurrency.py` 的 PG 用例承担。
    """
    from app.api import deps

    client = _client(tmp_path, monkeypatch)
    alice, _alice_id, _ = _new_user(client)
    notebook_id = _make_notebook(client, alice, name="Alice 的库")
    group_id = _make_group(client, alice, name="组")

    store = _app_group_store()
    original_create = store.create_grant

    def drop_notebook_then_create(*args, **kwargs):
        with deps.repository()._write() as db:
            db.execute("DELETE FROM notebooks WHERE id=?", (notebook_id,))
        return original_create(*args, **kwargs)

    monkeypatch.setattr(store, "create_grant", drop_notebook_then_create)
    denied = client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={"principal_type": "group", "principal_id": group_id, "role": "viewer"},
        headers=alice,
    )
    monkeypatch.setattr(store, "create_grant", original_create)
    assert denied.status_code == 404, denied.text
    # 笔记本维度的文案:不是「你已不再拥有这本笔记本的管理权」(库根本不在了),
    # 也不是「群组不存在」(组没有问题)。
    assert denied.json()["detail"] == "笔记本不存在"
    assert denied.headers.get("X-User-Message") == "1"


def test_granting_still_works_while_the_initiator_keeps_manage_rights(
    tmp_path, monkeypatch
):
    """反向护栏:发起人仍有管理权时发边照常成功——复检不能变成恒关的闸。"""
    client = _client(tmp_path, monkeypatch)
    alice, _alice_id, _ = _new_user(client)
    notebook_id = _make_notebook(client, alice)
    group_id = _make_group(client, alice, name="组")
    granted = client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={"principal_type": "group", "principal_id": group_id, "role": "viewer"},
        headers=alice,
    )
    assert granted.status_code == 200, granted.text


def test_approval_still_succeeds_while_the_author_keeps_manage_rights(
    tmp_path, monkeypatch
):
    """反向护栏:申请人仍有管理权时,批准必须照常成功——复检不能变成一道恒关的闸。"""
    client = _client(tmp_path, monkeypatch)
    boss, librarian, _lid, group_id, notebook_id = _make_member_owned_notebook(client)
    request_id = _submit(client, librarian, notebook_id, group_id).json()["id"]

    approved = client.post(
        f"/api/groups/{group_id}/share-requests/{request_id}/approve", headers=boss
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert [
        (g["principal_type"], g["role"])
        for g in client.get(f"/api/notebooks/{notebook_id}/grants", headers=librarian).json()
    ] == [("group", "viewer")]


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_a_system_admin_can_still_decide_without_being_a_group_member(
    tmp_path, monkeypatch, repo, decision
):
    """系统管理员的运维旁路必须**穿过** store 的资格复核(P2-T2 裁决 A)。

    `_require_group_admin` 放行系统管理员而不要求他是组成员,所以事务内那次复核的判据
    是「本人是组管理员 **或** 路由已证明他是系统管理员」。只按组成员行判会把旁路整个
    掐断——系统管理员会在自己放行过的守卫之后吃一个 403。
    """
    client = _client(tmp_path, monkeypatch)
    _boss, librarian, _lid, group_id, notebook_id = _make_member_owned_notebook(client)
    request_id = _submit(client, librarian, notebook_id, group_id).json()["id"]

    root, root_id, _ = _new_user(client)
    _promote_to_system_admin(repo, root_id)
    # 他**不是**这个组的成员,连成员行都没有。
    assert client.get(f"/api/groups/{group_id}", headers=root).status_code == 200

    decided = client.post(
        f"/api/groups/{group_id}/share-requests/{request_id}/{decision}", headers=root
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == ("approved" if decision == "approve" else "rejected")
    assert decided.json()["decided_by"] == root_id


def test_a_member_removed_in_the_toctou_window_cannot_file_a_request(
    tmp_path, monkeypatch
):
    """申请人的成员资格同样要在事务内复核(codex #519 R2 P2-1)。

    路由查过「你在不在这个组里」之后、插入之前被移出组,仍能落一条**非成员**的 pending
    申请——组管理员的审核队列里会出现一个已经不属于本组的人。非成员与「组不存在」同为
    404(群组可见性口径),与路由自己那次前置检查逐字同一个响应。
    """
    client = _client(tmp_path, monkeypatch)
    boss, librarian, librarian_id, group_id, notebook_id = _make_member_owned_notebook(client)

    store = _app_group_store()
    original = store.user_group_role

    def evict_then_answer(gid, uid):
        role = original(gid, uid)
        if gid == group_id and uid == librarian_id:
            store.remove_member(gid, librarian_id)  # 守卫刚过,写事务还没开
        return role

    monkeypatch.setattr(store, "user_group_role", evict_then_answer)
    denied = _submit(client, librarian, notebook_id, group_id)
    assert denied.status_code == 404, denied.text
    assert denied.json()["detail"] == "群组不存在"

    # 一条非成员的申请都不该落库。
    monkeypatch.setattr(store, "user_group_role", original)
    assert _pending_for(client, boss, group_id).json() == []


def test_a_notebook_deleted_in_the_toctou_window_is_a_404_not_a_500(
    tmp_path, monkeypatch
):
    """笔记本那个**外键父行**同样要在事务内复核(codex #519 R7 P2)。

    能力守卫(`notebook:manage`)通过之后、写事务开始之前,这本库可以被并发删掉。少了
    `_require_notebook_on` / `_lock_notebook_on`,`INSERT INTO notebook_share_requests` 撞
    `notebook_id` 的外键抛 `IntegrityError`——用户拿到的是「服务器出错」,而正确答案是 404。

    与 `test_a_member_removed_in_the_toctou_window_cannot_file_a_request` 同款插桩(删库
    动作插在守卫那次 `user_group_role` 之后、store 写事务之前),但钉的是**另一个**父行:
    那条钉群组成员资格(权限轴),这条钉笔记本存在性(外键轴)。只堵组那一个不算堵住。
    """
    from app.api import deps

    client = _client(tmp_path, monkeypatch)
    boss, librarian, librarian_id, group_id, notebook_id = _make_member_owned_notebook(client)

    store = _app_group_store()
    original = store.user_group_role

    def drop_notebook_then_answer(gid, uid):
        role = original(gid, uid)
        if gid == group_id and uid == librarian_id:
            # 守卫刚过、写事务还没开:库在这一瞬间被删掉。
            with deps.repository()._write() as db:
                db.execute("DELETE FROM notebooks WHERE id=?", (notebook_id,))
        return role

    monkeypatch.setattr(store, "user_group_role", drop_notebook_then_answer)
    denied = _submit(client, librarian, notebook_id, group_id)
    assert denied.status_code == 404, denied.text
    # 笔记本维度的文案,不是「群组不存在」——用户不该被支去查一个没问题的组。
    assert denied.json()["detail"] == "笔记本不存在"
    assert denied.headers.get("X-User-Message") == "1"

    # 一条指向已删笔记本的申请都不该落库。
    monkeypatch.setattr(store, "user_group_role", original)
    assert _pending_for(client, boss, group_id).json() == []


def test_store_level_decision_and_membership_rechecks_are_unconditional(repo):
    """直接走 store:非组管理员批准/驳回 → `GroupAdminRequiredError`;非成员申请 →
    `GroupMembershipRequiredError`。不经路由,证明复核住在写事务里而不是守卫里。"""
    from app.repositories.ports import (
        GroupAdminRequiredError,
        GroupMembershipRequiredError,
    )

    store = repo._runtime.groups
    _seed_users(repo, "tc-admin", "tc-member", "tc-outsider")
    _seed_notebook(repo, "nb-tc", "tc-member")
    group = store.create_group(
        name="g", kind="project", description="", created_by="tc-admin"
    )
    store.upsert_member(group["id"], "tc-member", role="member", added_by="tc-admin")
    request = store.create_share_request(
        "nb-tc", group_id=group["id"], requested_by="tc-member"
    )

    # 普通成员与完全的外人都不能审批。
    for who in ("tc-member", "tc-outsider"):
        with pytest.raises(GroupAdminRequiredError):
            store.approve_share_request(group["id"], request["id"], decided_by=who)
        with pytest.raises(GroupAdminRequiredError):
            store.reject_share_request(group["id"], request["id"], decided_by=who)
    # 旁路开关放行(路由证明他是系统管理员)。
    assert store.reject_share_request(
        group["id"], request["id"],
        decided_by="tc-outsider", decided_by_is_system_admin=True,
    )["status"] == "rejected"

    # 非成员发不出申请。
    with pytest.raises(GroupMembershipRequiredError):
        store.create_share_request("nb-tc", group_id=group["id"], requested_by="tc-outsider")


def test_decided_at_two_state_invariant_actually_rejects_bad_values():
    """两态断言必须真的拦得住坏值——**直接调 helper 注入** '' 与错配。

    这条与 `test_decided_at_is_null_while_pending_and_iso_once_decided` 分工:那条走
    store 的正常写路径,只经过合法值,把 `assert_share_request_decided_at` 的断言体换成
    `return` 后它仍全绿——「加了守卫≠有效」。这条直接喂坏值,断言体一旦被删就红。
    """
    from app.repositories.group_rows import assert_share_request_decided_at

    # 合法两态:不抛。
    assert_share_request_decided_at("pending", None)
    assert_share_request_decided_at("approved", "2026-08-18T00:00:00+08:00")
    assert_share_request_decided_at("rejected", "2026-08-18T00:00:00+08:00")

    # pending 却带 decided_at(尤其空串 '' —— 正是会 poison PG timestamptz 的那种)→ 抛。
    for bad in ("", "2026-08-18T00:00:00+08:00"):
        with pytest.raises(AssertionError):
            assert_share_request_decided_at("pending", bad)

    # 已决定却没有决定时间(None 或空串)→ 抛。
    for status in ("approved", "rejected"):
        for bad in (None, ""):
            with pytest.raises(AssertionError):
                assert_share_request_decided_at(status, bad)

    # 未知 status(shadow 停车哨兵)放行、不误伤——与「status 精确匹配」红线同向。
    assert_share_request_decided_at("__parked_sentinel__", "anything")


def test_the_partial_unique_index_holds_at_the_store_layer(repo):
    """同库同组的第二次 pending 申请撞唯一索引 → 幂等返回既有行,不抛也不重复。"""
    store = repo._runtime.groups
    _seed_users(repo, "u-o", "u-a")
    _seed_notebook(repo, "nb-u", "u-o")
    group = store.create_group(name="g", kind="project", description="", created_by="u-a")
    # 申请人必须真的是组成员——事务内的成员资格复核是无条件的(R2 P2-1)。
    store.upsert_member(group["id"], "u-o", role="member", added_by="u-a")

    first = store.create_share_request("nb-u", group_id=group["id"], requested_by="u-o")
    second = store.create_share_request("nb-u", group_id=group["id"], requested_by="u-o")
    assert first["id"] == second["id"]
    assert len(store.list_pending_share_requests(group["id"])) == 1

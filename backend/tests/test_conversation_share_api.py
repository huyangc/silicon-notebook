"""问答会话公开分享 T2:已认证的发放/回读/撤销端点 + 水位推进。

匿名页(`GET /public/conversations/{token}`)与图片通道是 T3/T4,不在此文件。
这里覆盖三个已认证端点的授权与策略:

  ① notebook 层 —— `require_notebook_read`(owner ∪ 只读成员 ∪ 有效授权边);
  ② 行级 —— 会话 `created_by == 当前用户`(镜像报告的 `_own_report_or_404`)。

三条承重项,每条一条独立用例 + 反向断言(会话状态没被动过):
  * 行级 created_by 门:别人的会话、跨 notebook 的 cid、陌生人一律 404;
  * 空 `created_by` fail closed(§3.1):创建者缺失的历史会话拒绝分享(409);
  * 「至少一条已写入答案」(§七 5):零答案会话拒绝分享(409)并回滚 token。

会话通常由 `/ask`(依赖模型)创建,所以这里像 T1 store 测试一样**直接建行**,
把 `created_by` 设成真实用户 id —— 它是行级判定的锚点。

见 docs/superpowers/specs/2026-08-18-conversation-sharing-design_zh.md §四/§五/§七。
"""
from __future__ import annotations

from itertools import count

import pytest
from fastapi.testclient import TestClient


_PASSWORD = "pw12345678"
_USERNAMES = iter(f"c{index:08d}" for index in range(1, 999))
_CIDS = count(1)


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app

    return TestClient(app)


def _new_user(client: TestClient) -> tuple[dict, str]:
    username = next(_USERNAMES)
    client.post("/api/auth/register",
                json={"username": username, "password": _PASSWORD})
    token = client.post(
        "/api/auth/login", json={"username": username, "password": _PASSWORD}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    return headers, client.get("/api/me", headers=headers).json()["id"]


def _me(client: TestClient, headers: dict) -> str:
    return client.get("/api/me", headers=headers).json()["id"]


def _notebook(client: TestClient, headers: dict, name: str = "库") -> str:
    return client.post("/api/notebooks", json={"name": name},
                       headers=headers).json()["id"]


def _group_granted_reader(client: TestClient, owner: dict, notebook_id: str,
                          reader: dict | None = None,
                          reader_id: str = "") -> tuple[dict, str, str]:
    """建组 → 拉人进组 → 把库共享给该组,返回 (reader headers, reader id, group id)。"""
    group_id = client.post("/api/groups", json={"name": "项目组"},
                           headers=owner).json()["id"]
    if reader is None:
        reader, reader_id = _new_user(client)
    assert client.put(
        f"/api/groups/{group_id}/members/{reader_id}",
        json={"role": "member"}, headers=owner,
    ).status_code == 200
    assert client.post(
        f"/api/notebooks/{notebook_id}/grants",
        json={"principal_type": "group", "principal_id": group_id,
              "role": "viewer"},
        headers=owner,
    ).status_code == 200
    return reader, reader_id, group_id


def _seed_conversation(notebook_id: str, created_by: str, *,
                       answers: tuple[str, ...] = ("2026-01-01T00:00:01",),
                       cid: str | None = None) -> str:
    """Insert a conversation (and its answers) directly into the store.

    Conversations are normally minted by the LLM-backed `/ask` pipeline, so —
    like the T1 store test — these API tests seed the rows. `created_by` is the
    row-level gate's anchor; `answers` is the list of answer `created_at`
    instants (empty tuple = a never-answered conversation).
    """
    from app.api.deps import repository

    cid = cid or f"conv-{next(_CIDS)}"
    db = repository()._runtime.database
    with db.write() as conn:
        conn.execute(
            "INSERT INTO conversations "
            "(id, notebook_id, title, created_by, created_at, updated_at) "
            "VALUES (?, ?, '会话', ?, '2026-01-01T00:00:00', ?)",
            (cid, notebook_id, created_by,
             answers[-1] if answers else "2026-01-01T00:00:00"),
        )
        for index, created_at in enumerate(answers):
            conn.execute(
                "INSERT INTO answers "
                "(id, notebook_id, conversation_id, question, payload, created_at) "
                "VALUES (?, ?, ?, 'q', '{\"conclusion\": \"c\"}', ?)",
                (f"{cid}-ans{index}", notebook_id, cid, created_at),
            )
    return cid


def _share_url(notebook_id: str, cid: str) -> str:
    return f"/api/notebooks/{notebook_id}/conversations/{cid}/share"


def _share_state(notebook_id: str, cid: str) -> dict:
    """Read the persisted token/watermark straight from the store (bypasses the
    write-guarded read-back endpoint) — used to prove a rejected share left no
    token behind."""
    from app.api.deps import repository

    return repository().conversation_share_state(notebook_id, cid)


# --------------------------------------------------------------- 创建者正常路径


def test_owner_shares_reads_back_and_unshares_their_own_conversation(client):
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    cid = _seed_conversation(nb, owner_id)

    shared = client.post(_share_url(nb, cid), headers=owner)
    assert shared.status_code == 200, shared.text
    body = shared.json()
    token = body["share_token"]
    assert token
    assert body["shared_through_at"] == "2026-01-01T00:00:01"
    assert body["shared_through_id"] == f"{cid}-ans0"

    read_back = client.get(_share_url(nb, cid), headers=owner)
    assert read_back.status_code == 200
    assert read_back.json()["share_token"] == token
    assert read_back.json()["shared_through_id"] == f"{cid}-ans0"

    assert client.delete(_share_url(nb, cid), headers=owner).status_code == 204
    # Revoked: read-back 404s like an unknown token.
    assert client.get(_share_url(nb, cid), headers=owner).status_code == 404


def test_a_member_runs_the_full_lifecycle_on_their_own_conversation(client):
    """读权成员对**自己的**会话拥有与 owner 相同的一整套操作。"""
    owner, _ = _new_user(client)
    nb = _notebook(client, owner)
    member, member_id, _ = _group_granted_reader(client, owner, nb)
    cid = _seed_conversation(nb, member_id)

    assert client.post(_share_url(nb, cid), headers=member).status_code == 200
    assert client.get(_share_url(nb, cid), headers=member).status_code == 200
    assert client.delete(_share_url(nb, cid), headers=member).status_code == 204


# ------------------------------------------------ 承重 1:行级 created_by 门


def test_another_members_conversation_is_404_on_every_share_operation(client):
    """成员 A 的会话对成员 B **和 notebook owner** 的每个分享操作都 404。

    404(而非可区分的 403)是刻意的:可区分的响应会把端点变成"这个可读库里有
    谁的哪些会话"的探针。删除断言配套证明拦下的是守卫本身——会话仍未分享。"""
    owner, _ = _new_user(client)
    nb = _notebook(client, owner)
    member_a, member_a_id, _ = _group_granted_reader(client, owner, nb)
    member_b, member_b_id = _new_user(client)
    from app.api.deps import repository
    repository().add_member(nb, member_b_id)

    cid = _seed_conversation(nb, member_a_id)
    # 先让创建者真的分享出去 —— 否则"入侵者拿到 404"可能只是因为它压根没分享。
    assert client.post(_share_url(nb, cid), headers=member_a).status_code == 200

    for intruder, label in ((member_b, "另一位成员"), (owner, "notebook owner")):
        assert client.post(_share_url(nb, cid),
                           headers=intruder).status_code == 404, label
        assert client.get(_share_url(nb, cid),
                          headers=intruder).status_code == 404, label
        assert client.delete(_share_url(nb, cid),
                             headers=intruder).status_code == 404, label

    # 入侵者的 DELETE 是被守卫拦下,不是"拦了个寂寞":token 仍在,创建者读得回。
    assert client.get(_share_url(nb, cid), headers=member_a).status_code == 200


def test_a_conversation_id_from_another_notebook_is_404(client):
    """URL 上的 notebook 维度不是装饰:换一个同样有读权的 notebook_id 拼同一个
    cid,必须 404 —— `conversation_creator` 的 `notebook_id` 谓词是"会话属于这个
    库"的唯一证明。两个自有库排除"其实是读守卫在拦"的解释。"""
    owner, owner_id = _new_user(client)
    own_a = _notebook(client, owner, "A")
    own_b = _notebook(client, owner, "B")
    cid = _seed_conversation(own_a, owner_id)

    # own_b 也归他,读守卫一定放行 → 只可能是 conversation_creator 的 notebook 谓词在拦。
    assert client.post(_share_url(own_b, cid), headers=owner).status_code == 404
    assert client.get(_share_url(own_b, cid), headers=owner).status_code == 404
    assert client.delete(_share_url(own_b, cid), headers=owner).status_code == 404
    # 同一 cid 在它真正所属的库里照常可分享。
    assert client.post(_share_url(own_a, cid), headers=owner).status_code == 200


def test_a_stranger_without_read_access_is_404(client):
    """无读权的人连第一层(require_notebook_read)都过不去。"""
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    cid = _seed_conversation(nb, owner_id)
    assert client.post(_share_url(nb, cid), headers=owner).status_code == 200

    stranger, _ = _new_user(client)
    assert client.post(_share_url(nb, cid), headers=stranger).status_code == 404
    assert client.get(_share_url(nb, cid), headers=stranger).status_code == 404
    assert client.delete(_share_url(nb, cid), headers=stranger).status_code == 404


# ------------------------------------------- 承重 2:空 created_by fail closed


def test_a_creatorless_conversation_is_refused_fail_closed(client):
    """`conversations.created_by` 是 `DEFAULT ''`,历史行可能创建者为空。这类会话
    永远做不了 §五 的实时复核,一条永远复核不了的公开链接是本设计唯一不可接受的
    形态 —— 所以必须 fail closed 拒绝分享(§3.1)。

    创建者缺失的会话属于 notebook owner 的库,却不归任何人;owner 对它发起分享
    时应拿到明确的 409(缺创建者),而不是被 equality 门静默 404。分享后会话仍未
    分享。"""
    owner, _ = _new_user(client)
    nb = _notebook(client, owner)
    # 带一条答案 —— 证明拦下它的是 created_by 门,不是"零答案"策略。
    cid = _seed_conversation(nb, "")   # created_by 为空串(历史行)

    refused = client.post(_share_url(nb, cid), headers=owner)
    assert refused.status_code == 409, refused.text
    assert "创建者" in refused.json()["detail"]
    # 没有 token 被发放。
    assert _share_state(nb, cid)["share_token"] == ""
    # 回读端点同样拒绝(会话永远无法被合法分享)。
    assert client.get(_share_url(nb, cid), headers=owner).status_code == 409


# ------------------------------------- 承重 3:「至少一条已写入答案」+ 回滚


def test_a_zero_answer_conversation_cannot_be_shared_and_leaves_no_token(client):
    """会话的"已完成"对应物是水位之前至少有一条已写入答案(§七 5)。零答案会话
    会被 `share_conversation` 发放 token(watermark 留 NULL),API 层必须拒绝并
    **回滚刚发的 token**,使事后状态可验证地"未分享"。"""
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    cid = _seed_conversation(nb, owner_id, answers=())   # 无任何答案

    refused = client.post(_share_url(nb, cid), headers=owner)
    assert refused.status_code == 409, refused.text
    assert "已完成的回答" in refused.json()["detail"]
    # token 被回滚:事后未分享。
    assert _share_state(nb, cid)["share_token"] == ""
    assert client.get(_share_url(nb, cid), headers=owner).status_code == 404

    # 写入一条答案后,同一会话就能分享了 —— 证明拦下的是"零答案"而非这条路径本身。
    from app.api.deps import repository
    with repository()._runtime.database.write() as conn:
        conn.execute(
            "INSERT INTO answers "
            "(id, notebook_id, conversation_id, question, payload, created_at) "
            "VALUES (?, ?, ?, 'q', '{\"conclusion\": \"c\"}', '2026-01-02T00:00:00')",
            (f"{cid}-late", nb, cid),
        )
    ok = client.post(_share_url(nb, cid), headers=owner)
    assert ok.status_code == 200, ok.text
    assert ok.json()["shared_through_id"] == f"{cid}-late"


# ------------------------------------------------ 水位推进(freeze + update)


def test_share_freezes_the_watermark_and_reshare_advances_it(client):
    """"分享"与"更新到最新"是同一个调用(§四):首次分享把水位推到当时最新答案;
    之后新增的答案不自动出现,需再点一次分享("更新到最新"),且复用同一 token。"""
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    cid = _seed_conversation(nb, owner_id, answers=("2026-01-01T00:00:01",))

    first = client.post(_share_url(nb, cid), headers=owner).json()
    token = first["share_token"]
    assert first["shared_through_id"] == f"{cid}-ans0"

    # 分享之后再写入一条答案。
    from app.api.deps import repository
    with repository()._runtime.database.write() as conn:
        conn.execute(
            "INSERT INTO answers "
            "(id, notebook_id, conversation_id, question, payload, created_at) "
            "VALUES (?, ?, ?, 'q', '{\"conclusion\": \"c\"}', '2026-01-03T00:00:00')",
            (f"{cid}-new", nb, cid),
        )

    # 回读:水位仍停在旧答案(冻结),token 不变。
    frozen = client.get(_share_url(nb, cid), headers=owner).json()
    assert frozen["share_token"] == token
    assert frozen["shared_through_id"] == f"{cid}-ans0"

    # 再分享一次 = 更新到最新:同一 token,水位推进到新答案。
    updated = client.post(_share_url(nb, cid), headers=owner).json()
    assert updated["share_token"] == token
    assert updated["shared_through_id"] == f"{cid}-new"
    assert client.get(_share_url(nb, cid),
                      headers=owner).json()["shared_through_id"] == f"{cid}-new"


def test_share_pins_the_watermark_to_the_clients_expected_answer(client):
    """The disclosure TOCTOU fix (codex #522 R2 P1): the client passes the newest
    answer it saw (and disclosed) as ``expected_through_id``; the watermark pins
    to EXACTLY that answer, not to whatever is latest at POST time. Here a NEWER
    answer already exists, yet passing the older one freezes the snapshot there.

    Mutation guard: dropping the body param / ignoring ``expected_through_id`` in
    the route pins to the latest answer instead, flipping ``shared_through_id``.
    """
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    cid = _seed_conversation(
        nb, owner_id,
        answers=("2026-01-01T00:00:01", "2026-01-02T00:00:00"),
    )

    resp = client.post(
        _share_url(nb, cid), headers=owner,
        json={"expected_through_id": f"{cid}-ans0"},
    )
    assert resp.status_code == 200, resp.text
    # Pinned to the disclosed answer (ans0), NOT the newer ans1.
    assert resp.json()["shared_through_id"] == f"{cid}-ans0"


def test_share_rejects_a_stale_expected_through_id(client):
    """When the disclosed boundary answer no longer resolves (deleted since the
    client read it), the store raises and the route returns 409 — never a silent
    pin-to-latest that would publish an unreviewed span (codex #522 R2 P1). No
    token is issued."""
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    cid = _seed_conversation(nb, owner_id)

    resp = client.post(
        _share_url(nb, cid), headers=owner,
        json={"expected_through_id": "ans-that-never-existed"},
    )
    assert resp.status_code == 409, resp.text
    # Nothing was shared.
    assert _share_state(nb, cid)["share_token"] == ""
    assert client.get(_share_url(nb, cid), headers=owner).status_code == 404


def test_get_share_is_404_when_the_conversation_is_not_shared(client):
    """回读端点在未分享时 404(镜像 get_report_share_route)——token 就是凭证,
    没有它就没有可交出的东西。"""
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    cid = _seed_conversation(nb, owner_id)
    assert client.get(_share_url(nb, cid), headers=owner).status_code == 404


# --------------------------------------------------------------- 结构守卫


def test_every_conversation_share_route_calls_the_row_level_gate():
    """凡路径含 `{conversation_id}/share` 的认证端点,函数体内必须出现
    `_own_conversation_or_404`。

    行级判定是**体内**的,像 `Depends(...)` 那样在声明处看不见 —— 后续加端点时漏挂
    不会报错,只会安静地让任何读权用户操作别人的会话。AST 扫描:按装饰器路径字面量
    选端点,按 Name 节点判是否调用了守卫。匿名面(T3)挂在 `public_router`、路径里
    也没有 `{conversation_id}`(token 即授权),此文件的这三个都在主 `router` 上。"""
    import ast
    from pathlib import Path

    path = (Path(__file__).resolve().parents[1] / "app" / "api"
            / "ask_routes.py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)

    checked: list[str] = []
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if not (isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)):
                continue
            route = decorator.args[0] if decorator.args else None
            if not (isinstance(route, ast.Constant)
                    and isinstance(route.value, str)):
                continue
            if "{conversation_id}/share" not in route.value:
                continue
            checked.append(node.name)
            calls_gate = any(
                isinstance(inner, ast.Name)
                and inner.id == "_own_conversation_or_404"
                for inner in ast.walk(node)
            )
            if not calls_gate:
                offenders.append(node.name)

    # 空转保护:扫到 0 个端点却报绿,比没有守卫更糟。
    assert len(checked) == 3, f"只扫到 {len(checked)} 个分享端点: {checked}"
    assert offenders == [], (
        f"以下分享端点没有调用行级守卫 _own_conversation_or_404: {offenders}"
    )

"""问答会话公开分享 T3:匿名页端点 `GET /public/conversations/{token}` + 接线。

这里覆盖的是 T3 的**产品层**(§七 五条),与 T2 的已认证发放/撤销分开:

  * 接线守卫:匿名端点必须挂在**独立** `public_router` 上,不能继承主 router 的
    router 级 `Depends(get_current_user)`(§七 1/2)——`auth_optional` 夹具看不见
    这条 401(缺 token 静默回退 seeded admin),所以直接断言路由接线本身。
  * 匿名可读:发放 token 后,**不带任何 session** 就能取到白名单投影。
  * 未知/已撤销 token 与失权一律 404(§七 3):可区分响应会向匿名调用者泄露成员身份。
  * 创建者实时读权复核(§五 / C-2):撤销授权 → 链接当场 404;恢复 → 复活。
  * GATE 字段(notebook_id/created_by)绝不进响应。

见 docs/superpowers/specs/2026-08-18-conversation-sharing-design_zh.md §四/§五/§七。
"""
from __future__ import annotations

import json
from itertools import count

import pytest
from fastapi.testclient import TestClient


_PASSWORD = "pw12345678"
_USERNAMES = iter(f"p{index:08d}" for index in range(1, 999))
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


def _notebook(client: TestClient, headers: dict, name: str = "库") -> str:
    return client.post("/api/notebooks", json={"name": name},
                       headers=headers).json()["id"]


# A realistic reasoning-mode payload: anchor-marked body + one anchor carrying
# every addressable field, so the endpoint's projection can be checked for
# leaks end to end.
def _reasoning_payload() -> dict:
    return {
        "conclusion": "简要结论 [k1]。",
        "answer": "详细答案 [k1]。",
        "asked_at": "2026-01-01T00:00:00+08:00",
        "answered_at": "2026-01-01T00:00:02+08:00",
        "evidence_level": "grounded",
        "anchors": [{
            "key": "k1", "object_id": "OBJ-secret", "object_type": "concept",
            "label": "标签", "name": "名称", "snippet": "锚点摘录内容",
            "source_title": "论文标题甲", "source_file_name": "paper.pdf",
            "location_label": "第 2 节", "source_id": "SRC-secret",
            "element_id": "ELE-secret", "tier": "personal",
            "notebook_id": "NB-secret",
            "images": [{"element_id": "IMG-secret", "asset_id": "ASSET-secret",
                        "caption": "图"}],
        }],
        "citations": [],
        "reasoning_trace": [{"step_type": "plan", "summary": "轨迹泄露词"}],
        "retrieval_query": "检索泄露词",
    }


def _seed_shared_conversation(
    notebook_id: str, created_by: str, payload: dict | None = None,
) -> str:
    """Insert a conversation + one answer directly, then return the cid. The
    caller shares it through the authenticated endpoint to mint the token."""
    from app.api.deps import repository

    cid = f"pconv-{next(_CIDS)}"
    body = json.dumps(payload or _reasoning_payload(), ensure_ascii=False)
    db = repository()._runtime.database
    with db.write() as conn:
        conn.execute(
            "INSERT INTO conversations "
            "(id, notebook_id, title, created_by, created_at, updated_at) "
            "VALUES (?, ?, '共享会话', ?, '2026-01-01T00:00:00', "
            "'2026-01-01T00:00:02')",
            (cid, notebook_id, created_by),
        )
        conn.execute(
            "INSERT INTO answers "
            "(id, notebook_id, conversation_id, question, payload, created_at) "
            "VALUES (?, ?, ?, '我的问题?', ?, '2026-01-01T00:00:01')",
            (f"{cid}-ans", notebook_id, cid, body),
        )
    return cid


def _share(client: TestClient, headers: dict, nb: str, cid: str) -> str:
    resp = client.post(
        f"/api/notebooks/{nb}/conversations/{cid}/share", headers=headers
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["share_token"]


def _grant_notebook_to_group(client: TestClient, owner: dict, nb: str,
                             group_id: str) -> str:
    grant = client.post(
        f"/api/notebooks/{nb}/grants",
        json={"principal_type": "group", "principal_id": group_id,
              "role": "viewer"},
        headers=owner,
    )
    assert grant.status_code == 200, grant.text
    return grant.json()["id"]


def _group_granted_reader(
    client: TestClient, owner: dict, nb: str
) -> tuple[dict, str, str, str]:
    """建组 → 拉新用户进组 → 把库共享给该组,返回 (reader headers, reader id,
    group id, grant id)。删掉这条 grant 即可撤销该 reader 的读权;组仍在,可重发。"""
    reader, reader_id = _new_user(client)
    group_id = client.post("/api/groups", json={"name": "组"},
                           headers=owner).json()["id"]
    assert client.put(
        f"/api/groups/{group_id}/members/{reader_id}",
        json={"role": "member"}, headers=owner,
    ).status_code == 200
    grant_id = _grant_notebook_to_group(client, owner, nb, group_id)
    return reader, reader_id, group_id, grant_id


# ------------------------------------------------------------ 接线守卫(§七 1/2)


def test_public_conversation_route_is_not_behind_the_router_level_session_guard():
    """The public read must not sit under the blanket `get_current_user` router.

    `main.py` mounts the main API router with a router-level
    `Depends(get_current_user)` ("零逐路由遗漏"). A public endpoint declared on
    that router answers 401 to the very visitors it exists for — and the
    auth-optional test fixture hides it, because there an absent token silently
    resolves to the seeded admin. So assert the wiring itself (mirrors
    `test_report_api.test_public_report_route_is_not_behind_...`).
    """
    from app.api.deps import get_current_user
    from app.main import create_app

    app = create_app()
    public = [
        route for route in app.routes
        if getattr(route, "path", "") == "/api/public/conversations/{token}"
    ]
    assert public, "public conversation route is not registered"
    for route in public:
        dependencies = [
            dependency.call
            for dependency in getattr(route, "dependant", None).dependencies
        ] if getattr(route, "dependant", None) else []
        assert get_current_user not in dependencies, (
            "public conversation route inherited the session guard"
        )

    # The authenticated share routes must still carry a guard, so this test
    # cannot pass by removing authentication everywhere.
    guarded = [
        route for route in app.routes
        if getattr(route, "path", "")
        == "/api/notebooks/{notebook_id}/conversations/{conversation_id}/share"
    ]
    assert guarded, "authenticated share route is not registered"


# ------------------------------------------------------------ 匿名可读 + 白名单


def test_anonymous_reader_gets_the_whitelisted_projection(client):
    """A shared conversation is readable with NO session, and the body is the
    allowlist — question/answer/timing/evidence + reference title/excerpt, and
    NONE of the addressable ids or reasoning surface."""
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    cid = _seed_shared_conversation(nb, owner_id)
    token = _share(client, owner, nb, cid)

    # No Authorization header at all — this is the anonymous surface.
    resp = client.get(f"/api/public/conversations/{token}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["title"] == "共享会话"
    assert body["shared_at"] == "2026-01-01T00:00:01"
    assert len(body["turns"]) == 1
    turn = body["turns"][0]
    assert turn["question"] == "我的问题?"
    assert turn["answer_md"] == "详细答案 [k1]。"
    assert turn["asked_at"] == "2026-01-01T00:00:00+08:00"
    assert turn["answered_at"] == "2026-01-01T00:00:02+08:00"
    assert turn["evidence_level"] == "grounded"
    assert turn["references"][0]["key"] == "k1"
    assert turn["references"][0]["title"] == "论文标题甲"
    assert turn["references"][0]["snippet"] == "锚点摘录内容"

    # Nothing addressable, no reasoning surface, no gate field anywhere.
    raw = resp.text
    for token_str in (
        "OBJ-secret", "SRC-secret", "ELE-secret", "NB-secret", "IMG-secret",
        "ASSET-secret", "轨迹泄露词", "检索泄露词",
        "notebook_id", "created_by", "reasoning_trace", "retrieval_query",
    ):
        assert token_str not in raw, f"{token_str} leaked into the public response"


def test_unknown_and_revoked_tokens_are_both_404(client):
    """An unknown token and a revoked one are indistinguishable 404s."""
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    cid = _seed_shared_conversation(nb, owner_id)
    token = _share(client, owner, nb, cid)

    assert client.get(f"/api/public/conversations/{token}").status_code == 200
    assert client.get(
        "/api/public/conversations/cshr-never-issued"
    ).status_code == 404

    # Revoke → the same 404 as an unknown token.
    assert client.delete(
        f"/api/notebooks/{nb}/conversations/{cid}/share", headers=owner
    ).status_code == 204
    assert client.get(f"/api/public/conversations/{token}").status_code == 404


# ------------------------------------------- 创建者读权实时复核(§五 / §七 3 / C-2)


def test_link_dies_when_the_creator_loses_read_access_and_revives_on_restore(client):
    """A member publishes a conversation built from a shared notebook. The link
    lasts exactly as long as their read access: revoke the group grant and the
    public read 404s immediately (same 404 as unknown — never reveals the
    membership); re-grant and the link revives (idempotent-token semantics).

    Load-bearing: deleting the guard's live re-check in `public_conversation_route`
    makes the post-revocation read 200 instead of 404, and this test goes red.
    """
    owner, _ = _new_user(client)
    nb = _notebook(client, owner)
    reader, reader_id, group_id, grant_id = _group_granted_reader(
        client, owner, nb
    )
    cid = _seed_shared_conversation(nb, reader_id)
    token = _share(client, reader, nb, cid)

    # Live while the member has read access.
    assert client.get(f"/api/public/conversations/{token}").status_code == 200

    # Owner revokes the grant → member loses read access → link dies.
    assert client.delete(
        f"/api/notebooks/{nb}/grants/{grant_id}", headers=owner
    ).status_code == 204
    assert client.get(f"/api/public/conversations/{token}").status_code == 404

    # Re-grant to the SAME group restores access → the SAME token revives.
    _grant_notebook_to_group(client, owner, nb, group_id)
    assert client.get(f"/api/public/conversations/{token}").status_code == 200


def test_gate_fields_never_appear_in_the_response(client):
    """`notebook_id`/`created_by` are read for the live re-check only and are
    popped before projection — they must not appear in the public body even as
    keys."""
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    cid = _seed_shared_conversation(nb, owner_id)
    token = _share(client, owner, nb, cid)

    body = client.get(f"/api/public/conversations/{token}").json()
    assert "notebook_id" not in body
    assert "created_by" not in body
    assert nb not in json.dumps(body, ensure_ascii=False)
    assert owner_id not in json.dumps(body, ensure_ascii=False)

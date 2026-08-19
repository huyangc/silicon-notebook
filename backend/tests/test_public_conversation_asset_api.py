"""问答会话公开分享 T4:匿名图片通道 `GET /public/conversations/{token}/assets/{alias}`。

覆盖设计 §六 + §七 的图片侧:

  * 别名而非 asset_id:页面投影发的是 token 派生的不透明别名,端点据此反查真
    asset 并回字节;裸 asset_id、被篡改的别名一律 404。
  * round-trip:投影算的别名 == 端点反查(两处共用 `conversation_asset_alias`);
    端点若用别的 token 计算就取不到 asset(整段 API 200 变 404)。
  * 未被引用到的 asset 的别名 → 404(不区分「不存在」与「不在本次分享里」)。
  * **同一套闸**:创建者读权失效 → 图片端点也 404(不只是页面);恢复即复活。
  * `Cache-Control: no-store`:撤销后浏览器缓存不能把「失效即 404」架空。
  * 开关关闭(MINERU_RETURN_IMAGES=false)→ 端点 404 且投影不给别名。
  * 未知 / 已撤销 token → 404。

见 docs/superpowers/specs/2026-08-18-conversation-sharing-design_zh.md §六/§七。
"""
from __future__ import annotations

import json
from itertools import count

import pytest
from fastapi.testclient import TestClient


_PASSWORD = "pw12345678"
_USERNAMES = iter(f"a{index:08d}" for index in range(1, 999))
_CIDS = count(1)

_IMAGE_BYTES = b"\x89PNG\r\n\x1a\nT4-fixture-image-bytes"


def _build_client(monkeypatch, tmp_path, *, images_enabled: bool = True) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MINERU_RETURN_IMAGES", "true" if images_enabled else "false")
    from app.core.config import get_settings

    # conftest clears this at test start; rebuild it against the env just set so
    # the request-time ``get_settings().mineru_return_images`` reads our value.
    get_settings.cache_clear()
    from app.main import app

    return TestClient(app)


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    return _build_client(monkeypatch, tmp_path, images_enabled=True)


@pytest.fixture
def client_no_images(tmp_path, monkeypatch) -> TestClient:
    return _build_client(monkeypatch, tmp_path, images_enabled=False)


def _new_user(client: TestClient) -> tuple[dict, str]:
    username = next(_USERNAMES)
    client.post("/api/auth/register",
                json={"username": username, "password": _PASSWORD})
    token = client.post(
        "/api/auth/login", json={"username": username, "password": _PASSWORD}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    return headers, client.get("/api/me", headers=headers).json()["id"]


def _notebook(client: TestClient, headers: dict) -> str:
    return client.post("/api/notebooks", json={"name": "库"},
                       headers=headers).json()["id"]


def _save_asset(notebook_id: str, created_by: str) -> str:
    """Persist a real ``notebook_assets`` row + on-disk file, return asset_id.

    Uses the same ``AssetService`` the authenticated upload path uses, so the
    endpoint's ``path_for``/``get_notebook_asset`` resolve to a file that
    exists."""
    from app.api.deps import repository
    from app.services.knowhow.assets import AssetService

    asset = AssetService(repository()).save(
        notebook_id, "figure.png", "image/png", _IMAGE_BYTES, created_by
    )
    return asset["id"]


def _payload_citing_asset(asset_id: str) -> dict:
    """A reasoning payload whose selected anchor attaches ``asset_id`` as an
    answer image (with a caption)."""
    return {
        "conclusion": "结论 [k1]。",
        "answer": "带图证据 [k1]。",
        "asked_at": "2026-01-01T00:00:00+08:00",
        "answered_at": "2026-01-01T00:00:02+08:00",
        "evidence_level": "grounded",
        "anchors": [{
            "key": "k1", "object_id": "OBJ", "object_type": "concept",
            "label": "标签", "name": "名称", "snippet": "锚点摘录",
            "source_title": "论文标题", "location_label": "§1",
            "source_id": "SRC", "element_id": "ELE",
            "images": [{"element_id": "IMGELE-secret", "asset_id": asset_id,
                        "caption": "配图说明"}],
        }],
        "citations": [],
    }


def _seed_conversation(notebook_id: str, created_by: str, payload: dict) -> str:
    from app.api.deps import repository

    cid = f"aconv-{next(_CIDS)}"
    body = json.dumps(payload, ensure_ascii=False)
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


def _alias_from_page(client: TestClient, token: str) -> str:
    """The alias the page projection hands an anonymous reader for the first
    image of the first turn."""
    body = client.get(f"/api/public/conversations/{token}").json()
    return body["turns"][0]["images"][0]["alias"]


def _grant_notebook_to_group(client, owner, nb, group_id) -> str:
    grant = client.post(
        f"/api/notebooks/{nb}/grants",
        json={"principal_type": "group", "principal_id": group_id, "role": "viewer"},
        headers=owner,
    )
    assert grant.status_code == 200, grant.text
    return grant.json()["id"]


def _group_granted_reader(client, owner, nb) -> tuple[dict, str, str, str]:
    reader, reader_id = _new_user(client)
    group_id = client.post("/api/groups", json={"name": "组"},
                           headers=owner).json()["id"]
    assert client.put(
        f"/api/groups/{group_id}/members/{reader_id}",
        json={"role": "member"}, headers=owner,
    ).status_code == 200
    grant_id = _grant_notebook_to_group(client, owner, nb, group_id)
    return reader, reader_id, group_id, grant_id


# --------------------------------------------------------- 别名 round-trip + 白名单


def test_alias_fetches_the_bytes_and_is_no_store(client):
    """The alias the page emits fetches the asset's bytes with the correct mime
    and ``Cache-Control: no-store``. The page never exposed the raw asset_id —
    the reader only ever had the alias."""
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    asset_id = _save_asset(nb, owner_id)
    cid = _seed_conversation(nb, owner_id, _payload_citing_asset(asset_id))
    token = _share(client, owner, nb, cid)

    alias = _alias_from_page(client, token)
    # The raw asset_id / element_id are NOT in the page body — only the alias.
    page_raw = client.get(f"/api/public/conversations/{token}").text
    assert asset_id not in page_raw
    assert "IMGELE-secret" not in page_raw

    # No Authorization header — this is the anonymous surface.
    resp = client.get(f"/api/public/conversations/{token}/assets/{alias}")
    assert resp.status_code == 200, resp.text
    assert resp.content == _IMAGE_BYTES
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.headers["cache-control"] == "no-store"


def test_endpoint_uses_the_same_token_as_the_projection(client):
    """Round-trip proof: the very alias the projection produced resolves at the
    endpoint (both call ``conversation_asset_alias``). Load-bearing — if the
    endpoint reversed the alias under a different token, this 200 becomes a 404.
    """
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    asset_id = _save_asset(nb, owner_id)
    cid = _seed_conversation(nb, owner_id, _payload_citing_asset(asset_id))
    token = _share(client, owner, nb, cid)

    from app.services.conversation_public_view import conversation_asset_alias

    alias = _alias_from_page(client, token)
    assert alias == conversation_asset_alias(token, asset_id)
    assert client.get(
        f"/api/public/conversations/{token}/assets/{alias}"
    ).status_code == 200


def test_raw_asset_id_and_tampered_alias_are_404(client):
    """The raw asset_id is not a valid alias (that is the whole point), and a
    tampered alias resolves to nothing."""
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    asset_id = _save_asset(nb, owner_id)
    cid = _seed_conversation(nb, owner_id, _payload_citing_asset(asset_id))
    token = _share(client, owner, nb, cid)
    alias = _alias_from_page(client, token)

    # The raw asset_id handed to the alias slot must not open anything.
    assert client.get(
        f"/api/public/conversations/{token}/assets/{asset_id}"
    ).status_code == 404
    # A one-char-tampered alias (same length) resolves to nothing.
    tampered = ("0" if alias[0] != "0" else "1") + alias[1:]
    assert client.get(
        f"/api/public/conversations/{token}/assets/{tampered}"
    ).status_code == 404


def test_alias_for_an_unreferenced_asset_is_404(client):
    """An asset that EXISTS but is not referenced by this shared conversation is
    not served — the endpoint reverses only against the snapshot's referenced
    assets."""
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    referenced = _save_asset(nb, owner_id)
    stranger = _save_asset(nb, owner_id)  # exists, but never cited
    cid = _seed_conversation(nb, owner_id, _payload_citing_asset(referenced))
    token = _share(client, owner, nb, cid)

    from app.services.conversation_public_view import conversation_asset_alias

    stranger_alias = conversation_asset_alias(token, stranger)
    assert client.get(
        f"/api/public/conversations/{token}/assets/{stranger_alias}"
    ).status_code == 404


# ----------------------------------------------- 同一套闸:创建者失权 → 图片也 404


def test_image_dies_when_the_creator_loses_read_access_and_revives(client):
    """§七 item 3 applies to the IMAGE endpoint too, via the shared gate: a
    member publishes a conversation citing an image from a shared notebook. The
    image link lives exactly as long as their read access — revoke the grant and
    the image 404s (same 404 as unknown); re-grant and it revives.

    Load-bearing: removing the live re-check from ``_public_conversation_or_404``
    makes the post-revocation image read 200 and turns this red — the page test
    alone would not catch a divergent image gate.
    """
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    asset_id = _save_asset(nb, owner_id)
    reader, reader_id, group_id, grant_id = _group_granted_reader(client, owner, nb)
    cid = _seed_conversation(nb, reader_id, _payload_citing_asset(asset_id))
    token = _share(client, reader, nb, cid)
    alias = _alias_from_page(client, token)

    assert client.get(
        f"/api/public/conversations/{token}/assets/{alias}"
    ).status_code == 200

    # Owner revokes the grant → the creator loses read access → image dies.
    assert client.delete(
        f"/api/notebooks/{nb}/grants/{grant_id}", headers=owner
    ).status_code == 204
    assert client.get(
        f"/api/public/conversations/{token}/assets/{alias}"
    ).status_code == 404

    # Re-grant → access restored → the SAME alias revives.
    _grant_notebook_to_group(client, owner, nb, group_id)
    assert client.get(
        f"/api/public/conversations/{token}/assets/{alias}"
    ).status_code == 200


def test_empty_creator_is_fail_closed_on_the_image_endpoint(client):
    """Defense in depth: the image endpoint must 404 a creatorless shared
    conversation on its own (shared gate), even under an ``everyone`` read
    grant, exactly like the page endpoint."""
    from app.api.deps import repository
    from app.services.conversation_public_view import conversation_asset_alias

    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    asset_id = _save_asset(nb, owner_id)
    cid = _seed_conversation(nb, "", _payload_citing_asset(asset_id))  # creatorless
    alias = conversation_asset_alias("EMPTY-CREATOR-TOK", asset_id)

    db = repository()._runtime.database
    with db.write() as conn:
        conn.execute(
            "UPDATE conversations SET share_token='EMPTY-CREATOR-TOK', "
            "shared_through_at='2026-01-01T00:00:01' WHERE id=?",
            (cid,),
        )
        conn.execute(
            "INSERT INTO notebook_grants "
            "(id, notebook_id, principal_type, principal_id, role, created_at) "
            "VALUES ('g-everyone', ?, 'everyone', '', 'viewer', "
            "'2026-01-01T00:00:00')",
            (nb,),
        )
    assert client.get(
        f"/api/public/conversations/EMPTY-CREATOR-TOK/assets/{alias}"
    ).status_code == 404


# ------------------------------------------------------- token 生命周期 + 开关


def test_unknown_and_revoked_tokens_are_404_for_images(client):
    owner, owner_id = _new_user(client)
    nb = _notebook(client, owner)
    asset_id = _save_asset(nb, owner_id)
    cid = _seed_conversation(nb, owner_id, _payload_citing_asset(asset_id))
    token = _share(client, owner, nb, cid)
    alias = _alias_from_page(client, token)

    assert client.get(
        f"/api/public/conversations/{token}/assets/{alias}"
    ).status_code == 200
    # Unknown token.
    assert client.get(
        f"/api/public/conversations/aconv-never/assets/{alias}"
    ).status_code == 404
    # Revoke → the image 404s like the page.
    assert client.delete(
        f"/api/notebooks/{nb}/conversations/{cid}/share", headers=owner
    ).status_code == 204
    assert client.get(
        f"/api/public/conversations/{token}/assets/{alias}"
    ).status_code == 404


def test_images_disabled_deployment_serves_no_aliases_and_404s(client_no_images):
    """MINERU_RETURN_IMAGES off: the page projection emits no image aliases, and
    the image endpoint 404s even for an alias derived correctly by hand — the
    deployment declined to store/serve images."""
    from app.services.conversation_public_view import conversation_asset_alias

    owner, owner_id = _new_user(client_no_images)
    nb = _notebook(client_no_images, owner)
    asset_id = _save_asset(nb, owner_id)
    cid = _seed_conversation(nb, owner_id, _payload_citing_asset(asset_id))
    token = _share(client_no_images, owner, nb, cid)

    page = client_no_images.get(f"/api/public/conversations/{token}").json()
    assert page["turns"][0]["images"] == []

    # Even a correctly-derived alias must not serve bytes.
    alias = conversation_asset_alias(token, asset_id)
    assert client_no_images.get(
        f"/api/public/conversations/{token}/assets/{alias}"
    ).status_code == 404


def test_image_public_route_is_not_behind_the_router_level_session_guard():
    """The image endpoint must sit on the anonymous ``public_router``, not under
    the blanket ``get_current_user`` router (§七 1/2) — otherwise it 401s the
    visitors it exists for, and the auth-optional fixture would hide that."""
    from app.api.deps import get_current_user
    from app.main import create_app

    app = create_app()
    routes = [
        route for route in app.routes
        if getattr(route, "path", "")
        == "/api/public/conversations/{token}/assets/{alias}"
    ]
    assert routes, "public conversation asset route is not registered"
    for route in routes:
        dependencies = [
            dependency.call
            for dependency in getattr(route, "dependant", None).dependencies
        ] if getattr(route, "dependant", None) else []
        assert get_current_user not in dependencies, (
            "public conversation asset route inherited the session guard"
        )

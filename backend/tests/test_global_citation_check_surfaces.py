"""全局问答终态引用核对(PR-D)的后端呈现面:部分失败照常交付,逐面如实带出。

核对失败**不作废回答**:回答全文照常交付,轮上带 ``citation_check``(仅
``failed > 0`` 时出现),失败的引用/锚点各带 ``verification``;干净的一轮任何
一面都不出现这两个键。这里钉的是读已落库作业的每一个面:

  * 存储层之上的五个投影(本人作业/会话、公开快照、管理员详情、MCP 页面)——
    场景在 ``global_citation_check_surface_cases``,SQLite 在此、PG 孪生在
    ``tests/postgres/test_global_citation_check_surfaces.py``;
  * HTTP 端点逐个走一遍真实应用:``GET /jobs/{id}``、``GET /conversations/{id}``、
    推送流的终态帧(取自库)、管理员与本人的活动详情、``/c/{token}`` 公开页;
  * 引用下钻:同一份回答里,带标记的引用 404(统一错误形状),未带标记的 200;
    公开页里带标记引用的附图不再外发,别名端点同样 404。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.models.global_ask import FLAGGED_CITATION_MESSAGE
from app.services.sqlite_repository import SQLiteRepository
from tests.global_ask_share_cases import insert_conversation, insert_job
from tests.global_citation_check_surface_cases import (
    CASE_IDS, CASES, CHECK, answer, job_payload, keys_anywhere,
)
from tests.test_memory_mcp import _seed_cited_source

_NEW_KEYS = {"citation_check", "verification"}
_IMAGE_BYTES = b"\x89PNG\r\n\x1a\ncitation-check-fixture-image"


# --------------------------------------------------------------- 存储层投影


@pytest.fixture
def store(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'citation_check.db'}",
        storage_dir=str(tmp_path / "storage"),
    )
    repo = SQLiteRepository(settings)
    yield repo._runtime.global_ask_store
    repo.close()


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_citation_check_surface_contract(store, case):
    case(store)


# --------------------------------------------------------------- HTTP 端点


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MINERU_RETURN_IMAGES", "true")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.api import deps

    deps.repository.cache_clear()
    from app.main import create_app

    return TestClient(create_app())


def _login(client: TestClient, username: str, password: str) -> dict:
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password},
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def world(client):
    """An owner with one notebook, two real cited elements in it, one image,
    and one conversation holding a partly failed turn and a clean turn."""
    from app.api.deps import repository
    from app.services.knowhow.assets import AssetService

    client.post("/api/auth/register", json={"username": "c00000001", "password": "pw12345678"})
    owner = _login(client, "c00000001", "pw12345678")
    owner_id = client.get("/api/me", headers=owner).json()["id"]
    notebook_id = client.post(
        "/api/notebooks", json={"name": "核对库"}, headers=owner,
    ).json()["id"]
    repo = repository()
    flagged = _seed_cited_source(repo, notebook_id, "flagged", text="被改动的原文")
    clean = _seed_cited_source(repo, notebook_id, "clean", text="未改动的原文")
    flagged_asset = AssetService(repo).save(
        notebook_id, "flagged.png", "image/png", _IMAGE_BYTES, owner_id)["id"]
    clean_asset = AssetService(repo).save(
        notebook_id, "clean.png", "image/png", _IMAGE_BYTES, owner_id)["id"]
    images = {
        flagged["element_id"]: [{"element_id": "img-f", "asset_id": flagged_asset, "caption": "失效图"}],
        clean["element_id"]: [{"element_id": "img-c", "asset_id": clean_asset, "caption": "正常图"}],
    }
    store = repo._runtime.global_ask_store
    conversation_id, _ = insert_conversation(store, "gconv-http", owner_id)
    for index, (job_id, partial) in enumerate((("gjob-partial", True), ("gjob-clean", False))):
        body = answer(notebook_id, partial=partial, flagged=flagged["element_id"],
                      clean=clean["element_id"], images=images)
        insert_job(store, conversation_id, owner_id, job_id, f"2026-01-01T00:00:0{index + 1}",
                   payload=job_payload(job_id, conversation_id, notebook_id, body))
    return {
        "owner": owner, "owner_id": owner_id, "notebook_id": notebook_id,
        "conversation_id": conversation_id, "flagged": flagged, "clean": clean,
        "flagged_asset": flagged_asset, "clean_asset": clean_asset,
    }


def _assert_partial(body: dict) -> None:
    assert body["answer"] == "甲 [k1],乙 [k2]。"
    assert body["citation_check"] == CHECK
    assert [row.get("verification") for row in body["citations"]] == ["changed", None]
    assert [row.get("verification") for row in body["anchors"]] == ["changed", None]


def test_job_and_conversation_reads_carry_the_check(client, world):
    owner = world["owner"]
    partial = client.get("/api/global-ask/jobs/gjob-partial", headers=owner)
    assert partial.status_code == 200, partial.text
    _assert_partial(partial.json()["answer"])
    clean = client.get("/api/global-ask/jobs/gjob-clean", headers=owner)
    assert clean.status_code == 200 and not _NEW_KEYS & keys_anywhere(clean.json())

    detail = client.get(
        f"/api/global-ask/conversations/{world['conversation_id']}", headers=owner,
    )
    assert detail.status_code == 200
    turns = {turn["job_id"]: turn for turn in detail.json()["turns"]}
    _assert_partial(turns["gjob-partial"]["answer"])
    assert not _NEW_KEYS & keys_anywhere(turns["gjob-clean"])
    # Session list: no badge (decided); the summary rows gain nothing.
    listed = client.get("/api/global-ask/conversations", headers=owner).json()
    assert not _NEW_KEYS & keys_anywhere(listed)


def test_the_stream_terminal_frame_carries_the_check_read_from_the_store(client, world):
    for job_id, partial in (("gjob-partial", True), ("gjob-clean", False)):
        with client.stream(
            "GET", f"/api/global-ask/jobs/{job_id}/stream", headers=world["owner"],
        ) as response:
            assert response.status_code == 200
            frames = [json.loads(line) for line in response.iter_lines() if line.strip()]
        assert [frame["event"] for frame in frames] == ["started", "final"]
        final = frames[-1]["job"]
        # The frame is exactly the job read (the store is the one terminal source).
        assert final == client.get(
            f"/api/global-ask/jobs/{job_id}", headers=world["owner"]).json()
        if partial:
            _assert_partial(final["answer"])
        else:
            assert not _NEW_KEYS & keys_anywhere(frames)


def test_a_flagged_citation_drill_down_is_404_and_a_clean_one_opens(client, world):
    owner = world["owner"]
    flagged = client.get(
        f"/api/global-ask/jobs/gjob-partial/citations/{world['flagged']['element_id']}",
        headers=owner,
    )
    assert flagged.status_code == 404
    assert flagged.json() == {"detail": FLAGGED_CITATION_MESSAGE}
    assert flagged.headers["X-User-Message"] == "1"

    opened = client.get(
        f"/api/global-ask/jobs/gjob-partial/citations/{world['clean']['element_id']}",
        headers=owner,
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["text"] == "未改动的原文"

    # The same element in the clean turn is not flagged there and opens.
    same = client.get(
        f"/api/global-ask/jobs/gjob-clean/citations/{world['flagged']['element_id']}",
        headers=owner,
    )
    assert same.status_code == 200 and same.json()["text"] == "被改动的原文"
    # A foreign or unknown job keeps the refusal it always had.
    unknown = client.get(
        f"/api/global-ask/jobs/gjob-nope/citations/{world['flagged']['element_id']}",
        headers=owner,
    )
    assert unknown.status_code == 404
    assert unknown.json()["detail"] != FLAGGED_CITATION_MESSAGE


def test_admin_and_owner_activity_detail_carry_the_check(client, world):
    admin = _login(client, "admin", "admin")
    owner_id = world["owner_id"]
    for viewer in (admin, world["owner"]):
        partial = client.get(f"/api/admin/users/{owner_id}/asks/gjob-partial", headers=viewer)
        assert partial.status_code == 200, partial.text
        _assert_partial(partial.json()["answer"])
        clean = client.get(f"/api/admin/users/{owner_id}/asks/gjob-clean", headers=viewer)
        assert clean.status_code == 200 and not _NEW_KEYS & keys_anywhere(clean.json())


def test_public_share_page_carries_the_check_and_closes_the_flagged_image(client, world):
    from app.services.conversation_public_view import conversation_asset_alias

    share = client.post(
        f"/api/global-ask/conversations/{world['conversation_id']}/share",
        headers=world["owner"],
    )
    assert share.status_code == 200, share.text
    token = share.json()["share_token"]
    page = client.get(f"/api/public/conversations/{token}")
    assert page.status_code == 200, page.text
    partial, clean = page.json()["turns"]

    assert partial["answer_md"] == "甲 [k1],乙 [k2]。"
    assert partial["citation_check"] == CHECK
    assert [ref.get("verification") for ref in partial["references"]] == ["changed", None]
    assert partial["references"][0]["snippet"] == "摘录-k1"
    assert [image["reference_keys"] for image in partial["images"]] == [["k2"]]
    assert not _NEW_KEYS & keys_anywhere(clean)
    assert [image["reference_keys"] for image in clean["images"]] == [["k1"], ["k2"]]

    flagged_alias = conversation_asset_alias(token, world["flagged_asset"])
    clean_alias = conversation_asset_alias(token, world["clean_asset"])
    # The flagged image is still served for the CLEAN turn that cites it
    # unflagged; the snapshot's union is what the endpoint may serve.
    assert client.get(f"/api/public/conversations/{token}/assets/{clean_alias}").status_code == 200
    assert client.get(f"/api/public/conversations/{token}/assets/{flagged_alias}").status_code == 200


def test_a_flagged_image_is_not_served_when_no_clean_turn_cites_it(client, world):
    from app.api.deps import repository
    from app.services.conversation_public_view import conversation_asset_alias

    store = repository()._runtime.global_ask_store
    conversation_id, _ = insert_conversation(store, "gconv-only-partial", world["owner_id"])
    body = answer(
        world["notebook_id"], partial=True, flagged=world["flagged"]["element_id"],
        clean=world["clean"]["element_id"],
        images={world["flagged"]["element_id"]: [
            {"element_id": "img-f", "asset_id": world["flagged_asset"], "caption": "失效图"},
        ]},
    )
    insert_job(store, conversation_id, world["owner_id"], "gjob-only", "2026-01-01T00:00:05",
               payload=job_payload("gjob-only", conversation_id, world["notebook_id"], body))
    token = client.post(
        f"/api/global-ask/conversations/{conversation_id}/share", headers=world["owner"],
    ).json()["share_token"]

    [turn] = client.get(f"/api/public/conversations/{token}").json()["turns"]
    assert turn["images"] == []
    flagged_alias = conversation_asset_alias(token, world["flagged_asset"])
    refused = client.get(f"/api/public/conversations/{token}/assets/{flagged_alias}")
    assert refused.status_code == 404

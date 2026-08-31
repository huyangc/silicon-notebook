from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'wish.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    return TestClient(create_app())


def _register(client: TestClient, username: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/register", json={"username": username, "password": "pw"}
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _admin(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_users_submit_and_vote_while_only_admin_publishes_plans(client):
    alice = _register(client, "a00000001")
    bob = _register(client, "b00000002")
    admin = _admin(client)

    feature = client.post(
        "/api/wishes",
        headers=alice,
        json={"kind": "feature", "title": "批量导出", "content": "希望支持批量导出。"},
    )
    assert feature.status_code == 201
    feature_id = feature.json()["id"]

    forbidden = client.post(
        "/api/wishes",
        headers=alice,
        json={"kind": "plan", "title": "下周计划", "content": "准备发布。"},
    )
    assert forbidden.status_code == 403

    plan = client.post(
        "/api/wishes",
        headers=admin,
        json={"kind": "plan", "title": "九月更新", "content": "优化报告导出。"},
    )
    assert plan.status_code == 201

    voted = client.post(f"/api/wishes/{feature_id}/vote", headers=bob)
    assert voted.json() == {"wish_id": feature_id, "voted": True, "vote_count": 1}
    items = client.get("/api/wishes?sort=priority", headers=bob).json()["items"]
    assert [item["kind"] for item in items] == ["plan", "feature"]
    assert items[1]["voted_by_me"] is True

    unvoted = client.post(f"/api/wishes/{feature_id}/vote", headers=bob)
    assert unvoted.json() == {"wish_id": feature_id, "voted": False, "vote_count": 0}
    assert client.post(f"/api/wishes/{plan.json()['id']}/vote", headers=bob).status_code == 409


def test_wish_input_is_trimmed_and_over_limit_is_actionable(client):
    user = _register(client, "c00000003")
    created = client.post(
        "/api/wishes",
        headers=user,
        json={"kind": "bug", "title": "  页面卡住  ", "content": "  点击后没有响应。  "},
    )
    assert created.status_code == 201
    assert created.json()["title"] == "页面卡住"
    assert created.json()["content"] == "点击后没有响应。"

    too_long = client.post(
        "/api/wishes",
        headers=user,
        json={"kind": "bug", "title": "x" * 121, "content": "说明"},
    )
    assert too_long.status_code == 400
    assert too_long.headers["X-User-Message"] == "1"
    assert too_long.json()["detail"] == "标题过长，请精简后重试"


def test_wish_wall_requires_authentication(client):
    assert client.get("/api/wishes").status_code == 401


def test_wishes_sort_by_absolute_time_across_offset_fallback(client, tmp_path):
    user = _register(client, "d00000004")
    earlier = client.post(
        "/api/wishes",
        headers=user,
        json={"kind": "bug", "title": "回拨前", "content": "第一条"},
    ).json()
    later = client.post(
        "/api/wishes",
        headers=user,
        json={"kind": "bug", "title": "回拨后", "content": "第二条"},
    ).json()

    # 01:15 at UTC-05 is 45 minutes later than 01:30 at UTC-04, even though
    # raw ISO text has the opposite lexical order around the DST fall-back.
    with sqlite3.connect(tmp_path / "wish.db") as database:
        database.execute(
            "UPDATE wishes SET created_at=? WHERE id=?",
            ("2026-11-01T01:30:00-04:00", earlier["id"]),
        )
        database.execute(
            "UPDATE wishes SET created_at=? WHERE id=?",
            ("2026-11-01T01:15:00-05:00", later["id"]),
        )

    for sort in ("latest", "priority"):
        items = client.get(f"/api/wishes?sort={sort}", headers=user).json()["items"]
        assert [item["id"] for item in items] == [later["id"], earlier["id"]]

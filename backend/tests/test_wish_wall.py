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


def test_wish_wall_uses_the_named_default_page_size(client):
    from app.models.wishes import WISH_PAGE_DEFAULT

    user = _register(client, "e00000005")
    page = client.get("/api/wishes", headers=user).json()
    assert page["limit"] == WISH_PAGE_DEFAULT


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


def test_author_edits_and_deletes_own_wish_while_others_are_refused(client):
    author = _register(client, "f00000006")
    other = _register(client, "f00000007")
    admin = _admin(client)
    wish = client.post(
        "/api/wishes",
        headers=author,
        json={"kind": "feature", "title": "旧标题", "content": "旧说明"},
    ).json()
    assert wish["status"] == "open"

    refused = client.patch(
        f"/api/wishes/{wish['id']}", headers=other, json={"title": "改别人的"}
    )
    assert refused.status_code == 403
    assert refused.json()["detail"] == "只能修改自己发布的内容"

    empty = client.patch(f"/api/wishes/{wish['id']}", headers=author, json={})
    assert empty.status_code == 400
    assert empty.json()["detail"] == "没有需要修改的内容"

    blank = client.patch(
        f"/api/wishes/{wish['id']}", headers=author, json={"title": "   "}
    )
    assert blank.status_code == 400
    assert blank.json()["detail"] == "标题不能为空"

    promoted = client.patch(
        f"/api/wishes/{wish['id']}", headers=author, json={"kind": "plan"}
    )
    assert promoted.status_code == 403
    assert promoted.json()["detail"] == "仅管理员可发布更新计划"

    edited = client.patch(
        f"/api/wishes/{wish['id']}",
        headers=author,
        json={"kind": "bug", "title": "  新标题  ", "content": "  新说明  "},
    )
    assert edited.status_code == 200
    assert edited.json()["kind"] == "bug"
    assert edited.json()["title"] == "新标题"
    assert edited.json()["content"] == "新说明"
    assert edited.json()["updated_at"] >= wish["updated_at"]

    # Unknown fields are rejected rather than silently ignored.
    assert client.patch(
        f"/api/wishes/{wish['id']}", headers=author, json={"status": "done"}
    ).status_code == 422

    # Administrators may edit anyone's wish, including promoting it to a plan.
    by_admin = client.patch(
        f"/api/wishes/{wish['id']}", headers=admin, json={"kind": "plan"}
    )
    assert by_admin.status_code == 200
    assert by_admin.json()["kind"] == "plan"
    # The plan rule is about the kind change: the original author keeps editing
    # the title/content of a plan an admin promoted, but still cannot re-type it.
    still_author = client.patch(
        f"/api/wishes/{wish['id']}", headers=author, json={"title": "作者改标题"}
    )
    assert still_author.status_code == 200
    assert still_author.json()["kind"] == "plan"
    assert still_author.json()["title"] == "作者改标题"
    # Repeating the unchanged kind alongside the edit is not a promotion.
    repeated = client.patch(
        f"/api/wishes/{wish['id']}",
        headers=author,
        json={"kind": "plan", "content": "作者改说明"},
    )
    assert repeated.status_code == 200
    assert repeated.json()["content"] == "作者改说明"
    assert client.patch(
        f"/api/wishes/{wish['id']}", headers=author, json={"kind": "feature"}
    ).status_code == 200

    assert client.delete(f"/api/wishes/{wish['id']}", headers=other).status_code == 403
    assert client.delete(f"/api/wishes/{wish['id']}", headers=author).status_code == 204
    gone = client.delete(f"/api/wishes/{wish['id']}", headers=author)
    assert gone.status_code == 404
    assert gone.json()["detail"] == "这条许愿墙内容不存在或已被删除"
    assert client.patch(
        f"/api/wishes/{wish['id']}", headers=author, json={"title": "x"}
    ).status_code == 404
    assert client.get("/api/wishes", headers=author).json()["total"] == 0


def test_admin_marks_status_and_closed_wishes_sink_in_priority_order(
    client, tmp_path
):
    alice = _register(client, "g00000008")
    bob = _register(client, "g00000009")
    admin = _admin(client)
    popular = client.post(
        "/api/wishes",
        headers=alice,
        json={"kind": "feature", "title": "热门", "content": "很多人想要"},
    ).json()
    quiet = client.post(
        "/api/wishes",
        headers=bob,
        json={"kind": "feature", "title": "冷门", "content": "少数人想要"},
    ).json()
    for voter in (alice, bob):
        assert client.post(f"/api/wishes/{popular['id']}/vote", headers=voter).status_code == 200

    forbidden = client.put(
        f"/api/wishes/{popular['id']}/status", headers=alice, json={"status": "done"}
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"] == "仅管理员可标记处理状态"
    assert client.put(
        f"/api/wishes/{popular['id']}/status", headers=admin, json={"status": "bogus"}
    ).status_code == 422
    missing = client.put(
        "/api/wishes/wish-missing/status", headers=admin, json={"status": "done"}
    )
    assert missing.status_code == 404

    done = client.put(
        f"/api/wishes/{popular['id']}/status", headers=admin, json={"status": "done"}
    )
    assert done.status_code == 200
    assert done.json()["status"] == "done"
    assert done.json()["vote_count"] == 2

    # Closed work sinks below open work in priority order even with more votes;
    # the latest view is untouched by status.
    priority = client.get("/api/wishes?sort=priority", headers=bob).json()["items"]
    assert [item["id"] for item in priority] == [quiet["id"], popular["id"]]
    latest = client.get("/api/wishes?sort=latest", headers=bob).json()["items"]
    assert [item["id"] for item in latest] == [quiet["id"], popular["id"]]
    assert [item["status"] for item in latest] == ["open", "done"]

    only_done = client.get("/api/wishes?status=done", headers=bob).json()
    assert [item["id"] for item in only_done["items"]] == [popular["id"]]
    assert only_done["total"] == 1
    assert client.get("/api/wishes?status=unknown", headers=bob).status_code == 422

    reopened = client.put(
        f"/api/wishes/{popular['id']}/status", headers=admin, json={"status": "in_progress"}
    )
    assert reopened.json()["status"] == "in_progress"
    priority = client.get("/api/wishes?sort=priority", headers=bob).json()["items"]
    assert [item["id"] for item in priority] == [popular["id"], quiet["id"]]

    # Pre-existing rows read as open: the column default is the backfill.
    with sqlite3.connect(tmp_path / "wish.db") as database:
        stored = database.execute(
            "SELECT status FROM wishes WHERE id=?", (quiet["id"],)
        ).fetchone()[0]
    assert stored == "open"

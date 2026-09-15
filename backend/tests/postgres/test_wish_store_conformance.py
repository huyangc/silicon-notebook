"""PostgreSQL conformance for the global wish-wall store."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.wish_store import WishStore


pytestmark = pytest.mark.postgres_integration
NOW = datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)


def _seed_user(database, user_id: str, role: str) -> None:
    with database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,'active',%s,%s)",
            (user_id, f"{user_id}@example.test", user_id, role, NOW, NOW),
        )


@pytest.fixture
def store(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 54
    counter = iter(("wish-feature", "wish-rejected", "wish-plan", "wish-bug"))
    return WishStore(
        postgres_database,
        new_id=lambda _prefix: next(counter),
        now=lambda: NOW,
    )


def test_create_vote_toggle_priority_and_admin_plan_guard(postgres_database, store):
    _seed_user(postgres_database, "user-plain", "user")
    _seed_user(postgres_database, "user-admin", "admin")

    feature = store.create_wish(
        kind="feature", title="批量导出", content="希望支持批量导出。", actor_id="user-plain"
    )
    assert feature["vote_count"] == 0
    assert feature["voted_by_me"] is False
    assert feature["status"] == "open"

    with pytest.raises(PermissionError):
        store.create_wish(
            kind="plan", title="越权计划", content="不应写入。", actor_id="user-plain"
        )

    plan = store.create_wish(
        kind="plan", title="九月更新", content="优化导出。", actor_id="user-admin"
    )
    assert store.toggle_wish_vote(feature["id"], "user-admin") == {
        "wish_id": feature["id"], "voted": True, "vote_count": 1,
    }
    listed = store.list_wishes(actor_id="user-admin", sort="priority")
    assert [item["id"] for item in listed["items"]] == [plan["id"], feature["id"]]
    assert listed["items"][1]["voted_by_me"] is True
    assert store.toggle_wish_vote(feature["id"], "user-admin")["voted"] is False

    with pytest.raises(ValueError):
        store.toggle_wish_vote(plan["id"], "user-plain")


def test_update_delete_and_status_follow_author_and_admin_rules(
    postgres_database, store
):
    _seed_user(postgres_database, "user-author", "user")
    _seed_user(postgres_database, "user-other", "user")
    _seed_user(postgres_database, "user-admin", "admin")

    feature = store.create_wish(
        kind="feature", title="旧标题", content="旧说明", actor_id="user-author"
    )
    bug = store.create_wish(
        kind="bug", title="崩溃", content="点开就崩", actor_id="user-other"
    )
    store.toggle_wish_vote(bug["id"], "user-author")

    # Only the author (or an admin) may edit; a non-admin may not promote to plan.
    with pytest.raises(PermissionError):
        store.update_wish(feature["id"], actor_id="user-other", title="改别人的")
    with pytest.raises(PermissionError):
        store.update_wish(feature["id"], actor_id="user-author", kind="plan")
    edited = store.update_wish(
        feature["id"], actor_id="user-author", title="新标题", content="新说明"
    )
    assert (edited["kind"], edited["title"], edited["content"]) == (
        "feature", "新标题", "新说明",
    )
    promoted = store.update_wish(feature["id"], actor_id="user-admin", kind="plan")
    assert promoted["kind"] == "plan"
    # The plan rule is about the kind change: the author keeps editing a plan an
    # admin promoted, but still cannot promote anything themselves.
    assert store.update_wish(
        feature["id"], actor_id="user-author", title="作者改标题"
    )["title"] == "作者改标题"
    by_admin = store.update_wish(feature["id"], actor_id="user-admin", kind="bug")
    assert by_admin["kind"] == "bug"
    assert by_admin["title"] == "作者改标题"
    with pytest.raises(KeyError):
        store.update_wish("wish-missing", actor_id="user-admin", title="x")

    # Status is admin-only and does not touch votes.
    with pytest.raises(PermissionError):
        store.set_wish_status(bug["id"], status="done", actor_id="user-other")
    done = store.set_wish_status(bug["id"], status="done", actor_id="user-admin")
    assert done["status"] == "done"
    assert done["vote_count"] == 1
    with pytest.raises(KeyError):
        store.set_wish_status("wish-missing", status="done", actor_id="user-admin")

    # Priority order sinks closed work below open work regardless of votes.
    ordered = store.list_wishes(actor_id="user-admin", sort="priority")
    assert [item["id"] for item in ordered["items"]] == [feature["id"], bug["id"]]
    assert store.list_wishes(actor_id="user-admin", status="done")["total"] == 1
    # Both rows are bugs by now (the admin retyped the feature above); kind and
    # status filters compose.
    assert store.list_wishes(
        actor_id="user-admin", kind="bug", status="open"
    )["total"] == 1
    assert store.list_wishes(actor_id="user-admin", kind="feature")["total"] == 0

    # Delete: author or admin; votes go with the row.
    with pytest.raises(PermissionError):
        store.delete_wish(bug["id"], actor_id="user-author")
    store.delete_wish(bug["id"], actor_id="user-admin")
    store.delete_wish(feature["id"], actor_id="user-author")
    with pytest.raises(KeyError):
        store.delete_wish(feature["id"], actor_id="user-author")
    with postgres_database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS c FROM wish_votes"
        ).fetchone()["c"] == 0
    assert store.list_wishes(actor_id="user-admin")["total"] == 0

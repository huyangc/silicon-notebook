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
    assert PostgresMigrator(postgres_database).migrate() == 48
    counter = iter(("wish-feature", "wish-rejected", "wish-plan"))
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

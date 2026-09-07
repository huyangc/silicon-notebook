"""PostgreSQL twin of the ``list_user_usage`` 「来源」总数口径修正 — proves the
new predicate (live notebooks + visible sources + actual-uploader attribution,
NULL falling back to the notebook owner) matches the SQLite-side fast suite in
``backend/tests/test_admin_users.py``.

See docs/superpowers/specs/2026-09-07-admin-usage-overview-usage-signals-design_zh.md
§3 Phase A for the design. Gated behind ``TEST_POSTGRES_URL`` via
``pytest.mark.postgres_integration`` (see backend/tests/postgres/conftest.py).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.query_store import QueryStore as PostgresQueryStore


pytestmark = pytest.mark.postgres_integration


NOW = datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc)


def _insert_user(connection, user_id: str) -> None:
    connection.execute(
        "INSERT INTO users (id,email,display_name,role,created_at,updated_at) "
        "VALUES (%s,%s,%s,'user',%s,%s)",
        (user_id, f"{user_id}@x", user_id.upper(), NOW, NOW),
    )


def _insert_notebook(connection, notebook_id: str, owner: str, status: str = "ready") -> None:
    connection.execute(
        "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (notebook_id, f"NB-{notebook_id}", owner, status, NOW, NOW),
    )


def _insert_source(connection, source_id, notebook_id, created_at, **kw) -> None:
    connection.execute(
        "INSERT INTO sources "
        "(id,notebook_id,title,source_type,status,parse_status,file_name,"
        "error_message,created_at,updated_at,uploaded_by) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            source_id, notebook_id, kw.get("title", "Doc"),
            kw.get("source_type", "pdf"), kw.get("status", "parsed"),
            kw.get("parse_status", "parsed"), kw.get("file_name", "doc.pdf"),
            kw.get("error_message", ""), created_at, created_at,
            kw.get("uploaded_by"),
        ),
    )


@pytest.fixture
def store(postgres_database, postgres_settings):
    assert PostgresMigrator(postgres_database).migrate() == 51
    return PostgresQueryStore(postgres_database, postgres_settings)


def test_sources_excludes_memory_and_knowhow_synthetic_rows(postgres_database, store):
    """VISIBLE_SOURCE_TYPES_PREDICATE 排除合成来源:与展开明细/last_active
    同一谓词,规格 §1.1 指出的矛盾之一。"""
    with postgres_database.write() as connection:
        _insert_user(connection, "u-visible")
        _insert_notebook(connection, "n-visible", "u-visible")
        _insert_source(
            connection, "src-visible", "n-visible", NOW,
            uploaded_by="u-visible",
        )
        _insert_source(
            connection, "src-memory", "n-visible", NOW,
            uploaded_by="u-visible", source_type="memory",
        )
        _insert_source(
            connection, "src-knowhow", "n-visible", NOW,
            uploaded_by="u-visible", source_type="knowhow",
        )
    usage = next(row for row in store.list_user_usage() if row["id"] == "u-visible")
    assert usage["sources"] == 1


def test_sources_attributes_shared_upload_to_the_submitter_not_owner(
    postgres_database, store
):
    """往别人共享库上传的人,来源数记在提交者名下,不是笔记本 owner——与
    `last_active`、`questions`、`reports` 同一归因方向(规格 §1.1)。"""
    with postgres_database.write() as connection:
        _insert_user(connection, "u-owner")
        _insert_user(connection, "u-uploader")
        _insert_notebook(connection, "n-shared", "u-owner")
        _insert_source(
            connection, "src-shared", "n-shared", NOW, uploaded_by="u-uploader",
        )
    usage = {row["id"]: row for row in store.list_user_usage()}
    assert usage["u-owner"]["sources"] == 0
    assert usage["u-uploader"]["sources"] == 1


def test_sources_excludes_copying_notebook(postgres_database, store):
    """深拷贝进行中的笔记本(status='copying')里的来源不计入总数,与
    `NOTEBOOK_LIVE_SQL` 在其它口径里的用法一致。"""
    with postgres_database.write() as connection:
        _insert_user(connection, "u-copying")
        _insert_notebook(connection, "n-copying", "u-copying", status="copying")
        _insert_source(
            connection, "src-copying", "n-copying", NOW, uploaded_by="u-copying",
        )
    usage = next(row for row in store.list_user_usage() if row["id"] == "u-copying")
    assert usage["sources"] == 0


def test_sources_with_null_uploaded_by_falls_back_to_notebook_owner(
    postgres_database, store
):
    """uploaded_by 为 NULL 的行(深拷贝落到接收方库里的副本、极早期未回填行)
    归笔记本 owner——来源数是**资产**口径,与 last_active 的**动作**口径刻意
    不同(规格 §7 决策 2)。"""
    with postgres_database.write() as connection:
        _insert_user(connection, "u-owner-fallback")
        _insert_notebook(connection, "n-null-upload", "u-owner-fallback")
        _insert_source(
            connection, "src-null-upload", "n-null-upload", NOW, uploaded_by=None,
        )
    usage = next(
        row for row in store.list_user_usage() if row["id"] == "u-owner-fallback"
    )
    assert usage["sources"] == 1


def test_sources_retained_branch_counts_by_actor_id(postgres_database, store):
    """留存快照(笔记本已删除)里的来源按 actor_id 计,不再按
    notebook_owner_id——与 last_active 的 retained 候选一致(规格 §3 Phase
    A)。retained_user_activity 写入时(postgres/notebook_store.py 的
    source_rows SELECT)已经用 VISIBLE_SOURCE_TYPES_PREDICATE 过滤过合成来
    源,这里不必再排除 memory/knowhow。"""
    from app.repositories.postgres.notebook_store import NotebookStore

    with postgres_database.write() as connection:
        _insert_user(connection, "u-retained-owner")
        _insert_user(connection, "u-retained-actor")
        _insert_notebook(connection, "n-retained-src", "u-retained-owner")
        _insert_source(
            connection, "src-retained-actor", "n-retained-src", NOW,
            uploaded_by="u-retained-actor",
        )

    notebooks = NotebookStore(
        postgres_database,
        new_id=lambda prefix: f"{prefix}-unused",
        now=lambda: datetime.now(timezone.utc),
        activity_retention_days=180,
    )
    notebooks.delete_row_and_orphan_embeddings("n-retained-src")

    usage = {row["id"]: row for row in store.list_user_usage()}
    assert usage["u-retained-actor"]["sources"] == 1
    assert usage["u-retained-owner"]["sources"] == 0

    with postgres_database.write() as connection:
        connection.execute(
            "UPDATE retained_user_activity "
            "SET expires_at=CURRENT_TIMESTAMP - INTERVAL '1 second'"
        )
    expired_usage = {row["id"]: row for row in store.list_user_usage()}
    assert expired_usage["u-retained-actor"]["sources"] == 0


def test_sources_retained_branch_falls_back_to_owner_when_actor_empty(
    postgres_database, store
):
    """留存来源的 actor_id 为空(删除时 uploaded_by 为 NULL)回落到当时的
    notebook_owner_id——与 live 分支 COALESCE(uploaded_by, nb.created_by) 同一条
    资产口径(规格 §3 Phase A / §7 决策 2)。否则删除笔记本会让这类来源从所有
    人的计数里消失。last_active 仍只看 actor_id,所以 owner 不会因此被刷成活跃。"""
    from app.repositories.postgres.notebook_store import NotebookStore

    with postgres_database.write() as connection:
        _insert_user(connection, "u-null-owner")
        _insert_notebook(connection, "n-null-src", "u-null-owner")
        _insert_source(connection, "src-null-actor", "n-null-src", NOW)

    before = next(row for row in store.list_user_usage() if row["id"] == "u-null-owner")
    assert before["sources"] == 1

    notebooks = NotebookStore(
        postgres_database,
        new_id=lambda prefix: f"{prefix}-unused",
        now=lambda: datetime.now(timezone.utc),
        activity_retention_days=180,
    )
    notebooks.delete_row_and_orphan_embeddings("n-null-src")

    after = next(row for row in store.list_user_usage() if row["id"] == "u-null-owner")
    assert after["sources"] == 1
    assert after["last_active"] is None

"""PostgreSQL twin of the ``list_user_usage`` 「来源」总数口径修正 — proves the
new predicate (live notebooks + visible sources + actual-uploader attribution,
NULL falling back to the notebook owner) matches the SQLite-side fast suite in
``backend/tests/test_admin_users.py``.

See docs/superpowers/specs/2026-09-07-admin-usage-overview-usage-signals-design_zh.md
§3 Phase A for the design. Gated behind ``TEST_POSTGRES_URL`` via
``pytest.mark.postgres_integration`` (see backend/tests/postgres/conftest.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
        "error_message,created_at,updated_at,uploaded_by,file_size) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            source_id, notebook_id, kw.get("title", "Doc"),
            kw.get("source_type", "pdf"), kw.get("status", "parsed"),
            kw.get("parse_status", "parsed"), kw.get("file_name", "doc.pdf"),
            kw.get("error_message", ""), created_at, created_at,
            kw.get("uploaded_by"), kw.get("file_size", 0),
        ),
    )


def _insert_ask(connection, job_id, notebook_id, created_by, created_at, *,
                 status: str = "completed") -> None:
    connection.execute(
        "INSERT INTO ask_jobs "
        "(id,notebook_id,created_by,mode,question,status,created_at,updated_at) "
        "VALUES (%s,%s,%s,'chunk','q?',%s,%s,%s)",
        (job_id, notebook_id, created_by, status, created_at, created_at),
    )


def _insert_report(connection, report_id, notebook_id, created_by, created_at, *,
                    status: str = "done") -> None:
    connection.execute(
        "INSERT INTO reports "
        "(id,notebook_id,question,status,created_by,created_at,updated_at) "
        "VALUES (%s,%s,'q?',%s,%s,%s,%s)",
        (report_id, notebook_id, status, created_by, created_at, created_at),
    )


def _insert_kg_build(connection, job_id, notebook_id, created_by, *,
                      status: str = "completed") -> None:
    connection.execute(
        "INSERT INTO kg_build_jobs "
        "(id,notebook_id,created_by,mode,status,created_at,updated_at) "
        "VALUES (%s,%s,%s,'full',%s,%s,%s)",
        (job_id, notebook_id, created_by, status, NOW, NOW),
    )


def _insert_memory_item(connection, item_id, notebook_id, created_by, *,
                         status: str = "confirmed") -> None:
    connection.execute(
        "INSERT INTO memory_items "
        "(id,notebook_id,created_by,origin,status,title,content_md,created_at,updated_at) "
        "VALUES (%s,%s,%s,'ask_answer',%s,'T','C',%s,%s)",
        (item_id, notebook_id, created_by, status, NOW, NOW),
    )


def _insert_knowhow_table(connection, table_id, notebook_id, created_by) -> None:
    connection.execute(
        "INSERT INTO knowhow_tables "
        "(id,notebook_id,title,created_by,created_at,updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (table_id, notebook_id, table_id, created_by, NOW, NOW),
    )


def _insert_notebook_member(connection, notebook_id, user_id) -> None:
    connection.execute(
        "INSERT INTO notebook_members (notebook_id,user_id,role,added_at) "
        "VALUES (%s,%s,'reader',%s)",
        (notebook_id, user_id, NOW),
    )


def _insert_group(connection, group_id, created_by) -> None:
    connection.execute(
        "INSERT INTO groups (id,name,kind,created_by,created_at,updated_at) "
        "VALUES (%s,%s,'project',%s,%s,%s)",
        (group_id, group_id, created_by, NOW, NOW),
    )


def _insert_group_member(connection, group_id, user_id) -> None:
    connection.execute(
        "INSERT INTO group_members (group_id,user_id,role,added_at) "
        "VALUES (%s,%s,'member',%s)",
        (group_id, user_id, NOW),
    )


@pytest.fixture
def store(postgres_database, postgres_settings):
    assert PostgresMigrator(postgres_database).migrate() == 52
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


# ---------------------------------------------------------------------------
# Phase B/C 使用强度信号(不含 last_seen,那是独立迁移任务):见规格
# docs/superpowers/specs/2026-09-07-admin-usage-overview-usage-signals-design_zh.md
# §3 Phase B/C。与 backend/tests/test_admin_users.py 的 SQLite 侧同一覆盖面。
# ---------------------------------------------------------------------------


def test_storage_bytes_matches_sources_attribution(postgres_database, store):
    """B2:与 `sources` 完全同一归因、同一 live+可见过滤,SUM(file_size)。"""
    with postgres_database.write() as connection:
        _insert_user(connection, "u-storage-self")
        _insert_user(connection, "u-storage-shared")
        _insert_notebook(connection, "n-storage", "u-storage-self")
        _insert_notebook(connection, "n-storage-copy", "u-storage-self", status="copying")
        _insert_source(
            connection, "s-self", "n-storage", NOW,
            uploaded_by="u-storage-self", file_size=1000,
        )
        _insert_source(
            connection, "s-shared", "n-storage", NOW,
            uploaded_by="u-storage-shared", file_size=2000,
        )
        _insert_source(
            connection, "s-null", "n-storage", NOW,
            uploaded_by=None, file_size=300,
        )
        _insert_source(
            connection, "s-memory", "n-storage", NOW,
            uploaded_by="u-storage-self", source_type="memory", file_size=5000,
        )
        _insert_source(
            connection, "s-copying", "n-storage-copy", NOW,
            uploaded_by="u-storage-self", file_size=9000,
        )
    usage = {row["id"]: row for row in store.list_user_usage()}
    assert usage["u-storage-self"]["storage_bytes"] == 1300
    assert usage["u-storage-shared"]["storage_bytes"] == 2000


def test_questions_30d_window(postgres_database, store):
    """B3:近 30 天提问数——窗口内/窗口外/贴近边界各一条,验证
    `created_at >= CURRENT_TIMESTAMP - INTERVAL '30 days'`。真实墙钟相对时间
    (PG 服务器与测试进程同机,时钟偏差可忽略)。"""
    now = datetime.now(timezone.utc)
    in_window = now - timedelta(days=10)
    near_boundary_included = now - timedelta(
        days=29, hours=23, minutes=50
    )
    out_window = now - timedelta(days=40)
    with postgres_database.write() as connection:
        _insert_user(connection, "u-30d")
        _insert_notebook(connection, "n-30d", "u-30d")
        _insert_ask(connection, "j-in", "n-30d", "u-30d", in_window)
        _insert_ask(connection, "j-near", "n-30d", "u-30d", near_boundary_included)
        _insert_ask(connection, "j-out", "n-30d", "u-30d", out_window)
    usage = next(row for row in store.list_user_usage() if row["id"] == "u-30d")
    assert usage["questions_30d"] == 2


def test_questions_30d_retained_branch_same_window(postgres_database, store):
    """B3 retained 分支:笔记本删除后的留存快照仍按同一 30 天窗口计入/排除。"""
    from app.repositories.postgres.notebook_store import NotebookStore

    now = datetime.now(timezone.utc)
    recent = now - timedelta(days=3)
    old = now - timedelta(days=40)
    with postgres_database.write() as connection:
        _insert_user(connection, "u-30d-retained")
        _insert_notebook(connection, "n-30d-retained", "u-30d-retained")
        _insert_ask(connection, "j-recent", "n-30d-retained", "u-30d-retained", recent)
        _insert_ask(connection, "j-old", "n-30d-retained", "u-30d-retained", old)

    notebooks = NotebookStore(
        postgres_database,
        new_id=lambda prefix: f"{prefix}-unused",
        now=lambda: datetime.now(timezone.utc),
        activity_retention_days=180,
    )
    notebooks.delete_row_and_orphan_embeddings("n-30d-retained")

    usage = next(row for row in store.list_user_usage() if row["id"] == "u-30d-retained")
    assert usage["questions_30d"] == 1


def test_questions_failed_and_reports_failed(postgres_database, store):
    """B5:status='failed' 计入失败数;cancelled 不算失败。"""
    with postgres_database.write() as connection:
        _insert_user(connection, "u-failed")
        _insert_notebook(connection, "n-failed", "u-failed")
        _insert_ask(connection, "j-failed", "n-failed", "u-failed", NOW, status="failed")
        _insert_ask(connection, "j-cancelled", "n-failed", "u-failed", NOW, status="cancelled")
        _insert_ask(connection, "j-done", "n-failed", "u-failed", NOW, status="completed")
        _insert_report(connection, "r-failed", "n-failed", "u-failed", NOW, status="failed")
        _insert_report(connection, "r-cancelled", "n-failed", "u-failed", NOW, status="cancelled")
        _insert_report(connection, "r-done", "n-failed", "u-failed", NOW, status="done")
    usage = next(row for row in store.list_user_usage() if row["id"] == "u-failed")
    assert usage["questions_failed"] == 1
    assert usage["reports_failed"] == 1


def test_failed_counts_retained_branch(postgres_database, store):
    """B5 retained 分支:笔记本删除后,留存快照里 status='failed' 的提问/报告
    仍计入失败数。"""
    from app.repositories.postgres.notebook_store import NotebookStore

    with postgres_database.write() as connection:
        _insert_user(connection, "u-failed-retained")
        _insert_notebook(connection, "n-failed-retained", "u-failed-retained")
        _insert_ask(
            connection, "j-failed", "n-failed-retained", "u-failed-retained", NOW,
            status="failed",
        )
        _insert_report(
            connection, "r-failed", "n-failed-retained", "u-failed-retained", NOW,
            status="failed",
        )

    notebooks = NotebookStore(
        postgres_database,
        new_id=lambda prefix: f"{prefix}-unused",
        now=lambda: datetime.now(timezone.utc),
        activity_retention_days=180,
    )
    notebooks.delete_row_and_orphan_embeddings("n-failed-retained")

    usage = next(
        row for row in store.list_user_usage() if row["id"] == "u-failed-retained"
    )
    assert usage["questions_failed"] == 1
    assert usage["reports_failed"] == 1


def test_kg_builds_counts_all_statuses_excludes_empty_creator(postgres_database, store):
    """B4:所有状态都算(与 questions 含失败/取消同口径);created_by 空串不算
    有效用户键。"""
    with postgres_database.write() as connection:
        _insert_user(connection, "u-kg")
        _insert_notebook(connection, "n-kg", "u-kg")
        _insert_kg_build(connection, "kg-done", "n-kg", "u-kg", status="completed")
        _insert_kg_build(connection, "kg-failed", "n-kg", "u-kg", status="failed")
        _insert_kg_build(connection, "kg-noowner", "n-kg", "", status="completed")
    usage = next(row for row in store.list_user_usage() if row["id"] == "u-kg")
    assert usage["kg_builds"] == 2


def test_memory_count_excludes_rejected(postgres_database, store):
    """Phase C:memory_count 排除 status='rejected'。"""
    with postgres_database.write() as connection:
        _insert_user(connection, "u-memory")
        _insert_notebook(connection, "n-memory", "u-memory")
        _insert_memory_item(connection, "m-confirmed", "n-memory", "u-memory", status="confirmed")
        _insert_memory_item(connection, "m-candidate", "n-memory", "u-memory", status="candidate")
        _insert_memory_item(connection, "m-rejected", "n-memory", "u-memory", status="rejected")
    usage = next(row for row in store.list_user_usage() if row["id"] == "u-memory")
    assert usage["memory_count"] == 2


def test_knowhow_tables_excludes_empty_creator(postgres_database, store):
    """Phase C:knowhow_tables 按 created_by,空串不算有效用户键。"""
    with postgres_database.write() as connection:
        _insert_user(connection, "u-knowhow")
        _insert_notebook(connection, "n-knowhow", "u-knowhow")
        _insert_knowhow_table(connection, "k1", "n-knowhow", "u-knowhow")
        _insert_knowhow_table(connection, "k2", "n-knowhow", "")
    usage = next(row for row in store.list_user_usage() if row["id"] == "u-knowhow")
    assert usage["knowhow_tables"] == 1


def test_joined_notebooks_and_groups(postgres_database, store):
    """Phase C:joined_notebooks 按 notebook_members.user_id(他人库的成员身份),
    groups 按 group_members.user_id;自有库/群组创建者不经这两张表。"""
    with postgres_database.write() as connection:
        _insert_user(connection, "u-owner")
        _insert_user(connection, "u-member")
        _insert_notebook(connection, "n-joined", "u-owner")
        _insert_notebook_member(connection, "n-joined", "u-member")
        _insert_group(connection, "g-joined", "u-owner")
        _insert_group_member(connection, "g-joined", "u-member")
    usage = {row["id"]: row for row in store.list_user_usage()}
    assert usage["u-member"]["joined_notebooks"] == 1
    assert usage["u-member"]["groups"] == 1
    assert usage["u-owner"]["joined_notebooks"] == 0
    assert usage["u-owner"]["groups"] == 0

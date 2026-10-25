"""行为面守卫(PG 侧代表性抽查)——`backend/tests/test_notebook_lifecycle_visibility.py`
的 PostgreSQL 对等用例(批 3·W1 T-1;codex PR#653 第 1 轮 P2)。

**取舍(不做 16 方法全量重复)**:两个后端的可见性谓词共享**同一份**
``NOTEBOOK_LIVE_SQL`` 常量字符串(``postgres/access_sql.py`` 与
``sqlite/access_sql.py`` 逐字相等,由
``test_notebook_live_status_literal_guard.py::
test_diag_db_notebook_live_predicate_matches_access_sql`` 钉住)。两侧唯一可能
分叉的地方是各自 SQL 方言的拼接**有没有正确套用**这份常量(占位符
``%s``/``?``、大小写、别名前缀),而不是谓词语义本身——语义已经在 SQLite 侧
16 个方法上被行为钉逐条锁死。所以 PG 侧只需要**抽查**几个代表性拼接形态
(裸列名、带别名前缀、跨表 JOIN)+ mount 闸,不必把 16 个方法在两个后端各测
一遍造成成对维护负担。抽查名单:``owned_notebook_rows``(裸列名)、
``granted_notebook_rows``(带别名前缀 + 多表 JOIN,PG 侧还有
``CROSS JOIN``→标准 JOIN 的方言差异)、``list_user_activity``(点查分支,PG 侧
时间列是 ``timestamptz`` 而非文本)、``notebook_exists_for_owner``(最简单的
裸 SELECT 1)、``notebook_analytics``(KeyError 存在性判定形态)、mount 闸
(``notebook_store.participant_ids``,PG 侧 ``MOUNT_JOIN``/``MOUNT_ORDER`` 有
``COLLATE "C"`` 排序方言差异)、**直连资源端点授权**
(``NOTEBOOK_READ_SQL``/``NOTEBOOK_ADMIN_SQL``/``NOTEBOOK_WRITE_SQL``——codex #653
第 2 轮发现的真规格缺口,`get_notebook` 的目录寻址闸挡不住直连资源端点,这三条
授权谓词必须自己挡住 deleting/copying;PG 侧同样只抽查 owner 的三权 + 群组授权
读者的读权,理由同上)。

变异验证见 SQLite 侧文件与 PR 报告——两侧共享常量,变异只需在其中一侧做一次
即可覆盖两侧(改坏常量本身两侧同时遭殃);这里额外做的是"PG 拼接没抄错"的
确定性检验,不是谓词语义的第二次证明。

**codex #659 R11 P1 新增**:``sharing_store.find_by_token``——裸列名 +
``AND NOTEBOOK_LIVE_SQL``，与 ``notebook_exists_for_owner`` 同一拼接形状，
按上面的取舍只抽查这一个代表（``list_shared_by_owner``/``notebook_row_on``
的 PG 拼接同样是这个形状，语义已在 SQLite 侧钉死，不重复抽查）。
"""
from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_notebook_lifecycle_visibility"),
]

NOW = "2026-09-01T00:00:00+00:00"


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.services.postgres_repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def _insert_user(db, user_id: str) -> None:
    db.execute(
        "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (user_id, f"{user_id}@x", user_id.upper(), "user", "active", user_id, NOW, NOW),
    )


def _insert_notebook(db, nid: str, owner: str) -> None:
    db.execute(
        "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at)"
        " VALUES (%s,%s,%s,%s,%s,%s)",
        (nid, f"NB-{nid}", owner, "ready", NOW, NOW),
    )


@pytest.fixture(params=["deleting", "pending_delete"])
def lifecycle(postgres_repository, request):
    """SQLite 侧同名 fixture 的精简版:只播种本文件实际要用到的几条腿
    (owned/granted/activity/mount),不重建全部 16 个方法的辅助数据。"""
    repo = postgres_repository
    owner_id = "u-owner-pg-lifecycle"
    member_id = "u-member-pg-lifecycle"
    active_id, copying_id, deleting_id = (
        "nb-active-pg-lifecycle",
        "nb-copying-pg-lifecycle",
        "nb-deleting-pg-lifecycle",
    )
    viewer_id = "nb-viewer-pg-lifecycle"
    group_id = "grp-pg-lifecycle"

    with repo._write() as db:
        _insert_user(db, owner_id)
        _insert_user(db, member_id)
        for nid in (active_id, copying_id, deleting_id, viewer_id):
            _insert_notebook(db, nid, owner_id)

        db.execute(
            "INSERT INTO groups (id,name,kind,description,created_by,owner_id,"
            "created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (group_id, "pg-lifecycle-group", "project", "", owner_id, owner_id, NOW, NOW),
        )
        db.execute(
            "INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (group_id, member_id, "member", NOW, owner_id),
        )
        for i, nid in enumerate((active_id, copying_id, deleting_id)):
            db.execute(
                "INSERT INTO notebook_grants "
                "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (f"gnt-pg-{i}", nid, "group", group_id, "viewer", owner_id, NOW),
            )

        for nid in (active_id, copying_id, deleting_id):
            db.execute(
                "INSERT INTO notebook_bases (notebook_id,base_notebook_id,created_at,created_by) "
                "VALUES (%s,%s,%s,%s)",
                (viewer_id, nid, NOW, owner_id),
            )

        for i, nid in enumerate((active_id, copying_id, deleting_id)):
            db.execute(
                "INSERT INTO ask_jobs "
                "(id,notebook_id,created_by,mode,question,status,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (f"ask-pg-{i}", nid, owner_id, "chunk", "q?", "completed", NOW, NOW),
            )

        # codex #659 R11 P1：分享面覆盖（find_by_token 是唯一新出现的 SQL
        # 拼接形态——裸列名 + AND NOTEBOOK_LIVE_SQL，与 notebook_exists_for_
        # owner 同一形状，理由见下方测试。
        share_tokens = {
            active_id: "tok-active-pg", copying_id: "tok-copying-pg",
            deleting_id: "tok-deleting-pg",
        }
        for nid, token in share_tokens.items():
            db.execute(
                "UPDATE notebooks SET is_shared=1,share_token=%s WHERE id=%s",
                (token, nid),
            )

        db.execute("UPDATE notebooks SET status='copying' WHERE id=%s", (copying_id,))
        # Twice, like the SQLite matrix: the six-phase delete in progress and
        # the one-day window with its deadline far away (open, and still
        # missing from every read path).
        db.execute(
            "UPDATE notebooks SET status=%s,status_before_delete='ready',delete_after=%s "
            "WHERE id=%s",
            (request.param, "2999-01-01T00:00:00+00:00", deleting_id),
        )

    return {
        "owner_id": owner_id,
        "member_id": member_id,
        "active_id": active_id,
        "copying_id": copying_id,
        "deleting_id": deleting_id,
        "viewer_id": viewer_id,
        "group_id": group_id,
        "share_tokens": share_tokens,
    }


def _check_owned_notebook_rows(postgres_repository, lifecycle):
    queries = postgres_repository._runtime.queries
    with postgres_repository._connect() as db:
        ids = {
            r["id"]
            for r in queries.owned_notebook_rows(db, lifecycle["owner_id"])
        }
    assert ids == {lifecycle["active_id"], lifecycle["viewer_id"]}


def _check_granted_notebook_rows(postgres_repository, lifecycle):
    queries = postgres_repository._runtime.queries
    with postgres_repository._connect() as db:
        ids = {
            r["id"]
            for r in queries.granted_notebook_rows(db, lifecycle["member_id"])
        }
    assert ids == {lifecycle["active_id"]}


def _check_notebook_exists_for_owner(postgres_repository, lifecycle):
    repo = postgres_repository
    assert repo._runtime.queries.notebook_exists_for_owner(lifecycle["active_id"], lifecycle["owner_id"]) is True
    assert repo._runtime.queries.notebook_exists_for_owner(lifecycle["copying_id"], lifecycle["owner_id"]) is False
    assert repo._runtime.queries.notebook_exists_for_owner(lifecycle["deleting_id"], lifecycle["owner_id"]) is False


def _check_notebook_analytics(postgres_repository, lifecycle):
    repo = postgres_repository
    repo.notebook_analytics(lifecycle["active_id"])
    with pytest.raises(KeyError):
        repo.notebook_analytics(lifecycle["copying_id"])
    with pytest.raises(KeyError):
        repo.notebook_analytics(lifecycle["deleting_id"])


def _check_list_user_activity_scoped(postgres_repository, lifecycle):
    repo = postgres_repository
    active = repo._runtime.queries.list_user_activity(
        lifecycle["owner_id"], notebook_id=lifecycle["active_id"], activity_type="ask"
    )
    assert [item["id"] for item in active["items"]] == ["ask-pg-0"]

    copying = repo._runtime.queries.list_user_activity(
        lifecycle["owner_id"], notebook_id=lifecycle["copying_id"], activity_type="ask"
    )
    assert copying == {"items": [], "has_more": False, "next_cursor": None}

    deleting = repo._runtime.queries.list_user_activity(
        lifecycle["owner_id"], notebook_id=lifecycle["deleting_id"], activity_type="ask"
    )
    assert deleting == {"items": [], "has_more": False, "next_cursor": None}


def _check_mount_gate_participant_resolution(postgres_repository, lifecycle):
    store = postgres_repository._runtime.notebook_store
    with postgres_repository._connect() as db:
        ids = set(store.participant_ids(db, lifecycle["viewer_id"], viewer_id=lifecycle["owner_id"]))
    assert ids == {lifecycle["viewer_id"], lifecycle["active_id"]}


def _check_direct_resource_authorization(postgres_repository, lifecycle):
    """直连资源端点授权(codex #653 R2,真规格缺口):`/sources/{id}`、`/elements`
    等端点不经 `get_notebook` 的目录寻址闸,直接走 `access_sql` 的
    NOTEBOOK_READ_SQL/NOTEBOOK_ADMIN_SQL/NOTEBOOK_WRITE_SQL 授权——deleting/copying
    必须被这三条谓词自己挡住。PG 侧只抽查(取舍见模块 docstring):owner 的三权
    + 群组授权读者(member_id,经 notebook_grants 而非 notebook_members 拿到读权,
    与 SQLite 侧覆盖的成员路径互补)的读权。"""
    repo = postgres_repository
    owner_id = lifecycle["owner_id"]
    active_id = lifecycle["active_id"]
    hidden_ids = (lifecycle["copying_id"], lifecycle["deleting_id"])

    assert repo.user_can_access_notebook(active_id, owner_id) is True
    assert repo._runtime.sharing.user_can_admin_notebook(active_id, owner_id) is True
    assert repo.user_can_read_notebook(active_id, owner_id) is True
    for hidden_id in hidden_ids:
        assert repo.user_can_access_notebook(hidden_id, owner_id) is False
        assert repo._runtime.sharing.user_can_admin_notebook(hidden_id, owner_id) is False
        assert repo.user_can_read_notebook(hidden_id, owner_id) is False

    member_id = lifecycle["member_id"]
    assert repo.user_can_read_notebook(active_id, member_id) is True
    for hidden_id in hidden_ids:
        assert repo.user_can_read_notebook(hidden_id, member_id) is False


def _check_find_by_token(postgres_repository, lifecycle):
    """codex #659 R11 P1：``sharing_store.find_by_token`` 的 PG 拼接抽查——
    裸列名 + ``AND NOTEBOOK_LIVE_SQL``，与 ``notebook_exists_for_owner``
    同一拼接形状（模块 docstring 的取舍：语义已在 SQLite 侧钉死，这里只验证
    PG 方言拼接没抄错）。"""
    store = postgres_repository._runtime.sharing_store
    tokens = lifecycle["share_tokens"]
    assert store.find_by_token(tokens[lifecycle["active_id"]]) == lifecycle["active_id"]
    assert store.find_by_token(tokens[lifecycle["copying_id"]]) is None
    assert store.find_by_token(tokens[lifecycle["deleting_id"]]) is None


def test_readonly_notebook_lifecycle_visibility_matrix(postgres_settings, lifecycle):
    """Exercise every read for both lifecycle states without rebuilding its seed."""
    from app.repositories.postgres import knowledge_counts_cache
    from app.services.postgres_repository import PostgresRepository

    for check in (
        _check_owned_notebook_rows,
        _check_granted_notebook_rows,
        _check_notebook_exists_for_owner,
        _check_notebook_analytics,
        _check_list_user_activity_scoped,
        _check_mount_gate_participant_resolution,
        _check_direct_resource_authorization,
        _check_find_by_token,
    ):
        knowledge_counts_cache.invalidate()
        repository = PostgresRepository(postgres_settings, migrate=False, seed=False)
        try:
            check(repository, lifecycle)
        except Exception as exc:
            exc.add_note(f"read-only notebook lifecycle scenario: {check.__name__}")
            raise
        finally:
            repository.close()
            knowledge_counts_cache.invalidate()


# --------------------------------------------------------------------------
# K3:Memory 读者与本人自助的 ask_jobs/reports 读带笔记本存活谓词(PG 全量,不抽查:
# 这些语句经 `live_read_access_exists_clause` 拼接,`postgres/search.py` 的 Memory
# 候选还有自己独立的一份谓词)。场景与断言与 SQLite 侧共用 `tests/memory_live_cases.py`。
# --------------------------------------------------------------------------
from tests import memory_live_cases as live_cases  # noqa: E402


@pytest.fixture
def memory_world(postgres_settings, tmp_path):
    from app.services.postgres_repository import PostgresRepository

    postgres_settings.storage_dir = str(tmp_path / "postgres-storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    postgres_settings.embed_dim = live_cases.EMBED_DIM
    repository = PostgresRepository(postgres_settings)
    try:
        yield live_cases.build_world(repository, postgres=True)
    finally:
        repository.close()


@pytest.mark.parametrize("state", live_cases.NON_LIVE_STATES)
def test_memory_readers_hide_non_live_notebooks(memory_world, state):
    live_cases.assert_readers_follow_lifecycle(memory_world, state)


def test_memory_open_delete_window_opens_only_the_export(memory_world):
    live_cases.assert_open_window_opens_only_the_export(memory_world)


@pytest.mark.parametrize("state", live_cases.NON_LIVE_STATES)
def test_memory_writes_refused_in_non_live_notebook(memory_world, state):
    live_cases.assert_writes_refused_then_allowed(memory_world, state)


@pytest.mark.parametrize("state", live_cases.NON_LIVE_STATES)
def test_memory_kg_ingest_recheck_ignores_notebook_lifecycle(memory_world, state):
    live_cases.assert_ingest_recheck_survives_lifecycle_flip(memory_world, state)


@pytest.mark.parametrize("state", live_cases.NON_LIVE_STATES)
def test_memory_post_write_read_ignores_a_later_lifecycle_flip(memory_world, state):
    live_cases.assert_post_write_read_survives_lifecycle_flip(memory_world, state)


@pytest.mark.parametrize("state", live_cases.NON_LIVE_STATES)
def test_memory_promotion_approval_refused_in_non_live_notebook(memory_world, state):
    live_cases.assert_promotion_approval_refused_in_non_live(memory_world, state)

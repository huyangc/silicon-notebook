from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace
import threading
import time
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.models.notebooks import NotebookCreate, NotebookUpdate, SharedByMeItem
from app.repositories.ports import ChunkWrite, SourceElementWrite
from app.repositories.postgres.chunk_store import ChunkStore as PostgresChunkStore
from app.repositories.postgres.identity_store import IdentityStore as PostgresIdentityStore
from app.repositories.postgres.kg_build_job_store import (
    KgBuildAlreadyRunning as PostgresKgBuildAlreadyRunning,
    KgBuildJobStore as PostgresKgBuildJobStore,
)
from app.repositories.postgres.group_store import GroupStore as PostgresGroupStore
from app.repositories.postgres.notebook_store import NotebookStore as PostgresNotebookStore
from app.repositories.postgres.model_status_store import (
    ModelStatusStore as PostgresModelStatusStore,
)
from app.repositories.postgres.query_store import QueryStore as PostgresQueryStore
from app.repositories.postgres.sharing_store import SharingStore as PostgresSharingStore
from app.repositories.postgres.source_store import SourceStore as PostgresSourceStore
from app.services.collection_catalog import ENUMERABLE_ELEMENT_KINDS
from app.services.notebook_catalog import NotebookSummaryQuery
from app.services.notebook_sharing import NotebookCopyService
from app.services.repository_runtime import RepositoryCompatibilitySeams
from app.services import repository_facade


NOW = "2026-07-22T10:00:00+00:00"


pytestmark = pytest.mark.postgres_integration


@dataclass
class CoreStores:
    database: Any
    identity: Any
    model_status: Any
    notebooks: Any
    sharing: Any
    groups: Any
    sources: Any
    chunks: Any
    queries: Any
    jobs: Any
    already_running: type[RuntimeError]


def _new_id_factory():
    counters: dict[str, int] = {}

    def new_id(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-conformance-{counters[prefix]}"

    return new_id


@pytest.fixture
def core_stores(request) -> CoreStores:
    new_id = _new_id_factory()

    def now() -> str:
        return NOW

    postgres_database = request.getfixturevalue("postgres_database")
    postgres_settings = request.getfixturevalue("postgres_settings")
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 27
    yield CoreStores(
        database=postgres_database,
        identity=PostgresIdentityStore(postgres_database, postgres_settings),
        model_status=PostgresModelStatusStore(postgres_database),
        notebooks=PostgresNotebookStore(postgres_database, new_id=new_id, now=now),
        sharing=PostgresSharingStore(
            postgres_database,
            postgres_settings,
            now=now,
            insert_row=PostgresSharingStore.insert_row_values,
        ),
        groups=PostgresGroupStore(postgres_database, new_id=new_id, now=now),
        sources=PostgresSourceStore(postgres_database, now=now),
        chunks=PostgresChunkStore(postgres_database),
        queries=PostgresQueryStore(postgres_database, postgres_settings),
        jobs=PostgresKgBuildJobStore(postgres_database, new_id=new_id, now=now),
        already_running=PostgresKgBuildAlreadyRunning,
    )


def _write_sql(
    stores: CoreStores,
    postgres_sql: str,
    params: tuple[object, ...] = (),
) -> None:
    with stores.database.write() as connection:
        connection.execute(
            postgres_sql,
            params,
        )


def _fetch_one(
    stores: CoreStores,
    postgres_sql: str,
    params: tuple[object, ...] = (),
):
    with stores.database.connect() as connection:
        return connection.execute(
            postgres_sql,
            params,
        ).fetchone()


def _fetch_all(
    stores: CoreStores,
    postgres_sql: str,
    params: tuple[object, ...] = (),
):
    with stores.database.connect() as connection:
        return connection.execute(
            postgres_sql,
            params,
        ).fetchall()


def _iso(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


@contextmanager
def _process_timezone(name: str):
    if not hasattr(time, "tzset"):
        pytest.skip("process timezone control requires time.tzset")
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


class _EmptySummaryQueries:
    """Small neutral projection collaborator for exercising real from_row."""

    @staticmethod
    def visible_source_count(connection, notebook_id):
        return 0

    @staticmethod
    def knowledge_type_count_rows(connection, notebook_id, statuses):
        return []

    @staticmethod
    def mounted_bases_row(connection, notebook_id):
        return []

    @staticmethod
    def notebook_has_kg(connection, notebook_id):
        return False

    @staticmethod
    def visible_pending_kg_source_count(connection, notebook_id):
        return 0


def test_canonical_repository_clocks_emit_offset_aware_iso():
    for clock in (repository_facade._now,):
        value = datetime.fromisoformat(clock())
        assert value.utcoffset() is not None


def test_canonical_repository_clocks_preserve_dst_fold_offset(monkeypatch):
    local_zone = ZoneInfo("America/Los_Angeles")

    class FoldOneClock:
        @classmethod
        def now(cls):
            return datetime(2026, 11, 1, 1, 30, fold=1)

    monkeypatch.setattr(repository_facade, "datetime", FoldOneClock)
    with _process_timezone(local_zone.key):
        values = [
            datetime.fromisoformat(clock())
            for clock in (repository_facade._now,)
        ]
    assert {value.utcoffset() for value in values} == {timedelta(hours=-8)}


def test_identity_username_password_and_session_semantics(core_stores: CoreStores):
    store = core_stores.identity
    created = store.create_user("a00123456", "correct horse battery staple")
    assert created.username == "a00123456"
    assert store.authenticate_user("a00123456", "wrong") is None
    assert store.authenticate_user("a00123456", "correct horse battery staple").id == created.id
    with pytest.raises(ValueError, match="username already exists"):
        store.create_user("a00123456", "another password")

    token = store.create_session(created.id)
    assert store.resolve_session(token).id == created.id
    store.delete_session(token)
    assert store.resolve_session(token) is None


def test_set_user_ui_mode_round_trips_and_survives_a_missing_profile_row(
    core_stores: CoreStores,
):
    # codex 双评审:set_user_ui_mode 先按 user_id UPDATE(兼容种子形状的 profile id)，
    # 缺行才走带 ON CONFLICT (id) 的补 INSERT(吞并发双补插竞态)。这里既验证正常
    # round-trip,也验证「profile 行缺失」这条补插路径——手工删掉该行模拟历史数据
    # 缺口,调用仍必须成功且读回正确值,不能因为 UPDATE 分支撞不到行就整体失败。
    store = core_stores.identity
    user = store.create_user("b00123456", "correct horse battery staple")

    updated = store.set_user_ui_mode(user.id, "advanced")
    assert updated.ui_mode == "advanced"
    fetched = _fetch_one(
        core_stores,
        "SELECT ui_mode FROM user_profiles WHERE user_id=%s",
        (user.id,),
    )
    assert fetched["ui_mode"] == "advanced"

    updated_again = store.set_user_ui_mode(user.id, "auto")
    assert updated_again.ui_mode == "auto"
    fetched_again = _fetch_one(
        core_stores,
        "SELECT ui_mode FROM user_profiles WHERE user_id=%s",
        (user.id,),
    )
    assert fetched_again["ui_mode"] == "auto"

    # 只应有一行 —— upsert 走 id 冲突,不会在缺失/重复写入间产生第二条 profile 行。
    row_count = _fetch_one(
        core_stores,
        "SELECT COUNT(*) AS n FROM user_profiles WHERE user_id=%s",
        (user.id,),
    )
    assert row_count["n"] == 1

    _write_sql(
        core_stores,
        "DELETE FROM user_profiles WHERE user_id=%s",
        (user.id,),
    )
    assert _fetch_one(
        core_stores,
        "SELECT 1 FROM user_profiles WHERE user_id=%s",
        (user.id,),
    ) is None

    recreated = store.set_user_ui_mode(user.id, "advanced")
    assert recreated.ui_mode == "advanced"
    fetched_recreated = _fetch_one(
        core_stores,
        "SELECT ui_mode FROM user_profiles WHERE user_id=%s",
        (user.id,),
    )
    assert fetched_recreated["ui_mode"] == "advanced"


def test_set_user_ui_mode_updates_a_non_derived_profile_row_in_place(
    core_stores: CoreStores,
):
    """种子管理员的 profile 行 id 是 `profile-local`,不是 f"profile-{user_id}" 的
    派生形状(seed 早于派生约定)。按 id 冲突的 upsert 撞不到它,会给同一 user_id
    插出第二行,随后 `WHERE user_id=%s` 的读取无确定性排序,偏好看似回退
    (codex R1 P1)。钉住:非派生 id 的既有行必须被原地 UPDATE,行数恒为 1。"""
    store = core_stores.identity
    user = store.create_user("c00123456", "correct horse battery staple")
    # 把该用户的 profile 行改造成种子形状:非派生 id。
    _write_sql(
        core_stores,
        "UPDATE user_profiles SET id=%s WHERE user_id=%s",
        ("profile-seeded-shape", user.id),
    )

    updated = store.set_user_ui_mode(user.id, "advanced")
    assert updated.ui_mode == "advanced"
    rows = _fetch_one(
        core_stores,
        "SELECT COUNT(*) AS n, MAX(ui_mode) AS mode FROM user_profiles WHERE user_id=%s",
        (user.id,),
    )
    assert rows["n"] == 1  # 原地更新,绝不因 id 形状不同插出第二行
    assert rows["mode"] == "advanced"
    # 行 id 保持种子形状不变 —— 说明走的是 UPDATE 而不是删旧插新。
    kept = _fetch_one(
        core_stores,
        "SELECT id FROM user_profiles WHERE user_id=%s",
        (user.id,),
    )
    assert kept["id"] == "profile-seeded-shape"


def test_change_user_password_round_trips_and_scopes_session_revocation(
    core_stores: CoreStores,
):
    """PG 侧自助改密与 SQLite 语义逐字对齐:旧密码失效/新密码生效、keep_token
    保留当前会话且其余全吊销、旧密码错拒绝、内置管理员 user-local 拒绝。"""
    from app.repositories.identity_errors import (
        BuiltinAdminPasswordError,
        PasswordMismatchError,
    )

    store = core_stores.identity
    user = store.create_user("d00123456", "old-pw")
    keep = store.create_session(user.id)
    other = store.create_session(user.id)

    store.change_user_password(user.id, "old-pw", "new-pw", keep_token=keep)

    assert store.authenticate_user("d00123456", "old-pw") is None
    assert store.authenticate_user("d00123456", "new-pw") is not None
    assert store.resolve_session(keep) is not None
    assert store.resolve_session(other) is None

    with pytest.raises(PasswordMismatchError):
        store.change_user_password(user.id, "wrong-old", "next-pw")
    with pytest.raises(ValueError):
        store.change_user_password(user.id, "new-pw", "   ")
    with pytest.raises(KeyError):
        store.change_user_password("user-missing", "a", "b")
    with pytest.raises(BuiltinAdminPasswordError):
        store.change_user_password("user-local", "admin", "next-pw")

    # login_with_password(codex R1 P1):验证+建会话单写事务、对 users 行
    # FOR UPDATE,与改密在行锁上串行。改密后旧密码登录必须失败。
    assert store.login_with_password("d00123456", "old-pw") is None
    logged_in = store.login_with_password("d00123456", "new-pw")
    assert logged_in is not None
    assert store.resolve_session(logged_in[1]) is not None
    assert store.login_with_password("d00999999", "new-pw") is None

    # register_user_with_session(codex R2 P2):注册+首个会话单写事务,注册后
    # 立刻重置必须扫到该会话;重名走 UniqueViolation → ValueError 翻译。
    registered, reg_token = store.register_user_with_session("f00123456", "reg-pw")
    assert store.resolve_session(reg_token) is not None
    with pytest.raises(ValueError):
        store.register_user_with_session("f00123456", "other-pw")


def test_admin_reset_user_password_rechecks_actor_and_revokes_all_sessions(
    core_stores: CoreStores,
):
    """PG 侧管理员重置:actor 现时角色在写事务内复检、目标全部会话吊销、
    目标缺失 KeyError、user-local 拒绝。"""
    from app.repositories.identity_errors import BuiltinAdminPasswordError

    store = core_stores.identity
    actor = store.create_user("e00123456", "actor-pw")
    target = store.create_user("e00123457", "old-pw")
    token_a = store.create_session(target.id)
    token_b = store.create_session(target.id)

    with pytest.raises(PermissionError):
        store.admin_reset_user_password(actor.id, target.id, "new-pw")

    _write_sql(core_stores, "UPDATE users SET role='admin' WHERE id=%s", (actor.id,))
    result = store.admin_reset_user_password(actor.id, target.id, "new-pw")
    assert result == {"id": target.id, "username": "e00123457"}

    assert store.authenticate_user("e00123457", "old-pw") is None
    assert store.authenticate_user("e00123457", "new-pw") is not None
    assert store.resolve_session(token_a) is None
    assert store.resolve_session(token_b) is None

    with pytest.raises(KeyError):
        store.admin_reset_user_password(actor.id, "user-missing", "next-pw")
    with pytest.raises(BuiltinAdminPasswordError):
        store.admin_reset_user_password(actor.id, "user-local", "next-pw")

    # actor==target(管理员重置自己):两行锁的 IN 去重返回一行,两个键命中
    # 同一行,语义与拆开锁时一致(codex R2 P2 的排序锁不改变该路径)。
    result_self = store.admin_reset_user_password(actor.id, actor.id, "self-pw")
    assert result_self["id"] == actor.id
    assert store.login_with_password("e00123456", "self-pw") is not None


def test_identity_session_expiry_and_touch_throttle(core_stores: CoreStores):
    user = core_stores.identity.create_user("h00123456", "password-7")
    expired = core_stores.identity.create_session(user.id)
    _write_sql(
        core_stores,
        "UPDATE auth_sessions SET expires_at=%s WHERE token=%s",
        ("2000-01-01T00:00:00+00:00", expired),
    )
    assert core_stores.identity.resolve_session(expired) is None
    assert _fetch_one(
        core_stores,
        "SELECT 1 FROM auth_sessions WHERE token=%s",
        (expired,),
    ) is None

    active = core_stores.identity.create_session(user.id)
    old_seen = "2000-01-01T00:00:00+00:00"
    _write_sql(
        core_stores,
        "UPDATE auth_sessions SET last_seen_at=%s,expires_at=%s WHERE token=%s",
        (old_seen, "2099-01-01T00:00:00+00:00", active),
    )
    assert core_stores.identity.resolve_session(active).id == user.id
    touched = _fetch_one(
        core_stores,
        "SELECT last_seen_at FROM auth_sessions WHERE token=%s",
        (active,),
    )
    assert _iso(touched["last_seen_at"]) > old_seen


def test_system_model_status_is_monotonic(core_stores: CoreStores):
    core_stores.model_status.record(
        service_id="llm",
        config_fingerprint="new-fingerprint",
        status="ok",
        latency_ms=12,
        code="",
        trigger="manual_test",
        support_id="support-new",
        checked_at="2030-01-01T00:02:00+00:00",
    )
    core_stores.model_status.record(
        service_id="llm",
        config_fingerprint="old-fingerprint",
        status="error",
        latency_ms=0,
        code="upstream_error",
        trigger="observed_failure",
        support_id="support-old",
        checked_at="2030-01-01T00:01:00+00:00",
    )
    status = core_stores.model_status.get_all()["llm"]
    assert status == {
        "config_fingerprint": "new-fingerprint",
        "status": "ok",
        "latency_ms": 12,
        "code": "",
        "trigger": "manual_test",
        "support_id": "support-new",
        "checked_at": "2030-01-01T00:02:00.000000+00:00",
    }


def test_notebook_mount_and_sharing_semantics(core_stores: CoreStores):
    owner = core_stores.identity.create_user("b00123456", "password-1")
    reader = core_stores.identity.create_user("c00123456", "password-2")
    personal_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Personal", purpose="  "), owner.id
    )
    base_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Reference", purpose="manual"), owner.id
    )
    core_stores.notebooks.set_tier(base_id, "base")
    core_stores.notebooks.replace_mounts(personal_id, [base_id, base_id], owner.id)
    assert core_stores.notebooks.participant_notebook_ids(personal_id) == [
        personal_id,
        base_id,
    ]
    assert core_stores.notebooks.get_row(personal_id)["purpose_auto"] == 1

    token = core_stores.sharing.set_share_token(personal_id, "shr-first")
    assert token == "shr-first"
    assert core_stores.sharing.set_share_token(personal_id, "shr-second") == token
    core_stores.sharing.add_member(personal_id, reader.id)
    core_stores.sharing.add_member(personal_id, reader.id)
    assert core_stores.sharing.is_member(personal_id, reader.id)
    assert [row["username"] for row in core_stores.sharing.list_members(personal_id)] == [
        "c00123456"
    ]
    core_stores.sharing.clear_share(personal_id)
    assert core_stores.sharing.find_by_token(token) is None
    assert core_stores.sharing.list_members(personal_id) == []


def _pg_group(core_stores: CoreStores, group_id: str, owner_id: str) -> str:
    with core_stores.database.write() as connection:
        connection.execute(
            "INSERT INTO groups (id,name,kind,description,created_by,created_at,updated_at) "
            "VALUES (%s,%s,'project','',%s,now(),now())",
            (group_id, group_id, owner_id),
        )
    return group_id


def _pg_group_member(
    core_stores: CoreStores, group_id: str, user_id: str, role: str = "member"
) -> None:
    with core_stores.database.write() as connection:
        connection.execute(
            "INSERT INTO group_members (group_id,user_id,role,added_at,added_by) "
            "VALUES (%s,%s,%s,now(),%s)",
            (group_id, user_id, role, user_id),
        )


def _pg_grant(
    core_stores: CoreStores,
    grant_id: str,
    notebook_id: str,
    principal_type: str,
    principal_id: str,
    owner_id: str,
) -> None:
    with core_stores.database.write() as connection:
        connection.execute(
            "INSERT INTO notebook_grants "
            "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
            "VALUES (%s,%s,%s,%s,'viewer',%s,now())",
            (grant_id, notebook_id, principal_type, principal_id, owner_id),
        )


def test_access_predicates_match_the_sqlite_matrix(core_stores: CoreStores):
    """PG 侧的读/写权矩阵必须与 `test_access_sql_contract.py` 的 SQLite 矩阵逐格相同。

    谓词的唯一定义点是 `repositories/*/access_sql.py` 两份镜像文件。SQLite 侧那份
    契约测试跑在 G1,单靠它看不见「只改了一个后端」的分叉;这条把同一张矩阵钉在 G3。
    写权恒 owner-only(只读成员与群组被授权者都是访客),不存在的 notebook 两权皆否。
    P1 扩展的四类授权边主体(user / group / group_admins / everyone)与哨兵停车行的
    fail-safe 一并在此对齐。
    """
    sharing = core_stores.sharing
    owner = core_stores.identity.create_user("d00123456", "password-30")
    member = core_stores.identity.create_user("e00123456", "password-31")
    stranger = core_stores.identity.create_user("f00123456", "password-32")
    grantee = core_stores.identity.create_user("g00123456", "password-33")
    group_member = core_stores.identity.create_user("h00123456", "password-34")
    group_admin = core_stores.identity.create_user("i00123456", "password-35")
    group_plain = core_stores.identity.create_user("j00123456", "password-36")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Access matrix"), owner.id
    )
    sharing.add_member(notebook_id, member.id)

    viewers = _pg_group(core_stores, "grp-viewers", owner.id)
    admins = _pg_group(core_stores, "grp-admins", owner.id)
    _pg_group_member(core_stores, viewers, group_member.id)
    _pg_group_member(core_stores, admins, group_admin.id, "admin")
    _pg_group_member(core_stores, admins, group_plain.id)
    _pg_grant(core_stores, "gr-user", notebook_id, "user", grantee.id, owner.id)
    _pg_grant(core_stores, "gr-group", notebook_id, "group", viewers, owner.id)
    _pg_grant(
        core_stores, "gr-admins", notebook_id, "group_admins", admins, owner.id
    )

    everyone_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Everyone grant"), owner.id
    )
    _pg_grant(core_stores, "gr-everyone", everyone_id, "everyone", "", owner.id)

    # 哨兵停车行(正向 shadow 给冲突行写的非白名单 principal_type)必须谁也不放行。
    sentinel_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Parked grant"), owner.id
    )
    for index, principal_id in enumerate(("", stranger.id, viewers)):
        _pg_grant(
            core_stores,
            f"gr-parked-{index}",
            sentinel_id,
            "__shadow_parked__",
            principal_id,
            owner.id,
        )
    missing = "nb-does-not-exist"

    # (user_id, notebook_id, 期望读权, 期望写权, P1 之前的旧读权口径)
    expected = [
        (owner.id, notebook_id, True, True, True),
        (member.id, notebook_id, True, False, True),  # 只读成员:能读,绝不能写
        (stranger.id, notebook_id, False, False, False),
        (grantee.id, notebook_id, True, False, False),
        (group_member.id, notebook_id, True, False, False),
        (group_admin.id, notebook_id, True, False, False),
        (group_plain.id, notebook_id, False, False, False),
        (owner.id, everyone_id, True, True, True),
        (stranger.id, everyone_id, True, False, False),
        (group_plain.id, everyone_id, True, False, False),
        (owner.id, sentinel_id, True, True, True),
        (stranger.id, sentinel_id, False, False, False),
        (grantee.id, sentinel_id, False, False, False),
        (group_member.id, sentinel_id, False, False, False),
        (owner.id, missing, False, False, False),  # 不存在的 notebook:两权皆否
        (member.id, missing, False, False, False),
        (stranger.id, missing, False, False, False),
        (grantee.id, missing, False, False, False),
    ]
    for user_id, target, expect_read, expect_write, legacy_read in expected:
        assert sharing.user_can_read_notebook(target, user_id) is expect_read
        assert sharing.user_can_access_notebook(target, user_id) is expect_write
        # P1 之前 service 层的旧口径(写权 or 成员):老主体上必须与新谓词逐格相同,
        # 只有授权边主体才允许「新真旧假」。
        legacy = sharing.user_can_access_notebook(
            target, user_id
        ) or sharing.is_member(target, user_id)
        assert legacy is legacy_read
        assert not (legacy and not expect_read)

    # 读权是实时判定而非一次性授予:踢掉成员/组成员/授权边都即刻失读权。
    sharing.remove_member(notebook_id, member.id)
    assert sharing.user_can_read_notebook(notebook_id, member.id) is False
    assert sharing.user_can_access_notebook(notebook_id, member.id) is False
    with core_stores.database.write() as connection:
        connection.execute(
            "DELETE FROM group_members WHERE group_id=%s AND user_id=%s",
            (viewers, group_member.id),
        )
        connection.execute("DELETE FROM notebook_grants WHERE id='gr-user'")
    assert sharing.user_can_read_notebook(notebook_id, group_member.id) is False
    assert sharing.user_can_read_notebook(notebook_id, grantee.id) is False
    # group_admins 那条边没动,组管理员仍可读——上面两条删除没有误伤别的主体。
    assert sharing.user_can_read_notebook(notebook_id, group_admin.id) is True


def test_group_store_crud_and_membership_mirror_the_sqlite_store(
    core_stores: CoreStores,
):
    """群组 / 组成员的 PG 行为必须与 `test_group_routes.py` 的 SQLite 矩阵逐条相同。

    G1 只跑得到 SQLite 那一份,单靠它看不见「只改了一个后端」的分叉。这里钉的三件事
    都是**分叉了不会报错、只会静默走样**的形态:建组是不是真的连组管理员一起落库、
    最后一名组管理员保护是不是真的在同一事务里判、成员/群组清单的顺序会不会随
    collation 漂。
    """
    from app.repositories.ports import LastGroupAdminError

    groups = core_stores.groups
    owner = core_stores.identity.create_user("k00123456", "password-40")
    member = core_stores.identity.create_user("l00123456", "password-41")
    outsider = core_stores.identity.create_user("m00123456", "password-42")

    created = groups.create_group(
        name="项目组", kind="project", description="说明", created_by=owner.id
    )
    # 建组即建组管理员 —— 中间没有「有组无管理员」的窗口。
    assert created["my_role"] == "admin"
    assert created["member_count"] == 1
    assert created["kind"] == "project"
    assert created["description"] == "说明"
    assert isinstance(created["created_at"], str) and created["created_at"]
    group_id = created["id"]

    assert groups.user_group_role(group_id, owner.id) == "admin"
    assert groups.user_group_role(group_id, outsider.id) is None
    assert groups.get_group("grp-missing") is None
    assert groups.get_group(group_id, user_id=outsider.id)["my_role"] == ""

    assert [g["id"] for g in groups.list_groups_for_user(owner.id)] == [group_id]
    assert groups.list_groups_for_user(outsider.id) == []
    everything = groups.list_all_groups(user_id=outsider.id)
    assert [g["id"] for g in everything] == [group_id]
    assert everything[0]["my_role"] == ""

    assert groups.upsert_member(
        group_id, member.id, role="member", added_by=owner.id
    ) == "added"
    assert groups.upsert_member(
        group_id, member.id, role="member", added_by=owner.id
    ) == "updated"
    members = groups.list_members(group_id)
    assert {m["id"]: (m["role"], m["username"]) for m in members} == {
        owner.id: ("admin", "k00123456"),
        member.id: ("member", "l00123456"),
    }
    assert all(isinstance(m["added_at"], str) and m["added_at"] for m in members)
    # 顺序是 `(added_at, user_id)`,与 SQLite 侧逐字相同。这里两行的 added_at 由固定
    # 时钟写成同一个值,所以实际比的是次键——它**必须**存在:并列 added_at 下缺次键
    # 时,PG 的 collation 与 SQLite 的字节序会给出不同的成员顺序。
    assert [m["id"] for m in members] == sorted(m["id"] for m in members)

    # 最后一名组管理员:降级与移除都必须被同一事务里的判定拦下。
    with pytest.raises(LastGroupAdminError):
        groups.upsert_member(group_id, owner.id, role="member", added_by=owner.id)
    with pytest.raises(LastGroupAdminError):
        groups.remove_member(group_id, owner.id)
    groups.upsert_member(group_id, member.id, role="admin", added_by=owner.id)
    assert groups.remove_member(group_id, owner.id) is True
    assert groups.remove_member(group_id, owner.id) is False

    assert groups.update_group(group_id, name="改名") is True
    assert groups.get_group(group_id)["name"] == "改名"
    assert groups.get_group(group_id)["description"] == "说明"  # 未传的字段不动
    assert groups.update_group(group_id) is True  # 合法 no-op
    assert groups.update_group("grp-missing", name="X") is False

    assert groups.find_user_by_username("k00123456")["id"] == owner.id
    assert groups.find_user_by_username("k0012345") is None  # 精确匹配,不认前缀
    assert groups.find_user_by_id(owner.id)["username"] == "k00123456"
    assert groups.find_user_by_id("user-missing") is None


def test_group_grants_crud_and_group_deletion_mirror_the_sqlite_store(
    core_stores: CoreStores,
):
    """授权边 CRUD + 删组清理。重复授权必须是**明确冲突**而不是静默复用。"""
    from app.repositories.ports import GroupGrantAlreadyExists

    groups = core_stores.groups
    owner = core_stores.identity.create_user("n00123456", "password-43")
    group = groups.create_group(
        name="甲组", kind="department", description="", created_by=owner.id
    )
    other = groups.create_group(
        name="乙组", kind="project", description="", created_by=owner.id
    )
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="共享库"), owner.id
    )
    another_notebook = core_stores.notebooks.create_row(
        NotebookCreate(name="另一本"), owner.id
    )

    grant = groups.create_grant(
        notebook_id,
        principal_type="group",
        principal_id=group["id"],
        role="viewer",
        created_by=owner.id,
    )
    assert grant["principal_name"] == "甲组"
    assert grant["principal_kind"] == "department"
    with pytest.raises(GroupGrantAlreadyExists):
        groups.create_grant(
            notebook_id,
            principal_type="group",
            principal_id=group["id"],
            role="admin",
            created_by=owner.id,
        )
    # 同组的另一个主体类型是**另一条**边,不受 UNIQUE 影响。
    groups.create_grant(
        notebook_id,
        principal_type="group_admins",
        principal_id=group["id"],
        role="admin",
        created_by=owner.id,
    )
    # 本端点建不出来、但库里可能存在的 everyone 行:不得被 LEFT JOIN 误配上组名。
    _pg_grant(core_stores, "gr-all", notebook_id, "everyone", "", owner.id)
    listed = groups.list_grants(notebook_id)
    assert {g["principal_type"] for g in listed} == {"group", "group_admins", "everyone"}
    everyone = next(g for g in listed if g["principal_type"] == "everyone")
    assert everyone["principal_name"] == "" and everyone["principal_kind"] == ""

    # grant id 必须与 notebook 一起验:否则「我有一本自己的库的管理权」就变成
    # 「我能删任何库上的授权边」。
    assert groups.grant_row(notebook_id, grant["id"])["id"] == grant["id"]
    assert groups.grant_row(another_notebook, grant["id"]) is None
    assert groups.delete_grant(another_notebook, grant["id"]) is False
    assert groups.delete_grant(notebook_id, grant["id"]) is True
    assert groups.delete_grant(notebook_id, grant["id"]) is False

    groups.create_grant(
        notebook_id,
        principal_type="group",
        principal_id=group["id"],
        role="viewer",
        created_by=owner.id,
    )
    groups.create_grant(
        another_notebook,
        principal_type="group",
        principal_id=other["id"],
        role="viewer",
        created_by=owner.id,
    )
    shared = groups.list_group_shared_notebooks(group["id"])
    assert [item["notebook_id"] for item in shared] == [notebook_id]
    assert shared[0]["name"] == "共享库"
    assert shared[0]["owner_username"] == "n00123456"
    assert sorted(shared[0]["roles"]) == ["admin", "viewer"]  # 同库两条边折成一项

    assert groups.delete_group_grants_for_notebook(group["id"], notebook_id) == 2
    assert groups.list_group_shared_notebooks(group["id"]) == []
    assert groups.delete_group_grants_for_notebook(group["id"], notebook_id) == 0
    # 别的组的边没被误伤。
    assert [i["notebook_id"] for i in groups.list_group_shared_notebooks(other["id"])] == [
        another_notebook
    ]

    # 删组:成员行经 FK 级联消失,指向本组的授权边由同一个写事务显式清掉
    # (`principal_id` 无 FK,级联够不着它)。
    assert groups.delete_group(other["id"]) is True
    assert groups.delete_group(other["id"]) is False
    assert groups.list_grants(another_notebook) == []
    assert _fetch_one(
        core_stores,
        "SELECT COUNT(*) AS c FROM group_members WHERE group_id=%s",
        (other["id"],),
    )["c"] == 0


def test_granted_notebook_rows_matches_the_sqlite_list_projection(
    core_stores: CoreStores,
):
    """「群组」分区的列表投影:只收两个群组主体,`group_admins` 只到组管理员手上。"""
    groups = core_stores.groups
    queries = core_stores.queries
    owner = core_stores.identity.create_user("o00123456", "password-44")
    plain = core_stores.identity.create_user("p00123456", "password-45")
    deputy = core_stores.identity.create_user("q00123456", "password-46")
    group = groups.create_group(
        name="芯片项目", kind="project", description="", created_by=owner.id
    )
    groups.upsert_member(group["id"], plain.id, role="member", added_by=owner.id)
    groups.upsert_member(group["id"], deputy.id, role="admin", added_by=owner.id)
    viewer_notebook = core_stores.notebooks.create_row(
        NotebookCreate(name="全员可读"), owner.id
    )
    admin_notebook = core_stores.notebooks.create_row(
        NotebookCreate(name="仅管理员"), owner.id
    )
    groups.create_grant(
        viewer_notebook,
        principal_type="group",
        principal_id=group["id"],
        role="viewer",
        created_by=owner.id,
    )
    groups.create_grant(
        admin_notebook,
        principal_type="group_admins",
        principal_id=group["id"],
        role="admin",
        created_by=owner.id,
    )
    # 本投影刻意不收 everyone 主体(它沿用公共知识库的隐藏惯例)。
    everyone_notebook = core_stores.notebooks.create_row(
        NotebookCreate(name="全员授权"), owner.id
    )
    _pg_grant(core_stores, "gr-everyone-list", everyone_notebook, "everyone", "", owner.id)

    with core_stores.database.connect() as connection:
        plain_rows = queries.granted_notebook_rows(connection, plain.id)
        deputy_rows = queries.granted_notebook_rows(connection, deputy.id)
    assert [row["id"] for row in plain_rows] == [viewer_notebook]
    assert plain_rows[0]["_owner_username"] == "o00123456"
    assert plain_rows[0]["_group_id"] == group["id"]
    assert plain_rows[0]["_group_name"] == "芯片项目"
    assert plain_rows[0]["_group_kind"] == "project"
    assert sorted(row["id"] for row in deputy_rows) == sorted(
        [viewer_notebook, admin_notebook]
    )

    # 降级即失效;半拷贝的哨兵状态与另外两段一样被排除。
    groups.upsert_member(group["id"], deputy.id, role="member", added_by=owner.id)
    _write_sql(
        core_stores,
        "UPDATE notebooks SET status='copying' WHERE id=%s",
        (viewer_notebook,),
    )
    with core_stores.database.connect() as connection:
        assert queries.granted_notebook_rows(connection, deputy.id) == []


def test_shared_members_validate_as_shared_by_me_string_fields(
    core_stores: CoreStores,
):
    owner = core_stores.identity.create_user("u00123456", "password-20")
    reader = core_stores.identity.create_user("v00123456", "password-21")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Shared members"), owner.id
    )
    core_stores.sharing.add_member(notebook_id, reader.id)

    item = SharedByMeItem(
        id=notebook_id,
        name="Shared members",
        share_token="shr-member-time",
        mode="readonly",
        size={"sources": 0},
        members=core_stores.sharing.list_members(notebook_id),
    )
    assert item.members == [
        {"username": "v00123456", "added_at": "2026-07-22T10:00:00+00:00"}
    ]


@pytest.mark.postgres_integration
def test_pg_task6_timestamp_inputs_normalize_naive_local_seams(
    postgres_database,
    postgres_settings,
):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 27
    local_zone = ZoneInfo("America/Los_Angeles")
    naive_local = datetime(2026, 7, 22, 3, 0, 0)
    expected_utc = naive_local.replace(tzinfo=local_zone).astimezone(timezone.utc)
    new_id = _new_id_factory()

    def clock() -> str:
        return naive_local.isoformat()

    identity = PostgresIdentityStore(postgres_database, postgres_settings)
    notebooks = PostgresNotebookStore(postgres_database, new_id=new_id, now=clock)
    sharing = PostgresSharingStore(
        postgres_database,
        postgres_settings,
        now=clock,
        insert_row=PostgresSharingStore.insert_row_values,
    )
    sources = PostgresSourceStore(postgres_database, now=clock)
    chunks = PostgresChunkStore(postgres_database)
    jobs = PostgresKgBuildJobStore(postgres_database, new_id=new_id, now=clock)

    with _process_timezone(local_zone.key):
        owner = identity.create_user("w00123456", "password-22")
        reader = identity.create_user("x00123456", "password-23")
        notebook_id = notebooks.create_row(NotebookCreate(name="Local clock"), owner.id)
        sources.insert_source(
            source_id="src-local-clock",
            notebook_id=notebook_id,
            title="Local clock source",
            source_type="markdown",
            status="parsed",
            parse_status="parsed",
            file_name="clock.md",
            file_path="uploads/clock.md",
            file_size=1,
            file_hash="clock",
            summary="",
            doc_type="",
        )
        with postgres_database.write() as connection:
            sources.replace_elements(
                connection,
                "src-local-clock",
                [SourceElementWrite("el-local-clock", "paragraph", "p", "body", {})],
                created_at=naive_local.isoformat(),
            )
        chunks.replace_source_chunks(
            "src-local-clock",
            notebook_id,
            [ChunkWrite("chunk-local-clock", "body", "p", ("el-local-clock",))],
            created_at=naive_local.replace(tzinfo=local_zone),
        )
        sharing.add_member(notebook_id, reader.id)
        job = jobs.create_job(notebook_id, owner.id, "incremental", 1)

        with postgres_database.connect() as connection:
            stored = connection.execute(
                "SELECT n.created_at AS notebook_created,s.created_at AS source_created,"
                "e.created_at AS element_created,c.created_at AS chunk_created,"
                "m.added_at AS member_added,j.created_at AS job_created "
                "FROM notebooks n JOIN sources s ON s.notebook_id=n.id "
                "JOIN source_elements e ON e.source_id=s.id "
                "JOIN chunks c ON c.source_id=s.id "
                "JOIN notebook_members m ON m.notebook_id=n.id "
                "JOIN kg_build_jobs j ON j.notebook_id=n.id "
                "WHERE n.id=%s AND j.id=%s",
                (notebook_id, job["id"]),
            ).fetchone()
    assert set(stored.values()) == {expected_utc}


@pytest.mark.postgres_integration
def test_pg_copy_sentinel_sweep_respects_naive_local_creation_time(
    postgres_database,
    postgres_settings,
):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 27
    settings = postgres_settings.model_copy(
        update={"notebook_copy_stale_seconds": 60}
    )
    new_id = _new_id_factory()
    local_zone = ZoneInfo("America/Los_Angeles")
    reference_utc = datetime.now(timezone.utc).replace(microsecond=0)
    clock_value = reference_utc.astimezone(local_zone).replace(tzinfo=None).isoformat()

    def clock() -> str:
        return clock_value

    identity = PostgresIdentityStore(postgres_database, settings)
    notebooks = PostgresNotebookStore(postgres_database, new_id=new_id, now=clock)
    sharing = PostgresSharingStore(
        postgres_database,
        settings,
        now=clock,
        insert_row=PostgresSharingStore.insert_row_values,
    )

    with _process_timezone(local_zone.key):
        owner = identity.create_user("y00123456", "password-24")
        source_notebook_id = notebooks.create_row(
            NotebookCreate(name="Sentinel template"), owner.id
        )
        template = sharing.snapshot_copy_rows(source_notebook_id)["notebooks"][0]

        def insert_sentinel(notebook_id: str, created_at: str) -> None:
            row = dict(template)
            row.update(
                id=notebook_id,
                name=notebook_id,
                status="copying",
                created_by=owner.id,
                created_at=created_at,
                updated_at=created_at,
            )
            sharing.insert_copy_rows("notebooks", [row], chunk_size=1)

        insert_sentinel("nb-copy-fresh-local", clock_value)
        assert sharing.sweep_stale_copies(created_by=owner.id) == 0

        stale_local = (
            reference_utc - timedelta(seconds=120)
        ).astimezone(local_zone).replace(tzinfo=None).isoformat()
        insert_sentinel("nb-copy-stale-local", stale_local)
        assert sharing.sweep_stale_copies(created_by=owner.id) == 1

        with postgres_database.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM notebooks WHERE status='copying' ORDER BY id COLLATE \"C\""
            ).fetchall()
    assert [row["id"] for row in rows] == ["nb-copy-fresh-local"]


@pytest.mark.postgres_integration
def test_pg_copy_sentinel_sweep_preserves_production_clock_dst_fold(
    postgres_database,
    postgres_settings,
    monkeypatch,
):
    from app.repositories.postgres import sharing_store as pg_sharing_store
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 27
    settings = postgres_settings.model_copy(
        update={"notebook_copy_stale_seconds": 120}
    )
    new_id = _new_id_factory()
    local_zone = ZoneInfo("America/Los_Angeles")

    class FoldClock:
        current = datetime(2026, 11, 1, 1, 30, fold=1)

        @classmethod
        def now(cls):
            return cls.current

    monkeypatch.setattr(repository_facade, "datetime", FoldClock)
    monkeypatch.setattr(
        pg_sharing_store,
        "utc_now",
        lambda: datetime(2026, 11, 1, 9, 31, tzinfo=timezone.utc),
    )
    identity = PostgresIdentityStore(postgres_database, settings)
    notebooks = PostgresNotebookStore(
        postgres_database,
        new_id=new_id,
        now=repository_facade._now,
    )
    sharing = PostgresSharingStore(
        postgres_database,
        settings,
        now=repository_facade._now,
        insert_row=PostgresSharingStore.insert_row_values,
    )

    with _process_timezone(local_zone.key):
        owner = identity.create_user("z00123456", "password-25")
        source_notebook_id = notebooks.create_row(
            NotebookCreate(name="Fold sentinel template"), owner.id
        )
        template = sharing.snapshot_copy_rows(source_notebook_id)["notebooks"][0]

        def insert_sentinel(notebook_id: str, created_at: str) -> None:
            row = dict(template)
            row.update(
                id=notebook_id,
                name=notebook_id,
                status="copying",
                created_by=owner.id,
                created_at=created_at,
                updated_at=created_at,
            )
            sharing.insert_copy_rows("notebooks", [row], chunk_size=1)

        fresh = repository_facade._now()
        assert datetime.fromisoformat(fresh).utcoffset() == timedelta(hours=-8)
        insert_sentinel("nb-copy-fold-fresh", fresh)

        FoldClock.current = datetime(2026, 11, 1, 1, 27, fold=1)
        stale = repository_facade._now()
        assert datetime.fromisoformat(stale).utcoffset() == timedelta(hours=-8)
        insert_sentinel("nb-copy-fold-stale", stale)

        assert sharing.sweep_stale_copies(created_by=owner.id) == 1
        with postgres_database.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM notebooks WHERE status='copying' ORDER BY id COLLATE \"C\""
            ).fetchall()
    assert [row["id"] for row in rows] == ["nb-copy-fold-fresh"]


def test_notebook_and_source_created_labels_use_local_calendar_date(
    core_stores: CoreStores,
):
    local_zone = ZoneInfo("Asia/Shanghai")
    local_created = datetime(2026, 7, 23, 0, 30, tzinfo=local_zone).isoformat()

    def clock() -> str:
        return local_created

    notebooks = type(core_stores.notebooks)(
        core_stores.database,
        new_id=_new_id_factory(),
        now=clock,
    )
    sources = type(core_stores.sources)(core_stores.database, now=clock)
    with _process_timezone(local_zone.key):
        owner = core_stores.identity.create_user("a00123456", "password-26")
        notebook_id = notebooks.create_row(
            NotebookCreate(name="Local calendar date"), owner.id
        )
        sources.insert_source(
            source_id="src-local-calendar",
            notebook_id=notebook_id,
            title="Local calendar source",
            source_type="markdown",
            status="parsed",
            parse_status="parsed",
            file_name="calendar.md",
            file_path="uploads/calendar.md",
            file_size=1,
            file_hash="calendar",
            summary="",
            doc_type="",
        )
        summaries = NotebookSummaryQuery(core_stores.database, _EmptySummaryQueries())
        with core_stores.database.connect() as connection:
            notebook = summaries.from_row(connection, notebooks.get_row(notebook_id))
        source = sources.get_source("src-local-calendar")
    assert notebook.created_label == "2026年7月23日"
    assert source.created_label == "2026年7月23日"


def test_cross_owner_base_visibility_fails_closed_after_downgrade(
    core_stores: CoreStores,
):
    owner = core_stores.identity.create_user("i00123456", "password-8")
    publisher = core_stores.identity.create_user("j00123456", "password-9")
    personal_id = core_stores.notebooks.create_row(NotebookCreate(name="Active"), owner.id)
    base_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Published secret name"), publisher.id
    )
    core_stores.notebooks.set_tier(base_id, "base")
    core_stores.notebooks.replace_mounts(personal_id, [base_id], owner.id)
    assert core_stores.notebooks.participant_notebook_ids(personal_id) == [
        personal_id,
        base_id,
    ]

    core_stores.notebooks.set_tier(base_id, "personal")
    assert core_stores.notebooks.participant_notebook_ids(personal_id) == [personal_id]
    edge = core_stores.notebooks.list_mount_edges_for_notebook(personal_id)[0]
    assert edge["active"] is False
    assert edge["name"] == "已不可用的知识库"


def test_replace_mounts_reuses_one_batch_timestamp(core_stores: CoreStores):
    owner = core_stores.identity.create_user("r00123456", "password-17")
    active_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Mount active"), owner.id
    )
    base_ids = [
        core_stores.notebooks.create_row(NotebookCreate(name=name), owner.id)
        for name in ("Mount base A", "Mount base B")
    ]
    calls: list[str] = []

    def increasing_clock() -> str:
        value = f"2026-07-22T10:00:0{len(calls)}+00:00"
        calls.append(value)
        return value

    store = type(core_stores.notebooks)(
        core_stores.database,
        new_id=_new_id_factory(),
        now=increasing_clock,
    )
    store.replace_mounts(active_id, base_ids, owner.id)

    with core_stores.database.connect() as connection:
        rows = connection.execute(
            "SELECT created_at FROM notebook_bases WHERE notebook_id=%s "
            'ORDER BY base_notebook_id COLLATE "C"',
            (active_id,),
        ).fetchall()
    assert calls == ["2026-07-22T10:00:00+00:00"]
    assert [_iso(row["created_at"]) for row in rows] == [calls[0], calls[0]]


def test_notebook_raw_rows_feed_neutral_summary_json_lists(
    core_stores: CoreStores,
):
    owner = core_stores.identity.create_user("s00123456", "password-18")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Raw JSON boundary"), owner.id
    )
    core_stores.notebooks.update_row(
        notebook_id,
        NotebookUpdate(
            expected_questions=["怎么验证？", "How to verify?"],
            source_types=["pdf", "markdown"],
            taxonomy=["analog", "模拟"],
        ),
    )
    core_stores.sharing.set_share_token(notebook_id, "shr-raw-json")
    summaries = NotebookSummaryQuery(core_stores.database, _EmptySummaryQueries())

    rows = [
        core_stores.notebooks.get_row(notebook_id),
        core_stores.sharing.notebook_row(notebook_id),
    ]
    with core_stores.database.connect() as connection:
        rows.append(core_stores.sharing.notebook_row_on(connection, notebook_id))
        projected = [summaries.from_row(connection, row) for row in rows]

    for summary in projected:
        assert summary.expected_questions == ["怎么验证？", "How to verify?"]
        assert summary.source_types == ["pdf", "markdown"]
        assert summary.taxonomy == ["analog", "模拟"]

    # This sharing-list projection does not expose any JSON notebook columns,
    # so it cannot accidentally cross the raw-row summary boundary.
    shared_rows = core_stores.sharing.list_shared_by_owner(owner.id)
    assert set(shared_rows[0].keys()) == {"id", "name", "share_token"}


def test_copy_snapshot_excludes_backend_ordinals_and_serializes_json(
    core_stores: CoreStores,
):
    owner = core_stores.identity.create_user("n00123456", "password-13")
    notebook_id = core_stores.notebooks.create_row(NotebookCreate(name="Copy"), owner.id)
    core_stores.sources.insert_source(
        source_id="src-copy",
        notebook_id=notebook_id,
        title="Copy source",
        source_type="markdown",
        status="parsed",
        parse_status="parsed",
        file_name="copy.md",
        file_path="uploads/copy.md",
        file_size=1,
        file_hash="hash",
        summary="",
        doc_type="",
    )
    with core_stores.database.write() as connection:
        core_stores.sources.replace_elements(
            connection,
            "src-copy",
            [SourceElementWrite("el-copy", "paragraph", "p", "body", {"k": 1})],
            created_at=NOW,
        )
    core_stores.chunks.replace_source_chunks(
        "src-copy",
        notebook_id,
        [ChunkWrite("chunk-copy", "body", "p", ("el-copy",))],
        created_at=NOW,
    )
    snapshot = core_stores.sharing.snapshot_copy_rows(notebook_id)
    assert "ordinal" not in snapshot["source_elements"][0]
    assert "ordinal" not in snapshot["chunks"][0]
    assert isinstance(snapshot["source_elements"][0]["metadata"], str)
    assert isinstance(snapshot["chunks"][0]["element_ids"], str)

    destination = dict(snapshot["notebooks"][0])
    destination.update(id="nb-copy-destination", status="copying")
    core_stores.sharing.insert_copy_rows(
        "notebooks", [destination], chunk_size=100
    )
    assert core_stores.sharing.notebook_row("nb-copy-destination")["status"] == "copying"
    core_stores.sharing.compensate_copy("nb-copy-destination")
    assert core_stores.sharing.notebook_row("nb-copy-destination") is None


def test_full_notebook_copy_preserves_source_fact_jsonb(
    core_stores: CoreStores, tmp_path,
):
    owner = core_stores.identity.create_user("f00123456", "password-13")
    recipient = core_stores.identity.create_user("f00987654", "password-13")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Fact copy"), owner.id
    )
    core_stores.sources.insert_source(
        source_id="src-fact-copy",
        notebook_id=notebook_id,
        title="Fact source",
        source_type="markdown",
        status="parsed",
        parse_status="parsed",
        file_name="fact.md",
        file_path="uploads/fact.md",
        file_size=1,
        file_hash="fact-hash",
        summary="",
        doc_type="",
    )
    with core_stores.database.write() as connection:
        core_stores.sources.replace_elements(
            connection,
            "src-fact-copy",
            [SourceElementWrite("el-fact-copy", "paragraph", "p", "body", {})],
            created_at=NOW,
        )
        connection.execute(
            "INSERT INTO knowledge_objects "
            "(id, notebook_id, object_type, status, owner, payload, evidence, "
            "source_id, created_at, updated_at) VALUES "
            "(%s, %s, 'concept', 'approved', '', %s::jsonb, %s::jsonb, %s, %s, %s)",
            (
                "ko-fact-copy",
                notebook_id,
                '{"name":"source fact"}',
                '[{"source_id":"src-fact-copy","element_id":"el-fact-copy"}]',
                "src-fact-copy",
                NOW,
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO knowledge_source_facts "
            "(id, notebook_id, source_id, source_generation, local_object_id, "
            "global_object_id, object_type, payload, evidence, projection_version, "
            "created_at, updated_at) VALUES "
            "(%s, %s, %s, 'run-fact-copy', 'local-1', %s, 'concept', "
            "%s::jsonb, %s::jsonb, 1, %s, %s)",
            (
                "ksf-fact-copy",
                notebook_id,
                "src-fact-copy",
                "ko-fact-copy",
                '{"name":"source fact"}',
                '[{"source_id":"src-fact-copy","element_id":"el-fact-copy"}]',
                NOW,
                NOW,
            ),
        )
        connection.execute(
            "INSERT INTO knowledge_source_fact_elements "
            "(fact_id, notebook_id, source_id, source_generation, element_id, created_at) "
            "VALUES ('ksf-fact-copy', %s, 'src-fact-copy', 'run-fact-copy', "
            "'el-fact-copy', %s)",
            (notebook_id, NOW),
        )
        connection.execute(
            "INSERT INTO knowledge_source_fact_backfills "
            "(source_id,notebook_id,source_generation,projection_version,status,"
            "after_object_id,objects_scanned,facts_written,incomplete_objects,"
            "failure_code,created_at,updated_at) VALUES "
            "('src-fact-copy',%s,'run-fact-copy',1,'complete','ko-fact-copy',"
            "1,1,0,'',%s,%s)",
            (notebook_id, NOW, NOW),
        )

    counters: dict[str, int] = {}

    def new_id(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-fact-copy-{counters[prefix]}"

    class Catalog:
        def get_notebook(self, target_id: str):
            row = core_stores.sharing.notebook_row(target_id)
            assert row is not None
            return SimpleNamespace(
                id=target_id, name=row["name"], status=row["status"]
            )

    copied = NotebookCopyService(
        store=core_stores.sharing,
        catalog=Catalog(),
        seams=RepositoryCompatibilitySeams(
            new_id=new_id,
            now=lambda: NOW,
            copy_chunk_size=lambda: 100,
            remap_json_ids=repository_facade._remap_json_ids,
            in_chunk_size=lambda: 500,
        ),
        storage_dir=lambda: tmp_path,
        schedule_projection=lambda _table_id: None,
    ).copy_notebook(notebook_id, new_owner_id=recipient.id)

    with core_stores.database.connect() as connection:
        fact = connection.execute(
            "SELECT payload, evidence, global_object_id, source_generation "
            "FROM knowledge_source_facts "
            "WHERE notebook_id=%s",
            (copied.id,),
        ).fetchone()
        binding = connection.execute(
            "SELECT source_id, element_id, source_generation "
            "FROM knowledge_source_fact_elements "
            "WHERE notebook_id=%s",
            (copied.id,),
        ).fetchone()
        backfill = connection.execute(
            "SELECT source_id,after_object_id,source_generation "
            "FROM knowledge_source_fact_backfills "
            "WHERE notebook_id=%s",
            (copied.id,),
        ).fetchone()
        copied_run = connection.execute(
            "SELECT id,status,error_message FROM extraction_runs "
            "WHERE notebook_id=%s AND source_id=%s AND run_type='kg'",
            (copied.id, binding["source_id"]),
        ).fetchone()
    assert fact["payload"] == {"name": "source fact"}
    assert fact["evidence"][0]["source_id"] == binding["source_id"]
    assert fact["evidence"][0]["element_id"] == binding["element_id"]
    assert fact["global_object_id"] != "ko-fact-copy"
    assert copied_run is not None
    assert copied_run["status"] == "completed"
    assert copied_run["error_message"].startswith("kg objects=")
    assert fact["source_generation"] == copied_run["id"]
    assert binding["source_generation"] == copied_run["id"]
    assert backfill["source_id"] == binding["source_id"]
    assert backfill["source_generation"] == copied_run["id"]
    assert backfill["after_object_id"] == fact["global_object_id"]


def test_source_jsonb_hydration_and_ordinal_sample_order(core_stores: CoreStores):
    owner = core_stores.identity.create_user("d00123456", "password-3")
    notebook_id = core_stores.notebooks.create_row(NotebookCreate(name="Sources"), owner.id)
    core_stores.sources.insert_source(
        source_id="src-core",
        notebook_id=notebook_id,
        title="Core source",
        source_type="markdown",
        status="parsed",
        parse_status="parsed",
        file_name="core.md",
        file_path="uploads/core.md",
        file_size=10,
        file_hash="hash",
        summary="summary",
        doc_type="academic_paper",
    )
    elements = (
        SourceElementWrite("el-z", "paragraph", "first", "first body", {"rank": 1}),
        SourceElementWrite("el-a", "paragraph", "second", "second body", {"rank": 2}),
    )
    with core_stores.database.write() as connection:
        core_stores.sources.replace_elements(
            connection, "src-core", elements, created_at=NOW
        )

    hydrated = {item.id: item for item in core_stores.sources.source_elements("src-core")}
    assert hydrated["el-z"].metadata == {"rank": 1}
    assert hydrated["el-a"].metadata == {"rank": 2}
    assert core_stores.sources.notebook_element_sample(notebook_id, max_chars=1000) == [
        {"location_label": "first", "text": "first body"},
        {"location_label": "second", "text": "second body"},
    ]


def test_source_visibility_physical_count_and_delete_cascade(core_stores: CoreStores):
    owner = core_stores.identity.create_user("k00123456", "password-10")
    notebook_id = core_stores.notebooks.create_row(NotebookCreate(name="Visibility"), owner.id)
    for source_id, source_type in (
        ("src-visible", "markdown"),
        ("src-memory", "memory"),
        ("src-knowhow", "knowhow"),
    ):
        core_stores.sources.insert_source(
            source_id=source_id,
            notebook_id=notebook_id,
            title=source_id,
            source_type=source_type,
            status="parsed",
            parse_status="parsed",
            file_name=f"{source_id}.md",
            file_path=f"uploads/{source_id}.md",
            file_size=1,
            file_hash="hash",
            summary="",
            doc_type="",
        )
    assert [item.id for item in core_stores.sources.list_sources(notebook_id)] == [
        "src-visible"
    ]
    physical = _fetch_one(
        core_stores,
        "SELECT COUNT(*) AS c FROM sources WHERE notebook_id=%s",
        (notebook_id,),
    )
    assert physical["c"] == 3

    with core_stores.database.write() as connection:
        core_stores.sources.replace_elements(
            connection,
            "src-visible",
            [SourceElementWrite("el-delete", "paragraph", "p", "body", {})],
            created_at=NOW,
        )
    core_stores.chunks.replace_source_chunks(
        "src-visible",
        notebook_id,
        [ChunkWrite("chunk-delete", "body", "p", ("el-delete",))],
        created_at=NOW,
    )
    with core_stores.database.write() as connection:
        core_stores.sources.delete_source_row(connection, "src-visible")
    assert _fetch_one(
        core_stores,
        "SELECT 1 FROM source_elements WHERE id=%s",
        ("el-delete",),
    ) is None
    assert _fetch_one(
        core_stores,
        "SELECT 1 FROM chunks WHERE id=%s",
        ("chunk-delete",),
    ) is None


def test_source_agent_provenance_matches_sqlite_semantics(core_stores: CoreStores):
    """v26 `sources.agent_profile_id`: "" -> NULL, projection is a bare bool.

    SQLite twin: ``tests/test_source_agent_provenance.py``. The dedup branch is
    the load-bearing half — ``insert_source_if_absent`` must never restamp an
    existing row's provenance, or an Agent could turn a person's source into an
    Agent-deletable one by re-uploading the same bytes.
    """
    owner = core_stores.identity.create_user("k00123499", "password-10")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Provenance"), owner.id
    )
    # "   " is the same statement as "" — both fold to NULL, byte-for-byte the
    # same expression as the SQLite twin. A non-NULL blank would read as "an
    # Agent added this" while naming no agent.
    for source_id, agent in (
        ("src-user", ""), ("src-blank", "   "), ("src-agent", "ap-agent-1"),
    ):
        core_stores.sources.insert_source(
            source_id=source_id,
            notebook_id=notebook_id,
            title=source_id,
            source_type="markdown",
            status="parsed",
            parse_status="parsed",
            file_name=f"{source_id}.md",
            file_path=f"uploads/{source_id}.md",
            file_size=1,
            file_hash="",
            summary="",
            doc_type="",
            agent_profile_id=agent,
        )
    stored = {
        row["id"]: row["agent_profile_id"]
        for row in _fetch_all(
            core_stores,
            "SELECT id,agent_profile_id FROM sources WHERE notebook_id=%s",
            (notebook_id,),
        )
    }
    assert stored == {
        "src-user": None, "src-blank": None, "src-agent": "ap-agent-1",
    }
    assert {
        item.id: item.agent_created
        for item in core_stores.sources.list_sources(notebook_id)
    } == {"src-user": False, "src-blank": False, "src-agent": True}
    assert core_stores.sources.get_source("src-agent").agent_created is True
    assert core_stores.sources.get_source("src-user").agent_created is False

    # Content dedup reuse keeps the FIRST writer's provenance in both directions.
    core_stores.sources.insert_source(
        source_id="src-person-hashed",
        notebook_id=notebook_id,
        title="shared bytes",
        source_type="markdown",
        status="parsed",
        parse_status="parsed",
        file_name="shared.md",
        file_path="uploads/shared.md",
        file_size=1,
        file_hash="digest-shared",
        summary="",
        doc_type="",
    )
    reused = core_stores.sources.insert_source_if_absent(
        source_id="src-agent-retry",
        notebook_id=notebook_id,
        digest="digest-shared",
        title="shared bytes",
        source_type="markdown",
        status="queued",
        parse_status="queued",
        file_name="shared.md",
        file_path="uploads/shared.md",
        file_size=1,
        summary="",
        doc_type="",
        agent_profile_id="ap-agent-1",
    )
    assert reused == "src-person-hashed"
    assert _fetch_one(
        core_stores,
        "SELECT agent_profile_id FROM sources WHERE id=%s",
        ("src-person-hashed",),
    )["agent_profile_id"] is None

    assert core_stores.sources.insert_source_if_absent(
        source_id="src-agent-first",
        notebook_id=notebook_id,
        digest="digest-agent",
        title="agent bytes",
        source_type="markdown",
        status="queued",
        parse_status="queued",
        file_name="agent.md",
        file_path="uploads/agent.md",
        file_size=1,
        summary="",
        doc_type="",
        agent_profile_id="ap-agent-1",
    ) is None
    assert core_stores.sources.insert_source_if_absent(
        source_id="src-person-retry",
        notebook_id=notebook_id,
        digest="digest-agent",
        title="agent bytes",
        source_type="markdown",
        status="queued",
        parse_status="queued",
        file_name="agent.md",
        file_path="uploads/agent.md",
        file_size=1,
        summary="",
        doc_type="",
    ) == "src-agent-first"
    assert _fetch_one(
        core_stores,
        "SELECT agent_profile_id FROM sources WHERE id=%s",
        ("src-agent-first",),
    )["agent_profile_id"] == "ap-agent-1"


def test_typed_collection_catalog_primitives_match_sqlite_semantics(
    core_stores: CoreStores,
):
    """``element_type_count_rows`` / ``source_change_signal_rows`` — the two
    primitives behind ``app.services.collection_catalog``.

    Same contract as the SQLite adapter: counts are grouped per
    (source, element_type) and restricted to the requested whitelist; signals
    cover every PHYSICAL source EXCEPT the private Memory synthetic rows (the
    Knowhow projection source stays in) and move on each of the three columns
    the element writers touch.  ``memory_source_ids`` returns exactly the rows
    the signal query drops — the two are one predicate in two directions, and
    a backend where they were not complements would silently list one member's
    confirmed Memory to the rest of a shared notebook.
    """
    owner = core_stores.identity.create_user("m00123456", "password-12")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Collections"), owner.id
    )
    # 白名单只有一处字面量(collection_catalog),这里 import 而不是再抄一份
    # ——源码守卫 test_collection_enumeration 会拦第二份副本。
    kinds = ENUMERABLE_ELEMENT_KINDS
    for source_id, source_type in (
        ("src-collect-a", "markdown"),
        ("src-collect-b", "markdown"),
        ("src-collect-hidden", "knowhow"),
        ("src-collect-memory", "memory"),
    ):
        core_stores.sources.insert_source(
            source_id=source_id,
            notebook_id=notebook_id,
            title=source_id,
            source_type=source_type,
            status="parsed",
            parse_status="parsed",
            file_name=f"{source_id}.md",
            file_path=f"uploads/{source_id}.md",
            file_size=1,
            file_hash="hash",
            summary="",
            doc_type="",
        )
    with core_stores.database.write() as connection:
        core_stores.sources.replace_elements(
            connection,
            "src-collect-a",
            (
                SourceElementWrite("el-ca-1", "formula", "p1", "E=mc^2", {}),
                SourceElementWrite("el-ca-2", "formula", "p2", "F=ma", {}),
                SourceElementWrite("el-ca-3", "table", "p3", "<table/>", {}),
                # Not enumerable: prose dominates by volume and listing it is
                # meaningless — it must not reach the counts.
                SourceElementWrite("el-ca-4", "paragraph", "p4", "body", {}),
                SourceElementWrite("el-ca-5", "page_text", "p5", "body", {}),
            ),
            created_at=NOW,
        )
        core_stores.sources.replace_elements(
            connection,
            "src-collect-b",
            (SourceElementWrite("el-cb-1", "formula", "p1", "V=IR", {}),),
            created_at=NOW,
        )
        core_stores.sources.replace_elements(
            connection,
            "src-collect-hidden",
            (SourceElementWrite("el-ch-1", "knowhow_cell", "r1c1", "cell", {}),),
            created_at=NOW,
        )
        core_stores.sources.replace_elements(
            connection,
            "src-collect-memory",
            (SourceElementWrite("el-cm-1", "formula", "p1", "private", {}),),
            created_at=NOW,
        )

    with core_stores.database.connect() as connection:
        counts = core_stores.sources.element_type_count_rows(
            connection,
            ["src-collect-a", "src-collect-b", "src-collect-hidden"],
            kinds,
        )
        assert sorted(counts) == [
            ("src-collect-a", "formula", 2),
            ("src-collect-a", "table", 1),
            ("src-collect-b", "formula", 1),
        ]
        # Unknown ids and an empty kind list are answered without a query.
        assert core_stores.sources.element_type_count_rows(
            connection, ["src-missing"], kinds
        ) == []
        assert core_stores.sources.element_type_count_rows(
            connection, ["src-collect-a"], []
        ) == []

        signals = {
            row[0]: row[1] for row in
            core_stores.sources.source_change_signal_rows(connection, notebook_id)
        }
        memory_ids = set(
            core_stores.sources.memory_source_ids(connection, notebook_id)
        )
    assert set(signals) == {
        "src-collect-a", "src-collect-b", "src-collect-hidden",
    }
    assert all(isinstance(value, str) and value for value in signals.values())
    # Complement, both directions: nothing counted twice, nothing lost.
    assert memory_ids == {"src-collect-memory"}
    assert set(signals) & memory_ids == set()
    assert set(signals) | memory_ids == {
        "src-collect-a", "src-collect-b", "src-collect-hidden",
        "src-collect-memory",
    }

    # updated_at + parse_status move together on every lifecycle transition.
    core_stores.sources.set_status("src-collect-a", "extracted")
    with core_stores.database.connect() as connection:
        after_status = {
            row[0]: row[1] for row in
            core_stores.sources.source_change_signal_rows(connection, notebook_id)
        }
    assert after_status["src-collect-a"] != signals["src-collect-a"]
    assert after_status["src-collect-b"] == signals["src-collect-b"]

    # chunked_at is the third component: a reparse nulls it in the same
    # transaction as the element swap, so the signal flips atomically with the
    # new element generation.
    _write_sql(
        core_stores,
        "UPDATE sources SET chunked_at=%s WHERE id=%s",
        (NOW, "src-collect-b"),
    )
    with core_stores.database.connect() as connection:
        after_chunked = {
            row[0]: row[1] for row in
            core_stores.sources.source_change_signal_rows(connection, notebook_id)
        }
    assert after_chunked["src-collect-b"] != signals["src-collect-b"]
    assert after_chunked["src-collect-a"] == after_status["src-collect-a"]

    # Batch guard: a source list wider than COUNT_IN_CHUNK must be answered
    # from EVERY batch, not just the first. Real sources sit at both ends with
    # non-existent ids padding across the boundary.
    padded = (
        ["src-collect-a"]
        + [f"src-pad-{index}" for index in range(1200)]
        + ["src-collect-b"]
    )
    assert len(padded) > core_stores.sources.COUNT_IN_CHUNK
    with core_stores.database.connect() as connection:
        spanned = core_stores.sources.element_type_count_rows(
            connection, padded, kinds
        )
    assert sorted(spanned) == [
        ("src-collect-a", "formula", 2),
        ("src-collect-a", "table", 1),
        ("src-collect-b", "formula", 1),
    ]

    # The Knowhow-table count rides the generic count primitive; the
    # PostgreSQL adapter allowlists identifiers, so this pins that
    # knowhow_tables/notebook_id is reachable rather than a 500.
    with core_stores.database.connect() as connection:
        assert core_stores.queries.count_rows(
            connection, "knowhow_tables", "notebook_id", notebook_id
        ) == 0

    # A deleted source simply drops out of the signal list.
    with core_stores.database.write() as connection:
        core_stores.sources.delete_source_row(connection, "src-collect-b")
    with core_stores.database.connect() as connection:
        remaining = {
            row[0]: row[1] for row in
            core_stores.sources.source_change_signal_rows(connection, notebook_id)
        }
    assert "src-collect-b" not in remaining

    # ``replace_elements`` moves ``updated_at`` in its own write transaction, so
    # the signal flips with the element generation on a FIRST parse too — no
    # status write, no chunked_at to null (neither happens here). Same
    # guarantee as SQLite; without it this backend would count a just-parsed
    # source as empty until the next status write — a silent parity divergence
    # in a completeness claim.
    with core_stores.database.write() as connection:
        core_stores.sources.replace_elements(
            connection,
            "src-collect-a",
            [
                SourceElementWrite(
                    id="el-collect-a-fresh", element_type="formula",
                    location_label="p9", text="E", metadata={},
                )
            ],
            created_at="2026-07-29T10:00:00.123456+00:00",
        )
    with core_stores.database.connect() as connection:
        after_swap = {
            row[0]: row[1] for row in
            core_stores.sources.source_change_signal_rows(connection, notebook_id)
        }
        assert core_stores.sources.element_type_count_rows(
            connection, ["src-collect-a"], kinds
        ) == [("src-collect-a", "formula", 1)]
    assert after_swap["src-collect-a"] != remaining["src-collect-a"]


def test_typed_collection_enumeration_primitives_match_sqlite_semantics(
    core_stores: CoreStores,
):
    """``element_page_rows`` / ``source_display_rows`` — the two primitives
    behind ``app.services.collection_enumeration``.

    The parity risk that matters here is the CURSOR: ``created_at`` is
    ``timestamptz`` on PostgreSQL and text on SQLite, so the executor hands
    the value back exactly as this adapter returned it.  A page boundary is
    therefore tested with the real ``datetime`` object, not a reformatted
    string — re-rendering it is precisely how a row gets skipped or repeated.
    """
    owner = core_stores.identity.create_user("n00123456", "password-12")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Enumeration"), owner.id
    )
    core_stores.sources.insert_source(
        source_id="src-enum",
        notebook_id=notebook_id,
        title="Enumerable",
        source_type="markdown",
        status="parsed",
        parse_status="parsed",
        file_name="enum.md",
        file_path="uploads/enum.md",
        file_size=1,
        file_hash="hash",
        summary="",
        doc_type="",
    )
    with core_stores.database.write() as connection:
        core_stores.sources.replace_elements(
            connection,
            "src-enum",
            tuple(
                SourceElementWrite(f"el-enum-{index}", "formula", f"p{index}",
                                   f"E={index}", {})
                for index in range(5)
            ) + (
                # Prose must never appear in a typed page.
                SourceElementWrite("el-enum-prose", "paragraph", "p9", "body", {}),
                # An image's short asset id is projected out of metadata IN
                # SQL; a table's unbounded HTML must never be selected.
                SourceElementWrite(
                    "el-enum-image", "image", "p8", "figure",
                    {"asset_id": "asset-enum", "mime": "image/png"},
                ),
                SourceElementWrite(
                    "el-enum-table", "table", "p7", "grid",
                    {"table_html": "<table>" + "x" * 2_000 + "</table>"},
                ),
            ),
            created_at=NOW,
        )
    # Two elements pushed to a later timestamp so the keyset exercises the
    # created_at half of the key, not only the id tie-break.
    _write_sql(
        core_stores,
        "UPDATE source_elements SET created_at=%s WHERE id=ANY(%s)",
        ("2026-07-23T00:00:00+00:00", ["el-enum-3", "el-enum-4"]),
    )

    collected: list[str] = []
    after = None
    with core_stores.database.connect() as connection:
        for _page in range(5):
            rows = core_stores.sources.element_page_rows(
                connection, "src-enum", "formula", after, 2
            )
            if not rows:
                break
            collected.extend(row["id"] for row in rows)
            after = (rows[-1]["created_at"], rows[-1]["id"])
        # A type with no elements is an empty page, not an error.
        assert core_stores.sources.element_page_rows(
            connection, "src-enum", "code_block", None, 5
        ) == []
        image_rows = core_stores.sources.element_page_rows(
            connection, "src-enum", "image", None, 5
        )
        table_rows = core_stores.sources.element_page_rows(
            connection, "src-enum", "table", None, 5
        )
        labels = core_stores.sources.source_display_rows(
            connection, ["src-enum", "src-missing", ""]
        )
    assert collected == [
        "el-enum-0", "el-enum-1", "el-enum-2", "el-enum-3", "el-enum-4",
    ]
    # asset_id is a projection, not the metadata column: the image's short id
    # comes back, the table's HTML never leaves the database.
    assert image_rows[0]["asset_id"] == "asset-enum"
    assert table_rows[0]["asset_id"] is None
    assert "metadata" not in table_rows[0].keys()
    assert [row["id"] for row in labels] == ["src-enum"]
    assert labels[0]["notebook_id"] == notebook_id
    assert labels[0]["title"] == "Enumerable"
    assert labels[0]["file_name"] == "enum.md"
    # No paper metadata row: the outer join keeps the source, with empty
    # paper fields rather than dropping the label entirely.
    assert not labels[0]["is_paper"]
    assert labels[0]["paper_title"] is None


def test_typed_collection_source_primitives_match_sqlite_semantics(
    core_stores: CoreStores,
):
    """The signal rows' ``user_visible`` projection / ``source_listing_rows`` —
    the two primitives behind the SOURCES collection (design doc §6.2).

    Parity risks that matter here:

    * ``user_visible`` must be the user-visible source predicate itself
      (``list_sources``' own), evaluated in THIS backend's SQL, because the
      listing is defined as "the signal rows that flag says are visible".  A
      backend whose flag only excluded Memory would list a phantom Knowhow
      projection document; one that excluded too much would hide a real document
      from the roster.  It is a projected column rather than a second query on
      purpose: nothing indexes ``source_type``, so asking "which ids are hidden"
      separately means re-scanning every source row of the notebook, on the
      request path, right after this query walked the same rows;
    * ``source_listing_rows`` must return the source-card projection on the
      CALLER's connection (summary + doc_type + the paper-meta outer join), and
      ``source_metadata`` must be the same query — they are one SQL by
      construction on both backends;
    * the signal rows' ``created_at`` sort key must order the roster the way
      THIS backend's ``list_sources`` orders it.  PostgreSQL hands back
      ``datetime`` where SQLite hands back text, so "sort the key
      lexicographically" is a per-adapter claim, not a shared one — and if it
      breaks here the roster silently comes out in an order no user has seen.
    """
    owner = core_stores.identity.create_user("q00123456", "password-12")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Roster"), owner.id
    )
    for source_id, source_type, doc_type, summary in (
        ("src-doc-a", "pdf", "academic_paper", "first summary"),
        ("src-doc-b", "markdown", "", ""),
        ("src-doc-hidden", "knowhow", "", ""),
        ("src-doc-memory", "memory", "", "private"),
    ):
        core_stores.sources.insert_source(
            source_id=source_id,
            notebook_id=notebook_id,
            title=source_id,
            source_type=source_type,
            status="parsed",
            parse_status="parsed",
            file_name=f"{source_id}.md",
            file_path=f"uploads/{source_id}.md",
            file_size=1,
            file_hash="hash",
            summary=summary,
            doc_type=doc_type,
        )

    visible_order = [
        source.id for source in core_stores.sources.list_sources(notebook_id)
    ]
    visible = set(visible_order)
    with core_stores.database.connect() as connection:
        memory = set(
            core_stores.sources.memory_source_ids(connection, notebook_id)
        )
        signal_rows = list(
            core_stores.sources.source_change_signal_rows(connection, notebook_id)
        )
        rows = core_stores.sources.source_listing_rows(
            connection, ["src-doc-a", "src-doc-b", "src-missing", ""]
        )
    assert visible == {"src-doc-a", "src-doc-b"}
    signalled = {row[0] for row in signal_rows}
    projected_visible = {row[0] for row in signal_rows if row[3]}
    projected_hidden = {row[0] for row in signal_rows if not row[3]}
    # The flag IS the source tab's predicate, evaluated in PostgreSQL.
    assert projected_visible == visible
    assert projected_hidden == {"src-doc-hidden"}
    # Memory never reaches the signal rows at all; ``memory_source_ids`` remains
    # the one place that knows about them (the KG side needs the ids, because
    # ``knowledge_objects`` carries no source type).  Pinned so a "helpful" merge
    # of the two predicates shows up as a failure.
    assert memory == {"src-doc-memory"}
    assert signalled & memory == set()
    assert projected_visible & projected_hidden == set()
    assert projected_visible | projected_hidden | memory == {
        "src-doc-a", "src-doc-b", "src-doc-hidden", "src-doc-memory",
    }
    # The roster order the sources collection walks == the source tab's order.
    ordered = [
        row[0] for row in sorted(
            (row for row in signal_rows if row[3]),
            key=lambda row: (row[2], row[0]),
        )
    ]
    assert ordered == visible_order

    listed = {row["id"]: row for row in rows}
    assert set(listed) == {"src-doc-a", "src-doc-b"}
    assert listed["src-doc-a"]["notebook_id"] == notebook_id
    assert listed["src-doc-a"]["summary"] == "first summary"
    assert listed["src-doc-a"]["doc_type"] == "academic_paper"
    assert listed["src-doc-a"]["source_type"] == "pdf"
    # No paper-meta row: the outer join keeps the document with empty fields.
    assert not listed["src-doc-a"]["is_paper"]
    assert listed["src-doc-a"]["paper_title"] is None
    # ``source_metadata`` is the same projection through its own connection.
    metadata = core_stores.sources.source_metadata(["src-doc-a", "src-doc-b"])
    assert set(metadata) == {"src-doc-a", "src-doc-b"}
    assert metadata["src-doc-a"]["summary"] == "first summary"
    assert metadata["src-doc-b"]["doc_type"] == ""


def test_bounded_visible_source_identity_rows_match_sqlite_semantics(
    core_stores: CoreStores,
):
    owner = core_stores.identity.create_user("v00123456", "password-12")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Bounded source identities"), owner.id
    )
    for source_id, source_type in (
        ("src-bounded-a", "pdf"),
        ("src-bounded-b", "markdown"),
        ("src-bounded-memory", "memory"),
        ("src-bounded-knowhow", "knowhow"),
    ):
        core_stores.sources.insert_source(
            source_id=source_id,
            notebook_id=notebook_id,
            title=source_id,
            source_type=source_type,
            status="parsed",
            parse_status="parsed",
            file_name=f"{source_id}.md",
            file_path=f"uploads/{source_id}.md",
            file_size=1,
            file_hash="hash",
            summary="",
            doc_type="",
        )
    core_stores.sources.upsert_paper_meta(
        "src-bounded-a",
        notebook_id,
        {
            "is_paper": True,
            "paper_title": "Bounded Paper Title",
            "authors": [],
        },
    )

    with core_stores.database.connect() as connection:
        first = core_stores.sources.visible_source_identity_rows_bounded(
            connection, notebook_id, 1
        )
        all_visible = core_stores.sources.visible_source_identity_rows_bounded(
            connection, notebook_id, 3
        )
        none = core_stores.sources.visible_source_identity_rows_bounded(
            connection, notebook_id, 0
        )

    assert [row["id"] for row in first] == ["src-bounded-a"]
    assert [row["id"] for row in all_visible] == [
        "src-bounded-a", "src-bounded-b"
    ]
    assert all_visible[0]["notebook_id"] == notebook_id
    assert all_visible[0]["file_name"] == "src-bounded-a.md"
    assert all_visible[0]["paper_title"] == "Bounded Paper Title"
    assert none == []
    index = _fetch_one(
        core_stores,
        "SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() "
        "AND indexname='idx_sources_visible_identity'",
    )
    assert index is not None
    assert "notebook_id, created_at, id" in index["indexdef"]
    assert "source_type" in index["indexdef"]


def test_compact_source_scope_snapshot_matches_sqlite_semantics(
    core_stores: CoreStores,
):
    owner = core_stores.identity.create_user("v00987654", "password-12")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Compact source scope"), owner.id
    )
    for source_id, source_type in (
        ("src-compact-a", "pdf"),
        ("src-compact-b", "markdown"),
        ("src-compact-memory", "memory"),
    ):
        core_stores.sources.insert_source(
            source_id=source_id,
            notebook_id=notebook_id,
            title=source_id,
            source_type=source_type,
            status="parsed",
            parse_status="parsed",
            file_name=f"{source_id}.md",
            file_path=f"uploads/{source_id}.md",
            file_size=1,
            file_hash="hash",
            summary="",
            doc_type="",
        )

    assert core_stores.sources.visible_source_scope_snapshot(
        notebook_id, ["src-compact-b", "src-foreign"]
    ) == (["src-compact-b"], 2)
    assert core_stores.sources.visible_source_scope_snapshot(
        notebook_id, []
    ) == ([], 2)
    core_stores.sources.IN_CHUNK = 1
    assert core_stores.sources.visible_source_scope_snapshot(
        notebook_id, ["src-compact-b", "src-foreign", "src-compact-a"]
    ) == (["src-compact-b", "src-compact-a"], 2)


def test_concurrent_share_issuance_converges_under_read_committed(
    core_stores: CoreStores,
):
    """Two concurrent shares must converge on one token — the PostgreSQL race.

    Under READ COMMITTED a read-then-write implementation lets both callers
    observe NULL and then overwrite unconditionally: the later token wins, and
    the link the first caller was already handed starts returning 404 despite
    the endpoint's idempotency contract. SQLite cannot reproduce this (its
    writer lock serialises the pair), so the guard has to live here.
    """
    import threading

    from app.repositories.postgres.report_store import ReportStore

    owner = core_stores.identity.create_user("r00778899", "password-12")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Concurrent share"), owner.id
    )
    reports = ReportStore(
        core_stores.database,
        new_id=_new_id_factory(),
        now=lambda: NOW,
        current_user_id=lambda: owner.id,
    )
    report_id = reports.create_report(notebook_id, "q", 2)

    start = threading.Barrier(4)
    issued: list[str] = []
    failures: list[BaseException] = []
    lock = threading.Lock()

    def issue():
        try:
            start.wait(timeout=10)
            token = reports.share_report(notebook_id, report_id)
        except BaseException as error:          # noqa: BLE001 — surfaced below
            with lock:
                failures.append(error)
            return
        with lock:
            issued.append(token)

    threads = [threading.Thread(target=issue) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not failures, failures
    assert len(issued) == 4
    assert len(set(issued)) == 1, issued
    # The winner is what is actually persisted, so every caller's link works.
    assert reports.report_share_token(notebook_id, report_id) == issued[0]
    assert reports.public_report_by_token(issued[0]) is None  # 未 done 不可读
    reports.update_report(notebook_id, report_id, status="done")
    assert reports.public_report_by_token(issued[0]) is not None


def test_terminal_understanding_write_keeps_the_generation_stamp_on_postgres(
    core_stores: CoreStores,
):
    """Run the jsonb preservation expression against PostgreSQL itself.

    The `||` / `jsonb_build_object` merge cannot be validated by inspection, and
    a stamp silently erased here is invisible until a finished report shows no
    elapsed time.  The SQLite lane covers the same behavior in
    ``test_report_api``.
    """
    from app.repositories.postgres.report_store import ReportStore

    owner = core_stores.identity.create_user("r00456789", "password-12")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Report timing"), owner.id
    )
    reports = ReportStore(
        core_stores.database,
        new_id=_new_id_factory(),
        now=lambda: NOW,
        current_user_id=lambda: owner.id,
    )
    report_id = reports.create_report(notebook_id, "为什么?", 8)
    reports.update_report(notebook_id, report_id, status="outline_ready")
    assert reports.claim_report_generation(notebook_id, report_id) is True
    started = reports.get_report(notebook_id, report_id)["generation_started_at"]
    assert started

    reports.update_report(
        notebook_id, report_id, status="done",
        understanding={"objective": "q", "credibility": {"anchor_count": 3}},
    )

    done = reports.get_report(notebook_id, report_id)
    assert done["generation_started_at"] == started
    assert done["understanding"]["objective"] == "q"
    assert "_generation_started_at" not in done["understanding"]

    # A report that never reached generation has no stamp to preserve, and the
    # merge must not invent one.
    second = reports.create_report(notebook_id, "另一个问题", 2)
    reports.update_report(
        notebook_id, second, status="done", understanding={"objective": "q2"},
    )
    never_claimed = reports.get_report(notebook_id, second)
    assert never_claimed["generation_started_at"] == ""
    assert never_claimed["understanding"] == {"objective": "q2"}


def test_report_source_rows_executes_all_postgres_aggregates_and_matches_sqlite(
    core_stores: CoreStores,
):
    """Execute every new corpus-profile query against PostgreSQL itself.

    This is intentionally a behavior test in the G3 conformance lane: source
    inspection cannot detect PostgreSQL type/alias errors or NULL ordering.
    The SQLite lane seeds the same result shape in
    ``test_memory_source_visibility``.
    """
    owner = core_stores.identity.create_user("r00123456", "password-12")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Report corpus profile"), owner.id
    )
    other_notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Other report corpus"), owner.id
    )

    def insert_source(
        source_id: str, source_type: str, doc_type: str, file_hash: str
    ) -> None:
        core_stores.sources.insert_source(
            source_id=source_id,
            notebook_id=notebook_id,
            title=source_id,
            source_type=source_type,
            status="parsed",
            parse_status="parsed",
            file_name=f"{source_id}.md",
            file_path=f"uploads/{source_id}.md",
            file_size=1,
            file_hash=file_hash,
            summary="",
            doc_type=doc_type,
        )

    for values in (
        ("profile-a", "markdown", "", "shared-hash"),
        ("profile-b", "pdf", "academic_paper", "shared-hash"),
        ("profile-c", "pdf", "textbook", ""),
        ("profile-corrupt-meta", "notes", "", ""),
        ("profile-hidden", "memory", "memory", ""),
    ):
        insert_source(*values)
    for source_id, title in (
        ("profile-a", "Profile Paper"),
        ("profile-b", "  Profile Paper  "),
    ):
        core_stores.sources.upsert_paper_meta(
            source_id,
            notebook_id,
            {
                "is_paper": True,
                "paper_title": title,
                "pub_year": 2026,
                "authors": [],
            },
        )
    # Simulate a malformed historical row: current writers clear publication
    # years when is_paper becomes false, but report distributions must remain
    # self-consistent if an old row retained one.
    core_stores.sources.upsert_paper_meta(
        "profile-c",
        notebook_id,
        {
            "is_paper": True,
            "paper_title": "Stale non-paper",
            "pub_year": 2024,
            "authors": [],
        },
    )
    with core_stores.database.write() as connection:
        connection.execute(
            "UPDATE source_paper_meta SET is_paper=0 WHERE source_id=%s AND notebook_id=%s",
            ("profile-c", notebook_id),
        )
    core_stores.sources.upsert_paper_meta(
        "profile-corrupt-meta",
        other_notebook_id,
        {
            "is_paper": True,
            # Deliberately collides with the valid title above. Removing the
            # notebook join from title-duplicate aggregation must change the
            # count, not merely the hydrated representative fields.
            "paper_title": "Profile Paper",
            "pub_year": 1999,
            "authors": [],
        },
    )

    snapshot = core_stores.sources.report_source_rows(
        notebook_id, representative_limit=16, distribution_limit=16
    )

    assert snapshot["total_sources"] == 4
    assert snapshot["metadata_sources"] == 2
    assert snapshot["known_year_sources"] == 2
    assert snapshot["identity_uncertain_sources"] == 2
    assert snapshot["hash_duplicate_excess"] == 1
    assert snapshot["title_duplicate_excess"] == 1
    assert snapshot["type_distribution"] == [
        {"type": "academic_paper", "count": 1},
        {"type": "markdown", "count": 1},
        {"type": "notes", "count": 1},
        {"type": "textbook", "count": 1},
    ]
    assert snapshot["year_distribution"] == [{"year": 2026, "count": 2}]
    representatives = {row["id"]: row for row in snapshot["representatives"]}
    assert set(representatives) == {
        "profile-a", "profile-b", "profile-c", "profile-corrupt-meta"
    }
    assert representatives["profile-corrupt-meta"]["paper_title"] is None
    assert representatives["profile-c"]["pub_year"] is None
    identity = core_stores.sources.report_source_identity_rows(
        ["profile-corrupt-meta"]
    )[0]
    assert identity["paper_title"] is None
    assert not identity["is_paper"]


def test_report_representative_prefix_matches_sqlite_with_null_years(
    core_stores: CoreStores,
):
    owner = core_stores.identity.create_user("s00123456", "password-12")

    def seed(notebook_id: str, source_id: str, source_type: str) -> None:
        core_stores.sources.insert_source(
            source_id=source_id,
            notebook_id=notebook_id,
            title=source_id,
            source_type=source_type,
            status="parsed",
            parse_status="parsed",
            file_name=f"{source_id}.md",
            file_path=f"uploads/{source_id}.md",
            file_size=1,
            file_hash=source_id,
            summary="",
            doc_type="",
        )

    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Representative null ordering"), owner.id
    )
    seed(notebook_id, "rep-known", "markdown")
    seed(notebook_id, "rep-unknown", "pdf")
    core_stores.sources.upsert_paper_meta(
        "rep-known",
        notebook_id,
        {
            "is_paper": True,
            "paper_title": "Known year",
            "pub_year": 2026,
            "authors": [],
        },
    )
    assert core_stores.sources.report_source_rows(
        notebook_id, representative_limit=1
    )["representatives"][0]["id"] == "rep-known"

    type_notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Representative type ordering"), owner.id
    )
    seed(type_notebook_id, "rep-a-unknown", "aaa")
    seed(type_notebook_id, "rep-z-known", "zzz")
    core_stores.sources.upsert_paper_meta(
        "rep-z-known",
        type_notebook_id,
        {
            "is_paper": True,
            "paper_title": "Known but later type",
            "pub_year": 2026,
            "authors": [],
        },
    )
    assert core_stores.sources.report_source_rows(
        type_notebook_id, representative_limit=1
    )["representatives"][0]["id"] == "rep-a-unknown"

    null_order_notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Representative explicit null order"), owner.id
    )
    for source_id in (
        "a-type-first",
        "b-known-sentinel",
        "c-null-sentinel",
        "d-known-candidate",
        "e-null-candidate",
    ):
        seed(null_order_notebook_id, source_id, "pdf")
    # Make every row share the normalized type key. The two candidate rows
    # below then both have type_rank > 1 and year_rank > 1.
    with core_stores.database.write() as connection:
        connection.execute(
            "UPDATE sources SET doc_type='shared-type' WHERE notebook_id=%s",
            (null_order_notebook_id,),
        )
    for source_id, year in (
        ("a-type-first", 2020),
        ("b-known-sentinel", 2026),
        ("d-known-candidate", 2026),
    ):
        core_stores.sources.upsert_paper_meta(
            source_id,
            null_order_notebook_id,
            {
                "is_paper": True,
                "paper_title": source_id,
                "pub_year": year,
                "authors": [],
            },
        )
    ordered_ids = [
        row["id"]
        for row in core_stores.sources.report_source_rows(
            null_order_notebook_id, representative_limit=5
        )["representatives"]
    ]
    assert ordered_ids.index("d-known-candidate") < ordered_ids.index(
        "e-null-candidate"
    )


def test_postgres_element_page_stays_on_the_typed_index(core_stores: CoreStores):
    """禁全表扫描:PostgreSQL 侧的翻页也必须落在
    ``idx_source_elements_source_type`` 上。计划器细节(Index Scan /
    Index Only Scan)不是契约,「不是 Seq Scan」和「用的是这条索引」才是。"""
    owner = core_stores.identity.create_user("p00123456", "password-12")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Plan"), owner.id
    )
    core_stores.sources.insert_source(
        source_id="src-plan",
        notebook_id=notebook_id,
        title="Plan",
        source_type="markdown",
        status="parsed",
        parse_status="parsed",
        file_name="plan.md",
        file_path="uploads/plan.md",
        file_size=1,
        file_hash="hash",
        summary="",
        doc_type="",
    )
    with core_stores.database.write() as connection:
        core_stores.sources.replace_elements(
            connection,
            "src-plan",
            tuple(
                SourceElementWrite(f"el-plan-{index}", "formula", "p1", "E", {})
                for index in range(40)
            ),
            created_at=NOW,
        )
    with core_stores.database.connect() as connection:
        # Same technique as the trigram guard in test_search_conformance: a
        # 40-row table is small enough that the planner would rightly scan it,
        # so seqscan is penalised and the question becomes "does an index path
        # for this exact shape EXIST at all?".  If none did, the plan would
        # still come back as a Seq Scan and both assertions would fire.
        connection.execute("SET LOCAL enable_seqscan=off")
        plan_rows = connection.execute(
            "EXPLAIN SELECT id,source_id,element_type,location_label,text,"
            "created_at,metadata->>'asset_id' AS asset_id "
            "FROM source_elements WHERE source_id=%s AND "
            "element_type=%s AND (created_at,id) > (%s,%s) "
            "ORDER BY created_at,id LIMIT %s",
            ("src-plan", "formula", NOW, "el-plan-0", 25),
        ).fetchall()
    plan = "\n".join(str(next(iter(row.values()))) for row in plan_rows)
    assert "idx_source_elements_source_type" in plan, plan
    assert "Seq Scan" not in plan, plan
    # An index range, not a sort of the whole (source, type) group.
    assert "Sort" not in plan, plan


def test_latest_extraction_run_uses_ordinal_tie_break(
    core_stores: CoreStores,
):
    owner = core_stores.identity.create_user("l00123456", "password-11")
    notebook_id = core_stores.notebooks.create_row(NotebookCreate(name="Runs"), owner.id)
    core_stores.sources.insert_source(
        source_id="src-runs",
        notebook_id=notebook_id,
        title="Runs",
        source_type="markdown",
        status="extracted",
        parse_status="extracted",
        file_name="runs.md",
        file_path="uploads/runs.md",
        file_size=1,
        file_hash="hash",
        summary="",
        doc_type="",
    )
    for run_id, status, error in (
        ("run-first", "completed", "windows_failed=1/2"),
        ("run-second", "failed", ""),
    ):
        _write_sql(
            core_stores,
            "INSERT INTO extraction_runs"
            "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (run_id, notebook_id, "src-runs", "kg", status, error, NOW, NOW),
        )
    assert core_stores.sources.list_sources(notebook_id)[0].extraction_warning is None


def test_paper_metadata_json_and_author_search_roundtrip(core_stores: CoreStores):
    owner = core_stores.identity.create_user("p00123456", "password-15")
    notebook_id = core_stores.notebooks.create_row(NotebookCreate(name="Paper"), owner.id)
    core_stores.sources.insert_source(
        source_id="src-paper",
        notebook_id=notebook_id,
        title="Imported document",
        source_type="pdf",
        status="parsed",
        parse_status="parsed",
        file_name="paper.pdf",
        file_path="uploads/paper.pdf",
        file_size=1,
        file_hash="hash",
        summary="",
        doc_type="academic_paper",
    )
    core_stores.sources.upsert_paper_meta(
        "src-paper",
        notebook_id,
        {
            "is_paper": True,
            "paper_title": "FinFET Study",
            "venue": "VLSI",
            "pub_year": 2026,
            "doi": "10.1/example",
            "keywords": ["FinFET", "scaling"],
            "model": "test-model",
            "raw_json": '{"is_paper":true}',
            "authors": [
                {"position": 0, "name": "Alice Wu", "affiliation": "Lab"}
            ],
        },
    )
    detail = core_stores.sources.get_source("src-paper")
    assert detail.paper_meta_status == "has_meta"
    assert detail.authors == ["Alice Wu"]
    assert detail.paper_meta is not None
    assert detail.paper_meta.keywords == ["FinFET", "scaling"]
    result = core_stores.sources.list_sources_page(notebook_id, q="alice wu")
    assert result.total_count == 1
    assert result.items[0].id == "src-paper"


def test_chunk_jsonb_and_ordinal_language_probe(core_stores: CoreStores):
    owner = core_stores.identity.create_user("e00123456", "password-4")
    notebook_id = core_stores.notebooks.create_row(NotebookCreate(name="Chunks"), owner.id)
    core_stores.sources.insert_source(
        source_id="src-chunks",
        notebook_id=notebook_id,
        title="Chunk source",
        source_type="markdown",
        status="parsed",
        parse_status="parsed",
        file_name="chunks.md",
        file_path="uploads/chunks.md",
        file_size=1,
        file_hash="h",
        summary="",
        doc_type="",
    )
    rows = tuple(
        ChunkWrite(f"chunk-{index:02d}", f"text-{index:02d}", "section", ("el",))
        for index in range(35)
    )
    core_stores.chunks.replace_source_chunks(
        "src-chunks", notebook_id, rows, created_at=NOW
    )
    with core_stores.database.connect() as connection:
        probe = core_stores.chunks.language_probe_rows(connection, notebook_id)
        stored = core_stores.chunks.rows_by_ids(
            connection, ["chunk-00", "chunk-34"]
        )
    assert {row["text"] for row in probe} == {
        *(f"text-{index:02d}" for index in range(30)),
        *(f"text-{index:02d}" for index in range(5, 35)),
    }
    by_id = {row["id"]: row for row in stored}
    assert by_id["chunk-00"]["element_ids"] in (["el"], '["el"]')


def test_kg_build_single_flight_conditional_updates_and_latest_ordinal(
    core_stores: CoreStores,
):
    owner = core_stores.identity.create_user("f00123456", "password-5")
    notebook_id = core_stores.notebooks.create_row(NotebookCreate(name="Jobs"), owner.id)
    first = core_stores.jobs.create_job(notebook_id, owner.id, "incremental", 2)
    with pytest.raises(core_stores.already_running):
        core_stores.jobs.create_job(notebook_id, owner.id, "rebuild", 2)
    assert core_stores.jobs.record_source_result(first["id"], succeeded=True)
    assert core_stores.jobs.finish(first["id"], "succeeded")
    assert not core_stores.jobs.record_source_result(first["id"], succeeded=True)

    second = core_stores.jobs.create_job(notebook_id, owner.id, "rebuild", 1)
    assert second["id"] != first["id"]
    assert core_stores.jobs.latest(notebook_id)["id"] == second["id"]


def test_kg_build_single_flight_is_atomic_across_two_connections(
    core_stores: CoreStores,
):
    owner = core_stores.identity.create_user("m00123456", "password-12")
    notebook_id = core_stores.notebooks.create_row(NotebookCreate(name="Race"), owner.id)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    failures: list[BaseException] = []
    lock = threading.Lock()

    def create(mode: str) -> None:
        try:
            barrier.wait(timeout=2)
            core_stores.jobs.create_job(notebook_id, owner.id, mode, 1)
            result = "created"
        except core_stores.already_running:
            result = "conflict"
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)
            return
        with lock:
            outcomes.append(result)

    workers = [
        threading.Thread(target=create, args=("incremental",)),
        threading.Thread(target=create, args=("rebuild",)),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)
    assert not failures
    assert not any(worker.is_alive() for worker in workers)
    assert sorted(outcomes) == ["conflict", "created"]


def test_notebook_updates_store_json_without_backend_leak(core_stores: CoreStores):
    owner = core_stores.identity.create_user("g00123456", "password-6")
    notebook_id = core_stores.notebooks.create_row(NotebookCreate(name="Before"), owner.id)
    core_stores.notebooks.update_row(
        notebook_id,
        NotebookUpdate(
            name="After",
            expected_questions=["Q1", "Q2"],
            source_types=["pdf"],
            taxonomy=["analog"],
        ),
    )
    row = core_stores.notebooks.get_row(notebook_id)
    assert row["name"] == "After"
    assert row["expected_questions"] in (["Q1", "Q2"], '["Q1", "Q2"]')


def test_notebook_delete_returns_paths_and_removes_orphan_embeddings(
    core_stores: CoreStores,
):
    owner = core_stores.identity.create_user("q00123456", "password-16")
    notebook_id = core_stores.notebooks.create_row(NotebookCreate(name="Delete"), owner.id)
    core_stores.sources.insert_source(
        source_id="src-delete-notebook",
        notebook_id=notebook_id,
        title="Delete",
        source_type="pdf",
        status="parsed",
        parse_status="parsed",
        file_name="delete.pdf",
        file_path="uploads/delete.pdf",
        file_size=1,
        file_hash="hash",
        summary="",
        doc_type="",
    )
    vector: object = b"\x00\x01"
    _write_sql(
        core_stores,
        "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) "
        "VALUES (%s,%s,%s,%s)",
        ("ko-delete", notebook_id, vector, NOW),
    )
    assert core_stores.notebooks.delete_row_and_orphan_embeddings(notebook_id) == [
        "uploads/delete.pdf"
    ]
    assert _fetch_one(
        core_stores,
        "SELECT 1 FROM notebooks WHERE id=%s",
        (notebook_id,),
    ) is None
    assert _fetch_one(
        core_stores,
        "SELECT 1 FROM knowledge_embeddings WHERE object_id=%s",
        ("ko-delete",),
    ) is None


# ---------------------------------------------------------- command catalog
# The PostgreSQL CatalogStore had no behavioural coverage at all: only the
# schema/migration phase test touched it. Four things in it are PG-ONLY code
# paths that a SQLite-side test can never reach, and each fails differently:
#
#   * the `errors.UniqueViolation` -> `CatalogJobAlreadyRunning` mapping keyed
#     on the literal constraint name — if that name is ever wrong, a duplicate
#     start surfaces as a 500 instead of a 409, and only on PostgreSQL;
#   * `id = ANY(%s)` instead of bounded placeholders;
#   * `COLLATE "C"` on the ordering keys, so a non-C-collated database still
#     pages in the order SQLite does;
#   * the NULL -> "" `finished_at` sentinel that keeps the domain shape
#     identical across backends.
def _catalog_stores(request):
    from app.repositories.postgres.catalog_store import CatalogStore

    stores = request.getfixturevalue("core_stores")
    new_id = _new_id_factory()
    return stores, CatalogStore(stores.database, new_id=new_id, now=lambda: NOW)


def test_postgres_catalog_single_flight_maps_the_unique_violation(request):
    from app.repositories.ports import CatalogJobAlreadyRunning

    stores, catalog = _catalog_stores(request)
    owner = stores.identity.create_user("c00123456", "password-catalog")
    notebook_id = stores.notebooks.create_row(NotebookCreate(name="Manual"), owner.id)
    stores.sources.insert_source(
        source_id="src-catalog", notebook_id=notebook_id, title="Manual",
        source_type="markdown", status="extracted", parse_status="extracted",
        file_name="m.md", file_path="/tmp/m.md", file_size=1, file_hash="h",
        summary="", doc_type="",
    )

    queued = catalog.create_job(notebook_id, "src-catalog", owner.id)
    assert queued["status"] == "queued"
    assert queued["finished_at"] == ""  # NULL restored to the domain sentinel
    with pytest.raises(CatalogJobAlreadyRunning):
        catalog.create_job(notebook_id, "src-catalog", owner.id)

    assert catalog.start_job(queued["id"], 3) is True
    with pytest.raises(CatalogJobAlreadyRunning):
        catalog.create_job(notebook_id, "src-catalog", owner.id)  # running too

    assert catalog.record_section(
        queued["id"], entries=2, rejected=1, uncovered=0
    ) is True
    assert catalog.finish_job(queued["id"], "succeeded") is True
    assert catalog.finish_job(queued["id"], "succeeded") is False  # idempotent
    settled = catalog.get_job(queued["id"])
    assert settled["sections_done"] == 1 and settled["entries"] == 2
    assert settled["finished_at"]
    # Guard released with the terminal state.
    assert catalog.active_job("src-catalog") is None
    assert catalog.create_job(notebook_id, "src-catalog", owner.id)["status"] == "queued"


def test_postgres_catalog_candidate_paging_and_id_lookup(request):
    stores, catalog = _catalog_stores(request)
    owner = stores.identity.create_user("d00123456", "password-catalog2")
    notebook_id = stores.notebooks.create_row(NotebookCreate(name="Manual2"), owner.id)
    stores.sources.insert_source(
        source_id="src-catalog2", notebook_id=notebook_id, title="Manual2",
        source_type="markdown", status="extracted", parse_status="extracted",
        file_name="m2.md", file_path="/tmp/m2.md", file_size=1, file_hash="h2",
        summary="", doc_type="",
    )
    job = catalog.create_job(notebook_id, "src-catalog2", owner.id)
    catalog.add_candidates([
        {
            "job_id": job["id"], "notebook_id": notebook_id,
            "source_id": "src-catalog2", "position": index + 1,
            "command_name": f"set_thing_{index}",
            "payload": {"syntax": f"set_thing_{index} -x"},
            "state": "candidate" if index % 2 == 0 else "rejected",
            "reject_info": {"fields": []},
        }
        for index in range(6)
    ])

    counts = catalog.candidate_counts(job["id"])
    assert counts["candidate"] == 3 and counts["rejected"] == 3

    first = catalog.list_candidates(job["id"], state="candidate", cursor=0, limit=2)
    assert [row["position"] for row in first] == [1, 3]
    assert first[0]["payload"]["syntax"] == "set_thing_0 -x"  # jsonb round-trip
    rest = catalog.list_candidates(
        job["id"], state="candidate", cursor=first[-1]["position"], limit=2
    )
    assert [row["position"] for row in rest] == [5]

    wanted = [row["id"] for row in first]
    fetched = catalog.candidates_by_ids(job["id"], wanted, limit=10)  # id = ANY(%s)
    assert [row["id"] for row in fetched] == wanted
    assert catalog.mark_candidates_applied(job["id"], wanted) == 2
    assert catalog.candidate_counts(job["id"])["applied"] == 2
    assert catalog.mark_candidates_applied(job["id"], wanted) == 0  # state-scoped


def test_hidden_source_ids_scope_memory_to_its_owner(
    core_stores: CoreStores,
):
    """``SourceStore.hidden_source_ids`` — the retrieval-scope freeze's hidden
    half, in PostgreSQL.

    Same contract as the SQLite adapter: the hidden half is NOT uniform —
    Knowhow projection sources are notebook-wide and reach every member, while
    a Memory projection source belongs to its ``memory_items.created_by`` and
    only that user's ceiling may admit it. A backend that dropped the owner
    predicate would freeze one member's private Memory into another member's
    non-narrowed run, where whole-graph/PPR and ordinary candidate retrieval
    could then read it. The SQLite test proves the predicate; this one proves
    PostgreSQL's own SQL actually executes and means the same thing.
    """
    alice = core_stores.identity.create_user("s00123456", "password-12")
    bob = core_stores.identity.create_user("s00123457", "password-12")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Scope owners"), alice.id
    )
    core_stores.sharing.add_member(notebook_id, bob.id)

    def insert(source_id: str, source_type: str, memory_id: str = "") -> None:
        core_stores.sources.insert_source(
            source_id=source_id,
            notebook_id=notebook_id,
            title=source_id,
            source_type=source_type,
            status="active",
            parse_status="parsed",
            file_name="",
            file_path="",
            file_size=0,
            file_hash="",
            summary="",
            doc_type="",
            memory_id=memory_id,
        )

    insert("src-scope-visible", "markdown")
    insert("src-scope-knowhow", "knowhow")
    for user, memory_id, source_id in (
        (alice, "mem-scope-alice", "src-scope-memory-alice"),
        (bob, "mem-scope-bob", "src-scope-memory-bob"),
    ):
        _write_sql(
            core_stores,
            "INSERT INTO memory_items"
            "(id,notebook_id,created_by,agent_profile_id,source_answer_id,"
            "origin,status,promotion_state,title,content_md,tags_json,"
            "embedding_status,embedding_error,created_at,updated_at) "
            "VALUES (%s,%s,%s,NULL,NULL,'ask_answer','confirmed','none',"
            "%s,%s,'[]'::jsonb,'pending','',%s,%s)",
            (memory_id, notebook_id, user.id, "private", "private body", NOW, NOW),
        )
        insert(source_id, "memory", memory_id=memory_id)

    assert core_stores.sources.all_visible_source_ids(notebook_id) == [
        "src-scope-visible"
    ]
    hidden = core_stores.sources.hidden_source_ids(notebook_id, bob.id)
    assert "src-scope-memory-alice" not in hidden
    assert set(hidden) == {"src-scope-knowhow", "src-scope-memory-bob"}

    hidden_alice = core_stores.sources.hidden_source_ids(notebook_id, alice.id)
    assert "src-scope-memory-bob" not in hidden_alice
    assert set(hidden_alice) == {"src-scope-knowhow", "src-scope-memory-alice"}

    # An unknown identity gets the shared Knowhow projection and no Memory.
    assert core_stores.sources.hidden_source_ids(
        notebook_id, "user-nobody"
    ) == ["src-scope-knowhow"]

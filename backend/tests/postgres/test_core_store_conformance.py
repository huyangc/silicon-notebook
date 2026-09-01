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
from app.repositories.ports import (
    ChunkWrite,
    DocumentCapacityExceeded,
    SourceElementWrite,
)
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
from tests.kg_extracted_parity_cases import (
    KG_EXTRACTED_CASES,
    kg_case_run_id,
    kg_case_source_id,
)


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

    assert PostgresMigrator(postgres_database).migrate() == 48
    yield CoreStores(
        database=postgres_database,
        identity=PostgresIdentityStore(postgres_database, postgres_settings),
        model_status=PostgresModelStatusStore(postgres_database),
        notebooks=PostgresNotebookStore(
            postgres_database,
            new_id=new_id,
            now=now,
            activity_retention_days=180,
        ),
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


def test_set_user_search_profile_round_trips_and_survives_a_missing_profile_row(
    core_stores: CoreStores,
):
    """Agentic Memory P3(T6):PostgreSQL 侧 ``set_user_search_profile`` 与
    SQLite 语义逐字对齐——``origin="user"`` 写入后可读回,``origin="job"``
    不覆盖已被用户写过的字段,且缺 profile 行时走补 INSERT 而不是 404。"""
    store = core_stores.identity
    user = store.create_user("e00123456", "correct horse battery staple")

    updated = store.set_user_search_profile(
        user.id, {"answer_language": "zh"}, origin="user"
    )
    assert updated.search_profile["fields"]["answer_language"]["value"] == "zh"
    assert updated.search_profile["fields"]["answer_language"]["origin"] == "user"

    # job 不覆盖已被用户写过的字段(跨进程存储层同一条契约)。
    unchanged = store.set_user_search_profile(
        user.id, {"answer_language": "en"}, origin="job"
    )
    assert unchanged.search_profile["fields"]["answer_language"]["value"] == "zh"

    # job 仍可自由填一个用户从未碰过的字段。
    filled = store.set_user_search_profile(
        user.id, {"answer_detail": "concise"}, origin="job"
    )
    assert filled.search_profile["fields"]["answer_detail"]["value"] == "concise"
    assert filled.search_profile["fields"]["answer_detail"]["origin"] == "job"
    assert filled.search_profile["fields"]["answer_language"]["value"] == "zh"

    # 清空(value=None)删除该字段条目。
    cleared = store.set_user_search_profile(
        user.id, {"answer_language": None}, origin="user"
    )
    assert "answer_language" not in cleared.search_profile["fields"]

    # 缺 profile 行时的补 INSERT 路径(镜像 set_user_ui_mode 的同款测试)。
    _write_sql(
        core_stores,
        "DELETE FROM user_profiles WHERE user_id=%s",
        (user.id,),
    )
    recreated = store.set_user_search_profile(
        user.id, {"answer_shape": "prose"}, origin="user"
    )
    assert recreated.search_profile["fields"]["answer_shape"]["value"] == "prose"
    row_count = _fetch_one(
        core_stores,
        "SELECT COUNT(*) AS n FROM user_profiles WHERE user_id=%s",
        (user.id,),
    )
    assert row_count["n"] == 1


def test_set_user_search_profile_serializes_concurrent_writers_via_row_lock(
    core_stores: CoreStores,
):
    """PostgreSQL 每个连接互相独立、没有 SQLite 那道进程级 write_lock,必须靠
    显式 ``SELECT ... FOR UPDATE`` 才能保证「用户编辑」与「后台归纳」并发写
    不同字段时两个字段都留下——这条测试专门钉这个跨连接场景(SQLite 侧的
    对应测试在 test_system_routes.py,靠进程写锁天然获得同样的保证,两条测试
    因此不是同一份重复,而是各自验证各自后端的并发保证机制)。"""
    store = core_stores.identity
    user = store.create_user("f00123456", "correct horse battery staple")

    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def _write_user():
        try:
            barrier.wait(timeout=5)
            store.set_user_search_profile(
                user.id, {"answer_shape": "table_first"}, origin="user"
            )
        except Exception as exc:  # pragma: no cover - surfaced via errors list
            errors.append(exc)

    def _write_job():
        try:
            barrier.wait(timeout=5)
            store.set_user_search_profile(
                user.id, {"answer_language": "zh"}, origin="job"
            )
        except Exception as exc:  # pragma: no cover - surfaced via errors list
            errors.append(exc)

    threads = [threading.Thread(target=_write_user), threading.Thread(target=_write_job)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, errors

    final = _fetch_one(
        core_stores,
        "SELECT search_profile_json FROM user_profiles WHERE user_id=%s",
        (user.id,),
    )
    from app.services.search_profile import parse_search_profile

    parsed = parse_search_profile(final["search_profile_json"])
    assert parsed["fields"]["answer_shape"]["value"] == "table_first"
    assert parsed["fields"]["answer_language"]["value"] == "zh"


def test_set_user_search_profile_skips_the_update_when_unchanged(
    core_stores: CoreStores, monkeypatch
):
    """T7-T9 修复轮(P2-6,PostgreSQL 镜像):合并结果与既存
    ``search_profile_json`` 逐字相同时,``set_user_search_profile`` 必须跳过
    ``UPDATE`` 本身——原理与 SQLite 侧的同名测试
    (``backend/tests/test_search_profile_job.py::
    test_repeated_job_write_of_the_same_value_skips_the_update``)一致,这里
    钉住 PostgreSQL 适配器的同一条优化:直接比对最终值不能证明「真的跳过
    了」,改用一把会推进的假时钟证明 ``user_profiles.updated_at`` 这一列
    没有被第二次写入。

    ``create_user`` 先用真实时钟建号(它自己的 ``utc_now()`` 调用不该被这条
    测试的假时钟污染),之后才把
    ``app.repositories.postgres.identity_store.utc_now`` 与
    ``app.services.search_profile.datetime`` 一起钉死——前者控制本方法用来
    推进**行级** ``updated_at`` 列的时钟输入,后者控制 ``merge_field`` 写入
    文档内**逐字段** ``updated_at`` 的时钟输入(两者是独立的时钟 seam,不钉
    住第二个,两次调用产出的 JSON 会在秒边界上偶发不同,让「同一份文档」这个
    前提本身不成立)。
    """
    from datetime import datetime as _real_datetime
    from datetime import timezone as _timezone

    import app.repositories.postgres.identity_store as pg_identity_store_module
    from app.domain import search_profile as search_profile_module  # B3: merge_field now lives (and reads datetime) here
    from app.services.search_profile import parse_search_profile

    store = core_stores.identity
    user = store.create_user("h00123456", "correct horse battery staple")

    class _FixedDatetime(_real_datetime):
        @classmethod
        def now(cls, tz=None):
            return _real_datetime(2026, 8, 20, 0, 0, 0, tzinfo=tz)

    monkeypatch.setattr(search_profile_module, "datetime", _FixedDatetime)

    first_tick = _real_datetime(2026, 8, 20, 0, 0, 0, tzinfo=_timezone.utc)
    second_tick = _real_datetime(2026, 8, 20, 0, 0, 5, tzinfo=_timezone.utc)
    row_clock = iter([first_tick, second_tick])
    monkeypatch.setattr(pg_identity_store_module, "utc_now", lambda: next(row_clock))

    store.set_user_search_profile(user.id, {"answer_language": "zh"}, origin="job")
    store.set_user_search_profile(user.id, {"answer_language": "zh"}, origin="job")

    row = _fetch_one(
        core_stores,
        "SELECT updated_at, search_profile_json FROM user_profiles WHERE user_id=%s",
        (user.id,),
    )
    # The row-level updated_at must still be the FIRST clock tick, not the
    # second one -- the second call's UPDATE (and its clock read) must never
    # have run.
    assert row["updated_at"] == first_tick
    parsed = parse_search_profile(row["search_profile_json"])
    assert parsed["fields"]["answer_language"]["value"] == "zh"


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
    role: str = "viewer",
) -> None:
    with core_stores.database.write() as connection:
        connection.execute(
            "INSERT INTO notebook_grants "
            "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,now())",
            (grant_id, notebook_id, principal_type, principal_id, role, owner_id),
        )


def test_access_predicates_match_the_sqlite_matrix(core_stores: CoreStores):
    """PG 侧的读/管理/写权矩阵必须与 `test_access_sql_contract.py` 的 SQLite 矩阵逐格相同。

    谓词的唯一定义点是 `repositories/*/access_sql.py` 两份镜像文件。SQLite 侧那份
    契约测试跑在 G1,单靠它看不见「只改了一个后端」的分叉;这条把同一张矩阵钉在 G3。
    写权恒 owner-only(只读成员与群组被授权者都是访客),不存在的 notebook 三权皆否。
    P1 扩展的四类授权边主体(user / group / group_admins / everyone)与哨兵停车行的
    fail-safe、以及 P2 新增的**管理权**(owner ∪ `role='admin'` 的有效授权边)一并
    在此对齐——管理权那一列是权限边界的放宽,单后端漂移的后果是「PG 部署的组管理员
    写不了 / 或写得比 SQLite 部署更多」,而这种分叉在 G1 里永远看不见。
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
        core_stores, "gr-admins", notebook_id, "group_admins", admins, owner.id,
        role="admin",
    )

    everyone_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Everyone grant"), owner.id
    )
    _pg_grant(core_stores, "gr-everyone", everyone_id, "everyone", "", owner.id)

    # everyone + role='admin'(手插的非法边,P2-T2 评审 P2-1):管理级主体判定排除
    # everyone,所以这条边对谁都不授予管理权;读权照旧全员放行。
    everyone_admin_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Everyone admin grant"), owner.id
    )
    _pg_grant(
        core_stores, "gr-everyone-admin", everyone_admin_id, "everyone", "", owner.id,
        role="admin",
    )

    # 管理库:把「主体类型」与「边的 role」两根轴交叉(与 SQLite 那份的 `managed` 同
    # 一形态)。`group` 边发成 admin → 整组人可管理;`group_admins` 边发成 viewer →
    # 组管理员可读不可管。按 principal_type 推断管理权的实现会把两格同时判反。
    managed_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Managed grant"), owner.id
    )
    _pg_grant(
        core_stores, "gr-managed-group", managed_id, "group", viewers, owner.id,
        role="admin",
    )
    _pg_grant(
        core_stores, "gr-managed-admins", managed_id, "group_admins", admins, owner.id,
        role="viewer",
    )

    # 哨兵停车行(正向 shadow 给冲突行写的非白名单 principal_type)必须谁也不放行,
    # 含最后那行带 `role='admin'` 的——管理权谓词比读权多一个 role 条件,于是多一种
    # 「先判 role、再放宽主体白名单」的失守形态,那行专钉它。
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
    _pg_grant(
        core_stores, "gr-parked-admin", sentinel_id, "__shadow_parked__", admins,
        owner.id, role="admin",
    )
    missing = "nb-does-not-exist"

    # (user_id, notebook_id, 期望读权, 期望**管理权**, 期望写权, P1 之前的旧读权口径)
    expected = [
        (owner.id, notebook_id, True, True, True, True),
        (member.id, notebook_id, True, False, False, True),  # 只读成员:能读,不能管/写
        (stranger.id, notebook_id, False, False, False, False),
        (grantee.id, notebook_id, True, False, False, False),
        (group_member.id, notebook_id, True, False, False, False),
        # P2 翻的那一格:管理边 → 管理权真,写权仍假(他不是 owner)。
        (group_admin.id, notebook_id, True, True, False, False),
        (group_plain.id, notebook_id, False, False, False, False),
        (owner.id, everyone_id, True, True, True, True),
        (stranger.id, everyone_id, True, False, False, False),
        (group_plain.id, everyone_id, True, False, False, False),
        # everyone + admin:读权全放行,管理权对谁都是 False(排除 everyone)。
        (stranger.id, everyone_admin_id, True, False, False, False),
        (group_plain.id, everyone_admin_id, True, False, False, False),
        (owner.id, everyone_admin_id, True, True, True, True),
        (owner.id, managed_id, True, True, True, True),
        (group_member.id, managed_id, True, True, False, False),   # group 边 + admin
        (group_admin.id, managed_id, True, False, False, False),   # group_admins + viewer
        (group_plain.id, managed_id, False, False, False, False),
        (stranger.id, managed_id, False, False, False, False),
        (owner.id, sentinel_id, True, True, True, True),
        (stranger.id, sentinel_id, False, False, False, False),
        (grantee.id, sentinel_id, False, False, False, False),
        (group_member.id, sentinel_id, False, False, False, False),
        (group_admin.id, sentinel_id, False, False, False, False),
        (owner.id, missing, False, False, False, False),  # 不存在的 notebook:三权皆否
        (member.id, missing, False, False, False, False),
        (stranger.id, missing, False, False, False, False),
        (grantee.id, missing, False, False, False, False),
        (group_admin.id, missing, False, False, False, False),
    ]
    for user_id, target, expect_read, expect_admin, expect_write, legacy_read in expected:
        assert sharing.user_can_read_notebook(target, user_id) is expect_read
        assert sharing.user_can_admin_notebook(target, user_id) is expect_admin
        assert sharing.user_can_access_notebook(target, user_id) is expect_write
        # 包含链 `写权 ⊆ 管理权 ⊆ 读权` 逐格成立(与 SQLite 那份同款结构断言)。
        assert not (expect_write and not expect_admin)
        assert not (expect_admin and not expect_read)
        # P1 之前 service 层的旧口径(写权 or 成员):老主体上必须与新谓词逐格相同,
        # 只有授权边主体才允许「新真旧假」。
        legacy = sharing.user_can_access_notebook(
            target, user_id
        ) or sharing.is_member(target, user_id)
        assert legacy is legacy_read
        assert not (legacy and not expect_read)

    # 读权/管理权都是实时判定而非一次性授予:踢掉成员/组成员/授权边都即刻失效。
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
    # group_admins 那条边没动,组管理员仍可读可管——上面两条删除没有误伤别的主体。
    assert sharing.user_can_read_notebook(notebook_id, group_admin.id) is True
    assert sharing.user_can_admin_notebook(notebook_id, group_admin.id) is True

    # 组内降级即刻失管理权,读权跟着一起没(那条边是他唯一的读权来源)。
    with core_stores.database.write() as connection:
        connection.execute(
            "UPDATE group_members SET role='member' WHERE group_id=%s AND user_id=%s",
            (admins, group_admin.id),
        )
    assert sharing.user_can_admin_notebook(notebook_id, group_admin.id) is False
    assert sharing.user_can_read_notebook(notebook_id, group_admin.id) is False

    # 把管理边降成 viewer:管理权当场消失,读权原样保留(两根轴各判各的)。
    with core_stores.database.write() as connection:
        connection.execute(
            "UPDATE group_members SET role='admin' WHERE group_id=%s AND user_id=%s",
            (admins, group_admin.id),
        )
        connection.execute(
            "UPDATE notebook_grants SET role='viewer' WHERE id='gr-admins'"
        )
    assert sharing.user_can_admin_notebook(notebook_id, group_admin.id) is False
    assert sharing.user_can_read_notebook(notebook_id, group_admin.id) is True


def test_group_store_crud_and_membership_mirror_the_sqlite_store(
    core_stores: CoreStores,
):
    """群组 / 组成员的 PG 行为必须与 `test_group_routes.py` 的 SQLite 矩阵逐条相同。

    G1 只跑得到 SQLite 那一份,单靠它看不见「只改了一个后端」的分叉。这里钉的三件事
    都是**分叉了不会报错、只会静默走样**的形态:建组是否同时落下 owner 与管理员、
    owner 保护和转让是否在同一事务里判、成员/群组清单的顺序会不会随 collation 漂。
    """
    from app.repositories.ports import (
        GroupAdminRequiredError,
        GroupOwnerProtectedError,
        GroupOwnerRequiredError,
        GroupOwnerTransferTargetError,
    )

    groups = core_stores.groups
    owner = core_stores.identity.create_user("k00123456", "password-40")
    member = core_stores.identity.create_user("l00123456", "password-41")
    outsider = core_stores.identity.create_user("m00123456", "password-42")

    created = groups.create_group(
        name="项目组", kind="project", description="说明", created_by=owner.id
    )
    # 建组即建组管理员 —— 中间没有「有组无管理员」的窗口。
    assert created["my_role"] == "admin"
    assert created["owner_id"] == owner.id
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

    # 唯一 owner 与成员行在同一事务里受保护；即便另有管理员也不能绕过转让。
    with pytest.raises(GroupOwnerProtectedError):
        groups.upsert_member(group_id, owner.id, role="member", added_by=owner.id)
    with pytest.raises(GroupOwnerProtectedError):
        groups.remove_member(group_id, owner.id)
    groups.upsert_member(group_id, member.id, role="admin", added_by=owner.id)

    with pytest.raises(GroupOwnerRequiredError):
        groups.transfer_group_owner(
            group_id, new_owner_id=member.id, actor_id=member.id
        )
    with pytest.raises(GroupOwnerTransferTargetError):
        groups.transfer_group_owner(
            group_id, new_owner_id=outsider.id, actor_id=owner.id
        )
    transferred = groups.transfer_group_owner(
        group_id, new_owner_id=member.id, actor_id=owner.id
    )
    assert transferred["owner_id"] == member.id
    assert {row["id"]: row["role"] for row in groups.list_members(group_id)} == {
        owner.id: "admin",
        member.id: "admin",
    }
    assert groups.remove_member(group_id, owner.id) is True
    assert groups.remove_member(group_id, owner.id) is False
    with pytest.raises(GroupOwnerProtectedError):
        groups.remove_member(group_id, member.id)

    # Invitation capability parity: state reads are side-effect free, issue is
    # idempotent, rotate/revoke are atomic under the group root lock, and join
    # adds only a plain member without touching an existing administrator.
    assert groups.get_invite_state(group_id, actor_id=member.id) == {
        "active": False, "token": "", "created_at": None,
    }
    with pytest.raises(GroupAdminRequiredError):
        groups.get_invite_state(group_id, actor_id=outsider.id)
    with pytest.raises(GroupAdminRequiredError):
        groups.issue_invite(
            group_id, token="gri-denied", actor_id=outsider.id
        )
    invite = groups.issue_invite(
        group_id, token="gri-first", actor_id=member.id
    )
    assert invite["token"] == "gri-first"
    assert groups.issue_invite(
        group_id, token="gri-unused", actor_id=member.id
    )["token"] == "gri-first"
    joined = groups.join_by_invite("gri-first", user_id=outsider.id)
    assert joined is not None and joined["my_role"] == "member"
    rotated = groups.issue_invite(
        group_id, token="gri-second", actor_id=member.id, rotate=True
    )
    assert rotated["token"] == "gri-second"
    assert groups.join_by_invite("gri-first", user_id=outsider.id) is None
    assert groups.revoke_invite(group_id, actor_id=member.id) is True
    assert groups.join_by_invite("gri-second", user_id=outsider.id) is None

    assert groups.update_group(group_id, name="改名") is True
    assert groups.get_group(group_id)["name"] == "改名"
    assert groups.get_group(group_id)["description"] == "说明"  # 未传的字段不动
    assert groups.update_group(group_id) is True  # 合法 no-op
    assert groups.update_group("grp-missing", name="X") is False

    assert groups.find_user_by_username("k00123456")["id"] == owner.id
    assert groups.find_user_by_username("k0012345") is None  # 精确匹配,不认前缀
    assert groups.find_user_by_id(owner.id)["username"] == "k00123456"
    assert groups.find_user_by_id("user-missing") is None

    # 删除也在同一把群组根锁内复核 live owner，路由前置判断不是授权真源。
    with pytest.raises(GroupOwnerRequiredError):
        groups.delete_group(group_id, actor_id=owner.id)
    assert groups.get_group(group_id)["owner_id"] == member.id


def test_group_grants_crud_and_group_deletion_mirror_the_sqlite_store(
    core_stores: CoreStores,
):
    """授权边 CRUD + 删组清理。重复授权必须是**明确冲突**而不是静默复用。"""
    from app.repositories.ports import (
        GroupAdminRequiredError,
        GroupGrantAlreadyExists,
        GroupNotFoundError,
    )

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
        admin_user_id=owner.id,
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
            admin_user_id=owner.id,
        )
    # 同组的另一个主体类型是**另一条**边,不受 UNIQUE 影响。
    groups.create_grant(
        notebook_id,
        principal_type="group_admins",
        principal_id=group["id"],
        role="admin",
        created_by=owner.id,
        admin_user_id=owner.id,
    )
    # 本端点建不出来、但库里可能存在的 everyone 行:不得被 LEFT JOIN 误配上组名。
    _pg_grant(core_stores, "gr-all", notebook_id, "everyone", "", owner.id)
    listed = groups.list_grants(notebook_id)
    assert {g["principal_type"] for g in listed} == {"group", "group_admins", "everyone"}
    everyone = next(g for g in listed if g["principal_type"] == "everyone")
    assert everyone["principal_name"] == "" and everyone["principal_kind"] == ""

    # 孤儿边(指向已不存在的组)必须带可识别的失效标注,而不是与正常条目长得一样、
    # 只是没有名字。`user`/`everyone` 主体**不得**被误标成 missing —— 它们本来就没有
    # 组可解析。
    _pg_grant(core_stores, "gr-orphan", notebook_id, "group", "grp-vanished", owner.id)
    with_orphan = {g["id"]: g for g in groups.list_grants(notebook_id)}
    assert with_orphan["gr-orphan"]["principal_kind"] == "missing"
    assert with_orphan["gr-orphan"]["principal_name"] == ""
    assert with_orphan["gr-all"]["principal_kind"] == ""
    _write_sql(core_stores, "DELETE FROM notebook_grants WHERE id='gr-orphan'")

    # grant id 必须与 notebook 一起验:否则「我有一本自己的库的管理权」就变成
    # 「我能删任何库上的授权边」。
    assert groups.delete_grant(another_notebook, grant["id"]) is False
    assert groups.delete_grant(notebook_id, grant["id"]) is True
    assert groups.delete_grant(notebook_id, grant["id"]) is False

    groups.create_grant(
        notebook_id,
        principal_type="group",
        principal_id=group["id"],
        role="viewer",
        created_by=owner.id,
        admin_user_id=owner.id,
    )
    groups.create_grant(
        another_notebook,
        principal_type="group",
        principal_id=other["id"],
        role="viewer",
        created_by=owner.id,
        admin_user_id=owner.id,
    )
    shared = groups.list_group_shared_notebooks(group["id"])
    assert [item["notebook_id"] for item in shared] == [notebook_id]
    assert shared[0]["name"] == "共享库"
    assert shared[0]["owner_username"] == "n00123456"
    assert sorted(shared[0]["roles"]) == ["admin", "viewer"]  # 同库两条边折成一项

    # 双重条件的群组那一半在**写事务里**复核:发起者不是组管理员 / 组不存在,
    # 都在插入之前失败关闭。路由层那次前置查询不参与判定(它只负责文案)。
    outsider = core_stores.identity.create_user("r00123456", "password-47")
    with pytest.raises(GroupAdminRequiredError):
        groups.create_grant(
            another_notebook,
            principal_type="group",
            principal_id=group["id"],
            role="viewer",
            created_by=outsider.id,
            admin_user_id=outsider.id,
        )
    with pytest.raises(GroupNotFoundError):
        groups.create_grant(
            another_notebook,
            principal_type="group",
            principal_id="grp-does-not-exist",
            role="viewer",
            created_by=owner.id,
            admin_user_id=owner.id,
        )

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


def test_share_requests_mirror_the_sqlite_approval_flow(core_stores: CoreStores):
    """成员贡献审批流的 PG 行为必须与 `test_share_requests.py` 的 SQLite 矩阵逐条相同。

    这里钉的是**分叉了不会报错、只会静默走样**的 PG 专有形态:`ON CONFLICT ON
    CONSTRAINT` 写授权边、`FOR UPDATE` 防并发双审、`iso_timestamp` 归一 `decided_at`、
    以及撞 `uq_share_requests_one_pending` 时靠 `exc.diag.constraint_name` 判幂等。
    """
    from app.repositories.ports import ShareRequestNotPendingError

    groups = core_stores.groups
    boss = core_stores.identity.create_user("p00123456", "password-70")
    librarian = core_stores.identity.create_user("q00123456", "password-71")
    group = groups.create_group(
        name="芯片项目", kind="project", description="", created_by=boss.id
    )
    groups.upsert_member(group["id"], librarian.id, role="member", added_by=boss.id)
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="共享库"), librarian.id
    )

    created = groups.create_share_request(
        notebook_id, group_id=group["id"], requested_by=librarian.id
    )
    assert created["status"] == "pending"
    # decided_at 两态:pending → None(经 iso_timestamp 归一后仍是 None,不是空串)。
    assert created["decided_at"] is None
    assert created["decided_by"] is None
    assert created["notebook_name"] == "共享库"
    assert created["requested_by_username"] == "q00123456"

    # 撞 pending 唯一索引 → 幂等返回既有行(靠 constraint_name 判,不是别的约束)。
    again = groups.create_share_request(
        notebook_id, group_id=group["id"], requested_by=librarian.id
    )
    assert again["id"] == created["id"]
    assert [r["id"] for r in groups.list_pending_share_requests(group["id"])] == [created["id"]]
    assert [r["id"] for r in groups.list_my_share_requests(notebook_id, requested_by=librarian.id)] == [created["id"]]
    # 全局入口(codex #519 R11 P1):唯一谓词是 requested_by,只回 pending。
    # boss 看得见整个组队列,但他自己没提过申请,所以这条清单对他是空的。
    assert [
        r["id"] for r in groups.list_pending_share_requests_by_requester(librarian.id)
    ] == [created["id"]]
    assert groups.list_pending_share_requests_by_requester(boss.id) == []

    # 批准:同事务写 (group, viewer) 边 + 状态 approved;decided_at 变非空 ISO。
    decided = groups.approve_share_request(
        group["id"], created["id"], decided_by=boss.id
    )
    assert decided["status"] == "approved"
    assert decided["decided_by"] == boss.id
    assert isinstance(decided["decided_at"], str) and decided["decided_at"]
    edges = groups.list_grants(notebook_id)
    assert [(g["principal_type"], g["role"]) for g in edges] == [("group", "viewer")]

    # 已决定的申请再批 → None(FOR UPDATE + 精确 status 匹配);撤回 → 409 语义。
    assert groups.approve_share_request(group["id"], created["id"], decided_by=boss.id) is None
    with pytest.raises(ShareRequestNotPendingError):
        groups.delete_share_request(notebook_id, created["id"], librarian.id)

    # 驳回一条新申请:状态 rejected、不写边;撤回一条 pending:删整行。
    reject_target = groups.create_share_request(
        notebook_id, group_id=group["id"], requested_by=librarian.id
    )
    rejected = groups.reject_share_request(
        group["id"], reject_target["id"], decided_by=boss.id
    )
    assert rejected["status"] == "rejected"
    assert isinstance(rejected["decided_at"], str) and rejected["decided_at"]

    withdraw_target = groups.create_share_request(
        notebook_id, group_id=group["id"], requested_by=librarian.id
    )
    assert (
        groups.delete_share_request(notebook_id, withdraw_target["id"], librarian.id)
        == "deleted"
    )
    assert (
        groups.delete_share_request(notebook_id, withdraw_target["id"], librarian.id)
        == "not_found"
    )
    assert groups.list_pending_share_requests(group["id"]) == []

    # 撤回只属于申请者本人:别人(哪怕是组管理员 boss)撤不掉,且与「不存在」同样
    # 返回 not_found、不泄露存在性(codex #519 R1 P1,与 SQLite 侧同口径)。
    others = groups.create_share_request(
        notebook_id, group_id=group["id"], requested_by=librarian.id
    )
    assert groups.delete_share_request(notebook_id, others["id"], boss.id) == "not_found"
    assert [r["id"] for r in groups.list_pending_share_requests(group["id"])] == [
        others["id"]
    ]
    assert (
        groups.delete_share_request(notebook_id, others["id"], librarian.id) == "deleted"
    )

    # 已决定的申请不进全局清单(撤不回来,列出来只是多披露一份历史 + 审批者身份)。
    decided_one = groups.create_share_request(
        notebook_id, group_id=group["id"], requested_by=librarian.id
    )
    assert [
        r["id"] for r in groups.list_pending_share_requests_by_requester(librarian.id)
    ] == [decided_one["id"]]
    assert groups.reject_share_request(
        group["id"], decided_one["id"], decided_by=boss.id
    )["status"] == "rejected"
    assert groups.list_pending_share_requests_by_requester(librarian.id) == []

    # 两个展示标签按**当前**权限逐个给(codex #519 R12 P2)。PG 侧的占位符顺序与 SQLite
    # 不同(`read_access_params` 的个数由谓词自己决定),所以这一格必须在 PG 上真跑一遍。
    labelled = groups.create_share_request(
        notebook_id, group_id=group["id"], requested_by=librarian.id
    )
    both = groups.list_pending_share_requests_by_requester(librarian.id)[0]
    assert both["notebook_name"] == "共享库" and both["group_name"] == "芯片项目"
    # 把他移出组:组名那半消失,而库名那半**不受连累**(他是这本库的 owner,读权还在)。
    assert groups.remove_member(group["id"], librarian.id) is True
    after = groups.list_pending_share_requests_by_requester(librarian.id)[0]
    assert after["id"] == labelled["id"], "隐藏的是标签,不是行"
    assert after["group_name"] == ""
    assert after["notebook_name"] == "共享库"


def test_share_request_authorization_rechecks_mirror_the_sqlite_store(
    core_stores: CoreStores,
):
    """审批资格 / 申请人成员资格的**事务内**复核必须双后端同款(codex #519 R2 P1+P2-1)。

    路由守卫与写事务之间的窗口足够让人被降级或移出组;批准会把**整组**的读权放出去,
    所以承重的复核住在 store 的写事务里。PG 侧的调用点排在 `FOR UPDATE` 之后,分叉了
    不会报错——只会让 PG 部署上的降级管理员仍能审批,而 SQLite 部署被拦下。
    """
    from app.repositories.ports import (
        GroupAdminRequiredError,
        GroupMembershipRequiredError,
    )

    groups = core_stores.groups
    boss = core_stores.identity.create_user("v00112233", "password-74")
    librarian = core_stores.identity.create_user("v00445566", "password-75")
    outsider = core_stores.identity.create_user("v00778899", "password-76")
    group = groups.create_group(
        name="复检组", kind="project", description="", created_by=boss.id
    )
    groups.upsert_member(group["id"], librarian.id, role="member", added_by=boss.id)
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="复检库"), librarian.id
    )
    request = groups.create_share_request(
        notebook_id, group_id=group["id"], requested_by=librarian.id
    )

    # 普通成员与完全的外人都不能审批(批准与驳回同一道闸)。
    for who in (librarian.id, outsider.id):
        with pytest.raises(GroupAdminRequiredError):
            groups.approve_share_request(group["id"], request["id"], decided_by=who)
        with pytest.raises(GroupAdminRequiredError):
            groups.reject_share_request(group["id"], request["id"], decided_by=who)
    # 一条边都没发出去、申请仍 pending。
    assert groups.list_grants(notebook_id) == []
    assert [r["id"] for r in groups.list_pending_share_requests(group["id"])] == [request["id"]]

    # 系统管理员的运维旁路由**路由**证明并传入,store 不读 users.role。
    decided = groups.approve_share_request(
        group["id"], request["id"],
        decided_by=outsider.id, decided_by_is_system_admin=True,
    )
    assert decided["status"] == "approved"

    # 非成员发不出申请(成员资格同样在事务内复核)。
    with pytest.raises(GroupMembershipRequiredError):
        groups.create_share_request(
            notebook_id, group_id=group["id"], requested_by=outsider.id
        )


def test_approval_rechecks_the_requesters_manage_rights_on_postgres(
    core_stores: CoreStores,
):
    """批准前复核**申请人**此刻仍对该库有管理权(codex #519 R4 裁决变更,PG 侧)。

    授权在生效时刻实时判定、绝不缓存:提交与批准之间库主可以撤掉申请人的管理权,而批准
    会把那次陈旧检查兑现成一条**活的**授权边。谓词取 `access_sql.NOTEBOOK_ADMIN_SQL`,
    两个后端必须判出同一个结果——分叉了不会报错,只会让某一个后端上的失权申请照样批得掉。
    """
    from app.repositories.ports import ShareRequesterUnauthorizedError

    groups = core_stores.groups
    alice = core_stores.identity.create_user("y00112233", "password-81")   # 库主
    bob = core_stores.identity.create_user("y00445566", "password-82")     # 申请人
    carol = core_stores.identity.create_user("y00778899", "password-83")   # G1 组管理员
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="失权库"), alice.id
    )
    # Bob 经「授权组」的 group_admins 边拿到管理权。
    grantor = groups.create_group(
        name="授权组", kind="project", description="", created_by=alice.id
    )
    groups.upsert_member(grantor["id"], bob.id, role="admin", added_by=alice.id)
    edge = groups.create_grant(
        notebook_id,
        principal_type="group_admins",
        principal_id=grantor["id"],
        role="admin",
        created_by=alice.id,
        admin_user_id=alice.id,
    )
    # 目标组 G1:Bob 只是成员。
    target = groups.create_group(
        name="G1", kind="project", description="", created_by=carol.id
    )
    groups.upsert_member(target["id"], bob.id, role="member", added_by=carol.id)
    request = groups.create_share_request(
        notebook_id, group_id=target["id"], requested_by=bob.id
    )

    # 还有管理权时批得掉这件事由下面的反向断言保证——先撤边,再证明批不掉。
    assert groups.delete_grant(notebook_id, edge["id"]) is True
    with pytest.raises(ShareRequesterUnauthorizedError):
        groups.approve_share_request(
            target["id"], request["id"], decided_by=carol.id
        )
    # 零副作用:没有边落库,申请仍 pending。
    assert groups.list_grants(notebook_id) == []
    assert [r["id"] for r in groups.list_pending_share_requests(target["id"])] == [request["id"]]

    # 驳回不做这条复检 —— 终止不产生授权。
    assert groups.reject_share_request(
        target["id"], request["id"], decided_by=carol.id
    )["status"] == "rejected"

    # 反向护栏:申请人仍是 owner 的库,批准照常成功(复检不是恒关的闸)。
    own = core_stores.notebooks.create_row(NotebookCreate(name="Bob 自己的"), bob.id)
    ok = groups.create_share_request(own, group_id=target["id"], requested_by=bob.id)
    assert groups.approve_share_request(
        target["id"], ok["id"], decided_by=carol.id
    )["status"] == "approved"


def test_share_request_conflict_is_narrowed_to_the_requester_on_postgres(
    core_stores: CoreStores,
):
    """幂等按申请者收窄 + 冲突恢复期间被决定则重试插入(codex #519 R3,PG 侧)。

    PG 的分类走 `exc.diag.constraint_name`(SQLite 走异常文本),两侧分叉了不会报错——
    只会让某一个后端把别人的申请行交给你,或者把一次唯一违例冒成 500。

    ⚠ 两个申请人都必须是目标组的**普通成员**(codex #519 R8 P2):组管理员分享进自己
    管理的组永远走 `create_grant`、不经这张表,所以由第三个人 `chief` 建组并审批。此前
    这里用建组的人当申请人,那条路 R8 之后已经被 `GroupAdminShouldShareDirectlyError`
    挡住——冲突收窄这件事本身与角色无关,换个人即可。
    """
    from app.repositories.ports import ShareRequestAlreadyPendingError

    groups = core_stores.groups
    chief = core_stores.identity.create_user("w00778899", "password-79")
    boss = core_stores.identity.create_user("w00112233", "password-77")
    other = core_stores.identity.create_user("w00445566", "password-78")
    group = groups.create_group(
        name="冲突组", kind="project", description="", created_by=chief.id
    )
    groups.upsert_member(group["id"], boss.id, role="member", added_by=chief.id)
    groups.upsert_member(group["id"], other.id, role="member", added_by=chief.id)
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="冲突库"), boss.id
    )

    mine = groups.create_share_request(
        notebook_id, group_id=group["id"], requested_by=boss.id
    )
    # 本人重复提交 → 幂等返回同一行。
    assert groups.create_share_request(
        notebook_id, group_id=group["id"], requested_by=boss.id
    )["id"] == mine["id"]
    # 别人提交 → 明确冲突,绝不把 boss 的行交给他。
    with pytest.raises(ShareRequestAlreadyPendingError):
        groups.create_share_request(
            notebook_id, group_id=group["id"], requested_by=other.id
        )

    # 冲突恢复期间那条 pending 被决定 → 重试插入而不是让 UniqueViolation 冒成 500。
    original = groups._pending_share_request
    calls: list[int] = []

    def decide_then_answer(nb_id, gid):
        if not calls:
            calls.append(1)
            # 审批必须由组管理员做 —— boss 现在只是普通成员(见 docstring 的 R8 P2 说明)。
            groups.approve_share_request(gid, mine["id"], decided_by=chief.id)
        return original(nb_id, gid)

    groups._pending_share_request = decide_then_answer  # type: ignore[assignment]
    try:
        retried = groups.create_share_request(
            notebook_id, group_id=group["id"], requested_by=boss.id
        )
    finally:
        groups._pending_share_request = original  # type: ignore[assignment]
    assert calls, "注入未生效——没有走到冲突恢复路径"
    assert retried["status"] == "pending" and retried["id"] != mine["id"]


@pytest.mark.parametrize(
    "decision,suffix",
    [("approve_share_request", "01"), ("reject_share_request", "02")],
)
def test_both_decisions_take_the_group_lock_before_rechecking(
    core_stores: CoreStores, decision: str, suffix: str
):
    """批准**与驳回**都必须先锁 `groups` 行,再复核审批资格(codex #519 R3)。

    只有 approve 拿锁时,reject 的事务内复核仍留着 TOCTOU 窗口:一个并发的降级事务可以
    在复核读到 `admin` 之后、`UPDATE` 之前提交,被降级的人照样把别人的申请判死。锁把
    「复核 + 决定」整段与成员变更串起来,两条路径的锁序必须一致。

    判据用 `pg_locks`:在决定的写事务**内部**(注入在资格复核这一步)查本事务是否已经
    持有 `groups` 那一行的行锁。持锁 = 真的锁过;没持 = 复核在裸奔。
    """
    groups = core_stores.groups
    boss = core_stores.identity.create_user(f"x001122{suffix}", "password-79")
    librarian = core_stores.identity.create_user(f"x004455{suffix}", "password-80")
    group = groups.create_group(
        name="锁序组", kind="project", description="", created_by=boss.id
    )
    groups.upsert_member(group["id"], librarian.id, role="member", added_by=boss.id)
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="锁序库"), librarian.id
    )
    request = groups.create_share_request(
        notebook_id, group_id=group["id"], requested_by=librarian.id
    )

    original = groups._require_share_decider_on
    held: list[int] = []

    def observe_then_recheck(connection, gid, decided_by, is_system_admin):
        # 本事务此刻是否已持有 groups 上的行锁?`transactionid`/`tuple` 锁都算。
        held.append(
            connection.execute(
                "SELECT COUNT(*) FROM pg_locks l "
                "JOIN pg_class c ON c.oid = l.relation "
                "WHERE c.relname = 'groups' AND l.pid = pg_backend_pid() "
                "AND l.granted"
            ).fetchone()["count"]
        )
        return original(connection, gid, decided_by, is_system_admin)

    groups._require_share_decider_on = observe_then_recheck  # type: ignore[assignment]
    try:
        getattr(groups, decision)(group["id"], request["id"], decided_by=boss.id)
    finally:
        groups._require_share_decider_on = original  # type: ignore[assignment]

    assert held, "资格复核没被调用——注入失效"
    assert held[0] > 0, (
        f"{decision} 在复核审批资格时没有持有 groups 行锁,TOCTOU 窗口仍在"
    )


def test_concurrent_revocation_cannot_slip_past_the_requester_recheck(
    core_stores: CoreStores,
    postgres_settings: Settings,
):
    """撤销申请人的管理边必须与批准在**授权边行**上串行(codex #519 R5)。

    R4 的复检跑的是不加锁的 `NOTEBOOK_ADMIN_SQL`:PG 在 READ COMMITTED 下让它看到语句
    开始时的快照,库主并发 `DELETE` 掉那条 admin 边并提交,审批事务照样读到撤销前的行、
    照样 INSERT 一条**活的** `(group, viewer)` 边。窗口更窄,但要防的事一件没防住。
    给授权边行补上 `FOR SHARE` 之后,撤销会**阻塞**到审批提交。(R8 P1 之后那把锁由
    `ADMIN_GRANT_GROUP_CHAIN_FOR_SHARE_SQL` / `ADMIN_GRANT_USER_ARM_FOR_SHARE_SQL` 提供,
    并且连让边生效的那行 `group_members` 一起锁住;本用例钉的仍是边行那一环。)

    交错是注入的:阻塞点选在申请人复检**之后、INSERT 授权边之前**(挂在 `new_id("gnt")`
    上),此时复检已经拿到了那条边的 `FOR SHARE`。放行前先用 `pg_stat_activity` 轮询证明
    撤销线程**真的卡在锁上**——不用 sleep 去和 1 秒的 `lock_timeout` 赛跑(CI 已经因为
    那个形态红过一次)。
    """
    import threading

    import psycopg

    groups = core_stores.groups
    alice = core_stores.identity.create_user("z00112233", "password-84")  # 库主
    bob = core_stores.identity.create_user("z00445566", "password-85")    # 申请人
    carol = core_stores.identity.create_user("z00778899", "password-86")  # G1 组管理员
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="竞态库"), alice.id
    )
    grantor = groups.create_group(
        name="授权组", kind="project", description="", created_by=alice.id
    )
    groups.upsert_member(grantor["id"], bob.id, role="admin", added_by=alice.id)
    edge = groups.create_grant(
        notebook_id,
        principal_type="group_admins",
        principal_id=grantor["id"],
        role="admin",
        created_by=alice.id,
        admin_user_id=alice.id,
    )
    target = groups.create_group(
        name="G1", kind="project", description="", created_by=carol.id
    )
    groups.upsert_member(target["id"], bob.id, role="member", added_by=carol.id)
    request = groups.create_share_request(
        notebook_id, group_id=target["id"], requested_by=bob.id
    )

    parked = threading.Event()   # 复检已通过并持锁,尚未插入授权边
    release = threading.Event()
    original_new_id = groups.new_id

    def blocking_new_id(prefix: str) -> str:
        if prefix == "gnt":
            parked.set()
            assert release.wait(timeout=15), "approve 迟迟未被放行"
        return original_new_id(prefix)

    failures: list[BaseException] = []
    lock = threading.Lock()

    def do_approve():
        try:
            groups.approve_share_request(
                target["id"], request["id"], decided_by=carol.id
            )
        except BaseException as error:  # noqa: BLE001 — surfaced below
            with lock:
                failures.append(error)

    def do_revoke():
        try:
            groups.delete_grant(notebook_id, edge["id"])
        except BaseException as error:  # noqa: BLE001 — surfaced below
            with lock:
                failures.append(error)

    def wait_until_a_backend_blocks_on_a_lock(deadline_seconds: float = 10.0) -> bool:
        end = time.monotonic() + deadline_seconds
        with psycopg.connect(postgres_settings.database_url) as observer:
            while time.monotonic() < end:
                blocked = observer.execute(
                    "SELECT COUNT(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
                ).fetchone()[0]
                if blocked:
                    return True
                time.sleep(0.01)
        return False

    groups.new_id = blocking_new_id  # type: ignore[assignment]
    try:
        approve_thread = threading.Thread(target=do_approve)
        approve_thread.start()
        assert parked.wait(timeout=15), "approve 未在插入授权边之前停住"

        revoke_thread = threading.Thread(target=do_revoke)
        revoke_thread.start()
        # 撤销必须卡在 `FOR SHARE` 持住的那条边行上。没有这把锁时它会**直接删掉并提交**,
        # 于是轮询等不到任何锁等待 —— 这条断言就是变异守卫本身。
        assert wait_until_a_backend_blocks_on_a_lock(), (
            "撤销没有被锁挡住——申请人复检没有锁住授权边行,竞态仍然成立"
        )
        assert revoke_thread.is_alive()

        release.set()
        approve_thread.join(timeout=30)
        revoke_thread.join(timeout=30)
        assert not approve_thread.is_alive() and not revoke_thread.is_alive()
    finally:
        groups.new_id = original_new_id  # type: ignore[assignment]
        release.set()

    assert not failures, failures
    # 序列化顺序是「批准在前、撤销在后」:批准落了 (group, viewer) 边,随后 alice 的撤销
    # 拿掉了 bob 那条 admin 边。终态里 bob 已失权,而那条 viewer 边是**在他仍有权时**批
    # 出去的 —— 语义正确(库主看到的是「我撤销时它刚好已经批了」)。
    remaining = {(g["principal_type"], g["role"]) for g in groups.list_grants(notebook_id)}
    assert ("group_admins", "admin") not in remaining, "撤销最终没有生效"
    assert remaining == {("group", "viewer")}


def test_concurrent_revocation_cannot_slip_past_create_grant(
    core_stores: CoreStores,
    postgres_settings: Settings,
):
    """`create_grant` 也要锁住**发起人**的授权边行(codex #519 R6 P1)。

    与 R5 的申请人复检同一条竞态、同一条裁决:凡是写 `notebook_grants` 的路径,都必须在
    同一写事务内复检并**锁住**发起人的笔记本侧权限。不锁的话,库主并发撤销掉发起人的
    admin 边并提交,而本事务仍读到撤销前的快照,照样发出一条新的授权边——失权者把访问权
    继续散了出去。

    交错注入在复检**之后、INSERT 之前**(挂 `new_id("gnt")`),再用 `pg_stat_activity`
    轮询证明撤销真的卡在锁上(不用 sleep 和 1 秒的 `lock_timeout` 赛跑)。
    """
    import threading

    import psycopg

    groups = core_stores.groups
    alice = core_stores.identity.create_user("b00112233", "password-87")  # 库主
    bob = core_stores.identity.create_user("b00445566", "password-88")    # 发起人
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="发边竞态库"), alice.id
    )
    grantor = groups.create_group(
        name="授权组", kind="project", description="", created_by=alice.id
    )
    groups.upsert_member(grantor["id"], bob.id, role="admin", added_by=alice.id)
    edge = groups.create_grant(
        notebook_id,
        principal_type="group_admins",
        principal_id=grantor["id"],
        role="admin",
        created_by=alice.id,
        admin_user_id=alice.id,
    )
    # Bob 自己的组:他要把 Alice 的库再散给它。
    bobs_group = groups.create_group(
        name="Bob 的组", kind="project", description="", created_by=bob.id
    )

    parked = threading.Event()
    release = threading.Event()
    # ⚠ park 点必须挂在**复检本身**上,不能挂 `new_id("gnt")`:`create_grant` 在进入写
    # 事务**之前**就把 grant id 铸好了,挂那里会停在复检与锁之前,证明不了任何事。
    original_recheck = groups._require_notebook_manage_on

    def parking_recheck(connection, notebook_id_, user_id_):
        original_recheck(connection, notebook_id_, user_id_)  # 先真复检 + 拿 FOR SHARE
        parked.set()
        assert release.wait(timeout=15), "create_grant 迟迟未被放行"

    failures: list[BaseException] = []
    lock = threading.Lock()

    def do_grant():
        try:
            groups.create_grant(
                notebook_id,
                principal_type="group",
                principal_id=bobs_group["id"],
                role="viewer",
                created_by=bob.id,
                admin_user_id=bob.id,
            )
        except BaseException as error:  # noqa: BLE001 — surfaced below
            with lock:
                failures.append(error)

    def do_revoke():
        try:
            groups.delete_grant(notebook_id, edge["id"])
        except BaseException as error:  # noqa: BLE001 — surfaced below
            with lock:
                failures.append(error)

    def wait_until_a_backend_blocks_on_a_lock(deadline_seconds: float = 10.0) -> bool:
        end = time.monotonic() + deadline_seconds
        with psycopg.connect(postgres_settings.database_url) as observer:
            while time.monotonic() < end:
                blocked = observer.execute(
                    "SELECT COUNT(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
                ).fetchone()[0]
                if blocked:
                    return True
                time.sleep(0.01)
        return False

    groups._require_notebook_manage_on = parking_recheck  # type: ignore[assignment]
    try:
        grant_thread = threading.Thread(target=do_grant)
        grant_thread.start()
        assert parked.wait(timeout=15), "create_grant 未在插入授权边之前停住"

        revoke_thread = threading.Thread(target=do_revoke)
        revoke_thread.start()
        # 撤销必须卡在复检持住的那条边行上;不加 FOR SHARE 时它会直接删掉并提交,
        # 轮询等不到任何锁等待——这条断言就是变异守卫本身。
        assert wait_until_a_backend_blocks_on_a_lock(), (
            "撤销没有被锁挡住——create_grant 的发起人复检没有锁住授权边行,竞态仍然成立"
        )
        assert revoke_thread.is_alive()

        release.set()
        grant_thread.join(timeout=30)
        revoke_thread.join(timeout=30)
        assert not grant_thread.is_alive() and not revoke_thread.is_alive()
    finally:
        groups._require_notebook_manage_on = original_recheck  # type: ignore[assignment]
        release.set()

    assert not failures, failures
    # 序列化成「发边在前、撤销在后」:Bob 的边发出去了(那一刻他确实还有权),随后
    # Alice 的撤销生效。终态里 Bob 的 admin 边已经没了。
    remaining = {(g["principal_type"], g["role"]) for g in groups.list_grants(notebook_id)}
    assert ("group_admins", "admin") not in remaining, "撤销最终没有生效"
    assert remaining == {("group", "viewer")}


def test_concurrent_approval_and_group_deletion_leave_no_orphan_grant(
    core_stores: CoreStores,
    postgres_settings: Settings,
):
    """批准写授权边必须与删组在 `groups` 行上串行,否则留下**孤儿边** —— PG 独有的竞态。

    `approve_share_request` 是真正写 `notebook_grants` 边的地方,而 `principal_id` 是多态
    无 FK 列,`DELETE FROM groups` 的 CASCADE 带不走它(见 `delete_group` docstring)。
    不在写事务开头锁 `groups` 行,一个并发的 `delete_group` 可以在「它清完本组的边」与
    「approve 插入新边」之间穿过去 —— 那条边指向一个已删除的组 = 孤儿边。SQLite 的进程
    写锁天然串行、复现不了,守卫必须住在这里(G3 conformance)。

    交错是**注入**的、不靠运气:把 approve 的授权边 id 生成(`new_id("gnt")`)挂在一个
    事件上,让 approve 停在「已锁请求行、尚未插入边」处;再放 `delete_group` 跑到它的
    阻塞点(补锁后卡在 groups 行锁、删锁后卡在 group DELETE 的 CASCADE),最后放行
    approve。补了 `FOR SHARE` 后两者在 groups 行上串行、终态无孤儿边;删掉那把锁,这个
    交错必然造出孤儿边 —— 删锁变异因此必红。

    ⚠ **放行判据必须是「delete 真的被锁挡住了」而不是「睡够了 N 秒」**(codex #519 R3
    必修 A)。本用例最初 `sleep(1.0)` 再断言线程还活着,而部署的
    `postgres_lock_timeout_seconds` 在测试夹具里正是 **1 秒** —— 等于让被测的交错去和
    生产的锁超时护栏赛跑:本机跑赢、CI 慢一点就跑输,`delete_group` 抛
    `LockNotAvailable` 而不是等到锁。改成直接观察 `pg_stat_activity`:轮询到那个后端
    确实处于 `wait_event_type='Lock'` 就立刻放行,持锁窗口因此是毫秒级、与机器快慢无关,
    而且比睡一秒**证明得更强**(睡够只证明「时间过去了」,轮询证明「它真的卡在锁上」)。
    观察连接刻意**绕开连接池**(池上限 2 已被两个线程占满,再取一条会卡在
    `pool_acquire_timeout`),用一条独立的 psycopg 连接只读 `pg_stat_activity`。

    刻意**不**在生产代码里把 `LockNotAvailable` 收敛成「组已不在」→ 404:①本用例里超时
    的是 `delete_group` 那一侧,在 approve 里捕获根本修不到这个失败;②更重要的是,把锁
    超时翻译成业务层的「找不到」会把真正的锁竞争静默吞掉 —— `lock_timeout` 正是部署用来
    暴露病理性争用的告警,让它变成一句「这条申请不存在」是拿掉体温计。
    """
    import threading

    import psycopg

    groups = core_stores.groups
    boss = core_stores.identity.create_user("t00112233", "password-72")
    librarian = core_stores.identity.create_user("t00445566", "password-73")
    group = groups.create_group(
        name="并发组", kind="project", description="", created_by=boss.id
    )
    groups.upsert_member(group["id"], librarian.id, role="member", added_by=boss.id)
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="并发库"), librarian.id
    )
    request = groups.create_share_request(
        notebook_id, group_id=group["id"], requested_by=librarian.id
    )

    parked = threading.Event()   # approve 已锁请求行、停在插入边之前
    release = threading.Event()  # 放行 approve 去插入边
    original_new_id = groups.new_id

    def blocking_new_id(prefix: str) -> str:
        # 只拦授权边 id(approve 的唯一 "gnt" 调用);其余原样放行。
        if prefix == "gnt":
            parked.set()
            assert release.wait(timeout=15), "approve 迟迟未被放行"
        return original_new_id(prefix)

    failures: list[BaseException] = []
    lock = threading.Lock()

    def do_approve():
        try:
            groups.approve_share_request(group["id"], request["id"], decided_by=boss.id)
        except BaseException as error:  # noqa: BLE001 — surfaced below
            with lock:
                failures.append(error)

    def do_delete():
        try:
            groups.delete_group(group["id"])
        except BaseException as error:  # noqa: BLE001 — surfaced below
            with lock:
                failures.append(error)

    def wait_until_a_backend_blocks_on_a_lock(deadline_seconds: float = 10.0) -> bool:
        """轮询到确有后端卡在锁等待上为止。绕开连接池(池上限 2 已被两个线程占满)。

        判据是 PostgreSQL 自己报的 `wait_event_type='Lock'`,不是「睡够了多久」——
        持锁窗口因此是毫秒级,与机器快慢无关,也就不会再和 `lock_timeout` 赛跑。
        """
        end = time.monotonic() + deadline_seconds
        with psycopg.connect(postgres_settings.database_url) as observer:
            while time.monotonic() < end:
                blocked = observer.execute(
                    "SELECT COUNT(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
                ).fetchone()[0]
                if blocked:
                    return True
                time.sleep(0.01)
        return False

    groups.new_id = blocking_new_id  # type: ignore[assignment]
    try:
        approve_thread = threading.Thread(target=do_approve)
        approve_thread.start()
        assert parked.wait(timeout=15), "approve 未在插入授权边之前停住"

        delete_thread = threading.Thread(target=do_delete)
        delete_thread.start()
        # delete_group 走到它的阻塞点:补锁后卡在 groups 行锁,删锁后已删完本组的边并卡在
        # group DELETE 的 CASCADE(请求行被 approve 的 FOR UPDATE 占住)。两种情形它都必然
        # 在**锁**上等待——观察到就立刻放行,绝不多持一毫秒。
        assert wait_until_a_backend_blocks_on_a_lock(), "delete_group 未进入锁等待,交错未成立"
        assert delete_thread.is_alive(), "delete_group 未进入预期的阻塞态,交错未成立"

        release.set()
        approve_thread.join(timeout=30)
        delete_thread.join(timeout=30)
        assert not approve_thread.is_alive() and not delete_thread.is_alive()
    finally:
        groups.new_id = original_new_id  # type: ignore[assignment]
        release.set()

    assert not failures, failures
    # 组已删除。
    assert groups.get_group(group["id"]) is None
    # 终态无孤儿边:那本库上不该再有指向已删组的授权边(补锁后 delete 会把 approve 刚写
    # 的边一并带走;删锁则会漏下一条 principal_kind="missing" 的孤儿边,这条断言即变红)。
    assert groups.list_grants(notebook_id) == []


def test_group_sharing_shows_up_in_the_owner_facing_projections(
    core_stores: CoreStores,
):
    """P1-T4 的两条 owner 视角投影必须与 SQLite 侧同义:

    * `summary_notebook_row` / `owned_notebook_rows` 的 `_shared_to_groups` 列
      ——「已分享」徽标的第二个来源;
    * `list_shared_by_owner` 的范围与 `group_count`——总览与徽标同一个判据。

    两个后端各写一条 SQL,而这两条查询共用中性层的同一份文本;这里钉的是「PG 上真的
    跑得通、且计数按组去重」——`group` 与 `group_admins` 是同一个组的两条边。
    """
    groups = core_stores.groups
    owner = core_stores.identity.create_user("s00123456", "password-49")
    group = groups.create_group(
        name="甲组", kind="project", description="", created_by=owner.id
    )
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="共享库"), owner.id
    )

    with core_stores.database.connect() as connection:
        row = core_stores.queries.summary_notebook_row(connection, notebook_id)
    assert bool(row["_shared_to_groups"]) is False
    assert core_stores.sharing.list_shared_by_owner(owner.id) == []

    for principal_type in ("group", "group_admins"):
        groups.create_grant(
            notebook_id,
            principal_type=principal_type,
            principal_id=group["id"],
            role="viewer" if principal_type == "group" else "admin",
            created_by=owner.id,
            admin_user_id=owner.id,
        )

    with core_stores.database.connect() as connection:
        row = core_stores.queries.summary_notebook_row(connection, notebook_id)
        owned = core_stores.queries.owned_notebook_rows(connection, owner.id)
    assert bool(row["_shared_to_groups"]) is True
    assert bool(owned[0]["_shared_to_groups"]) is True

    shared = core_stores.sharing.list_shared_by_owner(owner.id)
    assert [item["id"] for item in shared] == [notebook_id]
    assert shared[0]["share_token"] is None      # 只因群组共享而出现,没有分享链接
    assert shared[0]["group_count"] == 1         # 两条边、一个组

    # `user` / `everyone` 主体不算「共享给群组」:前者是只读共享(徽标那一半由
    # `notebooks.is_shared` 负责),后者是公共知识库的兼容映射。
    other_notebook = core_stores.notebooks.create_row(
        NotebookCreate(name="别的库"), owner.id
    )
    _pg_grant(core_stores, "gr-user-x", other_notebook, "user", owner.id, owner.id)
    _pg_grant(core_stores, "gr-all-x", other_notebook, "everyone", "", owner.id)
    assert [item["id"] for item in core_stores.sharing.list_shared_by_owner(owner.id)] == [
        notebook_id
    ]


def test_point_query_forms_of_the_two_list_projections(core_stores: CoreStores):
    """列表投影的两条查询都有**点查形态**(P1-T4:单库详情要按同一条谓词判来源)。

    刻意做成同一个方法的参数而不是另写两条 SQL:去重口径是「成员行优先」,详情与
    列表分叉了会让同一本库在卡片上和打开之后是两副面孔。这里钉的是 PG 侧那两条
    带过滤的语句真的跑得通、且只返回被点名的那本库。
    """
    groups = core_stores.groups
    owner = core_stores.identity.create_user("w00123456", "password-52")
    member = core_stores.identity.create_user("x00123456", "password-53")
    group = groups.create_group(
        name="甲组", kind="project", description="", created_by=owner.id
    )
    groups.upsert_member(group["id"], member.id, role="member", added_by=owner.id)

    granted = core_stores.notebooks.create_row(NotebookCreate(name="组里的库"), owner.id)
    joined = core_stores.notebooks.create_row(NotebookCreate(name="分享给我的"), owner.id)
    groups.create_grant(
        granted,
        principal_type="group",
        principal_id=group["id"],
        role="viewer",
        created_by=owner.id,
        admin_user_id=owner.id,
    )
    core_stores.sharing.add_member(joined, member.id)

    queries = core_stores.queries
    with core_stores.database.connect() as connection:
        all_granted = queries.granted_notebook_rows(connection, member.id)
        one_granted = queries.granted_notebook_rows(
            connection, member.id, notebook_id=granted
        )
        other_granted = queries.granted_notebook_rows(
            connection, member.id, notebook_id=joined
        )
        all_joined = queries.joined_notebook_rows(connection, member.id)
        one_joined = queries.joined_notebook_rows(
            connection, member.id, notebook_id=joined
        )
        other_joined = queries.joined_notebook_rows(
            connection, member.id, notebook_id=granted
        )

    assert [row["id"] for row in all_granted] == [granted]
    assert [row["id"] for row in one_granted] == [granted]
    assert other_granted == []            # 那本库不是经群组来的
    assert [row["id"] for row in all_joined] == [joined]
    assert [row["id"] for row in one_joined] == [joined]
    assert other_joined == []             # 那本库没有成员行

    # P2-T2:两条形态都必须带回**边自己的 role**(`can_manage_content` 的派生源,
    # 零新增查询)。少这一列不会报错——`notebook_grant_confers_admin` 读不到键时按
    # False 收,于是 PG 部署的组管理员静默看不到任何写入口,而 SQLite 部署照常。
    from app.repositories.group_rows import notebook_grant_confers_admin

    for row in (*all_granted, *one_granted):
        assert row["_grant_role"] == "viewer"
        assert notebook_grant_confers_admin(row) is False

    groups.create_grant(
        granted,
        principal_type="group_admins",
        principal_id=group["id"],
        role="admin",
        created_by=owner.id,
        admin_user_id=owner.id,
    )
    groups.upsert_member(group["id"], member.id, role="admin", added_by=owner.id)
    with core_stores.database.connect() as connection:
        rows = queries.granted_notebook_rows(connection, member.id, notebook_id=granted)
    assert sorted(row["_grant_role"] for row in rows) == ["admin", "viewer"]
    assert any(notebook_grant_confers_admin(row) for row in rows)


def test_notebook_row_reports_the_share_token_without_minting_one(
    core_stores: CoreStores,
):
    """`share_state`(只读的分享状态)读的就是这一行,所以它必须如实三态。

    ⚠ 这条钉的是**存储原语**,不是那个服务方法(`CoreStores` 里只有 store)。服务层
    的「打开弹窗不铸 token」由 `tests/test_group_routes.py` 的
    `test_reading_share_state_never_mints_a_token` 走 HTTP 钉住。这里补的是 PG 侧
    「没开过分享 → NULL、开过 → 那个 token、撤销 → 回到 NULL」的对等性。
    """
    owner = core_stores.identity.create_user("y00123456", "password-54")
    notebook_id = core_stores.notebooks.create_row(NotebookCreate(name="库"), owner.id)

    fresh = core_stores.sharing.notebook_row(notebook_id)
    assert fresh["share_token"] is None and not fresh["is_shared"]

    minted = core_stores.sharing.set_share_token(notebook_id, "shr-fixed")
    opened = core_stores.sharing.notebook_row(notebook_id)
    assert opened["share_token"] == minted and opened["is_shared"]

    core_stores.sharing.clear_share(notebook_id)
    assert core_stores.sharing.notebook_row(notebook_id)["share_token"] is None


def test_mountable_candidates_report_where_they_come_from(core_stores: CoreStores):
    """挂载候选的 `origin` 投影(P1-T4)必须与 SQLite 侧同一套判据与优先级。

    优先级 base → mine → shared:自己 owner 的公共知识库仍判 `base`,让本字段出现
    之前前端按 `tier` 分组的结果逐字不变。
    """
    groups = core_stores.groups
    owner = core_stores.identity.create_user("t00123456", "password-50")
    other = core_stores.identity.create_user("v00123456", "password-51")

    mine = core_stores.notebooks.create_row(NotebookCreate(name="我的库"), owner.id)
    public = core_stores.notebooks.create_row(NotebookCreate(name="公共库"), owner.id)
    core_stores.notebooks.set_tier(public, "base")

    group = groups.create_group(
        name="他的组", kind="project", description="", created_by=other.id
    )
    groups.upsert_member(group["id"], owner.id, role="member", added_by=other.id)
    borrowed = core_stores.notebooks.create_row(NotebookCreate(name="他的库"), other.id)
    groups.create_grant(
        borrowed,
        principal_type="group",
        principal_id=group["id"],
        role="viewer",
        created_by=other.id,
        admin_user_id=other.id,
    )

    target = core_stores.notebooks.create_row(NotebookCreate(name="挂载方"), owner.id)
    candidates = core_stores.notebooks.mountable_for_notebook(target)
    origins = {item["id"]: item["origin"] for item in candidates}
    assert origins[public] == "base"
    assert origins[mine] == "mine"
    assert origins[borrowed] == "shared"


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
        admin_user_id=owner.id,
    )
    groups.create_grant(
        admin_notebook,
        principal_type="group_admins",
        principal_id=group["id"],
        role="admin",
        created_by=owner.id,
        admin_user_id=owner.id,
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

    assert PostgresMigrator(postgres_database).migrate() == 48
    local_zone = ZoneInfo("America/Los_Angeles")
    naive_local = datetime(2026, 7, 22, 3, 0, 0)
    expected_utc = naive_local.replace(tzinfo=local_zone).astimezone(timezone.utc)
    new_id = _new_id_factory()

    def clock() -> str:
        return naive_local.isoformat()

    identity = PostgresIdentityStore(postgres_database, postgres_settings)
    notebooks = PostgresNotebookStore(
        postgres_database,
        new_id=new_id,
        now=clock,
        activity_retention_days=180,
    )
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

    assert PostgresMigrator(postgres_database).migrate() == 48
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
    notebooks = PostgresNotebookStore(
        postgres_database,
        new_id=new_id,
        now=clock,
        activity_retention_days=180,
    )
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

    assert PostgresMigrator(postgres_database).migrate() == 48
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
        activity_retention_days=180,
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
        activity_retention_days=core_stores.notebooks.activity_retention_days,
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
        activity_retention_days=core_stores.notebooks.activity_retention_days,
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
    # so it cannot accidentally cross the raw-row summary boundary.  ``group_count``
    # (group knowledge sharing P1-T4) is a derived integer, not a notebook column,
    # so it stays on the safe side of that boundary too.
    shared_rows = core_stores.sharing.list_shared_by_owner(owner.id)
    assert set(shared_rows[0].keys()) == {"id", "name", "share_token", "group_count"}
    assert shared_rows[0]["group_count"] == 0


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


def _capacity_insert_kwargs(notebook_id: str, source_id: str, digest: str) -> dict:
    return dict(
        source_id=source_id,
        notebook_id=notebook_id,
        digest=digest,
        title=source_id,
        source_type="markdown",
        status="queued",
        parse_status="queued",
        file_name=f"{source_id}.md",
        file_path="",
        file_size=1,
        summary="",
        doc_type="",
        capacity_limit=1,
    )


def test_document_capacity_gate_matches_sqlite_semantics(core_stores: CoreStores):
    """事务内文档容量闸(PR #584 codex R6)的 PG twin,与 SQLite 语义逐条对齐
    (SQLite 侧在 tests/test_document_limit_atomicity.py):

    * ``capacity_limit`` 满则拒(异常带事务内 current/limit),不插行;
    * 去重重查在容量闸之前——满库重传相同字节复用既有行,绝不被上限拒绝;
    * ``insert_source`` 自有事务分支同一把闸(URL 路径);joined connection +
      capacity_limit 响亮拒绝(ValueError);
    * 行锁找不到 notebook 行 → KeyError(与 get_row 同一个 not-found 信号)。
    """
    owner = core_stores.identity.create_user("k00123500", "password-13")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Capacity"), owner.id
    )

    assert core_stores.sources.insert_source_if_absent(
        **_capacity_insert_kwargs(notebook_id, "src-cap-a", "digest-cap-a")
    ) is None                                            # 0 < 1:入库
    with pytest.raises(DocumentCapacityExceeded) as exc:
        core_stores.sources.insert_source_if_absent(
            **_capacity_insert_kwargs(notebook_id, "src-cap-b", "digest-cap-b")
        )                                                # 1 >= 1:事务内拒绝
    assert exc.value.current == 1 and exc.value.limit == 1
    assert core_stores.sources.visible_document_count(notebook_id) == 1

    # 判序:满库 + 相同 digest → 复用,不抛不插。
    assert core_stores.sources.insert_source_if_absent(
        **_capacity_insert_kwargs(notebook_id, "src-cap-retry", "digest-cap-a")
    ) == "src-cap-a"

    # insert_source(自有事务,URL 路径)同一把闸。
    with pytest.raises(DocumentCapacityExceeded):
        core_stores.sources.insert_source(
            source_id="src-cap-url", notebook_id=notebook_id, title="u",
            source_type="pdf", status="queued", parse_status="queued",
            file_name="u.pdf", file_path="", file_size=0, file_hash="",
            summary="", doc_type="", capacity_limit=1,
        )
    core_stores.sources.insert_source(
        source_id="src-cap-url2", notebook_id=notebook_id, title="u2",
        source_type="pdf", status="queued", parse_status="queued",
        file_name="u2.pdf", file_path="", file_size=0, file_hash="",
        summary="", doc_type="", capacity_limit=2,       # 1 < 2:放行
    )
    assert core_stores.sources.visible_document_count(notebook_id) == 2

    # joined connection + capacity_limit:闸保证不了原子性,必须响亮拒绝。
    with core_stores.database.write() as connection:
        with pytest.raises(ValueError, match="owned write transaction"):
            core_stores.sources.insert_source(
                source_id="src-cap-x", notebook_id=notebook_id, title="x",
                source_type="pdf", status="queued", parse_status="queued",
                file_name="x.pdf", file_path="", file_size=0, file_hash="",
                summary="", doc_type="", connection=connection, capacity_limit=1,
            )

    # 行锁找不到 notebook 行 → KeyError。⚠ **PG 专属**的顺带防御,不是跨后端
    # parity 契约:行锁本来就要读该行,不在就当场按 not-found 收(路由翻成既有
    # 404);SQLite 侧无行锁、刻意不加存在性探针(同场景走到 INSERT 撞外键)。
    # 生产上两侧都在路由层 get_row/能力守卫处就已 404,这条深防路径不可达——
    # 已在 _lock_notebook_row_for_capacity docstring 登记为后端差异。
    with pytest.raises(KeyError):
        core_stores.sources.insert_source_if_absent(
            **_capacity_insert_kwargs("nb-missing", "src-cap-m", "digest-cap-m")
        )


def test_document_capacity_gate_serializes_concurrent_creators(
    core_stores: CoreStores,
):
    """并发正身:PG 的 ``write()`` 没有进程级锁(READ COMMITTED 下两个事务各自
    COUNT 都能看到「还剩 1」),串行化只能来自 notebook 行锁(FOR NO KEY UPDATE)。
    Barrier 让两个线程同时发起、各持一条池连接;赢家提交后输家的 COUNT 才跑,
    每局恰有一个入库。

    ⚠ 检出力说明(codex 评审 P2):Barrier 在调用入口,BEGIN/加锁在其后——对
    「删掉行锁」的变异,单局是**概率性**检出(输家的 COUNT 要恰好落进赢家
    BEGIN→COMMIT 的窗口才会双插;实测该变异单局即红,但没有保证)。所以按局
    重复、每局全新 notebook,任一局出现两行即红,把漏检压到可忽略;未变异代码
    上每局都由行锁保证确定通过,无 sleep、无时序假设。"""
    from concurrent.futures import ThreadPoolExecutor

    owner = core_stores.identity.create_user("k00123501", "password-14")
    for round_no in range(6):
        notebook_id = core_stores.notebooks.create_row(
            NotebookCreate(name=f"Capacity race {round_no}"), owner.id
        )
        barrier = threading.Barrier(2, timeout=10)
        refused: list[DocumentCapacityExceeded] = []
        inserted: list[str] = []
        lock = threading.Lock()

        def create(source_id: str, digest: str) -> None:
            barrier.wait()
            try:
                outcome = core_stores.sources.insert_source_if_absent(
                    **_capacity_insert_kwargs(notebook_id, source_id, digest)
                )
            except DocumentCapacityExceeded as exc:
                with lock:
                    refused.append(exc)
                return
            assert outcome is None
            with lock:
                inserted.append(source_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(create, f"src-race-a{round_no}", f"digest-race-a{round_no}"),
                pool.submit(create, f"src-race-b{round_no}", f"digest-race-b{round_no}"),
            ]
            for future in futures:
                future.result(timeout=30)

        assert len(inserted) == 1, (
            f"第 {round_no} 局:只剩 1 个名额必须恰好放行一个,实际 {inserted}"
        )
        assert len(refused) == 1
        assert refused[0].current == 1 and refused[0].limit == 1
        assert core_stores.sources.visible_document_count(notebook_id) == 1


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
    resolved = reports.public_report_by_token(issued[0])
    assert resolved is not None
    # 匿名路由要用 notebook_id/created_by 实时复核创建者的读权(P1-T3b 裁决),
    # 所以 token 查询必须在 PG 侧也把这两列带回来——它们是**服务端**闸的输入,
    # 不进 `public_report_payload` 的白名单。列名拼错只会在这一侧暴露。
    assert resolved["notebook_id"] == notebook_id
    assert resolved["created_by"] == owner.id
    # 未 done 与未知 token 仍不可区分。
    assert reports.public_report_by_token("rshr-never-issued") is None


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


def test_report_completion_cas_cannot_overwrite_a_cancelled_generation(
    core_stores: CoreStores,
):
    from app.repositories.postgres.report_store import ReportStore

    owner = core_stores.identity.create_user("r00876543", "password-12")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Report CAS"), owner.id
    )
    reports = ReportStore(
        core_stores.database,
        new_id=_new_id_factory(),
        now=lambda: NOW,
        current_user_id=lambda: owner.id,
    )
    report_id = reports.create_report(notebook_id, "为什么?", 2)
    reports.update_report(notebook_id, report_id, status="outline_ready")
    assert reports.claim_report_generation(notebook_id, report_id)
    assert reports.cancel_report(notebook_id, report_id)
    assert reports.complete_report_generation(
        notebook_id,
        report_id,
        sections=[{"markdown": "must not persist"}],
        content_md="must not persist",
        gaps=[],
        references=[],
    ) is False
    row = reports.get_report(notebook_id, report_id)
    assert row["status"] == "cancelled"
    assert row["content_md"] == ""


def test_report_cancel_cannot_overwrite_a_completed_generation(
    core_stores: CoreStores,
):
    from app.repositories.postgres.report_store import ReportStore

    owner = core_stores.identity.create_user("r00876544", "password-12")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Report reverse CAS"), owner.id
    )
    reports = ReportStore(
        core_stores.database,
        new_id=_new_id_factory(),
        now=lambda: NOW,
        current_user_id=lambda: owner.id,
    )
    report_id = reports.create_report(notebook_id, "为什么?", 2)
    reports.update_report(notebook_id, report_id, status="outline_ready")
    assert reports.claim_report_generation(notebook_id, report_id)
    assert reports.complete_report_generation(
        notebook_id,
        report_id,
        sections=[{"markdown": "durable"}],
        content_md="durable",
        gaps=[],
        references=[],
    )
    assert reports.cancel_report(notebook_id, report_id) is False
    row = reports.get_report(notebook_id, report_id)
    assert row["status"] == "done"
    assert row["content_md"] == "durable"


def test_report_generation_claim_atomically_refreshes_retry_scope_on_postgres(
    core_stores: CoreStores,
):
    from app.repositories.postgres.report_store import ReportStore

    owner = core_stores.identity.create_user("r00876545", "password-12")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="Report retry scope CAS"), owner.id
    )
    reports = ReportStore(
        core_stores.database,
        new_id=_new_id_factory(),
        now=lambda: NOW,
        current_user_id=lambda: owner.id,
    )
    report_id = reports.create_report(notebook_id, "为什么?", 2)
    reports.update_report(
        notebook_id,
        report_id,
        status="failed",
        understanding={
            "source_scope": {"source_ids": ["old"], "narrowed": False},
            "credibility": {"anchor_count": 3},
        },
        sections=[{"markdown": "stale"}],
        content_md="stale",
        references=[{"id": "stale"}],
    )

    refreshed = {
        "source_scope": {"source_ids": ["fresh"], "narrowed": True},
        "credibility": {"anchor_count": 99},
    }
    assert reports.claim_report_generation(
        notebook_id, report_id, refreshed
    ) is True
    claimed = reports.get_report(notebook_id, report_id)
    assert claimed["status"] == "generating"
    assert claimed["understanding"] == {
        "source_scope": {"source_ids": ["fresh"], "narrowed": True}
    }
    assert claimed["generation_started_at"]
    assert claimed["sections"] == []
    assert claimed["content_md"] == ""
    assert claimed["references"] == []

    assert reports.claim_report_generation(
        notebook_id,
        report_id,
        {"source_scope": {"source_ids": ["must-not-persist"]}},
    ) is False
    after_lost_cas = reports.get_report(notebook_id, report_id)
    assert after_lost_cas["understanding"] == claimed["understanding"]
    assert after_lost_cas["generation_started_at"] == claimed[
        "generation_started_at"
    ]


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
        notebook_id,
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
    # Current writers reject a source/notebook mismatch. Corrupt the row only
    # after that valid write to model legacy data without weakening the writer.
    with core_stores.database.write() as connection:
        connection.execute(
            "UPDATE source_paper_meta SET notebook_id=%s WHERE source_id=%s",
            (other_notebook_id, "profile-corrupt-meta"),
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


def _seed_kg_extracted_matrix(core_stores: CoreStores, notebook_id: str) -> None:
    """Seed the SHARED kg_extracted decision matrix (``tests/
    kg_extracted_parity_cases.py``) — one source per case, all in ONE notebook
    so the batched hydration really runs its "a page of many ids" shape."""
    base = datetime.fromisoformat(NOW)
    for index, (_label, has_object, runs, _expected) in enumerate(KG_EXTRACTED_CASES):
        source_id = kg_case_source_id(index)
        core_stores.sources.insert_source(
            source_id=source_id,
            notebook_id=notebook_id,
            title=source_id,
            source_type="markdown",
            status="extracted",
            parse_status="extracted",
            file_name=f"{source_id}.md",
            file_path=f"uploads/{source_id}.md",
            file_size=1,
            file_hash=f"hash-{index}",
            summary="",
            doc_type="",
        )
        if has_object:
            _write_sql(
                core_stores,
                "INSERT INTO knowledge_objects"
                "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,"
                "created_at,updated_at) VALUES "
                "(%s,%s,'concept','approved','','{}'::jsonb,'[]'::jsonb,%s,%s,%s)",
                (f"ko-kgx-{index:02d}", notebook_id, source_id, NOW, NOW),
            )
        # enumerate() 的 position 是这条 run 在元组里的插入序(也是 run id 后缀);
        # rank 只用来生成 created_at 字面量,可以在多条 run 间重复(同刻场景)——两个
        # 职责刻意分开,理由见 kg_extracted_parity_cases.py 模块 docstring。
        for position, (rank, status, error) in enumerate(runs):
            _write_sql(
                core_stores,
                "INSERT INTO extraction_runs"
                "(id,notebook_id,source_id,run_type,status,error_message,"
                "created_at,updated_at) VALUES (%s,%s,%s,'kg',%s,%s,%s,%s)",
                (
                    kg_case_run_id(index, position),
                    notebook_id,
                    source_id,
                    status,
                    error,
                    (base + timedelta(hours=rank)).isoformat(),
                    NOW,
                ),
            )


def test_kg_extracted_matrix_matches_the_shared_parity_table(core_stores: CoreStores):
    """``kg_extracted`` 的每一条判定分支,与 SQLite 侧吃同一张表。

    SQLite twin: ``tests/test_sources_page_batched.py`` 的
    ``test_batched_kg_extracted_matches_the_parity_matrix`` /
    ``test_single_row_kg_extracted_matches_the_parity_matrix``。两端的判据是各自
    手抄的方言 SQL(PG 正则 + ``strpos`` + ``ordinal`` tie-break,SQLite ``GLOB`` +
    ``instr`` + ``rowid``),共用同一张用例表才能让任一端漏掉某个分支时变红。

    批量路径(list_sources / list_sources_page)与单条路径(get_source)分别断言:
    这两处各有一份取数 SQL,漂了会让同一份来源在列表和详情里显示成两种状态。"""
    owner = core_stores.identity.create_user("k00123502", "password-17")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="KG matrix"), owner.id
    )
    _seed_kg_extracted_matrix(core_stores, notebook_id)

    expected = {label: value for label, _obj, _runs, value in KG_EXTRACTED_CASES}
    by_id = {item.id: item for item in core_stores.sources.list_sources(notebook_id)}
    assert {
        label: by_id[kg_case_source_id(index)].kg_extracted
        for index, (label, _obj, _runs, _value) in enumerate(KG_EXTRACTED_CASES)
    } == expected

    page = core_stores.sources.list_sources_page(notebook_id, offset=0, limit=50)
    assert {item.id: item.kg_extracted for item in page.items} == {
        kg_case_source_id(index): value
        for index, (_label, _obj, _runs, value) in enumerate(KG_EXTRACTED_CASES)
    }

    assert {
        label: core_stores.sources.get_source(kg_case_source_id(index)).kg_extracted
        for index, (label, _obj, _runs, _value) in enumerate(KG_EXTRACTED_CASES)
    } == expected


def test_kg_extracted_batch_query_is_driven_by_page_source_ids(
    core_stores: CoreStores,
):
    """Shape guard for 95d1268's rewrite: nothing else in this file has power
    to catch a regression back to the pre-rewrite shape, because the whole
    point of that rewrite was that both shapes give the SAME answer — the
    parity matrix above only checks output.

    ``sources_from_rows``'s kg_extracted judgement must be driven by the
    PAGE's own source ids (``WITH page_sources(source_id) AS (VALUES ...)``),
    never by scanning ``knowledge_objects`` and folding back to ``source_id``
    with DISTINCT — the shape that cost 3650ms / 1.01M subquery executions on
    the 49k-source fleet (see the rationale comment next to the query in
    ``postgres/source_store.py``). This checks the *query text* the adapter
    actually issues (captured with the same spy
    ``test_knowledge_store_conformance.py``'s plan tests use, so a hand-copied
    SQL string cannot drift out of sync with the real one) and the *plan*
    EXPLAIN produces for it.

    SQLite twin: ``test_sources_page_batched.py``'s
    ``test_kg_extracted_batch_query_is_driven_by_page_source_ids``.
    """
    owner = core_stores.identity.create_user("k00123503", "password-18")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="KG shape"), owner.id
    )
    # Two source ids, not one: a single-row VALUES CTE gets constant-folded to
    # a plain ``Result`` node (no ``Values Scan``) — the assertions below need
    # a real multi-row driver, matching production's "page of many ids" shape.
    for suffix in ("a", "b"):
        source_id = f"src-kgshape-{suffix}"
        core_stores.sources.insert_source(
            source_id=source_id,
            notebook_id=notebook_id,
            title=f"KG shape {suffix}",
            source_type="markdown",
            status="extracted",
            parse_status="extracted",
            file_name=f"kgshape-{suffix}.md",
            file_path=f"uploads/kgshape-{suffix}.md",
            file_size=1,
            file_hash=f"hash-kgshape-{suffix}",
            summary="",
            doc_type="",
        )
        _write_sql(
            core_stores,
            "INSERT INTO knowledge_objects"
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,"
            "created_at,updated_at) VALUES "
            "(%s,%s,'concept','approved','','{}'::jsonb,'[]'::jsonb,%s,%s,%s)",
            (f"ko-kgshape-{suffix}", notebook_id, source_id, NOW, NOW),
        )

    with core_stores.database.connect() as connection:
        connection.execute("SET LOCAL enable_seqscan=off")
        rows = connection.execute(
            "SELECT * FROM sources WHERE notebook_id=%s", (notebook_id,)
        ).fetchall()

        captured: list[tuple[str, object]] = []
        original_execute = connection.execute

        def spying_execute(sql, params=None, **kwargs):
            text = str(sql)
            if "knowledge_objects" in text:
                captured.append((text, params))
            return original_execute(sql, params, **kwargs)

        connection.execute = spying_execute
        try:
            core_stores.sources.sources_from_rows(connection, rows)
        finally:
            del connection.execute

        assert captured, (
            "sources_from_rows must issue a kg_extracted judgement query "
            "touching knowledge_objects")
        # 恰好一条含 knowledge_objects 的语句——若未来在它之前/之后再插入一条
        # 也含 knowledge_objects 的语句,``captured[0]`` 会静默指向错误的那条,
        # 后面对 captured_sql 的所有断言就名不副实。
        assert len(captured) == 1, (
            f"expected exactly one knowledge_objects-touching statement, "
            f"got {len(captured)}:\n" + "\n---\n".join(sql for sql, _ in captured)
        )
        captured_sql, captured_params = captured[0]
        assert "WITH page_sources(source_id) AS (VALUES" in captured_sql, (
            f"kg_extracted must be driven by the page's own source ids via a "
            f"VALUES CTE, not by scanning knowledge_objects, got:\n{captured_sql}"
        )
        assert "SELECT source_id FROM page_sources" in captured_sql, (
            f"the outer driving FROM must be page_sources, not knowledge_objects "
            f"(knowledge_objects may still appear inside the EXISTS semi-join "
            f"target), got:\n{captured_sql}"
        )
        assert "DISTINCT" not in captured_sql, (
            f"the old shape folded knowledge_objects rows back to source_id with "
            f"DISTINCT — the VALUES-CTE driver needs no DISTINCT at all, "
            f"got:\n{captured_sql}"
        )

        plan_text = "\n".join(
            str(row["QUERY PLAN"]) for row in connection.execute(
                f"EXPLAIN (COSTS OFF) {captured_sql}", captured_params
            ).fetchall()
        )

    assert "Values Scan" in plan_text, (
        f"expected the page ids to drive the plan via a Values Scan, got:\n{plan_text}"
    )
    assert "Nested Loop Semi Join" in plan_text, (
        f"expected the knowledge_objects EXISTS check to be a semi join off "
        f"the page's own ids, got:\n{plan_text}"
    )
    assert "Unique" not in plan_text, (
        f"a top-level DISTINCT/Unique over knowledge_objects is the OLD "
        f"knowledge_objects-driven shape this replaced, got:\n{plan_text}"
    )


def test_kg_extracted_batch_tie_break_survives_a_forced_sort_plan(
    core_stores: CoreStores,
):
    """The default plan does not exercise the ``,r.ordinal DESC`` tie-break at
    all — a reverse index scan over ``idx_extraction_runs_source_created``
    (``source_id,created_at``) happens to finish ties on the same
    ``created_at`` in heap-TID order, which for a freshly inserted table is
    the same order as insertion (== ``ordinal``). Deleting the tie-break
    clause therefore does NOT turn this file's default-plan tests red (see
    the case (h) rationale comment in ``kg_extracted_parity_cases.py``).

    This test forces the OTHER plan shape — ``SET LOCAL
    enable_indexscan=off; enable_bitmapscan=off`` inside the transaction the
    read connection already runs in (autocommit is off, mirroring the shape
    guard above at ~L4308) — so the planner has no index path left and falls
    back to Seq Scan + Sort, where the tie-break can only come from the
    explicit ``ORDER BY ...,r.ordinal DESC`` clause itself, not from any
    incidental scan order. It must run ``sources_from_rows`` on that SAME
    self-owned connection (not ``core_stores.sources.list_sources``, which
    opens its own connection and would run under the default plan again).
    """
    owner = core_stores.identity.create_user("k00123505", "password-19")
    notebook_id = core_stores.notebooks.create_row(
        NotebookCreate(name="KG tie-break sort plan"), owner.id
    )
    core_stores.sources.insert_source(
        source_id="src-kg-tiebreak",
        notebook_id=notebook_id,
        title="Tie-break",
        source_type="markdown",
        status="extracted",
        parse_status="extracted",
        file_name="tiebreak.md",
        file_path="uploads/tiebreak.md",
        file_size=1,
        file_hash="hash-tiebreak",
        summary="",
        doc_type="",
    )
    _write_sql(
        core_stores,
        "INSERT INTO knowledge_objects"
        "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,"
        "created_at,updated_at) VALUES "
        "(%s,%s,'concept','approved','','{}'::jsonb,'[]'::jsonb,%s,%s,%s)",
        ("ko-kg-tiebreak", notebook_id, "src-kg-tiebreak", NOW, NOW),
    )
    # 同一个 created_at:先插入干净的 completed,后插入 failed。tie-break 应选
    # 后插入的那条(与 kg_extracted_parity_cases.py 用例 (h) 同一个方向),
    # 覆盖判定必须为 False。
    for run_id, status, error in (
        ("run-tiebreak-0", "completed", "kg objects=3"),
        ("run-tiebreak-1", "failed", "RuntimeError: upstream timeout"),
    ):
        _write_sql(
            core_stores,
            "INSERT INTO extraction_runs"
            "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
            "VALUES (%s,%s,%s,'kg',%s,%s,%s,%s)",
            (run_id, notebook_id, "src-kg-tiebreak", status, error, NOW, NOW),
        )

    with core_stores.database.connect() as connection:
        connection.execute("SET LOCAL enable_indexscan=off")
        connection.execute("SET LOCAL enable_bitmapscan=off")
        rows = connection.execute(
            "SELECT * FROM sources WHERE id=%s", ("src-kg-tiebreak",)
        ).fetchall()
        result = core_stores.sources.sources_from_rows(connection, rows)

    assert result[0].kg_extracted is False, (
        "under a forced Seq Scan + Sort plan, the same-created_at tie between "
        "a completed run and a later-inserted failed run must resolve to the "
        "LATER-INSERTED (failed) run via the explicit ordinal DESC tie-break, "
        "not by way of an incidental index scan order"
    )


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


# ---------------------------------------------------------------------------
# ``list_sources_page`` 的 q 过滤谓词:四腿语义(title / file_name / 作者名 /
# 论文标题)。
#
# 这组用例先在**旧的 OR-跨表 EXISTS 形态**上跑通,再换成 id 半连接三腿 UNION
# 形态(见 postgres/source_store.py:list_sources_page 里的等价论证注释),所以
# 它们是那次改写的「语义不变」证明,而不是新形态的事后描述。计划形状的守卫
# (真的走上新的三条 GIN trgm 索引、真的发出 UNION 形状的 SQL)在
# ``tests/postgres/test_hotpath_indexes_batch4_live.py``;这里只管语义。
# SQLite 孪生:``tests/test_sources_pagination.py`` 的同名一组。
# ---------------------------------------------------------------------------


def _seed_search_source(
    stores: CoreStores, notebook_id: str, source_id: str, title: str,
    file_name: str, created: str, source_type: str = "markdown",
) -> None:
    """隐藏类型(memory/knowhow)要用原始 SQL 落行:``insert_source`` 的容量闸与
    Memory 投影写路径各有自己的前置条件,而这组用例要的只是「表里有这样一行」。
    ``created`` 逐条递增,让下面的顺序断言吃 ``(created_at, id)`` 主键而不是
    并列时间戳下的 id tie-break —— 与 SQLite 孪生的播种顺序逐条对齐。"""
    _write_sql(
        stores,
        "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
        "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
        "VALUES (%s,%s,%s,%s,'extracted','extracted',%s,%s,1,%s,'','',%s,%s)",
        (
            source_id, notebook_id, title, source_type, file_name,
            f"uploads/{file_name}", f"hash-{source_id}", created, NOW,
        ),
    )


def _seed_search_paper_meta(
    stores: CoreStores, notebook_id: str, source_id: str,
    paper_title: "str | None" = None, authors: "tuple[str, ...]" = (),
) -> None:
    _write_sql(
        stores,
        "INSERT INTO source_paper_meta(source_id,notebook_id,is_paper,paper_title,"
        "venue,pub_year,doi,keywords,raw_json,model,created_at,updated_at) "
        "VALUES (%s,%s,1,%s,'',NULL,'','[]'::jsonb,'{}'::jsonb,'',%s,%s)",
        (source_id, notebook_id, paper_title, NOW, NOW),
    )
    for position, name in enumerate(authors):
        _write_sql(
            stores,
            "INSERT INTO source_authors(id,source_id,notebook_id,position,name,"
            "affiliation,created_at) VALUES (%s,%s,%s,%s,%s,'',%s)",
            (
                f"{source_id}:auth:{position:03d}", source_id, notebook_id,
                position, name, NOW,
            ),
        )


def _search_ids(stores: CoreStores, notebook_id: str, needle) -> list[str]:
    page = stores.sources.list_sources_page(
        notebook_id, offset=0, limit=200, q=needle
    )
    ids = [item.id for item in page.items]
    # COUNT 与页查询共用同一份 where 片段;两者分叉会让「共 N 篇」与实际列出的
    # 行数对不上。
    assert page.total_count == len(ids), (needle, page.total_count, ids)
    return ids


def _seed_search_fixture(stores: CoreStores) -> "tuple[str, str]":
    owner = stores.identity.create_user("q00123456", "password-19")
    notebook_id = stores.notebooks.create_row(
        NotebookCreate(name="Search target"), owner.id
    )
    other_id = stores.notebooks.create_row(
        NotebookCreate(name="Search other"), owner.id
    )
    day = "2026-07-22T10:00:0{}+00:00".format
    _seed_search_source(
        stores, notebook_id, "s-title", "Needle Voltage Reference", "vref.md", day(0)
    )
    _seed_search_source(
        stores, notebook_id, "s-file", "Untitled import", "needle-doc.md", day(1)
    )
    _seed_search_source(
        stores, notebook_id, "s-author", "Anonymous report", "anon.pdf", day(2)
    )
    _seed_search_paper_meta(
        stores, notebook_id, "s-author", paper_title="Unrelated title",
        authors=("Zeta Needleman",),
    )
    _seed_search_source(
        stores, notebook_id, "s-ptitle", "Scanned upload", "scan.pdf", day(3)
    )
    _seed_search_paper_meta(
        stores, notebook_id, "s-ptitle", paper_title="Needle in a Haystack"
    )
    _seed_search_source(
        stores, notebook_id, "s-multi", "Needle everywhere", "needle-multi.md", day(4)
    )
    _seed_search_paper_meta(
        stores, notebook_id, "s-multi", paper_title="Needle title",
        authors=("Needle Author",),
    )
    _seed_search_source(
        stores, notebook_id, "s-miss", "Nothing to see", "plain.md", day(5)
    )
    _seed_search_source(
        stores, notebook_id, "s-memory", "Needle memory projection", "mem.md", day(6),
        source_type="memory",
    )
    _seed_search_paper_meta(
        stores, notebook_id, "s-memory", paper_title="Needle memory paper",
        authors=("Needle Ghost",),
    )
    _seed_search_source(
        stores, notebook_id, "s-knowhow", "Needle knowhow projection", "kh.md", day(7),
        source_type="knowhow",
    )
    _seed_search_paper_meta(
        stores, notebook_id, "s-knowhow", paper_title="Needle knowhow paper",
        authors=("Needle Ghost",),
    )
    _seed_search_source(
        stores, other_id, "s-other", "Needle everywhere", "needle-multi.md", day(0)
    )
    _seed_search_paper_meta(
        stores, other_id, "s-other", paper_title="Needle title",
        authors=("Needle Author",),
    )
    return notebook_id, other_id


def test_source_search_matches_every_leg(core_stores: CoreStores):
    notebook_id, _other = _seed_search_fixture(core_stores)
    assert _search_ids(core_stores, notebook_id, "voltage") == ["s-title"]
    assert _search_ids(core_stores, notebook_id, "vref.md") == ["s-title"]
    assert _search_ids(core_stores, notebook_id, "zeta needleman") == ["s-author"]
    assert _search_ids(core_stores, notebook_id, "haystack") == ["s-ptitle"]


def test_source_search_counts_a_multi_leg_hit_exactly_once(core_stores: CoreStores):
    """``s-multi`` 四条腿同时命中。旧形态是一个布尔 OR,天然只算一次;新形态是
    三腿 UNION 再 id 半连接,靠 UNION 去重 + ``IN`` 半连接语义保证同一件事 ——
    把 ``UNION`` 写成 ``UNION ALL`` 再 JOIN 回来就会在这里变红。"""
    notebook_id, _other = _seed_search_fixture(core_stores)
    assert _search_ids(core_stores, notebook_id, "needle-multi") == ["s-multi"]
    ids = _search_ids(core_stores, notebook_id, "needle")
    assert ids.count("s-multi") == 1
    assert ids == ["s-title", "s-file", "s-author", "s-ptitle", "s-multi"]


def test_source_search_excludes_hidden_source_types(core_stores: CoreStores):
    """memory / knowhow 合成源即使 title、论文标题、作者名全部命中也不能出现,
    更不能进 total_count。新形态里 UNION 第一腿自己也带 visible 谓词(既是
    partial 索引可用的前提,也让三腿的并集不含隐藏源),外层交集不变。"""
    notebook_id, _other = _seed_search_fixture(core_stores)
    ids = _search_ids(core_stores, notebook_id, "needle")
    assert "s-memory" not in ids and "s-knowhow" not in ids
    assert _search_ids(core_stores, notebook_id, "projection") == []
    assert _search_ids(core_stores, notebook_id, "needle ghost") == []


def test_source_search_never_leaks_another_notebook(core_stores: CoreStores):
    """干扰库里有逐字同名的 title / file_name / 作者名 / 论文标题,四条腿都必须
    留在本库内。"""
    notebook_id, other_id = _seed_search_fixture(core_stores)
    for needle in ("needle", "needle author", "needle title", "needle-multi"):
        assert "s-other" not in _search_ids(core_stores, notebook_id, needle), needle
    assert _search_ids(core_stores, other_id, "needle") == ["s-other"]
    assert _search_ids(core_stores, other_id, "haystack") == []


def test_source_search_ignores_a_legacy_cross_notebook_child_row(
    core_stores: CoreStores,
):
    """**本次改写唯一一处有意的语义变化,不属于等价性证明。**

    当前写者写不出「子表行的 notebook_id ≠ 其 source 的 notebook_id」这种行
    (``upsert_paper_meta`` 先 ``SELECT id FROM sources WHERE id=%s AND
    notebook_id=%s FOR KEY SHARE`` 做归属校验;深拷贝同时改写两个字段;全仓无
    source 换库路径),但早于这些写者的畸形历史行可能存在。仓库对这类行早有
    口径:``report_source_rows`` 家族在 JOIN 上写
    ``AND m.notebook_id=s.notebook_id``(见本文件
    ``test_report_source_rows_*`` 里那条同款畸形行用例),``notebook_analytics``
    的 is_paper 计数直接按 ``source_paper_meta.notebook_id`` 分组。新形态把
    搜索腿并入这套口径 —— 搜索谓词现与 ``report_source_rows`` 等报表腿一样,
    统一按子表自身 ``notebook_id`` 收窄。但同一调用链的水合腿
    ``paper_meta_for_sources``(见该函数 docstring)仍只按 ``source_id`` 取
    子表,不看子表的 notebook_id,是登记在案的残留分歧,不随本次改写统一。

    这条用例只钉住搜索腿:在这类畸形遗留行上,``display_title`` 因水合腿命中
    而显示得到,却因搜索腿收窄而搜不到 —— 与改动前(搜得到、报表算不到)方向
    相反。这条用例在旧实现上是**红**的 —— 那正是它存在的意义。SQLite 孪生:
    ``tests/test_sources_pagination.py`` 的同名一条。"""
    notebook_id, other_id = _seed_search_fixture(core_stores)
    _write_sql(
        core_stores,
        "UPDATE source_paper_meta SET notebook_id=%s WHERE source_id=%s",
        (other_id, "s-ptitle"),
    )
    _write_sql(
        core_stores,
        "UPDATE source_authors SET notebook_id=%s WHERE source_id=%s",
        (other_id, "s-author"),
    )
    assert _search_ids(core_stores, notebook_id, "haystack") == []
    assert _search_ids(core_stores, notebook_id, "zeta needleman") == []
    # 也不会泄漏进它被写坏成的那个库(source 行本身仍在原库)。
    assert _search_ids(core_stores, other_id, "haystack") == []
    assert _search_ids(core_stores, other_id, "zeta needleman") == []
    # 其余腿不受影响。
    assert _search_ids(core_stores, notebook_id, "scanned") == ["s-ptitle"]


def test_source_search_empty_query_path_is_unchanged(core_stores: CoreStores):
    notebook_id, _other = _seed_search_fixture(core_stores)
    for blank in ("", "   ", None):
        page = core_stores.sources.list_sources_page(
            notebook_id, offset=0, limit=200, q=blank
        )
        assert page.total_count == 6
        assert [item.id for item in page.items] == [
            "s-title", "s-file", "s-author", "s-ptitle", "s-multi", "s-miss",
        ]


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


def test_concurrent_first_writes_on_a_missing_profile_row_both_survive(
    core_stores: CoreStores,
):
    """codex #535 R7 P2:profile 行不存在时 FOR UPDATE 锁不到任何东西,两个
    并发首写各自对空文档 merge,后提交的 ON CONFLICT 会整份覆盖先提交的字段。
    先锁父 users 行(合法用户恒存在)把缺行情形也串行化——两个字段都必须活。"""
    store = core_stores.identity
    user = store.create_user("f00223456", "correct horse battery staple")
    with core_stores.database.write() as db:
        db.execute("DELETE FROM user_profiles WHERE user_id=%s", (user.id,))

    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def _write(field: str, value: str, origin: str) -> None:
        try:
            barrier.wait(timeout=5)
            store.set_user_search_profile(user.id, {field: value}, origin=origin)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=_write, args=("answer_shape", "prose", "user")),
        threading.Thread(target=_write, args=("answer_language", "en", "job")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not errors, errors

    final = _fetch_one(
        core_stores,
        "SELECT search_profile_json FROM user_profiles WHERE user_id=%s",
        (user.id,),
    )
    from app.services.search_profile import parse_search_profile

    parsed = parse_search_profile(final["search_profile_json"])
    assert parsed["fields"]["answer_shape"]["value"] == "prose"
    assert parsed["fields"]["answer_language"]["value"] == "en"

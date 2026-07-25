"""W1-1: schema_version 迁移机制。

给 _migrate 加 PRAGMA user_version 版本闸 + _add_column_if_missing 助手。
核心不变量：对已部署库(无 user_version 标记)安全、零行为变更、可反复重跑幂等。
"""
from contextlib import contextmanager

import pytest

from app.core.config import Settings
from app.services import sqlite_repository as sr
from app.services.sqlite_repository import SQLiteRepository


def _repo(tmp_path):
    s = Settings(database_url=f"sqlite:///{tmp_path}/t.db")
    return SQLiteRepository(s)


def _user_version(repo) -> int:
    with repo._connect() as db:
        return int(db.execute("PRAGMA user_version").fetchone()[0])


def test_fresh_db_is_stamped_to_current_schema_version(tmp_path):
    """新建库经 __init__ 迁移后，user_version 应被盖到当前 SCHEMA_VERSION。"""
    repo = _repo(tmp_path)
    assert _user_version(repo) >= 1
    assert _user_version(repo) == sr.SCHEMA_VERSION


def test_migration_25_irreversibly_scrubs_user_model_credentials_and_old_status(tmp_path):
    repo = _repo(tmp_path)
    with repo._write() as db:
        db.execute(
            "UPDATE user_profiles SET model_settings = ? WHERE user_id = 'user-local'",
            ('{"llm":{"api_key":"admin-secret","base_url":"https://private.example/v1"}}',),
        )
        db.execute(
            "INSERT INTO users "
            "(id,email,display_name,role,status,username,password_hash,password_salt,"
            "password_iterations,created_at,updated_at) "
            "VALUES ('user-two','two@example.test','two','user','active','b00123456',"
            "'hash','salt',1,'t','t')"
        )
        db.execute(
            "INSERT INTO user_profiles "
            "(id,user_id,memory_mode,domain_focus,created_at,updated_at,model_settings) "
            "VALUES ('profile-two','user-two','manual','[]','t','t',?)",
            ('{"rerank":{"api_key":"second-secret"}}',),
        )
        db.execute(
            "INSERT INTO model_service_status "
            "(user_id,service,config_fingerprint,status,latency_ms,code,trigger,checked_at) "
            "VALUES ('user-local','llm','fp','error',0,'upstream','observed_failure',"
            "'2030-01-01T00:00:00+00:00')"
        )
        db.execute("PRAGMA user_version = 24")

    # 从 v24 起 _migrate 依次应用 _migration_25(凭据清除+系统状态表)、
    # _migration_26(knowhow_changes/knowhow_milestones)与 _migration_27
    # (P1.5 的 sources.chunked_at 完成标记列)与 _migration_28(每笔记本文档数量上限的
    # app_settings 表 + user_profiles.upload_document_limit 列)与 _migration_29
    # (PostgreSQL 合并后两条 migration-24 血脉的和解)、_migration_30(sources 内容
    # 哈希去重索引)与 _migration_31(关系端点稳定 keyset 索引);本用例只断言 v25 的数据副作用,v26 两表结构由
    # test_knowhow_history_schema 单独钉、v27 的列由 test_legacy_db_compat 单独钉、
    # v28 的表/列由 test_document_limit 单独钉、v30/v31 索引由迁移与 snapshot
    # verifier 契约单独钉。
    assert repo._migrate() == [25, 26, 27, 28, 29, 30, 31]

    with repo._connect() as db:
        settings = db.execute(
            "SELECT user_id, model_settings FROM user_profiles ORDER BY user_id"
        ).fetchall()
        old_count = db.execute(
            "SELECT COUNT(*) FROM model_service_status"
        ).fetchone()[0]
        table_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='system_model_service_status'"
        ).fetchone()[0]
    assert [(row["user_id"], row["model_settings"]) for row in settings] == [
        ("user-local", "{}"), ("user-two", "{}")
    ]
    assert old_count == 0
    assert "service_id TEXT PRIMARY KEY" in table_sql
    assert "config_fingerprint TEXT NOT NULL" in table_sql
    assert "recovery_probe" in table_sql


def test_migration_25_rolls_back_scrub_table_and_version_when_stamp_fails(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    original_settings = '{"llm":{"api_key":"must-survive-rollback"}}'
    with repo._write() as db:
        db.execute(
            "UPDATE user_profiles SET model_settings = ? WHERE user_id = 'user-local'",
            (original_settings,),
        )
        db.execute(
            "INSERT INTO model_service_status "
            "(user_id,service,config_fingerprint,status,latency_ms,code,trigger,checked_at) "
            "VALUES ('user-local','llm','fp','error',0,'upstream','observed_failure',"
            "'2030-01-01T00:00:00+00:00')"
        )
        db.execute("DROP TABLE system_model_service_status")
        db.execute("PRAGMA user_version = 24")

    original_write = repo._runtime.database.write

    @contextmanager
    def fail_final_stamp():
        with original_write() as db:
            class FailingConnection:
                def execute(self, sql, parameters=()):
                    if "PRAGMA user_version = 25" in str(sql):
                        raise RuntimeError("injected version stamp failure")
                    return db.execute(sql, parameters)

                def __getattr__(self, name):
                    return getattr(db, name)

            yield FailingConnection()

    monkeypatch.setattr(repo._runtime.database, "write", fail_final_stamp)

    with pytest.raises(RuntimeError, match="version stamp failure"):
        repo._migrate()

    with repo._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 24
        assert db.execute(
            "SELECT model_settings FROM user_profiles WHERE user_id='user-local'"
        ).fetchone()[0] == original_settings
        assert db.execute("SELECT COUNT(*) FROM model_service_status").fetchone()[0] == 1
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='system_model_service_status'"
        ).fetchone() is None


def test_up_to_date_db_takes_fast_path_and_applies_nothing(tmp_path):
    """已是最新版本的库再次 _migrate 应走快路径、不应用任何步骤(返回空列表)。"""
    repo = _repo(tmp_path)  # __init__ 已迁移到 SCHEMA_VERSION
    assert repo._migrate() == []


def test_deployed_db_without_user_version_remigrates_idempotently(tmp_path):
    """模拟"版本机制上线前就已部署"的库：user_version=0 但 schema 已齐全。
    重跑 _migrate 必须幂等——不得因 duplicate column 报错，且重新盖上版本戳。"""
    repo = _repo(tmp_path)  # 先建齐全量 schema
    # 抹掉版本戳，模拟老部署库
    with repo._write() as db:
        db.execute("PRAGMA user_version = 0")
    assert _user_version(repo) == 0

    applied = repo._migrate()  # 关键：不得抛 duplicate column

    assert applied  # 应用了至少一步(非空/非 None)
    assert _user_version(repo) == sr.SCHEMA_VERSION
    # 经守卫式 ALTER 补的列仍在，schema 未被破坏
    with repo._connect() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(answers)").fetchall()}
    assert "conversation_id" in cols


def test_add_column_if_missing_adds_once_then_is_noop(tmp_path):
    """助手：列缺失则补、已存在则 no-op(不得 duplicate column 报错)。"""
    repo = _repo(tmp_path)
    with repo._write() as db:
        db.execute("CREATE TABLE _probe (id TEXT)")
        repo._add_column_if_missing(db, "_probe", "extra", "TEXT NOT NULL DEFAULT ''")
        cols_after_first = {r["name"] for r in db.execute("PRAGMA table_info(_probe)").fetchall()}
        # 第二次是 no-op，绝不能抛错
        repo._add_column_if_missing(db, "_probe", "extra", "TEXT NOT NULL DEFAULT ''")
        cols_after_second = {r["name"] for r in db.execute("PRAGMA table_info(_probe)").fetchall()}
    assert "extra" in cols_after_first
    assert cols_after_first == cols_after_second


def test_add_column_if_missing_skips_absent_table(tmp_path):
    """助手：表不存在时静默跳过(等价于现有 `rep_cols and` 守卫)，不得报错。"""
    repo = _repo(tmp_path)
    with repo._write() as db:
        repo._add_column_if_missing(db, "_no_such_table", "c", "TEXT")


# --- 每启动副作用不得被版本闸快路径吞掉（root cause 2：DDL 与运行代码分离） ---

def test_interrupted_merge_job_recovered_on_restart_even_via_fast_path(tmp_path):
    """崩溃兜底：卡在 'running' 的 merge_review_jobs 必须在每次重启被标记 failed，
    即便 schema 已最新、迁移走了快路径也不能漏(否则该 notebook 的单飞守卫永久卡死)。

    清算已从构造副作用改为服务端启动路径显式调用(见
    tests/test_startup_recovery_ownership.py),但「版本闸快路径不得吞掉它」这条
    仍然成立且仍需钉住——两者是独立的代码路径,快路径只短路 _migrate。"""
    s = Settings(database_url=f"sqlite:///{tmp_path}/t.db")
    repo = SQLiteRepository(s)
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) "
            "VALUES ('nb1', 'n', 't', 't')")
        db.execute(
            "INSERT INTO merge_review_jobs (notebook_id, status) VALUES ('nb1', 'running')")
    # 模拟服务端重启：同库上重建 repo（schema 已最新 → _migrate 走快路径）+ 显式清算
    restarted = SQLiteRepository(s)
    restarted._recover_interrupted_jobs()
    with repo._connect() as db:
        status = db.execute(
            "SELECT status FROM merge_review_jobs WHERE notebook_id='nb1'").fetchone()["status"]
    assert status == "failed"


def test_object_schemas_reseeded_on_restart_even_via_fast_path(tmp_path):
    """内建 object_schemas 种子必须每启动补齐(INSERT OR IGNORE)，
    即便迁移走快路径——否则 code 新增的内建类型永不落库。"""
    s = Settings(database_url=f"sqlite:///{tmp_path}/t.db")
    repo = SQLiteRepository(s)
    with repo._write() as db:
        db.execute("DELETE FROM object_schemas WHERE source='builtin'")
    # 模拟重启：同库上重建 repo（schema 已最新 → _migrate 走快路径）
    SQLiteRepository(s)
    with repo._connect() as db:
        n = db.execute(
            "SELECT COUNT(*) c FROM object_schemas WHERE source='builtin'").fetchone()["c"]
    assert n > 0

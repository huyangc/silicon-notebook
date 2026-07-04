"""W1-1: schema_version 迁移机制。

给 _migrate 加 PRAGMA user_version 版本闸 + _add_column_if_missing 助手。
核心不变量：对已部署库(无 user_version 标记)安全、零行为变更、可反复重跑幂等。
"""
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
    即便 schema 已最新、迁移走了快路径也不能漏(否则该 notebook 的单飞守卫永久卡死)。"""
    s = Settings(database_url=f"sqlite:///{tmp_path}/t.db")
    repo = SQLiteRepository(s)
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) "
            "VALUES ('nb1', 'n', 't', 't')")
        db.execute(
            "INSERT INTO merge_review_jobs (notebook_id, status) VALUES ('nb1', 'running')")
    # 模拟重启：同库上重建 repo（schema 已最新 → _migrate 走快路径）
    SQLiteRepository(s)
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

"""整体改造的约束护栏：任何重构阶段，既有（旧版本 / 未版本化）数据库都必须能被当前
代码直接加载——不报错、零数据丢失、schema 收敛到当前契约、每启动兜底与种子照跑。

这是 W1–W4 重构路线图的护栏：W2 抽 RetrievalService、W3 拆 repository / 拆 service、
W4 改写入路径……任何一处若意外改动了表结构或破坏了旧库加载路径，本测试即红。

为什么不依赖旧代码/git：`_migration_1` 与历史 `_migrate` 逐字节等价，故当前代码建出的
schema == 旧代码建出的 schema（由下面的 schema 契约 golden 锁定）。"旧库"通过把当前代码
建的库退化成"上线于版本机制之前"的样子来模拟：抹掉 user_version、在 ALTER 加的列上写
数据、留一个卡死的 running job、删一个内建种子。
"""
import os
import pathlib

from app.core.config import Settings
from app.services import sqlite_repository as sr
from app.services.sqlite_repository import SQLiteRepository

_GOLDEN = pathlib.Path(__file__).parent / "fixtures" / "schema_contract.txt"


def _snapshot_schema(repo) -> str:
    """全表列（名/类型/notnull/默认值/pk）+ 非内部索引的确定性快照。"""
    lines = []
    with repo._connect() as db:
        tables = [r["name"] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for t in tables:
            for c in db.execute(f"PRAGMA table_info({t})"):
                lines.append(f"{t}.{c['name']}|{c['type']}|{c['notnull']}|{c['dflt_value']}|{c['pk']}")
        idx = [r["name"] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return "\n".join(lines) + "\n--INDEXES--\n" + "\n".join(sorted(idx))


def _repo(tmp_path, name="t.db"):
    return SQLiteRepository(Settings(
        database_url=f"sqlite:///{tmp_path}/{name}", storage_dir=str(tmp_path / "storage")))


def test_fresh_schema_matches_committed_contract(tmp_path):
    """schema 契约锁：全新库的完整 schema 必须与 committed golden 一致。
    未来任一阶段意外改了表结构即红；确需改 schema 时应经 _migration_N + 重生成 golden：
        UPDATE_SCHEMA_GOLDEN=1 pytest tests/test_legacy_db_compat.py -k contract
    """
    snap = _snapshot_schema(_repo(tmp_path))
    if os.environ.get("UPDATE_SCHEMA_GOLDEN") == "1":
        _GOLDEN.parent.mkdir(exist_ok=True)
        _GOLDEN.write_text(snap, encoding="utf-8")
    assert snap == _GOLDEN.read_text(encoding="utf-8"), (
        "schema 偏离契约 golden。确为有意迁移，请 UPDATE_SCHEMA_GOLDEN=1 重生成；"
        "否则说明重构意外改动了表结构，会破坏既有库加载。")


def test_v24_schema_version_is_current(tmp_path):
    repo = _repo(tmp_path)
    with repo._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 24


def test_deployed_v22_db_upgrades_model_service_status_schema(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/v22.db",
        storage_dir=str(tmp_path / "storage"),
    )
    repo0 = SQLiteRepository(settings)
    with repo0._write() as db:
        db.execute("DROP TABLE model_service_status")
        db.execute("PRAGMA user_version = 22")

    repo1 = SQLiteRepository(settings)
    with repo1._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 24
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='model_service_status'"
        ).fetchone() is not None


def test_deployed_v23_db_upgrades_kg_canonical_scratch_schema(tmp_path):
    """_migration_24 的同款守卫：已部署到 v23 的库（有 model_service_status，
    缺写锁瘦身改造点 2 的 kg_canonical_scratch）必须在重新加载时补建该表。
    版本闸 `if current >= SCHEMA_VERSION: return []` 对已部署库短路，所以新表
    只能靠**追加** _migration_N 才会在这类库上真正建出来。"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/v23.db",
        storage_dir=str(tmp_path / "storage"),
    )
    repo0 = SQLiteRepository(settings)
    with repo0._write() as db:
        db.execute("DROP TABLE kg_canonical_scratch")
        db.execute("PRAGMA user_version = 23")

    repo1 = SQLiteRepository(settings)
    with repo1._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 24
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='kg_canonical_scratch'"
        ).fetchone() is not None


def test_legacy_unversioned_db_loads_without_data_loss(tmp_path):
    """核心约束：把一个"上线于版本机制之前"的旧库（未版本化 + 有数据 + 卡死 job +
    缺种子）交给当前代码直接加载——不报错、数据原样、schema 收敛到契约、兜底与种子照跑。"""
    db_url = f"sqlite:///{tmp_path}/legacy.db"
    settings = Settings(database_url=db_url, storage_dir=str(tmp_path / "s"))

    # 1) 用当前代码建全量 schema
    repo0 = SQLiteRepository(settings)
    # 2) 退化成"上线于版本机制之前"的旧库形态并灌代表性数据
    with repo0._write() as db:
        db.execute("PRAGMA user_version = 0")  # 老库从不设版本
        db.execute("INSERT INTO notebooks (id,name,created_at,updated_at) "
                   "VALUES ('nb','老库','t0','t0')")
        # 在多次 ALTER 迁移加的列上写非默认值 —— 验证这些数据原样保留
        db.execute("UPDATE notebooks SET tier='base', is_shared=1, "
                   "share_token='tok', purpose_auto=1 WHERE id='nb'")
        db.execute("INSERT INTO merge_review_jobs (notebook_id,status) VALUES ('nb','running')")
        deleted = db.execute("SELECT object_type FROM object_schemas "
                             "WHERE source='builtin' LIMIT 1").fetchone()["object_type"]
        db.execute("DELETE FROM object_schemas WHERE object_type=?", (deleted,))

    # 3) 用当前代码重新加载同一个库文件（= 生产升级路径 = 构造 + 服务端启动清算；
    #    清算不再是构造副作用，见 tests/test_startup_recovery_ownership.py）
    repo1 = SQLiteRepository(settings)
    repo1._recover_interrupted_jobs()

    assert repo1._migrate() == []  # 已迁移到位，二次走快路径不再重迁移
    with repo1._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == sr.SCHEMA_VERSION
        nb = dict(db.execute(
            "SELECT tier,is_shared,share_token,purpose_auto,name "
            "FROM notebooks WHERE id='nb'").fetchone())
        merge = db.execute(
            "SELECT status FROM merge_review_jobs WHERE notebook_id='nb'").fetchone()["status"]
        reseeded = db.execute(
            "SELECT COUNT(*) c FROM object_schemas WHERE object_type=?", (deleted,)).fetchone()["c"]

    # 数据原样（含历次 ALTER 加的列）
    assert nb == {"tier": "base", "is_shared": 1, "share_token": "tok",
                  "purpose_auto": 1, "name": "老库"}
    # 每启动兜底：卡死的 running job 被标记 failed
    assert merge == "failed"
    # 种子重补：被删的内建 object_schema 已补回
    assert reseeded == 1
    # schema 收敛到契约（旧库加载后与全新库完全一致）
    assert _snapshot_schema(repo1) == _GOLDEN.read_text(encoding="utf-8")

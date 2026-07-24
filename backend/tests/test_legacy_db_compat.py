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


def test_v31_schema_version_is_current(tmp_path):
    repo = _repo(tmp_path)
    with repo._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 31


def test_deployed_v22_db_upgrades_through_system_model_service_status(tmp_path):
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
        assert db.execute("PRAGMA user_version").fetchone()[0] == 31
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='model_service_status'"
        ).fetchone() is not None
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='system_model_service_status'"
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
        assert db.execute("PRAGMA user_version").fetchone()[0] == 31
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='kg_canonical_scratch'"
        ).fetchone() is not None


def test_deployed_v24_db_upgrades_adds_chunked_at(tmp_path):
    """_migration_27 的同款守卫：已部署到 v24 的库（缺 P1.5 引入的完成标记列
    sources.chunked_at）必须在重新加载时补出该列。版本闸 `if current >=
    SCHEMA_VERSION: return []` 对已部署库短路，所以新列只能靠**追加**
    _migration_N 才会在这类库上真正补出来。回填规则本身（哪些 parse_status
    被置值/哪些留 NULL）的变异验证见下面的
    test_migration_27_backfills_chunked_at_only_for_parsed_states，本测试只钉
    「列本身被补出」这一半。"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/v24.db",
        storage_dir=str(tmp_path / "storage"),
    )
    repo0 = SQLiteRepository(settings)
    with repo0._write() as db:
        db.execute("ALTER TABLE sources DROP COLUMN chunked_at")
        db.execute("PRAGMA user_version = 24")

    repo1 = SQLiteRepository(settings)
    with repo1._connect() as db:
        # 版本闸落到当前 SCHEMA_VERSION(非硬编码字面量,防后续新迁移使断言假红——
        # 与本文件 test_deployed_v22/v23 的硬编码写法不同的刻意选择)。
        assert db.execute("PRAGMA user_version").fetchone()[0] == sr.SCHEMA_VERSION
        cols = {r["name"] for r in db.execute("PRAGMA table_info(sources)").fetchall()}
    assert "chunked_at" in cols


def test_migration_27_backfills_chunked_at_only_for_parsed_states(tmp_path):
    """迁移变异验证(spec「迁移与回填」节，全设计最容易翻车的一步)：
    _migration_27 的回填 UPDATE 必须只命中 parse_status IN
    ('parsed','extracting','extracted') 的源，把 chunked_at 置成**各自的**
    updated_at（不是某个共享时间戳——每行给不同的 updated_at 以避免误通过）；
    其余状态（从未解析成功 / 未完成 / 合成源）必须留 NULL。回填规则若误伤
    前者会掩盖真实的历史分块失败，若误伤后者会让存量纯标题/短文 md 被 P2 的
    H3 集体误报缺分块——上线一墙假警报（spec 原话）。"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/backfill.db",
        storage_dir=str(tmp_path / "storage"),
    )
    # parse_status -> updated_at；覆盖回填规则应命中(前三态)与应排除(后五态)
    # 的完整状态集合。
    states = {
        "src-parsed": ("parsed", "2024-01-01T00:00:00Z"),
        "src-extracting": ("extracting", "2024-01-02T00:00:00Z"),
        "src-extracted": ("extracted", "2024-01-03T00:00:00Z"),
        "src-uploaded": ("uploaded", "2024-01-04T00:00:00Z"),
        "src-queued": ("queued", "2024-01-05T00:00:00Z"),
        "src-parsing": ("parsing", "2024-01-06T00:00:00Z"),
        "src-failed": ("failed", "2024-01-07T00:00:00Z"),
        "src-metadata-only": ("metadata-only", "2024-01-08T00:00:00Z"),
    }

    repo0 = SQLiteRepository(settings)
    with repo0._write() as db:
        # 退化成 v24 部署库形态：chunked_at 尚不存在（_migration_27 之前）。
        db.execute("ALTER TABLE sources DROP COLUMN chunked_at")
        db.execute(
            "INSERT INTO notebooks (id,name,created_at,updated_at) "
            "VALUES ('nb','测试笔记本','t0','t0')"
        )
        for source_id, (parse_status, updated_at) in states.items():
            db.execute(
                "INSERT INTO sources "
                "(id,notebook_id,title,source_type,parse_status,created_at,updated_at) "
                "VALUES (?,'nb',?,'file',?,?,?)",
                (source_id, source_id, parse_status, updated_at, updated_at),
            )
        db.execute("PRAGMA user_version = 24")

    # 重新构造仓储 = 触发 SQLiteRepository.__init__ → migrator.initialize() →
    # migrate()，current(24) < SCHEMA_VERSION 使 range() 扫到 _migration_25(#328 凭据清除)、_migration_26(#327 knowhow)与 _migration_27(chunked_at)。
    # initialize() 只跑 migrate()+seed()，不跑 _recover_interrupted_jobs()
    # （见 migrations.py:initialize 的说明），所以 queued/parsing/extracting
    # 这几个本会被启动清算改写的状态在这里原样保留，不干扰本测试。
    repo1 = SQLiteRepository(settings)
    with repo1._connect() as db:
        rows = {
            r["id"]: r["chunked_at"]
            for r in db.execute("SELECT id, chunked_at FROM sources").fetchall()
        }

    backfilled = {"parsed", "extracting", "extracted"}
    for source_id, (parse_status, updated_at) in states.items():
        if parse_status in backfilled:
            assert rows[source_id] == updated_at, (source_id, parse_status, rows[source_id])
        else:
            assert rows[source_id] is None, (source_id, parse_status, rows[source_id])


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

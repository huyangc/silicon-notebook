# backend/tests/test_legacy_leftover_sweep.py
"""批 3·W1 PR-4:存量删除残渣离线清扫的行为 pin(SQLite 侧)。

PostgreSQL twin(ctid 分页 + 真 advisory lock)在
``backend/tests/postgres/test_legacy_leftover_sweep_pg.py``。"""
import math
import os
import uuid

import pytest

from app.core.config import Settings
from app.services.legacy_leftover_sweep import (
    DIRECT_DISK_ROOTS,
    ORPHAN_ROW_TABLES,
    SCALE_DISK_ROOTS,
    DiskSweepReport,
    count_orphan_rows,
    find_orphan_disk,
    main,
    sweep_orphan_disk,
    sweep_orphan_rows,
)
from app.repositories.scale_build_lock import SCALE_BUILD_LOCK_UNAVAILABLE
from app.services.sqlite_repository import SQLiteRepository, _now


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    return SQLiteRepository(Settings())


def _mk_nb(repo, name="NB", owner="user-local"):
    nb_id = f"nb-{uuid.uuid4().hex[:10]}"
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks (id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (nb_id, name, "", "Semiconductor", "active", owner, now, now),
        )
    return nb_id


def _seed_rows(repo, notebook_id, per_table):
    """给 5 张孤儿候选表各插 per_table 行(最小非空列)。"""
    now = _now()
    with repo._write() as db:
        for i in range(per_table):
            db.execute(
                "INSERT INTO community_members (canonical_id,notebook_id,community_id)"
                " VALUES (?,?,?)",
                (f"can-{i}", notebook_id, f"comm-{i}"),
            )
            db.execute(
                "INSERT INTO conversations (id,notebook_id,created_at,updated_at)"
                " VALUES (?,?,?,?)",
                (f"conv-{notebook_id}-{i}", notebook_id, now, now),
            )
            db.execute(
                "INSERT INTO knowledge_object_sources (object_id,source_id,notebook_id)"
                " VALUES (?,?,?)",
                (f"ko-{i}", f"src-{i}", notebook_id),
            )
            db.execute(
                "INSERT INTO kg_cluster_scratch (notebook_id,run_id,object_id,seed)"
                " VALUES (?,?,?,?)",
                (notebook_id, "run-1", f"ko-{i}", f"seed-{i}"),
            )
            db.execute(
                "INSERT INTO kg_canonical_scratch (notebook_id,run_id,seed,canonical_id)"
                " VALUES (?,?,?,?)",
                (notebook_id, "run-1", f"seed-{i}", f"can-{i}"),
            )


def _table_count(repo, table, notebook_id):
    with repo._connect() as db:
        return db.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE notebook_id=?", (notebook_id,)
        ).fetchone()["n"]


def _database(repo):
    return repo._runtime.database


def test_row_sweep_removes_orphans_keeps_live_bounded_batches(repo):
    """行半三合一 pin:计数准确;只删孤儿、活本毫发无损;每批一个独立写事务,
    事务数 = 每表 ceil(n/batch)+1(终止条件是 rowcount==0,不是不足一批)。"""
    live = _mk_nb(repo)
    _seed_rows(repo, live, 2)
    _seed_rows(repo, "ghost-a", 3)
    _seed_rows(repo, "ghost-b", 2)

    counts = count_orphan_rows(_database(repo), "sqlite")
    assert counts == {table: 5 for table in ORPHAN_ROW_TABLES}

    database = _database(repo)
    real_write = database.write
    tx = {"n": 0}

    def counting_write(*args, **kwargs):
        tx["n"] += 1
        return real_write(*args, **kwargs)

    database.write = counting_write
    try:
        deleted = sweep_orphan_rows(database, "sqlite", batch_size=2)
    finally:
        database.write = real_write

    assert deleted == {table: 5 for table in ORPHAN_ROW_TABLES}
    expected_tx = len(ORPHAN_ROW_TABLES) * (math.ceil(5 / 2) + 1)
    assert tx["n"] == expected_tx
    assert count_orphan_rows(_database(repo), "sqlite") == {
        table: 0 for table in ORPHAN_ROW_TABLES
    }
    for table in ORPHAN_ROW_TABLES:
        assert _table_count(repo, table, live) == 2


def _seed_disk(storage, ghost, live):
    """5 棵根下铺目录:ghost 全套(scale 根含全部 scratch 兄弟),live 同样
    有 scratch 兄弟——pin「活本的 scratch 不归本清扫管」。"""
    for root in DIRECT_DISK_ROOTS:
        for nb in (ghost, live):
            (storage / root / nb).mkdir(parents=True)
            (storage / root / nb / "f.bin").write_bytes(b"x")
    for root in SCALE_DISK_ROOTS:
        parent = storage / root
        for name in (ghost, f"{ghost}.old", f"{ghost}.tmp", f"{ghost}.tmp-tok1"):
            (parent / name).mkdir(parents=True)
        (parent / live).mkdir(parents=True)
        (parent / f"{live}.tmp").mkdir(parents=True)


def test_disk_sweep_removes_orphans_and_scratch_keeps_live_and_symlinks(repo):
    """盘半 pin:孤儿的同名目录 + 三种 scratch 兄弟全清;活本(含活本自己的
    .tmp)不动;symlink 子项不进候选。SQLite 的 UNSUPPORTED 锁哨兵按持有走。"""
    live = _mk_nb(repo)
    storage = repo._runtime.source_files.storage_dir
    ghost = "nb-ghost001"
    _seed_disk(storage, ghost, live)
    link = storage / "notebooks" / "nb-linked"
    link.symlink_to(storage / "notebooks" / live)

    orphans = find_orphan_disk(_database(repo), "sqlite", storage)
    assert orphans == {root: [ghost] for root in DIRECT_DISK_ROOTS + SCALE_DISK_ROOTS}
    # inspect 只读:全部目录原样还在
    assert (storage / "kg_index" / f"{ghost}.tmp-tok1").is_dir()

    report = sweep_orphan_disk(_database(repo), "sqlite", storage)
    assert isinstance(report, DiskSweepReport) and report.clean
    assert report.removed == {
        root: [ghost] for root in DIRECT_DISK_ROOTS + SCALE_DISK_ROOTS
    }
    for root in DIRECT_DISK_ROOTS:
        assert not (storage / root / ghost).exists()
        assert (storage / root / live / "f.bin").is_file()
    for root in SCALE_DISK_ROOTS:
        for name in (ghost, f"{ghost}.old", f"{ghost}.tmp", f"{ghost}.tmp-tok1"):
            assert not (storage / root / name).exists()
        assert (storage / root / live).is_dir()
        assert (storage / root / f"{live}.tmp").is_dir()
    assert link.is_symlink() and (storage / "notebooks" / live).is_dir()


@pytest.mark.parametrize(
    "attempt,reason",
    [(None, "lock_held_elsewhere"), (SCALE_BUILD_LOCK_UNAVAILABLE, "lock_unavailable")],
)
def test_disk_sweep_skips_scale_roots_when_claim_not_held(repo, attempt, reason):
    """锁被占/无法评估:scale 三根整本跳过留声,notebooks/assets 照清。"""
    live = _mk_nb(repo)
    storage = repo._runtime.source_files.storage_dir
    ghost = "nb-ghost002"
    _seed_disk(storage, ghost, live)
    database = _database(repo)
    database.try_scale_build_lock = lambda notebook_id: attempt

    report = sweep_orphan_disk(database, "sqlite", storage)
    assert report.skipped == [(ghost, reason)]
    assert not report.clean
    for root in DIRECT_DISK_ROOTS:
        assert not (storage / root / ghost).exists()
    for root in SCALE_DISK_ROOTS:
        assert (storage / root / ghost).is_dir()
        assert (storage / root / f"{ghost}.tmp-tok1").is_dir()


def test_cli_inspect_is_readonly_and_apply_sweeps(repo, tmp_path, capsys):
    """CLI 收口 pin:默认 inspect 报数不动手、退出 0;--apply 清空后退出 0;
    再跑一次 inspect 全零。绝不打印数据库 URL。"""
    live = _mk_nb(repo)
    _seed_rows(repo, live, 1)
    _seed_rows(repo, "ghost-c", 3)
    storage = repo._runtime.source_files.storage_dir
    _seed_disk(storage, "nb-ghost003", live)

    assert main([]) == 0
    out = capsys.readouterr().out
    assert "community_members: 3" in out and "nb-ghost003" in out
    assert str(os.environ["DATABASE_URL"]) not in out
    assert (storage / "notebooks" / "nb-ghost003").is_dir()
    assert count_orphan_rows(_database(repo), "sqlite")["conversations"] == 3

    assert main(["--apply", "--batch-size", "2"]) == 0
    assert count_orphan_rows(_database(repo), "sqlite") == {
        table: 0 for table in ORPHAN_ROW_TABLES
    }
    assert not (storage / "notebooks" / "nb-ghost003").exists()
    assert not (storage / "kg_index" / "nb-ghost003.tmp-tok1").exists()
    for table in ORPHAN_ROW_TABLES:
        assert _table_count(repo, table, live) == 1

    assert main([]) == 0
    out = capsys.readouterr().out
    assert "community_members: 0" in out


def test_cli_rejects_contradictory_flags(repo):
    with pytest.raises(SystemExit) as excinfo:
        main(["--rows-only", "--disk-only"])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        main(["--batch-size", "0"])
    assert excinfo.value.code == 2

# backend/tests/test_legacy_leftover_sweep.py
"""批 3·W1 PR-4:存量删除残渣离线清扫的行为 pin(SQLite 侧)。

PostgreSQL twin(ctid 分页 + 真 advisory lock)在
``backend/tests/postgres/test_legacy_leftover_sweep_pg.py``。"""
import math
import os
import time
import uuid
from pathlib import Path

import pytest

from app.core.config import Settings
from app.migration.legacy_leftover_sweep import (
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
    """行半三合一 pin:计数准确;只删孤儿、活本毫发无损;两阶段逐 id 分页,
    每批一个独立写事务,事务数 = Σ_id ceil(n_id/batch)+1(终止条件是
    rowcount==0,不是不足一批);on_table_done 逐表回调落进度账。"""
    live = _mk_nb(repo)
    _seed_rows(repo, live, 2)
    _seed_rows(repo, "ghost-a", 3)
    _seed_rows(repo, "ghost-b", 2)

    counts = count_orphan_rows(_database(repo))
    assert counts == {table: 5 for table in ORPHAN_ROW_TABLES}

    database = _database(repo)
    real_write = database.write
    tx = {"n": 0}
    progress: list[tuple[str, int]] = []

    def counting_write(*args, **kwargs):
        tx["n"] += 1
        return real_write(*args, **kwargs)

    database.write = counting_write
    try:
        deleted = sweep_orphan_rows(
            database, "sqlite", batch_size=2,
            on_table_done=lambda t, n: progress.append((t, n)),
        )
    finally:
        database.write = real_write

    assert deleted == {table: 5 for table in ORPHAN_ROW_TABLES}
    assert progress == [(table, 5) for table in ORPHAN_ROW_TABLES]
    # 逐孤儿 id 分页:ghost-a 3 行→ceil(3/2)+1=3 事务,ghost-b 2 行→2 事务
    per_table = (math.ceil(3 / 2) + 1) + (math.ceil(2 / 2) + 1)
    assert tx["n"] == len(ORPHAN_ROW_TABLES) * per_table
    assert count_orphan_rows(_database(repo)) == {
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

    report = sweep_orphan_disk(_database(repo), "sqlite", storage, min_age_seconds=0)
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

    report = sweep_orphan_disk(database, "sqlite", storage, min_age_seconds=0)
    assert report.skipped == [(ghost, reason)]
    assert not report.clean
    for root in DIRECT_DISK_ROOTS:
        assert not (storage / root / ghost).exists()
    for root in SCALE_DISK_ROOTS:
        assert (storage / root / ghost).is_dir()
        assert (storage / root / f"{ghost}.tmp-tok1").is_dir()


def test_disk_sweep_age_gate_protects_recent_direct_dirs(repo):
    """内评 P1 pin:copy_notebook 先 copytree 后插行——闸值内的直删根候选
    必须跳过留声,scale 根不受闸管;时间流逝(now 注入,ctime 无法做旧)后
    重跑即清。"""
    live = _mk_nb(repo)
    storage = repo._runtime.source_files.storage_dir
    ghost = "nb-ghost005"
    _seed_disk(storage, ghost, live)

    report = sweep_orphan_disk(
        _database(repo), "sqlite", storage, min_age_seconds=3600
    )
    assert sorted(report.skipped) == [
        (ghost, "recent_dir:assets"), (ghost, "recent_dir:notebooks"),
    ]
    assert not report.clean
    for root in DIRECT_DISK_ROOTS:
        assert (storage / root / ghost / "f.bin").is_file()
    for root in SCALE_DISK_ROOTS:
        assert not (storage / root / ghost).exists()

    report = sweep_orphan_disk(
        _database(repo), "sqlite", storage,
        min_age_seconds=3600, now=time.time() + 7200,
    )
    assert report.clean
    for root in DIRECT_DISK_ROOTS:
        assert not (storage / root / ghost).exists()


def test_disk_sweep_age_gate_ignores_inherited_old_mtime(repo):
    """codex #666 R1 P1 pin:copy_notebook 的 copytree 会把**源目录的旧
    mtime** 原样复制到目的目录(copystat)——闸只看 mtime 时,刚落盘的在途
    拷贝会被判旧而误删。闸取 max(mtime, ctime):把候选的 mtime 人为做旧
    (等价于 copytree 继承),ctime 仍是刚才 → 必须仍被闸拦下。"""
    live = _mk_nb(repo)
    storage = repo._runtime.source_files.storage_dir
    ghost = "nb-ghost008"
    _seed_disk(storage, ghost, live)
    stale = time.time() - 7200
    for root in DIRECT_DISK_ROOTS:
        os.utime(storage / root / ghost, (stale, stale))  # 继承来的旧 mtime

    report = sweep_orphan_disk(
        _database(repo), "sqlite", storage, min_age_seconds=3600
    )
    assert sorted(report.skipped) == [
        (ghost, "recent_dir:assets"), (ghost, "recent_dir:notebooks"),
    ]
    for root in DIRECT_DISK_ROOTS:
        assert (storage / root / ghost / "f.bin").is_file(), (
            "旧 mtime 是 copytree 从源继承来的假信号,不许据此删在途拷贝"
        )


def test_disk_sweep_stops_scale_sweep_when_claim_lost_midway(repo):
    """#643 不变量① pin:每次破坏性 rmtree 前复验持锁;复验失败就地停手、
    记 lock_lost_mid_sweep,剩余 scratch 兄弟原样保留。"""
    live = _mk_nb(repo)
    storage = repo._runtime.source_files.storage_dir
    ghost = "nb-ghost006"
    _seed_disk(storage, ghost, live)

    class _LossyLock:
        supported = True
        claim_token = "tok"

        def __init__(self):
            self.checks = 0

        def verify_held(self):
            self.checks += 1
            return self.checks <= 1

        def release(self):
            return None

    database = _database(repo)
    database.try_scale_build_lock = lambda notebook_id: _LossyLock()
    report = sweep_orphan_disk(database, "sqlite", storage, min_age_seconds=0)
    assert (ghost, "lock_lost_mid_sweep") in report.skipped
    # 第一次复验通过删掉 kg_index 下排序最前的 .tmp-tok1,第二次复验失败停手
    assert not (storage / "kg_index" / f"{ghost}.tmp-tok1").exists()
    assert (storage / "kg_index" / ghost).is_dir()
    assert (storage / "kg_viz" / ghost).is_dir()


def test_disk_sweep_lock_probe_error_skips_that_id_only(repo):
    """锁探测抛错:该 id 记账跳过,不中止整轮,直删根照清。"""
    live = _mk_nb(repo)
    storage = repo._runtime.source_files.storage_dir
    ghost = "nb-ghost007"
    _seed_disk(storage, ghost, live)
    database = _database(repo)

    def exploding_probe(notebook_id):
        raise RuntimeError("pool down")

    database.try_scale_build_lock = exploding_probe
    report = sweep_orphan_disk(database, "sqlite", storage, min_age_seconds=0)
    assert report.skipped == [(ghost, "lock_probe_error")]
    for root in DIRECT_DISK_ROOTS:
        assert not (storage / root / ghost).exists()
    for root in SCALE_DISK_ROOTS:
        assert (storage / root / ghost).is_dir()


def test_scale_disk_roots_match_artifact_store_layout(repo):
    """SCALE_DISK_ROOTS 与 scale_artifact_store 目录公式各存一份字面量——
    钉住两边不失配(同款先例:_artifact_siblings 与 indexed_notebook_ids
    共享 scratch 常量的交叉 pin)。"""
    from app.repositories.filesystem.scale_artifact_store import ScaleArtifactStore

    store = ScaleArtifactStore(repo.settings)
    derived = {
        Path(store.scale_dir("x")).parent.name,
        Path(store.viz_dir("x")).parent.name,
        Path(store.source_partition_dir("x")).parent.name,
    }
    assert derived == set(SCALE_DISK_ROOTS)


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
    assert count_orphan_rows(_database(repo))["conversations"] == 3

    assert main(["--apply", "--batch-size", "2", "--min-age-seconds", "0"]) == 0
    assert count_orphan_rows(_database(repo)) == {
        table: 0 for table in ORPHAN_ROW_TABLES
    }
    assert not (storage / "notebooks" / "nb-ghost003").exists()
    assert not (storage / "kg_index" / "nb-ghost003.tmp-tok1").exists()
    for table in ORPHAN_ROW_TABLES:
        assert _table_count(repo, table, live) == 1

    assert main([]) == 0
    out = capsys.readouterr().out
    assert "community_members: 0" in out


def test_cli_apply_exits_1_when_age_gate_skips(repo, capsys):
    """对外契约 pin:退出码 1 = apply 有跳过。默认年龄闸
    (NOTEBOOK_COPY_STALE_SECONDS)拦下刚落盘的直删根候选 → 1,目录保留。"""
    live = _mk_nb(repo)
    storage = repo._runtime.source_files.storage_dir
    _seed_disk(storage, "nb-ghost004", live)

    assert main(["--apply", "--disk-only"]) == 1
    out = capsys.readouterr().out
    assert "recent_dir:notebooks" in out
    assert (storage / "notebooks" / "nb-ghost004").is_dir()
    # scale 根不受闸管,已在同一轮清掉
    assert not (storage / "kg_index" / "nb-ghost004").exists()


def test_cli_apply_exits_1_on_post_sweep_disk_residual(repo, monkeypatch):
    """codex #666 R2 P2 pin:快照之后才冒出来的孤儿目录(清扫期间崩溃的
    在途拷贝)在收尾复核里不只打印——必须计入退出码,不许把没扫干净当成功。"""
    import app.migration.legacy_leftover_sweep as sweep_mod

    _mk_nb(repo)
    storage = repo._runtime.source_files.storage_dir
    real_find = sweep_mod.find_orphan_disk
    calls = {"n": 0}

    def find_with_late_orphan(database, dialect, storage_dir):
        calls["n"] += 1
        if calls["n"] == 2:  # apply 收尾的残余复核那一次
            late = Path(storage_dir) / "notebooks" / "nb-late-ghost"
            late.mkdir(parents=True, exist_ok=True)
        return real_find(database, dialect, storage_dir)

    monkeypatch.setattr(sweep_mod, "find_orphan_disk", find_with_late_orphan)
    assert main(["--apply", "--disk-only", "--min-age-seconds", "0"]) == 1
    assert (storage / "notebooks" / "nb-late-ghost").is_dir()


def test_cli_rejects_contradictory_flags(repo):
    with pytest.raises(SystemExit) as excinfo:
        main(["--rows-only", "--disk-only"])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        main(["--batch-size", "0"])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        main(["--min-age-seconds", "-1"])
    assert excinfo.value.code == 2

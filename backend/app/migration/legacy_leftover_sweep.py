"""批 3·W1 PR-4：存量删除残渣的一次性离线清扫（孤儿行 + 孤儿磁盘产物）。

删除作业化（PR #659）只保证**今后**的删除不再留残渣；作业化之前的同步删除
路径半途崩溃/被 kill 留下的存量垃圾没有任何在线路径会再路过——行残渣压着
无关查询的 anti-join/聚合，盘残渣按崩溃次数静默吃磁盘。本模块给
``scripts/sweep_legacy_delete_leftovers.py`` 提供可测的清扫原语：

**行半**（``ORPHAN_ROW_TABLES``）：5 张对 ``notebooks`` 无外键、且旧同步删除
路径证实漏删过的表。两阶段（内评 P2：单阶段 ``NOT EXISTS + LIMIT`` 分页在
大表上每批都从堆首重扫，O(批数×全表扫)）——先每表一次 anti-join 物化
distinct 孤儿 ``notebook_id``（一次扫描），再逐 id 走该表的 ``notebook_id``
前导索引分页删（SQLite ``rowid IN (…LIMIT n)``、PostgreSQL ``ctid IN
(…LIMIT n)``，谓词同时保留 anti-join 兜双保险），每批一个独立写事务。
终止条件是 ``rowcount==0``，不是 ``rowcount<batch``（同 ``notebook_delete``
形二的教训：并发写者可以让一批变小而行仍然存在）。阶段一快照之后才变成
孤儿的 id 本轮不清，由 ``--apply`` 收尾的残余复核报出、重跑收敛。

**盘半**：5 棵存储根的直接子目录，名字（scale 根还要先剥
``SCRATCH_SUFFIXES``/``SCRATCH_INFIX`` 得到基 id）不在 ``notebooks`` 表里的
即孤儿。``notebooks/``、``assets/`` 两根直删，但设**年龄闸**（内评 P1）：
``copy_notebook`` 是仓库里唯一「目录先落盘、``notebooks`` 行后提交」的合法
在线路径（先 ``copytree`` 目的目录、快照物化后才插 copying 哨兵行），窗口
可达分钟级——闸值内（目录 mtime 距今 < ``min_age_seconds``，CLI 默认取
``notebook_copy_stale_seconds``）的候选跳过留声，绝不与在途拷贝抢目录；
过闸的目录与删除作业相位 5 ``_sweep_ingestion_stragglers`` 同款语义（行
没了/从未提交且早已过窗的目录不会再被任何在线路径认领）。默认闸值与
拷贝子系统**自己的失活窗口是同一个常量**：目录时间戳超过
``notebook_copy_stale_seconds`` 仍没有行的拷贝，``sweep_stale_copies``
按同一 cutoff 也已把它判死——闸的残余假设恰好就是拷贝子系统自身的活性
契约，不是本清扫另立的时间猜测。把闸调**低**于该窗口（含 0）会掏空这层
保护，CLI 要求搭配 ``--confirm-service-stopped`` 停服确认才放行
（codex #666 R3 P1）；调高恒安全。三棵 scale 根
（``kg_index``/``kg_viz``/``kg_index_partitions``）先取该 id 的跨进程排它
claim（``database.try_scale_build_lock``）再删同名 + scratch 兄弟：

* PostgreSQL：真 advisory lock。``None``（别人持有）与
  ``SCALE_BUILD_LOCK_UNAVAILABLE``（无法评估）都**跳过并留声**，绝不硬删；
  探测自身抛错也只记账跳过该 id，不中止整轮。
* SQLite：拿到的是 ``UNSUPPORTED_SCALE_BUILD_LOCK``（``verify_held`` 恒真）。
  离线构建 CLI 对 SQLite 是硬拒绝的，这里**不**拒绝，理由是目标不同：那边
  rmtree 的是**在册** notebook 的活产物 ``.tmp``，竞态是双构建者双写同一棵
  staging；这边的目标 id 已不在 ``notebooks`` 表——单进程部署的服务对离册
  id 发不起任何合法 scale build（入口都过 ``get_notebook``），唯一可能的
  并发写者是删除作业自己的相位 4/5，而那也是同一组幂等 rmtree，良性。

symlink 一律不清：候选扫描跳过 symlink 子项（该形态**静默保留**，不报
孤儿也不记 failure——symlink 指向哪里不归本清扫判断）；``_artifact_
siblings`` 捡到的 symlink 兄弟会被 ``shutil.rmtree`` 拒绝，留在原地并记为
failure（响亮不中止，同删除作业相位 4 的「记账不中止」）。本模块绝不打印
数据库 URL。
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.repositories.filesystem.scale_artifact_store import (
    SCRATCH_INFIX,
    SCRATCH_SUFFIXES,
)
from app.repositories.scale_build_lock import (
    SCALE_BUILD_LOCK_UNAVAILABLE,
    ScaleBuildLock,
)

# 旧同步删除路径漏删过的 5 张无外键表(两后端同名同列,且都有 notebook_id
# 前导索引;见模块 docstring)。表名只允许来自这份白名单——SQL 里的表名是
# f-string 拼接,绝不接受外部输入。
ORPHAN_ROW_TABLES: tuple[str, ...] = (
    "community_members",
    "conversations",
    "knowledge_object_sources",
    "kg_cluster_scratch",
    "kg_canonical_scratch",
)
# 子目录名即 notebook id 的两棵根(摄取/贴图产物)。
DIRECT_DISK_ROOTS: tuple[str, ...] = ("notebooks", "assets")
# 子目录名可能带 scratch 后缀/中缀的三棵 scale 产物根,删前须持排它 claim。
# 根名与 scale_artifact_store 的目录公式各存一份字面量,由
# test_scale_disk_roots_match_artifact_store_layout 钉住不失配。
SCALE_DISK_ROOTS: tuple[str, ...] = ("kg_index", "kg_viz", "kg_index_partitions")

DEFAULT_BATCH_SIZE = 2000
# notebooks 存在性批查询的 IN 分片大小,留在 SQLite 变量上限(999)之下。
_ID_PROBE_CHUNK = 400


def _placeholder(dialect: str) -> str:
    return "%s" if dialect == "postgresql" else "?"


def _row_handle(dialect: str) -> str:
    return "ctid" if dialect == "postgresql" else "rowid"


def _orphan_predicate(table: str) -> str:
    assert table in ORPHAN_ROW_TABLES
    return (
        f"NOT EXISTS (SELECT 1 FROM notebooks n WHERE n.id = {table}.notebook_id)"
    )


def count_orphan_rows(database) -> dict[str, int]:
    """只读:逐表数孤儿行(anti-join 全表扫,大库上分钟级——见 runbook 的
    低峰提示)。inspect 模式的输出,也是 --apply 后的残余复核。"""
    counts: dict[str, int] = {}
    with database.connect() as db:
        for table in ORPHAN_ROW_TABLES:
            row = db.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {_orphan_predicate(table)}"
            ).fetchone()
            counts[table] = int(row["n"])
    return counts


def _orphan_notebook_ids_in_table(database, table: str) -> list[str]:
    """阶段一:一次 anti-join 扫描物化该表的 distinct 孤儿 notebook_id。"""
    with database.connect() as db:
        rows = db.execute(
            f"SELECT DISTINCT notebook_id FROM {table} "
            f"WHERE {_orphan_predicate(table)}"
        ).fetchall()
    return sorted(str(row["notebook_id"]) for row in rows)


def sweep_orphan_rows(
    database,
    dialect: str,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    on_table_done: "Callable[[str, int], None] | None" = None,
) -> dict[str, int]:
    """两阶段逐表删孤儿行;每批独立写事务;返回逐表实删行数。

    ``on_table_done(table, deleted)`` 每张表收尾时回调——CLI 用它即时落
    进度账,单批语句超时冒出时已完成的表不失账。"""
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    handle = _row_handle(dialect)
    ph = _placeholder(dialect)
    deleted: dict[str, int] = {}
    for table in ORPHAN_ROW_TABLES:
        total = 0
        for notebook_id in _orphan_notebook_ids_in_table(database, table):
            while True:
                with database.write() as db:
                    cursor = db.execute(
                        f"DELETE FROM {table} WHERE {handle} IN ("
                        f"SELECT {handle} FROM {table} "
                        f"WHERE notebook_id = {ph} "
                        f"AND {_orphan_predicate(table)} LIMIT {ph})",
                        (notebook_id, batch_size),
                    )
                    count = cursor.rowcount
                if count <= 0:
                    break
                total += count
        deleted[table] = total
        if on_table_done is not None:
            on_table_done(table, total)
    return deleted


def _scratch_base_id(name: str) -> str:
    """scale 根子目录名 → 基 notebook id(与 ``_artifact_siblings`` 的分类
    形态共享同一对 ``SCRATCH_SUFFIXES``/``SCRATCH_INFIX`` 常量)。"""
    if SCRATCH_INFIX in name:
        return name.split(SCRATCH_INFIX, 1)[0]
    for suffix in SCRATCH_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _scan_candidate_ids(parent: Path, *, scratch: bool) -> set[str]:
    """一棵根下的候选 notebook id 集合;文件与 symlink 子项静默跳过
    (symlink 不进候选,也就永远不会成为本清扫的删除目标)。"""
    if not parent.is_dir():
        return set()
    candidates: set[str] = set()
    for entry in parent.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            continue
        base = _scratch_base_id(entry.name) if scratch else entry.name
        if base:
            candidates.add(base)
    return candidates


def _existing_notebook_ids(database, dialect: str, ids: set[str]) -> set[str]:
    """候选 id 里仍在 ``notebooks`` 表的那部分(分片 IN,只读连接)。"""
    ph = _placeholder(dialect)
    ordered = sorted(ids)
    existing: set[str] = set()
    with database.connect() as db:
        for start in range(0, len(ordered), _ID_PROBE_CHUNK):
            chunk = ordered[start : start + _ID_PROBE_CHUNK]
            marks = ",".join([ph] * len(chunk))
            rows = db.execute(
                f"SELECT id FROM notebooks WHERE id IN ({marks})", tuple(chunk)
            ).fetchall()
            existing.update(str(row["id"]) for row in rows)
    return existing


def find_orphan_disk(database, dialect: str, storage_dir: Path) -> dict[str, list[str]]:
    """只读:逐根列出孤儿 notebook id(scale 根按基 id 归并)。不设年龄闸——
    如实盘点;闸在 ``sweep_orphan_disk`` 的删除侧。"""
    storage = Path(storage_dir)
    per_root_candidates = {
        root: _scan_candidate_ids(storage / root, scratch=root in SCALE_DISK_ROOTS)
        for root in DIRECT_DISK_ROOTS + SCALE_DISK_ROOTS
    }
    all_ids: set[str] = set().union(*per_root_candidates.values())
    existing = _existing_notebook_ids(database, dialect, all_ids) if all_ids else set()
    return {
        root: sorted(candidates - existing)
        for root, candidates in per_root_candidates.items()
    }


@dataclass
class DiskSweepReport:
    """--apply 的盘半结果:逐根实删 id、跳过的 id(带原因:年龄闸/锁)、
    删不掉的路径(权限/竞态;留声不中止)。"""

    removed: dict[str, list[str]] = field(default_factory=dict)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed_paths: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.skipped and not self.failed_paths


def _rmtree_logged(path: Path, report: DiskSweepReport) -> bool:
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        report.failed_paths.append(str(path))
        return False
    return True


def _sweep_scale_roots_for_id(
    database, storage: Path, notebook_id: str, report: DiskSweepReport
) -> list[str]:
    """一个孤儿基 id 在三棵 scale 根下的全部同名+scratch 兄弟,持 claim 删。

    ``_artifact_siblings`` 是删除作业相位 4 的同一把分类器(含删除顺序:
    ``.tmp-<token>`` 先、live 最后);#643 不变量①同款——每次破坏性 rmtree
    前复验持锁,丢锁就地停手。锁探测抛错只记账跳过该 id(记账不中止)。"""
    from app.services.notebook_delete import _artifact_siblings

    try:
        attempt = database.try_scale_build_lock(notebook_id)
    except Exception:  # noqa: BLE001 - 每本记账跳过,绝不让一本中止整轮
        report.skipped.append((notebook_id, "lock_probe_error"))
        return []
    if attempt is SCALE_BUILD_LOCK_UNAVAILABLE:
        report.skipped.append((notebook_id, "lock_unavailable"))
        return []
    if attempt is None:
        report.skipped.append((notebook_id, "lock_held_elsewhere"))
        return []
    assert isinstance(attempt, ScaleBuildLock)
    removed_roots: list[str] = []
    try:
        for root in SCALE_DISK_ROOTS:
            parent = storage / root
            removed_any = False
            for entry in _artifact_siblings(parent, notebook_id):
                if not attempt.verify_held():
                    report.skipped.append((notebook_id, "lock_lost_mid_sweep"))
                    return removed_roots
                if _rmtree_logged(entry, report):
                    removed_any = True
            if removed_any:
                removed_roots.append(root)
    finally:
        attempt.release()
    return removed_roots


def sweep_orphan_disk(
    database,
    dialect: str,
    storage_dir: Path,
    *,
    min_age_seconds: float,
    now: "float | None" = None,
) -> DiskSweepReport:
    """--apply 的盘半:notebooks/assets 过年龄闸后直删,scale 三根持 claim 删。

    年龄闸只管两棵直删根(见模块 docstring 的 ``copy_notebook`` 窗口论证;
    scale 根的在途写者全部持 claim,由锁互斥兜住,不需要闸)。

    闸的时间信号取 ``max(st_mtime, st_ctime)``(codex #666 R1 P1):
    ``copy_notebook`` 用 ``shutil.copytree`` 落目的目录,copytree 会把**源
    目录的旧 mtime** 原样复制过来(``copystat``)——只看 mtime,刚落盘的
    在途拷贝会被判「старый」而误删。ctime(inode 变更时间)用户态无法回拨、
    copystat 设 mtime 反而会把它顶到当下,天然不可继承;取二者较新者,
    继承来的旧 mtime 拦不过新 ctime,真正的存量残渣两者都旧、照常过闸。
    ``now`` 仅供测试注入模拟时间流逝(ctime 无法人为做旧),生产恒为当前
    时刻。"""
    storage = Path(storage_dir)
    orphans = find_orphan_disk(database, dialect, storage)
    report = DiskSweepReport(removed={root: [] for root in orphans})
    if now is None:
        now = time.time()
    for root in DIRECT_DISK_ROOTS:
        for notebook_id in orphans[root]:
            target = storage / root / notebook_id
            try:
                stat = target.lstat()
            except OSError:
                continue  # 竞态里刚消失的目录:无事可做
            age = now - max(stat.st_mtime, stat.st_ctime)
            if age < min_age_seconds:
                report.skipped.append((notebook_id, f"recent_dir:{root}"))
                continue
            if _rmtree_logged(target, report):
                report.removed[root].append(notebook_id)
    scale_ids = sorted(set().union(*(orphans[root] for root in SCALE_DISK_ROOTS)))
    for notebook_id in scale_ids:
        for root in _sweep_scale_roots_for_id(database, storage, notebook_id, report):
            report.removed[root].append(notebook_id)
    return report


# ─────────────────────────────────────────────────────── CLI composition ──


def _open_database(settings, dialect: str):
    """轻组合:只要 Database(连接 + 排它 claim 接缝),不组装完整仓库、
    不迁移、不播种——脚本假定 schema 已由在役服务建好,缺表就响亮失败。
    延迟导入:让模块自身可在无 psycopg 环境下 import(SQLite 部署)。"""
    root_dir = Path(__file__).resolve().parents[3]
    if dialect == "postgresql":
        from app.repositories.postgres.database import PostgresDatabase

        return PostgresDatabase(settings, root_dir)
    from app.repositories.sqlite.database import SqliteDatabase

    return SqliteDatabase(settings, root_dir)


def _print_disk_findings(disk_orphans: dict[str, list[str]], *, applied: bool) -> None:
    label_prefix = "残余孤儿目录" if applied else "孤儿目录"
    for root, ids in disk_orphans.items():
        label = f"{label_prefix} {root}: {len(ids)}"
        print(label if not ids else f"{label}  {' '.join(ids)}")


def _print_disk_report(disk_report: DiskSweepReport) -> None:
    for root, ids in disk_report.removed.items():
        if ids:
            print(f"已清目录 {root}: {' '.join(sorted(ids))}")
    for notebook_id, reason in disk_report.skipped:
        print(f"跳过 {notebook_id}: {reason}")
    for path in disk_report.failed_paths:
        print(f"删除失败(留在原地): {path}")


def main(argv: "list[str] | None" = None) -> int:
    """0=完成(inspect,或 apply 后无跳过/无残留);1=apply 有跳过、删不掉的
    路径或残余孤儿行;2=参数拒绝。绝不打印数据库 URL。"""
    import argparse

    from app.core.config import Settings
    from app.core.database_url import database_identity

    parser = argparse.ArgumentParser(
        prog="sweep_legacy_delete_leftovers",
        description="存量删除残渣离线清扫(默认只读 inspect,--apply 才动手)",
    )
    parser.add_argument("--apply", action="store_true", help="执行清扫(默认只报数)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--rows-only", action="store_true", help="只清孤儿行")
    parser.add_argument("--disk-only", action="store_true", help="只清孤儿目录")
    parser.add_argument(
        "--min-age-seconds", type=int, default=None,
        help="直删根(notebooks/assets)的年龄闸;inode 时间戳距今更近的跳过。"
        "默认取 NOTEBOOK_COPY_STALE_SECONDS;低于该窗口(含 0)须搭配 "
        "--confirm-service-stopped",
    )
    parser.add_argument(
        "--confirm-service-stopped", action="store_true",
        help="确认在役服务已停止(batch_ingest 同款停服契约)。年龄闸低于 "
        "NOTEBOOK_COPY_STALE_SECONDS 时,「不与在途拷贝抢目录」只有停服才成立"
        "(codex #666 R3 P1),必须显式确认",
    )
    parser.add_argument(
        "--statement-timeout-seconds", type=int, default=3600,
        help="PostgreSQL 语句超时覆写(离线大扫描;默认 3600)",
    )
    args = parser.parse_args(argv)
    if args.batch_size < 1 or args.statement_timeout_seconds < 1:
        parser.error("--batch-size/--statement-timeout-seconds 必须为正")
    if args.min_age_seconds is not None and args.min_age_seconds < 0:
        parser.error("--min-age-seconds 不得为负")
    if args.rows_only and args.disk_only:
        parser.error("--rows-only 与 --disk-only 互斥")

    settings = Settings()
    dialect = database_identity(settings.database_url).scheme
    if dialect == "postgresql":
        settings = settings.model_copy(
            update={
                "postgres_statement_timeout_seconds": args.statement_timeout_seconds
            }
        )
    copy_stale_floor = max(1, settings.notebook_copy_stale_seconds)
    min_age = (
        args.min_age_seconds
        if args.min_age_seconds is not None
        else copy_stale_floor
    )
    if min_age < copy_stale_floor and not args.confirm_service_stopped:
        parser.error(
            "--min-age-seconds 低于 NOTEBOOK_COPY_STALE_SECONDS"
            f"({copy_stale_floor}s)会让在途拷贝失去年龄闸保护;"
            "只有停服时才允许,须搭配 --confirm-service-stopped"
        )
    database = _open_database(settings, dialect)
    storage = Path(str(settings.storage_dir))
    do_rows = not args.disk_only
    do_disk = not args.rows_only
    try:
        if args.apply:
            disk_report: DiskSweepReport | None = None
            disk_residual: dict[str, list[str]] = {}
            if do_rows:
                sweep_orphan_rows(
                    database,
                    dialect,
                    batch_size=args.batch_size,
                    on_table_done=lambda table, n: print(f"已删孤儿行 {table}: {n}"),
                )
            if do_disk:
                disk_report = sweep_orphan_disk(
                    database, dialect, storage, min_age_seconds=min_age
                )
                _print_disk_report(disk_report)
                disk_residual = find_orphan_disk(database, dialect, storage)
                _print_disk_findings(disk_residual, applied=True)
            residual = count_orphan_rows(database) if do_rows else {}
            leftover_rows = sum(residual.values())
            if leftover_rows:
                print(f"残余孤儿行(并发新增或删除失败): {residual}")
            # codex #666 R2 P2:盘面残余复核不只打印——快照之后才冒出来的
            # 孤儿目录(如清扫期间崩溃的在途拷贝)同样计入退出码,自动化
            # 不得把没扫干净当成功。
            leftover_dirs = sum(len(ids) for ids in disk_residual.values())
            clean = (
                leftover_rows == 0
                and leftover_dirs == 0
                and (disk_report is None or disk_report.clean)
            )
            return 0 if clean else 1
        if do_rows:
            for table, count in count_orphan_rows(database).items():
                print(f"孤儿行 {table}: {count}")
        if do_disk:
            _print_disk_findings(
                find_orphan_disk(database, dialect, storage), applied=False
            )
        return 0
    finally:
        close = getattr(database, "close", None) or getattr(
            database, "close_local", None
        )
        if close is not None:
            close()

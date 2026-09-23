"""Operator CLI for cross-environment notebook sync
(docs/incremental-sync-design.md §8).

Five subcommand groups:

- ``export`` calls :func:`app.migration.sync.export.export_notebooks`.
- ``import`` calls :func:`app.migration.sync.import_.import_package`.
- ``status`` reads the adapter-internal bookkeeping tables
  (``sync_export_state``, ``sync_imports``) those two engines maintain, plus
  the capture section below -- through :class:`app.migration.sync.database._Source`,
  same as every other subcommand in this module. It used to hand-roll its own
  third PostgreSQL/SQLite branch instead (a debt docs/incremental-sync-design.md
  §11 tracked); that branch is gone as of this module version.
- ``capture enable|disable|status`` reads and writes ``sync_capture_control``/
  ``sync_change_log`` directly, through :class:`app.migration.sync.database._Source`
  for backend dispatch -- this is one of the two pieces of sync logic that live in
  this module rather than being a thin wrapper over ``export.py``/
  ``import_.py``: ``enable``/``disable``'s transaction (the gate flip, and
  the idempotent-vs-first-time clearing of ``sync_export_state``/
  ``sync_change_log``) is implemented here (see ``_capture_enable``/
  ``_capture_disable``), because neither engine owns the capture control
  tables.
- ``prune-log`` (see ``_prune_log``) is the other piece implemented directly
  here, for the same reason: log retention is not owned by ``export.py`` or
  ``import_.py`` either. Everything else in this module stays argument
  parsing, exit codes, and human/JSON report rendering only.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.core.config import Settings
from app.migration.sync.database import SyncExportError, _Source
from app.migration.sync.export import ExportReport, export_notebooks
from app.migration.sync.import_ import (
    SyncImportError,
    _STATUS_DONE,
    _moment,
    _PriorImport,
    _read_moment,
    import_package,
)
from app.migration.sync.package import MODE_INCREMENTAL
from app.repositories.sqlite.database import SqliteDatabase

ROOT_DIR = Path(__file__).resolve().parents[4]

_SYNC_TABLES = ("sync_export_state", "sync_imports")
_CAPTURE_TABLES = ("sync_capture_control", "sync_change_log")


class SyncStatusError(RuntimeError):
    """``status`` could not read the adapter-internal bookkeeping tables --
    distinct from a generic query failure so a database that predates
    v83/0063_sync_control_tables.sql gets a named diagnosis instead of a raw
    "no such table"/"relation does not exist" driver error."""


class SyncCaptureError(RuntimeError):
    """A ``sync capture`` subcommand could not read or write the change-
    capture control tables -- distinct from a generic query failure so a
    database that predates v84/0064_sync_change_capture.sql gets a named
    diagnosis instead of a raw "no such table"/"relation does not exist"
    driver error, the same reasoning as ``SyncStatusError`` for the older
    v83/0063 tables.

    ``missing_tables`` carries the same information ``str(self)`` already
    prose-describes, structured -- so a caller that wants to degrade
    gracefully instead of failing outright (``_cmd_status``'s capture
    section, see below) does not have to parse the message back apart.
    """

    def __init__(
        self, message: str, *, missing_tables: frozenset[str] = frozenset()
    ) -> None:
        super().__init__(message)
        self.missing_tables = missing_tables


class SyncSchemaColumnsError(RuntimeError):
    """The v85/0065 schema shape a caller needs is not (fully) there yet --
    either a COLUMN a later migration adds to a table that already existed
    (``sync_export_state.captured``/``exported_snapshot``), or ``sync_export_runs``,
    the WHOLE table v85/0065 added for the export lease (docs/incremental-
    sync-design.md §7 "控制行门"/"导出水位"). Distinct from
    :class:`SyncStatusError`/:class:`SyncCaptureError` (the older v83/0063/
    v84/0064 table-existence guards) because those do not catch either gap:
    a v83 or v84 database has ``sync_export_state`` and fails only the column
    check, or has neither v85 addition and fails both -- either way the raw
    "no such column"/"no such table" (or PostgreSQL's "column/relation ...
    does not exist") driver error is what a caller would see without this,
    so both gaps are folded into ONE diagnosis here rather than one
    exception per gap.

    ``missing_columns``/``missing_tables`` mirror :class:`SyncCaptureError`'s
    ``missing_tables`` for the same reason: a caller that wants to degrade
    gracefully (``sync status``'s watermark/lease sections, see
    ``_load_sync_status``) reads them back structured instead of parsing the
    message apart.
    """

    def __init__(
        self,
        message: str,
        *,
        missing_columns: frozenset[str] = frozenset(),
        missing_tables: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(message)
        self.missing_columns = missing_columns
        self.missing_tables = missing_tables


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, datetime):
        # SQLite hands back naive text (this deployment's own local
        # bookkeeping is always written as UTC ISO 8601 -- see package.py's
        # timestamp normalization for the row payloads this mirrors); treat
        # a naive value as UTC so both backends serialize to the same
        # instant instead of the sqlite value silently reading as local time.
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _print_json(payload: Any) -> None:
    print(json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True))


def _export_report_as_json(report: ExportReport) -> dict[str, Any]:
    """Adapt :class:`ExportReport` to the same JSON shape as
    ``ImportReport.as_json()`` / the on-disk import report / ``sync_imports.
    report_json`` -- without adding a JSON concern to ``export.py``'s
    dataclass, which has no reader of its own for that shape."""
    return {
        "package_dir": str(report.package_dir),
        "package_id": report.package_id,
        "run_id": report.run_id,
        "notebooks": list(report.notebooks),
        "skipped": dict(sorted(report.skipped.items())),
        "table_counts": dict(sorted(report.table_counts.items())),
        "file_count": report.file_count,
        "bytes_written": report.bytes_written,
        "mode": report.mode,
        "captured": report.captured,
        "captured_through_seq": report.captured_through_seq,
        "from_seq": report.from_seq,
        "to_seq": report.to_seq,
        "base_package_id": report.base_package_id,
        "scoped": report.scoped,
        "watermark_advanced": report.watermark_advanced,
        "deletes": report.deletes,
        "deleted_notebooks": list(report.deleted_notebooks),
        "skipped_mirror_changes": report.skipped_mirror_changes,
        "empty": report.empty,
        "warnings": list(report.warnings),
        "missing_files": list(report.missing_files),
    }


# ------------------------------------------------------------------- export


def _cmd_export(args: argparse.Namespace, settings: Settings) -> int:
    source_env = args.source_env or settings.sync_env
    if not source_env:
        print(
            "sync export: 需要 --source-env，或设置 SILICON_NOTEBOOK_SYNC_ENV "
            "作为本环境的默认同步标识",
            file=sys.stderr,
        )
        return 2
    try:
        report = export_notebooks(
            settings,
            target_env=args.target,
            out_dir=Path(args.out),
            notebook_ids=tuple(args.notebook) if args.notebook else None,
            source_env=source_env,
            full=args.full,
        )
    except SyncExportError as exc:
        print(f"sync export: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        _print_json(_export_report_as_json(report))
        return 0
    print(f"导出包: {report.package_dir}")
    print(f"包 id: {report.package_id}")
    if report.run_id:
        print(f"租约 run_id: {report.run_id}")
    print(f"模式: {report.mode}")
    print(f"序号区间: from_seq={report.from_seq}, to_seq={report.to_seq}")
    if report.base_package_id:
        print(f"续接自包: {report.base_package_id}")
    print(f"本次快照捕获到的变更日志水位: seq={report.captured_through_seq}")
    print(f"本次水位 captured={'true' if report.captured else 'false'}")
    if report.scoped:
        print("范围: 按 --notebook 限定，本次未推进水位")
    elif not report.watermark_advanced:
        print("水位未推进")
    if report.empty:
        print("窗口为空，仍产出空增量包；水位已记录。")
    print(f"笔记本: {len(report.notebooks)} 个")
    if report.deleted_notebooks:
        print(f"已删除的笔记本: {len(report.deleted_notebooks)} 个")
        for notebook_id in report.deleted_notebooks:
            print(f"  {notebook_id}")
    if report.deletes:
        print(f"删除行数: {report.deletes}")
    if report.skipped_mirror_changes:
        print(f"跳过的镜像笔记本变更: {report.skipped_mirror_changes} 条")
    if report.skipped:
        print(f"跳过的笔记本: {len(report.skipped)} 个")
        for notebook_id, reason in sorted(report.skipped.items()):
            print(f"  {notebook_id}: {reason}")
    nonempty_tables = {name: count for name, count in report.table_counts.items() if count}
    print(f"逐表行数（{len(nonempty_tables)} / {len(report.table_counts)} 张表非空）：")
    for table, count in sorted(nonempty_tables.items()):
        print(f"  {table}: {count}")
    print(f"文件数: {report.file_count}")
    print(f"字节数: {report.bytes_written}")
    if report.warnings:
        print("警告:")
        for warning in report.warnings:
            print(f"  {warning}")
    if report.missing_files:
        print(f"缺失文件: {len(report.missing_files)} 个（详情见 --json 输出）")
    return 0


# ------------------------------------------------------------------- import


def _cmd_import(args: argparse.Namespace, settings: Settings) -> int:
    try:
        report = import_package(
            settings,
            Path(args.package_dir),
            create_missing_users=args.create_missing_users,
            dry_run=args.dry_run,
            importer_user_id=args.importer_user,
            resume=args.resume,
            verify_files=args.verify_files,
            take_over=args.take_over,
        )
    except SyncImportError as exc:
        print(f"sync import: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        _print_json(report.as_json())
        return 0
    if report.already_applied:
        print(f"包 {report.package_id}（来自 {report.source_env}）已经引入过，本次未重复执行。")
        return 0
    if report.dry_run:
        print("== 预检模式（--dry-run）：以下是预览，未写入任何数据 ==")
    print(f"包 id: {report.package_id}（来自 {report.source_env}）")
    base_note = f"，base={report.base_package_id}" if report.base_package_id else ""
    print(f"模式: {report.mode}{base_note}")
    print(f"笔记本: {len(report.notebooks)} 个")
    if not report.dry_run:
        # dry-run 从不触达行/文件应用阶段，tables/files_copied 恒为空——打印它们
        # 只会误导成「什么都没有要导」，所以这两行只在真正执行的跑里出现。
        active_tables = {
            name: outcome
            for name, outcome in report.tables.items()
            if outcome.inserted or outcome.updated or outcome.skipped
        }
        print(f"逐表结果（{len(active_tables)} / {len(report.tables)} 张表有变化）：")
        for table, outcome in sorted(active_tables.items()):
            resumed = "，续跑自上次中断" if outcome.resumed else ""
            print(
                f"  {table}: 新增 {outcome.inserted}、更新 {outcome.updated}、"
                f"跳过 {outcome.skipped}{resumed}"
            )
    if report.skipped_row_total:
        print(f"跳过的行: {report.skipped_row_total}（详情见 --json 输出）")
    mapping = report.user_mapping
    print(
        f"用户映射: 匹配 {len(mapping.matched)}、新建 {len(mapping.created)}、"
        f"未匹配 {len(mapping.unmatched)}"
    )
    if report.groups_created:
        label = "将新建组" if report.dry_run else "新建组"
        print(f"{label}: {', '.join(sorted(report.groups_created))}")
    if not report.dry_run:
        print(f"文件数: {report.files_copied}")
    if not report.dry_run and report.mode == MODE_INCREMENTAL:
        # 全量包这几个字段恒为 0/空（§8「导入相位」），dry-run 从不触达删除重放/
        # 笔记本删除传播这两个相位——只在真正执行的增量导入里打印才不误导。
        print(
            f"删除重放: 应用 {report.deletes_applied}、目标端已不存在 "
            f"{report.deletes_absent}、孤儿跳过 {report.deletes_orphan_skipped}、"
            f"随笔记本删除作业整本清理 {report.deletes_folded_into_notebook_deletion}、"
            f"目标端正在拷贝、本次未动 {report.deletes_skipped_for_copying}"
        )
        if report.source_authoritative_collisions_resolved:
            print(
                "记忆修订/来源的唯一键冲突按源端权威消解: "
                f"{report.source_authoritative_collisions_resolved} 条（只替换目标端自建的那条，"
                "源端未动的历史保留）"
            )
        if report.notebooks_deleted:
            print(
                "已排队删除的笔记本: "
                + "、".join(sorted(report.notebooks_deleted))
                + "（由目标端应用的删除作业完成清理）"
            )
        if report.notebooks_delete_skipped:
            print(
                "跳过删除的笔记本（目标端正在拷贝，留给运维在拷贝结束后处理）: "
                + "、".join(sorted(report.notebooks_delete_skipped))
            )
    if report.warnings:
        # 含目标是 SQLite 时「必须停机导入」一类的提示——来自报告本身，这里不再
        # 自行判断后端去重复那个决定。
        print("警告:")
        for warning in report.warnings:
            print(f"  {warning}")
    return 0


# ------------------------------------------------------------------- status


def _missing_sync_tables(conn: Any, *, is_postgres: bool) -> set[str]:
    if is_postgres:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() "
            "AND table_name IN ('sync_export_state', 'sync_imports')"
        ).fetchall()
        found = {str(row["table_name"]) for row in rows}
    else:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('sync_export_state', 'sync_imports')"
        ).fetchall()
        found = {str(row["name"]) for row in rows}
    return set(_SYNC_TABLES) - found


# The two v85/0065 columns every reader of ``sync_export_state`` beyond the
# plain v83/0063 four (``target_env``, ``exported_through_seq``,
# ``exported_at``, ``package_id``) depends on. A set, not a tuple, because
# both callers below only ever need set difference against it.
_EXPORT_STATE_V85_COLUMNS = frozenset({"captured", "exported_snapshot"})


def _missing_export_state_columns(conn: Any, *, is_postgres: bool) -> set[str]:
    """Which of ``_EXPORT_STATE_V85_COLUMNS`` ``sync_export_state`` does NOT
    have -- empty on a v85/0065+ database.

    Column-level, not table-level: ``_missing_sync_tables`` above already
    guards the TABLE (v83/0063); this guards columns two later migrations
    (v85/0065) added to a table that migration never touched otherwise. A
    v83 or v84 database has the table and fails this check, which is exactly
    one of the two gaps ``SyncSchemaColumnsError`` exists to diagnose by name
    instead of letting a raw driver error through the first time a caller
    SELECTs one of these columns.
    """
    if is_postgres:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'sync_export_state' "
            "AND column_name IN ('captured', 'exported_snapshot')"
        ).fetchall()
        found = {str(row["column_name"]) for row in rows}
    else:
        rows = conn.execute("PRAGMA table_info(sync_export_state)").fetchall()
        found = {str(row["name"]) for row in rows}
    return set(_EXPORT_STATE_V85_COLUMNS) - found


def _missing_export_runs_table(conn: Any, *, is_postgres: bool) -> bool:
    """Whether ``sync_export_runs`` -- the v85/0065 export lease table (see
    ``_migration_85``'s own docstring, or PostgreSQL's
    ``0065_sync_export_snapshot.sql``) -- is missing. The other v85/0065 gap
    :class:`SyncSchemaColumnsError` diagnoses, alongside
    ``_missing_export_state_columns``: a v83/0063 or v84/0064 database has
    neither, a database migrated only far enough to add the two
    ``sync_export_state`` columns but not this table cannot exist (both land
    in the same migration), so in practice this and the column check always
    agree -- but they are still two independent facts, checked and reported
    independently, so the diagnosis names exactly what is missing rather than
    assuming one implies the other.
    """
    if is_postgres:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = 'sync_export_runs'"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'sync_export_runs'"
        ).fetchall()
    return not rows


def _require_v85_schema(source: _Source, conn: Any) -> None:
    """Raise :class:`SyncSchemaColumnsError` unless ``sync_export_state`` has
    both v85/0065 columns AND ``sync_export_runs`` exists. Callers that must
    not proceed without the full v85/0065 shape (``sync prune-log`` -- see
    ``_prune_log_bounds``, which reads both the columns and the lease table)
    use this; ``sync status`` does not, because it degrades each section
    independently instead of refusing the whole command (see
    ``_load_sync_status``)."""
    missing_columns = _missing_export_state_columns(conn, is_postgres=source.is_postgres)
    missing_runs_table = _missing_export_runs_table(conn, is_postgres=source.is_postgres)
    if not missing_columns and not missing_runs_table:
        return
    missing_tables = {"sync_export_runs"} if missing_runs_table else set()
    detail_parts = []
    if missing_columns:
        detail_parts.append("缺列: " + "、".join(sorted(missing_columns)))
    if missing_tables:
        detail_parts.append("缺表: " + "、".join(sorted(missing_tables)))
    raise SyncSchemaColumnsError(
        "本环境尚未迁移到 v85/0065（" + "，".join(detail_parts) + "）；"
        "先启动一次后端完成 schema 迁移，或手动执行相应迁移脚本，再重试。",
        missing_columns=frozenset(missing_columns),
        missing_tables=frozenset(missing_tables),
    )


# How stale an ``sync_export_runs`` lease's heartbeat may be before it is
# treated as DEAD -- left by a run that crashed rather than one still going.
# Matches ``_migration_85``'s own docstring ("A row whose heartbeat_at is
# more than an hour old is a DEAD lease"); kept here rather than imported
# from export.py because this module's import whitelist (guarded by
# tests/test_sync_manifest.py) keeps it from reaching into that module's
# internals, and because the rule is a CLI-facing policy in its own right
# (prune-log's bound, status's display) rather than an export-writer detail.
_LEASE_DEAD_AFTER_SECONDS = 3600


def _load_export_leases(source: _Source, conn: Any) -> list[dict[str, Any]]:
    """Every ``sync_export_runs`` row, each annotated with ``dead`` (bool):
    whether its ``heartbeat_at`` is more than ``_LEASE_DEAD_AFTER_SECONDS``
    old, or unparseable (treated as dead -- a lease this module cannot even
    read the age of must not be trusted as live).

    Callers decide what to DO with dead rows (``_prune_log_bounds`` ignores
    them for the seq floor but still names them; ``_load_sync_status``
    displays every row, dead or not) -- this function only classifies.
    """
    rows = source.fetch(
        conn,
        "SELECT target_env, run_id, package_id, started_at, heartbeat_at, "
        "floor_seq, floor_xmin FROM sync_export_runs ORDER BY target_env",
    )
    now = datetime.now(timezone.utc)
    for row in rows:
        heartbeat = _read_moment(row.get("heartbeat_at"))
        row["dead"] = (
            heartbeat is None
            or (now - heartbeat).total_seconds() > _LEASE_DEAD_AFTER_SECONDS
        )
    return rows


def _chain_candidate_sort_key(row: _PriorImport) -> tuple[datetime, int, str]:
    """Deterministic ordering for chain-head candidates: ``(created_at,
    to_seq, package_id)``.

    ``created_at`` leads because that is what the chain rule itself orders by
    (:meth:`_PriorImport.downstream_of`) -- ``to_seq`` only orders packages
    WITHIN one capture epoch and says nothing across a ``sync capture
    disable``/``enable`` reset (codex #788 r2 P1). Rows can still tie (an
    undated row falls back to the epoch; two packages can share a second), so
    ``package_id`` breaks it and guarantees a total order regardless of which
    physical row order the caller built ``prior`` from (neither
    ``sync_imports`` nor the in-memory grouping below carries an ORDER BY of
    its own)."""
    created_at = row.created_at or datetime.min.replace(tzinfo=timezone.utc)
    return (created_at, row.to_seq, row.package_id)


def _chain_heads_for(prior: Sequence[_PriorImport]) -> list[_PriorImport]:
    """Every ``done`` row for this ``source_env`` that nothing else is
    downstream of -- the same "done, and nothing downstream of it" rule
    ``import_.py``'s ``_assert_chain`` enforces (design doc §8), read back
    here for display rather than re-derived: reuses
    :meth:`_PriorImport.downstream_of`, the actual comparison rule, instead
    of re-deriving it.

    **Usually returns one row. Can legitimately return more than one**
    (codex review round 2, P2-1): a full baseline and a later
    ``--notebook``-scoped import, or a gate-closed full import, can share the
    same watermark position without either being downstream of the other
    (neither of those two shapes advances the source's own watermark, so
    the chain rules never treat them as continuing anything -- see the
    design doc's §8 "已知边界"). The OLD version of this function returned
    the first undominated row it found while walking ``prior`` in whatever
    order the caller passed it in -- silently non-deterministic whenever more
    than one existed, since neither ``sync_imports`` nor
    ``_load_sync_status``'s in-memory grouping carries an ORDER BY. This
    version computes the FULL undominated set and lets the caller decide what
    "ambiguous" means for its output, rather than guessing on this function's
    behalf.

The narrowing that keeps this cheap on a ``source_env`` with a long
    ``done`` history follows the rule rather than the watermark (codex #788
    r2 P1 -- it used to narrow by maximum ``to_seq``, which stopped being
    sound the moment ``downstream_of`` stopped comparing heights). Let P be
    the NEWEST row that participates in the chain
    (:attr:`_PriorImport.participates_in_chain`). Every row exported before P
    has P downstream of it and can never be a head, so only rows at or after
    P's ``created_at`` need the pairwise comparison -- O(n) to find P plus
    O(k^2) over the usually tiny tail, instead of O(n^2) over every ``done``
    row. Two shapes sit outside that argument and are added back explicitly:
    an UNDATED row (nothing is downstream of it and it is downstream of
    nothing, so it is always a head), and the case where NO row participates
    at all (every ``done`` row is then a head).

    Returned sorted by ``_chain_candidate_sort_key`` -- deterministic
    regardless of the order ``prior`` arrived in, so two callers building the
    same set from rows fetched in a different physical order still print or
    serialize it identically (the reason the OLD version's row-order
    dependence was a real bug and not just a style nit).
    """
    done = [row for row in prior if row.status == _STATUS_DONE]
    if not done:
        return []
    undated = [row for row in done if row.created_at is None]
    dated = [row for row in done if row.created_at is not None]
    participating = [row for row in dated if row.participates_in_chain]
    if participating:
        floor = max(row.created_at for row in participating)
        candidates = [
            row for row in dated if row.created_at >= floor
        ] + undated
    else:
        candidates = done
    heads = [
        candidate
        for candidate in candidates
        if not any(
            other.package_id != candidate.package_id and other.downstream_of(candidate)
            for other in candidates
        )
    ]
    return sorted(heads, key=_chain_candidate_sort_key)


def _pending_notebook_deletes(source: _Source, conn: Any) -> dict[str, int]:
    """How many mirrored notebooks (non-empty ``sync_origin``) are sitting in
    ``status='deleting'`` per source environment, grouped by ``sync_origin``.

    An incremental import that propagates a source-side notebook deletion
    (design doc §8 "笔记本删除传播") only flips the row to ``deleting`` and
    queues a ``notebook_delete_jobs`` row -- the actual cleanup runs later, in
    the target application's own delete-job worker. This count is how an
    operator notices a notebook stuck waiting on that worker rather than
    assuming the import itself failed.
    """
    rows = source.fetch(
        conn,
        "SELECT sync_origin, COUNT(*) AS pending FROM notebooks "
        "WHERE status = 'deleting' AND sync_origin <> '' GROUP BY sync_origin",
    )
    return {str(row["sync_origin"]): int(row["pending"]) for row in rows}


def _load_sync_status(settings: Settings) -> dict[str, Any]:
    """Read ``sync_export_state``/``sync_imports`` on whichever backend
    ``settings.database_url`` names.

    Every statement issued here is a plain ``SELECT`` -- nothing is written.
    Goes through :class:`app.migration.sync.database._Source`, the same
    facade ``_load_capture_status`` below uses: this function used to
    hand-roll its own third PostgreSQL/SQLite branch instead, a duplication
    docs/incremental-sync-design.md §11 tracked as a TODO for this PR to fix,
    which is what this version does. ``source.read()`` opens PostgreSQL's
    ``REPEATABLE READ, READ ONLY`` / SQLite's deferred ``BEGIN`` the same way
    ``_load_capture_status`` does; a short read-only status query does not
    need that consistency any more than the capture section already sitting
    next to it does, so there is no reason for the two to differ.

    The v83/0063 TABLE check (``_missing_sync_tables``) still raises
    ``SyncStatusError``, same as before: no table at all means this section
    has nothing whatsoever to show. Missing v85/0065 COLUMNS on that table --
    a v83 or v84 database -- is different: the four v83 columns still read
    fine, so this DEGRADES (each export row carries only those four; no
    ``captured``/``exported_snapshot`` keys) rather than failing the whole
    command the way a raw "no such column" error would, matching the
    ``sync capture`` section's own already-established degrade-not-fail
    stance on a v84-missing database (``_capture_section_for_status``). The
    returned dict's ``exports_note`` names the gap when degraded, ``None``
    otherwise, so both renderers below (and any JSON consumer) can tell the
    two shapes apart without inspecting individual rows. ``sync_export_runs``
    (the v85/0065 export lease -- docs/incremental-sync-design.md §7 "控制行
    门"/"导出水位") degrades the SAME way, independently, via ``runs``/
    ``runs_note``: a v83/v84 database has neither the columns nor this
    table, but the two are still checked and reported separately rather than
    one implying the other.
    """
    exports_sql_v85 = (
        "SELECT target_env, exported_through_seq, exported_at, package_id, "
        "captured, exported_snapshot FROM sync_export_state ORDER BY target_env"
    )
    exports_sql_v83 = (
        "SELECT target_env, exported_through_seq, exported_at, package_id "
        "FROM sync_export_state ORDER BY target_env"
    )
    imports_sql = (
        "SELECT package_id, source_env, status, started_at, finished_at, "
        "from_seq, to_seq, report_json FROM sync_imports ORDER BY started_at DESC"
    )
    source = _Source(settings, ROOT_DIR)
    try:
        with source.read() as conn:
            missing_tables = _missing_sync_tables(conn, is_postgres=source.is_postgres)
            if missing_tables:
                raise SyncStatusError(
                    "本环境尚未迁移到 v83/0063（缺表: " + "、".join(sorted(missing_tables)) + "）；"
                    "先启动一次后端完成 schema 迁移，或手动执行相应迁移脚本，再重试。"
                )
            missing_columns = _missing_export_state_columns(
                conn, is_postgres=source.is_postgres
            )
            if missing_columns:
                exports = source.fetch(conn, exports_sql_v83)
                exports_note = (
                    "captured/snapshot 需 v85/0065（缺列: "
                    + "、".join(sorted(missing_columns))
                    + "）；先启动一次后端完成 schema 迁移可看到完整信息。"
                )
            else:
                exports = source.fetch(conn, exports_sql_v85)
                for row in exports:
                    row["captured"] = bool(row["captured"])
                exports_note = None
            if _missing_export_runs_table(conn, is_postgres=source.is_postgres):
                runs = []
                runs_note = (
                    "在途导出需 v85/0065（缺表: sync_export_runs）；"
                    "先启动一次后端完成 schema 迁移可看到在途导出。"
                )
            else:
                runs = _load_export_leases(source, conn)
                runs_note = None
            imports = source.fetch(conn, imports_sql)
            pending_notebook_deletes = _pending_notebook_deletes(source, conn)
    finally:
        source.close()
    for row in imports:
        report = row.get("report_json")
        if isinstance(report, str):
            try:
                report = json.loads(report)
            except (TypeError, ValueError):
                report = {}
        row["report_json"] = report or {}
        row["notebooks"] = len(row["report_json"].get("notebooks", []) or [])
    # Chain heads are computed here, off the ALREADY-FETCHED `imports` rows
    # (report_json now parsed by the loop above), rather than by calling back
    # into `_prior_imports` once per source_env inside the read transaction.
    # That used to mean one extra `sync_imports` query AND one extra
    # `SELECT DISTINCT package_id FROM sync_import_progress` query per
    # source_env; `_load_sync_status` already has every `sync_imports` column
    # `_prior_imports` reads (this function's own `imports_sql` above now
    # also selects `from_seq`/`to_seq`) and never needed `has_progress` in the
    # first place, so building the `_PriorImport` rows locally, grouped by
    # source_env, drops both redundant round trips to zero rather than just
    # hoisting the progress query once (codex review round 2, P2-3).
    prior_by_source_env: dict[str, list[_PriorImport]] = {}
    for row in imports:
        prior_by_source_env.setdefault(str(row["source_env"]), []).append(
            _PriorImport(
                package_id=str(row["package_id"]),
                status=str(row["status"]),
                started_at=_read_moment(row.get("started_at")),
                created_at=_read_moment(row["report_json"].get("package_created_at")),
                heartbeat_at=None,
                has_progress=False,
                notebooks=None,
                from_seq=int(row.get("from_seq") or 0),
                to_seq=int(row.get("to_seq") or 0),
                base_package_id=str(row["report_json"].get("base_package_id") or ""),
                scoped=(
                    bool(row["report_json"]["scoped"])
                    if isinstance(row["report_json"].get("scoped"), bool)
                    else None
                ),
            )
        )
    chain_heads: dict[str, list[dict[str, Any]]] = {
        source_env: [
            {
                "package_id": head.package_id,
                "to_seq": head.to_seq,
                "created_at": head.created_at,
            }
            for head in _chain_heads_for(prior)
        ]
        for source_env, prior in prior_by_source_env.items()
    }
    return {
        "exports": exports,
        "exports_note": exports_note,
        "runs": runs,
        "runs_note": runs_note,
        "imports": imports,
        "chain_heads": chain_heads,
        "pending_notebook_deletes": pending_notebook_deletes,
    }


def _cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    state = _load_sync_status(settings)
    # Same read function `sync capture status` uses under a healthy read --
    # see _load_capture_status's docstring -- but degraded rather than
    # raised on a v84/0064 capture-tables-missing database: the export
    # watermark and import sections above already read fine off the v83
    # tables, and one missing section must not turn this whole command into
    # a hard failure. See _capture_section_for_status's docstring.
    state["capture"] = _capture_section_for_status(settings, exact=args.exact_count)
    if args.as_json:
        _print_json(state)
        return 0
    print("导出水位（本环境作为源）：")
    if state.get("exports_note"):
        print(f"  （{state['exports_note']}）")
    if not state["exports"]:
        print("  （无）")
    for row in state["exports"]:
        if "captured" not in row:
            # Degraded shape (see _load_sync_status's docstring): only the
            # four v83/0063 columns are available, exports_note above
            # already explained why.
            print(
                f"  -> {row['target_env']}: seq={row['exported_through_seq']} "
                f"于 {row['exported_at']}（包 {row['package_id']}）"
            )
            continue
        snapshot_note = ""
        if row["exported_snapshot"]:
            try:
                xmin = _Source.snapshot_xmin(str(row["exported_snapshot"]))
                snapshot_note = f"，snapshot xmin={xmin}"
            except SyncExportError:
                # A malformed exported_snapshot (should never happen -- see
                # _prune_log_bounds's own guard against it) must not abort
                # the rest of `status`; one row's unreadable snapshot is a
                # one-line hint, not a reason to hide every other row.
                snapshot_note = "，snapshot 解析失败"
        captured_note = "true" if row["captured"] else "false"
        print(
            f"  -> {row['target_env']}: seq={row['exported_through_seq']} "
            f"于 {row['exported_at']}（包 {row['package_id']}，captured="
            f"{captured_note}{snapshot_note}）"
        )
    print("在途导出（本环境作为源）：")
    if state.get("runs_note"):
        print(f"  （{state['runs_note']}）")
    if not state["runs"]:
        print("  （无）")
    for row in state["runs"]:
        dead_note = "，已死" if row["dead"] else ""
        xmin_note = (
            f"，floor_xmin={row['floor_xmin']}" if row.get("floor_xmin") is not None else ""
        )
        print(
            f"  -> {row['target_env']}: floor_seq={row['floor_seq']}{xmin_note} "
            f"开始于 {row['started_at']}，心跳于 {row['heartbeat_at']}{dead_note}"
        )
    print("已引入的包（本环境作为目标）：")
    if not state["imports"]:
        print("  （无）")
    for row in state["imports"]:
        finished_at = row["finished_at"] if row["finished_at"] is not None else "-"
        suffix = ""
        if row["status"] == "superseded":
            superseded_by = row["report_json"].get("superseded_by")
            if superseded_by:
                suffix = f"（被 {superseded_by} 取代）"
        elif row["status"] == "running":
            heartbeat_at = row["report_json"].get("heartbeat_at") or "-"
            suffix = f"，心跳: {heartbeat_at}"
        print(
            f"  {row['package_id']} 来自 {row['source_env']}：{row['status']}，"
            f"{row['notebooks']} 个笔记本，开始于 {row['started_at']}，"
            f"结束于 {finished_at}{suffix}"
        )
    print("导入链头（本环境作为目标；下一个增量包的 base_package_id 应等于它）：")
    chain_heads = state["chain_heads"]
    pending_deletes = state["pending_notebook_deletes"]
    source_envs = sorted(set(chain_heads) | set(pending_deletes))
    if not source_envs:
        print("  （无）")
    for source_env in source_envs:
        heads = chain_heads.get(source_env) or []
        if not heads:
            head_desc = "尚无入链的已完成包（下一个包只能是 mode=full）"
        else:
            head_desc = "、".join(
                f"{head['package_id']}（to_seq={head['to_seq']}，创建于 "
                f"{head['created_at'].isoformat() if head['created_at'] is not None else '-'}）"
                for head in heads
            )
            if len(heads) > 1:
                # More than one done package that neither has moved past the
                # other -- print all of them rather than silently pick one
                # (codex review round 2, P2-1); see _chain_heads_for's
                # docstring for how this can legitimately happen.
                head_desc += (
                    "——链头不唯一；源端下一个窗口的 base 会是其中推进过水位的那个"
                    "（to_seq 最大且 base 非空或 to_seq>0）"
                )
        pending = pending_deletes.get(source_env, 0)
        pending_note = (
            f"，等待删除作业清理的镜像 {pending} 个（由目标端应用的删除作业完成）"
            if pending
            else ""
        )
        print(f"  -> {source_env}: {head_desc}{pending_note}")
    _print_capture_section(state["capture"])
    return 0


# ------------------------------------------------------------------ capture


def _missing_capture_tables(conn: Any, *, is_postgres: bool) -> set[str]:
    in_list = ", ".join(f"'{name}'" for name in _CAPTURE_TABLES)
    if is_postgres:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() "
            f"AND table_name IN ({in_list})"
        ).fetchall()
        found = {str(row["table_name"]) for row in rows}
    else:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            f"AND name IN ({in_list})"
        ).fetchall()
        found = {str(row["name"]) for row in rows}
    return set(_CAPTURE_TABLES) - found


def _require_capture_tables(source: _Source, conn: Any) -> None:
    missing = _missing_capture_tables(conn, is_postgres=source.is_postgres)
    if missing:
        raise SyncCaptureError(
            "本环境尚未迁移到 v84/0064（缺表: " + "、".join(sorted(missing)) + "）；"
            "先启动一次后端完成 schema 迁移，或手动执行相应迁移脚本，再重试。",
            missing_tables=frozenset(missing),
        )


def _capture_control_row(source: _Source, conn: Any) -> dict[str, Any] | None:
    """The one ``sync_capture_control`` row, or ``None`` when the migration
    left it unseeded (its own docstring: an absent row reads exactly like a
    disabled one).

    Read-only: a plain, unlocked ``SELECT``. Used by the status paths
    (``sync status``/``sync capture status``), which only ever read.
    ``_capture_enable``/``_capture_disable`` do NOT use this -- they need to
    both read the current state and act on it atomically against a
    concurrent enable/disable, which a separate read-then-write pair cannot
    guarantee (see their own docstrings for the single-statement
    ``RETURNING``-based mechanism that replaces it).
    """
    rows = source.fetch(
        conn,
        "SELECT enabled, enabled_at, disabled_at FROM sync_capture_control "
        "WHERE singleton = 1",
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "enabled": bool(row["enabled"]),
        "enabled_at": row["enabled_at"],
        "disabled_at": row["disabled_at"],
    }


_CAPTURE_LOG_RANGE_SQL = (
    "SELECT (SELECT MIN(seq) FROM sync_change_log) AS min_seq, "
    "(SELECT MAX(seq) FROM sync_change_log) AS max_seq"
)


def _capture_log_range(source: _Source, conn: Any) -> dict[str, Any]:
    """``{"min_seq", "max_seq"}``, ``None``/``None`` on an empty log.

    Two scalar subqueries, deliberately NOT ``SELECT MIN(seq), MAX(seq)`` in
    one aggregate: SQLite only applies its min/max index optimization to a
    query whose single aggregate is one MIN or one MAX, so the combined form
    is a full ``SCAN sync_change_log`` (codex #783 r1). As two scalar
    subqueries each endpoint is one primary-key probe on both backends
    (PostgreSQL rewrites each into an Index Only Scan + LIMIT 1).
    ``_CAPTURE_LOG_RANGE_SQL`` is shared with the test that pins the SQLite
    query plan."""
    row = source.fetch(conn, _CAPTURE_LOG_RANGE_SQL)[0]
    return {"min_seq": row["min_seq"], "max_seq": row["max_seq"]}


def _capture_log_count_approx(
    source: _Source, conn: Any, log_range: Mapping[str, Any]
) -> int:
    """A cheap, non-exact row count for the log -- never a scan of the table.

    PostgreSQL: ``pg_class.reltuples``, the planner's own row-count estimate
    (refreshed by autovacuum/``ANALYZE``, not by this read). It can lag
    right after a bulk change (e.g. a ``sync capture disable`` that just
    deleted every row) until the next autovacuum run; that lag is the
    tradeoff for never scanning.
    SQLite has no equivalent statistics catalog this cheap: ``MAX(seq) -
    MIN(seq) + 1`` is used instead, an upper bound on the true row count
    (``seq`` never repeats; the only deleters are ``sync capture disable``,
    which empties the log, and ``sync prune-log``, which removes a
    contiguous prefix so MIN(seq) simply moves up -- either way the bound
    stays exact in the common case and can only ever over-count, never
    under-count).
    """
    if source.is_postgres:
        row = source.fetch(
            conn,
            "SELECT reltuples FROM pg_class WHERE oid = 'sync_change_log'::regclass",
        )[0]
        return max(int(row["reltuples"]), 0)
    return int(log_range["max_seq"]) - int(log_range["min_seq"]) + 1


def _capture_log_count_exact(source: _Source, conn: Any) -> int:
    return int(
        source.fetch(conn, "SELECT COUNT(*) AS n FROM sync_change_log")[0]["n"]
    )


def _capture_log_snapshot(
    source: _Source, conn: Any, *, exact: bool = False
) -> dict[str, Any]:
    """``{"min_seq", "max_seq", "log_rows", "log_rows_exact"}``.

    Cheap by default: MIN/MAX (index probes, see ``_capture_log_range``)
    plus an approximate count that never scans the table
    (``_capture_log_count_approx``). ``exact=True`` (``--exact-count``)
    additionally pays for a real ``COUNT(*)`` -- a full scan of the log,
    which on a deployment that has run change capture for a while can be
    the single most expensive thing ``sync status``/``sync capture status``
    do, which is why it is opt-in rather than the default. An empty log
    (``min_seq is None``) never needs either count: 0 is exact regardless.
    """
    log_range = _capture_log_range(source, conn)
    if log_range["min_seq"] is None:
        return {**log_range, "log_rows": 0, "log_rows_exact": True}
    if exact:
        return {
            **log_range,
            "log_rows": _capture_log_count_exact(source, conn),
            "log_rows_exact": True,
        }
    return {
        **log_range,
        "log_rows": _capture_log_count_approx(source, conn, log_range),
        "log_rows_exact": False,
    }


def _capture_status_payload(
    source: _Source, conn: Any, *, exact: bool = False
) -> dict[str, Any]:
    """One shape, shared by ``sync capture status`` and the capture section
    ``sync status`` appends to its own output -- see ``_load_capture_status``.
    """
    control = _capture_control_row(source, conn)
    return {
        "enabled": control["enabled"] if control else False,
        "enabled_at": control["enabled_at"] if control else None,
        "disabled_at": control["disabled_at"] if control else None,
        **_capture_log_snapshot(source, conn, exact=exact),
    }


def _load_capture_status(settings: Settings, *, exact: bool = False) -> dict[str, Any]:
    """Open a read connection over whichever backend ``settings`` names and
    return ``_capture_status_payload``'s shape.

    The one function both ``sync capture status`` and the capture section of
    ``sync status`` call: there is exactly one place that decides what
    "capture status" means, the same reasoning ``_load_sync_status`` follows
    for the export/import bookkeeping tables.
    """
    source = _Source(settings, ROOT_DIR)
    try:
        with source.read() as conn:
            _require_capture_tables(source, conn)
            return _capture_status_payload(source, conn, exact=exact)
    finally:
        source.close()


def _format_log_range(payload: Mapping[str, Any]) -> str:
    """``"0 行"`` or ``"N 行（seq A–B）"`` (``"约 N 行..."`` when
    ``log_rows_exact`` is false) -- shared by every place that prints
    ``log_rows``/``min_seq``/``max_seq`` so an empty log never prints a
    literal ``None`` seq and an approximate count is never shown as if it
    were exact."""
    if not payload["log_rows"]:
        return "0 行"
    prefix = "" if payload.get("log_rows_exact", True) else "约 "
    return f"{prefix}{payload['log_rows']} 行（seq {payload['min_seq']}–{payload['max_seq']}）"


def _print_capture_status(capture: Mapping[str, Any]) -> None:
    print("变更捕获：")
    if capture["enabled"]:
        print(f"  开启，自 {capture['enabled_at']} 起")
    else:
        state = "从未开启过" if capture["enabled_at"] is None else f"已关闭，于 {capture['disabled_at']}"
        print(f"  关闭（{state}）")
    print(f"  变更日志: {_format_log_range(capture)}")


def _capture_section_for_status(
    settings: Settings, *, exact: bool = False
) -> dict[str, Any]:
    """The capture section ``sync status`` (not ``sync capture status``)
    appends to its own output.

    Unlike ``sync capture status``, which exits 2 when the v84/0064 capture
    tables are missing -- an operator asking specifically about capture
    wants that failure -- ``sync status`` degrades instead: it exists to
    show whatever it CAN read (export watermarks, imported packages) even on
    a database that has not been migrated past v83/0063 yet, and one missing
    section must not turn that whole, otherwise-successful read into a
    hard failure. On a healthy read this returns ``_load_capture_status``'s
    shape unchanged; on a missing-tables read it returns a small diagnostic
    object instead (``missing_tables`` + the same prose ``detail`` the
    raised ``SyncCaptureError`` carries) that the human/JSON renderers below
    both know how to recognize.
    """
    try:
        return _load_capture_status(settings, exact=exact)
    except SyncCaptureError as exc:
        return {"missing_tables": sorted(exc.missing_tables), "detail": str(exc)}


def _print_capture_section(capture: Mapping[str, Any]) -> None:
    if "missing_tables" in capture:
        print(f"变更捕获：{capture['detail']}")
        return
    _print_capture_status(capture)


def _capture_enable(source: _Source) -> dict[str, Any]:
    """Turn the capture gate on inside one write transaction.

    Idempotent: an already-enabled gate is left exactly as it is --
    ``enabled_at`` does not move and nothing is cleared -- so running
    ``enable`` twice in a row (an operator retrying after an ambiguous exit
    code, say) never re-triggers the consequences below a second time.

    The enabled-vs-idempotent decision is made ATOMICALLY by the first
    statement, not by a separate ``SELECT`` before it: a plain
    read-then-branch has a real race between two concurrent ``enable``
    calls when the control row does not exist yet (the common case -- the
    row is never seeded by the migration) -- both would read "not
    enabled" and both would report a first-time enable, and whichever
    commits last would silently clobber the other's ``enabled_at`` with its
    own ON CONFLICT DO UPDATE. Instead, the ``INSERT ... ON CONFLICT DO
    UPDATE ... RETURNING enabled_at`` below preserves the EXISTING row's
    ``enabled_at`` (referenced by table name, which in an upsert's SET/
    RETURNING clauses means "before this statement", as opposed to
    ``excluded.*`` for "the row this statement proposed") whenever the
    existing row was already enabled, and returns it -- so comparing the
    returned value against this call's own ``moment`` tells the two cases
    apart without ever needing a prior read, on either backend (the SQL is
    portable). Once this statement returns, this transaction holds
    PostgreSQL's row lock (or SQLite's write lock) on the singleton row
    until commit, so the subsequent clears below can never race a second
    enable/disable. ``SqliteDatabase.begin_immediate`` is still taken first
    to close the (much smaller) window between a *cross-process* SQLite
    reader and this transaction's own first write statement, matching the
    convention other guarded writes in this codebase use (see its
    docstring) -- PostgreSQL needs no such extra step because ``write()``
    already opens a real transaction there.

    Turning capture on for the first time invalidates every existing export
    watermark: a package produced before this moment was a full snapshot of
    rows the change log has no record of, so it can never be extended with
    an incremental slice read from the log. ``sync_export_state`` is
    cleared so the next export for every target runs full again and
    establishes a log-backed baseline (docs/incremental-sync-design.md §7).
    ``sync_change_log`` is cleared too -- capture just forced a full
    baseline, so there is nothing in the log worth keeping, and this mops
    up any residual rows a long-running write transaction might commit
    AFTER a previous ``disable`` already ran (``disable`` cannot see or
    clear a row that has not committed yet; the next ``enable`` can, and
    does). This is the asymmetry between the two: ``disable`` clears
    best-effort at the moment it runs, ``enable`` guarantees a clean slate.
    """
    moment = _moment(source, datetime.now(timezone.utc))
    with source.write() as conn:
        if not source.is_postgres:
            SqliteDatabase.begin_immediate(conn)
        _require_capture_tables(source, conn)
        row = source.fetch(
            conn,
            source.sql(
                "INSERT INTO sync_capture_control (singleton, enabled, enabled_at) "
                "VALUES (1, ?, ?) "
                "ON CONFLICT (singleton) DO UPDATE SET "
                "enabled = excluded.enabled, "
                "enabled_at = CASE WHEN sync_capture_control.enabled "
                "THEN sync_capture_control.enabled_at ELSE excluded.enabled_at END "
                "RETURNING enabled_at"
            ),
            (True, moment),
        )[0]
        already_enabled = row["enabled_at"] != moment
        if already_enabled:
            return {
                "already_enabled": True,
                "enabled_at": row["enabled_at"],
                "cleared_export_watermarks": 0,
                "cleared_log_rows": 0,
                **_capture_log_snapshot(source, conn),
            }
        cleared_exports = conn.execute("DELETE FROM sync_export_state").rowcount
        cleared_log = conn.execute("DELETE FROM sync_change_log").rowcount
        return {
            "already_enabled": False,
            "enabled_at": moment,
            "cleared_export_watermarks": cleared_exports,
            "cleared_log_rows": cleared_log,
            "min_seq": None,
            "max_seq": None,
            "log_rows": 0,
            "log_rows_exact": True,
        }


def _capture_disable(source: _Source) -> dict[str, Any]:
    """Turn the capture gate off inside one write transaction.

    Idempotent the same way ``_capture_enable`` is: a gate that is already
    disabled, or was never enabled at all, is left untouched. The already-
    disabled-vs-transitioning decision is made the same atomic,
    single-statement way ``_capture_enable`` makes its own (see that
    docstring for why a read-then-branch is not safe): ``UPDATE ...
    RETURNING disabled_at`` preserves the OLD ``disabled_at`` when the row
    was already disabled and only writes the new one when it was actually
    enabled a moment ago (``sync_capture_control.enabled`` in the CASE
    reads the pre-update value, standard SQL UPDATE semantics on both
    backends). No row matching at all (never enabled -- the common
    starting state) is its own, unambiguous "already disabled" case.

    Disabling clears BOTH ``sync_export_state`` and ``sync_change_log`` --
    best-effort, at the moment this transaction commits: the log stops
    growing from here, so leaving old export watermarks in place would make
    a future re-enable think it can resume an incremental sequence the log
    can no longer back, and leaving old log rows in place would let a stale
    seq leak into a later export's ``captured_through_seq`` reading. "Best-
    effort" because a write transaction that is already in flight when this
    commits, and only commits its own change-log row afterward, is
    invisible to this DELETE -- ``_capture_enable``'s docstring explains why
    the NEXT ``enable`` is where that residual row is guaranteed to be
    swept up instead. The next export after a disable is a full snapshot,
    exactly like the very first export ever taken.
    """
    moment = _moment(source, datetime.now(timezone.utc))
    with source.write() as conn:
        if not source.is_postgres:
            SqliteDatabase.begin_immediate(conn)
        _require_capture_tables(source, conn)
        rows = source.fetch(
            conn,
            source.sql(
                "UPDATE sync_capture_control SET "
                "enabled = ?, "
                "disabled_at = CASE WHEN sync_capture_control.enabled "
                "THEN ? ELSE sync_capture_control.disabled_at END "
                "WHERE singleton = 1 "
                "RETURNING disabled_at"
            ),
            (False, moment),
        )
        if not rows:
            return {
                "already_disabled": True,
                "disabled_at": None,
                "cleared_export_watermarks": 0,
                "cleared_log_rows": 0,
                **_capture_log_snapshot(source, conn),
            }
        disabled_at = rows[0]["disabled_at"]
        already_disabled = disabled_at != moment
        if already_disabled:
            return {
                "already_disabled": True,
                "disabled_at": disabled_at,
                "cleared_export_watermarks": 0,
                "cleared_log_rows": 0,
                **_capture_log_snapshot(source, conn),
            }
        cleared_exports = conn.execute("DELETE FROM sync_export_state").rowcount
        cleared_log = conn.execute("DELETE FROM sync_change_log").rowcount
        return {
            "already_disabled": False,
            "disabled_at": moment,
            "cleared_export_watermarks": cleared_exports,
            "cleared_log_rows": cleared_log,
            "min_seq": None,
            "max_seq": None,
            "log_rows": 0,
            "log_rows_exact": True,
        }


def _cmd_capture_enable(args: argparse.Namespace, settings: Settings) -> int:
    source = _Source(settings, ROOT_DIR)
    try:
        result = _capture_enable(source)
    except SyncCaptureError as exc:
        print(f"sync capture enable: {exc}", file=sys.stderr)
        return 2
    finally:
        source.close()
    if args.as_json:
        _print_json(result)
        return 0
    if result["already_enabled"]:
        print(f"变更捕获已开启（自 {result['enabled_at']} 起），未作改动。")
    else:
        print(f"变更捕获已开启，于 {result['enabled_at']}。")
        print(
            f"已清空导出水位: {result['cleared_export_watermarks']} 条"
            "（此前的全量包早于本次捕获，不能再当增量基线，下一次导出会是全量）。"
        )
        print(f"已清空变更日志: {result['cleared_log_rows']} 行。")
    print(f"变更日志: {_format_log_range(result)}")
    return 0


def _cmd_capture_disable(args: argparse.Namespace, settings: Settings) -> int:
    source = _Source(settings, ROOT_DIR)
    try:
        result = _capture_disable(source)
    except SyncCaptureError as exc:
        print(f"sync capture disable: {exc}", file=sys.stderr)
        return 2
    finally:
        source.close()
    if args.as_json:
        _print_json(result)
        return 0
    if result["already_disabled"]:
        print("变更捕获本来就是关闭的，未作改动。")
    else:
        print(f"变更捕获已关闭，于 {result['disabled_at']}。")
        print(
            f"已清空导出水位: {result['cleared_export_watermarks']} 条、"
            f"变更日志: {result['cleared_log_rows']} 行。"
        )
    return 0


def _cmd_capture_status(args: argparse.Namespace, settings: Settings) -> int:
    try:
        capture = _load_capture_status(settings, exact=args.exact_count)
    except SyncCaptureError as exc:
        print(f"sync capture status: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        _print_json(capture)
        return 0
    _print_capture_status(capture)
    return 0


# ---------------------------------------------------------------- prune-log


class SyncPruneError(RuntimeError):
    """``sync prune-log`` refused to run.

    Distinct from :class:`SyncCaptureError` (missing v84/0064 tables, the
    same diagnosis every other capture-touching command gives) and from a
    plain "nothing to do": this is the retention rule itself refusing,
    because no target's watermark has ``captured = 1`` at all (see
    ``_prune_log_bounds``), or because a ``captured = 1`` watermark carries
    no ``exported_snapshot`` on PostgreSQL, which should never happen and
    would make the txid bound below unsafe to compute if it were tolerated.
    """


def _prune_log_bounds(
    source: _Source, conn: Any, *, lock: bool = False
) -> tuple[int, int | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """``(min_seq, min_xmin, active_leases, dead_leases)`` -- the two upper
    bounds ``_prune_log``'s ``DELETE``/``COUNT`` predicate is built from,
    plus the lease rows that fed (or did not feed) ``min_seq``
    (docs/incremental-sync-design.md §7 "保留策略"/"在途导出租约").

    ``dead_leases`` are classified here but NOT deleted here -- eviction is
    the caller's job (``_prune_log``'s real, non-``dry-run`` branch), because
    it must happen in the SAME locked transaction this function ran in, and
    this function has no opinion on whether its caller intends to write
    anything at all (``--dry-run`` calls it too, read-only). See ``lock``
    below and codex #784 r6 for why "classify" and "evict" being two
    different statements, let alone two different transactions, is exactly
    the bug this shape closes.

    ``lock=True`` -- used by the real run, never by ``--dry-run`` -- takes a
    PostgreSQL ``SELECT ... FOR UPDATE`` over EVERY ``sync_export_runs`` row
    (live and dead alike) before any of the reads below, so this function's
    view of which leases are dead cannot be invalidated by a concurrent lease
    claim or publish (both of which lock their OWN target's row the same
    way) before the caller's ``DELETE`` commits. On SQLite the caller's own
    ``begin_immediate`` already provides the same serialization (one writer
    at a time), so this flag is a no-op there. Checked AFTER
    ``_require_v85_schema`` on purpose: locking a table that might not exist
    would itself raise a raw driver error, and the schema check already
    gives that gap a named diagnosis.

    Checks ``_require_v85_schema`` FIRST, before anything below names
    ``captured``/``exported_snapshot`` or queries ``sync_export_runs``: those
    are v85/0065 additions, and a v83/0063 or v84/0064 database is missing
    some or all of them, which would otherwise surface as a raw "no such
    column"/"no such table" (or PostgreSQL's "... does not exist") driver
    error instead of the named diagnosis :class:`SyncSchemaColumnsError`
    gives.

    The seq bound is taken over **two** sources, each contributing its own
    minimum, and the smaller of the two wins:

    - ``sync_export_state`` rows with ``captured = 1``: a target that has
      never exported, or whose latest export fell back to full with the
      capture gate off, has no change-log window behind its watermark at all
      and simply abstains from voting -- it must not drag ``min_seq`` down to
      some earlier, unrelated number and block pruning for every target that
      DOES have a safe bound (that would be the bug: one stale/never-exported
      target holding retention hostage for the entire log).
    - LIVE ``sync_export_runs`` leases (``not row["dead"]``, see
      ``_load_export_leases``): an export already in flight is reading a
      window that ends at its lease's ``floor_seq``, a lower bound on the
      watermark it has not published yet -- rows above that floor are not
      safe to delete even though no watermark says so yet. A DEAD lease
      (crashed run, or a stale heartbeat that has NOT been refreshed since --
      see below) is excluded from the bound -- it no longer represents work
      in progress -- and is returned, in ``dead_leases`` (now carrying
      ``run_id`` too, not just ``target_env``/``heartbeat_at``), so the real
      run's caller can delete it BY THAT EXACT ``(target_env, run_id)`` pair
      in the same locked transaction (making its expiry irreversible -- a
      stalled export that resumes afterwards finds no lease row at all and
      refuses to publish, the same as if it had been taken over; see
      docs/incremental-sync-design.md §7 "在途导出租约"). Deleting a dead
      lease was NOT always this function's contract: earlier versions only
      EXCLUDED dead leases from the bound and left the row in place, which
      let a stalled export that later resumed and refreshed its own
      unchanged ``heartbeat_at`` look live again to the ownership check a
      publish performs -- passing that check with a snapshot for which this
      very function had already, correctly, allowed the rows it needed to be
      pruned. Deleting the row the moment it is classified dead is what
      closes that window: there is no state left for a resurrection to find.

    ``min_xmin`` is ``None`` on SQLite -- there is no PostgreSQL compensation
    window to bound there (``sync_change_log.txid`` is NULL on every row; see
    docs/incremental-sync-design.md §7 "导出水位"), and no lease ever
    contributes to it there either (``sync_export_runs.floor_xmin`` is NULL
    on every SQLite row, by construction -- there is no txid dimension to
    bound). On PostgreSQL ``min_xmin`` is taken over the SAME two sources as
    ``min_seq``: the captured watermarks' snapshot xmins, and every LIVE
    lease's ``floor_xmin`` (the xmin of the snapshot the lease's own claiming
    transaction saw, taken before the export's read snapshot -- so it is
    ``<=`` that snapshot's xmin, and every transaction still in flight at the
    export's snapshot and committing afterwards has ``txid >= floor_xmin``;
    docs/incremental-sync-design.md §7 "在途导出租约"). A live PostgreSQL
    lease whose ``floor_xmin`` is ``NULL`` -- which should never happen; the
    lease-claiming transaction always reads one -- is NOT skipped or
    defaulted: this function has no safe number to guess, so it raises
    :class:`SyncPruneError` naming every such lease, for as long as that
    lease stays live. A DEAD lease's ``floor_xmin`` (NULL or not) is ignored
    the same way its ``floor_seq`` is.

    Raises :class:`SyncPruneError` when NOT A SINGLE target has a
    ``captured = 1`` watermark: that is the one case with no safe upper bound
    to compute from anything, as opposed to some targets abstaining while
    others (captured watermarks OR live leases) still supply one. A live
    lease alone, with no captured watermark at all, does not lift this
    refusal -- a lease is a bound on what a FUTURE watermark will be, not a
    substitute for one already published, so there would still be nothing
    proven safe to delete.
    """
    _require_v85_schema(source, conn)
    if lock and source.is_postgres:
        # Row locks over EVERY lease (live and dead alike), taken before any
        # read below and held until the caller's transaction commits. A
        # concurrent claim (INSERT ... ON CONFLICT DO UPDATE) or heartbeat
        # UPDATE on any of these rows now waits behind this call, so the
        # dead/live classification and the eviction that follows are one
        # atomic decision. Rows that do not exist yet cannot be locked, but
        # a brand-new lease is by definition live and only raises the bound.
        source.fetch(
            conn,
            "SELECT target_env FROM sync_export_runs ORDER BY target_env FOR UPDATE",
        )
    rows = source.fetch(
        conn,
        source.sql(
            "SELECT exported_through_seq, exported_snapshot FROM sync_export_state "
            "WHERE captured = ?"
        ),
        (True,),
    )
    if not rows:
        raise SyncPruneError(
            "没有任何目标环境的导出水位带 captured=1：没有任何日志行有安全的删除"
            "上限可言——不是某个目标环境的上限不明确，而是根本没有可以依据的水位。"
            "先在变更捕获开着的前提下对至少一个目标环境跑一次导出，建立一个"
            "captured=1 的水位，再重试。"
        )
    min_seq = min(int(row["exported_through_seq"]) for row in rows)
    if source.is_postgres:
        missing_snapshot = [row for row in rows if not row["exported_snapshot"]]
        if missing_snapshot:
            raise SyncPruneError(
                "sync_export_state 中存在 captured=1 但 exported_snapshot 为空的水位行"
                "（数据不一致，不应该出现）：拒绝在不完整的快照信息上做保留判定。"
            )
        min_xmin: int | None = min(
            _Source.snapshot_xmin(str(row["exported_snapshot"])) for row in rows
        )
    else:
        min_xmin = None

    leases = _load_export_leases(source, conn)
    live_leases = [row for row in leases if not row["dead"]]
    active_leases = [
        {"target_env": row["target_env"], "floor_seq": int(row["floor_seq"])}
        for row in live_leases
    ]
    dead_leases = [
        {
            "target_env": row["target_env"],
            "run_id": row["run_id"],
            "heartbeat_at": row["heartbeat_at"],
        }
        for row in leases
        if row["dead"]
    ]
    if active_leases:
        min_seq = min(min_seq, min(row["floor_seq"] for row in active_leases))
    if source.is_postgres and live_leases:
        missing_floor_xmin = [
            row for row in live_leases if row.get("floor_xmin") is None
        ]
        if missing_floor_xmin:
            names = "、".join(
                sorted(str(row["target_env"]) for row in missing_floor_xmin)
            )
            raise SyncPruneError(
                f"以下目标环境的在途导出租约没有 floor_xmin（target: {names}）：这不应该出现"
                "（只有旧版本写下的租约行才会这样），拒绝在缺失信息上猜测一个 txid 下界。"
                "等这次导出发布水位、或它的心跳超过一小时被判定为死租约后再重试。"
            )
        min_xmin = min(
            [min_xmin, *(int(row["floor_xmin"]) for row in live_leases)]
        )
    return min_seq, min_xmin, active_leases, dead_leases


def _prune_log_clause(
    source: _Source, min_seq: int, min_xmin: int | None, threshold: Any
) -> tuple[str, list[Any]]:
    """The shared ``WHERE`` clause and its bind params for both the
    ``--dry-run`` ``COUNT`` and the real ``DELETE``, so the two can never
    drift apart into counting one thing and deleting another.

    The ``txid`` condition is included only on PostgreSQL: ``sync_change_log
    .txid`` is NULL on every SQLite row, and SQL's ``NULL < x`` is never
    true, so a ``txid < ?`` clause included unconditionally would silently
    refuse to delete anything on SQLite rather than doing what the seq/
    changed_at conditions alone already correctly decide there.
    """
    parts = ["seq <= ?"]
    params: list[Any] = [min_seq]
    if source.is_postgres:
        parts.append("txid < ?")
        params.append(min_xmin)
    parts.append("changed_at < ?")
    params.append(threshold)
    return " AND ".join(parts), params


# Rows per DELETE batch. A constant, not tunable from the CLI: this is an
# implementation detail of how the delete is chunked, not a retention
# parameter (that is --keep-days). 5000 keeps one batch's transaction short
# on a log that has gone unpruned for a long time, at the cost of more round
# trips than a single unbounded DELETE -- the right tradeoff for a command
# that is run interactively/from cron, not on a hot path.
_PRUNE_LOG_BATCH_SIZE = 5000


def _prune_log_delete_batch(
    source: _Source, clause: str, params: Sequence[Any]
) -> int:
    """Delete UP TO ``_PRUNE_LOG_BATCH_SIZE`` rows matching ``clause`` and
    return how many it actually deleted, in its OWN transaction (SQLite:
    ``write()`` + ``begin_immediate``, the same convention every other
    guarded write in this module uses; PostgreSQL: ``write()``'s own
    transaction).

    ``DELETE ... WHERE seq IN (SELECT seq FROM ... WHERE <clause> ORDER BY
    seq LIMIT N)`` rather than a bare ``DELETE ... WHERE <clause> LIMIT N``
    because neither backend's ``DELETE`` supports ``LIMIT`` directly (SQLite
    only with a compile-time option this codebase does not rely on;
    PostgreSQL never) -- the subquery picks the rows, the outer statement
    deletes exactly those.
    """
    with source.write() as conn:
        if not source.is_postgres:
            SqliteDatabase.begin_immediate(conn)
        cursor = conn.execute(
            source.sql(
                "DELETE FROM sync_change_log WHERE seq IN ("
                "SELECT seq FROM sync_change_log WHERE "
                f"{clause} ORDER BY seq LIMIT {_PRUNE_LOG_BATCH_SIZE})"
            ),
            tuple(params),
        )
        return int(cursor.rowcount)


def _prune_log(
    source: _Source, *, keep_days: int, dry_run: bool
) -> dict[str, Any]:
    """Delete (or, with ``dry_run``, count) ``sync_change_log`` rows no
    future export could still need, and evict any DEAD ``sync_export_runs``
    lease found along the way (docs/incremental-sync-design.md §7
    "保留策略"/"在途导出租约"; codex #784 r6).

    ``--dry-run`` reads only, in ``source.read()``: bounds are computed by
    ``_prune_log_bounds(..., lock=False)``, dead leases are named (they would
    be evicted by a real run) but never deleted, and the row count is one
    ``COUNT(*)`` against the same clause a real run's batches would use.

    The REAL run computes the bounds AND evicts every dead lease it found
    inside ONE locked write transaction: SQLite takes ``begin_immediate``,
    PostgreSQL locks every ``sync_export_runs`` row with ``FOR UPDATE``
    (``_prune_log_bounds(..., lock=True)``) before reading anything. This is
    what makes a dead lease's expiry irreversible instead of racy: without
    the lock, a lease this call sees as dead (heartbeat stale for over an
    hour) could be refreshed by the stalled export resuming, moments after
    this call reads it and before it deletes it -- the ownership check a
    later publish performs would then find a live-looking, unmodified lease
    row and let that export publish a watermark built from a snapshot whose
    compensation window this very call already excluded from the bound (and
    may already have pruned rows out of). Locking BEFORE reading, and
    deleting each dead lease by its exact ``(target_env, run_id)`` in the
    SAME transaction the classification ran in, closes that gap: either this
    transaction's lock wins and the lease is gone before the stalled export's
    own lock-and-verify at publish time can see it, or the export's lock
    (claim or publish) wins and holds the row until it commits, so this call
    (which acquires its lock afterward) reads a state that already reflects
    whatever that export just did. There is no interleaving where both see a
    stale, still-there row. The eviction ``DELETE`` matches ``(target_env,
    run_id)`` ALONE -- it does not repeat the ``heartbeat_at`` staleness
    check -- because the lock already guarantees nothing else touched this
    row between the classification and the delete; re-deriving "is it dead"
    a second time from a fresh clock read would let the two computations
    (which rows were EXCLUDED from the bound vs. which rows get DELETED)
    disagree over something as fragile as which side of a moving cutoff
    "now" falls on, and they must always be exactly the same set.

    The bounds (``min_seq``/``min_xmin``) themselves are computed ONCE, not
    re-derived per change-log batch below. That is safe, not merely
    convenient: both bounds only ever move in the direction that WIDENS what
    may legally be deleted (a later, more-advanced ``captured=1`` watermark
    raises ``min_seq``/``min_xmin``; nothing ever lowers them once written --
    see ``_advance_watermark``'s own monotonic clamp). A bound fixed at the
    start of this call is therefore never less safe than one recomputed
    mid-run; at worst a concurrent export that advances a watermark while
    this command is still deleting simply leaves a few more now-safe-to-
    delete rows for the NEXT ``prune-log`` to catch, which is the same "safe
    to under-delete, never to over-delete" property a single bounds-
    computing transaction has, regardless of how many separate transactions
    the actual row deletions run in afterward.

    The real run's change-log deletion is therefore still CHUNKED, and still
    OUTSIDE the bounds-and-eviction transaction (it does not need that lock
    -- ``sync_change_log`` rows are never claimed by a lease):
    ``_prune_log_delete_batch`` runs in its own transaction, repeatedly,
    until a batch deletes fewer than ``_PRUNE_LOG_BATCH_SIZE`` rows (the
    signal that nothing matching is left) or zero (nothing did). Row counts
    accumulate across batches into the returned ``deleted``; ``batches``
    counts how many non-empty batches ran, so an operator watching a large
    prune can see it make progress rather than one command blocking for an
    unknown time.
    """
    threshold = _moment(
        source, datetime.now(timezone.utc) - timedelta(days=keep_days)
    )

    if dry_run:
        with source.read() as conn:
            _require_capture_tables(source, conn)
            min_seq, min_xmin, active_leases, dead_leases = _prune_log_bounds(
                source, conn
            )
            clause, params = _prune_log_clause(source, min_seq, min_xmin, threshold)
            count = source.fetch(
                conn,
                source.sql(f"SELECT COUNT(*) AS n FROM sync_change_log WHERE {clause}"),
                params,
            )[0]["n"]
        return {
            "dry_run": True,
            "deleted": 0,
            "would_delete": int(count),
            "batches": 0,
            "keep_days": keep_days,
            "min_seq": min_seq,
            "min_txid": min_xmin,
            "active_leases": active_leases,
            "dead_leases": dead_leases,
        }

    with source.write() as conn:
        if not source.is_postgres:
            SqliteDatabase.begin_immediate(conn)
        _require_capture_tables(source, conn)
        min_seq, min_xmin, active_leases, dead_leases = _prune_log_bounds(
            source, conn, lock=True
        )
        for row in dead_leases:
            conn.execute(
                source.sql(
                    "DELETE FROM sync_export_runs WHERE target_env = ? AND run_id = ?"
                ),
                (row["target_env"], row["run_id"]),
            )
    clause, params = _prune_log_clause(source, min_seq, min_xmin, threshold)

    deleted = 0
    batches = 0
    while True:
        batch_deleted = _prune_log_delete_batch(source, clause, params)
        if batch_deleted == 0:
            break
        deleted += batch_deleted
        batches += 1
        if batch_deleted < _PRUNE_LOG_BATCH_SIZE:
            break
    return {
        "dry_run": False,
        "deleted": deleted,
        "would_delete": 0,
        "batches": batches,
        "keep_days": keep_days,
        "min_seq": min_seq,
        "min_txid": min_xmin,
        "active_leases": active_leases,
        "dead_leases": dead_leases,
    }


def _cmd_prune_log(args: argparse.Namespace, settings: Settings) -> int:
    source = _Source(settings, ROOT_DIR)
    try:
        result = _prune_log(source, keep_days=args.keep_days, dry_run=args.dry_run)
    except (SyncCaptureError, SyncPruneError, SyncSchemaColumnsError) as exc:
        print(f"sync prune-log: {exc}", file=sys.stderr)
        return 2
    finally:
        source.close()
    if args.as_json:
        _print_json(result)
        return 0
    if result["dry_run"]:
        print(f"预检（--dry-run）：会删除 {result['would_delete']} 行，不会真的删除。")
    else:
        print(f"已删除 {result['deleted']} 行（{result['batches']} 批）。")
    bound_note = f"seq<={result['min_seq']}"
    if result["min_txid"] is not None:
        bound_note += f"，txid<{result['min_txid']}"
    print(f"保留天数: {result['keep_days']} 天；删除上限: {bound_note}")
    if result["active_leases"]:
        parts = "；".join(
            f"target {row['target_env']}, floor_seq {row['floor_seq']}"
            for row in result["active_leases"]
        )
        print(f"在途导出：{len(result['active_leases'])} 个（{parts}）")
    if result["dead_leases"]:
        parts = "；".join(
            f"target {row['target_env']}, run {row['run_id']}"
            for row in result["dead_leases"]
        )
        if result["dry_run"]:
            print(
                f"死租约：{len(result['dead_leases'])} 个（{parts}）——未参与本次判据，"
                "真的执行（去掉 --dry-run）时会被删除"
            )
        else:
            print(f"已删除死租约：{len(result['dead_leases'])} 个（{parts}）")
    return 0


# --------------------------------------------------------------------- CLI


def _keep_days(value: str) -> int:
    """``argparse`` ``type=`` for ``--keep-days``: a non-negative integer, or
    a usage error (argparse turns ``ArgumentTypeError`` into an ``exit(2)``
    with a message naming the bad value, before ``_cmd_prune_log`` ever
    runs) -- a negative value would build ``now() - N days`` INTO THE
    FUTURE, silently keeping every row regardless of age instead of the
    "too young to prune" floor ``--keep-days`` is supposed to be."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--keep-days 必须是非负整数，得到 {value!r}"
        ) from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"--keep-days 必须是非负整数，得到 {value!r}")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync_notebooks.py",
        description="跨环境笔记本内容同步：导出、引入与水位查询"
        "（docs/incremental-sync-design.md）。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="导出笔记本内容到同步包")
    export_parser.add_argument("--target", required=True, help="目标环境标识")
    export_parser.add_argument("--out", required=True, help="导出包所在目录")
    export_parser.add_argument(
        "--notebook",
        action="append",
        default=None,
        help="限定笔记本 id（可重复传递）；省略则导出全部存活、非镜像笔记本",
    )
    export_parser.add_argument(
        "--source-env",
        default=None,
        help="源环境标识；省略则取 SILICON_NOTEBOOK_SYNC_ENV",
    )
    export_parser.add_argument(
        "--full",
        action="store_true",
        help="强制全量导出，即使该 target 有可用的增量水位；仍会正常推进水位"
        "（与 --notebook 同时给出时，--notebook 的规则优先：永不推进水位）",
    )
    export_parser.add_argument("--json", action="store_true", dest="as_json")
    export_parser.set_defaults(handler=_cmd_export)

    import_parser = subparsers.add_parser("import", help="把同步包引入本环境")
    import_parser.add_argument("package_dir", help="同步包目录")
    import_parser.add_argument(
        "--create-missing-users",
        action="store_true",
        help="源端用户按 username 在目标端找不到时，创建无凭据本地用户",
    )
    import_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只做预检与身份映射并输出报告，不写入任何数据",
    )
    import_parser.add_argument(
        "--importer-user",
        default=None,
        help="执行导入的用户 id，用于映射不到时的兜底 created_by/actor",
    )
    import_parser.add_argument(
        "--resume",
        action="store_true",
        help="显式续跑同一个包的中断导入；缺省时，同一 source_env 已有 running 的"
        "导入会被拒绝，防止误把两个不相关的包接到一起",
    )
    import_parser.add_argument(
        "--verify-files",
        action="store_true",
        dest="verify_files",
        help="导入每个文件时二次校验 sha256，而不是只信任包的 checksums.json",
    )
    import_parser.add_argument(
        "--take-over",
        action="store_true",
        dest="take_over",
        help="显式接管同一 source_env 一个仍是 running 的导入；仅当已经确认那个"
        "进程真的死了才用——用 `sync status` 看该行的心跳（heartbeat_at）判断，"
        "不要凭经过的时间猜测",
    )
    import_parser.add_argument("--json", action="store_true", dest="as_json")
    import_parser.set_defaults(handler=_cmd_import)

    status_parser = subparsers.add_parser(
        "status", help="查看本环境的导出水位与已引入的包"
    )
    status_parser.add_argument("--json", action="store_true", dest="as_json")
    status_parser.add_argument(
        "--exact-count",
        action="store_true",
        dest="exact_count",
        help="变更日志按 COUNT(*) 精确计数，而不是默认的近似值"
        "（会全表扫描，大库慎用）",
    )
    status_parser.set_defaults(handler=_cmd_status)

    capture_parser = subparsers.add_parser(
        "capture", help="开关/查看本环境的源端变更捕获"
    )
    capture_subparsers = capture_parser.add_subparsers(
        dest="capture_command", required=True
    )

    capture_enable_parser = capture_subparsers.add_parser(
        "enable",
        help="开启变更捕获；首次开启会清空本环境的导出水位与变更日志"
        "（下一次导出会是全量）",
    )
    capture_enable_parser.add_argument("--json", action="store_true", dest="as_json")
    capture_enable_parser.set_defaults(handler=_cmd_capture_enable)

    capture_disable_parser = capture_subparsers.add_parser(
        "disable", help="关闭变更捕获，并清空导出水位与变更日志"
    )
    capture_disable_parser.add_argument("--json", action="store_true", dest="as_json")
    capture_disable_parser.set_defaults(handler=_cmd_capture_disable)

    capture_status_parser = capture_subparsers.add_parser(
        "status", help="查看变更捕获的开关状态与日志行数"
    )
    capture_status_parser.add_argument("--json", action="store_true", dest="as_json")
    capture_status_parser.add_argument(
        "--exact-count",
        action="store_true",
        dest="exact_count",
        help="按 COUNT(*) 精确计数，而不是默认的近似值（会全表扫描，大库慎用）",
    )
    capture_status_parser.set_defaults(handler=_cmd_capture_status)

    prune_log_parser = subparsers.add_parser(
        "prune-log",
        help="清理不再被任何目标环境的 captured 水位需要的变更日志行"
        "（docs/incremental-sync-design.md §7「保留策略」）",
    )
    prune_log_parser.add_argument(
        "--keep-days",
        type=_keep_days,
        default=30,
        help="早于此天数（按 changed_at）的日志行才会被删除，默认 30；必须是非负整数",
    )
    prune_log_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只报告会删除多少行，不真的删除",
    )
    prune_log_parser.add_argument("--json", action="store_true", dest="as_json")
    prune_log_parser.set_defaults(handler=_cmd_prune_log)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = Settings()
        return int(args.handler(args, settings))
    except KeyboardInterrupt:
        print(f"sync {args.command}: interrupted", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - every failure is reported, never a raw traceback
        if os.environ.get("SILICON_NOTEBOOK_DEBUG"):
            raise
        print(f"sync {args.command}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

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
from app.migration.sync.import_ import SyncImportError, _moment, import_package
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
        "notebooks": list(report.notebooks),
        "skipped": dict(sorted(report.skipped.items())),
        "table_counts": dict(sorted(report.table_counts.items())),
        "file_count": report.file_count,
        "bytes_written": report.bytes_written,
        "mode": report.mode,
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
    print(f"模式: {report.mode}")
    print(f"序号区间: from_seq={report.from_seq}, to_seq={report.to_seq}")
    if report.base_package_id:
        print(f"续接自包: {report.base_package_id}")
    print(f"本次快照捕获到的变更日志水位: seq={report.captured_through_seq}")
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
    """
    exports_sql = (
        "SELECT target_env, exported_through_seq, exported_at, package_id, "
        "captured, exported_snapshot FROM sync_export_state ORDER BY target_env"
    )
    imports_sql = (
        "SELECT package_id, source_env, status, started_at, finished_at, "
        "report_json FROM sync_imports ORDER BY started_at DESC"
    )
    source = _Source(settings, ROOT_DIR)
    try:
        with source.read() as conn:
            missing = _missing_sync_tables(conn, is_postgres=source.is_postgres)
            if missing:
                raise SyncStatusError(
                    "本环境尚未迁移到 v83/0063（缺表: " + "、".join(sorted(missing)) + "）；"
                    "先启动一次后端完成 schema 迁移，或手动执行相应迁移脚本，再重试。"
                )
            exports = source.fetch(conn, exports_sql)
            imports = source.fetch(conn, imports_sql)
    finally:
        source.close()
    for row in exports:
        row["captured"] = bool(row["captured"])
    for row in imports:
        report = row.get("report_json")
        if isinstance(report, str):
            try:
                report = json.loads(report)
            except (TypeError, ValueError):
                report = {}
        row["report_json"] = report or {}
        row["notebooks"] = len(row["report_json"].get("notebooks", []) or [])
    return {"exports": exports, "imports": imports}


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
    if not state["exports"]:
        print("  （无）")
    for row in state["exports"]:
        snapshot_note = ""
        if row["exported_snapshot"]:
            xmin = _Source.snapshot_xmin(str(row["exported_snapshot"]))
            snapshot_note = f"，snapshot xmin={xmin}"
        captured_note = "true" if row["captured"] else "false"
        print(
            f"  -> {row['target_env']}: seq={row['exported_through_seq']} "
            f"于 {row['exported_at']}（包 {row['package_id']}，captured="
            f"{captured_note}{snapshot_note}）"
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


def _prune_log_bounds(source: _Source, conn: Any) -> tuple[int, int | None]:
    """``(min_seq, min_xmin)`` -- the two upper bounds ``_prune_log``'s
    ``DELETE``/``COUNT`` predicate is built from
    (docs/incremental-sync-design.md §7 "保留策略").

    Both are taken **only** over ``sync_export_state`` rows with
    ``captured = 1``: a target that has never exported, or whose latest
    export fell back to full with the capture gate off, has no change-log
    window behind its watermark at all and simply abstains from voting on
    either bound -- it must not drag ``min_seq`` down to some earlier,
    unrelated number and block pruning for every target that DOES have a
    safe bound (that would be the bug: one stale/never-exported target
    holding retention hostage for the entire log).

    ``min_xmin`` is ``None`` on SQLite -- there is no PostgreSQL compensation
    window to bound there (``sync_change_log.txid`` is NULL on every row; see
    docs/incremental-sync-design.md §7 "导出水位"). Raises
    :class:`SyncPruneError` when NOT A SINGLE target has a ``captured = 1``
    watermark: that is the one case with no safe upper bound to compute from
    anything, as opposed to some targets abstaining while others still
    supply a bound.
    """
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
    if not source.is_postgres:
        return min_seq, None
    missing_snapshot = [row for row in rows if not row["exported_snapshot"]]
    if missing_snapshot:
        raise SyncPruneError(
            "sync_export_state 中存在 captured=1 但 exported_snapshot 为空的水位行"
            "（数据不一致，不应该出现）：拒绝在不完整的快照信息上做保留判定。"
        )
    min_xmin = min(
        _Source.snapshot_xmin(str(row["exported_snapshot"])) for row in rows
    )
    return min_seq, min_xmin


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


def _prune_log(
    source: _Source, *, keep_days: int, dry_run: bool
) -> dict[str, Any]:
    """Delete (or, with ``dry_run``, count) ``sync_change_log`` rows no
    future export could still need (docs/incremental-sync-design.md §7
    "保留策略").

    One transaction either way: the watermark bounds are read and acted on
    (deleted, or counted) inside the SAME transaction, so a concurrent
    export that advances a watermark mid-run cannot leave this command
    computing a bound from a state that no longer holds by the time it acts.
    ``--dry-run`` uses ``source.read()`` (a consistent snapshot, nothing
    written); a real run uses ``source.write()`` with
    ``SqliteDatabase.begin_immediate`` first on SQLite, the same convention
    ``_capture_enable``/``_capture_disable`` use for a guarded write.
    """
    threshold = _moment(
        source, datetime.now(timezone.utc) - timedelta(days=keep_days)
    )
    if dry_run:
        with source.read() as conn:
            _require_capture_tables(source, conn)
            min_seq, min_xmin = _prune_log_bounds(source, conn)
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
            "keep_days": keep_days,
            "min_seq": min_seq,
            "min_txid": min_xmin,
        }

    with source.write() as conn:
        if not source.is_postgres:
            SqliteDatabase.begin_immediate(conn)
        _require_capture_tables(source, conn)
        min_seq, min_xmin = _prune_log_bounds(source, conn)
        clause, params = _prune_log_clause(source, min_seq, min_xmin, threshold)
        cursor = conn.execute(
            source.sql(f"DELETE FROM sync_change_log WHERE {clause}"), tuple(params)
        )
        deleted = cursor.rowcount
    return {
        "dry_run": False,
        "deleted": int(deleted),
        "would_delete": 0,
        "keep_days": keep_days,
        "min_seq": min_seq,
        "min_txid": min_xmin,
    }


def _cmd_prune_log(args: argparse.Namespace, settings: Settings) -> int:
    source = _Source(settings, ROOT_DIR)
    try:
        result = _prune_log(source, keep_days=args.keep_days, dry_run=args.dry_run)
    except (SyncCaptureError, SyncPruneError) as exc:
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
        print(f"已删除 {result['deleted']} 行。")
    bound_note = f"seq<={result['min_seq']}"
    if result["min_txid"] is not None:
        bound_note += f"，txid<{result['min_txid']}"
    print(f"保留天数: {result['keep_days']} 天；删除上限: {bound_note}")
    return 0


# --------------------------------------------------------------------- CLI


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
        type=int,
        default=30,
        help="早于此天数（按 changed_at）的日志行才会被删除，默认 30",
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

"""Operator CLI for cross-environment notebook sync
(docs/incremental-sync-design.md §8).

Three subcommands, each a thin wrapper over this package's already-reviewed
engines -- ``export`` calls :func:`app.migration.sync.export.export_notebooks`,
``import`` calls :func:`app.migration.sync.import_.import_package`, and
``status`` reads the two adapter-internal bookkeeping tables
(``sync_export_state``, ``sync_imports``) those engines maintain. This module
adds no sync logic of its own: argument parsing, exit codes, and human/JSON
report rendering only.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.core.config import Settings
from app.core.database_url import database_identity
from app.migration.sync.export import ExportReport, SyncExportError, export_notebooks
from app.migration.sync.import_ import SyncImportError, import_package
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.sqlite.database import SqliteDatabase

ROOT_DIR = Path(__file__).resolve().parents[4]

_SYNC_TABLES = ("sync_export_state", "sync_imports")


class SyncStatusError(RuntimeError):
    """``status`` could not read the adapter-internal bookkeeping tables --
    distinct from a generic query failure so a database that predates
    v83/0063_sync_control_tables.sql gets a named diagnosis instead of a raw
    "no such table"/"relation does not exist" driver error."""


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
    print(f"笔记本: {len(report.notebooks)} 个")
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

    Every statement issued here is a plain ``SELECT`` -- nothing is written --
    but, unlike ``export.py``'s ``_Source.read()``, the connection is not
    opened inside an explicit ``READ ONLY`` transaction: this is a short
    status query against adapter-internal bookkeeping tables, not part of
    either sync engine's own read/write contract, so it does not need that
    engine's consistent-snapshot guarantee.

    TODO(PR-3): once the change-log triggers land, this and ``_Source``'s
    read-connection setup should probably share one small connection facade
    instead of each hand-rolling the PostgreSQL/SQLite branch (tracked in
    docs/incremental-sync-design.md).
    """
    is_postgres = database_identity(settings.database_url).scheme == "postgresql"
    exports_sql = (
        "SELECT target_env, exported_through_seq, exported_at, package_id "
        "FROM sync_export_state ORDER BY target_env"
    )
    imports_sql = (
        "SELECT package_id, source_env, status, started_at, finished_at, "
        "report_json FROM sync_imports ORDER BY started_at DESC"
    )

    def _read(conn: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        missing = _missing_sync_tables(conn, is_postgres=is_postgres)
        if missing:
            raise SyncStatusError(
                "本环境尚未迁移到 v83/0063（缺表: " + "、".join(sorted(missing)) + "）；"
                "先启动一次后端完成 schema 迁移，或手动执行相应迁移脚本，再重试。"
            )
        exports = [dict(row) for row in conn.execute(exports_sql).fetchall()]
        imports = [dict(row) for row in conn.execute(imports_sql).fetchall()]
        return exports, imports

    if is_postgres:
        database: Any = PostgresDatabase(settings, ROOT_DIR)
        try:
            with database.connect() as conn:
                exports, imports = _read(conn)
        finally:
            database.close()
    else:
        database = SqliteDatabase(settings, ROOT_DIR)
        try:
            exports, imports = _read(database.connect())
        finally:
            database.close()
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
    if args.as_json:
        _print_json(state)
        return 0
    print("导出水位（本环境作为源）：")
    if not state["exports"]:
        print("  （无）")
    for row in state["exports"]:
        print(
            f"  -> {row['target_env']}: seq={row['exported_through_seq']} "
            f"于 {row['exported_at']}（包 {row['package_id']}）"
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
    status_parser.set_defaults(handler=_cmd_status)

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
